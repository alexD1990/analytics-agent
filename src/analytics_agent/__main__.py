"""Command line for analytics-agent.

  analytics-agent new [dir]        creates a new agent: agent.yml, instructions.md and definitions/
  analytics-agent check            validates the configuration and tests all connections
  analytics-agent brief            shows the instructions the agent gets
  analytics-agent desktop-config   prints the entry for claude_desktop_config.json
  analytics-agent serve            starts the MCP server (used by Claude Desktop)

All commands except new take --config <path>. Without it, ANALYTICS_AGENT_CONFIG is used,
or agent.yml in this folder or a parent folder.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import sys

from .config import Config, ConfigError, load_config


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def _privileges(engine, connection) -> str | None:
    """Warns if the database user can write. Best effort."""
    import duckdb
    cur = engine._cursor()
    try:
        if connection.type == "mysql":
            rows = cur.execute(f"SELECT * FROM mysql_query('{connection.name}', 'SHOW GRANTS')").fetchall()
            text = " ".join(str(r[0]) for r in rows).upper()
            dangerous = [p for p in ("ALL PRIVILEGES", "INSERT", "UPDATE", "DELETE", "DROP", "ALTER")
                         if p in text]
        else:
            rows = cur.execute(
                f"SELECT * FROM postgres_query('{connection.name}', $$"
                "SELECT DISTINCT privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = current_user AND privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE') "
                "UNION ALL SELECT 'SUPERUSER' FROM pg_roles WHERE rolname = current_user AND rolsuper"
                "$$)").fetchall()
            dangerous = [r[0] for r in rows]
    except duckdb.Error as e:
        return f"Could not check the database user's privileges ({e})."
    finally:
        cur.close()
    if dangerous:
        return (f"The database user has write privileges ({', '.join(dangerous)}). The agent connects "
                "read-only, but a dedicated user with SELECT only is safer.")
    return None


def check(config: Config) -> int:
    from .engine import Engine, EngineError

    ok = True
    print(f"Configuration: {config.file}")
    for warning in config.warnings:
        print(f"  ! {warning}")

    # 1. Connect without the lock: tables, filters and privileges
    print(f"\nConnections ({len(config.connections)}):")
    try:
        engine = Engine(config, lock=False)
    except EngineError as e:
        print(f"  ✗ {e}")
        return 1
    all_tables = engine.tables()
    for c in config.connections:
        tables = [t for t in all_tables if t.connection == c.name]
        print(f"  ✓ {c.name} ({c.type}): {len(tables)} tables")
        for t in tables[:15]:
            print(f"      {t.display_name}")
        if len(tables) > 15:
            print(f"      … and {len(tables) - 15} more")
        if not tables:
            print("    ! No tables are visible. Check tables.include/exclude and the privileges.")
        if c.type in ("mysql", "postgres"):
            message = _privileges(engine, c)
            if message:
                print(f"    ! {message}")
    engine.close()

    # 2. As the server runs: locked, with definitions
    print("\nHow the agent sees the data" + (" (DuckDB locked):" if config.security.lock_duckdb else ":"))
    try:
        engine = Engine(config)
        for c in config.connections:
            first = next((t for t in engine.tables() if t.connection == c.name), None)
            if first is not None:
                engine.run(f"SELECT * FROM {first.quoted} LIMIT 1")
                print(f"  ✓ {c.name}: query works")
        for d in engine.definitions():
            engine.run(f"SELECT * FROM {d.quoted} LIMIT 1")
            print(f"  ✓ {d.display_name}")
        engine.close()
    except Exception as e:
        ok = False
        print(f"  ✗ {e}")
        if config.security.lock_duckdb and "external" in str(e).lower():
            print("    The lock seems to be blocking the connection. Try security.lock_duckdb: false.")

    # 3. Notebook
    if config.notebook.enabled:
        print("\nNotebook:")
        missing = [p for p in ("ipykernel", "jupyter_client", "nbformat", "matplotlib")
                   if importlib.util.find_spec(p) is None]
        if missing:
            ok = False
            print(f"  ✗ Missing packages: {', '.join(missing)}. Run pip install -e . again.")
        else:
            print(f"  ✓ Analyses are saved in {config.notebook.dir}")

    print(f"\nLog: {config.log.dir / (config.name + '.jsonl')}")
    print("\nEverything looks good." if ok else "\nFix the errors above and run check again.")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# desktop-config
# ---------------------------------------------------------------------------

def desktop_config(config: Config) -> int:
    file = str(config.file)
    python = sys.executable
    distro = os.environ.get("WSL_DISTRO_NAME")

    if distro:
        command = f"{shlex.quote(python)} -m analytics_agent serve --config {shlex.quote(file)}"
        entry = {"command": "wsl.exe", "args": ["-d", distro, "-e", "bash", "-lc", command]}
        location = r"%APPDATA%\Claude\claude_desktop_config.json (in Windows)"
    else:
        entry = {"command": python, "args": ["-m", "analytics_agent", "serve", "--config", file]}
        if sys.platform == "darwin":
            location = "~/Library/Application Support/Claude/claude_desktop_config.json"
        elif sys.platform == "win32":
            location = r"%APPDATA%\Claude\claude_desktop_config.json"
        else:
            location = "~/.config/Claude/claude_desktop_config.json"

    print(f"Paste this under \"mcpServers\" in {location}:\n")
    print(f"\"{config.name}\": " + json.dumps(entry, indent=2, ensure_ascii=False))
    print("\nThen quit Claude Desktop completely and start the app again.")
    return 0

# ---------------------------------------------------------------------------
# new
# ---------------------------------------------------------------------------

_SKELETON_YML = """\
name: {name}

