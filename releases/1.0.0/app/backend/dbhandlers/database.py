"""
dbhandlers/database.py
----------------------
Builds the SQLAlchemy engine from DATABASE_URL.

- Production: Supabase Postgres. Use the "postgresql+psycopg://" form.
  Supabase's transaction pooler (port 6543) does not support prepared
  statements, so they are switched off below.
- Development / tests: SQLite file under shared/data/.

The app connects with a server-side role, which bypasses Supabase row
level security. Tenant isolation is therefore enforced in the app on
every query (see services/business/access.py) and checked by the
preflight tests.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app_platform.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def utcnow() -> datetime:
    """All stored datetimes are naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_engine(url: str) -> Engine:
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        return engine
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={"prepare_threshold": None},
    )


def init_engine(url: str | None = None) -> Engine:
    global _engine, _SessionLocal
    url = url or get_settings().database_url
    _engine = _make_engine(url)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    return _engine  # type: ignore[return-value]


def create_all() -> None:
    from dbhandlers import models  # noqa: F401  (importing registers the tables)
    _ = models

    Base.metadata.create_all(get_engine())


def session_factory() -> sessionmaker:
    if _SessionLocal is None:
        init_engine()
    return _SessionLocal  # type: ignore[return-value]


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, committed by the handler."""
    db = session_factory()()
    try:
        yield db
    finally:
        db.close()


def is_postgres() -> bool:
    return get_engine().dialect.name == "postgresql"
