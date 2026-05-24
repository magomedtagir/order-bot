import logging
from datetime import date as date_cls

from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy import select

from config import settings
from bot.models.models import Synonym, OrderStatus, STATUS_LABELS, STATUS_ALIASES, STATUS_TRANSITIONS, UnknownItem
from bot.services.normalizer import normalizer
from bot.keyboards import (
    build_status_keyboard,
    build_orders_summary_keyboard,
    build_status_detail_keyboard,
    build_date_picker_keyboard,
)
from bot.services.order_service import (
    get_order_by_number,
    update_order_status,
    get_recent_orders,
    get_unknown_items,
    get_status_counts,
    get_orders_by_status,
)

logger = logging.getLogger(__name__)


def _session_factory(context: ContextTypes.DEFAULT_TYPE) -> async_sessionmaker:
    return context.bot_data["session_factory"]


async def _require_admin(update: Update) -> bool:
    admin_ids = settings.admin_ids_list
    if admin_ids and update.effective_user.id not in admin_ids:
        await update.message.reply_text("⛔ У вас нет прав для выполнения этой команды.")
        return False
    return True


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return

    async with _session_factory(context)() as session:
        counts = await get_status_counts(session)

    keyboard = build_orders_summary_keyboard(counts)
    sent = await update.message.reply_text(
        "📋 <b>ЗАКАЗЫ НА СЕЙЧАС</b>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    context.bot_data["orders_summary"] = {
        "chat_id": sent.chat_id,
        "message_id": sent.message_id,
    }


async def cmd_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return

    if not context.args:
        await update.message.reply_text("Использование: /order <номер>")
        return

    try:
        order_number = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Некорректный номер заказа.")
        return

    async with _session_factory(context)() as session:
        order = await get_order_by_number(session, order_number)

    if not order:
        await update.message.reply_text(f"❌ Заказ #{order_number:03d} не найден.")
        return

    label = STATUS_LABELS.get(order.status, order.status.value)
    lines = [
        f"📦 <b>Заказ #{order.order_number:03d}</b>",
        f"Клиент: {order.client_name}",
        f"Статус: {label}",
        f"Создан: {order.created_at.strftime('%d.%m.%Y %H:%M')}",
        "",
        "<b>Позиции:</b>",
    ]
    for item in order.items:
        name = item.normalized_name or item.raw_name
        marks = ""
        if item.is_unknown:
            marks += " ❓"
        if item.stock_out:
            marks += " ❌"
        unit = f" {item.unit}" if item.unit else ""
        lines.append(f"  • {name}{marks} — {item.quantity}{unit}")

    if order.history:
        lines.append("")
        lines.append("<b>История статусов:</b>")
        for h in sorted(order.history, key=lambda x: x.changed_at):
            old = STATUS_LABELS.get(h.old_status, "—") if h.old_status else "—"
            new = STATUS_LABELS.get(h.new_status, h.new_status.value)
            lines.append(f"  {h.changed_at.strftime('%d.%m.%Y %H:%M')}: {old} → {new}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Использование: /status <номер> <статус>\n"
            "Статусы: queued, processing, delivery, delivered"
        )
        return

    try:
        order_number = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Некорректный номер заказа.")
        return

    status_key = context.args[1].lower()
    new_status = STATUS_ALIASES.get(status_key)
    if new_status is None:
        await update.message.reply_text(
            f"❌ Неизвестный статус: <code>{status_key}</code>\n"
            "Доступные: queued, processing, delivery, delivered",
            parse_mode="HTML",
        )
        return

    async with _session_factory(context)() as session:
        order = await get_order_by_number(session, order_number)
        if not order:
            await update.message.reply_text(f"❌ Заказ #{order_number:03d} не найден.")
            return

        chat_id = order.chat_id
        client_name = order.client_name
        await update_order_status(session, order, new_status, update.effective_user.id)

    label = STATUS_LABELS[new_status]
    keyboard = build_status_keyboard(order_number, new_status)
    await update.message.reply_text(
        f"✅ Заказ #{order_number:03d} ({client_name})\nСтатус: {label}",
        reply_markup=keyboard,
    )

    notification = f"📢 Статус заказа #{order_number:03d} ({client_name}): {label}"
    if chat_id and chat_id != update.effective_chat.id:
        try:
            await context.bot.send_message(chat_id=chat_id, text=notification)
        except Exception as exc:
            logger.warning("Failed to notify chat %s: %s", chat_id, exc)