agent:
  mode: interactive

connections:
  # At least one connection is required. See README, "Connecting databases".
  # - name: crm
  #   type: sqlite
  #   path: data/crm.db
"""

_SKELETON_INSTRUCTIONS = """\
## Role
You are a data analyst for [organization]. You answer concretely with numbers and are clear
about uncertainty and assumptions.

## Data sources
- `[connection]`: what it contains and what it is used for.

## Rules
1. Before you give a final answer, validate the data: look for duplicates, NULL values,
   invalid statuses and values that do not match across connections.
2. When a methodological choice can change the number you report, state the choice explicitly.

## Known weaknesses
- [field or table that is unreliable, and why]
"""


def new(dir: str, name: str) -> int:
    import re
    from pathlib import Path

    from .config import DEFAULT_FILENAME, DEFINITIONS_DIR, INSTRUCTIONS_FILE

    if not re.match(r"^[A-Za-z0-9_-]+$", name):
        print("--name may only contain letters a–z, digits, - and _.", file=sys.stderr)
        return 2

    root = Path(dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    print(f"New agent in {root}:")

    files = {
        root / DEFAULT_FILENAME: _SKELETON_YML.format(name=name),
        root / INSTRUCTIONS_FILE: _SKELETON_INSTRUCTIONS,
    }
    for path, content in files.items():
        if path.exists():
            print(f"  – {path.name} already exists, left it untouched")
        else:
            path.write_text(content, encoding="utf-8")
            print(f"  ✓ {path.name}")

    definitions_dir = root / DEFINITIONS_DIR
    if definitions_dir.exists():
        print(f"  – {DEFINITIONS_DIR}/ already exists")
    else:
        definitions_dir.mkdir()
        print(f"  ✓ {DEFINITIONS_DIR}/")

    yml = root / DEFAULT_FILENAME
    print("\nNext steps:")
    print(f"  1. Add the connections in {yml} (see README, \"Connecting databases\").")
    print(f"  2. Write the role, data sources, rules and known weaknesses in {root / INSTRUCTIONS_FILE}.")
    print(f"  3. analytics-agent check --config {yml}")
    print(f"  4. analytics-agent desktop-config --config {yml}")
    return 0

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", "-c", help="path to the agent's yml file")

    parser = argparse.ArgumentParser(prog="analytics-agent", description="Analytics agent for Claude Desktop.")
    commands = parser.add_subparsers(dest="command", required=True)
    new_parser = commands.add_parser("new", help="creates a new agent: agent.yml, instructions.md and definitions/")
    new_parser.add_argument("dir", nargs="?", default=".", help="where the agent should live (default: this folder)")
    new_parser.add_argument("--name", default="analytics-agent", help="the name the server gets in Claude Desktop")
    commands.add_parser("check", parents=[common], help="validates the configuration and tests the connections")
    commands.add_parser("brief", parents=[common], help="shows the instructions the agent gets")
    commands.add_parser("desktop-config", parents=[common],
                        help="prints the entry for claude_desktop_config.json")
    commands.add_parser("serve", parents=[common], help="starts the MCP server")
    args = parser.parse_args(argv)

    if args.command == "new":
        return new(args.dir, args.name)

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Error in the configuration:\n{e}", file=sys.stderr)
        return 2

    if args.command == "check":
        return check(config)
    if args.command == "brief":
        from .brief import build_brief
        print(build_brief(config))
        return 0
    if args.command == "desktop-config":
        return desktop_config(config)

    # serve: stdout belongs to the MCP protocol, so errors go to stderr.
    from .engine import EngineError
    from .server import build_server
    try:
        server = build_server(config)
    except EngineError as e:
        print(f"Could not start: {e}", file=sys.stderr)
        return 1
    server.run()
    return 0

if __name__ == "__main__":
    sys.exit(main())