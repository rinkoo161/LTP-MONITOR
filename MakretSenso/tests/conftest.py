"""Shared fixtures. DB tests run against MS_DB_URL_TEST (marketsense_test),
schema created fresh per test session, truncated per test."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from marketsense.core.config import settings
from marketsense.db.models import Base


@pytest.fixture(scope="session")
def test_engine():
    engine = create_engine(settings().db_url_test)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_factory(test_engine):
    """Session factory over a clean database (all tables truncated)."""
    with test_engine.connect() as conn:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        conn.commit()
    return sessionmaker(bind=test_engine, expire_on_commit=False)
