from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


def _normalise_db_url(url: str) -> str:
    """Force the psycopg v3 driver since we install `psycopg[binary]` (v3) in requirements.txt.

    SQLAlchemy's default for a bare `postgresql://` is psycopg v2, which we don't install,
    so the import fails on startup. Rewrite to `postgresql+psycopg://` to be explicit.
    Also handles the legacy `postgres://` scheme some hosts (Heroku etc.) emit.
    """
    if url.startswith("postgresql+"):
        return url  # already explicit (e.g. postgresql+psycopg, postgresql+asyncpg)
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


DB_URL = _normalise_db_url(settings.database_url)
engine = create_engine(DB_URL, pool_pre_ping=True, pool_size=5, max_overflow=10)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
