"""sql() for the Python kernel.

When the agent runs the code, the query is sent to the analytics-agent server
(gateway.py), which runs it with the same checks as run_query. The kernel
itself has no database connection and no passwords.

If a human later opens the notebook in Jupyter, without the analytics-agent
server, sql() instead connects directly to the databases using agent.yml
(found via ANALYTICS_AGENT_CONFIG or by searching upwards from the working
directory). The user's own privileges are then used.
"""
from __future__ import annotations

import os
import pickle
import urllib.error
import urllib.request
from functools import lru_cache

import pandas as pd

__all__ = ["sql", "SqlError"]

# No proxy: the request must always go straight to 127.0.0.1.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class SqlError(Exception):
    """The query was rejected or failed. The message explains why."""


def sql(query: str) -> pd.DataFrame:
    """Runs a single SELECT against the connections in agent.yml and returns a DataFrame.

    Tables are referenced as connection.table, for example crm.customers, and
    definitions as def.<name>. The query can join across connections.
    """
    url = os.environ.get("ANALYTICS_AGENT_GATEWAY")
    if not url:
        return _local_engine().df(query)

    request = urllib.request.Request(
        url,
        data=query.encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {os.environ.get('ANALYTICS_AGENT_TOKEN', '')}",
                 "Content-Type": "text/plain; charset=utf-8"},
    )
    try:
        with _opener.open(request) as response:
            return pickle.loads(response.read())
    except urllib.error.HTTPError as e:
        message = e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        message = (f"Could not reach the analytics-agent server ({e.reason}). "
                   "Start the analysis again with new_analysis.")
    raise SqlError(message)


@lru_cache(maxsize=1)
def _local_engine():
    from .config import load_config
    from .engine import Engine
    return Engine(load_config())