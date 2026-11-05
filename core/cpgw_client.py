"""
IBKR Client Portal Gateway REST client.

Replaces core/ib_client.py (which used the TWS binary protocol via `ibapi`).
This client talks JSON over HTTPS to a running CPGW instance on localhost.

Auth is handled separately by `core.ibkr_auth.login()` — this client just
takes the cookie and uses it on every request. The `live.cpgw_keeper` daemon
runs the auth + tickle loop.

Endpoint paths and parameter shapes were cross-referenced from the IBKR docs
and the Voyz/ibind library. See SECURITY.md for the audit and known caveats.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

import httpx
import pandas as pd


# ---- bar size / period translation ---------------------------------------
# Old config (TWS notation) → CPGW notation.
# CPGW accepts: 1min, 2min, 3min, 5min, 10min, 15min, 30min, 1h, 2h, 3h, 4h, 8h, 1d, 1w, 1m
_BAR_SIZE_MAP = {
    "1 min": "1min", "2 mins": "2min", "3 mins": "3min", "5 mins": "5min",
    "10 mins": "10min", "15 mins": "15min", "30 mins": "30min",
    "1 hour": "1h", "1 hours": "1h", "2 hours": "2h", "3 hours": "3h",
    "4 hours": "4h", "8 hours": "8h",
    "1 day": "1d", "1 week": "1w", "1 month": "1m",
}

# CPGW accepts: {1-30}min, {1-8}h, {1-1000}d, {1-792}w, {1-182}m, {1-15}y
_PERIOD_MAP = {
    "1 D": "1d", "2 D": "2d", "5 D": "5d", "10 D": "10d", "30 D": "30d",
    "1 W": "1w", "2 W": "2w", "1 M": "1m", "3 M": "3m", "6 M": "6m",
    "1 Y": "1y", "2 Y": "2y", "5 Y": "5y",
}


def _translate_bar(bar: str) -> str:
    if bar in _BAR_SIZE_MAP.values():
        return bar  # already CPGW form
    if bar in _BAR_SIZE_MAP:
        return _BAR_SIZE_MAP[bar]
    raise ValueError(f"unrecognised bar size {bar!r}; CPGW expects e.g. 5min, 1h, 1d")


def _translate_period(period: str) -> str:
    if period in _PERIOD_MAP.values():
        return period
    if period in _PERIOD_MAP:
        return _PERIOD_MAP[period]
    raise ValueError(f"unrecognised period {period!r}; CPGW expects e.g. 5d, 1m, 2y")


# ---- exceptions ----------------------------------------------------------

class CPGWError(RuntimeError):
    """Any non-2xx from the gateway, or a structural problem with its response."""


class NotAuthenticatedError(CPGWError):
    """Session cookie is missing or expired. cpgw_keeper should re-auth."""


# ---- order types ---------------------------------------------------------

@dataclass
class OrderTicket:
    """One submitted order's identifiers as returned by CPGW."""
    order_id: str
    local_order_id: str | None = None
    order_status: str | None = None
    raw: dict[str, Any] | None = None


# ---- the client ----------------------------------------------------------

