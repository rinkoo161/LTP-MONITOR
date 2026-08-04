"""Engine/session factory. One engine per process."""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from marketsense.core.config import settings


@lru_cache(maxsize=1)
def get_engine(url: str | None = None):
    return create_engine(url or settings().db_url, pool_pre_ping=True, pool_size=5)


@lru_cache(maxsize=1)
def get_sessionmaker(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)


def session() -> Session:
    return get_sessionmaker()()
