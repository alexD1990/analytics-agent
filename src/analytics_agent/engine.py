"""DuckDB as a shared engine for all the connections in agent.yml.

Each connection is attached (ATTACH) read-only in a single DuckDB database, so
the agent uses one SQL dialect and can join across sources. Definitions with
sql become views in the def schema. Finally DuckDB is locked: no access to
files outside the data folders, no network calls from queries and no changes
to settings.

Passwords are given to DuckDB as temporary secrets (CREATE SECRET). They are
shown redacted if anyone asks for them.
"""
from __future__ import annotations

import fnmatch
import re
import threading
from dataclasses import dataclass
from typing import Callable, TypeVar

import duckdb
import pandas as pd

from .config import (FILE_CONNECTIONS, Config, DuckdbConnection, PostgresConnection,
                     SqliteConnection, TableFilter)

DEF_SCHEMA = "def"
_SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast", "performance_schema", "mysql", "sys"}
_EXTENSIONS = {"sqlite": "sqlite", "mysql": "mysql", "postgres": "postgres"}
_VECTORS_PER_CHUNK = 50  # sql() fetches data in chunks of 50 × 2048 rows

T = TypeVar("T")


class EngineError(Exception):
    """Error from the engine, with a message that can be shown to the agent or the user."""


class QueryTimeout(EngineError):
    pass


