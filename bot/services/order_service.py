import re
import logging
from datetime import datetime, date
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, cast, Date
from sqlalchemy.orm import selectinload

from config import settings
from bot.models.models import Order, OrderItem, StatusHistory, OrderStatus, UnknownItem
from bot.services.normalizer import normalizer
from bot.services.stock_service import stock_checker

logger = logging.getLogger(__name__)

_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "\U00010000-\U0010FFFF"
    "]+",
    flags=re.UNICODE,
)
_ITEM_RE = re.compile(r"^(.+?)\s+([\d.,]+)\s*(.*)$")


def _clean_client_name(text: str) -> str:
    text = _EMOJI_RE.sub("", text)
    return re.sub(r"[\s:]+$", "", text).strip()


def parse_order_text(text: str) -> Optional[dict]:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        return None

    if not lines[0].startswith("\U0001F534"):
        return None

    header_parts = []
    item_lines = []
    items_started = False

    for line in lines[1:]:
        if not items_started and not _ITEM_RE.match(line):
            cleaned = _clean_client_name(line)
            if cleaned:
                header_parts.append(cleaned)
        else:
            items_started = True
            item_lines.append(line)

    client_name = " / ".join(header_parts)
    if not client_name:
        return None

    items: list[dict] = []
    for line in item_lines:
        m = _ITEM_RE.match(line)
        if m:
            items.append({
                "raw_name": m.group(1).strip(),
                "quantity": m.group(2).replace(",", "."),
                "unit": m.group(3).strip(),
            })

    if not items:
        return None

    return {"client_name": client_name, "items": items}


async def _next_order_number(session: AsyncSession) -> int:
    result = await session.execute(select(func.max(Order.order_number)))
    current = result.scalar_one_or_none()
    return (current or 0) + 1


async def _build_items(
    order_id: int,
    items: list[dict],
    client_name: str,
    session: AsyncSession,
) -> tuple[list[OrderItem], list[str], list[str]]:
    # Refresh stock cache once per call (no-op if still fresh)
    if settings.STOCK_API_TOKEN:
        try:
            await stock_checker.ensure_fresh(
                settings.STOCK_API_URL_IPSH,
                settings.STOCK_API_URL_IPD,
                settings.STOCK_API_TOKEN,
            )
        except Exception as exc:
            logger.warning("[STOCK] ensure_fresh failed: %s", exc)

    result = []
    unknown_raw_names = []
    stock_out_names = []

    for item in items:
        norm_name, is_unknown = await normalizer.normalize(
            item["raw_name"], client_name, session, order_id=order_id
        )
        if is_unknown:
            unknown_raw_names.append(item["raw_name"])

        stock_out = False
        if norm_name and not is_unknown and settings.STOCK_API_TOKEN:
            in_stock, _ = stock_checker.check(norm_name)
            stock_out = not in_stock
            if stock_out:
                stock_out_names.append(norm_name)

        result.append(OrderItem(
            order_id=order_id,
            raw_name=item["raw_name"],
            normalized_name=norm_name,
            quantity=item["quantity"],
            unit=item["unit"],
            is_unknown=is_unknown,
            stock_out=stock_out,
        ))
    return result, unknown_raw_names, stock_out_names


async def create_order(
    session: AsyncSession,
    source_text: str,
    client_name: str,
    items: list[dict],
    message_id: int,
    chat_id: int,
) -> tuple[Order, list[OrderItem], list[str], list[str]]:
    order_number = await _next_order_number(session)
    order = Order(
        order_number=order_number,
        source_text=source_text,
        client_name=client_name,
        status=OrderStatus.QUEUED,
        message_id=message_id,
        chat_id=chat_id,
    )
    session.add(order)
    await session.flush()

    item_objs, unknown_raw_names, stock_out_names = await _build_items(order.id, items, client_name, session)
    for obj in item_objs:
        session.add(obj)

    session.add(StatusHistory(
        order_id=order.id,
        old_status=None,
        new_status=OrderStatus.QUEUED,
        changed_by=None,
    ))
    await session.commit()
    return order, item_objs, unknown_raw_names, stock_out_names


async def update_order_items(
    session: AsyncSession,
    order: Order,
    items: list[dict],
    source_text: str,
) -> tuple[list[OrderItem], list[str], list[str]]:
    # Remove old unknown_items for this order
    await session.execute(delete(UnknownItem).where(UnknownItem.order_id == order.id))
    await session.execute(delete(OrderItem).where(OrderItem.order_id == order.id))
    await session.flush()

    item_objs, unknown_raw_names, stock_out_names = await _build_items(order.id, items, order.client_name, session)
    for obj in item_objs:
        session.add(obj)

    order.source_text = source_text
    order.updated_at = datetime.utcnow()
    await session.commit()
    return item_objs, unknown_raw_names, stock_out_names


async def update_order_status(
    session: AsyncSession,
    order: Order,
    new_status: OrderStatus,
    changed_by: int,
) -> None:
    old_status = order.status
    order.status = new_status
    order.updated_at = datetime.utcnow()
    session.add(StatusHistory(
        order_id=order.id,
        old_status=old_status,
        new_status=new_status,
        changed_by=changed_by,
    ))
    await session.commit()


async def set_bot_message_id(session: AsyncSession, order_id: int, bot_message_id: int) -> None:
    result = await session.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if order:
        order.bot_message_id = bot_message_id
        await session.commit()


async def get_order_by_message(
    session: AsyncSession, message_id: int, chat_id: int
) -> Optional[Order]:
    result = await session.execute(
        select(Order)
        .where(Order.message_id == message_id, Order.chat_id == chat_id)
        .options(selectinload(Order.items))
    )
    return result.scalar_one_or_none()


async def get_order_by_number(session: AsyncSession, order_number: int) -> Optional[Order]:
    result = await session.execute(
        select(Order)
        .where(Order.order_number == order_number)
        .options(selectinload(Order.items), selectinload(Order.history))
    )
    return result.scalar_one_or_none()


async def get_recent_orders(session: AsyncSession, limit: int = 10) -> list[Order]:
    result = await session.execute(
        select(Order)
        .order_by(Order.created_at.desc())
        .limit(limit)
        .options(selectinload(Order.items))
    )
    return list(result.scalars().all())


async def get_status_counts(session: AsyncSession) -> dict[OrderStatus, int]:
    result = await session.execute(
        select(Order.status, func.count(Order.id).label("cnt"))
        .group_by(Order.status)
    )
    counts = {s: 0 for s in OrderStatus}
    for row in result.all():
        counts[row.status] = row.cnt
    return counts


async def get_orders_by_status(
    session: AsyncSession,
    status: OrderStatus,
    date_filter: Optional[date] = None,
) -> list[Order]:
    q = (
        select(Order)
        .where(Order.status == status)
        .order_by(Order.created_at.asc())
        .options(selectinload(Order.items))
    )
    if date_filter is not None:
        q = q.where(cast(Order.updated_at, Date) == date_filter)
    result = await session.execute(q)
    return list(result.scalars().all())


async def get_unknown_items(session: AsyncSession) -> list[UnknownItem]:
    result = await session.execute(
        select(UnknownItem)
        .where(UnknownItem.resolved == False)
        .options(selectinload(UnknownItem.order))
    )
    return list(result.scalars().all())
