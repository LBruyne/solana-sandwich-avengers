import os
from pathlib import Path

import clickhouse_connect
from dotenv import load_dotenv


def get_client():
    """Return a ClickHouse client using .env credentials with fallback chain."""
    for env_path in [
        Path(__file__).resolve().parent.parent / ".env",
        Path(__file__).resolve().parent.parent.parent / "analyst" / ".env",
    ]:
        if env_path.exists():
            load_dotenv(env_path)
            break

    host = os.getenv("CLICKHOUSE_HOST", "localhost")
    port = int(os.getenv("CLICKHOUSE_PORT", "8123"))
    database = os.getenv("CLICKHOUSE_DATABASE", "solwich")
    username = os.getenv("CLICKHOUSE_USERNAME", "default")
    password = os.getenv("CLICKHOUSE_PASSWORD", "sol")

    return clickhouse_connect.get_client(
        host=host,
        port=port,
        database=database,
        username=username,
        password=password,
    )
