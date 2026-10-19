from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models import Base

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One unit of work: commit on success, roll back on any exception."""
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all never alters existing tables: upgrade databases created before statement import existed
        for ddl in (
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS external_key VARCHAR(40)",
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS import_id INTEGER",
            "CREATE INDEX IF NOT EXISTS ix_transactions_external_key ON transactions (external_key)",
            "CREATE INDEX IF NOT EXISTS ix_transactions_import_id ON transactions (import_id)",
        ):
            await conn.execute(text(ddl))
