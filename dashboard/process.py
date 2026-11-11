"""
Subprocess manager for the dashboard.

The dashboard runs as its own process, separate from the bot. To "start the bot"
from a button click we spawn `python -m live.runner ...` as a subprocess and
keep its PID in a small JSON file so the dashboard survives restarts.

Scope: this is a home-server control surface, not a multi-tenant orchestrator.
One runner per symbol at a time. Phase 5 swaps this out for systemd units.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.config import LOGS_DIR, ROOT


STATE_FILE = LOGS_DIR / "processes.json"


@dataclass
class ProcInfo:
    name: str  # "runner:SPY" or "train:SPY:xgboost"
    pid: int
    cmd: list[str]
    started_at: float
    log_path: str


def _load_state() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict[str, dict]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def list_processes() -> list[ProcInfo]:
    """Return all tracked processes, pruning entries whose PID is gone."""
    state = _load_state()
    out: list[ProcInfo] = []
    changed = False
    for name, entry in list(state.items()):
        if not _pid_alive(entry["pid"]):
            del state[name]
            changed = True
            continue
        out.append(ProcInfo(name=name, **entry))
    if changed:
        _save_state(state)
    return out


def get_process(name: str) -> Optional[ProcInfo]:
    for p in list_processes():
        if p.name == name:
            return p
    return None


def start_runner(
    symbol: str, model: str, *, dry_run: bool = True, quantity: float = 1.0
) -> ProcInfo:
    name = f"runner:{symbol}"
    if get_process(name) is not None:
        raise RuntimeError(f"Runner already active for {symbol}")
    log_path = LOGS_DIR / f"runner_{symbol}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "live.runner",
        "--symbol",
        symbol,
        "--model",
        model,
        "--quantity",
        str(quantity),
    ]
    cmd.append("--dry-run" if dry_run else "--live")
    log_fh = open(log_path, "a", buffering=1)
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    info = ProcInfo(
        name=name,
        pid=proc.pid,
        cmd=cmd,
        started_at=time.time(),
        log_path=str(log_path),
    )
    state = _load_state()
    state[name] = {
        "pid": info.pid,
        "cmd": info.cmd,
        "started_at": info.started_at,
        "log_path": info.log_path,
    }
    _save_state(state)
    return info


def start_cpgw_keeper() -> ProcInfo:
    name = "cpgw_keeper"
    if get_process(name) is not None:
        raise RuntimeError("CPGW keeper already active")
    log_path = LOGS_DIR / "cpgw_keeper.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "live.cpgw_keeper"]
    log_fh = open(log_path, "a", buffering=1)
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    info = ProcInfo(
        name=name,
        pid=proc.pid,
        cmd=cmd,
        started_at=time.time(),
        log_path=str(log_path),
    )
    state = _load_state()
    state[name] = {
        "pid": info.pid,
        "cmd": info.cmd,
        "started_at": info.started_at,
        "log_path": info.log_path,
    }
    _save_state(state)
    return info


def start_sentiment_worker() -> ProcInfo:
    name = "sentiment"
    if get_process(name) is not None:
        raise RuntimeError("Sentiment worker already active")
    log_path = LOGS_DIR / "sentiment.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "live.sentiment_worker"]
    log_fh = open(log_path, "a", buffering=1)
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    info = ProcInfo(
        name=name,
        pid=proc.pid,
        cmd=cmd,
        started_at=time.time(),
        log_path=str(log_path),
    )
    state = _load_state()
    state[name] = {
        "pid": info.pid,
        "cmd": info.cmd,
        "started_at": info.started_at,
        "log_path": info.log_path,
    }
    _save_state(state)
    return info


def start_portfolio(*, dry_run: bool = True) -> ProcInfo:
    name = "portfolio"
    if get_process(name) is not None:
        raise RuntimeError("Portfolio runner already active")
    log_path = LOGS_DIR / "portfolio.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "live.portfolio_runner"]
    cmd.append("--dry-run" if dry_run else "--live")
    log_fh = open(log_path, "a", buffering=1)
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    info = ProcInfo(
        name=name,
        pid=proc.pid,
        cmd=cmd,
        started_at=time.time(),
        log_path=str(log_path),
    )
    state = _load_state()
    state[name] = {
        "pid": info.pid,
        "cmd": info.cmd,
        "started_at": info.started_at,
        "log_path": info.log_path,
    }
    _save_state(state)
    return info


def start_training(symbol: str, kind: str = "xgboost", classifier: str = "multiclass") -> ProcInfo:
    name = f"train:{symbol}:{kind}"
    if get_process(name) is not None:
        raise RuntimeError(f"Training already active: {name}")
    log_path = LOGS_DIR / f"train_{symbol}_{kind}.log"
    cmd = [
        sys.executable,
        "-m",
        "scripts.train_symbol",
        "--symbol",
        symbol,
        "--kind",
        kind,
        "--classifier",
        classifier,
    ]
    log_fh = open(log_path, "a", buffering=1)
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    info = ProcInfo(
        name=name,
        pid=proc.pid,
        cmd=cmd,
        started_at=time.time(),
        log_path=str(log_path),
    )
    state = _load_state()
    state[name] = {
        "pid": info.pid,
        "cmd": info.cmd,
        "started_at": info.started_at,
        "log_path": info.log_path,
    }
    _save_state(state)
    return info


def stop_process(name: str) -> bool:
    info = get_process(name)
    if info is None:
        return False
    try:
        os.killpg(os.getpgid(info.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    # Give it a moment, then SIGKILL if still around
    for _ in range(20):
        if not _pid_alive(info.pid):
            break
        time.sleep(0.1)
    if _pid_alive(info.pid):
        try:
            os.killpg(os.getpgid(info.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    state = _load_state()
    state.pop(name, None)
    _save_state(state)
    return True


def tail_log(name: str, lines: int = 100) -> str:
    info = get_process(name)
    log_path: Optional[Path]
    if info is not None:
        log_path = Path(info.log_path)
    else:
        # Process gone but log may still exist — try a couple of common names
        if name.startswith("runner:"):
            log_path = LOGS_DIR / f"runner_{name.split(':', 1)[1]}.log"
        elif name.startswith("train:"):
            _, sym, kind = name.split(":")
            log_path = LOGS_DIR / f"train_{sym}_{kind}.log"
        else:
            log_path = None
    if log_path is None and name == "portfolio":
        log_path = LOGS_DIR / "portfolio.log"
    if log_path is None and name == "sentiment":
        log_path = LOGS_DIR / "sentiment.log"
    if log_path is None and name == "cpgw_keeper":
        log_path = LOGS_DIR / "cpgw_keeper.log"
    if log_path is None or not log_path.exists():
        return "(no log file)"
    content = log_path.read_text(errors="replace").splitlines()
    return "\n".join(content[-lines:])
