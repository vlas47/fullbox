import json
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.utils import timezone

LABEL_SIZES = [
    {
        "key": "item",
        "title": "Товар",
        "width_mm": 58,
        "height_mm": 40,
        "preview_scale": 2.1,
        "description": "Этикетка для товара (58x40).",
    },
    {
        "key": "item_cz",
        "title": "Товар ЧЗ",
        "width_mm": 58,
        "height_mm": 40,
        "preview_scale": 2.1,
        "description": "Этикетка для товара с честным знаком (58x40).",
    },
    {
        "key": "box",
        "title": "Короб",
        "width_mm": 58,
        "height_mm": 60,
        "preview_scale": 1.7,
        "description": "Этикетка для коробов (58x60).",
    },
    {
        "key": "pallet",
        "title": "Паллет",
        "width_mm": 75,
        "height_mm": 120,
        "preview_scale": 1.1,
        "description": "Этикетка для паллет (75x120).",
    },
]

LABEL_FIELDS = [
    "barcode",
    "box_client",
    "box_basis",
    "box_pallet_number",
    "cz_code",
    "article",
    "name",
    "size",
    "brand",
    "subject",
    "color",
    "composition",
    "supplier",
    "country",
]
LABEL_SIZE_KEYS = [item["key"] for item in LABEL_SIZES]
SCANNER_EOLS = ("CrLf", "Cr", "Lf", "Tab", "None")
SCANNER_DEFAULT = {
    "enabled": True,
    "port": "COM3",
    "baud": 9600,
    "eol": "CrLf",
    "idle_ms": 200,
}


def available_printers_path() -> Path:
    return settings.BASE_DIR.parent / "available_printers.json"


def load_available_printers_data() -> tuple[list[str], dict]:
    path = available_printers_path()
    if not path.exists():
        return [], {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return [], {}
    meta = {}
    if isinstance(data, dict) and isinstance(data.get("meta"), dict):
        meta = data.get("meta") or {}
    printers = data.get("printers") if isinstance(data, dict) else data
    if isinstance(printers, str):
        printers = [printers]
    if not isinstance(printers, list):
        return [], meta
    normalized = []
    for item in printers:
        text = str(item).strip()
        if text:
            normalized.append(text)
    seen = set()
    unique = []
    for item in normalized:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique, meta


def label_settings_path() -> Path:
    return settings.BASE_DIR.parent / "label_settings.json"


def print_agent_status_path() -> Path:
    return settings.BASE_DIR.parent / "print_agent_status.json"


def scanner_settings_path() -> Path:
    return settings.BASE_DIR.parent / "scanner_settings.json"


def load_print_agent_status() -> dict:
    path = print_agent_status_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_print_agent_status(agent: str, when: datetime | None = None) -> None:
    when_value = when or timezone.now()
    payload = load_print_agent_status()
    payload["agent"] = str(agent or "").strip()
    payload["last_seen"] = when_value.isoformat()
    print_agent_status_path().write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def set_print_agent_pause(paused: bool, by: str | None = None, when: datetime | None = None) -> dict:
    when_value = when or timezone.now()
    payload = load_print_agent_status()
    payload["paused"] = bool(paused)
    if paused:
        payload["paused_at"] = when_value.isoformat()
        if by:
            payload["paused_by"] = str(by)
    else:
        payload["resumed_at"] = when_value.isoformat()
        if by:
            payload["resumed_by"] = str(by)
    print_agent_status_path().write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def _parse_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_int(value, default: int, min_value: int | None = None) -> int:
    if value is None:
        return default
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if min_value is not None and parsed < min_value:
        return default
    return parsed


def _normalize_scanner_block(data: dict) -> dict:
    normalized = dict(SCANNER_DEFAULT)
    normalized["enabled"] = _parse_bool(data.get("enabled"), SCANNER_DEFAULT["enabled"])
    port = str(data.get("port") or data.get("port_name") or "").strip()
    if port:
        normalized["port"] = port
    normalized["baud"] = _parse_int(data.get("baud"), SCANNER_DEFAULT["baud"], min_value=1)
    eol_raw = str(data.get("eol") or data.get("delimiter") or "").strip()
    if eol_raw:
        eol_key = eol_raw.lower()
        eol_lookup = {item.lower(): item for item in SCANNER_EOLS}
        if eol_key in eol_lookup:
            normalized["eol"] = eol_lookup[eol_key]
    normalized["idle_ms"] = _parse_int(data.get("idle_ms"), SCANNER_DEFAULT["idle_ms"], min_value=0)
    return normalized


def normalize_scanner_settings(data: dict | None) -> dict:
    if not isinstance(data, dict):
        return {"default": dict(SCANNER_DEFAULT)}
    payload = data.get("default") if isinstance(data.get("default"), dict) else data
    normalized = {"default": _normalize_scanner_block(payload)}
    updated_at = data.get("updated_at")
    updated_by = data.get("updated_by")
    if updated_at:
        normalized["updated_at"] = str(updated_at)
    if updated_by:
        normalized["updated_by"] = str(updated_by)
    return normalized


def load_scanner_settings() -> dict:
    path = scanner_settings_path()
    if not path.exists():
        return {"default": dict(SCANNER_DEFAULT)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"default": dict(SCANNER_DEFAULT)}
    return normalize_scanner_settings(data)


def save_scanner_settings(data: dict, updated_by: str | None = None, when: datetime | None = None) -> dict:
    path = scanner_settings_path()
    normalized = normalize_scanner_settings(data)
    when_value = when or timezone.now()
    normalized["updated_at"] = when_value.isoformat()
    if updated_by:
        normalized["updated_by"] = str(updated_by).strip()
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


def _clean_label_text(data: dict | None) -> dict:
    if not isinstance(data, dict):
        return {}
    cleaned = {}
    for field in LABEL_FIELDS:
        value = data.get(field)
        if value is None:
            continue
        cleaned[field] = str(value).strip()
    return cleaned


def _clean_label_fonts(data: dict | None) -> dict:
    if not isinstance(data, dict):
        return {}
    cleaned = {}
    for field in LABEL_FIELDS:
        value = data.get(field)
        if value is None:
            continue
        try:
            parsed = float(str(value).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if parsed <= 0:
            continue
        cleaned[field] = parsed
    return cleaned


def clean_label_enabled(data: dict | None) -> dict:
    if not isinstance(data, dict):
        return {}
    cleaned: dict[str, bool] = {}
    for field in LABEL_FIELDS:
        if field not in data:
            continue
        value = data.get(field)
        if isinstance(value, bool):
            cleaned[field] = value
            continue
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "on"}:
            cleaned[field] = True
        elif text in {"0", "false", "no", "off"}:
            cleaned[field] = False
    return cleaned


def normalize_label_settings(data: dict | None) -> dict:
    if not isinstance(data, dict):
        return {}
    normalized = {}
    for key in LABEL_SIZE_KEYS:
        entry = data.get(key)
        if not isinstance(entry, dict):
            continue
        text = _clean_label_text(entry.get("text"))
        fonts = _clean_label_fonts(entry.get("fonts"))
        enabled = clean_label_enabled(entry.get("enabled"))
        if not text and not fonts and not enabled:
            continue
        normalized[key] = {"text": text, "fonts": fonts}
        if enabled:
            normalized[key]["enabled"] = enabled
    return normalized


def load_label_settings() -> dict:
    path = label_settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return normalize_label_settings(data)


def save_label_settings(data: dict) -> None:
    path = label_settings_path()
    normalized = normalize_label_settings(data)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
