"""
dashboard_server.py — Central Control Dashboard Backend
========================================================
FastAPI application serving:
  • A full web UI for monitoring and controlling the trading bot
  • REST API for all control operations
  • WebSocket for real-time state push to the browser

Default port: 8088  (configured via DASHBOARD_PORT in .env)
Default pass: set DASHBOARD_PASSWORD in .env

Authentication: session cookie (24-hour expiry)
"""

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Dict, List, Optional

import uvicorn
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from bot_state import BotState, get_state

logger = logging.getLogger(__name__)

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "changeme")
DASHBOARD_PORT     = int(os.getenv("DASHBOARD_PORT", "8088"))
SESSION_TTL        = 86_400          # 24 hours

# In-memory session store: {token: expiry_epoch}
_sessions: Dict[str, float] = {}


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def _create_session() -> str:
    token = secrets.token_hex(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token


def _valid(token: Optional[str]) -> bool:
    if not token or token not in _sessions:
        return False
    if time.time() > _sessions[token]:
        _sessions.pop(token, None)
        return False
    return True


def _require_auth(dash_token: Optional[str] = Cookie(default=None)) -> None:
    if not _valid(dash_token):
        raise HTTPException(status_code=401, detail="Unauthorised")


# ═══════════════════════════════════════════════════════════════════════════════
# REQUEST / RESPONSE MODELS
# ═══════════════════════════════════════════════════════════════════════════════
class LoginRequest(BaseModel):
    password: str


class ConfigUpdate(BaseModel):
    updates: dict


class NewsEventRequest(BaseModel):
    time_et: str    # "HH:MM"
    title:   str = "Manual Event"


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD SERVER
# ═══════════════════════════════════════════════════════════════════════════════
class DashboardServer:
    """Manages the FastAPI app and uvicorn server lifecycle."""

    def __init__(self, state: BotState) -> None:
        self.state        = state
        self._app         = self._build_app()
        self._server_task: Optional[asyncio.Task] = None
        self._broadcast_task: Optional[asyncio.Task] = None

    # ── build FastAPI app ────────────────────────────────────────────────────
    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Trading Bot Control Center", docs_url=None, redoc_url=None)

        # Serve static files (index.html lives in ./static/)
        static_dir = Path(__file__).parent / "static"
        static_dir.mkdir(exist_ok=True)

        # ── Auth ─────────────────────────────────────────────────────────────
        @app.post("/api/login")
        async def login(body: LoginRequest, response: JSONResponse = None):
            if body.password == DASHBOARD_PASSWORD:
                token = _create_session()
                resp  = JSONResponse({"ok": True})
                resp.set_cookie("dash_token", token, httponly=True,
                                max_age=SESSION_TTL, samesite="lax")
                return resp
            raise HTTPException(401, "Invalid password")

        @app.post("/api/logout")
        async def logout(dash_token: Optional[str] = Cookie(default=None)):
            _sessions.pop(dash_token or "", None)
            resp = JSONResponse({"ok": True})
            resp.delete_cookie("dash_token")
            return resp

        # ── State snapshot ────────────────────────────────────────────────────
        @app.get("/api/state", dependencies=[Depends(_require_auth)])
        async def get_state_snap():
            return self.state.snapshot()

        # ── Bot controls ──────────────────────────────────────────────────────
        @app.post("/api/control/pause", dependencies=[Depends(_require_auth)])
        async def pause():
            self.state.command_queue.put_nowait({"_command": "pause",   "secret": config.WEBHOOK_SECRET})
            return {"ok": True, "action": "pause"}

        @app.post("/api/control/resume", dependencies=[Depends(_require_auth)])
        async def resume():
            self.state.command_queue.put_nowait({"_command": "resume",  "secret": config.WEBHOOK_SECRET})
            return {"ok": True, "action": "resume"}

        @app.post("/api/control/flatten", dependencies=[Depends(_require_auth)])
        async def flatten():
            self.state.command_queue.put_nowait({"_command": "flatten", "secret": config.WEBHOOK_SECRET})
            return {"ok": True, "action": "flatten"}

        @app.post("/api/control/stop", dependencies=[Depends(_require_auth)])
        async def stop():
            if self.state.bot_ref:
                self.state.bot_ref.stop()
            return {"ok": True, "action": "stop"}

        # ── Config ────────────────────────────────────────────────────────────
        @app.get("/api/config", dependencies=[Depends(_require_auth)])
        async def get_config():
            return self.state.live_config

        @app.post("/api/config", dependencies=[Depends(_require_auth)])
        async def update_config(body: ConfigUpdate):
            changed = self.state.apply_config_update(body.updates)
            logger.info("Config updated via dashboard: %s", changed)
            await self.state.broadcast()
            return {"ok": True, "changed": changed}

        # ── News ──────────────────────────────────────────────────────────────
        @app.get("/api/news", dependencies=[Depends(_require_auth)])
        async def get_news():
            return {
                "events":        self.state.news_events,
                "blackout":      self.state.news_blackout_active,
                "next_event_min": self.state.next_event_min,
            }

        @app.post("/api/news/add", dependencies=[Depends(_require_auth)])
        async def add_news(body: NewsEventRequest):
            if self.state.bot_ref and hasattr(self.state.bot_ref, "news"):
                self.state.bot_ref.news.add_manual_event(body.time_et, body.title)
            return {"ok": True}

        # ── Trade history ─────────────────────────────────────────────────────
        @app.get("/api/trades", dependencies=[Depends(_require_auth)])
        async def get_trades():
            return list(reversed(self.state.trade_history))

        # ── Log stream (REST polling fallback if WS not available) ────────────
        @app.get("/api/logs", dependencies=[Depends(_require_auth)])
        async def get_logs(n: int = 80):
            return list(self.state._logs)[-n:]

        # ── WebSocket — real-time push ────────────────────────────────────────
        @app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket, dash_token: Optional[str] = Cookie(default=None)):
            if not _valid(dash_token):
                await ws.close(code=4001)
                return

            await ws.accept()
            self.state.ws_add(ws)
            logger.info("Dashboard WebSocket client connected")

            try:
                # Send full snapshot immediately on connect
                import json
                await ws.send_text(
                    json.dumps({"type": "state", "data": self.state.snapshot()})
                )
                # Keep connection alive (client sends pings; we just wait)
                while True:
                    try:
                        data = await asyncio.wait_for(ws.receive_text(), timeout=30)
                        if data == "ping":
                            await ws.send_text("pong")
                    except asyncio.TimeoutError:
                        await ws.send_text("ping")
            except WebSocketDisconnect:
                pass
            except Exception as exc:
                logger.debug("Dashboard WS closed: %s", exc)
            finally:
                self.state.ws_remove(ws)
                logger.info("Dashboard WebSocket client disconnected")

        # ── Serve the static dashboard ────────────────────────────────────────
        @app.get("/", response_class=HTMLResponse)
        @app.get("/dashboard", response_class=HTMLResponse)
        async def serve_dashboard():
            html_path = static_dir / "index.html"
            if html_path.exists():
                return FileResponse(str(html_path))
            return HTMLResponse("<h1>Dashboard file not found. Check static/index.html</h1>", 500)

        # Mount any other static assets (CSS/JS if separated)
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        return app

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        """Start the uvicorn server and periodic broadcast task."""
        cfg = uvicorn.Config(
            self._app,
            host="0.0.0.0",
            port=DASHBOARD_PORT,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(cfg)
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

        self._server_task    = asyncio.create_task(server.serve(),   name="dashboard_server")
        self._broadcast_task = asyncio.create_task(self._broadcast_loop(), name="dashboard_broadcast")
        logger.info("Dashboard started on http://0.0.0.0:%d", DASHBOARD_PORT)

    async def _broadcast_loop(self) -> None:
        """Push state updates to WebSocket clients every 2 seconds."""
        while True:
            try:
                await asyncio.sleep(2)
                if self.state._ws_clients:
                    await self.state.broadcast()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Broadcast loop error: %s", exc)

    def stop(self) -> None:
        for task in (self._server_task, self._broadcast_task):
            if task and not task.done():
                task.cancel()
