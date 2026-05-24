import json
import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import select

from config import settings
from bot.models.models import Base, Synonym

logger = logging.getLogger(__name__)

engine = create_async_engine(settings.DATABASE_URL, echo=False)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created")


async def migrate_db() -> None:
    """Idempotent: adds columns that may be missing in existing databases."""
    import sqlite3
    db_path = settings.DATABASE_URL.replace("sqlite+aiosqlite:///", "")
    conn = sqlite3.connect(db_path)
    migrations = [
        ("order_items", "stock_out", "BOOLEAN NOT NULL DEFAULT 0"),
    ]
    for table, col, defn in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
            conn.commit()
            logger.info("DB migration: added column %s.%s", table, col)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.close()


async def load_initial_synonyms() -> None:
    synonyms_path = Path(__file__).parent.parent.parent / "data" / "synonyms.json"
    if not synonyms_path.exists():
        logger.warning("synonyms.json not found, skipping initial load")
        return

    with open(synonyms_path, encoding="utf-8") as f:
        data: dict[str, str] = json.load(f)

    async with AsyncSessionLocal() as session:
        inserted = 0
        for raw_name, normalized_name in data.items():
            exists = await session.execute(
                select(Synonym).where(Synonym.raw_name == raw_name)
            )
            if not exists.scalar_one_or_none():
                session.add(Synonym(raw_name=raw_name, normalized_name=normalized_name))
                inserted += 1
        await session.commit()

    logger.info("Loaded %d new synonyms from synonyms.json", inserted)
