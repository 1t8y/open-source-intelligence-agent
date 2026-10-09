"""Database engine and session factory helpers."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_database_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create a pooled SQLAlchemy engine with connection health checks."""
    return create_engine(database_url, echo=echo, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create sessions that avoid implicit flushes and expired objects."""
    return sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