class CPGWClient:
    """
    Wraps the Client Portal REST API. Stateless beyond holding the session
    cookie + the resolved account ID.

    Typical lifecycle:
        client = CPGWClient(cookie=<from ibkr_auth.login>)
        client.bootstrap()                           # finds the account ID
        client.tickle()                              # call this every 60s
        df = client.get_historical_data("SPY", bar="5min", period="5d")
        client.place_bracket_order(...)
    """

    def __init__(
        self,
        cookie: str,
        *,
        port: int = 5000,
        host: str = "localhost",
        timeout: float = 30.0,
    ) -> None:
        self.account_id: str | None = None
        self._conid_cache: dict[str, int] = {}
        self._client = httpx.Client(
            base_url=f"https://{host}:{port}",
            verify=False,            # CPGW uses a self-signed cert; URL is localhost-pinned
            timeout=timeout,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "user-agent": "tradingbot/1.0",
                "cookie": cookie,
            },
        )

    # ---- low-level ----

    def update_cookie(self, cookie: str) -> None:
        """Called by the keeper after re-auth so existing instances keep working."""
        self._client.headers["cookie"] = cookie

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            r = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise CPGWError(f"HTTP transport error on {method} {path}: {e}") from e

        if r.status_code == 401:
            raise NotAuthenticatedError(f"{method} {path} -> 401, session expired")
        if r.status_code >= 400:
            raise CPGWError(
                f"{method} {path} -> {r.status_code}: {r.text[:300]}"
            )
        if not r.text:
            return None
        try:
            return r.json()
        except ValueError as e:
            raise CPGWError(f"{method} {path} returned non-JSON: {r.text[:300]}") from e

    # ---- session ----

    def auth_status(self) -> dict[str, Any]:
        """`{authenticated: bool, connected: bool, competing: bool, ...}`."""
        return self._request("POST", "/v1/api/iserver/auth/status")

    def tickle(self) -> dict[str, Any]:
        """Keep-alive. Call ~ every 60 s or the gateway times out."""
        return self._request("POST", "/v1/api/tickle")

    def reauthenticate(self) -> dict[str, Any]:
        """Re-attach to the brokerage backend without a full SSO re-login."""
        return self._request("POST", "/v1/api/iserver/reauthenticate")

    def ssodh_init(self, *, publish: bool = True, compete: bool = True) -> dict[str, Any]:
        """First call after fresh SSO login — initialises the brokerage session."""
        return self._request(
            "POST", "/v1/api/iserver/auth/ssodh/init",
            json={"publish": publish, "compete": compete},
        )

    def logout(self) -> dict[str, Any]:
        return self._request("POST", "/v1/api/logout")

    # ---- accounts ----

    def list_accounts(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/api/portfolio/accounts")

    def bootstrap(self) -> str:
        """
        Run the post-login dance:
          1. `/iserver/accounts` (required before any other iserver call)
          2. `/portfolio/accounts` to get the account id
          3. cache the account id on the instance

        Returns the account id.
        """
        # The /iserver/accounts call wakes up the brokerage session
        self._request("GET", "/v1/api/iserver/accounts")
        accts = self.list_accounts()
        if not accts:
            raise CPGWError("No accounts returned — is the session authenticated?")
        # Paper accounts are returned alongside the live one when both exist;
        # caller is expected to have authenticated against the right type.
        acct_id = accts[0].get("id") or accts[0].get("accountId")
        if not acct_id:
            raise CPGWError(f"Account response missing 'id': {accts[0]}")
        self.account_id = acct_id
        return acct_id

    def account_summary(self) -> dict[str, Any]:
        self._ensure_account()
        return self._request("GET", f"/v1/api/portfolio/{self.account_id}/summary")

    def positions(self, page: int = 0) -> list[dict[str, Any]]:
        self._ensure_account()
        return self._request("GET", f"/v1/api/portfolio/{self.account_id}/positions/{page}")

    def _ensure_account(self) -> None:
        if self.account_id is None:
            self.bootstrap()

    # ---- contracts ----

    def conid_for_stock(self, symbol: str) -> int:
        """Resolve a US stock/ETF symbol to its CPGW conid. Cached per instance."""
        if symbol in self._conid_cache:
            return self._conid_cache[symbol]

        # Preferred: /trsrv/stocks is precise + returns one result per ticker
        try:
            data = self._request("GET", "/v1/api/trsrv/stocks", params={"symbols": symbol})
            instruments = data.get(symbol) if isinstance(data, dict) else None
            if instruments:
                first = instruments[0]
                contracts = first.get("contracts", [])
                # Pick the SMART/USD contract for US listings
                preferred = next(
                    (c for c in contracts
                     if c.get("exchange") == "SMART" or c.get("isUS") in (True, "true")),
                    contracts[0] if contracts else None,
                )
                if preferred and preferred.get("conid"):
                    conid = int(preferred["conid"])
                    self._conid_cache[symbol] = conid
                    return conid
        except CPGWError:
            pass  # fall through to secdef/search

        # Fallback: /iserver/secdef/search (more general, less reliable for exact match)
        results = self._request(
            "GET", "/v1/api/iserver/secdef/search",
            params={"symbol": symbol, "name": "false", "secType": "STK"},
        )
        if not results:
            raise CPGWError(f"No conid found for symbol {symbol!r}")
        conid = int(results[0]["conid"])
        self._conid_cache[symbol] = conid
        return conid

    # ---- market data ----

    def get_historical_data(
        self,
        symbol: str,
        *,
        bar: str = "5 mins",
        period: str = "5 D",
        outside_rth: bool = True,
    ) -> pd.DataFrame:
        """
        Bars for `symbol`, returned as a DataFrame indexed by timestamp with
        Open/High/Low/Close/Volume columns — same shape the rest of the bot
        already expects from indicators.calculate_indicators.

        `bar` and `period` accept either TWS notation ("5 mins", "5 D") or
        CPGW notation ("5min", "5d") — they're translated either way.
        """
        conid = self.conid_for_stock(symbol)
        params = {
            "conid": conid,
            "bar": _translate_bar(bar),
            "period": _translate_period(period),
            "outsideRth": "true" if outside_rth else "false",
        }
        data = self._request("GET", "/v1/api/iserver/marketdata/history", params=params)
        bars = (data or {}).get("data") or []
        if not bars:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        # CPGW format: each bar is {t: ms_epoch, o, h, l, c, v}
        df = pd.DataFrame(bars).rename(
            columns={"t": "time", "o": "Open", "h": "High",
                     "l": "Low", "c": "Close", "v": "Volume"},
        )
        df["time"] = pd.to_datetime(df["time"], unit="ms")
        df = df.set_index("time")[["Open", "High", "Low", "Close", "Volume"]]
        df = df.astype(float)
        return df

    # ---- orders ----

    def place_orders(self, orders: list[dict[str, Any]]) -> list[OrderTicket]:
        """
        Submit one or more orders. Each order dict follows the CPGW schema:
          {conid, side, orderType, quantity, tif, price?, auxPrice?, cOID?, parentId?}

        Returns parsed OrderTicket objects in the same order. If CPGW prompts
        for confirmation (warning dialog), we auto-confirm — see _maybe_reply.
        """
        self._ensure_account()
        resp = self._request(
            "POST", f"/v1/api/iserver/account/{self.account_id}/orders",
            json={"orders": orders},
        )
        return self._handle_order_response(resp)

    def _handle_order_response(self, resp: Any) -> list[OrderTicket]:
        out: list[OrderTicket] = []
        if not isinstance(resp, list):
            raise CPGWError(f"Unexpected place-orders response: {resp!r}")
        for item in resp:
            if isinstance(item, dict) and item.get("id") and item.get("message"):
                # Warning prompt — auto-confirm once and recurse
                reply = self._reply(item["id"], confirmed=True)
                out.extend(self._handle_order_response(reply))
                continue
            if isinstance(item, dict) and (item.get("order_id") or item.get("orderId")):
                out.append(OrderTicket(
                    order_id=str(item.get("order_id") or item.get("orderId")),
                    local_order_id=item.get("local_order_id"),
                    order_status=item.get("order_status") or item.get("orderStatus"),
                    raw=item,
                ))
            else:
                # Unexpected shape — capture for debugging but don't crash
                out.append(OrderTicket(order_id="?", raw=item))
        return out

    def _reply(self, reply_id: str, *, confirmed: bool) -> Any:
        return self._request(
            "POST", f"/v1/api/iserver/reply/{reply_id}",
            json={"confirmed": confirmed},
        )

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        self._ensure_account()
        return self._request(
            "DELETE", f"/v1/api/iserver/account/{self.account_id}/order/{order_id}",
        )

    def list_open_orders(self) -> dict[str, Any]:
        return self._request("GET", "/v1/api/iserver/account/orders")

    # ---- order builders (high-level helpers the runners actually call) ----

    @staticmethod
    def build_market_order(*, conid: int, side: str, quantity: float,
                           tif: str = "DAY") -> dict[str, Any]:
        return {
            "conid": int(conid),
            "side": side.upper(),                     # "BUY" / "SELL"
            "orderType": "MKT",
            "quantity": float(quantity),
            "tif": tif,
        }

    @staticmethod
    def build_limit_order(*, conid: int, side: str, quantity: float, price: float,
                          tif: str = "DAY") -> dict[str, Any]:
        return {
            "conid": int(conid),
            "side": side.upper(),
            "orderType": "LMT",
            "quantity": float(quantity),
            "price": float(price),
            "tif": tif,
        }

    def place_bracket_order(
        self,
        *,
        symbol: str,
        quantity: float,
        entry_price: float,
        take_profit_price: float,
        stop_loss_price: float,
        tif: str = "DAY",
        cOID_prefix: str = "tbot",
    ) -> list[OrderTicket]:
        """
        Submit a parent LMT BUY + child LMT SELL (take-profit) + child STP SELL
        (stop) bracket. Children reference the parent via `parentId = parent's cOID`.

        Returns 3 OrderTickets.
        """
        conid = self.conid_for_stock(symbol)
        parent_coid = f"{cOID_prefix}-{symbol}-{int(time.time() * 1000)}"

        parent = {
            "conid": conid,
            "cOID": parent_coid,
            "side": "BUY",
            "orderType": "LMT",
            "price": float(entry_price),
            "quantity": float(quantity),
            "tif": tif,
        }
        take_profit = {
            "conid": conid,
            "parentId": parent_coid,
            "side": "SELL",
            "orderType": "LMT",
            "price": float(take_profit_price),
            "quantity": float(quantity),
            "tif": tif,
        }
        stop = {
            "conid": conid,
            "parentId": parent_coid,
            "side": "SELL",
            "orderType": "STP",
            "price": float(stop_loss_price),   # CPGW uses `price` for the stop trigger
            "quantity": float(quantity),
            "tif": tif,
        }
        return self.place_orders([parent, take_profit, stop])

    def place_market_buy(self, symbol: str, quantity: float) -> OrderTicket:
        """Convenience for the long-term rebalance legs (no bracket)."""
        conid = self.conid_for_stock(symbol)
        out = self.place_orders([self.build_market_order(
            conid=conid, side="BUY", quantity=quantity,
        )])
        return out[0]

    def place_market_sell(self, symbol: str, quantity: float) -> OrderTicket:
        conid = self.conid_for_stock(symbol)
        out = self.place_orders([self.build_market_order(
            conid=conid, side="SELL", quantity=quantity,
        )])
        return out[0]


# Suppress urllib3's "Unverified HTTPS request" warning — it floods logs and
# we know we're talking to a self-signed localhost cert.
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass
