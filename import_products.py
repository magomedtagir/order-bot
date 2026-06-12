"""
One-time script: import products from Excel into the `products` table.
Usage: python import_products.py [path_to_excel]
Default file: order_bot_template_v2_clean.xlsx
"""
import asyncio
import sys
from pathlib import Path

import openpyxl
from dotenv import load_dotenv

load_dotenv()

from config import settings
from bot.db.database import engine
from bot.models.models import Base, Product
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def main(xlsx_path: str) -> None:
    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb["Товары"]

    names: list[str] = []
    for row in ws.iter_rows(min_row=3, values_only=True):
        full_name = row[1]
        if full_name and str(full_name).strip():
            names.append(str(full_name).strip())

    print(f"Найдено товаров в Excel: {len(names)}")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        existing = (await session.execute(select(Product))).scalars().all()
        existing_names = {p.full_name for p in existing}

        added = 0
        for name in names:
            if name not in existing_names:
                session.add(Product(full_name=name))
                added += 1

        await session.commit()

    print(f"Добавлено новых товаров: {added}")
    print(f"Уже было в БД: {len(existing_names)}")
    await engine.dispose()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "order_bot_template_v2_clean.xlsx"
    if not Path(path).exists():
        print(f"Файл не найден: {path}")
        sys.exit(1)
    asyncio.run(main(path))
