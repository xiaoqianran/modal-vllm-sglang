"""Require a Bearer key across the vLLM HTTP server."""
import hmac
import os


class BearerAuth:
    def __init__(self, app):
        self.app = app
        key = os.environ.get("LLM_API_KEY")
        if not key:
            raise RuntimeError("LLM_API_KEY must be set in the Modal Secret")
        self.key = key.encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        header = dict(scope["headers"]).get(b"authorization", b"")
        scheme, _, supplied = header.partition(b" ")
        if scheme.lower() == b"bearer" and hmac.compare_digest(supplied, self.key):
            await self.app(scope, receive, send)
            return

        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"content-type", b"application/json"), (b"www-authenticate", b"Bearer"),
        ]})
        await send({"type": "http.response.body", "body": b'{"error":"Unauthorized"}'})
