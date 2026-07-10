"""Database connection configuration with mounted-secret support."""

from __future__ import annotations

import os
from urllib.parse import quote

from memory_plane.config.secrets import read_secret_env


def read_database_dsn(
    direct_name: str = "UAM_DATABASE_URL",
    *,
    component_prefix: str = "UAM_DATABASE",
) -> str | None:
    """Read a complete DSN or assemble one from explicit connection components.

    A complete ``*_URL`` (or ``*_URL_FILE``) remains supported. Component mode
    exists so passwords can be mounted as files without interpolating their
    contents into Docker Compose environment values.
    """
    direct = read_secret_env(direct_name)
    if direct:
        return direct

    names = {
        "user": f"{component_prefix}_USER",
        "password": f"{component_prefix}_PASSWORD",
        "host": f"{component_prefix}_HOST",
        "port": f"{component_prefix}_PORT",
        "database": f"{component_prefix}_NAME",
    }
    values = {
        "user": os.getenv(names["user"]),
        "password": read_secret_env(names["password"]),
        "host": os.getenv(names["host"]),
        "port": os.getenv(names["port"], "5432"),
        "database": os.getenv(names["database"]),
    }
    configured = any(
        os.getenv(name) or os.getenv(f"{name}_FILE")
        for name in names.values()
        if name != names["port"]
    )
    if not configured:
        return None

    missing = [key for key in ("user", "password", "host", "database") if not values[key]]
    if missing:
        raise ValueError(
            f"incomplete {component_prefix} database configuration; missing: "
            + ", ".join(missing)
        )
    try:
        port = int(values["port"] or "5432")
    except ValueError as exc:
        raise ValueError(f"{names['port']} must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{names['port']} must be between 1 and 65535")

    host = str(values["host"])
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    user = quote(str(values["user"]), safe="")
    password = quote(str(values["password"]), safe="")
    database = quote(str(values["database"]), safe="")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"