async def cmd_synonyms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return

    async with _session_factory(context)() as session:
        result = await session.execute(select(Synonym).order_by(Synonym.raw_name))
        syns = result.scalars().all()

    if not syns:
        await update.message.reply_text("📭 Справочник синонимов пуст.")
        return

    lines = ["📖 <b>Справочник синонимов:</b>\n"]
    for s in syns:
        lines.append(f"  <code>{s.raw_name}</code> → {s.normalized_name}")

    text = "\n".join(lines)
    if len(text) > 4096:
        text = text[:4090] + "\n..."
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_add_synonym(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            'Использование: /add_synonym "сокращение" "полное название"\n'
            'Пример: /add_synonym "вар сгущ" "Вареная сгущенка"'
        )
        return

    raw_name = context.args[0].strip('"\'')
    normalized_name = " ".join(context.args[1:]).strip('"\'')

    async with _session_factory(context)() as session:
        result = await session.execute(
            select(Synonym).where(Synonym.raw_name == raw_name)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.normalized_name = normalized_name
        else:
            session.add(Synonym(raw_name=raw_name, normalized_name=normalized_name))
        await session.commit()

    normalizer.add_to_cache(raw_name, normalized_name)
    logger.info("Synonym saved: %r → %r", raw_name, normalized_name)
    await update.message.reply_text(
        f'✅ Синоним сохранён: "<code>{raw_name}</code>" → "{normalized_name}"',
        parse_mode="HTML",
    )


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return

    async with _session_factory(context)() as session:
        items = await get_unknown_items(session)

    if not items:
        await update.message.reply_text("✅ Нераспознанных позиций нет.")
        return

    lines = [f"❓ <b>Нераспознанные позиции ({len(items)}):</b>\n"]
    for item in items:
        lines.append(
            f"  Заказ #{item.order.order_number:03d} ({item.client_id}): "
            f"<code>{item.raw_name}</code>\n"
            f"  base: <code>{item.base_name}</code>\n"
            f"  → /resolve {item.order.order_number} \"{item.raw_name}\" \"полное название\""
        )

    text = "\n".join(lines)
    if len(text) > 4096:
        text = text[:4090] + "\n..."
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_resolve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return

    # /resolve <order_number> <raw_name> <full_name>
    # args может содержать quoted strings — собираем вручную
    raw_args = update.message.text.split(None, 3)[1:]  # drop /resolve
    if len(raw_args) < 3:
        await update.message.reply_text(
            'Использование: /resolve <номер_заказа> "<сырое_название>" "<полное_название>"'
        )
        return

    try:
        order_number = int(raw_args[0])
    except ValueError:
        await update.message.reply_text("❌ Некорректный номер заказа.")
        return

    raw_name = raw_args[1].strip('"«»\'')
    full_name = raw_args[2].strip('"«»\'')

    from sqlalchemy import update as sa_update
    from bot.models.models import OrderItem

    # Сессия 1: обновить order_items и unknown_items
    async with _session_factory(context)() as session:
        order = await get_order_by_number(session, order_number)
        if not order:
            await update.message.reply_text(f"❌ Заказ #{order_number:03d} не найден.")
            return

        client_name = order.client_name

        await session.execute(
            sa_update(OrderItem)
            .where(OrderItem.order_id == order.id, OrderItem.raw_name == raw_name)
            .values(normalized_name=full_name, is_unknown=False)
        )

        await session.execute(
            sa_update(UnknownItem)
            .where(UnknownItem.order_id == order.id, UnknownItem.raw_name == raw_name)
            .values(resolved=True)
        )

        await session.commit()

    # Сессия 2: записать в кэш клиента (add_resolution делает свой commit)
    async with _session_factory(context)() as session:
        await normalizer.add_resolution(session, client_name, raw_name, full_name)

    await update.message.reply_text(
        f'✅ Позиция «{raw_name}» в заказе #{order_number:03d} сопоставлена с «{full_name}»\n'
        f'Кэш клиента {client_name} обновлён.'
    )


async def cmd_cache(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return

    if not context.args:
        await update.message.reply_text("Использование: /cache <имя_клиента>")
        return

    client_name = " ".join(context.args)
    cache = normalizer.get_client_cache(client_name)

    if not cache:
        await update.message.reply_text(f"📭 Кэш клиента «{client_name}» пуст.")
        return

    lines = [f"🗂 <b>Кэш клиента «{client_name}»:</b>\n"]
    for base_name, entry in cache.items():
        lines.append(
            f"  <code>{base_name}</code> → {entry['full_name']} "
            f"[{entry['resolved_by']}, {entry['confidence']:.2f}]"
        )

    text = "\n".join(lines)
    if len(text) > 4096:
        text = text[:4090] + "\n..."
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_cache_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return

    if not context.args:
        await update.message.reply_text("Использование: /cache_clear <имя_клиента>")
        return

    client_name = " ".join(context.args)
    async with _session_factory(context)() as session:
        count = await normalizer.clear_client_cache(session, client_name)

    await update.message.reply_text(
        f"🗑 Кэш клиента «{client_name}» очищен ({count} записей)."
    )


async def cmd_cache_clear_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return

    from sqlalchemy import delete
    from bot.models.models import ClientNameCache

    async with _session_factory(context)() as session:
        result = await session.execute(delete(ClientNameCache))
        count = result.rowcount
        await session.commit()

    normalizer._client_cache.clear()

    await update.message.reply_text(f"🗑 Весь кэш очищен ({count} записей).")


# ── Детальный просмотр заказов по статусу ──────────────────────────────────

async def _send_status_detail(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    status: OrderStatus,
    date_filter=None,
) -> None:
    async with _session_factory(context)() as session:
        orders = await get_orders_by_status(session, status, date_filter)

    label = STATUS_LABELS[status]

    if not orders:
        suffix = f" за {date_filter.strftime('%d.%m.%Y')}" if date_filter else ""
        await context.bot.send_message(chat_id=chat_id, text=f"{label} — заказов нет{suffix}.")
        return

    lines = [f"<b>{label.upper()} — {len(orders)} заказ(а/ов)</b>"]
    for order in orders:
        time_str = order.created_at.strftime("%H:%M")
        lines.append("")
        lines.append(f"📦 <b>#{order.order_number:03d}</b> | {order.client_name} | {len(order.items)} поз. | {time_str}")
        for item in order.items[:5]:
            name = item.normalized_name or item.raw_name
            unit = f" {item.unit}" if item.unit else ""
            lines.append(f"  • {name} — {item.quantity}{unit}")
        if len(order.items) > 5:
            lines.append(f"  <i>...и ещё {len(order.items) - 5} позиций</i>")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."

    keyboard = build_status_detail_keyboard(orders, status)
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard)


async def cmd_queued(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    await _send_status_detail(context, update.effective_chat.id, OrderStatus.QUEUED)


async def cmd_processing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    await _send_status_detail(context, update.effective_chat.id, OrderStatus.PROCESSING)


async def cmd_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    await _send_status_detail(context, update.effective_chat.id, OrderStatus.DELIVERY_SCHEDULED)


async def cmd_delivered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return
    await _send_status_detail(context, update.effective_chat.id, OrderStatus.DELIVERED, date_cls.today())


async def handle_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if settings.admin_ids_list and query.from_user.id not in settings.admin_ids_list:
        await query.answer("⛔ У вас нет прав для просмотра заказов", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data.startswith("orders_status:"):
        raw = data[len("orders_status:"):]
        # Формат: "queued" или "delivered:2024-05-18"
        if ":" in raw:
            status_val, date_str = raw.split(":", 1)
            date_filter = date_cls.fromisoformat(date_str)
        else:
            status_val = raw
            date_filter = date_cls.today() if status_val == "delivered" else None

        # Нормализуем ключ для STATUS_ALIASES
        status = STATUS_ALIASES.get(status_val) or OrderStatus(status_val)
        await _send_status_detail(context, query.message.chat_id, status, date_filter)

    elif data == "orders_date_picker":
        await query.message.reply_text(
            "📅 Выберите дату:",
            reply_markup=build_date_picker_keyboard(),
        )

    elif data.startswith("orders_date:"):
        date_str = data[len("orders_date:"):]
        date_filter = date_cls.fromisoformat(date_str)
        await _send_status_detail(context, query.message.chat_id, OrderStatus.DELIVERED, date_filter)

    elif data == "orders_back":
        async with _session_factory(context)() as session:
            counts = await get_status_counts(session)
        keyboard = build_orders_summary_keyboard(counts)
        await query.edit_message_text(
            "📋 <b>ЗАКАЗЫ НА СЕЙЧАС</b>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )


# ── Остатки ────────────────────────────────────────────────────────────────

async def cmd_stock_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return

    if not settings.STOCK_API_TOKEN:
        await update.message.reply_text("⚠️ STOCK_API_TOKEN не задан в .env")
        return

    from bot.services.stock_service import stock_checker
    msg = await update.message.reply_text("⏳ Обновляю остатки...")
    try:
        count = await stock_checker.refresh(
            settings.STOCK_API_URL_IPSH,
            settings.STOCK_API_URL_IPD,
            settings.STOCK_API_TOKEN,
        )
        ts = stock_checker.last_refresh.strftime("%d.%m.%Y %H:%M:%S")
        await msg.edit_text(f"✅ Остатки обновлены: {count} позиций\nВремя: {ts}")
    except Exception as exc:
        await msg.edit_text(f"❌ Ошибка при получении остатков: {exc}")


async def cmd_stock_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update):
        return

    from bot.services.stock_service import stock_checker
    if not stock_checker.last_refresh:
        await update.message.reply_text("📦 Остатки ещё не загружены. Используй /stock_refresh")
        return

    ts = stock_checker.last_refresh.strftime("%d.%m.%Y %H:%M:%S")
    await update.message.reply_text(
        f"📦 <b>Остатки со склада</b>\n"
        f"Последнее обновление: {ts}\n"
        f"Позиций в кэше: {stock_checker.item_count}",
        parse_mode="HTML",
    )
