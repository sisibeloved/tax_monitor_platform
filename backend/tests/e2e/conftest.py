from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from tax_risk.config import Settings


BACKEND_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATTERN = re.compile(r"tax_risk_e2e_[0-9a-f]{32}")
SCHEMA_MARKER = "tax_risk_e2e_owned_v1"


def _validate_schema_name(schema_name: str) -> None:
    if SCHEMA_PATTERN.fullmatch(schema_name) is None:
        raise RuntimeError(f"refusing unsafe E2E schema name: {schema_name!r}")


def _isolated_url(base_url: URL, schema_name: str) -> str:
    _validate_schema_name(schema_name)
    existing_options = base_url.query.get("options")
    if isinstance(existing_options, tuple):
        existing_options = " ".join(existing_options)
    search_path = f"-csearch_path={schema_name}"
    options = f"{existing_options} {search_path}" if existing_options else search_path
    return base_url.update_query_dict({"options": options}).render_as_string(
        hide_password=False
    )


def _upgrade(database_url: str) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(BACKEND_ROOT / "alembic.ini"),
            "upgrade",
            "head",
        ],
        cwd=BACKEND_ROOT,
        env=os.environ | {"DATABASE_URL": database_url},
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "E2E isolated-schema migration failed\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


@pytest.fixture
def e2e_database_url() -> Iterator[str | None]:
    """Create a disposable schema locally; HTTP-against-deployment mode needs none."""

    if os.getenv("E2E_BASE_URL"):
        yield None
        return

    base_url = make_url(Settings().database_url)
    schema_name = f"tax_risk_e2e_{uuid4().hex}"
    _validate_schema_name(schema_name)
    admin_engine = create_engine(base_url, poolclass=NullPool)
    with admin_engine.begin() as connection:
        connection.execute(CreateSchema(schema_name))
        quoted = connection.dialect.identifier_preparer.quote(schema_name)
        connection.exec_driver_sql(
            f"COMMENT ON SCHEMA {quoted} IS '{SCHEMA_MARKER}'"
        )
    admin_engine.dispose()

    database_url = _isolated_url(base_url, schema_name)
    try:
        _upgrade(database_url)
        yield database_url
    finally:
        cleanup_engine = create_engine(base_url, poolclass=NullPool)
        try:
            with cleanup_engine.begin() as connection:
                marker = connection.execute(
                    text(
                        """
                        SELECT obj_description(namespace.oid, 'pg_namespace')
                        FROM pg_namespace AS namespace
                        WHERE namespace.nspname = :schema_name
                        """
                    ),
                    {"schema_name": schema_name},
                ).scalar_one_or_none()
                if marker != SCHEMA_MARKER:
                    raise RuntimeError(
                        f"refusing to drop unowned E2E schema {schema_name!r}: "
                        f"marker was {marker!r}"
                    )
                connection.execute(DropSchema(schema_name, cascade=True))
        finally:
            cleanup_engine.dispose()
