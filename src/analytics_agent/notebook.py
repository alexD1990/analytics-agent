"""Python notebook for the agent: one persistent Jupyter kernel per analysis.

Each analysis gets its own folder under notebook.dir, with analysis.ipynb and
all files the agent saves. A human can open the notebook, see exactly what
the agent did and run it again.

The kernel is started with the same Python as the server, without the
passwords from agent.yml and .env in its environment. Database access goes
through sql() and the SQL gateway.

Important: nothing may be written to stdout, because stdout is used by the MCP
protocol. The kernel is started with stdout to /dev/null and stderr to a log
file, and output from the code is fetched over the Jupyter protocol, never via print.
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import nbformat
from jupyter_client.kernelspec import KernelSpec, KernelSpecManager
from jupyter_client.manager import AsyncKernelManager
from mcp.server.fastmcp import Image

OUTPUT_TYPES = {"stream", "display_data", "execute_result", "error"}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SECRET_NAME = re.compile(r"PASS|SECRET|TOKEN|KEY|CREDENTIAL|AUTH", re.IGNORECASE)

# Run first in every kernel, and becomes the first code cell in the notebook.
STARTUP_CODE = '''\
# Setup (run automatically by analytics-agent)
%matplotlib inline
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from analytics_agent.kernel_client import sql

pd.set_option("display.max_rows", 30)
pd.set_option("display.max_columns", 20)
pd.set_option("display.width", 160)
plt.rcParams.update({"figure.figsize": (8, 5), "figure.dpi": 100})
'''

VARIABLES_CODE = '''\
def _show_variables():
    import pandas as pd
    for name, value in sorted(globals().items()):
        if name.startswith("_") or name in ("In", "Out", "sql"):
            continue
        if isinstance(value, (pd.DataFrame, pd.Series)):
            print(f"{name}: {type(value).__name__} {value.shape}")
        elif isinstance(value, (int, float, str, list, dict, tuple)):
            print(f"{name}: {type(value).__name__} = {repr(value)[:80]}")
_show_variables()
del _show_variables
'''


class _KernelSpecs(KernelSpecManager):
    """Always starts the kernel with the same Python as the server (same venv)."""

    def get_kernel_spec(self, kernel_name):
        return KernelSpec(
            argv=[sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
            display_name="analytics-agent",
            language="python",
        )


def _dir_name(title: str) -> str:
    # æøå are kept, so titles in Norwegian and Danish still give readable folder names.
    slug = re.sub(r"[^a-z0-9æøå]+", "-", title.lower()).strip("-")[:50] or "analysis"
    return f"{datetime.now():%Y-%m-%d_%H%M%S}_{slug}"


class Analysis:
    """One analysis = one Python kernel + one folder with a notebook and files."""

    def __init__(self, config, log, gateway):
        self.config = config
        self.log = log
        self.gateway = gateway
        self.km: AsyncKernelManager | None = None
        self.kc = None
        self.nb = None
        self.dir: Path | None = None
        self.path: Path | None = None
        self._kernel_log = None
        # Claude can call several tools in parallel; the kernel must be used by one call at a time.
        self.lock = asyncio.Lock()

    # -- lifecycle -----------------------------------------------------------

    def _kernel_env(self) -> dict[str, str]:
        secret = self.config.secret_variables
        env = {k: v for k, v in os.environ.items()
               if k not in secret and not _SECRET_NAME.search(k)}
        env["ANALYTICS_AGENT_GATEWAY"] = self.gateway.url
        env["ANALYTICS_AGENT_TOKEN"] = self.gateway.token
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (package_root, env.get("PYTHONPATH")) if p)
        return env

    async def start(self, title: str) -> None:
        await self.stop()
        self.dir = self.config.notebook.dir / _dir_name(title)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log.dir.mkdir(parents=True, exist_ok=True)
        self._kernel_log = self.log.kernel_log.open("a", encoding="utf-8")

        try:
            self.km = AsyncKernelManager(kernel_name="analytics-agent",
                                         kernel_spec_manager=_KernelSpecs())
            # stdout must NOT be inherited from the server: the server's stdout is the MCP protocol.
            await self.km.start_kernel(cwd=str(self.dir), env=self._kernel_env(),
                                       stdout=subprocess.DEVNULL, stderr=self._kernel_log)
            self.kc = self.km.client()
            self.kc.start_channels()
            await self.kc.wait_for_ready(timeout=60)
        except Exception:
            await self.stop()
            raise

        self.path = self.dir / "analysis.ipynb"
        self.nb = nbformat.v4.new_notebook()
        self.nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3",
                                          "language": "python"}
        self.nb.cells.append(nbformat.v4.new_markdown_cell(
            f"# {title}\n\n*Started {datetime.now():%Y-%m-%d %H:%M} by {self.config.name}.*"))

        reply, messages, _ = await self.run(STARTUP_CODE, timeout_s=60)
        self.add_cell(STARTUP_CODE, reply, messages)
        if reply is None or reply["content"]["status"] != "ok":
            error, _, _ = self.text_and_figures(messages)
            raise RuntimeError(f"The kernel setup failed:\n{error or 'timeout'}")

    async def stop(self) -> None:
        if self.kc is not None:
            self.kc.stop_channels()
        if self.km is not None:
            try:
                await self.km.shutdown_kernel(now=True)
            except Exception:
                pass
        if self._kernel_log is not None:
            self._kernel_log.close()
        self.km = self.kc = self.nb = self.path = self.dir = self._kernel_log = None

    def kill(self) -> None:
        """Synchronous cleanup when the server exits (atexit)."""
        try:
            process = getattr(getattr(self.km, "provisioner", None), "process", None)
            if process is not None and process.poll() is None:
                process.kill()
        except Exception:
            pass

    async def require_active(self) -> None:
        if self.nb is None:
            raise ValueError("No active analysis. Call new_analysis first.")
        if self.km is None or not await self.km.is_alive():
            raise ValueError("The kernel has stopped, for example because it ran out of memory. "
                             "Call new_analysis to start again.")

    # -- execution -----------------------------------------------------------

    async def run(self, code: str, timeout_s: int, store_history: bool = True):
        """Runs code and returns (reply, messages, interrupted)."""
        messages: list[dict] = []

        def collect(msg):
            if msg["msg_type"] in OUTPUT_TYPES:
                messages.append(msg)

        try:
            reply = await self.kc.execute_interactive(
                code,
                store_history=store_history,
                allow_stdin=False,   # input() must never read from the MCP stdin
                stop_on_error=False,
                timeout=timeout_s,
                output_hook=collect,  # without its own hook, jupyter_client writes to stdout
            )
        except TimeoutError:
            await self.km.interrupt_kernel()
            return None, messages, True
        return reply, messages, False

    def text_and_figures(self, messages: list[dict]) -> tuple[str, list[Image], int]:
        """Turns Jupyter messages into text and images for the model."""
        max_chars = self.config.notebook.max_chars
        max_figures = self.config.notebook.max_figures
        parts, figures, omitted = [], [], 0
        for msg in messages:
            type_, content = msg["msg_type"], msg["content"]
            if type_ == "stream":
                parts.append(content["text"])
            elif type_ == "error":
                parts.append(ANSI.sub("", "\n".join(content["traceback"])) + "\n")
            else:  # display_data / execute_result
                data = content.get("data", {})
                if "image/png" in data:
                    if len(figures) < max_figures:
                        figures.append(Image(data=base64.b64decode(data["image/png"]), format="png"))
                    else:
                        omitted += 1
                elif "text/plain" in data:
                    parts.append(data["text/plain"] + "\n")

        text = "".join(parts)
        if len(text) > max_chars:
            # Keep the start and the end: the start shows the result, the end the error message.
            half = max_chars // 2
            text = f"{text[:half]}\n\n[... {len(text) - max_chars} characters omitted ...]\n\n{text[-half:]}"
        return text, figures, omitted

    # -- the notebook file ---------------------------------------------------

    def add_cell(self, code: str, reply, messages: list[dict]) -> None:
        cell = nbformat.v4.new_code_cell(code)
        if reply is not None:
            cell.execution_count = reply["content"].get("execution_count")
        cell.outputs = [nbformat.v4.output_from_msg(m) for m in messages]
        if reply is None or reply["content"]["status"] != "ok":
            # Standard tag that lets the notebook keep running past the cell that failed.
            cell.metadata["tags"] = ["raises-exception"]
        self.nb.cells.append(cell)
        self.save()

    def add_note(self, markdown: str) -> None:
        self.nb.cells.append(nbformat.v4.new_markdown_cell(markdown))
        self.save()

    def save(self) -> None:
        nbformat.write(self.nb, self.path)

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.config.root))
        except ValueError:
            return str(path)