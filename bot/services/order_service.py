import asyncio
import re
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete
from sqlalchemy.orm import selectinload

from config import settings
from bot.models.models import Order, OrderItem, UnknownItem
from bot.services.normalizer import normalizer
from bot.services.stock_service import stock_checker

logger = logging.getLogger(__name__)

_order_create_lock = asyncio.Lock()

REORDER_LOOKBACK_WEEKS = 4
ACTIVE_CHAT_LOOKBACK_DAYS = 30

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
_QTY_TOKEN_RE = re.compile(r"^\d+[.,]?\d*$")
_HYPHEN_BEFORE_DIGIT_RE = re.compile(r"(\S)-(\d)")
_DIGIT_BEFORE_LETTER_RE = re.compile(r"(\d)([^\d\s.,])")

# Units recognized as a single trailing token when disambiguating two items
# smashed onto one line (e.g. "Маковая начинка 1  маргарин-2 ящ"). Only
# consulted when another quantity follows later on the same line — the last
# (or only) quantity on a line always takes everything after it as its unit,
# whether or not that text is a "known" unit word.
_UNIT_WORDS = {
    "кг", "г", "гр", "л", "мл", "шт", "в",
    "уп", "упак", "упаковка",
    "вед", "ведер", "ведро",
    "мешок", "мешка", "мешков",
    "кор", "коробка", "коробок",
    "ящ", "ящик", "ящиков",
    "кон", "канистра", "канистр",
    "литр", "литров",
    "бут", "бутылка",
    "пач", "пачка", "пачек",
}


def _clean_client_name(text: str) -> str:
    text = _EMOJI_RE.sub("", text)
    return re.sub(r"[\s:]+$", "", text).strip()


def _tokenize_item_line(line: str) -> list[str]:
    # Treat a hyphen or a bare digit/letter boundary as a token separator too,
    # so "дрожжи-3 ящ" and "лавка 1в" split the same way as "дрожжи 3 ящ".
    line = _HYPHEN_BEFORE_DIGIT_RE.sub(r"\1 \2", line)
    line = _DIGIT_BEFORE_LETTER_RE.sub(r"\1 \2", line)
    return line.split()


def _split_line_items(line: str) -> list[dict]:
    """Split one order line into one or more items.

    Usually a line is a single "name quantity unit" item, but managers
    sometimes type two items on one line with no separator (no comma, just
    extra spaces) — e.g. "Маковая начинка 1  маргарин-2 ящ" is two items.
    """
    tokens = _tokenize_item_line(line)
    qty_indices = [i for i, t in enumerate(tokens) if _QTY_TOKEN_RE.match(t)]
    if not qty_indices:
        return []

    items = []
    name_start = 0
    for pos, qty_idx in enumerate(qty_indices):
        name_tokens = tokens[name_start:qty_idx]
        if not name_tokens:
            continue

        next_qty_idx = qty_indices[pos + 1] if pos + 1 < len(qty_indices) else None
        if next_qty_idx is not None:
            between = tokens[qty_idx + 1:next_qty_idx]
            if between and between[0].lower() in _UNIT_WORDS:
                unit = between[0]
                name_start = qty_idx + 2
            else:
                unit = ""
                name_start = qty_idx + 1
        else:
            unit = " ".join(tokens[qty_idx + 1:])
            name_start = len(tokens)

        items.append({
            "raw_name": " ".join(name_tokens),
            "quantity": tokens[qty_idx].replace(",", "."),
            "unit": unit,
        })
    return items


def _line_has_quantity(line: str) -> bool:
    return any(_QTY_TOKEN_RE.match(t) for t in _tokenize_item_line(line))


def parse_order_text(text: str) -> Optional[dict]:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        return None

    if not lines[0].startswith("\U0001F534"):
        return None

    header_parts = []
    item_lines = []
    items_started = False

    for idx, line in enumerate(lines[1:]):
        # The very first line after the flags is always the client/branch
        # header, even if it happens to end in a digit (e.g. "Хлебозавод 3")
        # and would otherwise look like an item line.
        is_header_line = not items_started and (idx == 0 or not _line_has_quantity(line))
        if is_header_line:
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
        items.extend(_split_line_items(line))

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
    if settings.STOCK_API_TOKEN:
        try:
            await stock_checker.ensure_fresh(
                settings.STOCK_API_BASE_URL,
                settings.stock_bases_list,
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
    async with _order_create_lock:
        order_number = await _next_order_number(session)
        order = Order(
            order_number=order_number,
            source_text=source_text,
            client_name=client_name,
            message_id=message_id,
            chat_id=chat_id,
        )
        session.add(order)
        # Commit inside the lock so the row is visible to the next caller's
        # SELECT MAX — PostgreSQL READ COMMITTED won't see an uncommitted flush.
        await session.commit()

    item_objs, unknown_raw_names, stock_out_names = await _build_items(order.id, items, client_name, session)
    for obj in item_objs:
        session.add(obj)

    await session.commit()
    return order, item_objs, unknown_raw_names, stock_out_names


async def update_order_items(
    session: AsyncSession,
    order: Order,
    items: list[dict],
    source_text: str,
) -> tuple[list[OrderItem], list[str], list[str]]:
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
        .options(selectinload(Order.items))
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


async def get_unknown_items(session: AsyncSession) -> list[UnknownItem]:
    result = await session.execute(
        select(UnknownItem)
        .where(UnknownItem.resolved == False)
        .options(selectinload(UnknownItem.order))
    )
    return list(result.scalars().all())


async def compute_reorder_report(
    session: AsyncSession, weeks: int = REORDER_LOOKBACK_WEEKS
) -> list[dict]:
    """Compare recent per-item order volume against current 1C stock.

    Flags an item when its current stock is already below the average
    weekly volume ordered over the lookback window (i.e. it would run out
    within a week at the recent pace).
    """
    cutoff = datetime.utcnow() - timedelta(weeks=weeks)
    result = await session.execute(
        select(OrderItem.normalized_name, OrderItem.unit, OrderItem.quantity, Order.created_at)
        .join(Order, OrderItem.order_id == Order.id)
        .where(
            Order.created_at >= cutoff,
            OrderItem.is_unknown == False,
            OrderItem.normalized_name.isnot(None),
        )
    )
    rows = result.all()
    if not rows:
        return []

    totals: dict[tuple[str, str], float] = defaultdict(float)
    earliest = datetime.utcnow()
    for normalized_name, unit, quantity, created_at in rows:
        try:
            qty = float(quantity.replace(",", "."))
        except (ValueError, AttributeError):
            continue
        totals[(normalized_name, unit or "")] += qty
        earliest = min(earliest, created_at)

    span_weeks = max((datetime.utcnow() - earliest).days / 7, 1.0)

    reorder = []
    for (name, unit), total_qty in totals.items():
        weekly_avg = total_qty / span_weeks
        if weekly_avg <= 0:
            continue
        _, current_qty = stock_checker.check(name)
        if current_qty is None:
            continue
        if current_qty < weekly_avg:
            reorder.append({
                "name": name,
                "unit": unit,
                "current_qty": current_qty,
                "weekly_avg": weekly_avg,
            })

    reorder.sort(key=lambda r: r["current_qty"] / r["weekly_avg"])
    return reorder


async def get_active_chat_ids(session: AsyncSession, days: int = ACTIVE_CHAT_LOOKBACK_DAYS) -> list[int]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    result = await session.execute(
        select(Order.chat_id)
        .where(Order.chat_id.isnot(None), Order.created_at >= cutoff)
        .distinct()
    )
    return [row[0] for row in result.all()]
