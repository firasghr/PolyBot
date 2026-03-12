import asyncio
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import Base

# Using an absolute path for safety, or relative to the execution directory
DATABASE_URL = "sqlite+aiosqlite:///polybot_v3.db"

# Create the async SQLite engine
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
)

# Create an async session maker
async_session_maker = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)

async def init_db() -> None:
    """Creates all tables defined in models if they don't exist."""
    async with engine.begin() as conn:
        # For production use Alembic; for this research bot, create_all is fine
        await conn.run_sync(Base.metadata.create_all)

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for providing a database session."""
    async with async_session_maker() as session:
        yield session
