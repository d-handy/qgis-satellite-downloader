"""Local HTTP server with range requests, so /vsicurl/ code runs offline.

GDAL reads remote rasters through ``/vsicurl/`` and refuses ``file://`` URLs,
so tests that exercise the real read path serve a temporary folder on
127.0.0.1 instead. Single byte ranges are honoured; anything else gets the
whole file, which GDAL accepts as a server without multi-range support.
"""

from __future__ import annotations

import functools
import os
import re
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from osgeo import gdal

_RANGE = re.compile(r"bytes=(\d+)-(\d*)$")


class _RangeHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - signature of the base class
        pass  # keep test output clean

    def send_head(self):
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(404)
            return None
        size = os.path.getsize(path)
        match = _RANGE.match(self.headers.get("Range", ""))
        fh = open(path, "rb")  # noqa: SIM115 - closed by the base class
        if match:
            start = int(match.group(1))
            end = min(int(match.group(2) or size - 1), size - 1)
            if start >= size:
                fh.close()
                self.send_error(416)
                return None
            fh.seek(start)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            length = end - start + 1
        else:
            self.send_response(200)
            length = size
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        return _Limited(fh, length)


class _Limited:
    """File wrapper that stops after ``length`` bytes (the requested range)."""

    def __init__(self, fh, length: int):
        self._fh, self._left = fh, length

    def read(self, n: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        n = self._left if n < 0 else min(n, self._left)
        data = self._fh.read(n)
        self._left -= len(data)
        return data

    def close(self) -> None:
        self._fh.close()


class LocalServer:
    """Serve ``folder`` over HTTP on a free port; use as a context manager."""

    def __init__(self, folder: str):
        handler = functools.partial(_RangeHandler, directory=folder)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def url(self, name: str) -> str:
        port = self._httpd.server_address[1]
        return f"http://127.0.0.1:{port}/{name}"

    def __enter__(self) -> LocalServer:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        gdal.VSICurlClearCache()  # cached blocks would outlive the files
