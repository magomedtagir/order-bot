import html as _html
from datetime import date, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.models.models import OrderStatus, STATUS_LABELS, STATUS_TRANSITIONS


def build_status_keyboard(order_number: int, current_status: OrderStatus) -> InlineKeyboardMarkup | None:
    next_statuses = STATUS_TRANSITIONS.get(current_status, [])
    if not next_statuses:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            STATUS_LABELS[s],
            callback_data=f"status:{order_number}:{s.value}:{current_status.value}",
        )
        for s in next_statuses
    ]])


def build_order_text(order_number: int, client_name: str, items_text: str, status: OrderStatus) -> str:
    return (
        f"✅ <b>Заказ #{order_number:03d} принят</b>\n"
        f"Клиент: {_html.escape(client_name)}\n\n"
        f"{items_text}\n\n"
        f"Статус: {STATUS_LABELS[status]}"
    )


def build_orders_summary_keyboard(counts: dict) -> InlineKeyboardMarkup:
    today = date.today().isoformat()
    delivered_count = counts.get(OrderStatus.DELIVERED, 0)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🟡 В очереди ({counts.get(OrderStatus.QUEUED, 0)})",
            callback_data="orders_status:queued",
        )],
        [InlineKeyboardButton(
            f"🔵 В обработке ({counts.get(OrderStatus.PROCESSING, 0)})",
            callback_data="orders_status:processing",
        )],
        [InlineKeyboardButton(
            f"🟠 Запланирована доставка ({counts.get(OrderStatus.DELIVERY_SCHEDULED, 0)})",
            callback_data="orders_status:delivery_scheduled",
        )],
        [InlineKeyboardButton(
            f"🟢 Доставлено сегодня ({delivered_count})",
            callback_data=f"orders_status:delivered:{today}",
        )],
    ])


def build_date_picker_keyboard() -> InlineKeyboardMarkup:
    today = date.today()
    buttons = []
    for i in range(7):
        d = today - timedelta(days=i)
        label = "Сегодня" if i == 0 else d.strftime("%d.%m")
        buttons.append([InlineKeyboardButton(label, callback_data=f"orders_date:{d.isoformat()}")])
    buttons.append([InlineKeyboardButton("« Назад к сводке", callback_data="orders_back")])
    return InlineKeyboardMarkup(buttons)


def build_status_detail_keyboard(
    orders: list,
    current_status: OrderStatus,
) -> InlineKeyboardMarkup | None:
    next_statuses = STATUS_TRANSITIONS.get(current_status, [])
    buttons = []

    if next_statuses:
        next_s = next_statuses[0]
        for order in orders:
            client_short = order.client_name[:22]
            buttons.append([InlineKeyboardButton(
                f"{STATUS_LABELS[next_s]} #{order.order_number:03d} {client_short}",
                callback_data=f"status:{order.order_number}:{next_s.value}:{current_status.value}",
            )])

    if current_status == OrderStatus.DELIVERED:
        buttons.append([InlineKeyboardButton("📅 Другой день", callback_data="orders_date_picker")])

    return InlineKeyboardMarkup(buttons) if buttons else None
