"""Logging of tool calls to <log.dir>/<name>.jsonl.

The log is written to a file and never to stdout, because stdout is used by the MCP protocol.
"""
from __future__ import annotations

import inspect
import json
import threading
import time
from datetime import datetime
from functools import wraps


def _as_text(result) -> str:
    """Turns a result into text for the log. Images are logged as <Image>."""
    if isinstance(result, (list, tuple)):
        result = " ".join(p if isinstance(p, str) else f"<{type(p).__name__}>" for p in result)
    return str(result)[:2000]


class Log:
    def __init__(self, config):
        self.dir = config.log.dir
        self.file = self.dir / f"{config.name}.jsonl"
        self.kernel_log = self.dir / f"{config.name}-kernel.log"
        self._lock = threading.Lock()

    def write(self, tool: str, input_data: dict, status: str, output, start: float) -> None:
        event = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "tool": tool,
            "input": input_data,
            "status": status,
            "output": _as_text(output),
            "ms": round((time.perf_counter() - start) * 1000),
        }
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def tool(self, function):
        """Decorator that logs input, result, status and time. Supports async."""
        name = function.__name__

        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_wrapper(*args, **kwargs):
                start = time.perf_counter()
                try:
                    result = await function(*args, **kwargs)
                except Exception as e:
                    self.write(name, kwargs, "error", str(e), start)
                    raise
                self.write(name, kwargs, "ok", result, start)
                return result
            return async_wrapper

        @wraps(function)
        def sync_wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                result = function(*args, **kwargs)
            except Exception as e:
                self.write(name, kwargs, "error", str(e), start)
                raise
            self.write(name, kwargs, "ok", result, start)
            return result
        return sync_wrapper