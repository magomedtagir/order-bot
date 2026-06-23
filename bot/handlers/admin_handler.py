import logging
import shlex

from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy import select, update as sa_update

from config import settings
from bot.models.models import Synonym, UnknownItem, OrderItem, ClientNameCache
from bot.services.normalizer import normalizer
from bot.services.order_service import (
    get_order_by_number,
    get_unknown_items,
)

logger = logging.getLogger(__name__)


def _session_factory(context: ContextTypes.DEFAULT_TYPE) -> async_sessionmaker:
    return context.bot_data["session_factory"]


async def _require_admin(update: Update) -> bool:
    if not update.effective_user:
        return False
    admin_ids = settings.admin_ids_list
    if admin_ids and update.effective_user.id not in admin_ids:
        await update.message.reply_text("⛔ У вас нет прав для выполнения этой команды.")
        return False
    return True


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

    lines = [
        f"📦 <b>Заказ #{order.order_number:03d}</b>",
        f"Клиент: {order.client_name}",
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

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


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

    # /resolve <order_number> "<raw_name>" "<full_name>"
    try:
        tokens = shlex.split(update.message.text)
    except ValueError:
        await update.message.reply_text(
            'Ошибка разбора. Использование: /resolve <номер_заказа> "<сырое_название>" "<полное_название>"'
        )
        return

    raw_args = tokens[1:]  # drop /resolve or /resolve@botname
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

    raw_name = raw_args[1]
    full_name = raw_args[2]

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
        await normalizer.prepare_resolution(session, client_name, raw_name, full_name)
        await session.commit()

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

    async with _session_factory(context)() as session:
        result = await session.execute(delete(ClientNameCache))
        count = result.rowcount
        await session.commit()

    normalizer._client_cache.clear()

    await update.message.reply_text(f"🗑 Весь кэш очищен ({count} записей).")


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
            settings.STOCK_API_BASE_URL,
            settings.stock_bases_list,
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
