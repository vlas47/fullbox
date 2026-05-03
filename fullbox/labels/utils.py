import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db import models
from django.utils import timezone

from processing_app.models import ProcessingPrintJob

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
        "width_mm": 58,
        "height_mm": 60,
        "preview_scale": 1.7,
        "description": "Этикетка для паллет (58x60).",
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
PRINT_AGENT_ONLINE_SECONDS = 30
PRINT_QUEUE_STUCK_SECONDS = 180
PRINT_REFRESH_WAIT_SECONDS = 8.0
PRINT_REFRESH_POLL_SECONDS = 0.25
_CLIENT_PREFIX_RE = re.compile(r"^\s*клиент\s*:\s*", re.IGNORECASE)
_IP_PREFIX_RE = re.compile(r"^\s*(?:ип|индивидуальный\s+предприниматель)\s+", re.IGNORECASE)
SCANNER_DEFAULT = {
    "enabled": True,
    "port": "COM3",
    "baud": 9600,
    "eol": "CrLf",
    "idle_ms": 200,
}


def available_printers_path() -> Path:
    return settings.BASE_DIR.parent / "available_printers.json"


def shorten_client_label(name: str) -> str:
    text = str(name or "").replace("\xa0", " ").strip()
    if not text:
        return "-"
    text = _CLIENT_PREFIX_RE.sub("", text)
    normalized = " ".join(text.split())
    if not normalized:
        return "-"
    if _IP_PREFIX_RE.match(normalized):
        body = _IP_PREFIX_RE.sub("", normalized).strip()
        parts = [part for part in body.split() if part]
        if parts:
            surname = parts[0]
            initials = "".join(f"{part[0].upper()}." for part in parts[1:3] if part)
            short = f"ИП {surname}"
            if initials:
                short = f"{short} {initials}"
            return short.strip()
        return "ИП"
    return normalized


def _normalize_printer_names(printers) -> list[str]:
    if isinstance(printers, str):
        printers = [printers]
    if not isinstance(printers, list):
        return []
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
    return unique


def _load_agent_printers_data() -> tuple[list[str], dict]:
    try:
        from agent.models import DeviceAgent
    except Exception:
        return [], {}

    online_since = timezone.now() - timedelta(seconds=60)
    agents = list(
        DeviceAgent.objects.filter(last_seen__gte=online_since).order_by("-last_seen", "-updated_at")
    )
    if not agents:
        status = load_print_agent_status()
        preferred = str(status.get("agent") or "").strip()
        if preferred:
            agents = list(
                DeviceAgent.objects.filter(
                    models.Q(name=preferred) | models.Q(host=preferred) | models.Q(agent_id=preferred)
                ).order_by("-last_seen", "-updated_at")[:1]
            )
        if not agents:
            agents = list(DeviceAgent.objects.order_by("-last_seen", "-updated_at")[:1])
    if not agents:
        return [], {}

    printers: list[str] = []
    seen = set()
    sources: list[str] = []
    latest_agent = None
    for agent in agents:
        meta = agent.meta if isinstance(agent.meta, dict) else {}
        for printer in _normalize_printer_names(meta.get("printers")):
            key = printer.lower()
            if key in seen:
                continue
            seen.add(key)
            printers.append(printer)
        label = str(agent.name or agent.host or agent.agent_id or "").strip()
        if label:
            sources.append(label)
        if latest_agent is None and agent.last_seen:
            latest_agent = agent

    if not printers:
        return [], {}

    meta: dict[str, object] = {
        "source": "agent",
        "agents": sources,
    }
    if latest_agent and latest_agent.last_seen:
        meta["updated_at"] = timezone.localtime(latest_agent.last_seen).strftime("%d.%m.%Y %H:%M:%S")
        meta["updated_by"] = str(
            latest_agent.name or latest_agent.host or latest_agent.agent_id or ""
        ).strip()
    return printers, meta


