"""One-time script to initialize the database and load initial synonyms."""
import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

from bot.db.database import init_db, load_initial_synonyms


async def main() -> None:
    await init_db()
    await load_initial_synonyms()
    print("✅ Database initialized successfully.")


if __name__ == "__main__":
    asyncio.run(main())
