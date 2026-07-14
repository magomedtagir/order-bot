import html
import logging
from typing import Optional
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy.ext.asyncio import async_sessionmaker

from config import settings
from bot.models.models import OrderItem
from bot.keyboards import build_order_text
from bot.services.order_service import (
    parse_order_text,
    create_order,
    get_order_by_message,
    update_order_items,
    set_bot_message_id,
    compute_reorder_report,
    get_active_chat_ids,
)

logger = logging.getLogger(__name__)


def _session_factory(context: ContextTypes.DEFAULT_TYPE) -> async_sessionmaker:
    return context.bot_data["session_factory"]


async def _send_stock_out_notice(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    order_number: int,
    client_name: str,
    stock_out_names: list[str],
) -> None:
    if not stock_out_names:
        return
    try:
        names_list = "\n".join(f"  • {n}" for n in stock_out_names)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f'🚫 Нет в наличии (заказ #{order_number:03d}, {client_name}):\n'
                f'{names_list}'
            )
        )
    except Exception as exc:
        logger.warning("Failed to notify admin about stock: %s", exc)


def _format_reorder_report(items: list[dict]) -> str:
    if not items:
        return "✅ Все позиции в достатке — на ближайшую неделю дозаказ не требуется."
    lines = ["📦 <b>Пора дозаказать</b> (остаток меньше расхода за неделю):\n"]
    for r in items:
        unit = f" {html.escape(r['unit'])}" if r["unit"] else ""
        lines.append(
            f"  • {html.escape(r['name'])} — остаток {r['current_qty']:.1f}{unit}, "
            f"расход/нед ~{r['weekly_avg']:.1f}{unit}"
        )
    return "\n".join(lines)


async def send_reorder_report(
    context: ContextTypes.DEFAULT_TYPE, chat_ids: Optional[list[int]] = None
) -> None:
    """Build the reorder report and send it to the given chats (or all recently active chats)."""
    if settings.STOCK_API_TOKEN:
        from bot.services.stock_service import stock_checker
        try:
            await stock_checker.refresh(
                settings.STOCK_API_BASE_URL, settings.stock_bases_list, settings.STOCK_API_TOKEN,
            )
        except Exception as exc:
            logger.warning("[REORDER] Stock refresh failed: %s", exc)

    async with _session_factory(context)() as session:
        report = await compute_reorder_report(session)
        targets = chat_ids if chat_ids is not None else await get_active_chat_ids(session)

    text = _format_reorder_report(report)
    for chat_id in targets:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception as exc:
            logger.warning("[REORDER] Failed to send report to %s: %s", chat_id, exc)


async def weekly_reorder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not settings.STOCK_API_TOKEN:
        logger.info("[REORDER] Skipped weekly job — STOCK_API_TOKEN not set")
        return
    logger.info("[REORDER] Running weekly reorder report job")
    await send_reorder_report(context)


def _format_items(items: list[OrderItem]) -> str:
    lines = []
    for item in items:
        name = html.escape(item.normalized_name or item.raw_name)
        unit = html.escape(item.unit or "")
        line = f"  • {name} — {item.quantity} {unit}".rstrip()
        if item.stock_out:
            line += " ❌ нет в наличии"
        lines.append(line)
    return "\n".join(lines)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    parsed = parse_order_text(message.text)
    if not parsed:
        return

    async with _session_factory(context)() as session:
        order, item_objs, _unknown_items, stock_out_names = await create_order(
            session=session,
            source_text=message.text,
            client_name=parsed["client_name"],
            items=parsed["items"],
            message_id=message.message_id,
            chat_id=message.chat_id,
        )

    logger.info("Order #%03d created: %s (%d items)", order.order_number, order.client_name, len(parsed["items"]))

    await _send_stock_out_notice(
        context, message.chat_id, order.order_number, order.client_name, stock_out_names,
    )

    items_text = _format_items(item_objs)
    text = build_order_text(order.order_number, order.client_name, items_text)
    sent = await message.reply_text(text, parse_mode="HTML")

    async with _session_factory(context)() as session:
        await set_bot_message_id(session, order.id, sent.message_id)


async def handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.edited_message
    if not message or not message.text:
        return

    logger.info("[EDIT] msg_id=%s chat_id=%s text=%r", message.message_id, message.chat_id, message.text[:50])

    parsed = parse_order_text(message.text)
    if not parsed:
        logger.info("[EDIT] not an order — skipped")
        return

    async with _session_factory(context)() as session:
        order = await get_order_by_message(session, message.message_id, message.chat_id)
        if not order:
            logger.warning("[EDIT] order not found for msg_id=%s chat_id=%s", message.message_id, message.chat_id)
            return

        order_number = order.order_number
        bot_message_id = order.bot_message_id
        item_objs, _unknown_raw_names, stock_out_names = await update_order_items(
            session, order, parsed["items"], message.text
        )

    logger.info("Order #%03d updated via edit", order_number)

    await _send_stock_out_notice(
        context, message.chat_id, order_number, parsed["client_name"], stock_out_names,
    )

    if bot_message_id:
        items_text = _format_items(item_objs)
        text = build_order_text(order_number, parsed["client_name"], items_text)
        try:
            await context.bot.edit_message_text(
                chat_id=message.chat_id,
                message_id=bot_message_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("Failed to edit bot message: %s", exc)
            await message.reply_text(f"✏️ Заказ #{order_number:03d} обновлён | Позиций: {len(item_objs)}")
    else:
        await message.reply_text(f"✏️ Заказ #{order_number:03d} обновлён | Позиций: {len(item_objs)}")
