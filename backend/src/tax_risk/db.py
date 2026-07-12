from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from tax_risk.config import Settings


class Base(DeclarativeBase):
    """Base class for all persisted entities."""


settings = Settings()
engine: Engine = create_engine(settings.database_url, pool_pre_ping=True)
session_factory: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)
