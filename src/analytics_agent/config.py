"""Reads and validates an agent's configuration.

An agent is a folder with:
  agent.yml (or another name, given with --config)  connections and settings
  instructions.md                                   role, rules and known weaknesses (optional)
  definitions/<name>.sql                            one definition per file (optional)

Relative paths are resolved relative to the folder the yml file is in. Values like
${NAME} or ${NAME:-default} are read from environment variables or from a .env file
next to the yml file.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import (BaseModel, ConfigDict, Field, PrivateAttr, SecretStr,
                      ValidationError, field_validator, model_validator)

DEFAULT_FILENAME = "agent.yml"
INSTRUCTIONS_FILE = "instructions.md"
DEFINITIONS_DIR = "definitions"
CONFIG_ENV_VAR = "ANALYTICS_AGENT_CONFIG"
CONNECTION_TYPES = ("sqlite", "duckdb", "mysql", "postgres")
RESERVED_NAMES = {"memory", "system", "temp", "main", "def", "information_schema", "pg_catalog"}
IDENTIFIER = r"^[a-z_][a-z0-9_]*$"
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    """Error in the configuration. The message is written for humans."""


class _Model(BaseModel):
    # Unknown fields are errors, so typos in the yml file are caught.
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

class TableFilter(_Model):
    """Which tables the agent sees. Patterns like 'tmp_*' are allowed.
    This controls visibility, not access: access is controlled by the database user."""
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class _Connection(_Model):
    name: str = Field(pattern=IDENTIFIER)
    description: str | None = None
    tables: TableFilter = Field(default_factory=TableFilter)

    @field_validator("name")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if value in RESERVED_NAMES:
            raise ValueError(f"'{value}' is a reserved name, choose another one")
        return value


class SqliteConnection(_Connection):
    type: Literal["sqlite"]
    path: Path

    @property
    def default_schema(self) -> str:
        return "main"


class DuckdbConnection(_Connection):
    type: Literal["duckdb"]
    path: Path

    @property
    def default_schema(self) -> str:
        return "main"


class MysqlConnection(_Connection):
    type: Literal["mysql"]
    host: str
    port: int = 3306
    database: str
    user: str
    password: SecretStr | None = None

    @property
    def default_schema(self) -> str:
        return self.database


class PostgresConnection(_Connection):
    type: Literal["postgres"]
    host: str
    port: int = 5432
    database: str
    user: str
    password: SecretStr | None = None
    # Written as `schema` in the yml file. The attribute has another name because
    # `schema` would shadow a method on pydantic's BaseModel.
    db_schema: str = Field("public", alias="schema")

    @property
    def default_schema(self) -> str:
        return self.db_schema


Connection = Annotated[
    Union[SqliteConnection, DuckdbConnection, MysqlConnection, PostgresConnection],
    Field(discriminator="type"),
]
FILE_CONNECTIONS = (SqliteConnection, DuckdbConnection)


# ---------------------------------------------------------------------------
# The rest of the configuration
# ---------------------------------------------------------------------------

class Limits(_Model):
    max_rows: int = Field(200, ge=1, le=10_000)
    max_rows_python: int = Field(1_000_000, ge=1)
    sql_timeout_s: int = Field(30, ge=1)
    python_timeout_s: int = Field(60, ge=1)
    python_max_timeout_s: int = Field(300, ge=1)

    @model_validator(mode="after")
    def _check_timeouts(self):
        if self.python_max_timeout_s < self.python_timeout_s:
            raise ValueError("python_max_timeout_s cannot be lower than python_timeout_s")
        return self


class NotebookConfig(_Model):
    enabled: bool = True
    dir: Path = Path("notebooks")
    max_chars: int = Field(4000, ge=500)
    max_figures: int = Field(4, ge=0, le=10)


class LogConfig(_Model):
    dir: Path = Path("logs")


class Security(_Model):
    lock_duckdb: bool = True


class Definition(_Model):
    name: str = Field(pattern=IDENTIFIER)
    description: str
    sql: str | None = None
    file: Path | None = None


class AgentConfig(_Model):
    # interactive: the user drives the analysis step by step; the agent does one step and waits.
    # autonomous:  the agent solves the whole question itself and delivers a finished answer.
    mode: Literal["interactive", "autonomous"] = "interactive"
    # Filled from instructions.md and definitions/*.sql, not from the yml file.
    instructions: str | None = None
    definitions: list[Definition] = Field(default_factory=list)


class Config(_Model):
    name: str = Field("analytics-agent", pattern=r"^[A-Za-z0-9_-]+$")
    connections: list[Connection] = Field(min_length=1)
    limits: Limits = Field(default_factory=Limits)
    notebook: NotebookConfig = Field(default_factory=NotebookConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    security: Security = Field(default_factory=Security)
    agent: AgentConfig = Field(default_factory=AgentConfig)

    _file: Path | None = PrivateAttr(default=None)
    _secrets: set = PrivateAttr(default_factory=set)
    _warnings: list = PrivateAttr(default_factory=list)

    @field_validator("connections")
    @classmethod
    def _unique_connections(cls, connections):
        _require_unique([c.name for c in connections], "connection")
        return connections

    @property
    def file(self) -> Path:
        """The path to the yml file that was read."""
        return self._file

    @property
    def root(self) -> Path:
        """The agent's folder. Relative paths are resolved from here."""
        return self._file.parent

    @property
    def secret_variables(self) -> set[str]:
        """Environment variables used by the yml file or .env. Kept away from the Python kernel."""
        return set(self._secrets)

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)


