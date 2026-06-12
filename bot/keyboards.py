import html as _html


def build_order_text(order_number: int, client_name: str, items_text: str) -> str:
    return (
        f"✅ <b>Заказ #{order_number:03d} принят</b>\n"
        f"Клиент: {_html.escape(client_name)}\n\n"
        f"{items_text}"
    )
