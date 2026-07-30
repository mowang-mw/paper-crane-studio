from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.database import Database
from backend.app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Give every test an isolated SQLite database and project media root."""

    isolated = Settings.for_data_dir(tmp_path / "data")
    isolated.ensure_directories()
    return isolated


@pytest.fixture
def database(settings: Settings) -> Iterator[Database]:
    instance = Database(str(settings.database_url))
    instance.create_schema()
    try:
        yield instance
    finally:
        instance.dispose()


@pytest.fixture
def app(settings: Settings, database: Database) -> FastAPI:
    return create_app(settings, database=database)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
