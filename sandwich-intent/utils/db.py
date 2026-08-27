import os
from pathlib import Path

import clickhouse_connect
from dotenv import load_dotenv


ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env():
    """Load `sandwich-intent/.env` into the process environment, once per process.

    Every credential this package needs lives in that one file, so scripts that
    need something other than ClickHouse (an API key, say) call this rather than
    growing a second config path.
    """
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)


def require_env(name, hint=""):
    """Return `name` from the environment, or fail with an instruction, never a default.

    A missing credential must stop the run: silently falling back would turn an
    unset key into an empty result set that reads like a finding.
    """
    load_env()
    v = os.getenv(name)
    if not v:
        raise SystemExit(
            f"{name} is not set. Add it to {ENV_PATH} (see .env.example)"
            + (f" -- {hint}" if hint else "") + ".")
    return v


def get_client(database=None):
    """Return a ClickHouse client using .env credentials with fallback chain.

    `database` overrides CLICKHOUSE_DATABASE, so a script can target a specific
    database without editing the shared .env.
    """
    load_env()

    host = os.getenv("CLICKHOUSE_HOST", "localhost")
    port = int(os.getenv("CLICKHOUSE_PORT", "8123"))
    database = database or os.getenv("CLICKHOUSE_DATABASE", "solwich")
    username = os.getenv("CLICKHOUSE_USERNAME", "default")
    password = os.getenv("CLICKHOUSE_PASSWORD", "")

    return clickhouse_connect.get_client(
        host=host,
        port=port,
        database=database,
        username=username,
        password=password,
    )
