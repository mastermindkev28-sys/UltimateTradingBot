"""
webhook_server.py — TradingView Webhook Receiver
=================================================
Lightweight FastAPI server that listens for JSON payloads from TradingView
Pine Script alerts and queues them for the trading bot to process.

Security:
  • Shared secret in every payload ("secret" field) validated against
    the WEBHOOK_SECRET environment variable.
  • Requests exceeding MAX_PAYLOAD_BYTES are rejected.
  • Rate-limited to MAX_REQUESTS_PER_MINUTE per source IP.

The server runs in a separate asyncio task, not a thread, so it shares
the same event loop as the trading bot.
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Any, Callable, Coroutine, Dict, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

import config

logger = logging.getLogger(__name__)

MAX_PAYLOAD_BYTES      = 8_192       # 8 KB — plenty for a Pine Script alert
MAX_REQUESTS_PER_MIN   = 10          # per IP rate limit
RATE_WINDOW_SECONDS    = 60


# ═══════════════════════════════════════════════════════════════════════════════
# RATE LIMITER (simple in-memory, good enough for single-instance bot)
# ═══════════════════════════════════════════════════════════════════════════════
class _RateLimiter:
    def __init__(self, max_requests: int, window: int) -> None:
        self._max  = max_requests
        self._win  = window
        self._data: Dict[str, list] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        times = self._data[key]
        # Purge old entries
        self._data[key] = [t for t in times if now - t < self._win]
        if len(self._data[key]) >= self._max:
            return False
        self._data[key].append(now)
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK SERVER
# ═══════════════════════════════════════════════════════════════════════════════
class WebhookServer:
    """
    Manages the FastAPI application and uvicorn server lifecycle.

    The callback `on_signal` is called with the validated parsed payload
    whenever a legitimate TradingView signal arrives.
    """

    def __init__(
        self,
        on_signal: Callable[[dict], Coroutine],
    ) -> None:
        self._on_signal   = on_signal
        self._rate_limiter = _RateLimiter(MAX_REQUESTS_PER_MIN, RATE_WINDOW_SECONDS)
        self._app          = self._build_app()
        self._server_task: Optional[asyncio.Task] = None
        self._signal_queue: asyncio.Queue = asyncio.Queue(maxsize=20)

    # ── FastAPI app ─────────────────────────────────────────────────────────
    def _build_app(self) -> FastAPI:
        app = FastAPI(
            title="UltimateTradingBot Webhook",
            docs_url=None,    # Disable swagger in production
            redoc_url=None,
        )

        @app.get("/health")
        async def health() -> dict:
            return {"status": "ok", "mode": "DEMO" if config.DEMO_MODE else "LIVE"}

        @app.post("/webhook")
        async def webhook(request: Request) -> JSONResponse:
            # ── size guard ────────────────────────────────────────────────
            content_length = request.headers.get("content-length", "0")
            try:
                if int(content_length) > MAX_PAYLOAD_BYTES:
                    raise HTTPException(413, "Payload too large")
            except ValueError:
                pass

            # ── rate limit ────────────────────────────────────────────────
            client_ip = request.client.host if request.client else "unknown"
            if not self._rate_limiter.is_allowed(client_ip):
                logger.warning("Rate limit exceeded for %s", client_ip)
                raise HTTPException(429, "Too many requests")

            # ── parse body ────────────────────────────────────────────────
            try:
                body = await request.body()
                payload = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.warning("Malformed webhook payload: %s", exc)
                raise HTTPException(400, "Invalid JSON payload")

            if not isinstance(payload, dict):
                raise HTTPException(400, "Payload must be a JSON object")

            # ── secret validation ─────────────────────────────────────────
            if payload.get("secret", "") != config.WEBHOOK_SECRET:
                logger.warning(
                    "Webhook secret mismatch from %s — payload keys: %s",
                    client_ip,
                    list(payload.keys()),
                )
                raise HTTPException(403, "Forbidden")

            # ── enqueue for processing ────────────────────────────────────
            logger.info(
                "📡 Webhook received from %s | action=%s instrument=%s",
                client_ip,
                payload.get("action", "?"),
                payload.get("instrument", "?"),
            )
            try:
                self._signal_queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.error("Signal queue full — dropping signal from %s", client_ip)
                raise HTTPException(503, "Signal queue full")

            return JSONResponse({"status": "accepted"}, status_code=202)

        @app.post("/flatten")
        async def emergency_flatten(request: Request) -> JSONResponse:
            """
            Emergency endpoint: POST /flatten  with {"secret":"..."} body
            triggers immediate flatten of all positions.
            """
            try:
                body = await request.body()
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                raise HTTPException(400, "Invalid JSON")

            if payload.get("secret", "") != config.WEBHOOK_SECRET:
                raise HTTPException(403, "Forbidden")

            # Inject a special flatten signal
            await self._signal_queue.put({"_command": "flatten", "secret": config.WEBHOOK_SECRET})
            logger.warning("⚡ Emergency flatten triggered via webhook")
            return JSONResponse({"status": "flatten_initiated"})

        @app.post("/pause")
        async def pause_resume(request: Request) -> JSONResponse:
            """Toggle bot pause via webhook (for operators)."""
            try:
                body = await request.body()
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                raise HTTPException(400, "Invalid JSON")

            if payload.get("secret", "") != config.WEBHOOK_SECRET:
                raise HTTPException(403, "Forbidden")

            cmd = payload.get("command", "pause")
            await self._signal_queue.put({"_command": cmd, "secret": config.WEBHOOK_SECRET})
            return JSONResponse({"status": f"{cmd}_initiated"})

        return app

    # ── server lifecycle ────────────────────────────────────────────────────
    def start(self) -> None:
        """Start the uvicorn server as an asyncio task."""
        cfg = uvicorn.Config(
            self._app,
            host=config.WEBHOOK_HOST,
            port=config.WEBHOOK_PORT,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(cfg)

        # Prevent uvicorn from installing its own signal handlers
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

        self._server_task = asyncio.create_task(server.serve(), name="webhook_server")
        # Start the consumer task
        asyncio.create_task(self._consume_queue(), name="signal_consumer")
        logger.info(
            "Webhook server started on %s:%d  (health: GET /health)",
            config.WEBHOOK_HOST,
            config.WEBHOOK_PORT,
        )

    async def _consume_queue(self) -> None:
        """Drain the signal queue and invoke the callback for each signal."""
        while True:
            try:
                payload = await self._signal_queue.get()
                try:
                    await self._on_signal(payload)
                except Exception as exc:
                    logger.error("Signal callback error: %s", exc, exc_info=True)
                finally:
                    self._signal_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Queue consumer error: %s", exc)

    def stop(self) -> None:
        """Cancel the server task."""
        if self._server_task and not self._server_task.done():
            self._server_task.cancel()

    # ── queue helpers (for testing) ─────────────────────────────────────────
    async def inject_signal(self, payload: dict) -> None:
        """Directly inject a signal (used in dry-run / testing)."""
        await self._signal_queue.put(payload)
