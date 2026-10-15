"""Assembles the instructions the agent gets, from the yml file and instructions.md.

The brief is delivered in three places, so it is guaranteed to reach the model:
  1. as the response from new_analysis, which the agent calls first in every conversation,
  2. as the server's instructions,
  3. as the MCP prompt "instructions" (can be attached from the + menu in Claude Desktop).

The way of working depends on agent.mode:
  interactive – the user drives the analysis step by step; the agent does one step and waits.
  autonomous  – the agent solves the whole question itself and delivers a finished answer.
"""
from __future__ import annotations

from .config import Config

DEFAULT_INSTRUCTIONS = (
    "You are a thorough data analyst. You answer concretely with numbers, validate the data "
    "before you draw conclusions, and are clear about uncertainty and assumptions."
)


def _numbered(points: list[str]) -> str:
    return "\n".join(f"{i}. {p}" for i, p in enumerate(points, 1))


def _interactive(config: Config) -> tuple[str, list[str]]:
    notebook = config.notebook.enabled
    points = [
        "Do what the user asks for in this step, and no more. Do not expand the task on your own initiative.",
        "Answer briefly: the result, possibly one figure, and what you have saved"
        + (" in the kernel, with descriptive variable names (for example \"saved as `revenue_by_month`\"), "
           "so the user can refer to it later." if notebook else "."),
        f"Use `run_query` for quick lookups (max {config.limits.max_rows} rows returned)"
        + (", and `run_python` when the step needs calculations or figures. Fetch data with "
           "`sql(\"SELECT ...\")` inside Python, and never paste tables in as code. "
           "`pd`, `np` and `plt` are imported." if notebook else "."),
        "If the step requires a methodological choice (rounding, denominator, period boundaries, handling of "
        "anomalies), say which choice you made. If the choice materially affects the answer, ask before you continue.",
        "Briefly point out data quality issues you notice along the way, but do not investigate them unless "
        "the user asks you to.",
    ]
    if config.agent.instructions:
        points.append("The rules in the instructions above always apply, but within the step the user asks for. "
                      "They are not a reason to expand the step.")
    if config.agent.definitions:
        points.append("Use the definitions below when they fit, instead of building your own logic.")
    if notebook:
        points.append("Look at every figure you make before you answer: axes, units and that it shows what you think.")
    points.append("End with one short suggestion for the next step, and wait for the user.")
    if notebook:
        points.append("Use `write_note` only when the user asks for it.")
    points.append("If the user asks for a complete analysis or a finished answer, work independently "
                  "until the question is answered, then go back to step by step.")
    return "Way of working (interactive: the user drives the analysis step by step)", points


def _autonomous(config: Config) -> tuple[str, list[str]]:
    notebook = config.notebook.enabled
    points = [
        "Get an overview with `overview` and `describe_table`, and test small queries with "
        f"`run_query` (max {config.limits.max_rows} rows returned).",
    ]
    if notebook:
        points += [
            "Use `run_python` for calculations over full datasets, statistics and figures. "
            "Fetch data with `sql(\"SELECT ...\")` inside Python, and never paste tables in as code. "
            "`pd`, `np` and `plt` are imported.",
            "Look at every figure you make before you conclude: axes, units and that it shows what you think.",
        ]
    if config.agent.instructions:
        points.append("The rules in the instructions above take precedence over this way of working if they conflict.")
    if config.agent.definitions:
        points.append("Use the definitions below when they fit, instead of building your own logic.")
    points.append("Always state which filters, definitions and methodological choices you used in the answer.")
    if notebook:
        points.append("Use `write_note` to explain choices along the way, and end with a note "
                      "that summarizes findings, assumptions and data quality issues.")
    points.append("If the user asks to take it step by step, do one step at a time and wait for the user.")
    return "Way of working (autonomous: solve the whole question and deliver a finished answer)", points


def build_brief(config: Config) -> str:
    agent = config.agent
    title, way_of_working = (_interactive if agent.mode == "interactive" else _autonomous)(config)

    parts = [f"# {config.name}"]
    if agent.instructions:
        parts.append(agent.instructions.strip())
    else:
        parts.append("## Role and instructions\n" + DEFAULT_INSTRUCTIONS)
    parts.append(f"## {title}\n" + _numbered(way_of_working))

    if agent.definitions:
        lines = ["## Definitions",
                 "Use these definitions. If the question requires a deviation, say so explicitly and justify it."]
        for d in agent.definitions:
            line = f"- **{d.name}**: {d.description.strip()}"
            if d.sql:
                line += f" Available as `def.{d.name}`."
            lines.append(line)
        parts.append("\n".join(lines))

    connections = ["## Connections"]
    for c in config.connections:
        connections.append(f"- `{c.name}` ({c.type})" + (f": {c.description.strip()}" if c.description else ""))
    connections.append(f"\nTables are referenced as connection.table, for example `{config.connections[0].name}.<table>`. "
                       "Queries can join across connections.")
    parts.append("\n".join(connections))

    return "\n\n".join(parts)