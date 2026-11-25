"""Telegram notifier: send only, plain text, redacted, deduped and rate capped.

The bot token lives in the request URL, so every text that leaves this module
(messages, events, errors) goes through the Redactor. No parse_mode is sent, so
message text is never interpreted as markup. Send failures return False and
never raise.

    python -m live.telegram_notifier --test     # send one test message
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from core.secret_store import Redactor

ROOT = Path(__file__).resolve().parent.parent
API_BASE = "https://api.telegram.org"
TAG = "[tradingbot paper]"
MAX_TEXT_CHARS = 3500
TIMEOUT_SEC = 10.0
KINDS = ("summary", "order", "error", "session", "health")
ERROR_COOLDOWN_SEC = 900
DEFAULT_COOLDOWN_SEC = 0
CAP_PER_HOUR = 20
CAP_WINDOW_SEC = 3600


@dataclass(frozen=True)
class TelegramConfig:
    token: str = field(default="", repr=False)
    chat_id: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    @classmethod
    def from_env(cls) -> TelegramConfig:
        return cls(
            token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        )


class TelegramNotifier:
    def __init__(
        self,
        config: TelegramConfig,
        *,
        source: str,
        storage: Any = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.config = config
        self.source = source
        self.storage = storage
        self._transport = transport
        self._clock = clock or time.time
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", source) or "unknown"
        self._state_path = state_path or ROOT / "logs" / f"telegram_state_{safe}.json"
        self._redactor = Redactor([config.token])
        self._disabled_logged = False

    # --- events ---

    def _event(self, level: str, message: str) -> None:
        if self.storage is None:
            return
        try:
            self.storage.log_event(level, self.source, self._redactor.redact(message))
        except Exception:
            pass

    # --- state ---

    def _load_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._state_path.read_text())
            if isinstance(raw, dict):
                return {
                    "last_sent": {
                        str(k): float(v) for k, v in dict(raw.get("last_sent", {})).items()
                    },
                    "sent": [float(t) for t in list(raw.get("sent", []))],
                    "suppressed": int(raw.get("suppressed", 0)),
                }
        except Exception:
            pass
        return {"last_sent": {}, "sent": [], "suppressed": 0}

    def _save_state(self, state: dict[str, Any]) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_name(self._state_path.name + ".tmp")
            tmp.write_text(json.dumps(state))
            os.replace(tmp, self._state_path)
        except Exception:
            pass

    def clear_dedupe_prefix(self, prefix: str) -> None:
        """Forget the persisted cooldown of every dedupe key that starts with `prefix`.

        Never raises. Used after --reset-lockout so the next lockout is announced."""
        try:
            state = self._load_state()
            kept = {k: v for k, v in state["last_sent"].items() if not k.startswith(prefix)}
            if len(kept) != len(state["last_sent"]):
                state["last_sent"] = kept
                self._save_state(state)
        except Exception:
            pass

    # --- sending ---

    def send(
        self,
        kind: str,
        text: str,
        *,
        dedupe_key: str | None = None,
        cooldown_sec: float | None = None,
    ) -> bool:
        try:
            return self._send(kind, text, dedupe_key, cooldown_sec)
        except Exception as exc:  # never raise out of a notifier
            self._event("warn", f"telegram send failed: {type(exc).__name__}")
            return False

    def _send(
        self,
        kind: str,
        text: str,
        dedupe_key: str | None,
        cooldown_sec: float | None,
    ) -> bool:
        if not self.config.enabled:
            if not self._disabled_logged:
                self._disabled_logged = True
                self._event("info", "telegram disabled: TELEGRAM_BOT_TOKEN or CHAT_ID not set")
            return False

        now = self._clock()
        state = self._load_state()
        body = self._redactor.redact(text)

        if dedupe_key is None and kind == "error":
            dedupe_key = f"error:{body[:80]}"
        if cooldown_sec is None:
            cooldown_sec = ERROR_COOLDOWN_SEC if kind == "error" else DEFAULT_COOLDOWN_SEC
        if dedupe_key is not None and cooldown_sec > 0:
            last = state["last_sent"].get(dedupe_key)
            if last is not None and now - last < cooldown_sec:
                return False

        state["sent"] = [t for t in state["sent"] if now - t < CAP_WINDOW_SEC]
        if len(state["sent"]) >= CAP_PER_HOUR:
            state["suppressed"] += 1
            self._save_state(state)
            return False

        prefix = f"{TAG} {kind}: "
        if state["suppressed"]:
            n = state["suppressed"]
            word = "alert" if n == 1 else "alerts"
            prefix += f"({n} {word} suppressed) "
        message = (prefix + body)[:MAX_TEXT_CHARS]

        if not self._post(message):
            return False

        state["sent"].append(now)
        state["suppressed"] = 0
        if dedupe_key is not None:
            state["last_sent"][dedupe_key] = now
        cutoff = now - max(cooldown_sec, CAP_WINDOW_SEC) * 24
        state["last_sent"] = {k: v for k, v in state["last_sent"].items() if v >= cutoff}
        self._save_state(state)
        return True

    def _post(self, message: str) -> bool:
        url = f"{API_BASE}/bot{self.config.token}/sendMessage"
        try:
            with httpx.Client(transport=self._transport, timeout=TIMEOUT_SEC) as client:
                resp = client.post(url, data={"chat_id": self.config.chat_id, "text": message})
        except Exception as exc:
            self._event("warn", f"telegram send failed: {type(exc).__name__}: {exc}")
            return False
        if resp.status_code != 200:
            self._event("warn", f"telegram send failed: HTTP {resp.status_code}")
            return False
        return True


def main(
    argv: list[str] | None = None,
    *,
    notifier_factory: Callable[[], Any] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Telegram notifier")
    parser.add_argument("--test", action="store_true", help="send one test message")
    args = parser.parse_args(argv)
    if not args.test:
        parser.print_help()
        return 2
    if notifier_factory is None:

        def notifier_factory() -> TelegramNotifier:
            return TelegramNotifier(TelegramConfig.from_env(), source="cli")

    notifier = notifier_factory()
    ok = notifier.send("health", "test message", dedupe_key="cli_test", cooldown_sec=0)
    print("telegram test message sent" if ok else "telegram test message NOT sent")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
