"""Test harness.

Every test runs with a temporary database, the injected clock reset, and the
deterministic rules classifier, so results never depend on network or wall time.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def environment(tmp_path_factory):
    db_file = tmp_path_factory.mktemp("data") / "test.db"
    os.environ["APP_DB"] = str(db_file)

    from app import db
    from app.core import clock, config

    db.init_db()
    config.clear_overrides()
    config.set_override("triage.mode", "rules")
    config.set_override("policy.mode", "extractive")
    clock.reset()
    yield
    clock.reset()
    config.clear_overrides()


@pytest.fixture(autouse=True)
def restore_config():
    """Each test may change configuration; the next test must not inherit it."""
    from app.core import config

    before = dict(config.snapshot()["overrides"])
    yield
    config.clear_overrides()
    for key, value in before.items():
        config.set_override(key, value)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
