from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from tax_risk.config import Settings


BACKEND_ROOT = Path(__file__).resolve().parents[2]
PYTEST_SCHEMA_PATTERN = re.compile(r"tax_risk_pytest_[0-9a-f]{32}")
PYTEST_SCHEMA_MARKER = "tax_risk_pytest_owned_v1"


def _validate_pytest_schema_name(schema_name: str) -> None:
    if PYTEST_SCHEMA_PATTERN.fullmatch(schema_name) is None:
        raise RuntimeError(f"refusing unsafe pytest schema name: {schema_name!r}")


def _create_owned_schema(base_url: URL, schema_name: str) -> None:
    _validate_pytest_schema_name(schema_name)
    admin_engine = create_engine(base_url, poolclass=NullPool)
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema_name))
            quoted_schema = connection.dialect.identifier_preparer.quote(schema_name)
            connection.exec_driver_sql(
                f"COMMENT ON SCHEMA {quoted_schema} IS '{PYTEST_SCHEMA_MARKER}'"
            )
    finally:
        admin_engine.dispose()


def _isolated_schema_url(base_url: URL, schema_name: str) -> str:
    _validate_pytest_schema_name(schema_name)
    existing_options = base_url.query.get("options")
    if isinstance(existing_options, tuple):
        existing_options = " ".join(existing_options)

    search_path_option = f"-csearch_path={schema_name}"
    options = (
        f"{existing_options} {search_path_option}"
        if existing_options
        else search_path_option
    )
    isolated_url = base_url.update_query_dict({"options": options})
    return isolated_url.render_as_string(hide_password=False)


def _upgrade_isolated_schema(database_url: str) -> None:
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
            "isolated-schema migration failed\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


def _drop_owned_schema(base_url: URL, schema_name: str) -> None:
    _validate_pytest_schema_name(schema_name)
    admin_engine = create_engine(base_url, poolclass=NullPool)
    try:
        with admin_engine.begin() as connection:
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
            if marker != PYTEST_SCHEMA_MARKER:
                raise RuntimeError(
                    f"refusing to drop unowned pytest schema {schema_name!r}: "
                    f"marker was {marker!r}"
                )
            connection.execute(DropSchema(schema_name, cascade=True))
    finally:
        admin_engine.dispose()


@pytest.fixture(scope="session")
def isolated_database_url() -> Iterator[str]:
    base_url = make_url(Settings().database_url)
    schema_name = f"tax_risk_pytest_{uuid4().hex}"
    _create_owned_schema(base_url, schema_name)
    database_url = _isolated_schema_url(base_url, schema_name)
    try:
        _upgrade_isolated_schema(database_url)
        yield database_url
    finally:
        _drop_owned_schema(base_url, schema_name)


@pytest.fixture(scope="session")
def engine(isolated_database_url: str) -> Iterator[Engine]:
    database_engine = create_engine(isolated_database_url)
    try:
        yield database_engine
    finally:
        database_engine.dispose()