def load_available_printers_data() -> tuple[list[str], dict]:
    path = available_printers_path()
    file_printers: list[str] = []
    if not path.exists():
        file_meta = {}
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            data = None
        file_meta = {}
        if isinstance(data, dict) and isinstance(data.get("meta"), dict):
            file_meta = data.get("meta") or {}
        printers = data.get("printers") if isinstance(data, dict) else data
        file_printers = _normalize_printer_names(printers)

    agent_printers, agent_meta = _load_agent_printers_data()
    merged = []
    seen = set()
    for item in [*agent_printers, *file_printers]:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)

    if agent_printers:
        return merged, agent_meta or file_meta
    return merged, file_meta


def _is_virtual_printer(name: str) -> bool:
    text = str(name or "").strip().lower()
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "pdf",
            "xps",
            "onenote",
            "fax",
            "microsoft print",
            "save to pdf",
            "anydesk",
            "rustdesk",
            "remote desktop",
            "redirected",
        )
    )


def is_virtual_printer_name(name: str) -> bool:
    return _is_virtual_printer(name)


def split_printers_by_kind(printers) -> tuple[list[str], list[str]]:
    real_printers: list[str] = []
    virtual_printers: list[str] = []
    for item in _normalize_printer_names(printers):
        if _is_virtual_printer(item):
            virtual_printers.append(item)
        else:
            real_printers.append(item)
    return real_printers, virtual_printers


def _printer_state_payload(
    name: str,
    *,
    agent_online: bool,
    detail: dict | None = None,
) -> dict:
    entry = detail if isinstance(detail, dict) else {}
    printer_name = str(entry.get("name") or name or "").strip()
    jobs = _parse_int(entry.get("jobs"), 0, min_value=0)
    is_default = _parse_bool(entry.get("is_default"), False)
    is_paused = _parse_bool(entry.get("is_paused"), False)
    is_offline = _parse_bool(entry.get("is_offline"), False)
    is_busy = _parse_bool(entry.get("is_busy"), False)
    is_local = _parse_bool(entry.get("is_local"), False)
    is_network = _parse_bool(entry.get("is_network"), False)
    status_text = str(entry.get("status") or "").strip()
    is_virtual = _is_virtual_printer(printer_name)

    if not agent_online:
        state_key = "unavailable"
        state_label = "Нет связи с агентом"
        color = "red"
    elif not entry:
        state_key = "unknown"
        state_label = "Нет данных"
        color = "gray"
    elif is_offline:
        state_key = "offline"
        state_label = "Не готов"
        color = "red"
    elif is_paused:
        state_key = "paused"
        state_label = "Остановлен"
        color = "red"
    elif is_busy or jobs > 0:
        state_key = "busy"
        state_label = "Печатает"
        color = "gold"
    else:
        state_key = "ready"
        state_label = "Готов"
        color = "green"

    return {
        "name": printer_name,
        "state_key": state_key,
        "state_label": state_label,
        "color": color,
        "is_ready": state_key == "ready",
        "is_default": is_default,
        "is_paused": is_paused,
        "is_offline": is_offline,
        "is_busy": is_busy,
        "is_local": is_local,
        "is_network": is_network,
        "is_virtual": is_virtual,
        "jobs": jobs,
        "status": status_text,
    }


def _preferred_print_agent():
    try:
        from agent.models import DeviceAgent
    except Exception:
        return None

    now = timezone.now()
    online_since = now - timedelta(seconds=PRINT_AGENT_ONLINE_SECONDS)
    status = load_print_agent_status()
    preferred = str(status.get("agent") or "").strip()
    base_qs = DeviceAgent.objects.all().order_by("-last_seen", "-updated_at")
    if preferred:
        exact = base_qs.filter(
            models.Q(name=preferred) | models.Q(host=preferred) | models.Q(agent_id=preferred)
        ).first()
        if exact:
            return exact
    online_agent = base_qs.filter(last_seen__gte=online_since).first()
    if online_agent:
        return online_agent
    return base_qs.first()


