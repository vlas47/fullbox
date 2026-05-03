from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.utils import timezone

from agent.models import AgentContext, DeviceAgent
from audit.models import log_order_action, log_staff_overaction
from employees.models import Employee
from marking.models import MarkingCode
from reachtruck.services.putaway_planner import (
    build_putaway_rows,
    normalize_putaway_location,
    parse_putaway_destinations,
    suggest_putaway_destinations,
)
from sklad.services.stock_availability import StockAvailabilityService
from sklad.services.warehouse_policy import WarehouseActionPolicy
from sklad.services.warehouse_state import WarehouseGoodsStateResolver, WarehouseStateCode
from sku.models import Agency, SKU, SKUBarcode
from sklad.services.warehouse_commands import WarehouseCommandService
from todo.models import Task


_IP_PREFIX_RE = re.compile(r"\bиндивидуальный предприниматель\b", re.IGNORECASE)


def _manager_due_date(submitted_at):
    cutoff = submitted_at.replace(hour=14, minute=0, second=0, microsecond=0)
    if submitted_at <= cutoff:
        return submitted_at.replace(hour=18, minute=0, second=0, microsecond=0)
    next_day = submitted_at + timedelta(days=1)
    return next_day.replace(hour=13, minute=0, second=0, microsecond=0)


@dataclass
class ReceivingWorkflowResult:
    act_payload: dict
    placement_payload: dict
    placement_previously_closed: bool
    storekeeper_tasks_closed: int = 0
    manager_followup_created: bool = False


@dataclass
class ReceivingDispatchResult:
    payload: dict
    manager_tasks_closed: int = 0
    manager_task_created: bool = False
    storekeeper_task_created: bool = False


@dataclass
class ReceivingSubmissionResult:
    order_id: str
    payload: dict
    status_value: str
    status_label: str
    was_update: bool = False
    review_dispatched: bool = False


@dataclass
class ReceivingStatusUpdateResult:
    applied: bool
    payload: dict
    description: str = ""
    manager_tasks_closed: int = 0


@dataclass
class ReceivingActionResult:
    status: str
    payload: dict = field(default_factory=dict)
    reason: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class ReceivingFlowPreparationResult:
    status: str
    status_payload: dict = field(default_factory=dict)
    receiving_mode: str = "standard"
    boxes: list[dict] = field(default_factory=list)
    pallets: list[dict] = field(default_factory=list)
    act_items: list[dict] = field(default_factory=list)
    placement_items: list[dict] = field(default_factory=list)
    act_units: list[dict] = field(default_factory=list)
    flow_state: dict = field(default_factory=dict)
    has_mismatch: bool = False
    normalized_eta: str = ""
    vehicle_number: str = ""
    has_closed_placement_act: bool = False
    reason: str = ""


@dataclass
class ReceivingWarehouseMoveResult:
    status: str
    reason: str = ""
    error_message: str = ""
    created_count: int = 0
    skipped_existing_count: int = 0
    skipped_missing_destination_count: int = 0
    total_count: int = 0
    destinations_by_pallet: dict[str, dict] = field(default_factory=dict)
    source_facts: list[str] = field(default_factory=list)


@dataclass
class ReceivingWarehouseMovePanelResult:
    progress: dict = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    can_send: bool = False


@dataclass
class ReceivingPlacementPreparationResult:
    status: str
    placement_items: list[dict] = field(default_factory=list)
    boxes: list[dict] = field(default_factory=list)
    pallets: list[dict] = field(default_factory=list)
    has_closed_act: bool = False
    reason: str = ""


@dataclass
class ReceivingFlowBoxActionResult:
    status: str
    action_kind: str = ""
    action_label: str = ""
    snapshot: dict = field(default_factory=dict)
    reason: str = ""


def _format_payload_value(value):
    if value is None or value == "":
        return "-"
    return str(value).strip()


def _place_type_label(value: str) -> str:
    text = _format_payload_value(value)
    if text == "-":
        return text
    labels = {
        "pallet": "Паллет",
        "box": "Короб",
        "bag": "Мешок",
    }
    return labels.get(text, text)


def _format_datetime_value(value):
    text = _format_payload_value(value)
    if text == "-":
        return text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    return parsed.strftime("%d.%m.%Y, %H:%M")


def _describe_payload_changes(old_payload, new_payload):
    changes = []
    fields = [
        ("eta_at", "Плановая дата/время"),
        ("expected_boxes", "Количество мест"),
        ("place_type", "Тип мест"),
        ("vehicle_number", "Номер авто"),
        ("driver_phone", "Телефон водителя"),
        ("comment", "Комментарий"),
    ]
    for key, label in fields:
        if key == "eta_at":
            old_val = _format_datetime_value((old_payload or {}).get(key))
            new_val = _format_datetime_value((new_payload or {}).get(key))
        elif key == "place_type":
            old_val = _place_type_label((old_payload or {}).get(key))
            new_val = _place_type_label((new_payload or {}).get(key))
        else:
            old_val = _format_payload_value((old_payload or {}).get(key))
            new_val = _format_payload_value((new_payload or {}).get(key))
        if old_val != new_val:
            changes.append(f"{label}: {old_val} → {new_val}")
    old_items = (old_payload or {}).get("items") or []
    new_items = (new_payload or {}).get("items") or []
    if old_items != new_items:
        changes.append(f"Состав поставки: {len(old_items)} → {len(new_items)} поз.")
    return changes


