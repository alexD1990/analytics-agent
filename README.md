# analytics-agent

An analytics agent for Claude Desktop. The agent can read SQL databases, run Python in a
notebook for analysis and figures, and follows instructions and definitions you decide.

Supported databases: **SQLite, DuckDB, MySQL and PostgreSQL**. All of them are attached
read-only through DuckDB, so the agent uses one SQL dialect and can join across the
sources.

The code is the same for everyone. What makes an agent yours is a small folder with your
connections, your instructions and your business definitions.

## Requirements

- [Claude Desktop](https://claude.ai/download) on macOS or Windows
- Python 3.10 or newer
- git
- Access to your data: a SQLite or DuckDB file, or the address of a MySQL or PostgreSQL
  database and a user with read access

On Windows you can run analytics-agent either in WSL or directly in Windows. Both work;
`desktop-config` detects which one you use and writes the right entry for Claude Desktop.

## What leaves your machine

Your databases stay where they are, and the agent never gets a copy of them. But Claude
works by reading the results of the queries and Python cells it runs: tables, numbers,
sample rows and figures are sent to Claude as part of the conversation, just like text you
type yourself.

Before you connect sensitive data (customer data, personal data, financial records), check
that this is allowed under your organization's rules. You can limit what the agent sees
with `tables.include/exclude`, with definitions that only expose aggregated or anonymized
views, and with a database user that only has access to what the agent needs.

## Getting started

### 1. Install

```bash
git clone https://github.com/alexD1990/analytics-agent.git && cd analytics-agent
python3 -m venv .venv
source .venv/bin/activate          # Windows without WSL: .venv\Scripts\activate
pip install -e .
```

Check that it works:

```bash
analytics-agent --help
```

The virtual environment must be activated (`source .venv/bin/activate`) every time you open
a new terminal.

### 2. Try the example agent

`example/` contains a finished agent for a fictional online store that sells coffee and
coffee equipment. It works without changes.

```bash
analytics-agent check          --config example/agent/agent-example.yml
analytics-agent desktop-config --config example/agent/agent-example.yml
```

`example/data/` contains ready-made SQLite files, built once with
`build_example_data.py` (run it again to rebuild them; the result is identical). The data
contains deliberate data quality issues (spelling variants, credit notes, dates that don't
add up), so you can see how the agent handles them.

### 3. Create your own agent

Create the agent in its own folder, preferably outside the repository:

```bash
analytics-agent new ~/agents/sales --name sales-analyst
```

This creates `agent.yml`, `instructions.md` and `definitions/`. Fill in the connections
(see [Connecting databases](#connecting-databases)), the instructions and any definitions,
and run:

```bash
analytics-agent check          --config ~/agents/sales/agent.yml
analytics-agent desktop-config --config ~/agents/sales/agent.yml
```

Keeping the agent outside the repository means `git pull` never touches it. You can also
run `analytics-agent new` in the root of the repository: the agent files there are ignored
by git, so internal addresses and rules are not committed.

**Spend your time on `instructions.md` and `definitions/`.** The connections give the
agent access to the data; the instructions and definitions are what make its answers
correct. Explain what the business does, what the tables mean, which fields are
unreliable, and how key numbers such as revenue or active customers are defined.

### 4. Connect to Claude Desktop

1. Paste the entry from `desktop-config` under `"mcpServers"` in
   `claude_desktop_config.json`. The command prints where the file is.
2. Quit Claude Desktop completely (from the menu bar or the system tray, not just the
   window) and start it again.
3. Turn off the built-in code execution in Claude Desktop, so Python runs through the
   notebook and not in a cloud sandbox without access to your data.
4. Consider creating a project with this instruction: *"Start every conversation with new_analysis."*

Test it with a question about your data, for example *"Start with new_analysis. Which
products sold the most last month?"*

### Updating

```bash
cd analytics-agent
git pull
pip install -e .        # only needed if dependencies have changed, but harmless
```

Then restart Claude Desktop. Your agent folders are not affected.

## An agent is a folder

```
my-agent/
├── agent.yml            connections and settings
├── instructions.md      role, data sources, rules and known weaknesses
├── definitions/         one .sql file per business definition
├── .env                 passwords (optional)
├── logs/                created automatically
└── notebooks/           created automatically, one subfolder per analysis
```

All relative paths are resolved from the folder the yml file is in. Several agents: create
one folder per agent, and add one entry per agent in Claude Desktop with `desktop-config`.

### agent.yml

Only `connections` is required. Everything else has defaults.

| Field | Default | Content |
|---|---|---|
| `name` | `analytics-agent` | The name the server gets in Claude Desktop. |
| `agent.mode` | `interactive` | `interactive`: the user drives step by step. `autonomous`: the agent solves the whole question. Can always be overridden in the conversation. |
| `connections` | – | At least one. See below. |
| `limits.max_rows` | `200` | Rows `run_query` shows the agent. |
| `limits.max_rows_python` | `1000000` | Rows `sql()` in Python can fetch. |
| `limits.sql_timeout_s` | `30` | |
| `limits.python_timeout_s` | `60` | Default per cell. |
| `limits.python_max_timeout_s` | `300` | |
| `notebook.enabled` | `true` | |
| `notebook.dir` | `notebooks` | |
| `notebook.max_chars` / `max_figures` | `4000` / `4` | How much a cell sends back to the agent. |
| `log.dir` | `logs` | |
| `security.lock_duckdb` | `true` | Locks DuckDB out of files and the network after connecting. |

### instructions.md

Free text inserted into the agent's instructions as is. Use `##` for headings.
Recommended sections: **Role**, **Data sources**, **Rules** and **Known weaknesses**.
Without the file, the agent gets a general analyst role.

State things the agent cannot see in the data, such as the currency of amounts, whether
prices include VAT, and which fields are known to be wrong. See
`example/agent/instructions.md` for an example.

### definitions/

One file per definition. The file name becomes the name, and the comment at the top becomes the description:

```sql
-- Completed orders (status COMPLETED). The period is filtered in the query.
SELECT * FROM shop.orders WHERE status = 'COMPLETED'
```

`valid_orders.sql` becomes the view `def.valid_orders`, available both in `run_query`
and in `sql()` in Python. A definition can use other definitions, and the order is found
automatically. A file with only a comment is a text definition the agent must follow, without a view.
File names: lowercase letters a–z, digits and `_`.

## Connecting databases

Tables are referenced as `<connection>.<table>`, for example `crm.customers`.

**SQLite / DuckDB**
```yaml
connections:
  - name: crm
    type: sqlite            # or duckdb
    path: data/crm.db
    description: Customer register.     # optional
```

**MySQL**
```yaml
  - name: erp
    type: mysql
    host: db.internal.example
    port: 3306
    database: erp
    user: analytics_read            # dedicated user with SELECT only
    password: ${ERP_DB_PASSWORD}
    tables:
      exclude: [audit_log, "tmp_*"]
```

**PostgreSQL**
```yaml
  - name: dw
    type: postgres
    host: localhost
    port: 5432
    database: warehouse
    user: analytics_read
    password: ${DW_DB_PASSWORD}
    schema: public
    tables:
      include: ["fact_*", "dim_*"]
```

Passwords are written as `${VARIABLE}` and read from the environment or from `.env` in the agent folder:

```
ERP_DB_PASSWORD=secret
```

`tables.include/exclude` controls what the agent sees, not what it has access to.
MySQL and PostgreSQL require network access the first time, because DuckDB downloads the extension.

## The tools the agent gets

| Tool | What it does |
|---|---|
| `new_analysis` | Called first. Gives the agent the instructions and starts a new Python kernel and notebook. |
| `overview` | Tables with row counts, and the definitions. |
| `describe_table` | Columns, types and sample rows. |
| `run_query` | A single SELECT, answer as a table (row limit). |
| `run_python` | Python in a persistent kernel. `sql("SELECT ...")` returns a DataFrame. Figures are returned as images. |
| `show_variables` | What exists in the kernel. |
| `write_note` | Explanatory text in the notebook. |

Each analysis is saved in `notebooks/<timestamp>_<title>/analysis.ipynb`. All tool calls,
including `sql()` from Python, are logged to `logs/<name>.jsonl`.

## Security

- All connections are read-only, and only a single SELECT or WITH per query is allowed.
- DuckDB is locked after connecting: no reading of other files, no network calls from
  queries and no changes to settings.
- The Python kernel has no database connection and does not see the passwords. `sql()` sends
  the query to the server, which runs it with the same checks and logs it.
- Use a dedicated database user with SELECT only. `check` warns if the user can write.
- Limitation: the Python kernel runs as your user and can read files you have access to,
  including `.env`. Container isolation is planned for a later version.

See also [What leaves your machine](#what-leaves-your-machine).

## Troubleshooting

- **The server does not show up in Claude Desktop:** check that you quit Claude Desktop
  completely before starting it again. Then see `mcp-server-<name>.log` in Claude Desktop's
  log folder, and run `analytics-agent check --config <yml>`.
- **`analytics-agent: command not found`:** the virtual environment is not activated. Run
  `source .venv/bin/activate` (Windows without WSL: `.venv\Scripts\activate`).
- **`new_analysis` fails:** see `logs/<name>-kernel.log` in the agent folder.
- **MySQL/Postgres fails with "Could not load the DuckDB extension":** the extension is
  downloaded the first time and needs network access. On networks with strict firewalls or
  proxies, run `analytics-agent check` once from a network that allows it.
- **Queries against MySQL/Postgres fail with the lock on:** set `security.lock_duckdb: false`.
- **Re-running a notebook in Jupyter:** use the Python from `.venv`. Outside the agent,
  `sql()` connects directly to the databases using the same yml file.

## License

MIT. See [LICENSE](LICENSE).