def build_print_status_snapshot() -> dict:
    agent_status = load_print_agent_status()
    agent = _preferred_print_agent()
    agent_name = ""
    last_seen_text = "нет данных"
    agent_online = False
    agent_meta = {}

    if agent is not None:
        agent_name = str(agent.name or agent.host or agent.agent_id or "").strip()
        agent_meta = agent.meta if isinstance(agent.meta, dict) else {}
        if agent.last_seen:
            try:
                last_seen_text = timezone.localtime(agent.last_seen).strftime("%d.%m.%Y %H:%M:%S")
            except (TypeError, ValueError):
                last_seen_text = str(agent.last_seen)
            agent_online = agent.last_seen >= timezone.now() - timedelta(seconds=PRINT_AGENT_ONLINE_SECONDS)
    if not agent_name:
        agent_name = str(agent_status.get("agent") or "").strip() or "неизвестно"
    if last_seen_text == "нет данных":
        last_seen_raw = agent_status.get("last_seen")
        if last_seen_raw:
            try:
                last_seen = datetime.fromisoformat(str(last_seen_raw))
                if timezone.is_naive(last_seen):
                    last_seen = timezone.make_aware(last_seen)
                last_seen_text = timezone.localtime(last_seen).strftime("%d.%m.%Y %H:%M:%S")
                agent_online = agent_online or (
                    last_seen >= timezone.now() - timedelta(seconds=PRINT_AGENT_ONLINE_SECONDS)
                )
            except (TypeError, ValueError):
                last_seen_text = str(last_seen_raw)

    available_printers, available_printers_meta = load_available_printers_data()
    raw_details = agent_meta.get("printer_details")
    if not isinstance(raw_details, list):
        raw_details = []
    detail_by_name: dict[str, dict] = {}
    for item in raw_details:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        detail_by_name[name.lower()] = item

    names: list[str] = []
    seen_names = set()
    for item in available_printers:
        name = str(item or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        names.append(name)
    for item in raw_details:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        names.append(name)

    printer_statuses = [
        _printer_state_payload(name, agent_online=agent_online, detail=detail_by_name.get(name.lower()))
        for name in names
    ]
    printer_statuses.sort(key=lambda item: (item["name"].lower(), item["state_key"]))

    pending_qs = ProcessingPrintJob.objects.filter(status=ProcessingPrintJob.STATUS_PENDING).order_by("created_at")
    printing_qs = ProcessingPrintJob.objects.filter(status=ProcessingPrintJob.STATUS_PRINTING).order_by("created_at")
    failed_qs = ProcessingPrintJob.objects.filter(status=ProcessingPrintJob.STATUS_FAILED).order_by("-updated_at")
    stuck_before = timezone.now() - timedelta(seconds=PRINT_QUEUE_STUCK_SECONDS)
    stuck_qs = printing_qs.filter(updated_at__lt=stuck_before)

    def _format_job(job: ProcessingPrintJob) -> str:
        target = str(job.printer_name or "").strip() or "принтер не выбран"
        label = str(job.card_id or job.barcode or job.article or "").strip()
        if label:
            return f"#{job.id} · {target} · {label}"
        return f"#{job.id} · {target}"

    pending_jobs = [_format_job(job) for job in pending_qs[:5]]
    printing_jobs = [_format_job(job) for job in printing_qs[:5]]
    failed_jobs = [_format_job(job) for job in failed_qs[:5]]
    stuck_jobs = [_format_job(job) for job in stuck_qs[:5]]

    pending_count = pending_qs.count()
    printing_count = printing_qs.count()
    failed_count = failed_qs.count()
    stuck_count = stuck_qs.count()
    paused = bool(agent_status.get("paused"))

    if paused:
        print_status_line = "Печать остановлена"
    elif stuck_count:
        print_status_line = f"Есть зависшие задания: {stuck_count}"
    elif failed_count:
        print_status_line = f"Есть ошибки печати: {failed_count}"
    elif printing_count:
        print_status_line = f"Печатается: {printing_count}"
    elif pending_count:
        print_status_line = f"Ожидает в очереди: {pending_count}"
        if not agent_online:
            print_status_line = f"{print_status_line} (агент не активен)"
    else:
        ready_count = sum(1 for item in printer_statuses if item["state_key"] == "ready")
        total_count = len(printer_statuses)
        if total_count:
            print_status_line = f"Готово к печати: {ready_count} из {total_count}"
        else:
            print_status_line = "Нет данных по принтерам"

    agent_line = f"{agent_name} · {last_seen_text}" if last_seen_text else agent_name
    details_updated_at = str(agent_meta.get("printer_details_updated_at") or "").strip()
    if details_updated_at:
        try:
            details_updated_dt = datetime.fromisoformat(details_updated_at)
            if timezone.is_naive(details_updated_dt):
                details_updated_dt = timezone.make_aware(details_updated_dt)
            details_updated_at = timezone.localtime(details_updated_dt).strftime("%d.%m.%Y %H:%M:%S")
        except (TypeError, ValueError):
            pass

    return {
        "available_printers": names,
        "available_printers_meta": available_printers_meta,
        "print_status_line": print_status_line,
        "print_agent_line": agent_line,
        "print_agent_online": agent_online,
        "print_paused": paused,
        "print_queue_pending": pending_count,
        "print_queue_printing": printing_count,
        "print_queue_failed": failed_count,
        "print_queue_stuck": stuck_count,
        "print_queue_pending_jobs": pending_jobs,
        "print_queue_printing_jobs": printing_jobs,
        "print_queue_failed_jobs": failed_jobs,
        "print_queue_stuck_jobs": stuck_jobs,
        "printer_statuses": printer_statuses,
        "printer_details_updated_at": details_updated_at,
    }


def refresh_print_agent_printers() -> tuple[dict, str]:
    try:
        from agent.models import AgentCommand
    except Exception:
        return build_print_status_snapshot(), "Контур агента недоступен."

    agent = _preferred_print_agent()
    if agent is None:
        return build_print_status_snapshot(), "Активный агент печати не найден."

    command = AgentCommand.objects.create(
        agent_id=agent.agent_id,
        command="printer.list_details",
        payload={},
    )
    deadline = time.monotonic() + PRINT_REFRESH_WAIT_SECONDS
    while time.monotonic() < deadline:
        command.refresh_from_db()
        if command.status in {AgentCommand.STATUS_DONE, AgentCommand.STATUS_FAILED}:
            break
        time.sleep(PRINT_REFRESH_POLL_SECONDS)

    command.refresh_from_db()
    message = ""
    if command.status != AgentCommand.STATUS_DONE:
        if command.status == AgentCommand.STATUS_FAILED:
            message = str(command.error or "Агент вернул ошибку при обновлении принтеров.").strip()
        else:
            message = "Агент не ответил на запрос обновления принтеров."
        return build_print_status_snapshot(), message

    result = command.result if isinstance(command.result, dict) else {}
    raw_printers = result.get("printers")
    if not isinstance(raw_printers, list):
        return build_print_status_snapshot(), "Агент не вернул список принтеров."

    normalized_rows: list[dict] = []
    printer_names: list[str] = []
    for item in raw_printers:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        printer_names.append(name)
        normalized_rows.append(
            {
                "name": name,
                "is_default": _parse_bool(item.get("is_default"), False),
                "is_paused": _parse_bool(item.get("is_paused"), False),
                "is_offline": _parse_bool(item.get("is_offline"), False),
                "is_busy": _parse_bool(item.get("is_busy"), False),
                "is_local": _parse_bool(item.get("is_local"), False),
                "is_network": _parse_bool(item.get("is_network"), False),
                "jobs": _parse_int(item.get("jobs"), 0, min_value=0),
                "status": str(item.get("status") or "").strip(),
            }
        )

    meta = dict(agent.meta or {}) if isinstance(agent.meta, dict) else {}
    meta["printers"] = _normalize_printer_names(printer_names)
    meta["printer_details"] = normalized_rows
    meta["printer_details_updated_at"] = timezone.now().isoformat()
    agent.meta = meta
    agent.save(update_fields=["meta", "updated_at"])
    return build_print_status_snapshot(), ""


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
    raw_bytes: bytes
    try:
        raw_bytes = path.read_bytes()
    except OSError:
        return {}
    encodings = ("utf-8", "utf-8-sig", "cp1251", "latin-1")
    parsed = None
    used_encoding = ""
    for encoding in encodings:
        try:
            parsed = json.loads(raw_bytes.decode(encoding))
            used_encoding = encoding
            break
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    if not isinstance(parsed, dict):
        return {}
    if used_encoding and used_encoding != "utf-8":
        try:
            path.write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
    return parsed


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
