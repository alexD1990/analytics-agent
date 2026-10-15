"""Local SQL gateway between the Python kernel and the server.

The Python kernel has no database connection of its own and sees no
passwords. When the agent's code calls sql(), the query is sent here, and the
server runs it with the same checks as run_query: SELECT only, timeout, row
limit and logging. The gateway only listens on 127.0.0.1 and requires a random
token that only the kernel gets.
"""
from __future__ import annotations

import hmac
import pickle
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class SqlGateway:
    def __init__(self, engine, log):
        self.token = secrets.token_urlsafe(32)
        expected = f"Bearer {self.token}".encode()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != "/sql":
                    return self._respond(404, "Unknown path".encode())
                received = self.headers.get("Authorization", "").encode("utf-8", "replace")
                if not hmac.compare_digest(received, expected):
                    return self._respond(403, "Invalid token".encode())

                length = int(self.headers.get("Content-Length") or 0)
                query = self.rfile.read(length).decode("utf-8")
                start = time.perf_counter()
                try:
                    df = engine.df(query)
                except Exception as e:
                    log.write("sql (python)", {"sql": query}, "error", str(e), start)
                    return self._respond(400, str(e).encode("utf-8"))
                log.write("sql (python)", {"sql": query}, "ok",
                          f"{len(df)} rows × {df.shape[1]} columns", start)
                # The server pickles, the kernel unpickles: data only flows from the trusted to the less trusted side.
                self._respond(200, pickle.dumps(df, protocol=pickle.HIGHEST_PROTOCOL),
                              "application/octet-stream")

            def _respond(self, code: int, data: bytes, content_type: str = "text/plain; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # no noise on stderr
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/sql"
        threading.Thread(target=self._server.serve_forever, name="sql-gateway", daemon=True).start()

    def stop(self) -> None:
        self._server.shutdown()