"""
tradovate_client.py — Tradovate REST + WebSocket Client
========================================================
Handles authentication, order placement, position management, and real-time
market data via Tradovate's REST API and WebSocket interface.

WebSocket protocol notes (Tradovate SockJS-style):
  • On connect the server sends "o"  (socket opened)
  • Client sends:  authorize\n{id}\n\n{"token":"ACCESS_TOKEN"}
  • Server responds inside a["..."] frames
  • Client must send [] (empty heartbeat) every 2.5 s or server disconnects
  • Market data on wss://md.tradovateapi.com/v1/websocket
"""

import asyncio
import json
import logging
import time as time_mod
import aiohttp
import websockets
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH MANAGER
# ═══════════════════════════════════════════════════════════════════════════════
class TradovateAuth:
    """Manages Tradovate OAuth2 access tokens with automatic refresh."""

    def __init__(self) -> None:
        self.access_token:  Optional[str]   = None
        self.md_token:      Optional[str]   = None   # market-data token (same request)
        self.expiry_epoch:  float           = 0.0
        self._session:      Optional[aiohttp.ClientSession] = None

    # ── internal helpers ────────────────────────────────────────────────────
    async def _session_(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _post(self, path: str, payload: dict) -> dict:
        session = await self._session_()
        url = f"{config.TRADOVATE_BASE_URL}{path}"
        async with session.post(url, json=payload) as resp:
            data = await resp.json()
            if resp.status not in (200, 201):
                raise RuntimeError(f"Tradovate {path} → HTTP {resp.status}: {data}")
            return data

    # ── public API ──────────────────────────────────────────────────────────
    async def authenticate(self) -> str:
        """Obtain an access token using username + password credentials."""
        payload = {
            "name":       config.TRADOVATE_USERNAME,
            "password":   config.TRADOVATE_PASSWORD,
            "appId":      config.TRADOVATE_APP_ID,
            "appVersion": config.TRADOVATE_APP_VERSION,
            "cid":        config.TRADOVATE_CID,
            "sec":        config.TRADOVATE_SECRET,
        }
        data = await self._post("/auth/accesstokenrequest", payload)

        if "accessToken" not in data:
            raise ValueError(f"Authentication failed — response: {data}")

        self.access_token = data["accessToken"]
        self.md_token     = data.get("mdAccessToken", self.access_token)
        # Tradovate tokens expire in 24 h; refresh 1 h before expiry
        self.expiry_epoch = time_mod.time() + 82800  # 23 h
        logger.info("✅ Tradovate authenticated (mode=%s)", "DEMO" if config.DEMO_MODE else "LIVE")
        return self.access_token

    async def get_token(self) -> str:
        """Return valid token, refreshing automatically if near expiry."""
        if not self.access_token or time_mod.time() > self.expiry_epoch - config.TOKEN_REFRESH_MARGIN:
            await self.authenticate()
        return self.access_token  # type: ignore[return-value]

    def auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ═══════════════════════════════════════════════════════════════════════════════
# REST CLIENT
# ═══════════════════════════════════════════════════════════════════════════════
class TradovateREST:
    """Low-level REST wrapper with retry logic."""

    def __init__(self, auth: TradovateAuth) -> None:
        self.auth = auth

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """Execute an HTTP request with exponential-backoff retries."""
        await self.auth.get_token()
        session = await self.auth._session_()
        url = f"{config.TRADOVATE_BASE_URL}{path}"
        headers = self.auth.auth_headers()

        for attempt in range(1, config.MAX_RETRY_ATTEMPTS + 1):
            try:
                async with session.request(method, url, headers=headers, **kwargs) as resp:
                    text = await resp.text()
                    if resp.status == 401:
                        # Token expired mid-session — re-auth once
                        logger.warning("401 received — re-authenticating …")
                        await self.auth.authenticate()
                        headers = self.auth.auth_headers()
                        continue
                    if resp.status not in (200, 201):
                        logger.error("HTTP %d %s: %s", resp.status, path, text[:200])
                        raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
                    return json.loads(text) if text else {}
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt == config.MAX_RETRY_ATTEMPTS:
                    raise
                delay = config.RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning("Request failed (attempt %d/%d) — retrying in %.1fs: %s",
                               attempt, config.MAX_RETRY_ATTEMPTS, delay, exc)
                await asyncio.sleep(delay)
        raise RuntimeError("All retry attempts exhausted")

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, data: Optional[dict] = None) -> Any:
        return await self._request("POST", path, json=data or {})