def _require_unique(names: list[str], what: str) -> None:
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"{what} names must be unique, these are repeated: {', '.join(duplicates)}")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def find_config_file(path: str | os.PathLike | None = None) -> Path:
    """Finds the yml file: the given path, ANALYTICS_AGENT_CONFIG, or agent.yml in
    this folder or a parent folder."""
    if path:
        file = Path(path).expanduser()
        if not file.is_file():
            raise ConfigError(f"Could not find the configuration file {file}.")
        return file.resolve()
    if os.environ.get(CONFIG_ENV_VAR):
        return find_config_file(os.environ[CONFIG_ENV_VAR])
    for folder in (Path.cwd(), *Path.cwd().parents):
        if (folder / DEFAULT_FILENAME).is_file():
            return (folder / DEFAULT_FILENAME).resolve()
    raise ConfigError("Could not find agent.yml in this folder or any parent folder. "
                      "Run `analytics-agent new` to create one, or give the path with --config.")


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Reads the yml file, instructions.md and definitions/, fills in variables and
    validates everything. Raises ConfigError."""
    file = find_config_file(path)
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{file.name} is not valid YAML:\n{e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{file.name} is empty or lacks a key: value structure.")
    _check_moved_fields(raw, file)

    env_file = _read_env_file(file.parent / ".env")
    used: set[str] = set()
    data = _substitute_variables(raw, {**env_file, **os.environ}, used, ())
    # A section with only comments under it is read as null. Treat it as
    # omitted, so the error message becomes "missing" and not "must be a list".
    data = {k: v for k, v in data.items() if v is not None}
    _add_agent_files(data, file.parent)

    try:
        config = Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format(e, data, file)) from None

    config._file = file
    config._secrets = used | set(env_file)
    config._warnings = _find_warnings(raw)
    _resolve_paths(config, file.parent)
    return config


_MOVED = {
    "instructions": "write the instructions in instructions.md next to {file}",
    "rules": "write the rules in instructions.md next to {file}",
    "definitions": "put each definition in definitions/<name>.sql next to {file}",
}


def _check_moved_fields(raw: dict, file: Path) -> None:
    agent = raw.get("agent")
    if not isinstance(agent, dict):
        return
    errors = [f"  - agent → {field}: {text.format(file=file.name)}"
              for field, text in _MOVED.items() if field in agent]
    if errors:
        raise ConfigError(f"{file.name} uses fields that have been moved out of the yml file:\n"
                          + "\n".join(errors))


def _add_agent_files(data: dict, root: Path) -> None:
    """Fills agent.instructions and agent.definitions from the files in the agent's folder."""
    agent = data.get("agent", {})
    if not isinstance(agent, dict):
        return  # pydantic produces the error message
    instructions = root / INSTRUCTIONS_FILE
    if instructions.is_file():
        text = instructions.read_text(encoding="utf-8").strip()
        if text:
            agent["instructions"] = text
    agent["definitions"] = _read_definitions(root / DEFINITIONS_DIR)
    data["agent"] = agent


def _read_definitions(folder: Path) -> list[dict]:
    """One definition per .sql file. The file name becomes the name (def.<name>), the
    comment at the top becomes the description. A file with only a comment is a text definition."""
    if not folder.is_dir():
        return []
    definitions = []
    for file in sorted(folder.glob("*.sql")):
        where = f"{DEFINITIONS_DIR}/{file.name}"
        if not re.match(IDENTIFIER, file.stem):
            raise ConfigError(f"{where}: the file name becomes the name of the definition (def.{file.stem}) "
                              "and may only contain lowercase letters a–z, digits and _. "
                              "The first character cannot be a digit.")
        description, sql = _split_comment(file.read_text(encoding="utf-8"))
        if not description:
            raise ConfigError(f"{where}: missing description. Start the file with a comment "
                              "(-- ...) that explains what the definition is.")
        definitions.append({"name": file.stem, "description": description,
                            "sql": sql or None, "file": file})
    return definitions


