from __future__ import annotations


_ORDER_TYPE_SUFFIXES = {
    "receiving": "PR",
    "processing": "OBR",
    "shipping": "OTG",
}


def format_order_number(order_type: str | None, order_id: str | None) -> str:
    raw = str(order_id or "").strip()
    if not raw:
        return "-"

    normalized_type = str(order_type or "").strip().lower()
    if normalized_type == "shipping":
        number_raw = raw[3:] if raw.startswith("SO-") else raw
        number = str(int(number_raw)) if number_raw.isdigit() else number_raw
        return f"{number}_OTG"

    suffix = _ORDER_TYPE_SUFFIXES.get(normalized_type)
    if suffix and raw.isdigit():
        return f"{raw}_{suffix}"
    return raw


def replace_order_number_in_title(
    title: str | None,
    order_type: str | None,
    order_id: str | None,
    *,
    default_title: str | None = None,
) -> str:
    base_title = str(title or "").strip()
    display_id = format_order_number(order_type, order_id)
    if not base_title:
        return default_title or f"Заявка №{display_id}"

    raw = str(order_id or "").strip()
    if raw:
        if f"№{raw}" in base_title:
            return base_title.replace(f"№{raw}", f"№{display_id}")
        if raw in base_title:
            return base_title.replace(raw, display_id)
    return base_title
