"""A single MCP server with SQL tools and a Python notebook, set up from agent.yml.

(Deliberately without "from __future__ import annotations": FastMCP reads the
tools' type annotations to build the schema the model sees.)
"""
import atexit

from mcp.server.fastmcp import FastMCP

from .brief import build_brief
from .config import Config
from .engine import Engine
from .gateway import SqlGateway
from .log import Log
from .notebook import VARIABLES_CODE, Analysis

MAX_SAMPLE_ROWS = 20


def _as_table(columns, rows) -> str:
    """Markdown table. NULL is shown explicitly."""
    def cell(v) -> str:
        return "NULL" if v is None else str(v).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(map(str, columns)) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def _number(n: int) -> str:
    return f"{n:,}"


def build_server(config: Config) -> FastMCP:
    engine = Engine(config)
    log = Log(config)
    brief = build_brief(config)
    limits = config.limits
    definitions = {d.name: d for d in config.agent.definitions if d.sql}

    analysis = None
    if config.notebook.enabled:
        analysis = Analysis(config, log, SqlGateway(engine, log))
        atexit.register(analysis.kill)

    tables = engine.tables()
    example = tables[0].display_name if tables else f"{config.connections[0].name}.table"

    mcp = FastMCP(config.name, instructions=brief)

    # -----------------------------------------------------------------------
    # Start
    # -----------------------------------------------------------------------

    @mcp.tool(description=(
        "Call this first in every conversation. Returns the instructions, rules and definitions "
        "you must follow"
        + (", and starts a new Python kernel with empty memory and a new notebook." if analysis else ".")
        + "\n\ntitle: short description of the analysis"
        + (", used as the name of the analysis folder." if analysis else ".")))
    @log.tool
    async def new_analysis(title: str) -> str:
        if analysis is None:
            return brief
        async with analysis.lock:
            await analysis.start(title)
            path = analysis.relative(analysis.path)
        return (f"{brief}\n\n---\nNew analysis started. Notebook: {path}\n"
                "In Python, sql(), pd, np and plt are available. Files you save end up in the analysis folder.")

    @mcp.prompt(name="instructions", description="The instructions and definitions the agent follows")
    def instructions() -> str:
        return brief

    # -----------------------------------------------------------------------
    # SQL
    # -----------------------------------------------------------------------

    @mcp.tool(description=(
        "Shows all tables the agent has access to, with row counts, and the definitions "
        "(def.<name>) to use when they fit."))
    @log.tool
    def overview() -> str:
        all_tables = engine.tables()
        count = len(all_tables) <= 50
        rows = []
        for t in all_tables:
            n = engine.row_count(t) if count else None
            rows.append((t.display_name, "?" if n is None else n))
        parts = ["## Tables", _as_table(["table", "row_count"], rows)]
        if not count:
            parts.append("(Row counts are not computed because there are many tables.)")
        if definitions:
            parts += ["## Definitions (use these when they fit)",
                      _as_table(["name", "description"],
                                [(f"def.{d.name}", d.description.strip()) for d in definitions.values()])]
        return "\n\n".join(parts)

    @mcp.tool(description=(
        "Shows columns, data types and sample rows for one table or definition.\n\n"
        f"table: the name as overview shows it, for example '{example}' or 'def.<name>'.\n"
        f"sample_rows: number of rows to show (max {MAX_SAMPLE_ROWS})."))
    @log.tool
    def describe_table(table: str, sample_rows: int = 5) -> str:
        t = engine.find(table)
        if t is None:
            valid = [x.display_name for x in engine.definitions() + engine.tables()]
            listing = ", ".join(valid[:50]) + (" …" if len(valid) > 50 else "")
            raise ValueError(f"Unknown table '{table}'. Valid names: {listing}")

        n = max(0, min(int(sample_rows), MAX_SAMPLE_ROWS))
        columns, names, rows = engine.describe(t, n)
        parts = [f"## {t.display_name}"]
        d = definitions.get(t.name) if t.connection == "memory" else None
        if d:
            parts += [d.description.strip(), f"```sql\n{d.sql.strip()}\n```"]
        parts += ["### Columns", _as_table(["column", "type"], columns),
                  "### Sample rows", _as_table(names, rows)]
        return "\n\n".join(parts)

    @mcp.tool(description=(
        f"Runs a single SELECT query and returns at most {limits.max_rows} rows as a table.\n\n"
        f"Tables are referenced as connection.table, for example {example}, and definitions as "
        "def.<name>. The query can join across connections. Only a single SELECT or "
        f"WITH query is allowed. Timeout: {limits.sql_timeout_s} s."
        + (" If you need full datasets, statistics or figures: use run_python and fetch data "
           "with sql() there." if analysis else "")))
    @log.tool
    def run_query(sql: str) -> str:
        columns, rows, more = engine.run(sql)
        result = _as_table(columns, rows)
        if more:
            result += (f"\n\n(Showing the first {limits.max_rows} rows, but the result has more. "
                       "Use aggregation or LIMIT"
                       + (", or fetch the data with sql() in run_python.)" if analysis else ".)"))
        return result

    # -----------------------------------------------------------------------
    # Notebook
    # -----------------------------------------------------------------------

    if analysis is None:
        return mcp

    @mcp.tool(description=(
        "Runs Python code in the analysis kernel and returns output and figures.\n\n"
        "Variables persist between calls, as in a notebook. Fetch data with "
        f"df = sql(\"SELECT ...\") (max {_number(limits.max_rows_python)} rows; aggregate in SQL "
        "first when you can). pd, np and plt are imported. End the cell with an expression, or "
        "use print(), to see results. Figures are returned as images: look at them before you "
        "conclude. Errors are returned as a traceback so you can fix the code.\n\n"
        f"timeout_s: maximum run time in seconds (default {limits.python_timeout_s}, "
        f"max {limits.python_max_timeout_s})."))
    @log.tool
    async def run_python(code: str, timeout_s: int = limits.python_timeout_s):
        async with analysis.lock:
            await analysis.require_active()
            timeout_s = max(1, min(int(timeout_s), limits.python_max_timeout_s))
            reply, messages, interrupted = await analysis.run(code, timeout_s)
            analysis.add_cell(code, reply, messages)

        text, figures, omitted = analysis.text_and_figures(messages)
        notes = []
        if interrupted:
            notes.append(f"The cell was interrupted after {timeout_s} s. Aggregate in SQL before you fetch "
                         "data, or split up the work. If the kernel still hangs, call new_analysis.")
        if omitted:
            notes.append(f"{omitted} figure(s) were not shown "
                         f"(max {config.notebook.max_figures} per cell).")
        if not text and not figures and not interrupted:
            notes.append("No output. End the cell with an expression or use print().")

        number = reply["content"].get("execution_count") if reply else None
        head = f"[{number}]" if number else "[interrupted]"
        response = "\n".join([head, text.rstrip(), *notes]).strip()
        return [response, *figures]

    @mcp.tool(description="Shows DataFrames, Series and simple variables that exist in the kernel now.")
    @log.tool
    async def show_variables() -> str:
        async with analysis.lock:
            await analysis.require_active()
            _, messages, _ = await analysis.run(VARIABLES_CODE, 15, store_history=False)
        text, _, _ = analysis.text_and_figures(messages)
        return text.strip() or "No variables yet."

    @mcp.tool(description=(
        "Adds a text cell (markdown) to the notebook."
        + (" Use it when the user asks for a note or a summary."
           if config.agent.mode == "interactive" else
           " Use it to explain what you are doing and why, and end each analysis with a "
           "summary of findings, assumptions and data quality issues.")))
    @log.tool
    async def write_note(markdown: str) -> str:
        async with analysis.lock:
            if analysis.nb is None:
                raise ValueError("No active analysis. Call new_analysis first.")
            analysis.add_note(markdown)
            return f"Note added to {analysis.relative(analysis.path)}"

    return mcp