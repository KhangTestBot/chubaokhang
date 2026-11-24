"""Vercel Python Serverless Function entrypoint for Locket AI + Photobooth backend."""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler

# Allow rapid 4-shot Photobooth captures on warm Vercel containers without hitting the local 3/min default.
os.environ.setdefault("RATE_LIMIT_PER_MINUTE", "20")
os.environ.setdefault("MAX_CONCURRENT", "4")
from urllib.parse import parse_qs, urlsplit

from backend_py.server import create_vercel_handler

_Delegate = create_vercel_handler()


class handler(BaseHTTPRequestHandler):
    server_version = _Delegate.server_version
    sys_version = _Delegate.sys_version
    protocol_version = _Delegate.protocol_version

    def setup(self):
        super().setup()
        if getattr(self, "connection", None) is not None:
            self.connection.settimeout(10)

    def log_message(self, format, *args):
        pass

    _json = _Delegate._json
    _error = _Delegate._error
    send_error = _Delegate.send_error
    _delegate_dispatch = _Delegate._dispatch

    def _dispatch(self):
        parts = urlsplit(self.path)
        if parts.path in ("/api/index", "/api", "/api/"):
            params = parse_qs(parts.query)
            if "__path" in params and params["__path"]:
                self.path = params["__path"][0]
        return self._delegate_dispatch()

    def do_GET(self):
        return self._dispatch()

    def do_POST(self):
        return self._dispatch()

    def do_PUT(self):
        return self._dispatch()

    def do_DELETE(self):
        return self._dispatch()

    def do_PATCH(self):
        return self._dispatch()

    def do_HEAD(self):
        return self._dispatch()

    def do_OPTIONS(self):
        return self._dispatch()