# ═══════════════════════════════════════════════════════════════════════════════
# TRADOVATE WEBSOCKET  (trading channel)
# ═══════════════════════════════════════════════════════════════════════════════
class TradovateWebSocket:
    """
    Real-time trading WebSocket (order fills, position updates, account events).
    Protocol: Tradovate uses a SockJS-compatible custom framing.
    """

    HEARTBEAT_INTERVAL = 2.4   # seconds (server drops after ~2.5 s of silence)

    def __init__(self, auth: TradovateAuth) -> None:
        self.auth = auth
        self._ws:          Optional[Any] = None
        self._running:     bool          = False
        self._req_counter: int           = 0
        self._callbacks:   Dict[str, List[Callable]] = {}
        self._pending:     Dict[str, asyncio.Future] = {}

    # ── internal helpers ────────────────────────────────────────────────────
    def _next_id(self) -> str:
        self._req_counter += 1
        return str(self._req_counter)

    def _parse_frame(self, raw: str) -> List[dict]:
        """Parse Tradovate WebSocket frames into a list of message dicts."""
        if raw in ("o", "h"):
            return []
        if raw.startswith("a"):
            try:
                msgs = json.loads(raw[1:])   # strip leading 'a'
                return [self._parse_message(m) for m in msgs]
            except json.JSONDecodeError:
                return []
        if raw.startswith("c"):
            logger.warning("WS closed by server: %s", raw)
            self._running = False
            return []
        return []

    @staticmethod
    def _parse_message(raw_msg: str) -> dict:
        """
        Message format:  "ENDPOINT\nID\nSTATUS\nBODY"
        Some messages:   "EVENT_TYPE\nBODY"
        """
        parts = raw_msg.split("\n", 3)
        if len(parts) < 2:
            return {"raw": raw_msg}
        result: dict = {"endpoint": parts[0]}
        if len(parts) >= 3:
            result["status"] = parts[1]
        if len(parts) == 4:
            try:
                result["body"] = json.loads(parts[3])
            except json.JSONDecodeError:
                result["body"] = parts[3]
        return result

    async def _send_frame(self, endpoint: str, req_id: str, body: dict) -> None:
        body_str = json.dumps(body)
        frame = f'["{endpoint}\\n{req_id}\\n\\n{body_str}"]'
        await self._ws.send(frame)

    async def _heartbeat_loop(self) -> None:
        while self._running and self._ws:
            try:
                await self._ws.send("[]")
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
            except Exception as exc:
                logger.debug("Heartbeat error: %s", exc)
                break

    # ── public API ──────────────────────────────────────────────────────────
    async def connect(self) -> None:
        token = await self.auth.get_token()
        self._ws = await websockets.connect(
            config.TRADOVATE_WS_URL,
            ping_interval=None,   # we manage heartbeats manually
            close_timeout=5,
        )
        # Wait for "o" (socket opened)
        opening = await self._ws.recv()
        if opening != "o":
            logger.warning("Unexpected WS opening frame: %s", opening)

        # Authorize
        req_id = self._next_id()
        await self._send_frame("authorize", req_id, {"token": token})
        asyncio.create_task(self._heartbeat_loop())
        asyncio.create_task(self._receive_loop())
        self._running = True
        logger.info("✅ Trading WebSocket connected")

    async def _receive_loop(self) -> None:
        while self._running and self._ws:
            try:
                raw = await self._ws.recv()
                messages = self._parse_frame(raw)
                for msg in messages:
                    await self._dispatch(msg)
            except websockets.ConnectionClosed:
                logger.warning("Trading WebSocket connection closed")
                self._running = False
                break
            except Exception as exc:
                logger.error("WS receive error: %s", exc)

    async def _dispatch(self, msg: dict) -> None:
        """Dispatch incoming message to registered callbacks."""
        event_type = msg.get("endpoint", "")
        for key, handlers in self._callbacks.items():
            if key in event_type:
                for handler in handlers:
                    try:
                        await handler(msg)
                    except Exception as exc:
                        logger.error("WS callback error (%s): %s", key, exc)

    def on_event(self, event_keyword: str, callback: Callable) -> None:
        """Register a callback for messages containing event_keyword."""
        self._callbacks.setdefault(event_keyword, []).append(callback)

    async def close(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()


# ═══════════════════════════════════════════════════════════════════════════════
# HIGH-LEVEL TRADOVATE CLIENT
# ═══════════════════════════════════════════════════════════════════════════════
class TradovateClient:
    """
    High-level Tradovate client used by the rest of the bot.
    Combines REST + WebSocket into a single interface.
    """

    def __init__(self) -> None:
        self.auth    = TradovateAuth()
        self.rest    = TradovateREST(self.auth)
        self.ws      = TradovateWebSocket(self.auth)
        self._account_id: Optional[int] = None

    # ── initialisation ──────────────────────────────────────────────────────
    async def authenticate(self) -> None:
        await self.auth.authenticate()

    async def connect_ws(self) -> None:
        """Connect the real-time trading WebSocket (optional but recommended)."""
        try:
            await self.ws.connect()
        except Exception as exc:
            logger.warning("WebSocket connect failed (REST-only mode): %s", exc)

    # ── account information ─────────────────────────────────────────────────
    async def get_accounts(self) -> List[dict]:
        """Return list of accounts accessible to this credential."""
        result = await self.rest.get("/account/list")
        return result if isinstance(result, list) else []

    async def get_account_equity(self, account_id: int) -> float:
        """
        Return net liquidation value for the account.
        Tradovate exposes this via /cashBalance/getcashbalancesnapshot.
        """
        try:
            data = await self.rest.get(
                "/cashBalance/getcashbalancesnapshot",
                params={"accountId": account_id},
            )
            # 'totalCashValue' or 'netLiq' depending on account type
            equity = data.get("totalCashValue") or data.get("netLiq") or data.get("cashBalance", 0)
            return float(equity)
        except Exception as exc:
            logger.error("Failed to get equity: %s", exc)
            return 0.0

    async def get_positions(self, account_id: int) -> List[dict]:
        """Return all open positions for the account."""
        try:
            result = await self.rest.get(
                "/position/list",
                params={"accountId": account_id},
            )
            positions = result if isinstance(result, list) else []
            # Filter to non-zero positions
            return [p for p in positions if p.get("netPos", 0) != 0]
        except Exception as exc:
            logger.error("Failed to get positions: %s", exc)
            return []

    async def get_open_orders(self, account_id: int) -> List[dict]:
        """Return all working orders for the account."""
        try:
            result = await self.rest.get(
                "/order/list",
                params={"accountId": account_id},
            )
            orders = result if isinstance(result, list) else []
            return [o for o in orders if o.get("ordStatus") in ("Working", "PendingNew")]
        except Exception as exc:
            logger.error("Failed to get orders: %s", exc)
            return []

    # ── contract resolution ─────────────────────────────────────────────────
    async def find_contract(self, symbol: str) -> Optional[dict]:
        """
        Resolve a symbol (e.g. 'MGC') to the front-month contract.
        Returns the contract dict including 'id' and 'name'.
        """
        try:
            result = await self.rest.get("/contract/find", params={"name": symbol})
            if isinstance(result, list) and result:
                return result[0]
            if isinstance(result, dict):
                return result
        except Exception as exc:
            logger.error("Contract resolution failed for %s: %s", symbol, exc)
        return None

    async def get_contract_id(self, symbol: str) -> Optional[int]:
        contract = await self.find_contract(symbol)
        return contract["id"] if contract else None

    # ── order placement ─────────────────────────────────────────────────────
    async def place_market_order(
        self,
        account_id: int,
        symbol: str,
        action: str,     # "Buy" or "Sell"
        qty: int,
    ) -> dict:
        """Place a plain market order (no brackets)."""
        contract = await self.find_contract(symbol)
        if not contract:
            raise ValueError(f"Cannot resolve contract for symbol: {symbol}")

        payload = {
            "accountSpec":   config.TRADOVATE_USERNAME,
            "accountId":     account_id,
            "action":        action,
            "symbol":        contract["name"],
            "orderQty":      qty,
            "orderType":     "Market",
            "isAutomated":   True,
        }
        logger.info("Placing market order: %s", payload)
        return await self.rest.post("/order/placeorder", payload)

    async def place_bracket_order(
        self,
        account_id:    int,
        symbol:        str,
        action:        str,       # "Buy" or "Sell"
        contracts:     int,
        sl_price:      float,
        tp1_price:     float,
        tp2_price:     Optional[float] = None,
        contracts_tp1: int = 0,
        contracts_tp2: int = 0,
    ) -> Optional[dict]:
        """
        Place an entry with attached stop-loss and up to two take-profit brackets.

        Tradovate's OSO (One-Sends-Other) endpoint attaches bracket orders that
        activate once the parent order is filled.  We split the position into
        two child legs when tp2 is provided:
          • Leg 1 — contracts_tp1 @ TP1 limit  (with shared SL)
          • Leg 2 — contracts_tp2 @ TP2 limit  (with shared SL)
        """
        contract = await self.find_contract(symbol)
        if not contract:
            raise ValueError(f"Cannot resolve contract: {symbol}")

        contract_name = contract["name"]
        exit_action   = "Sell" if action == "Buy" else "Buy"

        # ── Single-bracket path (tp2 not provided or qty split is odd) ──
        if tp2_price is None or contracts_tp2 == 0:
            payload = {
                "accountSpec": config.TRADOVATE_USERNAME,
                "accountId":   account_id,
                "action":      action,
                "symbol":      contract_name,
                "orderQty":    contracts,
                "orderType":   "Market",
                "isAutomated": True,
                "bracket1": {
                    "action":    exit_action,
                    "orderType": "Limit",
                    "price":     tp1_price,
                },
                "bracket2": {
                    "action":     exit_action,
                    "orderType":  "Stop",
                    "stopPrice":  sl_price,
                },
            }
            logger.info("Placing single-bracket order: %s × %s %s | SL=%.2f TP=%.2f",
                        contracts, action, contract_name, sl_price, tp1_price)
            return await self.rest.post("/order/placeoso", payload)

        # ── Two-bracket path — place TWO separate OSO orders ────────────
        # Leg 1: TP1
        payload1 = {
            "accountSpec": config.TRADOVATE_USERNAME,
            "accountId":   account_id,
            "action":      action,
            "symbol":      contract_name,
            "orderQty":    contracts_tp1,
            "orderType":   "Market",
            "isAutomated": True,
            "bracket1": {
                "action":    exit_action,
                "orderType": "Limit",
                "price":     tp1_price,
            },
            "bracket2": {
                "action":     exit_action,
                "orderType":  "Stop",
                "stopPrice":  sl_price,
            },
        }
        # Leg 2: TP2
        payload2 = {
            "accountSpec": config.TRADOVATE_USERNAME,
            "accountId":   account_id,
            "action":      action,
            "symbol":      contract_name,
            "orderQty":    contracts_tp2,
            "orderType":   "Market",
            "isAutomated": True,
            "bracket1": {
                "action":    exit_action,
                "orderType": "Limit",
                "price":     tp2_price,
            },
            "bracket2": {
                "action":     exit_action,
                "orderType":  "Stop",
                "stopPrice":  sl_price,
            },
        }
        logger.info(
            "Placing dual-bracket order: %s %s %s | "
            "SL=%.2f  TP1=%.2f (×%d)  TP2=%.2f (×%d)",
            action, contracts, contract_name,
            sl_price, tp1_price, contracts_tp1, tp2_price, contracts_tp2,
        )
        results = await asyncio.gather(
            self.rest.post("/order/placeoso", payload1),
            self.rest.post("/order/placeoso", payload2),
            return_exceptions=True,
        )
        # Return the first successful result; log failures
        for r in results:
            if isinstance(r, Exception):
                logger.error("Bracket leg failed: %s", r)
            else:
                return r    # first non-exception is the "primary" result
        return None

    async def cancel_order(self, order_id: int) -> dict:
        """Cancel a specific order by ID."""
        return await self.rest.post("/order/cancelorder", {"orderId": order_id})

    async def liquidate_position(self, account_id: int, contract_id: int) -> dict:
        """
        Market-close an entire position for a given contract.
        Uses Tradovate's /order/liquidateposition endpoint.
        """
        payload = {
            "accountId":  account_id,
            "contractId": contract_id,
            "admin":      False,
        }
        logger.warning("Liquidating position — contractId=%d", contract_id)
        return await self.rest.post("/order/liquidateposition", payload)

    async def flatten_all(self, account_id: int) -> None:
        """Emergency: market-close ALL open positions immediately."""
        positions = await self.get_positions(account_id)
        if not positions:
            logger.info("flatten_all: no open positions")
            return

        tasks = [
            self.liquidate_position(account_id, p["contractId"])
            for p in positions
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for p, r in zip(positions, results):
            if isinstance(r, Exception):
                logger.error("Failed to liquidate contractId=%d: %s", p["contractId"], r)
            else:
                logger.info("Liquidated contractId=%d", p["contractId"])

    # ── quote / market data (REST polling fallback) ─────────────────────────
    async def get_quote(self, symbol: str) -> Optional[dict]:
        """
        Fetch a single best bid/offer snapshot via REST.
        Used as a fallback when the market-data WebSocket is not connected.
        """
        try:
            contract = await self.find_contract(symbol)
            if not contract:
                return None
            result = await self.rest.get(
                "/md/getquote",
                params={"contractId": contract["id"]},
            )
            return result if isinstance(result, dict) else None
        except Exception as exc:
            logger.error("Quote fetch failed: %s", exc)
            return None

    # ── cleanup ─────────────────────────────────────────────────────────────
    async def close(self) -> None:
        await self.ws.close()
        await self.auth.close()