def _literal(value) -> str:
    """SQL string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def _ident(value) -> str:
    """Quoted SQL identifier."""
    return '"' + str(value).replace('"', '""') + '"'


@dataclass(frozen=True)
class Table:
    connection: str       # DuckDB catalog (the connection's name, or memory for def)
    schema: str
    name: str
    display_name: str     # how the agent sees and writes the name, e.g. crm.customers

    @property
    def quoted(self) -> str:
        return ".".join(_ident(p) for p in (self.connection, self.schema, self.name))


def _visible(filter: TableFilter, name: str, display: str) -> bool:
    def matches(patterns: list[str]) -> bool:
        return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(display, p) for p in patterns)
    if filter.include and not matches(filter.include):
        return False
    return not matches(filter.exclude)

_DEF_REFERENCE = re.compile(r'\bdef\s*\.\s*"?([A-Za-z_][A-Za-z0-9_]*)"?', re.IGNORECASE)


def _ordered(definitions: list) -> list:
    """Sorts the definitions so each comes after the ones it uses (def.<name>).
    Independent definitions are in alphabetical order."""
    by_name = {d.name: d for d in definitions}
    remaining = {d.name: {r.lower() for r in _DEF_REFERENCE.findall(d.sql)} & by_name.keys()
                 for d in definitions}
    done: set[str] = set()
    order = []
    while remaining:
        ready = sorted(n for n, deps in remaining.items() if deps <= done)
        if not ready:
            raise EngineError("These definitions depend on each other in a cycle, directly or "
                              f"indirectly: {', '.join(sorted(remaining))}. One of them must stop using another.")
        for n in ready:
            order.append(by_name[n])
            done.add(n)
            del remaining[n]
    return order

class Engine:
    def __init__(self, config: Config, lock: bool | None = None):
        """Connects to all sources. lock=None follows security.lock_duckdb in agent.yml."""
        self.config = config
        self._con = duckdb.connect(":memory:")
        self._cursor_lock = threading.Lock()
        self._load_extensions()
        for connection in config.connections:
            try:
                self._attach(connection)
            except duckdb.Error as e:
                raise EngineError(f"Could not connect to '{connection.name}' ({connection.type}): {e}") from None
        self._create_definitions()
        self.locked = config.security.lock_duckdb if lock is None else lock
        if self.locked:
            self._lock_down()

    # -- setup ---------------------------------------------------------------

    def _load_extensions(self) -> None:
        for type_ in sorted({c.type for c in self.config.connections}):
            extension = _EXTENSIONS.get(type_)
            if extension is None:
                continue
            try:
                self._con.execute(f"LOAD {extension}")
            except duckdb.Error:
                try:
                    self._con.execute(f"INSTALL {extension}")
                    self._con.execute(f"LOAD {extension}")
                except duckdb.Error as e:
                    raise EngineError(
                        f"Could not load the DuckDB extension '{extension}'. It is downloaded the first "
                        f"time and requires network access. Details: {e}") from None

    def _attach(self, c) -> None:
        # Connection names are validated as simple identifiers in config.py.
        if isinstance(c, SqliteConnection):
            self._con.execute(f"ATTACH {_literal(c.path)} AS {c.name} (TYPE sqlite, READ_ONLY)")
            return
        if isinstance(c, DuckdbConnection):
            self._con.execute(f"ATTACH {_literal(c.path)} AS {c.name} (READ_ONLY)")
            return

        secret = f"agent_{c.name}"
        fields = [f"TYPE {c.type}", f"HOST {_literal(c.host)}", f"PORT {int(c.port)}",
                  f"DATABASE {_literal(c.database)}", f"USER {_literal(c.user)}"]
        if c.password is not None:
            fields.append(f"PASSWORD {_literal(c.password.get_secret_value())}")
        self._con.execute(f"CREATE SECRET {secret} ({', '.join(fields)})")

        options = [f"TYPE {c.type}", f"SECRET {secret}", "READ_ONLY"]
        if isinstance(c, PostgresConnection):
            options.insert(1, f"SCHEMA {_literal(c.db_schema)}")
        self._con.execute(f"ATTACH '' AS {c.name} ({', '.join(options)})")

    def _create_definitions(self) -> None:
        definitions = [d for d in self.config.agent.definitions if d.sql]
        if not definitions:
            return
        self._con.execute(f"CREATE SCHEMA memory.{DEF_SCHEMA}")
        for d in _ordered(definitions):
            try:
                self._con.execute(f"CREATE VIEW memory.{DEF_SCHEMA}.{d.name} AS {d.sql}")
            except duckdb.Error as e:
                source = f"definitions/{d.file.name}" if d.file else f"The definition '{d.name}'"
                raise EngineError(f"{source} is invalid: {e}") from None

    def _lock_down(self) -> None:
        dirs = sorted({str(c.path.parent) for c in self.config.connections
                       if isinstance(c, FILE_CONNECTIONS)})
        try:
            if dirs:
                self._con.execute(f"SET allowed_directories = [{', '.join(_literal(d) for d in dirs)}]")
            self._con.execute("SET enable_external_access = false")
            self._con.execute("SET lock_configuration = true")
        except duckdb.Error as e:
            raise EngineError(f"Could not lock DuckDB ({e}). Upgrade duckdb, or set "
                              "security.lock_duckdb: false in agent.yml.") from None

    # -- helpers -------------------------------------------------------------

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        with self._cursor_lock:
            return self._con.cursor()

    def _with_timeout(self, cur, seconds: int, work: Callable[[], T]) -> T:
        interrupted = threading.Event()

        def interrupt():
            interrupted.set()
            cur.interrupt()

        timer = threading.Timer(seconds, interrupt)
        timer.daemon = True
        timer.start()
        try:
            return work()
        except duckdb.Error:
            if interrupted.is_set():
                raise QueryTimeout(f"The query was interrupted after {seconds} s. Filter earlier, "
                                   "aggregate more or fetch less data.") from None
            raise
        finally:
            timer.cancel()

    def check_select(self, sql: str) -> str:
        """Accepts only a single SELECT or WITH query. Returns the query without a semicolon."""
        cur = self._cursor()
        try:
            statements = cur.extract_statements(sql)
        except duckdb.Error as e:
            raise EngineError(f"Invalid SQL: {e}") from None
        finally:
            cur.close()
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise EngineError("Only a single SELECT or WITH query is allowed.")
        return sql.strip().rstrip(";").rstrip()

    # -- catalog -------------------------------------------------------------

    def tables(self) -> list[Table]:
        """Tables and views the agent should see, after the table filters in agent.yml."""
        cur = self._cursor()
        try:
            rows = cur.execute(
                "SELECT table_catalog, table_schema, table_name FROM information_schema.tables "
                "ORDER BY 1, 2, 3").fetchall()
        finally:
            cur.close()
        connections = {c.name: c for c in self.config.connections}
        result = []
        for catalog, schema, name in rows:
            c = connections.get(catalog)
            if c is None or schema in _SYSTEM_SCHEMAS:
                continue
            display = f"{catalog}.{name}" if schema == c.default_schema else f"{catalog}.{schema}.{name}"
            if _visible(c.tables, name, display):
                result.append(Table(catalog, schema, name, display))
        return result

    def definitions(self) -> list[Table]:
        return [Table("memory", DEF_SCHEMA, d.name, f"{DEF_SCHEMA}.{d.name}")
                for d in self.config.agent.definitions if d.sql]

    def find(self, display_name: str) -> Table | None:
        for t in self.definitions() + self.tables():
            if t.display_name == display_name.strip():
                return t
        return None

    def row_count(self, table: Table, seconds: int = 5) -> int | None:
        cur = self._cursor()
        try:
            return self._with_timeout(
                cur, seconds, lambda: cur.execute(f"SELECT COUNT(*) FROM {table.quoted}").fetchone()[0])
        except (EngineError, duckdb.Error):
            return None
        finally:
            cur.close()

    def describe(self, table: Table, sample_rows: int):
        """Returns (columns as [(name, type)], column names, sample rows)."""
        cur = self._cursor()

        def work():
            columns = [(c[0], c[1]) for c in cur.execute(f"DESCRIBE {table.quoted}").fetchall()]
            cur.execute(f"SELECT * FROM {table.quoted} LIMIT {int(sample_rows)}")
            return columns, [d[0] for d in cur.description], cur.fetchall()

        try:
            return self._with_timeout(cur, self.config.limits.sql_timeout_s, work)
        finally:
            cur.close()

    # -- queries -------------------------------------------------------------

    def run(self, sql: str):
        """For run_query: returns (columns, rows, more_rows_exist)."""
        query = self.check_select(sql)
        limits = self.config.limits
        cur = self._cursor()

        def work():
            cur.execute(query)
            return [d[0] for d in cur.description], cur.fetchmany(limits.max_rows + 1)

        try:
            columns, rows = self._with_timeout(cur, limits.sql_timeout_s, work)
        finally:
            cur.close()
        return columns, rows[:limits.max_rows], len(rows) > limits.max_rows

    def df(self, sql: str) -> pd.DataFrame:
        """For sql() in Python: the whole result as a DataFrame, up to max_rows_python.
        If the limit is exceeded, the call fails instead of silently truncating the data."""
        query = self.check_select(sql)
        limits = self.config.limits
        cur = self._cursor()

        def work() -> pd.DataFrame:
            cur.execute(query)
            chunks, count = [], 0
            while True:
                chunk = cur.fetch_df_chunk(_VECTORS_PER_CHUNK)
                if chunk is None or len(chunk) == 0:
                    if not chunks and chunk is not None:
                        chunks.append(chunk)  # empty result, but with columns
                    break
                chunks.append(chunk)
                count += len(chunk)
                if count > limits.max_rows_python:
                    raise EngineError(
                        f"The result has more than {limits.max_rows_python:,} rows. "
                        "Aggregate or filter in SQL before fetching the data.")
            if not chunks:
                return pd.DataFrame(columns=[d[0] for d in cur.description])
            return chunks[0] if len(chunks) == 1 else pd.concat(chunks, ignore_index=True)

        try:
            return self._with_timeout(cur, limits.sql_timeout_s, work)
        finally:
            cur.close()

    def close(self) -> None:
        self._con.close()