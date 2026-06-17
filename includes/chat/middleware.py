"""ASGI middleware and logging handlers for EagleAgent."""

import logging
import urllib.parse

import chainlit as cl

logger = logging.getLogger(__name__)


class OAuthErrorRedirectMiddleware:
    """Pure ASGI middleware: redirects 401s on OAuth callback paths to the login page.

    Uses raw ASGI to avoid the known issues that BaseHTTPMiddleware causes with
    WebSocket connections and streaming responses in Starlette/Chainlit.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Only intercept HTTP requests on OAuth callback paths
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not (path.startswith("/auth/oauth/") and path.endswith("/callback")):
            await self.app(scope, receive, send)
            return

        status_code = None

        async def send_wrapper(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                if status_code == 401:
                    params = urllib.parse.urlencode(
                        {"error": "Access denied. Your account is not authorised to use this application."}
                    )
                    redirect_url = f"/login?{params}"
                    await send({
                        "type": "http.response.start",
                        "status": 302,
                        "headers": [
                            [b"location", redirect_url.encode()],
                            [b"content-length", b"0"],
                        ],
                    })
                    return
                await send(message)
            elif message["type"] == "http.response.body":
                if status_code == 401:
                    await send({"type": "http.response.body", "body": b""})
                    return
                await send(message)

        await self.app(scope, receive, send_wrapper)


class GeminiRetryNotifier(logging.Handler):
    """Intercepts google_genai retry log messages and pushes them to the UI.

    Debounced: only sends one UI notification per 10-second window to avoid spam
    when Google does rapid-fire exponential backoff retries.
    """

    def __init__(self, level: int = logging.NOTSET):
        super().__init__(level)
        self._last_notified = 0.0

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "Retrying" not in msg:
            return
        if "503" not in msg and "429" not in msg:
            return
        import time
        now = time.monotonic()
        if now - self._last_notified < 10:
            return  # Debounce: skip if we notified recently
        self._last_notified = now
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            loop.create_task(self._send_notification())
        except RuntimeError:
            pass  # No event loop — nothing we can do

    @staticmethod
    async def _send_notification() -> None:
        try:
            await cl.Message(
                content="\u23f3 Model temporarily overloaded — retrying automatically...",
                author="System",
            ).send()
        except Exception:
            pass  # Never break the app for a UI notification