class ReceivingWorkflowService:
    RECEIVING_GOODS_TYPE_LABELS = {
        "op": "Оптовый",
        "gv": "Готовый",
        "br": "Брак",
        "vz": "Возврат",
        "rh": "Расходный",
        "no": "Не обработанный",
    }
    RECEIVING_MODES = {"standard", "cz"}
    FLOW_BOX_ACTIONS = {
        "edit": ("update", "Редактирование короба"),
        "delete": ("delete", "Удаление короба"),
        "delete_batch": ("delete", "Удаление группы коробов"),
        "move_batch": ("update", "Перемещение группы коробов"),
        "print_batch": ("update", "Печать этикеток группы коробов"),
    }
    FLOW_PAYLOAD_IGNORED_KEYS = {
        "comment",
        "message",
        "status",
        "status_label",
        "submit_action",
        "flow_state",
        "flow_boxes",
        "flow_pallets",
        "flow_active_box",
        "flow_active_pallet",
        "packing_assignee",
        "packing_assignee_id",
        "packing_assignee_role",
    }
    FLOW_PAYLOAD_FALLBACK_IGNORED_KEYS = {
        "comment",
        "message",
        "flow_state",
        "flow_boxes",
        "flow_pallets",
        "flow_active_box",
        "flow_active_pallet",
        "packing_assignee",
        "packing_assignee_id",
        "packing_assignee_role",
    }

    @staticmethod
    def _authenticated_user(user):
        return user if getattr(user, "is_authenticated", False) else None

    @staticmethod
    def _parse_qty_value(raw: str | None) -> int | None:
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        try:
            return int(text)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _item_key(sku: str | None, name: str | None, size: str | None) -> str:
        sku_part = (sku or "").strip().lower()
        name_part = (name or "").strip().lower()
        size_part = (size or "").strip().lower()
        return f"{sku_part}|{name_part}|{size_part}"

    @classmethod
    def _latest_payload(cls, entries) -> dict:
        fallback_payload = {}
        for entry in reversed(entries or []):
            payload = entry.payload or {}
            if not payload:
                continue
            if not fallback_payload:
                fallback_keys = set(payload.keys()) - cls.FLOW_PAYLOAD_FALLBACK_IGNORED_KEYS
                if fallback_keys:
                    fallback_payload = payload
            significant_keys = set(payload.keys()) - cls.FLOW_PAYLOAD_IGNORED_KEYS
            if significant_keys:
                return payload
        return fallback_payload or (entries[-1].payload or {} if entries else {})

    @staticmethod
    def _barcode_value_for_sku(sku, size: str | None) -> str:
        if not sku:
            return ""
        barcodes = list(getattr(sku, "barcodes", []).all())
        if not barcodes:
            return ""
        size_value = (size or "").strip()
        if size_value:
            for barcode in barcodes:
                if (barcode.size or "").strip() == size_value:
                    return barcode.value or ""
        primary = next((barcode for barcode in barcodes if barcode.is_primary), None)
        if primary:
            return primary.value or ""
        return barcodes[0].value or ""

    @classmethod
    def _receiving_sku_map(cls, agency_id: int | None, items: list[dict]) -> dict[str, SKU]:
        if not agency_id or not items:
            return {}
        sku_codes = {
            str(item.get("sku_code") or item.get("sku") or "").strip()
            for item in items
            if str(item.get("sku_code") or item.get("sku") or "").strip()
        }
        if not sku_codes:
            return {}
        result = {}
        for sku in SKU.objects.filter(agency_id=agency_id, deleted=False, sku_code__in=sku_codes):
            key = str(sku.sku_code or "").strip().lower()
            if key and key not in result:
                result[key] = sku
        return result

    @classmethod
    def _receiving_marked_items_map(cls, agency_id: int | None, items: list[dict]) -> dict[str, dict]:
        sku_map = cls._receiving_sku_map(agency_id, items)
        result = {}
        fallback_result = {}
        for item in items or []:
            key = cls._item_key(item.get("sku_code"), item.get("name"), item.get("size"))
            sku_code_key = str(item.get("sku_code") or item.get("sku") or "").strip().lower()
            sku = sku_map.get(sku_code_key)
            candidate = {
                "sku_code": str(item.get("sku_code") or item.get("sku") or "").strip(),
                "name": str(item.get("name") or "").strip(),
                "size": str(item.get("size") or "").strip(),
                "barcode": str(item.get("barcode") or "").strip() or cls._barcode_value_for_sku(sku, item.get("size")),
                "qty": cls._parse_qty_value(item.get("qty") or item.get("actual_qty")) or 0,
                "sku_id": sku.id if sku else None,
            }
            if sku and sku.honest_sign:
                result[key] = candidate
            else:
                fallback_result[key] = candidate
        return result or fallback_result

    @classmethod
    def _collect_receiving_marking_units(
        cls,
        order_id: str,
        valid_box_codes: set[str],
        box_to_pallet: dict[str, str],
        marked_items_map: dict[str, dict],
    ) -> tuple[list[dict], dict[str, int], bool]:
        units = []
        totals = {}
        has_orphan_units = False
        marked_by_pair = {
            (
                str(item.get("sku_code") or "").strip().lower(),
                str(item.get("size") or "").strip().lower(),
            ): (item_key, item)
            for item_key, item in marked_items_map.items()
        }
        queryset = (
            MarkingCode.objects.filter(order_type="receiving", order_id=order_id, used_at__isnull=False)
            .order_by("used_at", "created_at", "id")
        )
        for code in queryset:
            matched = marked_by_pair.get(
                (
                    str(code.sku_code or "").strip().lower(),
                    str(code.size or "").strip().lower(),
                )
            )
            if not matched:
                has_orphan_units = True
                continue
            matched_key, item = matched
            box_code = str(code.box_barcode or "").strip()
            if not box_code or box_code not in valid_box_codes:
                has_orphan_units = True
                continue
            totals[matched_key] = int(totals.get(matched_key, 0)) + 1
            units.append(
                {
                    "sku_code": item.get("sku_code") or "",
                    "name": item.get("name") or "",
                    "size": item.get("size") or "",
                    "barcode": code.barcode or item.get("barcode") or "",
                    "marking_code": code.code,
                    "box_code": box_code,
                    "pallet_code": box_to_pallet.get(box_code, ""),
                    "qty": 1,
                }
            )
        return units, totals, has_orphan_units

    @classmethod
    def _normalize_flow_items(cls, raw_items) -> list[dict]:
        items = []
        for raw in raw_items or []:
            if not isinstance(raw, dict):
                continue
            qty = cls._parse_qty_value(raw.get("qty")) or 0
            if qty <= 0:
                continue
            sku_code = (raw.get("sku_code") or raw.get("sku") or "").strip()
            name = (raw.get("name") or "").strip()
            size = (raw.get("size") or "").strip()
            if not (sku_code or name or size):
                continue
            items.append(
                {
                    "sku_code": sku_code,
                    "sku": sku_code,
                    "name": name,
                    "size": size,
                    "qty": qty,
                }
            )
        return items

    @classmethod
    def normalize_receiving_flow_state(
        cls,
        *,
        boxes_data,
        pallets_data,
        active_box: str = "",
        active_pallet: str = "",
    ) -> dict:
        cleaned_boxes = []
        seen_box_codes = set()
        for idx, box in enumerate(boxes_data or []):
            if not isinstance(box, dict):
                continue
            items = cls._normalize_flow_items(box.get("items") or [])
            sealed = bool(box.get("sealed"))
            if not items and sealed:
                continue
            code = str(box.get("code") or "").strip() or f"BOX-{idx + 1}"
            if code in seen_box_codes:
                code = f"{code}-{idx + 1}"
            seen_box_codes.add(code)
            cleaned_boxes.append(
                {
                    "code": code,
                    "items": items,
                    "sealed": sealed,
                }
            )

        cleaned_pallets = []
        seen_pallet_codes = set()
        for idx, pallet in enumerate(pallets_data or []):
            if not isinstance(pallet, dict):
                continue
            code = str(pallet.get("code") or "").strip() or f"PALLET-{idx + 1}"
            if code in seen_pallet_codes:
                code = f"{code}-{idx + 1}"
            sealed = bool(pallet.get("sealed"))
            boxes = [
                str(box_code).strip()
                for box_code in (pallet.get("boxes") or [])
                if str(box_code or "").strip()
            ]
            boxes = [box_code for box_code in boxes if box_code in seen_box_codes]
            items = cls._normalize_flow_items(pallet.get("items") or [])
            if not boxes and not items and sealed:
                continue
            seen_pallet_codes.add(code)
            location = pallet.get("location")
            if isinstance(location, dict):
                location = dict(location)
            elif isinstance(location, str):
                location = {"zone": location}
            else:
                location = {}
            location.setdefault("zone", "PR")
            cleaned_pallets.append(
                {
                    "code": code,
                    "boxes": boxes,
                    "items": items,
                    "sealed": sealed,
                    "location": location,
                }
            )

        active_box_code = str(active_box or "").strip()
        if active_box_code not in seen_box_codes:
            open_boxes = [box for box in cleaned_boxes if not box.get("sealed")]
            if open_boxes:
                active_box_code = open_boxes[0].get("code") or ""
            elif cleaned_boxes:
                active_box_code = cleaned_boxes[0].get("code") or ""
            else:
                active_box_code = ""

        active_pallet_code = str(active_pallet or "").strip()
        if active_pallet_code not in seen_pallet_codes:
            open_pallets = [pallet for pallet in cleaned_pallets if not pallet.get("sealed")]
            if open_pallets:
                active_pallet_code = open_pallets[0].get("code") or ""
            elif cleaned_pallets:
                active_pallet_code = cleaned_pallets[0].get("code") or ""
            else:
                active_pallet_code = ""

        return {
            "boxes": cleaned_boxes,
            "pallets": cleaned_pallets,
            "activeBox": active_box_code,
            "activePallet": active_pallet_code,
        }

    @staticmethod
    def _normalize_eta_value(raw: str) -> str:
        eta_raw = (raw or "").strip()
        if not eta_raw:
            return ""
        try:
            eta_value = datetime.fromisoformat(eta_raw)
            if timezone.is_naive(eta_value):
                eta_value = timezone.make_aware(eta_value, timezone.get_current_timezone())
            return timezone.localtime(eta_value).isoformat()
        except ValueError:
            return eta_raw

    @staticmethod
    def _parse_int_value(raw) -> int:
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _normalize_zone_code(cls, raw: str) -> str:
        text = (raw or "").strip().upper()
        return text or "PR"

    @classmethod
    def _normalize_receiving_move_location(cls, raw_location, fallback_payload=None) -> dict:
        source = raw_location if isinstance(raw_location, dict) else {}
        fallback = fallback_payload if isinstance(fallback_payload, dict) else {}
        zone = cls._normalize_zone_code(
            source.get("zone")
            or source.get("location")
            or fallback.get("zone")
            or fallback.get("location")
            or ""
        )
        row = cls._parse_int_value(source.get("row") or fallback.get("row")) if zone in {"MR", "OS"} else 0
        section = cls._parse_int_value(source.get("section") or fallback.get("section")) if zone == "OS" else 0
        tier = cls._parse_int_value(source.get("tier") or fallback.get("tier")) if zone == "OS" else 0
        cell = cls._parse_int_value(source.get("cell") or fallback.get("cell")) if zone == "OS" else 0
        return {
            "zone": zone,
            "row": row if zone in {"MR", "OS"} else "",
            "section": section if zone == "OS" else "",
            "tier": tier if zone == "OS" else "",
            "cell": cell if zone == "OS" else "",
        }

    @classmethod
    def _resolve_actor_name(cls, user) -> str:
        actor = cls._resolve_observer(user)
        if actor and actor.full_name:
            return actor.full_name
        auth_user = cls._authenticated_user(user)
        if auth_user:
            full_name = auth_user.get_full_name().strip()
            if full_name:
                return full_name
            username = getattr(auth_user, "username", "") or str(auth_user)
            if username:
                return username
        return "Сотрудник"

    @staticmethod
    def _shorten_ip_name(name: str) -> str:
        if not name:
            return "-"
        normalized = _IP_PREFIX_RE.sub("ИП", name)
        return " ".join(normalized.split()) or "-"

    @classmethod
    def _client_display(cls, agency: Agency | None) -> tuple[str, str]:
        if not agency:
            return "-", ""
        name = agency.agn_name or agency.fio_agn or str(agency)
        return cls._shorten_ip_name(name), (agency.pref or "").strip()

    @classmethod
    def _resolve_observer(cls, user):
        actor = cls._authenticated_user(user)
        if not actor:
            return None
        return Employee.objects.filter(user=actor, is_active=True).first()

    @staticmethod
    def _flow_closed(entries) -> bool:
        for entry in reversed(entries or []):
            payload = entry.payload or {}
            if payload.get("flow_reopened"):
                return False
            if payload.get("flow_closed"):
                return True
        return False

    @classmethod
    def _resolve_storekeeper(cls, user):
        actor = cls._authenticated_user(user)
        employee = None
        if actor:
            employee = Employee.objects.filter(user=actor, role="storekeeper", is_active=True).first()
        if employee:
            return employee
        return (
            Employee.objects.filter(role="storekeeper", is_active=True)
            .order_by("full_name")
            .first()
        )

    @staticmethod
    def _close_manager_tasks(order_id: str) -> int:
        return int(
            Task.objects.filter(
                route=f"/orders/receiving/{order_id}/",
                assigned_to__role="manager",
            )
            .exclude(status="done")
            .update(status="done")
        )

    @staticmethod
    def _close_storekeeper_tasks(order_id: str) -> int:
        return int(
            Task.objects.filter(
                route=f"/orders/receiving/{order_id}/",
                assigned_to__role="storekeeper",
            )
            .exclude(status="done")
            .update(status="done")
        )

    @staticmethod
    def _current_status_entry(entries):
        for entry in reversed(entries or []):
            payload = entry.payload or {}
            if entry.action == "status":
                return entry
            if payload.get("status") or payload.get("status_label") or payload.get("submit_action"):
                return entry
        return entries[-1] if entries else None

    @staticmethod
    def _warehouse_status_from_payload(payload: dict | None) -> bool:
        payload = payload or {}
        status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
        status_label = (payload.get("status_label") or "").lower()
        return status_value in {"warehouse", "on_warehouse"} or "склад" in status_label or "ожидании поставки" in status_label

    @classmethod
    def _receiving_state_result(cls, entries):
        if not entries:
            return None
        status_entry = cls._current_status_entry(entries)
        payload = cls._latest_payload(entries)
        return WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=str(entries[-1].order_id or ""),
            agency=entries[-1].agency if entries else None,
            payload=(status_entry.payload if status_entry else payload) or payload,
        )

    @classmethod
    def can_start_receiving_flow(cls, entries) -> bool:
        state_result = cls._receiving_state_result(entries)
        status_entry = cls._current_status_entry(entries) if entries else None
        return WarehouseActionPolicy.can_start_receiving_flow(
            state_result,
            flow_closed=cls._flow_closed(entries),
            allow_legacy_warehouse=bool(
                state_result
                and state_result.code == WarehouseStateCode.UNKNOWN
                and cls._warehouse_status_from_payload(status_entry.payload if status_entry else {})
            ),
        ).allowed

    @classmethod
    def can_create_receiving_act(cls, entries, *, role: str = "storekeeper") -> bool:
        if not entries:
            return False
        status_entry = cls._current_status_entry(entries)
        status_payload = status_entry.payload or {} if status_entry else {}
        status_result = WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=str(getattr(status_entry, "order_id", "") or (entries[-1].order_id if entries else "")),
            agency=entries[-1].agency if entries else None,
            payload=status_payload,
        )
        return WarehouseActionPolicy.can_create_receiving_act(
            status_result,
            has_receiving_act=bool(cls._find_act_entry(entries, "receiving", "акт приемки")),
            role=role,
            allow_legacy_warehouse=bool(
                status_result.code == WarehouseStateCode.UNKNOWN
                and cls._warehouse_status_from_payload(status_payload)
            ),
        ).allowed

    @classmethod
    def can_autostart_receiving_act(cls, entries) -> bool:
        if not entries or cls._find_act_entry(entries, "receiving", "акт приемки"):
            return False
        status_entry = cls._current_status_entry(entries)
        return cls._warehouse_status_from_payload(status_entry.payload if status_entry else {})

    @classmethod
    def find_receiving_flow_state(cls, entries) -> dict:
        for entry in reversed(entries or []):
            payload = entry.payload or {}
            flow_state = payload.get("flow_state")
            if isinstance(flow_state, dict):
                return flow_state
            boxes = payload.get("flow_boxes")
            pallets = payload.get("flow_pallets")
            if boxes or pallets:
                return {
                    "boxes": boxes or [],
                    "pallets": pallets or [],
                    "activeBox": payload.get("flow_active_box") or "",
                    "activePallet": payload.get("flow_active_pallet") or "",
                }
        return {}

    @classmethod
    def prepare_receiving_flow_completion(
        cls,
        *,
        order_id: str,
        entries,
        boxes_raw: str,
        pallets_raw: str,
    ) -> ReceivingFlowPreparationResult:
        if not order_id or not entries:
            return ReceivingFlowPreparationResult(status="missing", reason="missing")
        status_entry = cls._current_status_entry(entries)
        status_payload = dict(status_entry.payload or {}) if status_entry else {}
        payload = dict(cls._latest_payload(entries))
        for key in (
            "items",
            "receiving_mode",
            "eta_at",
            "vehicle_number",
            "goods_type",
            "goods_type_label",
        ):
            value = status_payload.get(key)
            if value not in (None, "", []):
                payload[key] = value
        planned_items = payload.get("items") or []
        receiving_mode = (payload.get("receiving_mode") or "standard").strip().lower()
        if receiving_mode not in cls.RECEIVING_MODES:
            receiving_mode = "standard"
        marked_items_map = cls._receiving_marked_items_map(entries[-1].agency_id if entries else None, planned_items)
        plan_map = {}
        for item in planned_items:
            key = cls._item_key(item.get("sku_code"), item.get("name"), item.get("size"))
            planned_qty = cls._parse_qty_value(item.get("qty")) or 0
            entry = plan_map.setdefault(
                key,
                {
                    "sku_code": item.get("sku_code"),
                    "name": item.get("name"),
                    "size": item.get("size"),
                    "planned_qty": 0,
                    "comment": item.get("comment"),
                },
            )
            entry["planned_qty"] += planned_qty

        try:
            boxes_data = json.loads(boxes_raw or "[]")
            pallets_data = json.loads(pallets_raw or "[]")
        except json.JSONDecodeError:
            return ReceivingFlowPreparationResult(status="invalid", reason="invalid_json")
        if not isinstance(boxes_data, list):
            boxes_data = []
        if not isinstance(pallets_data, list):
            pallets_data = []

        cleaned_boxes = []
        seen_box_codes = set()
        for idx, box in enumerate(boxes_data):
            if not isinstance(box, dict):
                continue
            items = cls._normalize_flow_items(box.get("items") or [])
            if not items:
                continue
            code = str(box.get("code") or "").strip() or f"BOX-{idx + 1}"
            if code in seen_box_codes:
                code = f"{code}-{idx + 1}"
            seen_box_codes.add(code)
            cleaned_boxes.append(
                {
                    "code": code,
                    "items": items,
                    "sealed": True,
                }
            )

        cleaned_pallets = []
        seen_pallet_codes = set()
        for idx, pallet in enumerate(pallets_data):
            if not isinstance(pallet, dict):
                continue
            code = str(pallet.get("code") or "").strip() or f"PALLET-{idx + 1}"
            if code in seen_pallet_codes:
                code = f"{code}-{idx + 1}"
            seen_pallet_codes.add(code)
            boxes = [
                str(box_code).strip()
                for box_code in (pallet.get("boxes") or [])
                if str(box_code or "").strip()
            ]
            boxes = [box_code for box_code in boxes if box_code in seen_box_codes]
            items = cls._normalize_flow_items(pallet.get("items") or [])
            if not boxes and not items:
                continue
            location = pallet.get("location")
            if isinstance(location, dict):
                location = dict(location)
            elif isinstance(location, str):
                location = {"zone": location}
            else:
                location = {}
            location.setdefault("zone", "PR")
            cleaned_pallets.append(
                {
                    "code": code,
                    "boxes": boxes,
                    "items": items,
                    "sealed": True,
                    "location": location,
                }
            )

        if not cleaned_boxes or not cleaned_pallets:
            return ReceivingFlowPreparationResult(status="invalid", reason="missing_containers")

        pallet_box_codes = set()
        for pallet in cleaned_pallets:
            for box_code in pallet.get("boxes") or []:
                if box_code:
                    pallet_box_codes.add(box_code)
        unassigned_boxes = [box for box in cleaned_boxes if box["code"] not in pallet_box_codes]
        if unassigned_boxes:
            return ReceivingFlowPreparationResult(status="invalid", reason="unassigned_boxes")

        totals = {}

        def add_total(item, qty, field):
            key = cls._item_key(item.get("sku_code"), item.get("name"), item.get("size"))
            entry = totals.setdefault(key, {"box": 0, "pallet": 0, "total": 0})
            entry[field] += qty
            entry["total"] += qty

        for box in cleaned_boxes:
            for item in box.get("items") or []:
                qty = cls._parse_qty_value(item.get("qty")) or 0
                add_total(item, qty, "box")
        for pallet in cleaned_pallets:
            for item in pallet.get("items") or []:
                qty = cls._parse_qty_value(item.get("qty")) or 0
                add_total(item, qty, "pallet")

        valid_box_codes = {box["code"] for box in cleaned_boxes}
        box_to_pallet = {}
        for pallet in cleaned_pallets:
            for box_code in pallet.get("boxes") or []:
                box_to_pallet[str(box_code or "").strip()] = pallet.get("code") or ""

        act_items = []
        has_mismatch = False
        for key, plan in plan_map.items():
            entry = totals.get(key, {"box": 0, "pallet": 0, "total": 0})
            actual_qty = entry["total"]
            if actual_qty != plan["planned_qty"]:
                has_mismatch = True
            act_items.append(
                {
                    "sku_code": plan.get("sku_code"),
                    "name": plan.get("name"),
                    "size": plan.get("size"),
                    "planned_qty": plan.get("planned_qty"),
                    "actual_qty": actual_qty,
                    "comment": plan.get("comment"),
                }
            )

        extra_keys = set(totals.keys()) - set(plan_map.keys())
        for key in extra_keys:
            entry = totals.get(key)
            if not entry or entry["total"] <= 0:
                continue
            parts = key.split("|")
            sku_code = parts[0] if len(parts) > 0 else ""
            name = parts[1] if len(parts) > 1 else ""
            size = parts[2] if len(parts) > 2 else ""
            has_mismatch = True
            act_items.append(
                {
                    "sku_code": sku_code,
                    "name": name,
                    "size": size,
                    "planned_qty": 0,
                    "actual_qty": entry["total"],
                    "comment": "",
                    "extra": True,
                }
            )

        act_units = []
        if receiving_mode == "cz" and marked_items_map:
            act_units, marked_totals, has_orphan_units = cls._collect_receiving_marking_units(
                order_id,
                valid_box_codes,
                box_to_pallet,
                marked_items_map,
            )
            if has_orphan_units:
                return ReceivingFlowPreparationResult(status="invalid", reason="orphan_units")
            for key in marked_items_map:
                actual_total = int((totals.get(key) or {}).get("total") or 0)
                scanned_total = int(marked_totals.get(key, 0))
                if actual_total != scanned_total:
                    return ReceivingFlowPreparationResult(status="invalid", reason="marking_mismatch")

        if not act_items:
            return ReceivingFlowPreparationResult(status="invalid", reason="missing_items")

        flow_state = cls.normalize_receiving_flow_state(
            boxes_data=boxes_data,
            pallets_data=pallets_data,
            active_box="",
            active_pallet="",
        )
        has_closed_act = any(
            (entry.payload or {}).get("act") == "placement"
            and ((entry.payload or {}).get("act_state") or "closed") == "closed"
            for entry in entries
        )
        placement_items = []
        for item in act_items:
            key = cls._item_key(item.get("sku_code"), item.get("name"), item.get("size"))
            entry = totals.get(key, {"box": 0, "pallet": 0, "total": 0})
            placement_items.append(
                {
                    "sku_code": item.get("sku_code"),
                    "name": item.get("name"),
                    "size": item.get("size"),
                    "actual_qty": item.get("actual_qty") or 0,
                    "box_qty": entry["box"],
                    "pallet_qty": entry["pallet"],
                    "comment": item.get("comment"),
                }
            )

        return ReceivingFlowPreparationResult(
            status="ok",
            status_payload=status_payload,
            receiving_mode=receiving_mode,
            boxes=cleaned_boxes,
            pallets=cleaned_pallets,
            act_items=act_items,
            placement_items=placement_items,
            act_units=act_units,
            flow_state=flow_state,
            has_mismatch=has_mismatch,
            normalized_eta=cls._normalize_eta_value(payload.get("eta_at") or ""),
            vehicle_number=(payload.get("vehicle_number") or "").strip(),
            has_closed_placement_act=has_closed_act,
        )

    @classmethod
    def save_receiving_flow_draft(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        boxes_raw: str,
        pallets_raw: str,
        active_box: str = "",
        active_pallet: str = "",
        user=None,
    ) -> ReceivingActionResult:
        if role != "storekeeper":
            return ReceivingActionResult(status="forbidden")
        if not order_id or not entries:
            return ReceivingActionResult(status="missing")
        flow_closed = cls._flow_closed(entries)
        if flow_closed:
            return ReceivingActionResult(status="closed")
        state_result = cls._receiving_state_result(entries)
        status_entry = cls._current_status_entry(entries)
        status_payload = dict(status_entry.payload or {}) if status_entry else {}
        can_start = WarehouseActionPolicy.can_start_receiving_flow(
            state_result,
            flow_closed=flow_closed,
            allow_legacy_warehouse=bool(
                state_result
                and state_result.code == WarehouseStateCode.UNKNOWN
                and cls._warehouse_status_from_payload(status_payload)
            ),
        ).allowed
        if not can_start:
            return ReceivingActionResult(status="not_allowed")
        try:
            boxes_data = json.loads(boxes_raw or "[]")
            pallets_data = json.loads(pallets_raw or "[]")
        except json.JSONDecodeError:
            return ReceivingActionResult(status="invalid_json")
        if not isinstance(boxes_data, list):
            boxes_data = []
        if not isinstance(pallets_data, list):
            pallets_data = []
        flow_state = cls.normalize_receiving_flow_state(
            boxes_data=boxes_data,
            pallets_data=pallets_data,
            active_box=active_box,
            active_pallet=active_pallet,
        )
        payload = {"flow_state": flow_state}
        latest = entries[-1] if entries else None
        draft_entry = None
        for entry in reversed(entries or []):
            if entry.action == "update" and (entry.payload or {}).get("flow_state"):
                draft_entry = entry
                break
        if draft_entry:
            draft_entry.payload = payload
            draft_entry.description = "Черновик приемки потоком"
            draft_entry.save(update_fields=["payload", "description"])
        else:
            log_order_action(
                "update",
                order_id=order_id,
                order_type="receiving",
                user=cls._authenticated_user(user),
                agency=latest.agency if latest else None,
                description="Черновик приемки потоком",
                payload=payload,
            )
        return ReceivingActionResult(
            status="saved",
            payload=payload,
            meta={"updated_existing": bool(draft_entry)},
        )

    @classmethod
    def prepare_receiving_placement_close(
        cls,
        *,
        order_id: str,
        entries,
        boxes_raw: str,
        pallets_raw: str,
        order_type: str = "receiving",
    ) -> ReceivingPlacementPreparationResult:
        if not order_id or not entries:
            return ReceivingPlacementPreparationResult(status="missing", reason="missing")
        receiving_entry = cls._find_act_entry(entries, "receiving", "акт приемки")
        act_items = (receiving_entry.payload or {}).get("act_items") if receiving_entry else []
        if not act_items:
            return ReceivingPlacementPreparationResult(status="invalid", reason="missing_act_items")
        try:
            boxes_data = json.loads(boxes_raw or "[]")
            pallets_data = json.loads(pallets_raw or "[]")
        except json.JSONDecodeError:
            return ReceivingPlacementPreparationResult(status="invalid", reason="invalid_json")
        if not isinstance(boxes_data, list):
            boxes_data = []
        if isinstance(pallets_data, list):
            pallets_data = [
                pallet
                for pallet in pallets_data
                if isinstance(pallet, dict)
                and (((pallet.get("items") or []) or (pallet.get("boxes") or [])))
            ]
        else:
            pallets_data = []
        boxes = [box for box in boxes_data if isinstance(box, dict)]
        pallets = [pallet for pallet in pallets_data if isinstance(pallet, dict)]

        totals: dict[str, dict[str, int]] = {}

        def add_total(item, qty, field):
            key = cls._item_key(item.get("sku"), item.get("name"), item.get("size"))
            entry = totals.setdefault(key, {"box": 0, "pallet": 0, "total": 0})
            entry[field] += qty
            entry["total"] += qty

        for box in boxes:
            for item in (box.get("items") or []):
                qty = cls._parse_qty_value((item or {}).get("qty")) or 0
                add_total(item or {}, qty, "box")
        for pallet in pallets:
            for item in (pallet.get("items") or []):
                qty = cls._parse_qty_value((item or {}).get("qty")) or 0
                add_total(item or {}, qty, "pallet")

        placement_items = []
        for item in act_items:
            key = cls._item_key(item.get("sku_code"), item.get("name"), item.get("size"))
            entry = totals.get(key, {"box": 0, "pallet": 0, "total": 0})
            actual_qty = cls._parse_qty_value(item.get("actual_qty")) or 0
            if entry["total"] != actual_qty:
                return ReceivingPlacementPreparationResult(status="invalid", reason="qty_mismatch")
            placement_items.append(
                {
                    "sku_code": item.get("sku_code"),
                    "name": item.get("name"),
                    "size": item.get("size"),
                    "actual_qty": actual_qty,
                    "box_qty": entry["box"],
                    "pallet_qty": entry["pallet"],
                    "comment": item.get("comment"),
                }
            )

        for box in boxes:
            if not box.get("sealed"):
                return ReceivingPlacementPreparationResult(status="invalid", reason="unsealed_box")

        pallet_box_codes = set()
        for pallet in pallets:
            for box_code in (pallet.get("boxes") or []):
                text = str(box_code or "").strip()
                if text:
                    pallet_box_codes.add(text)
        unassigned_boxes = [
            box
            for box in boxes
            if str(box.get("code") or "").strip() and str(box.get("code") or "").strip() not in pallet_box_codes
        ]
        if unassigned_boxes:
            return ReceivingPlacementPreparationResult(status="invalid", reason="unassigned_boxes")

        occupied_cells = StockAvailabilityService.occupied_os_cell_keys(
            exclude_order_type=order_type,
            exclude_order_id=str(order_id or ""),
        )
        used_cells = set()
        normalized_pallets = []
        allowed_zones = {"PR", "OTG", "MR", "OS"}
        for pallet in pallets:
            if not pallet.get("sealed"):
                return ReceivingPlacementPreparationResult(status="invalid", reason="unsealed_pallet")
            location = cls._normalize_receiving_move_location(pallet.get("location"), pallet)
            zone = cls._normalize_zone_code(location.get("zone") or "PR")
            if zone not in allowed_zones:
                zone = "PR"
            row = cls._parse_int_value(location.get("row"))
            section = cls._parse_int_value(location.get("section"))
            tier = cls._parse_int_value(location.get("tier"))
            cell = cls._parse_int_value(location.get("cell"))
            if zone == "MR":
                if not row:
                    return ReceivingPlacementPreparationResult(status="invalid", reason="invalid_mr_location")
            elif zone == "OS":
                if not (row and section and tier and cell):
                    return ReceivingPlacementPreparationResult(status="invalid", reason="invalid_os_location")
                key = (row, section, tier, cell)
                if key in occupied_cells or key in used_cells:
                    return ReceivingPlacementPreparationResult(status="invalid", reason="occupied_os_location")
                used_cells.add(key)
            normalized_pallets.append(
                {
                    **pallet,
                    "location": {
                        "zone": zone,
                        "row": row if zone in {"MR", "OS"} else "",
                        "section": section if zone == "OS" else "",
                        "tier": tier if zone == "OS" else "",
                        "cell": cell if zone == "OS" else "",
                    },
                }
            )

        has_closed_act = any(
            (entry.payload or {}).get("act") == "placement"
            and ((entry.payload or {}).get("act_state") or "closed") == "closed"
            for entry in entries
        )
        return ReceivingPlacementPreparationResult(
            status="ok",
            placement_items=placement_items,
            boxes=boxes,
            pallets=normalized_pallets,
            has_closed_act=has_closed_act,
        )

    @classmethod
    def build_receiving_flow_page_context(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        user=None,
        session_key: str = "",
        query_params: dict | None = None,
        row_sections: dict | None = None,
        tiers: list | tuple | None = None,
        cells_per_tier: int = 0,
    ) -> dict:
        latest = entries[-1] if entries else None
        status_entry = cls._current_status_entry(entries)
        payload = cls._latest_payload(entries)
        items = payload.get("items") or []
        sku_map = cls._receiving_sku_map(latest.agency_id if latest else None, items)
        display_items = []
        for item in items:
            sku_code = str(item.get("sku_code") or "").strip()
            sku = sku_map.get(sku_code.lower()) if sku_code else None
            weight_value = item.get("weight_kg")
            if weight_value in (None, "") and sku and sku.weight_kg is not None:
                weight_value = sku.weight_kg
            display_items.append(
                {
                    "sku_code": sku_code,
                    "name": item.get("name") or "",
                    "size": item.get("size") or "",
                    "qty": item.get("qty") or 0,
                    "comment": item.get("comment") or "",
                    "weight_kg": str(weight_value).strip() if weight_value not in (None, "") else "",
                }
            )
        client_label, client_prefix = cls._client_display(latest.agency if latest else None)
        status_payload = status_entry.payload or {} if status_entry else {}
        goods_type = (status_payload.get("goods_type") or "").strip().lower()
        receiving_mode = (status_payload.get("receiving_mode") or "standard").strip().lower()
        if receiving_mode not in cls.RECEIVING_MODES:
            receiving_mode = "standard"
        goods_type_labels = {
            "op": "Оптовый",
            "gv": "Готовый",
            "br": "Брак",
            "vz": "Возврат",
            "rh": "Расходный",
            "no": "Не обработанный",
        }
        barcode_map = {}
        catalog_items = []
        marked_items_map = cls._receiving_marked_items_map(latest.agency_id if latest else None, items)
        if receiving_mode == "cz" and not marked_items_map:
            receiving_mode = "standard"
        if latest and latest.agency_id:
            for sku in SKU.objects.filter(agency_id=latest.agency_id, deleted=False).only("sku_code", "name", "size", "weight_kg"):
                catalog_items.append(
                    {
                        "sku_code": sku.sku_code,
                        "name": sku.name,
                        "size": sku.size,
                        "weight_kg": str(sku.weight_kg).strip() if sku.weight_kg is not None else "",
                    }
                )
            for barcode in SKUBarcode.objects.select_related("sku").filter(
                sku__agency_id=latest.agency_id,
                sku__deleted=False,
            ):
                value = (barcode.value or "").strip()
                if not value:
                    continue
                sku = barcode.sku
                barcode_map[value] = {
                    "sku_code": sku.sku_code,
                    "name": sku.name,
                    "size": (barcode.size or sku.size or "").strip(),
                    "weight_kg": str(sku.weight_kg).strip() if sku.weight_kg is not None else "",
                }
        placement_entry = cls._find_act_entry(entries, "placement", "акт размещения")
        placement_payload = placement_entry.payload if placement_entry else {}
        if not isinstance(placement_payload, dict):
            placement_payload = {}
        placement_pallets = placement_payload.get("act_pallets") or []
        if not isinstance(placement_pallets, list):
            placement_pallets = []
        flow_state = cls.find_receiving_flow_state(entries)
        if not flow_state or not (flow_state.get("boxes") or flow_state.get("pallets")):
            if placement_entry:
                placement_boxes = placement_payload.get("act_boxes") or []
                if placement_boxes or placement_pallets:
                    active_box = next((box.get("code") for box in placement_boxes if not box.get("sealed")), "")
                    active_pallet = next((pallet.get("code") for pallet in placement_pallets if not pallet.get("sealed")), "")
                    flow_state = {
                        "boxes": placement_boxes,
                        "pallets": placement_pallets,
                        "activeBox": active_box or "",
                        "activePallet": active_pallet or "",
                    }
        flow_locked = cls._flow_closed(entries)
        receiving_result = cls._receiving_state_result(entries)
        warehouse_move_panel = cls.build_receiving_warehouse_move_panel(
            order_id=str(order_id or ""),
            placement_pallets=placement_pallets,
            receiving_result=receiving_result,
            flow_locked=flow_locked,
            role_allowed=role in {"storekeeper", "manager", "head_manager", "director", "admin"},
            agency_id=latest.agency_id if latest else None,
            row_sections=row_sections,
            tiers=tiers,
            cells_per_tier=cells_per_tier,
        )
        params = query_params or {}
        warehouse_move_progress = warehouse_move_panel.progress
        warehouse_move_status = str(params.get("warehouse_move") or "").strip().lower()
        warehouse_move_created_count = cls._parse_int_value(params.get("warehouse_created"))
        warehouse_move_skipped_count = cls._parse_int_value(params.get("warehouse_skipped"))
        warehouse_move_missing_count = cls._parse_int_value(params.get("warehouse_missing"))
        warehouse_move_error = str(params.get("warehouse_error") or "").strip()
        occupied_cells = StockAvailabilityService.occupied_os_cells(
            exclude_order_type="receiving",
            exclude_order_id=order_id,
            include_agency=True,
        )
        act_entry = cls._find_act_entry(entries, "receiving", "акт приемки")
        act_print_url = ""
        if act_entry and flow_locked:
            act_print_url = f"/orders/receiving/{order_id}/act/print/?return=/orders/receiving/{order_id}/flow/"
        scanner_agents = []
        preferred_agent_id = ""
        preferred_agent_locked = False
        try:
            now = timezone.now()
            online_threshold = timezone.now() - timedelta(seconds=30)
            active_context = None
            if getattr(user, "is_authenticated", False):
                context_qs = AgentContext.objects.filter(
                    user=user,
                    active=True,
                    expires_at__gt=now,
                )
                if order_id:
                    try:
                        order_value = int(str(order_id).strip())
                    except (TypeError, ValueError):
                        order_value = None
                    if order_value is not None:
                        context_qs = context_qs.filter(order_id=order_value)
                session_key_text = str(session_key or "").strip()
                if session_key_text:
                    active_context = (
                        context_qs.filter(session_key=session_key_text)
                        .order_by("-last_seen", "-updated_at")
                        .first()
                    )
                    if active_context and active_context.agent_id:
                        preferred_agent_locked = True
                if not active_context:
                    active_context_count = context_qs.count()
                    if active_context_count == 1:
                        active_context = context_qs.order_by("-last_seen", "-updated_at").first()
                        if active_context and active_context.agent_id:
                            preferred_agent_locked = True
                if active_context and active_context.agent_id:
                    preferred_agent_id = str(active_context.agent_id).strip()
            all_agents = []
            for agent in DeviceAgent.objects.all().order_by("-last_seen", "-updated_at"):
                is_online = bool(agent.last_seen and agent.last_seen >= online_threshold)
                all_agents.append(
                    {
                        "agent_id": agent.agent_id,
                        "title": agent.name or agent.host or agent.agent_id,
                        "status": "онлайн" if is_online else "нет связи",
                        "is_online": is_online,
                        "last_seen": agent.last_seen.isoformat() if agent.last_seen else "",
                    }
                )
            if preferred_agent_id and preferred_agent_locked:
                scanner_agents = [
                    agent_data
                    for agent_data in all_agents
                    if str(agent_data.get("agent_id") or "").strip() == preferred_agent_id
                ]
            if not scanner_agents:
                scanner_agents = all_agents
        except Exception:
            scanner_agents = []
            preferred_agent_id = ""
            preferred_agent_locked = False
        try:
            from labels.utils import load_label_settings

            label_settings = load_label_settings()
        except Exception:
            label_settings = {}
        status_audience = "storekeeper" if role == "storekeeper" else "default"
        if not receiving_result:
            receiving_result = WarehouseGoodsStateResolver.resolve_for_receiving_order(
                order_id=str(order_id or ""),
                agency=latest.agency if latest else None,
                payload=payload,
            )
        return {
            "order_id": order_id,
            "client_label": client_label,
            "client_prefix": client_prefix,
            "status_label": receiving_result.label_for(status_audience),
            "goods_type": goods_type,
            "goods_type_label": goods_type_labels.get(goods_type, ""),
            "receiving_mode": receiving_mode,
            "receiving_mode_label": "Приемка с ЧЗ" if receiving_mode == "cz" else "Обычная приемка",
            "items": display_items,
            "barcode_map": barcode_map,
            "catalog_items": catalog_items,
            "marked_items": list(marked_items_map.values()),
            "marking_scan_url": f"/marking/receiving/{order_id}/scan/" if marked_items_map else "",
            "flow_state": flow_state,
            "flow_locked": flow_locked,
            "scanner_agents": scanner_agents,
            "preferred_agent_id": preferred_agent_id,
            "preferred_agent_locked": preferred_agent_locked,
            "label_settings": label_settings,
            "can_send_to_warehouse_action": warehouse_move_panel.can_send,
            "warehouse_move_progress": warehouse_move_progress,
            "warehouse_total_pallets": int(warehouse_move_progress.get("total_pallets") or 0),
            "warehouse_done_pallets": int(warehouse_move_progress.get("done_count") or 0),
            "warehouse_created_pallets": int(warehouse_move_progress.get("created_count") or 0),
            "warehouse_in_progress_pallets": int(warehouse_move_progress.get("in_progress_count") or 0),
            "warehouse_not_created_pallets": int(warehouse_move_progress.get("not_created_count") or 0),
            "warehouse_move_status": warehouse_move_status,
            "warehouse_move_created_count": warehouse_move_created_count,
            "warehouse_move_skipped_count": warehouse_move_skipped_count,
            "warehouse_move_missing_count": warehouse_move_missing_count,
            "warehouse_move_rows": warehouse_move_panel.rows,
            "warehouse_move_error": warehouse_move_error,
            "occupied_cells": occupied_cells,
            "current_agency_id": latest.agency_id if latest else 0,
            "act_print_url": act_print_url,
        }

    @classmethod
    def build_receiving_act_page_context(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        client_view: bool = False,
        client_agency=None,
        cabinet_url: str = "",
    ) -> dict:
        latest = entries[-1] if entries else None
        status_entry = cls._current_status_entry(entries)
        payload = cls._latest_payload(entries)
        order_title = "Заявка на приемку без указания товара" if not (payload.get("items") or []) else "Заявка на приемку"
        items = payload.get("items") or []
        act_entry = cls._find_act_entry(entries, "receiving", "акт приемки")
        act_items = (act_entry.payload or {}).get("act_items") if act_entry else []
        flow_closed = cls._flow_closed(entries)
        act_label = ((act_entry.payload or {}).get("act_label") or "Акт приемки") if act_entry else "Акт приемки"
        act_documents = []
        if act_entry and flow_closed:
            act_documents = [
                {"label": "Акт приемки печатная форма", "url": f"/orders/receiving/{order_id}/act/print/"},
                {"label": "МХ-1", "url": f"/orders/receiving/{order_id}/act/mx1/print/"},
            ]
            if role == "storekeeper":
                act_documents = [act_documents[0]]
        can_submit = role == "storekeeper" and cls.can_create_receiving_act(entries, role="storekeeper")
        if client_view:
            can_submit = False
        base_items = act_items or items
        sku_ids = set()
        sku_codes = set()
        for item in base_items:
            sku_code = str(item.get("sku_code") or "").strip()
            if sku_code:
                sku_codes.add(sku_code)
            sku_id_raw = item.get("sku_id")
            if sku_id_raw:
                try:
                    sku_ids.add(int(sku_id_raw))
                except (TypeError, ValueError):
                    pass
        sku_by_id = {}
        if sku_ids:
            for sku in SKU.objects.filter(id__in=sku_ids, deleted=False).prefetch_related("barcodes"):
                sku_by_id[sku.id] = sku
        sku_by_code = {}
        if sku_codes:
            sku_qs = SKU.objects.filter(sku_code__in=sku_codes, deleted=False)
            if latest and latest.agency_id:
                sku_qs = sku_qs.filter(agency_id=latest.agency_id)
            for sku in sku_qs.prefetch_related("barcodes"):
                sku_by_code.setdefault(sku.sku_code, sku)
        display_items = []
        for item in base_items:
            actual_value = "" if not act_items else item.get("actual_qty")
            planned_value = item.get("planned_qty") if act_items else item.get("qty")
            name = item.get("name")
            size = item.get("size")
            sku_code_value = item.get("sku_code") or ""
            sku_id_value = item.get("sku_id")
            sku_id = None
            if sku_id_value not in (None, ""):
                try:
                    sku_id = int(sku_id_value)
                except (TypeError, ValueError):
                    sku_id = None
            sku = sku_by_id.get(sku_id) or sku_by_code.get(sku_code_value)
            barcode_value = item.get("barcode") or cls._barcode_value_for_sku(sku, size)
            display_items.append(
                {
                    "sku_code": sku_code_value or "-",
                    "barcode": barcode_value,
                    "name": name or "-",
                    "size": size or "-",
                    "planned_qty": planned_value if planned_value not in (None, "") else "-",
                    "actual_qty": actual_value if actual_value is not None else "",
                    "comment": item.get("comment") or "",
                }
            )
        client_label, client_prefix = cls._client_display(latest.agency if latest else None)
        sku_options = []
        sku_name_options = []
        barcode_options = []
        barcode_map = {}
        if latest and latest.agency_id:
            name_seen = set()
            barcode_seen = set()
            for sku in SKU.objects.filter(agency_id=latest.agency_id, deleted=False).prefetch_related("barcodes").order_by("sku_code"):
                barcode_values = []
                for barcode in sku.barcodes.all():
                    value = (barcode.value or "").strip()
                    if not value:
                        continue
                    barcode_values.append(value)
                    if value not in barcode_seen:
                        barcode_seen.add(value)
                        barcode_options.append(value)
                    barcode_map.setdefault(
                        value,
                        {"sku": sku.sku_code, "name": sku.name, "size": (barcode.size or sku.size or "").strip()},
                    )
                sku_options.append(
                    {
                        "code": sku.sku_code,
                        "name": sku.name,
                        "barcodes_joined": "|".join(barcode_values),
                    }
                )
                if sku.name and sku.name not in name_seen:
                    name_seen.add(sku.name)
                    sku_name_options.append(sku.name)
        arrival_value = payload.get("eta_at")
        vehicle_value = payload.get("vehicle_number")
        driver_phone_value = payload.get("driver_phone")
        if act_entry:
            act_payload = act_entry.payload or {}
            arrival_value = act_payload.get("eta_at") or arrival_value
            vehicle_value = act_payload.get("vehicle_number") or vehicle_value
            driver_phone_value = act_payload.get("driver_phone") or driver_phone_value
        arrival_input = ""
        if arrival_value:
            try:
                parsed = datetime.fromisoformat(str(arrival_value))
                if timezone.is_naive(parsed):
                    parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
                arrival_input = timezone.localtime(parsed).strftime("%Y-%m-%dT%H:%M")
            except (TypeError, ValueError):
                arrival_input = ""
        status_audience = "client" if client_view else ("storekeeper" if role == "storekeeper" else "default")
        receiving_status_label = WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=str(order_id or ""),
            agency=latest.agency if latest else client_agency,
            payload=payload,
        ).label_for(status_audience)
        resolved_cabinet_url = cabinet_url
        if client_view and client_agency:
            resolved_cabinet_url = f"/client/dashboard/?client={client_agency.id}"
        return {
            "order_id": order_id,
            "order_title": order_title,
            "client_label": client_label,
            "client_prefix": client_prefix,
            "status_label": receiving_status_label,
            "cabinet_url": resolved_cabinet_url,
            "client_view": client_view,
            "client_param": client_agency.id if client_agency else "",
            "arrival_at": _format_datetime_value(arrival_value),
            "arrival_value": _format_datetime_value(arrival_value),
            "arrival_input": arrival_input,
            "vehicle_number": vehicle_value or "-",
            "vehicle_value": vehicle_value or "",
            "driver_phone": _format_payload_value(driver_phone_value),
            "can_submit": can_submit,
            "can_add_items": can_submit,
            "act_exists": bool(act_entry),
            "act_label": act_label,
            "act_documents": act_documents,
            "items": display_items,
            "sku_options": sku_options,
            "sku_name_options": sku_name_options,
            "barcode_options": barcode_options,
            "barcode_map": barcode_map,
            "agency_id": latest.agency_id if latest else "",
        }

    @classmethod
    def build_receiving_placement_page_context(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
    ) -> dict:
        latest = entries[-1] if entries else None
        status_entry = cls._current_status_entry(entries)
        status_payload = status_entry.payload or {} if status_entry else {}
        receiving_act = cls._find_act_entry(entries, "receiving", "акт приемки")
        placement_act = cls._find_act_entry(entries, "placement", "акт размещения")
        receiving_items = (receiving_act.payload or {}).get("act_items") if receiving_act else []
        placement_items = (placement_act.payload or {}).get("act_items") if placement_act else []
        display_items = []
        for item in placement_items or receiving_items:
            display_items.append(
                {
                    "sku_code": item.get("sku_code") or "",
                    "name": item.get("name") or "-",
                    "size": item.get("size") or "-",
                    "actual_qty": item.get("actual_qty") or 0,
                    "box_qty": item.get("box_qty") or 0,
                    "pallet_qty": item.get("pallet_qty") or 0,
                }
            )
        catalog_items = []
        remaining_items = []
        for item in receiving_items:
            catalog_items.append(
                {
                    "sku_code": item.get("sku_code") or "",
                    "name": item.get("name") or "",
                    "size": item.get("size") or "",
                }
            )
            remaining_items.append(
                {
                    "sku_code": item.get("sku_code") or "",
                    "name": item.get("name") or "",
                    "size": item.get("size") or "",
                    "actual_qty": item.get("actual_qty") or 0,
                }
            )
        barcode_map = {}
        if latest and latest.agency_id and receiving_items:
            sku_codes = {
                (item.get("sku_code") or "").strip()
                for item in receiving_items
                if (item.get("sku_code") or "").strip()
            }
            if sku_codes:
                for sku in SKU.objects.filter(
                    agency_id=latest.agency_id,
                    sku_code__in=sku_codes,
                    deleted=False,
                ).prefetch_related("barcodes"):
                    for barcode in sku.barcodes.all():
                        value = (barcode.value or "").strip()
                        if not value:
                            continue
                        barcode_map.setdefault(
                            value,
                            {
                                "sku": sku.sku_code,
                                "name": sku.name,
                                "size": (barcode.size or sku.size or "").strip(),
                            },
                        )
                    sku_code_barcode = (sku.code or "").strip()
                    if sku_code_barcode:
                        barcode_map.setdefault(
                            sku_code_barcode,
                            {
                                "sku": sku.sku_code,
                                "name": sku.name,
                                "size": (sku.size or "").strip(),
                            },
                        )
        client_label, _client_prefix = cls._client_display(latest.agency if latest else None)
        can_submit = role == "storekeeper"
        act_state = (placement_act.payload or {}).get("act_state") or "closed" if placement_act else "open"
        signed_by_storekeeper = cls._act_storekeeper_signed(entries)
        receiving_result = WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=str(order_id or ""),
            agency=latest.agency if latest else None,
            payload=status_payload,
        ) if latest else None
        can_open_act = WarehouseActionPolicy.can_open_receiving_placement(
            receiving_result,
            role=role,
            act_state=act_state,
            signed_by_storekeeper=signed_by_storekeeper,
        ).allowed
        boxes_data = (placement_act.payload or {}).get("act_boxes") if placement_act else []
        pallets_data = (placement_act.payload or {}).get("act_pallets") if placement_act else []
        occupied_cells = StockAvailabilityService.occupied_os_cells(
            exclude_order_type="receiving",
            exclude_order_id=order_id,
            include_agency=True,
        )
        audience = "storekeeper" if role == "storekeeper" else "default"
        status_label = receiving_result.label_for(audience) if receiving_result else "-"
        return {
            "order_id": order_id,
            "client_label": client_label,
            "status_label": status_label,
            "goods_type": (status_payload.get("goods_type") or "").strip().lower(),
            "order_detail_url": f"/orders/receiving/{order_id}/",
            "can_submit": can_submit,
            "can_open_act": can_open_act,
            "signed_by_storekeeper": signed_by_storekeeper,
            "act_exists": bool(placement_act),
            "act_state": act_state,
            "items": display_items,
            "catalog_items": catalog_items,
            "remaining_items": remaining_items,
            "barcode_map": barcode_map,
            "boxes_data": boxes_data,
            "pallets_data": pallets_data,
            "occupied_cells": occupied_cells,
            "current_agency_id": latest.agency_id if latest else 0,
        }

    @classmethod
    def log_receiving_flow_box_action(
        cls,
        *,
        order_id: str,
        action: str,
        payload: dict | None,
        user=None,
    ) -> ReceivingFlowBoxActionResult:
        action_key = str(action or "").strip().lower()
        action_kind, action_label = cls.FLOW_BOX_ACTIONS.get(action_key, ("", ""))
        if not action_label:
            return ReceivingFlowBoxActionResult(status="invalid_action", reason="invalid_action")
        source = payload if isinstance(payload, dict) else {}
        box_code = (source.get("box_code") or "").strip()
        box_codes = [
            str(value).strip()
            for value in (source.get("box_codes") or [])
            if str(value or "").strip()
        ]
        if box_code and box_code not in box_codes:
            box_codes.insert(0, box_code)
        if action_key in {"edit", "delete"} and not box_code:
            return ReceivingFlowBoxActionResult(status="missing_box", reason="missing_box")
        if action_key in {"delete_batch", "move_batch", "print_batch"} and not box_codes:
            return ReceivingFlowBoxActionResult(status="missing_boxes", reason="missing_boxes")
        snapshot = {
            "order_id": str(order_id or ""),
            "box_code": box_code,
            "box_codes": box_codes,
            "box_count": len(box_codes) if box_codes else (1 if box_code else 0),
            "pallet_code": (source.get("pallet_code") or "").strip(),
            "pallet_index": cls._parse_int_value(source.get("pallet_index")),
            "box_index": cls._parse_int_value(source.get("box_index")),
            "total_qty": cls._parse_int_value(source.get("total_qty")),
            "items": source.get("items") if isinstance(source.get("items"), list) else [],
            "previous_active_box": (source.get("previous_active_box") or "").strip(),
            "new_active_box": (source.get("new_active_box") or "").strip(),
            "source_pallet_code": (source.get("source_pallet_code") or "").strip(),
            "source_pallet_index": cls._parse_int_value(source.get("source_pallet_index")),
            "target_pallet_code": (source.get("target_pallet_code") or "").strip(),
            "target_pallet_index": cls._parse_int_value(source.get("target_pallet_index")),
            "create_new_pallet": bool(source.get("create_new_pallet")),
            "boxes": source.get("boxes") if isinstance(source.get("boxes"), list) else [],
        }
        from audit.models import OrderAuditEntry

        entry = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
            .select_related("agency")
            .order_by("-created_at")
            .first()
        )
        if action_key in {"edit", "delete"}:
            description = f"{action_label} {box_code} (заявка {order_id})"
            if snapshot["pallet_index"] and snapshot["box_index"]:
                description += f", палета {snapshot['pallet_index']}, короб {snapshot['box_index']}"
        else:
            description = f"{action_label} ({snapshot['box_count']} шт., заявка {order_id})"
            if snapshot["source_pallet_index"]:
                description += f", палета {snapshot['source_pallet_index']}"
            if snapshot["target_pallet_index"]:
                description += f" -> палета {snapshot['target_pallet_index']}"
        log_staff_overaction(
            action_kind,
            user=cls._authenticated_user(user),
            agency=entry.agency if entry else None,
            description=description,
            snapshot=snapshot,
        )
        return ReceivingFlowBoxActionResult(
            status="logged",
            action_kind=action_kind,
            action_label=action_label,
            snapshot=snapshot,
        )

    @classmethod
    def parse_receiving_warehouse_destinations(
        cls,
        raw,
    ) -> tuple[dict[str, dict] | None, str]:
        return parse_putaway_destinations(
            raw,
            allowed_zones={"PR", "MR", "OS"},
            allowed_zones_label="PR, MR или OS",
        )

    @classmethod
    def create_receiving_warehouse_moves(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        user=None,
        destinations_by_pallet: dict[str, dict] | None = None,
    ) -> ReceivingWarehouseMoveResult:
        order_key = str(order_id or "").strip()
        if not order_key or not entries:
            return ReceivingWarehouseMoveResult(status="missing")
        placement_entry = cls._find_act_entry(entries, "placement", "акт размещения")
        placement_payload = placement_entry.payload if placement_entry else {}
        if not isinstance(placement_payload, dict):
            placement_payload = {}
        if (placement_payload.get("act_state") or "closed").lower() != "closed":
            return ReceivingWarehouseMoveResult(status="missing")
        normalized_destinations: dict[str, dict] = {}
        if isinstance(destinations_by_pallet, dict):
            for raw_pallet_code, raw_destination in destinations_by_pallet.items():
                pallet_code = str(raw_pallet_code or "").strip()
                if not pallet_code:
                    continue
                normalized_destinations[pallet_code] = normalize_putaway_location(raw_destination)
        latest = entries[-1]
        actor_name = cls._resolve_actor_name(user)
        command_role = role if role in {"storekeeper", "manager", "head_manager", "director", "admin"} else "storekeeper"
        command_result = WarehouseCommandService.create_receiving_putaway_tasks(
            order_id=order_key,
            agency=latest.agency if latest else None,
            role=command_role,
            placement_payload=placement_payload,
            requested_by=cls._authenticated_user(user),
            requested_by_name=actor_name,
            requested_by_role=role or command_role,
            destinations_by_pallet=normalized_destinations,
            latest_moves_by_pallet=cls._latest_receiving_moves_by_pallet(order_key),
            flow_closed=True,
            not_created_count=1,
        )
        return ReceivingWarehouseMoveResult(
            status=command_result.status,
            reason=command_result.reason,
            created_count=int(command_result.created_count or 0),
            skipped_existing_count=int(command_result.skipped_existing_count or 0),
            skipped_missing_destination_count=int(command_result.skipped_missing_destination_count or 0),
            total_count=int(command_result.total_count or 0),
            destinations_by_pallet=normalized_destinations,
            source_facts=list(command_result.source_facts or []),
        )

    @classmethod
    def _latest_receiving_moves_by_pallet(cls, order_id: str) -> dict[str, dict]:
        target_id = str(order_id or "").strip()
        latest: dict[str, dict] = {}
        if not target_id:
            return latest
        from audit.models import OrderAuditEntry

        entries = OrderAuditEntry.objects.filter(order_type="stock_move").order_by("-created_at")
        for entry in entries:
            payload = entry.payload or {}
            if str(payload.get("receiving_order_id") or "").strip() != target_id:
                continue
            if str(payload.get("processing_order_id") or "").strip():
                continue
            pallet_code = str(payload.get("pallet_code") or "").strip()
            if not pallet_code or pallet_code in latest:
                continue
            latest[pallet_code] = {
                "status": str(payload.get("status") or payload.get("submit_action") or "").strip().lower(),
                "status_label": str(payload.get("status_label") or "").strip(),
                "order_id": str(entry.order_id or "").strip(),
                "to_zone": payload.get("to_zone"),
                "to_row": payload.get("to_row"),
                "to_section": payload.get("to_section"),
                "to_tier": payload.get("to_tier"),
                "to_cell": payload.get("to_cell"),
            }
        return latest

    @classmethod
    def suggest_receiving_destinations(
        cls,
        pallets,
        *,
        exclude_order_type: str,
        exclude_order_id: str,
        agency_id: int | None = None,
        row_sections: dict | None = None,
        tiers: list | tuple | None = None,
        cells_per_tier: int = 0,
    ) -> dict[str, dict]:
        return suggest_putaway_destinations(
            pallets,
            exclude_order_type=exclude_order_type,
            exclude_order_id=exclude_order_id,
            agency_id=agency_id,
            row_sections=row_sections,
            tiers=tiers,
            cells_per_tier=cells_per_tier,
        )

    @classmethod
    def build_receiving_warehouse_move_progress(cls, order_id: str, placement_pallets) -> dict:
        order_key = str(order_id or "").strip()
        pallets = placement_pallets if isinstance(placement_pallets, list) else []
        pallet_codes = []
        seen = set()
        for pallet in pallets:
            if not isinstance(pallet, dict):
                continue
            code = str(pallet.get("code") or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            pallet_codes.append(code)
        latest_moves = cls._latest_receiving_moves_by_pallet(order_key)
        created_count = 0
        in_progress_count = 0
        done_count = 0
        canceled_count = 0
        other_count = 0
        not_created_count = 0
        for code in pallet_codes:
            status = str((latest_moves.get(code) or {}).get("status") or "").strip().lower()
            if status == "done":
                done_count += 1
            elif status == "created":
                created_count += 1
            elif status == "in_progress":
                in_progress_count += 1
            elif status in {"canceled", "cancelled"}:
                canceled_count += 1
                not_created_count += 1
            elif status:
                other_count += 1
            else:
                not_created_count += 1
        total_pallets = len(pallet_codes)
        active_count = created_count + in_progress_count
        return {
            "total_pallets": total_pallets,
            "created_count": created_count,
            "in_progress_count": in_progress_count,
            "active_count": active_count,
            "done_count": done_count,
            "canceled_count": canceled_count,
            "other_count": other_count,
            "not_created_count": not_created_count,
            "has_any_task": bool(active_count or done_count or canceled_count or other_count),
            "all_done": bool(total_pallets) and done_count >= total_pallets,
        }

    @classmethod
    def build_receiving_warehouse_move_rows(
        cls,
        order_id: str,
        placement_pallets,
        *,
        agency_id: int | None = None,
        row_sections: dict | None = None,
        tiers: list | tuple | None = None,
        cells_per_tier: int = 0,
    ) -> list[dict]:
        order_key = str(order_id or "").strip()
        pallets = placement_pallets if isinstance(placement_pallets, list) else []
        latest_moves = cls._latest_receiving_moves_by_pallet(order_key)
        suggested = cls.suggest_receiving_destinations(
            pallets,
            exclude_order_type="receiving",
            exclude_order_id=order_key,
            agency_id=agency_id,
            row_sections=row_sections,
            tiers=tiers,
            cells_per_tier=cells_per_tier,
        )
        return build_putaway_rows(
            pallets,
            latest_moves_by_pallet=latest_moves,
            suggested_destinations=suggested,
            source_label="PR · Зона приемки",
        )

    @classmethod
    def build_receiving_warehouse_move_panel(
        cls,
        *,
        order_id: str,
        placement_pallets,
        receiving_result,
        flow_locked: bool,
        role_allowed: bool,
        agency_id: int | None = None,
        row_sections: dict | None = None,
        tiers: list | tuple | None = None,
        cells_per_tier: int = 0,
    ) -> ReceivingWarehouseMovePanelResult:
        progress = cls.build_receiving_warehouse_move_progress(order_id, placement_pallets)
        rows = cls.build_receiving_warehouse_move_rows(
            order_id,
            placement_pallets,
            agency_id=agency_id,
            row_sections=row_sections,
            tiers=tiers,
            cells_per_tier=cells_per_tier,
        )
        can_send = WarehouseActionPolicy.can_send_receiving_to_storage(
            receiving_result,
            flow_closed=flow_locked,
            role_allowed=role_allowed,
            not_created_count=int(progress.get("not_created_count") or 0),
        ).allowed
        return ReceivingWarehouseMovePanelResult(
            progress=progress,
            rows=rows,
            can_send=can_send,
        )

    @classmethod
    def send_receiving_to_storage(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        user=None,
        destinations_raw=None,
    ) -> ReceivingWarehouseMoveResult:
        order_key = str(order_id or "").strip()
        if not order_key or not entries:
            return ReceivingWarehouseMoveResult(status="missing")
        status_entry = cls._current_status_entry(entries)
        latest = entries[-1]
        command_result = WarehouseCommandService.send_receiving_to_storage(
            order_id=order_key,
            agency=latest.agency if latest else None,
            role=role or "",
            status_payload=(status_entry.payload or {}) if status_entry else {},
            flow_closed=cls._flow_closed(entries),
            not_created_count=1,
        )
        if command_result.status == "denied":
            return ReceivingWarehouseMoveResult(
                status="not_ready",
                reason=command_result.reason,
                source_facts=list(command_result.source_facts or []),
            )
        destinations_by_pallet, destination_error = cls.parse_receiving_warehouse_destinations(destinations_raw)
        if destination_error:
            return ReceivingWarehouseMoveResult(
                status="invalid_destination",
                error_message=destination_error,
                source_facts=list(command_result.source_facts or []),
            )
        move_result = cls.create_receiving_warehouse_moves(
            order_id=order_key,
            entries=entries,
            role=role or "",
            user=user,
            destinations_by_pallet=destinations_by_pallet,
        )
        if move_result.created_count > 0:
            move_result.status = "ok"
            return move_result
        if (
            move_result.skipped_missing_destination_count > 0
            and move_result.skipped_existing_count <= 0
        ):
            move_result.status = "missing_destination"
            return move_result
        if move_result.total_count > 0 and move_result.skipped_existing_count >= move_result.total_count:
            move_result.status = "exists"
            return move_result
        if move_result.skipped_existing_count > 0 or move_result.skipped_missing_destination_count > 0:
            move_result.status = "blocked"
            return move_result
        move_result.status = "none"
        return move_result

    @classmethod
    def _find_act_entry(cls, entries, act_type: str, label_hint: str):
        for entry in reversed(entries or []):
            if (entry.payload or {}).get("act") == act_type:
                return entry
        for entry in reversed(entries or []):
            label = ((entry.payload or {}).get("act_label") or "").lower()
            if label_hint in label:
                return entry
        return None

    @staticmethod
    def _is_done_status(entry) -> bool:
        if not entry:
            return False
        payload = entry.payload or {}
        status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
        status_label = (payload.get("status_label") or "").lower()
        if status_value in {"done", "completed", "closed", "finished"}:
            return True
        return "выполн" in status_label

    @staticmethod
    def _act_storekeeper_signed_from_payload(payload: dict) -> bool:
        return bool((payload or {}).get("act_storekeeper_signed"))

    @staticmethod
    def _act_manager_signed_from_payload(payload: dict) -> bool:
        return bool((payload or {}).get("act_manager_signed"))

    @classmethod
    def _act_storekeeper_signed(cls, entries) -> bool:
        act_entry = cls._find_act_entry(entries, "receiving", "акт приемки")
        if not act_entry:
            return False
        return cls._act_storekeeper_signed_from_payload(act_entry.payload or {})

    @classmethod
    def _placement_closed(cls, entries) -> bool:
        placement_entry = cls._find_act_entry(entries, "placement", "акт размещения")
        if not placement_entry:
            return False
        state = ((placement_entry.payload or {}).get("act_state") or "closed").lower()
        return state == "closed"

    @classmethod
    def _create_manager_followup_task(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        user=None,
        submitted_at=None,
        observer=None,
    ) -> bool:
        if not agency:
            return False
        manager = (
            Employee.objects.filter(role="manager", is_active=True)
            .order_by("full_name")
            .first()
        )
        if not manager:
            return False
        title = f"Проверьте размещение по заявке на приемку товара №{order_id}"
        existing = Task.objects.filter(
            route=f"/orders/receiving/{order_id}/",
            assigned_to=manager,
            title=title,
        ).exclude(status="done")
        if existing.exists():
            return False
        due_at = submitted_at if submitted_at is not None else timezone.localtime()
        description = f"Клиент: {agency.agn_name or agency.inn or agency.id}"
        Task.objects.create(
            title=title,
            description=description,
            route=f"/orders/receiving/{order_id}/",
            assigned_to=manager,
            observer=observer,
            created_by=cls._authenticated_user(user),
            due_date=_manager_due_date(due_at),
        )
        return True

    @classmethod
    def _create_manager_review_task(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        user=None,
        submitted_at=None,
    ) -> bool:
        if not agency:
            return False
        manager = (
            Employee.objects.filter(role="manager", is_active=True)
            .order_by("full_name")
            .first()
        )
        if not manager:
            return False
        due_at = submitted_at if submitted_at is not None else timezone.localtime()
        description = f"Клиент: {agency.agn_name or agency.inn or agency.id}"
        Task.objects.create(
            title=f"Подтвердите заявку на приемку товара №{order_id}",
            description=description,
            route=f"/orders/receiving/{order_id}/",
            assigned_to=manager,
            created_by=cls._authenticated_user(user),
            due_date=_manager_due_date(due_at),
        )
        return True

    @classmethod
    def _create_storekeeper_task(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        user=None,
        submitted_at=None,
        observer=None,
    ) -> bool:
        if not agency:
            return False
        storekeeper = (
            Employee.objects.filter(role="storekeeper", is_active=True)
            .order_by("full_name")
            .first()
        )
        if not storekeeper:
            return False
        due_at = submitted_at if submitted_at is not None else timezone.localtime()
        description = f"Клиент: {agency.agn_name or agency.inn or agency.id}"
        Task.objects.create(
            title=f"Принять заявку на приемку товара №{order_id}",
            description=description,
            route=f"/orders/receiving/{order_id}/",
            assigned_to=storekeeper,
            observer=observer,
            created_by=cls._authenticated_user(user),
            due_date=due_at + timedelta(days=1),
        )
        return True

    @classmethod
    def submit_receiving_for_review(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        user=None,
        submitted_at=None,
    ) -> ReceivingDispatchResult:
        created = cls._create_manager_review_task(
            order_id=str(order_id or ""),
            agency=agency,
            user=user,
            submitted_at=submitted_at if submitted_at is not None else timezone.localtime(),
        )
        return ReceivingDispatchResult(
            payload={},
            manager_task_created=created,
        )

    @classmethod
    def start_receiving_work(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        user=None,
    ) -> ReceivingActionResult:
        if not order_id or not entries:
            return ReceivingActionResult(status="missing")
        status_entry = cls._current_status_entry(entries)
        latest = entries[-1]
        status_payload = (status_entry.payload or {}) if status_entry else {}
        command_result = WarehouseCommandService.start_receiving_flow(
            order_id=str(order_id or ""),
            agency=latest.agency if latest else None,
            role=role or "",
            status_payload=status_payload,
            flow_closed=cls._flow_closed(entries),
        )
        if command_result.status != "started":
            return ReceivingActionResult(
                status=command_result.status,
                payload=dict(command_result.payload_update or {}),
                reason=command_result.reason,
                meta=dict(command_result.meta or {}),
            )
        payload = dict(status_payload)
        payload.update(command_result.payload_update)
        employee = cls._resolve_storekeeper(user)
        if employee:
            payload["storekeeper_employee_id"] = employee.id
            payload["storekeeper_name"] = employee.full_name or ""
        log_order_action(
            "status",
            order_id=order_id,
            order_type="receiving",
            user=cls._authenticated_user(user),
            agency=latest.agency if latest else None,
            description="Заявка взята в работу кладовщиком",
            payload=payload,
        )
        return ReceivingActionResult(
            status="started",
            payload=payload,
        )

    @classmethod
    def configure_receiving_act(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        goods_type: str,
        receiving_mode: str,
        user=None,
    ) -> ReceivingStatusUpdateResult:
        normalized_goods_type = str(goods_type or "").strip().lower()
        normalized_mode = str(receiving_mode or "").strip().lower()
        if role != "storekeeper" or normalized_goods_type not in cls.RECEIVING_GOODS_TYPE_LABELS:
            return ReceivingStatusUpdateResult(applied=False, payload={})
        status_entry = cls._current_status_entry(entries)
        payload = dict(status_entry.payload or {}) if status_entry else {}
        existing_type = (payload.get("goods_type") or "").strip().lower()
        payload_changed = False
        description = ""
        if not existing_type:
            payload["goods_type"] = normalized_goods_type
            payload["goods_type_label"] = cls.RECEIVING_GOODS_TYPE_LABELS[normalized_goods_type]
            payload_changed = True
            description = f"Тип товара: {cls.RECEIVING_GOODS_TYPE_LABELS[normalized_goods_type]}"
        if normalized_mode in cls.RECEIVING_MODES and payload.get("receiving_mode") != normalized_mode:
            payload["receiving_mode"] = normalized_mode
            payload_changed = True
            if existing_type:
                description = "Обновлен режим приемки"
        if not payload_changed:
            return ReceivingStatusUpdateResult(applied=False, payload=payload)
        latest = entries[-1] if entries else None
        log_order_action(
            "status",
            order_id=order_id,
            order_type="receiving",
            user=cls._authenticated_user(user),
            agency=latest.agency if latest else None,
            description=description,
            payload=payload,
        )
        return ReceivingStatusUpdateResult(
            applied=True,
            payload=payload,
            description=description,
        )

    @classmethod
    def reopen_receiving_flow(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        user=None,
    ) -> ReceivingActionResult:
        if not order_id or not entries:
            return ReceivingActionResult(status="missing")
        latest = entries[-1] if entries else None
        closed_entry = next(
            (entry for entry in reversed(entries) if (entry.payload or {}).get("flow_closed")),
            None,
        )
        closed_payload = closed_entry.payload if closed_entry else {}
        command_result = WarehouseCommandService.reopen_receiving_flow(
            order_id=str(order_id or ""),
            agency=latest.agency if latest else None,
            role=role or "",
            status_payload=(closed_payload if isinstance(closed_payload, dict) else {}),
            flow_closed=cls._flow_closed(entries),
            flow_closed_at=(closed_payload or {}).get("flow_closed_at"),
        )
        if command_result.status != "reopened":
            return ReceivingActionResult(
                status=command_result.status,
                payload=dict(command_result.payload_update or {}),
                reason=command_result.reason,
                meta=dict(command_result.meta or {}),
            )
        actor = cls._authenticated_user(user)
        log_staff_overaction(
            "update",
            user=actor,
            agency=latest.agency if latest else None,
            description=f"Избыточное действие: повторное открытие приемки потоком (заявка {order_id})",
            snapshot=command_result.meta,
        )
        log_order_action(
            "update",
            order_id=order_id,
            order_type="receiving",
            user=actor,
            agency=latest.agency if latest else None,
            description="Повторное открытие приемки потоком",
            payload=command_result.payload_update,
        )
        return ReceivingActionResult(
            status="reopened",
            payload=dict(command_result.payload_update or {}),
            meta=dict(command_result.meta or {}),
        )

    @classmethod
    def submit_receiving_order(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        payload: dict,
        submit_action: str,
        user=None,
        submitted_at=None,
        existing_order_id: str = "",
        old_payload: dict | None = None,
        dispatch_review: bool = False,
    ) -> ReceivingSubmissionResult:
        actor = cls._authenticated_user(user)
        order_key = str(existing_order_id or order_id or "").strip()
        status_value = str((payload or {}).get("status") or "").strip()
        status_label = str((payload or {}).get("status_label") or "").strip()
        review_dispatched = False
        if existing_order_id:
            changes = _describe_payload_changes(old_payload or {}, payload or {})
            if changes:
                description = f"Исправление заявки №{order_key}: " + "; ".join(changes)
            else:
                description = f"Исправление заявки №{order_key}: без изменений"
            if submit_action == "send":
                description = f"Отправлено менеджеру. {description}"
            log_order_action(
                "update",
                order_id=order_key,
                order_type="receiving",
                user=actor,
                agency=agency,
                description=description,
                payload=payload,
            )
            if submit_action == "send" and dispatch_review:
                cls.submit_receiving_for_review(
                    order_id=order_key,
                    agency=agency,
                    user=actor,
                    submitted_at=submitted_at if submitted_at is not None else timezone.localtime(),
                )
                review_dispatched = True
            return ReceivingSubmissionResult(
                order_id=order_key,
                payload=dict(payload or {}),
                status_value=status_value,
                status_label=status_label,
                was_update=True,
                review_dispatched=review_dispatched,
            )

        action_label = "черновик" if submit_action == "draft" else "заявка"
        log_order_action(
            "create",
            order_id=order_key,
            order_type="receiving",
            user=actor,
            agency=agency,
            description=f"Заявка на приемку №{order_key} ({action_label})",
            payload=payload,
        )
        if submit_action != "draft" and dispatch_review:
            cls.submit_receiving_for_review(
                order_id=order_key,
                agency=agency,
                user=actor,
                submitted_at=submitted_at if submitted_at is not None else timezone.localtime(),
            )
            review_dispatched = True
        return ReceivingSubmissionResult(
            order_id=order_key,
            payload=dict(payload or {}),
            status_value=status_value,
            status_label=status_label,
            was_update=False,
            review_dispatched=review_dispatched,
        )

    @classmethod
    def send_receiving_act_to_client(
        cls,
        *,
        order_id: str,
        entries,
        user=None,
    ) -> ReceivingStatusUpdateResult:
        if not entries:
            return ReceivingStatusUpdateResult(applied=False, payload={})
        receiving_entry = cls._find_act_entry(entries, "receiving", "акт приемки")
        placement_entry = cls._find_act_entry(entries, "placement", "акт размещения")
        if not receiving_entry or not placement_entry:
            return ReceivingStatusUpdateResult(applied=False, payload={})
        if not cls._placement_closed(entries):
            return ReceivingStatusUpdateResult(applied=False, payload={})
        receiving_payload = receiving_entry.payload or {}
        if not cls._act_storekeeper_signed_from_payload(receiving_payload):
            return ReceivingStatusUpdateResult(applied=False, payload={})
        if not cls._act_manager_signed_from_payload(receiving_payload):
            return ReceivingStatusUpdateResult(applied=False, payload={})
        status_entry = cls._current_status_entry(entries)
        if cls._is_done_status(status_entry):
            return ReceivingStatusUpdateResult(applied=False, payload={})
        actor = cls._authenticated_user(user)
        latest = entries[-1]
        agency = next((entry.agency for entry in reversed(entries) if entry.agency), None) or latest.agency
        payload = dict(status_entry.payload or {}) if status_entry else {}
        payload["status"] = "done"
        payload["status_label"] = "Выполнена"
        act_label = (receiving_entry.payload or {}).get("act_label") or "Акт приемки"
        payload["act_sent"] = act_label
        payload["act_sent_at"] = timezone.localtime().isoformat()
        if "act_viewed" not in payload:
            payload["act_viewed"] = False
        log_order_action(
            "status",
            order_id=order_id,
            order_type="receiving",
            user=actor,
            agency=agency,
            description="Акт отправлен клиенту",
            payload=payload,
        )
        log_order_action(
            "update",
            order_id=order_id,
            order_type="receiving",
            user=actor,
            agency=agency,
            description=f"{act_label} отправлен клиенту",
            payload={"message": act_label},
        )
        closed_count = cls._close_manager_tasks(str(order_id or ""))
        return ReceivingStatusUpdateResult(
            applied=True,
            payload=payload,
            description="Акт отправлен клиенту",
            manager_tasks_closed=closed_count,
        )

    @classmethod
    def open_receiving_placement(
        cls,
        *,
        order_id: str,
        entries,
        role: str,
        user=None,
    ) -> ReceivingActionResult:
        if not order_id or not entries:
            return ReceivingActionResult(status="missing")
        placement_entry = cls._find_act_entry(entries, "placement", "акт размещения")
        if not placement_entry:
            return ReceivingActionResult(status="missing")
        payload = placement_entry.payload or {}
        current_state = (payload.get("act_state") or "closed").lower()
        status_entry = cls._current_status_entry(entries)
        command_result = WarehouseCommandService.open_receiving_placement(
            order_id=str(order_id or ""),
            agency=entries[-1].agency if entries else None,
            role=role or "",
            act_state=current_state,
            signed_by_storekeeper=cls._act_storekeeper_signed(entries),
            status_payload=(status_entry.payload or {}) if status_entry else {},
        )
        if command_result.status != "opened":
            return ReceivingActionResult(
                status=command_result.status,
                payload=dict(command_result.payload_update or {}),
                reason=command_result.reason,
                meta=dict(command_result.meta or {}),
            )
        act_payload = dict(payload)
        act_payload.update(command_result.payload_update)
        if payload.get("act_label") and not command_result.payload_update.get("act_label"):
            act_payload["act_label"] = payload.get("act_label")
        latest = entries[-1]
        log_order_action(
            "status",
            order_id=order_id,
            order_type="receiving",
            user=cls._authenticated_user(user),
            agency=latest.agency,
            description="Открыт акт размещения",
            payload=act_payload,
        )
        return ReceivingActionResult(
            status="opened",
            payload=act_payload,
        )

    @classmethod
    def confirm_receiving_to_warehouse(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        status_payload: dict | None,
        user=None,
        submitted_at=None,
    ) -> ReceivingDispatchResult:
        actor = cls._authenticated_user(user)
        observer = cls._resolve_observer(user)
        payload = dict(status_payload or {})
        payload["status"] = "warehouse"
        payload["status_label"] = "В ожидании поставки товара"
        log_order_action(
            "status",
            order_id=order_id,
            order_type="receiving",
            user=actor,
            agency=agency,
            description="Подтверждено и отправлено на склад",
            payload=payload,
        )
        closed_count = cls._close_manager_tasks(str(order_id or ""))
        storekeeper_created = cls._create_storekeeper_task(
            order_id=str(order_id or ""),
            agency=agency,
            user=actor,
            submitted_at=submitted_at if submitted_at is not None else timezone.localtime(),
            observer=observer,
        )
        return ReceivingDispatchResult(
            payload=payload,
            manager_tasks_closed=closed_count,
            storekeeper_task_created=storekeeper_created,
        )

    @classmethod
    def complete_receiving_flow(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        status_payload: dict | None,
        has_mismatch: bool,
        receiving_mode: str,
        act_items: list[dict],
        placement_items: list[dict],
        boxes: list[dict],
        pallets: list[dict],
        flow_state: dict,
        act_units: list[dict] | None = None,
        eta_at: str = "",
        vehicle_number: str = "",
        has_closed_placement_act: bool = False,
        user=None,
        submitted_at=None,
    ) -> ReceivingWorkflowResult:
        actor = cls._authenticated_user(user)
        observer = cls._resolve_observer(user)
        command_result = WarehouseCommandService.complete_receiving_flow(
            order_id=order_id,
            agency=agency,
            status_payload=status_payload,
            has_mismatch=has_mismatch,
            receiving_mode=receiving_mode,
            act_items=act_items,
            placement_items=placement_items,
            boxes=boxes,
            pallets=pallets,
            flow_state=flow_state,
            act_units=act_units,
            eta_at=eta_at,
            vehicle_number=vehicle_number,
            has_closed_placement_act=has_closed_placement_act,
            performed_by=actor,
        )
        act_payload = dict(command_result.payload_update)
        placement_payload = dict(command_result.meta.get("placement_payload") or {})
        placement_previously_closed = bool(command_result.meta.get("placement_previously_closed"))

        log_order_action(
            "status",
            order_id=order_id,
            order_type="receiving",
            user=actor,
            agency=agency,
            description="Создан акт приемки",
            payload=act_payload,
        )
        log_order_action(
            "update",
            order_id=order_id,
            order_type="receiving",
            user=actor,
            agency=agency,
            description="Обновлен акт размещения" if placement_previously_closed else "Создан акт размещения",
            payload=placement_payload,
        )

        closed_count = 0
        if not placement_previously_closed:
            closed_count = cls._close_storekeeper_tasks(str(order_id or ""))
        created_followup = cls._create_manager_followup_task(
            order_id=str(order_id or ""),
            agency=agency,
            user=actor,
            submitted_at=submitted_at if submitted_at is not None else timezone.localtime(),
            observer=observer,
        )
        return ReceivingWorkflowResult(
            act_payload=act_payload,
            placement_payload=placement_payload,
            placement_previously_closed=placement_previously_closed,
            storekeeper_tasks_closed=closed_count,
            manager_followup_created=created_followup,
        )

    @classmethod
    def close_receiving_placement(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        status_payload: dict | None,
        placement_items: list[dict],
        boxes: list[dict],
        pallets: list[dict],
        has_closed_act: bool = False,
        user=None,
        submitted_at=None,
    ) -> ReceivingWorkflowResult:
        actor = cls._authenticated_user(user)
        observer = cls._resolve_observer(user)
        command_result = WarehouseCommandService.close_receiving_placement(
            order_id=order_id,
            agency=agency,
            status_payload=status_payload,
            placement_items=placement_items,
            boxes=boxes,
            pallets=pallets,
            has_closed_act=has_closed_act,
            performed_by=actor,
        )
        act_payload = dict(command_result.payload_update)
        placement_previously_closed = bool(command_result.meta.get("placement_previously_closed"))

        log_order_action(
            "status",
            order_id=order_id,
            order_type="receiving",
            user=actor,
            agency=agency,
            description="Обновлен акт размещения" if placement_previously_closed else "Создан акт размещения",
            payload=act_payload,
        )

        closed_count = 0
        if not placement_previously_closed:
            closed_count = cls._close_storekeeper_tasks(str(order_id or ""))
        created_followup = cls._create_manager_followup_task(
            order_id=str(order_id or ""),
            agency=agency,
            user=actor,
            submitted_at=submitted_at if submitted_at is not None else timezone.localtime(),
            observer=observer,
        )
        return ReceivingWorkflowResult(
            act_payload=act_payload,
            placement_payload=act_payload,
            placement_previously_closed=placement_previously_closed,
            storekeeper_tasks_closed=closed_count,
            manager_followup_created=created_followup,
        )