def _split_comment(text: str) -> tuple[str, str]:
    """Separates the comment lines at the top (-- ...) from the rest of the SQL."""
    lines = text.splitlines()
    description: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("--"):
            description.append(line[2:].strip())
        elif line:
            break
        i += 1
    sql = "\n".join(lines[i:]).strip().rstrip(";").strip()
    return " ".join(d for d in description if d), sql


def _read_env_file(path: Path) -> dict[str, str]:
    """Reads a simple .env file: NAME=value per line, # for comments."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _substitute_variables(node, environment: dict, used: set, path: tuple):
    if isinstance(node, dict):
        return {k: _substitute_variables(v, environment, used, path + (k,)) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute_variables(v, environment, used, path + (i,)) for i, v in enumerate(node)]
    if isinstance(node, str):
        def replace(match: re.Match) -> str:
            name, default = match.group(1), match.group(2)
            used.add(name)
            if name in environment:
                return environment[name]
            if default is not None:
                return default
            where = " → ".join(str(p) for p in path)
            raise ConfigError(f"{where}: the environment variable {name} is not set. "
                              "Set it in the environment or in .env next to the yml file.")
        return _VARIABLE.sub(replace, node)
    return node


def _abs(path: Path, root: Path) -> Path:
    path = path.expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def _resolve_paths(config: Config, root: Path) -> None:
    for connection in config.connections:
        if isinstance(connection, FILE_CONNECTIONS):
            connection.path = _abs(connection.path, root)
            if not connection.path.is_file():
                raise ConfigError(f"connections[{connection.name}] → path: file not found: {connection.path}")
    config.notebook.dir = _abs(config.notebook.dir, root)
    config.log.dir = _abs(config.log.dir, root)


def _find_warnings(raw: dict) -> list[str]:
    warnings = []
    for c in raw.get("connections") or []:
        if isinstance(c, dict) and isinstance(c.get("password"), str) and "${" not in c["password"]:
            warnings.append(f"connections[{c.get('name', '?')}]: the password is written in plain text. "
                            "Use ${VARIABLE} and put the value in .env or in the environment.")
    return warnings


# ---------------------------------------------------------------------------
# Error messages
# ---------------------------------------------------------------------------

_MESSAGES = {
    "missing": "missing (required)",
    "extra_forbidden": "unknown field – typo?",
    "int_parsing": "must be an integer",
    "int_type": "must be an integer",
    "bool_parsing": "must be true or false",
    "bool_type": "must be true or false",
    "string_type": "must be text",
    "list_type": "must be a list",
    "dict_type": "must be a group of fields (key: value)",
    "model_type": "must be a group of fields (key: value)",
    "path_type": "must be a file path",
}


def _format(error: ValidationError, data: dict, file: Path) -> str:
    lines = [f"{file.name} has {error.error_count()} error(s):"]
    for e in error.errors():
        lines.append(f"  - {_location(e['loc'], data)}: {_message(e)}")
    return "\n".join(lines)


def _location(loc: tuple, data) -> str:
    """Turns ('connections', 0, 'sqlite', 'path') into 'connections[crm] → path'."""
    parts: list[str] = []
    node = data
    i = 0
    while i < len(loc):
        part = loc[i]
        if isinstance(part, int) and isinstance(node, list) and parts:
            element = node[part] if part < len(node) else None
            name = element.get("name") if isinstance(element, dict) else None
            parts[-1] = f"{parts[-1]}[{name or part}]"
            node = element
            if i + 1 < len(loc) and loc[i + 1] in CONNECTION_TYPES:
                i += 1  # skip the type pydantic inserts for connections
        else:
            parts.append(str(part))
            node = node.get(part) if isinstance(node, dict) else None
        i += 1
    return " → ".join(parts) or "(top level)"


def _message(e: dict) -> str:
    type_, ctx, loc = e["type"], e.get("ctx") or {}, e["loc"]
    if loc == ("connections",) and type_ in ("missing", "too_short"):
        return "at least one SQL connection is required (see README, \"Connecting databases\")"
    if type_ == "union_tag_invalid":
        return f"unknown type '{ctx.get('tag')}'. Valid types: {', '.join(CONNECTION_TYPES)}"
    if type_ == "union_tag_not_found":
        return f"missing type ({', '.join(CONNECTION_TYPES)})"
    if type_ == "literal_error":
        return f"invalid value. Allowed: {ctx.get('expected', '')}"
    if type_ == "greater_than_equal":
        return f"must be at least {ctx.get('ge')}"
    if type_ == "less_than_equal":
        return f"can be at most {ctx.get('le')}"
    if type_ == "string_pattern_mismatch":
        if loc == ("name",):
            return "only letters a–z, digits, - and _ are allowed"
        return "only lowercase letters a–z, digits and _ are allowed, and the first character cannot be a digit"
    return _MESSAGES.get(type_) or e["msg"].removeprefix("Value error, ")