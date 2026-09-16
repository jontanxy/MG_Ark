from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def make_engine(database_url: str) -> Engine:
    is_sqlite = database_url.startswith("sqlite")
    if is_sqlite:
        path = database_url.removeprefix("sqlite:///")
        if not path or path == ":memory:":
            raise ValueError("DATABASE_URL must point at a file (sqlite:///data/mg_archive.sqlite3); ':memory:' is not supported")
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        database_url,
        future=True,
        # Writes are tiny and every handler commits before awaiting; a short busy timeout keeps the event loop responsive.
        connect_args={"check_same_thread": False, "timeout": 5} if is_sqlite else {},
        pool_pre_ping=not is_sqlite,
    )
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - trivial
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


def init_db(database_url: str) -> Engine:
    """Create the engine, session factory and tables. Safe to call once at startup."""
    global _engine, _session_factory
    from . import models  # noqa: F401  (register tables)

    _engine = make_engine(database_url)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    Base.metadata.create_all(_engine)
    upgrade_schema(_engine)
    return _engine


def upgrade_schema(engine: Engine) -> list[str]:
    """Add columns that newer versions introduced to tables created by older versions (SQLite-safe, additive only).

    ``create_all`` only creates missing *tables*; this fills in missing nullable/defaulted *columns* so an
    existing database keeps working after an upgrade. Returns the ``table.column`` names that were added.
    """
    added: list[str] = []
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                if column.primary_key or (not column.nullable and column.default is None and column.server_default is None):
                    raise RuntimeError(f"Cannot add required column {table.name}.{column.name} automatically")
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(engine.dialect)}'
                conn.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
    return added


def get_session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        raise RuntimeError("init_db() has not been called")
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
