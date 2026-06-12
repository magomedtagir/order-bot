import html
import logging
from sqlalchemy import update as sa_update
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.models.models import OrderItem, UnknownItem
from bot.keyboards import build_order_text
from bot.services.normalizer import normalizer
from bot.services.order_service import (
    parse_order_text,
    create_order,
    get_order_by_message,
    update_order_items,
    set_bot_message_id,
)

logger = logging.getLogger(__name__)


def _session_factory(context: ContextTypes.DEFAULT_TYPE) -> async_sessionmaker:
    return context.bot_data["session_factory"]


def _hint_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


async def _send_unknown_hints(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    unknown_names: list[str],
    client_name: str,
    order_id: int,
    order_number: int,
) -> None:
    hint_requests: dict = context.bot_data.setdefault("hint_requests", {})
    for raw in unknown_names:
        try:
            sent_hint = await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f'❓ Неизвестная позиция в заказе #{order_number:03d} '
                    f'({client_name}): «{raw}»\n'
                    f'Ответьте на это сообщение правильным названием.'
                )
            )
            hint_requests[_hint_key(chat_id, sent_hint.message_id)] = {
                "raw_name": raw,
                "client_name": client_name,
                "order_id": order_id,
                "order_number": order_number,
            }
        except Exception as exc:
            logger.warning("Failed to notify chat about unknown item: %s", exc)


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


async def _handle_hint_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Called when a message is a reply — check if it's a hint for an unknown item."""
    message = update.message
    if not message or not message.reply_to_message or not message.text:
        return

    key = _hint_key(message.chat_id, message.reply_to_message.message_id)
    hint_requests: dict = context.bot_data.setdefault("hint_requests", {})
    hint = hint_requests.get(key)
    if not hint:
        return

    hint_text = message.text.strip()
    if not hint_text:
        return

    resolved_name = normalizer.find_product(hint_text) or hint_text

    async with _session_factory(context)() as session:
        await session.execute(
            sa_update(OrderItem)
            .where(OrderItem.order_id == hint["order_id"], OrderItem.raw_name == hint["raw_name"])
            .values(normalized_name=resolved_name, is_unknown=False)
        )
        await session.execute(
            sa_update(UnknownItem)
            .where(UnknownItem.order_id == hint["order_id"], UnknownItem.raw_name == hint["raw_name"])
            .values(resolved=True)
        )
        await normalizer.prepare_resolution(session, hint["client_name"], hint["raw_name"], resolved_name)
        await session.commit()

    hint_requests.pop(key, None)

    note = " (найдено в каталоге)" if resolved_name != hint_text else ""
    await message.reply_text(
        f'✅ «{hint["raw_name"]}» → «{resolved_name}»{note}\nЗапомнил для клиента {hint["client_name"]}.'
    )
    logger.info(
        "[HINT] raw=%r → %r (client=%s, order=%d)",
        hint["raw_name"], resolved_name, hint["client_name"], hint["order_id"],
    )


def _format_items(items: list[OrderItem]) -> str:
    lines = []
    for item in items:
        name = html.escape(item.normalized_name or item.raw_name)
        unit = html.escape(item.unit or "")
        if item.is_unknown:
            name = f"<b>{name}</b>"
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
        await _handle_hint_reply(update, context)
        return

    async with _session_factory(context)() as session:
        order, item_objs, unknown_items, stock_out_names = await create_order(
            session=session,
            source_text=message.text,
            client_name=parsed["client_name"],
            items=parsed["items"],
            message_id=message.message_id,
            chat_id=message.chat_id,
        )

    logger.info("Order #%03d created: %s (%d items)", order.order_number, order.client_name, len(parsed["items"]))

    await _send_unknown_hints(
        context, message.chat_id, unknown_items,
        order.client_name, order.id, order.order_number,
    )
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

    parsed = parse_order_text(message.text)
    if not parsed:
        return

    async with _session_factory(context)() as session:
        order = await get_order_by_message(session, message.message_id, message.chat_id)
        if not order:
            return

        order_number = order.order_number
        order_id = order.id
        bot_message_id = order.bot_message_id
        item_objs, unknown_raw_names, stock_out_names = await update_order_items(
            session, order, parsed["items"], message.text
        )

    logger.info("Order #%03d updated via edit", order_number)

    await _send_unknown_hints(
        context, message.chat_id, unknown_raw_names,
        parsed["client_name"], order_id, order_number,
    )
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
