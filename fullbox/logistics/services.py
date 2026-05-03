from __future__ import annotations

import re
from datetime import datetime, timedelta

from django.contrib import messages
from django.db import DatabaseError
from django.db.models import Prefetch
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone

from agent.models import DeviceAgent
from audit.models import OrderAuditEntry
from employees.access import get_employee_for_user, resolve_cabinet_url
from employees.models import Employee
from fullbox.order_numbers import format_order_number
from head_manager.models import Carrier, OwnCompany
from shipping.models import ShippingOrder
from shipping.packing import _shipping_packing_summary, shipping_packing_slips_data
from sklad.models import WarehouseReserve, WarehouseStockSnapshot
from sklad.services import WarehouseGoodsStateResolver
from sklad.services.warehouse_transitions import WarehouseStateCode
from sklad.services.warehouse_write_path import WarehouseWritePathService
from todo.models import Task

from .models import (
    LogisticsTrip,
    LogisticsTripOrder,
    display_trip_number,
    is_draft_trip_number,
    next_draft_trip_number,
    next_trip_number,
    trip_number_sequence_value,
)


ALLOWED_ROLES = ("logistician", "head_manager", "director", "admin", "developer")
TRIP_READ_ROLES = ALLOWED_ROLES + ("storekeeper",)
PREPARING_STATUSES = [
    ShippingOrder.STATUS_SUBMITTED,
    ShippingOrder.STATUS_RESERVED,
    ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
    ShippingOrder.STATUS_PICKING,
]
READY_STATUSES = [ShippingOrder.STATUS_PACKED]
ACTIVE_TRIP_STATUSES = [
    LogisticsTrip.STATUS_DRAFT,
    LogisticsTrip.STATUS_PLANNED,
    LogisticsTrip.STATUS_LOADING,
    LogisticsTrip.STATUS_DEPARTED,
]
SHIPPING_STATUS_LABELS = {
    ShippingOrder.STATUS_DRAFT: "Черновик клиента",
    ShippingOrder.STATUS_SUBMITTED: "На согласовании менеджера",
    ShippingOrder.STATUS_RESERVED: "Согласована и передана в работу кладовщику",
    ShippingOrder.STATUS_STOREKEEPER_ACCEPTED: "Принята в работу складом",
    ShippingOrder.STATUS_PICKING: "Доставка в OTG",
    ShippingOrder.STATUS_PACKED: "Подготовлена складом",
    ShippingOrder.STATUS_SHIPPED: "Отгружена",
    ShippingOrder.STATUS_PARTIAL: "Отгружена частично",
    ShippingOrder.STATUS_CANCELED: "Отменена",
}
TRIP_STATUS_LABELS = dict(LogisticsTrip.STATUS_CHOICES)
PLATE_LETTER_MAP = str.maketrans(
    {
        "A": "А",
        "B": "В",
        "E": "Е",
        "K": "К",
        "M": "М",
        "H": "Н",
        "O": "О",
        "P": "Р",
        "C": "С",
        "T": "Т",
        "Y": "У",
        "X": "Х",
    }
)
PLATE_REGEX = re.compile(r"^[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}\d{2,3}$")
TRIP_LOADING_AUDIT_TYPE = "logistics_trip"
TRIP_LOADING_AUDIT_ACT = "trip_loading_progress"


def _display_shipping_status(
    order: ShippingOrder,
    *,
    is_in_trip: bool = False,
    is_loaded_for_trip: bool = False,
) -> str:
    trip_status = ""
    if is_loaded_for_trip:
        trip_status = LogisticsTrip.STATUS_DEPARTED
    elif is_in_trip and order.status == ShippingOrder.STATUS_PACKED:
        trip_status = LogisticsTrip.STATUS_PLANNED
    result = WarehouseGoodsStateResolver.resolve_for_shipping_order(order, trip_status=trip_status)
    return result.label_for("logistician")


def _short_agency_name(name: str | None) -> str:
    raw = str(name or "").strip()
    if not raw:
        return "-"
    return re.sub(r"^Индивидуальный предприниматель\b", "ИП", raw, flags=re.IGNORECASE)


def _parse_date(value: str | None):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _normalize_vehicle_number(value: str | None) -> str:
    raw = re.sub(r"[^0-9A-Za-zА-Яа-я]", "", str(value or "").upper()).translate(PLATE_LETTER_MAP)
    if not raw:
        return ""
    if not PLATE_REGEX.match(raw):
        raise ValueError("Укажите номер машины в формате А123ВС77 или А123ВС777.")
    return raw


def _format_vehicle_number_for_plate(value: str | None) -> str:
    raw = str(value or "").strip()
    if len(raw) < 6:
        return raw
    return f"{raw[0]} {raw[1:4]} {raw[4:6]} {raw[6:]}"


def _default_own_company() -> OwnCompany | None:
    return OwnCompany.objects.filter(is_active=True).order_by("-is_default", "name").first()


def _trip_shipper_name() -> str:
    company = _default_own_company()
    if company is None:
        return 'ООО "ФуллБокс"'
    return str(company.short_name or company.name or 'ООО "ФуллБокс"').strip()


def _trip_customer_summary(trip_orders: list[LogisticsTripOrder]) -> str:
    values: list[str] = []
    for item in trip_orders:
        agency = getattr(item.shipping_order, "agency", None)
        candidate = str(getattr(agency, "short_name", "") or getattr(agency, "agn_name", "") or "").strip()
        if candidate:
            values.append(candidate)
    unique = list(dict.fromkeys(values))
    return ", ".join(unique) if unique else "-"


def _trip_consignee_summary(trip_orders: list[LogisticsTripOrder]) -> str:
    values: list[str] = []
    for item in trip_orders:
        order = item.shipping_order
        candidate = str(order.destination_warehouse or order.destination_address or "").strip()
        if candidate:
            values.append(candidate)
    unique = list(dict.fromkeys(values))
    return ", ".join(unique) if unique else "-"


def _carrier_queryset():
    return Carrier.objects.filter(is_active=True).order_by("short_name", "name", "id")


def _resolve_trip_carrier(request) -> Carrier | None:
    carrier_id = _parse_int(request.POST.get("carrier_id"))
    if carrier_id <= 0:
        return None
    carrier = _carrier_queryset().filter(pk=carrier_id).first()
    if carrier is None:
        raise ValueError("Выберите перевозчика из справочника.")
    return carrier


def _normalize_driver_phone(value: str | None) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return ""
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError("Телефон водителя укажите в формате +7 900 000-00-00.")
    return f"+7 {digits[0:3]} {digits[3:6]}-{digits[6:8]}-{digits[8:10]}"


def _first_active_employee_by_roles(*roles: str) -> Employee | None:
    for role in [role for role in roles if role]:
        employee = Employee.objects.filter(role=role, is_active=True).order_by("full_name").first()
        if employee:
            return employee
    return None


def _trip_is_draft(trip: LogisticsTrip) -> bool:
    return trip.status in {LogisticsTrip.STATUS_DRAFT, LogisticsTrip.STATUS_PLANNED} and is_draft_trip_number(trip.number)


def _trip_public_number(trip: LogisticsTrip) -> str:
    if _trip_is_draft(trip):
        return "Черновик"
    return display_trip_number(trip.number)


def _trip_status_label(trip: LogisticsTrip) -> str:
    if _trip_is_draft(trip):
        return "Черновик"
    return TRIP_STATUS_LABELS.get(trip.status, trip.get_status_display())


def _assign_public_trip_number(trip: LogisticsTrip) -> str:
    sequence = trip_number_sequence_value(trip.number)
    if sequence > 0:
        candidate = f"{sequence}_RS"
        conflict = LogisticsTrip.objects.exclude(pk=trip.pk).filter(number=candidate).exists()
        if not conflict:
            return candidate
    return next_trip_number()


def _can_manage_trip(role: str | None) -> bool:
    return role in ALLOWED_ROLES


def _can_edit_trip(role: str | None, trip: LogisticsTrip) -> bool:
    return _can_manage_trip(role) and trip.status in {LogisticsTrip.STATUS_DRAFT, LogisticsTrip.STATUS_PLANNED}


def _can_return_trip(role: str | None, trip: LogisticsTrip) -> bool:
    return role == "storekeeper" and trip.status == LogisticsTrip.STATUS_LOADING


def _trip_route(trip: LogisticsTrip) -> str:
    return f"/logistics/trips/{trip.pk}/"


def _trip_title(trip: LogisticsTrip) -> str:
    return f"Рейс №{_trip_public_number(trip)}"


def _trip_loading_url(trip: LogisticsTrip) -> str:
    return f"/logistics/trips/{trip.pk}/loading/"


def _can_start_loading(role: str | None, trip: LogisticsTrip) -> bool:
    return role == "storekeeper" and trip.status == LogisticsTrip.STATUS_LOADING


def _can_begin_loading(role: str | None, trip: LogisticsTrip) -> bool:
    return role == "storekeeper" and trip.status in {LogisticsTrip.STATUS_PLANNED, LogisticsTrip.STATUS_LOADING}


def _scanner_agents_payload() -> list[dict]:
    try:
        online_threshold = timezone.now() - timedelta(seconds=30)
        payload: list[dict] = []
        for agent in DeviceAgent.objects.all().order_by("-last_seen", "-updated_at"):
            is_online = bool(agent.last_seen and agent.last_seen >= online_threshold)
            meta = agent.meta if isinstance(agent.meta, dict) else {}
            com_status = meta.get("com_status") if isinstance(meta.get("com_status"), dict) else {}
            com_config = meta.get("com") if isinstance(meta.get("com"), dict) else {}
            payload.append(
                {
                    "agent_id": agent.agent_id,
                    "title": agent.name or agent.host or agent.agent_id,
                    "status": "онлайн" if is_online else "нет связи",
                    "is_online": is_online,
                    "host": agent.host,
                    "version": agent.version,
                    "last_seen": agent.last_seen.isoformat() if agent.last_seen else "",
                    "com_port": str(
                        com_status.get("port") or com_config.get("port") or com_config.get("port_name") or ""
                    ).strip(),
                    "com_enabled": bool(
                        com_status.get("enabled") if "enabled" in com_status else com_config.get("enabled")
                    ),
                    "com_connected": bool(com_status.get("connected")),
                    "com_error": str(com_status.get("error") or "").strip(),
                }
            )
        return payload
    except DatabaseError:
        return []


def _normalize_loading_scan(value: str | None) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).upper()


def _trip_loading_entry(trip: LogisticsTrip) -> OrderAuditEntry | None:
    return (
        OrderAuditEntry.objects.filter(
            order_id=str(trip.pk),
            order_type=TRIP_LOADING_AUDIT_TYPE,
            payload__act=TRIP_LOADING_AUDIT_ACT,
        )
        .order_by("-created_at")
        .first()
    )


def _trip_loading_payload(trip: LogisticsTrip) -> dict:
    entry = _trip_loading_entry(trip)
    if not entry or not isinstance(entry.payload, dict):
        return {}
    return dict(entry.payload or {})


def _trip_loading_rows(trip: LogisticsTrip) -> list[dict]:
    trip_orders = list(
        trip.orders.select_related("shipping_order", "shipping_order__agency")
        .order_by("-delivery_sequence", "-loading_sequence", "-id")
    )
    rows: list[dict] = []
    for position, item in enumerate(trip_orders, start=1):
        order = item.shipping_order
        packing_summary = _shipping_packing_summary(order) or {}
        pallets_raw = shipping_packing_slips_data(order, packing_summary) if packing_summary else []
        pallets: list[dict] = []
        for pallet in pallets_raw:
            load_key = str(pallet.get("pallet_code") or pallet.get("qr_value") or pallet.get("slip_key") or "").strip()
            accepted_scans = {
                _normalize_loading_scan(load_key),
                _normalize_loading_scan(pallet.get("pallet_code")),
                _normalize_loading_scan(pallet.get("qr_value")),
            }
            accepted_scans.discard("")
            pallets.append(
                {
                    "load_key": load_key,
                    "load_key_norm": _normalize_loading_scan(load_key),
                    "pallet_label": str(pallet.get("pallet_label") or "-"),
                    "pallet_code": str(pallet.get("pallet_code") or "").strip(),
                    "qr_value": str(pallet.get("qr_value") or "").strip(),
                    "box_count": int(_parse_int(pallet.get("box_count"))),
                    "accepted_scans": accepted_scans,
                }
            )
        rows.append(
            {
                "item": item,
                "order": order,
                "display_number": format_order_number("shipping", order.number),
                "agency_name": _short_agency_name(getattr(order.agency, "agn_name", None)),
                "destination": order.destination_warehouse or order.destination_address or "-",
                "route_position": position,
                "pallets": pallets,
                "pallet_count": len(pallets),
                "box_count": sum(int(pallet["box_count"]) for pallet in pallets),
            }
        )
    return rows


def _trip_loaded_pallet_keys_from_warehouse(trip: LogisticsTrip, rows: list[dict]) -> list[str]:
    trip_key = str(trip.number or "").strip()
    if not trip_key:
        return []
    loaded_keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        order = row["order"]
        snapshots = _warehouse_shipping_snapshots_for_order(
            order,
            trip_id=trip_key,
            state_codes=[WarehouseStateCode.LOADED_TO_VEHICLE.value],
        )
        loaded_pallet_codes = {
            str(snapshot.parent_container.container_code or "").strip().lower()
            for snapshot in snapshots
            if snapshot.parent_container_id and str(snapshot.parent_container.container_code or "").strip()
        }
        if not loaded_pallet_codes:
            continue
        for pallet in row["pallets"]:
            pallet_code = str(pallet.get("pallet_code") or "").strip().lower()
            load_key_norm = str(pallet.get("load_key_norm") or "").strip()
            if pallet_code and pallet_code in loaded_pallet_codes and load_key_norm and load_key_norm not in seen:
                seen.add(load_key_norm)
                loaded_keys.append(load_key_norm)
    return loaded_keys


def _trip_loading_progress(trip: LogisticsTrip, rows: list[dict]) -> dict:
    payload = _trip_loading_payload(trip)
    raw_loaded = payload.get("loaded_pallet_keys") or []
    if not isinstance(raw_loaded, list):
        raw_loaded = []
    known_keys = {
        pallet["load_key_norm"]
        for row in rows
        for pallet in row["pallets"]
        if pallet["load_key_norm"]
    }
    loaded_keys = [key for key in (_normalize_loading_scan(value) for value in raw_loaded) if key and key in known_keys]
    for key in _trip_loaded_pallet_keys_from_warehouse(trip, rows):
        if key in known_keys and key not in loaded_keys:
            loaded_keys.append(key)
    loaded_set = set(loaded_keys)
    current_row = None
    for row in rows:
        loaded_count = 0
        for pallet in row["pallets"]:
            pallet["is_loaded"] = pallet["load_key_norm"] in loaded_set
            if pallet["is_loaded"]:
                loaded_count += 1
        row["loaded_count"] = loaded_count
        row["remaining_count"] = max(len(row["pallets"]) - loaded_count, 0)
        row["is_complete"] = row["remaining_count"] == 0
        if current_row is None and not row["is_complete"]:
            current_row = row
    return {
        "loaded_keys": loaded_keys,
        "loaded_set": loaded_set,
        "current_row": current_row,
        "all_complete": current_row is None and bool(rows),
        "last_loaded_key": str(payload.get("last_loaded_key") or "").strip(),
    }


def _save_trip_loading_progress(
    trip: LogisticsTrip,
    *,
    loaded_keys: list[str],
    pallet: dict,
    order: ShippingOrder,
    user=None,
) -> None:
    payload = {
        "act": TRIP_LOADING_AUDIT_ACT,
        "trip_pk": trip.pk,
        "loaded_pallet_keys": loaded_keys,
        "last_loaded_key": pallet["load_key_norm"],
        "last_loaded_pallet_label": pallet["pallet_label"],
        "last_loaded_order_number": str(order.number or "").strip(),
        "last_loaded_at": timezone.localtime().isoformat(),
    }
    OrderAuditEntry.objects.create(
        order_id=str(trip.pk),
        order_type=TRIP_LOADING_AUDIT_TYPE,
        action="update",
        agency=order.agency,
        user=user if getattr(user, "is_authenticated", False) else None,
        description=f"Погрузка рейса {_trip_public_number(trip)}: загружена паллета {pallet['pallet_label']}.",
        payload=payload,
    )


def _warehouse_shipping_snapshots_for_order(
    order: ShippingOrder,
    *,
    trip_id: str | None = None,
    state_codes: list[str] | None = None,
) -> list[WarehouseStockSnapshot]:
    active_reserve_statuses = [
        WarehouseReserve.STATUS_ACTIVE,
        WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
        WarehouseReserve.STATUS_ALLOCATED,
        WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
        WarehouseReserve.STATUS_SATISFIED,
    ]
    query = WarehouseStockSnapshot.objects.select_related("active_operation").filter(
        agency=order.agency,
        shipping_reserved_qty__gt=0,
        is_archived=False,
    )
    if trip_id is not None:
        query = query.filter(current_trip_id=str(trip_id or "").strip())
    if state_codes:
        query = query.filter(warehouse_state_code__in=list(state_codes))
    snapshots = list(query.order_by("id"))
    prepared: list[WarehouseStockSnapshot] = []
    order_key = str(order.number or "").strip()
    for snapshot in snapshots:
        has_reserve = WarehouseReserve.objects.filter(
            agency=snapshot.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id=order_key,
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            status__in=active_reserve_statuses,
        ).exists()
        if has_reserve:
            prepared.append(snapshot)
    return prepared


def _sync_trip_loading_completion_to_warehouse(trip: LogisticsTrip, *, user=None) -> None:
    trip_key = str(trip.number or "").strip()
    if not trip_key:
        return
    trip_orders = list(
        trip.orders.select_related("shipping_order", "shipping_order__agency").order_by("loading_sequence", "id")
    )
    for item in trip_orders:
        order = item.shipping_order
        loaded = _warehouse_shipping_snapshots_for_order(
            order,
            trip_id=trip_key,
            state_codes=[WarehouseStateCode.LOADED_TO_VEHICLE.value],
        )
        if loaded:
            continue

        loading = _warehouse_shipping_snapshots_for_order(
            order,
            trip_id=trip_key,
            state_codes=[WarehouseStateCode.LOADING_IN_PROGRESS.value],
        )
        operation = next(
            (
                snapshot.active_operation
                for snapshot in loading
                if snapshot.active_operation is not None
                and str(snapshot.active_operation.operation_type or "").strip() == "load_to_vehicle"
            ),
            None,
        )
        if operation is not None:
            WarehouseWritePathService.complete_loading(operation=operation, performed_by=user)
            continue

        assigned = _warehouse_shipping_snapshots_for_order(
            order,
            trip_id=trip_key,
            state_codes=[WarehouseStateCode.ASSIGNED_TO_TRIP.value],
        )
        if not assigned:
            ready = _warehouse_shipping_snapshots_for_order(
                order,
                state_codes=[WarehouseStateCode.READY_FOR_LOADING.value],
            )
            if not ready:
                continue
            WarehouseWritePathService.assign_to_trip(
                agency=order.agency,
                order_id=order.number,
                trip_id=trip_key,
                assigned_by=user,
            )
        operation = WarehouseWritePathService.start_loading(
            agency=order.agency,
            order_id=order.number,
            trip_id=trip_key,
            started_by=user,
        )
        WarehouseWritePathService.complete_loading(operation=operation, performed_by=user)


def _trip_loading_response_payload(trip: LogisticsTrip, rows: list[dict], progress: dict, result: dict | None = None) -> dict:
    current_row = progress["current_row"]
    return {
        "ok": bool(result.get("ok")) if isinstance(result, dict) else True,
        "result": result or {},
        "all_complete": bool(progress["all_complete"]),
        "current_order_number": current_row["display_number"] if current_row else "",
        "current_order_id": current_row["order"].pk if current_row else None,
        "loaded_total": len(progress["loaded_keys"]),
        "total_pallets": sum(len(row["pallets"]) for row in rows),
        "rows": [
            {
                "order_id": row["order"].pk,
                "display_number": row["display_number"],
                "agency_name": row["agency_name"],
                "destination": row["destination"],
                "route_position": row["route_position"],
                "loaded_count": row["loaded_count"],
                "pallet_count": row["pallet_count"],
                "is_complete": row["is_complete"],
                "is_current": current_row is not None and row["order"].pk == current_row["order"].pk,
                "pallets": [
                    {
                        "pallet_label": pallet["pallet_label"],
                        "pallet_code": pallet["pallet_code"],
                        "qr_value": pallet["qr_value"],
                        "box_count": pallet["box_count"],
                        "is_loaded": bool(pallet.get("is_loaded")),
                    }
                    for pallet in row["pallets"]
                ],
            }
            for row in rows
        ],
    }


def _trip_loading_document_rows(rows: list[dict]) -> list[dict]:
    document_rows: list[dict] = []
    for row in rows:
        order = row["order"]
        document_rows.append(
            {
                "order_id": order.pk,
                "display_number": row["display_number"],
                "agency_name": row["agency_name"],
                "transport_note_url": f"/shipping/{order.pk}/transport-note/docx/",
                "return_act_url": f"/shipping/{order.pk}/return-act/doc/",
            }
        )
    return document_rows


def _ensure_trip_storekeeper_task(trip: LogisticsTrip, user=None) -> Task | None:
    if not trip.pk:
        return None
    storekeeper = _first_active_employee_by_roles("storekeeper")
    if not storekeeper:
        return None
    trip_orders = list(
        trip.orders.select_related("shipping_order", "shipping_order__agency").order_by(
            "loading_sequence", "delivery_sequence", "id"
        )
    )
    client_preview = ", ".join(
        list(
            dict.fromkeys(_short_agency_name(getattr(item.shipping_order.agency, "agn_name", None)) for item in trip_orders)
        )[:3]
    ) or "-"
    destinations = ", ".join(
        list(
            dict.fromkeys(
                (item.shipping_order.destination_warehouse or item.shipping_order.destination_address or "-")
                for item in trip_orders
            )
        )[:2]
    ) or "-"
    vehicle_bits = [bit for bit in [trip.vehicle_name.strip(), _format_vehicle_number_for_plate(trip.vehicle_number)] if bit]
    description = (
        f"Рейс сформирован логистом и ожидает машину. "
        f"Клиенты: {client_preview}. "
        f"Склады назначения: {destinations}. "
        f"Транспорт: {' '.join(vehicle_bits) or '-'}. "
        f"Водитель: {trip.driver_name or '-'}, {trip.driver_phone or '-'}."
    )
    route = _trip_route(trip)
    title = _trip_title(trip)
    due_date = timezone.localtime()
    existing = Task.objects.filter(route=route, assigned_to__role="storekeeper").order_by("-created_at").first()
    observer = trip.assigned_logistician
    if existing:
        existing.title = title
        existing.description = description
        existing.assigned_to = storekeeper
        existing.observer = observer
        existing.due_date = due_date
        if existing.status == "done":
            existing.status = "backlog"
        existing.save(
            update_fields=["title", "description", "assigned_to", "observer", "due_date", "status", "updated_at"]
        )
        return existing
    return Task.objects.create(
        title=title,
        description=description,
        route=route,
        assigned_to=storekeeper,
        observer=observer,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        due_date=due_date,
    )


def _close_trip_tasks_by_role(trip: LogisticsTrip, role: str) -> None:
    Task.objects.filter(route=_trip_route(trip), assigned_to__role=role).exclude(status="done").update(
        status="done",
        updated_at=timezone.now(),
    )


def _ensure_trip_logistician_rework_task(trip: LogisticsTrip, user=None) -> Task | None:
    logistician = trip.assigned_logistician
    if not trip.pk or logistician is None:
        return None
    observer = get_employee_for_user(user) if getattr(user, "is_authenticated", False) else None
    title = _trip_title(trip)
    description = (
        f"Кладовщик вернул рейс {_trip_public_number(trip)} логисту на доработку. "
        f"Проверь состав рейса, маршрут и параметры загрузки перед повторной передачей на погрузку."
    )
    route = _trip_route(trip)
    due_date = timezone.localtime()
    existing = Task.objects.filter(route=route, assigned_to=logistician).order_by("-created_at").first()
    if existing:
        existing.title = title
        existing.description = description
        existing.observer = observer
        existing.due_date = due_date
        if existing.status == "done":
            existing.status = "backlog"
        existing.save(update_fields=["title", "description", "observer", "due_date", "status", "updated_at"])
        return existing
    return Task.objects.create(
        title=title,
        description=description,
        route=route,
        assigned_to=logistician,
        observer=observer,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        due_date=due_date,
    )


def _apply_trip_transport_fields(trip: LogisticsTrip, request, *, validate_vehicle: bool = False) -> None:
    raw_vehicle_number = request.POST.get("vehicle_number")
    raw_driver_phone = request.POST.get("driver_phone")
    trip.vehicle_number = (
        _normalize_vehicle_number(raw_vehicle_number) if validate_vehicle else str(raw_vehicle_number or "").strip()
    )
    trip.driver_name = str(request.POST.get("driver_name") or "").strip()
    trip.driver_phone = _normalize_driver_phone(raw_driver_phone)
    trip.carrier = _resolve_trip_carrier(request)


def _active_trip_links(
    order_ids: list[int] | None = None, exclude_trip_id: int | None = None
) -> dict[int, LogisticsTripOrder]:
    qs = (
        LogisticsTripOrder.objects.select_related("trip", "shipping_order")
        .filter(trip__status__in=ACTIVE_TRIP_STATUSES)
        .order_by("-trip__created_at", "-id")
    )
    if order_ids:
        qs = qs.filter(shipping_order_id__in=order_ids)
    if exclude_trip_id is not None:
        qs = qs.exclude(trip_id=exclude_trip_id)
    result: dict[int, LogisticsTripOrder] = {}
    for item in qs:
        result.setdefault(item.shipping_order_id, item)
    return result


def _packing_payload_map(order_numbers: list[str]) -> dict[str, dict]:
    if not order_numbers:
        return {}
    qs = (
        OrderAuditEntry.objects.filter(
            order_type="shipping",
            order_id__in=order_numbers,
            payload__act="shipping_packing",
        )
        .order_by("order_id", "-created_at")
    )
    result: dict[str, dict] = {}
    for entry in qs:
        if entry.order_id in result:
            continue
        result[entry.order_id] = dict(entry.payload or {})
    return result


def _dashboard_row_due_date(row: dict):
    order = row["order"]
    if order.slot_date:
        return order.slot_date
    if order.planned_ship_date:
        return order.planned_ship_date
    if order.eta_at:
        return timezone.localtime(order.eta_at).date()
    if order.created_at:
        return timezone.localtime(order.created_at).date()
    return None


def _sort_dashboard_ready_rows(rows: list[dict]) -> list[dict]:
    def sort_moment(row: dict):
        trip_link = row.get("trip_link")
        if trip_link is not None:
            trip = trip_link.trip
            return trip.updated_at or trip.created_at or timezone.now()
        order = row["order"]
        return order.updated_at or order.created_at or timezone.now()

    def sort_key(row: dict):
        trip_link = row.get("trip_link")
        moment = sort_moment(row)
        if trip_link is not None:
            trip = trip_link.trip
            route_rank = int(trip_link.loading_sequence or trip_link.delivery_sequence or 0)
            return (-moment.timestamp(), 0, trip.pk, route_rank, row["order"].pk)
        return (-moment.timestamp(), 1, row["order"].pk)

    return sorted(rows, key=sort_key)


def _order_rows(orders, *, active_links: dict[int, LogisticsTripOrder], packing_payloads: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    for order in orders:
        packing_payload = packing_payloads.get(order.number) or {}
        trip_link = active_links.get(order.id)
        is_in_trip = trip_link is not None
        is_loaded_for_trip = bool(
            trip_link and trip_link.trip.status == LogisticsTrip.STATUS_DEPARTED and order.status == ShippingOrder.STATUS_PACKED
        )
        status_label = _display_shipping_status(
            order,
            is_in_trip=is_in_trip,
            is_loaded_for_trip=is_loaded_for_trip,
        )
        dashboard_stage = "shipped" if is_loaded_for_trip else "ready"
        dashboard_subtext = "Отгружено" if is_loaded_for_trip else "Готово к погрузке"
        rows.append(
            {
                "order": order,
                "display_number": format_order_number("shipping", order.number),
                "agency_name": _short_agency_name(getattr(order.agency, "agn_name", None)),
                "status_label": status_label,
                "status_tone": "shipped" if is_loaded_for_trip else ("default" if is_in_trip else "packed"),
                "dashboard_stage": dashboard_stage,
                "dashboard_subtext": dashboard_subtext,
                "box_count": int(packing_payload.get("delivered_box_count") or order.expected_boxes or 0),
                "pallet_count": int(packing_payload.get("pallet_count") or 0),
                "trip_link": trip_link,
                "trip_display_number": _trip_public_number(trip_link.trip) if trip_link else "",
                "is_in_trip": is_in_trip,
            }
        )
    return rows


def _trip_rows(limit: int | None = 20):
    trips = (
        LogisticsTrip.objects.select_related("assigned_logistician", "created_by")
        .prefetch_related(
            Prefetch(
                "orders",
                queryset=LogisticsTripOrder.objects.select_related("shipping_order", "shipping_order__agency").order_by(
                    "loading_sequence", "delivery_sequence", "id"
                ),
            )
        )
        .order_by("-created_at")
    )
    if limit is not None:
        trips = trips[:limit]
    rows: list[dict] = []
    for trip in trips:
        trip_orders = list(trip.orders.all())
        if not trip_orders:
            continue
        client_preview = ", ".join(
            list(
                dict.fromkeys(
                    (_short_agency_name(item.shipping_order.agency.agn_name) or item.shipping_order.number)
                    for item in trip_orders
                )
            )[:3]
        )
        destination_preview = ", ".join(
            list(
                dict.fromkeys(
                    (item.shipping_order.destination_warehouse or item.shipping_order.destination_address or "-")
                    for item in trip_orders
                )
            )[:2]
        )
        rows.append(
            {
                "trip": trip,
                "display_number": _trip_public_number(trip),
                "status_label": _trip_status_label(trip),
                "order_count": len(trip_orders),
                "client_preview": client_preview,
                "destination_preview": destination_preview or "-",
                "driver_name": trip.driver_name or "-",
                "vehicle_name": trip.vehicle_name or "-",
                "vehicle_number_display": _format_vehicle_number_for_plate(trip.vehicle_number) or "-",
                "logistician_name": getattr(trip.assigned_logistician, "full_name", "") or "-",
            }
        )
    return rows


def _ready_orders_for_trip(trip_id: int | None = None):
    qs = ShippingOrder.objects.select_related("agency", "marketplace").filter(status__in=READY_STATUSES).order_by(
        "slot_date", "eta_at", "created_at"
    )
    if trip_id is not None:
        existing_ids = list(LogisticsTripOrder.objects.filter(trip_id=trip_id).values_list("shipping_order_id", flat=True))
        if existing_ids:
            qs = qs.exclude(id__in=existing_ids)
    order_ids = list(qs.values_list("id", flat=True))
    active_links = _active_trip_links(order_ids=order_ids, exclude_trip_id=trip_id)
    if active_links:
        qs = qs.exclude(id__in=list(active_links.keys()))
    order_numbers = list(qs.values_list("number", flat=True))
    packing_payloads = _packing_payload_map(order_numbers)
    return _order_rows(list(qs), active_links={}, packing_payloads=packing_payloads)


def build_logistics_dashboard_context(*, role: str, employee=None) -> dict:
    preparing_orders = list(
        ShippingOrder.objects.select_related("agency", "marketplace")
        .filter(status__in=PREPARING_STATUSES)
        .order_by("slot_date", "eta_at", "created_at")
    )
    ready_orders = list(
        ShippingOrder.objects.select_related("agency", "marketplace")
        .filter(status__in=READY_STATUSES)
        .order_by("slot_date", "eta_at", "created_at")
    )
    all_orders = preparing_orders + ready_orders
    active_links = _active_trip_links(order_ids=[order.id for order in all_orders])
    packing_payloads = _packing_payload_map([order.number for order in all_orders])
    trip_rows = _trip_rows()
    sidebar_trip = next((row["trip"] for row in trip_rows if row["trip"].status in ACTIVE_TRIP_STATUSES), None)
    return {
        "role": role,
        "cabinet_url": resolve_cabinet_url(role),
        "preparing_orders": _order_rows(
            preparing_orders,
            active_links=active_links,
            packing_payloads=packing_payloads,
        ),
        "ready_orders": _sort_dashboard_ready_rows(
            _order_rows(
                ready_orders,
                active_links=active_links,
                packing_payloads=packing_payloads,
            )
        ),
        "trips": trip_rows,
        "sidebar_trip": sidebar_trip,
        "trip_vehicle_choices": LogisticsTrip.VEHICLE_CHOICES,
        "is_logistician": role == "logistician",
    }


def handle_logistics_dashboard_post(request, *, role: str, employee=None):
    action = (request.POST.get("action") or "").strip()
    if action != "create_trip":
        return None
    selected_ids = [_parse_int(value) for value in request.POST.getlist("order_ids") if _parse_int(value) > 0]
    if not selected_ids:
        messages.error(request, "Выберите хотя бы одну готовую заявку для формирования рейса.")
        return redirect("logistics:dashboard")
    selected_orders = list(
        ShippingOrder.objects.select_related("agency")
        .filter(id__in=selected_ids, status=ShippingOrder.STATUS_PACKED)
        .order_by("slot_date", "eta_at", "created_at")
    )
    if len(selected_orders) != len(set(selected_ids)):
        messages.error(request, "Часть заявок недоступна для консолидации в рейс.")
        return redirect("logistics:dashboard")
    active_links = _active_trip_links(order_ids=[order.id for order in selected_orders])
    if active_links:
        linked_orders = ", ".join(active_links[item_id].shipping_order.number for item_id in active_links)
        messages.error(request, f"Некоторые заявки уже включены в активные рейсы: {linked_orders}.")
        return redirect("logistics:dashboard")

    try:
        trip = LogisticsTrip(
            number=next_draft_trip_number(),
            trip_date=_parse_date(request.POST.get("trip_date")),
            status=LogisticsTrip.STATUS_DRAFT,
            vehicle_type=str(request.POST.get("vehicle_type") or "").strip(),
            vehicle_name=str(request.POST.get("vehicle_name") or "").strip(),
            route_comment=str(request.POST.get("route_comment") or "").strip(),
            assigned_logistician=employee if employee and employee.role == "logistician" else None,
            created_by=request.user if request.user.is_authenticated else None,
        )
        _apply_trip_transport_fields(trip, request, validate_vehicle=False)
        trip.save()
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("logistics:dashboard")
    for index, order in enumerate(selected_orders, start=1):
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=index,
            delivery_sequence=index,
        )
    messages.success(request, "Создан черновик рейса.")
    return redirect("logistics:trip-detail", pk=trip.pk)


def build_logistics_trip_list_context(*, role: str) -> dict:
    return {
        "role": role,
        "cabinet_url": resolve_cabinet_url(role),
        "trips": _trip_rows(limit=None),
    }


def get_trip_detail_trip(pk: int) -> LogisticsTrip:
    return get_object_or_404(
        LogisticsTrip.objects.select_related("assigned_logistician", "created_by", "carrier").prefetch_related(
            Prefetch(
                "orders",
                queryset=LogisticsTripOrder.objects.select_related("shipping_order", "shipping_order__agency").order_by(
                    "loading_sequence", "delivery_sequence", "id"
                ),
            )
        ),
        pk=pk,
    )


def build_logistics_trip_detail_context(*, role: str, trip: LogisticsTrip) -> dict:
    can_edit_trip = _can_edit_trip(role, trip)
    can_return_trip = _can_return_trip(role, trip)
    can_begin_loading = _can_begin_loading(role, trip)
    trip_orders = list(trip.orders.all())
    packing_payloads = _packing_payload_map([item.shipping_order.number for item in trip_orders])
    loading_rows = _trip_loading_rows(trip)
    loading_progress = _trip_loading_progress(trip, loading_rows)
    loaded_order_ids = {row["order"].pk for row in loading_rows if row.get("is_complete")} if loading_progress["loaded_set"] else set()
    order_rows = []
    for index, item in enumerate(trip_orders, start=1):
        order = item.shipping_order
        packing_payload = packing_payloads.get(order.number) or {}
        order_rows.append(
            {
                "item": item,
                "order": order,
                "route_position": index,
                "display_number": format_order_number("shipping", order.number),
                "agency_name": _short_agency_name(getattr(order.agency, "agn_name", None)),
                "status_label": _display_shipping_status(
                    order,
                    is_in_trip=True,
                    is_loaded_for_trip=order.pk in loaded_order_ids,
                ),
                "box_count": int(packing_payload.get("delivered_box_count") or order.expected_boxes or 0),
                "pallet_count": int(packing_payload.get("pallet_count") or 0),
            }
        )
    return {
        "role": role,
        "cabinet_url": resolve_cabinet_url(role),
        "can_edit_trip": can_edit_trip,
        "can_return_trip": can_return_trip,
        "can_begin_loading": can_begin_loading,
        "can_start_loading": _can_start_loading(role, trip),
        "show_loading_entrypoint": can_begin_loading,
        "can_finalize_trip": can_edit_trip and trip.status in {LogisticsTrip.STATUS_DRAFT, LogisticsTrip.STATUS_PLANNED},
        "trip": trip,
        "trip_display_number": _trip_public_number(trip),
        "trip_loading_url": _trip_loading_url(trip),
        "trip_is_departed": trip.status == LogisticsTrip.STATUS_DEPARTED,
        "trip_status_choices": LogisticsTrip.STATUS_CHOICES,
        "trip_vehicle_choices": LogisticsTrip.VEHICLE_CHOICES,
        "trip_orders": order_rows,
        "trip_total_pallets": sum(row["pallet_count"] for row in order_rows),
        "trip_total_boxes": sum(row["box_count"] for row in order_rows),
        "trip_vehicle_number_display": _format_vehicle_number_for_plate(trip.vehicle_number),
        "carrier_options": list(_carrier_queryset()),
        "shipper_name": _trip_shipper_name(),
        "customer_name": _trip_customer_summary(trip_orders),
        "consignee_name": _trip_consignee_summary(trip_orders),
        "available_ready_orders": _ready_orders_for_trip(trip_id=trip.pk),
        "status_label": _trip_status_label(trip),
    }


def handle_logistics_trip_detail_post(request, *, role: str, trip: LogisticsTrip):
    can_edit_trip = _can_edit_trip(role, trip)
    can_return_trip = _can_return_trip(role, trip)
    can_begin_loading = _can_begin_loading(role, trip)
    action = (request.POST.get("action") or "").strip()
    if action == "start_loading":
        if not can_begin_loading:
            return HttpResponseForbidden("Доступ запрещен")
        if trip.status != LogisticsTrip.STATUS_LOADING:
            trip.status = LogisticsTrip.STATUS_LOADING
            trip.save(update_fields=["status", "updated_at"])
            messages.success(request, f"Погрузка рейса {_trip_public_number(trip)} начата.")
        return redirect("logistics:trip-loading", pk=trip.pk)
    if action == "return_to_logistic":
        if not can_return_trip:
            return HttpResponseForbidden("Доступ запрещен")
        trip.status = LogisticsTrip.STATUS_PLANNED
        trip.save(update_fields=["status", "updated_at"])
        _close_trip_tasks_by_role(trip, "storekeeper")
        _ensure_trip_logistician_rework_task(trip, request.user)
        messages.success(request, f"Рейс {_trip_public_number(trip)} возвращен логисту на доработку.")
        return redirect("logistics:trip-detail", pk=trip.pk)
    if not can_edit_trip:
        return HttpResponseForbidden("Доступ запрещен")
    if action == "update_loading_params":
        try:
            _apply_trip_transport_fields(trip, request, validate_vehicle=True)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("logistics:trip-detail", pk=trip.pk)
        trip.save(update_fields=["vehicle_number", "driver_name", "driver_phone", "carrier", "updated_at"])
        messages.success(request, "Параметры загрузки обновлены.")
        return redirect("logistics:trip-detail", pk=trip.pk)
    if action == "update_trip":
        try:
            trip.trip_date = _parse_date(request.POST.get("trip_date"))
            trip.status = str(request.POST.get("status") or trip.status).strip() or trip.status
            trip.vehicle_type = str(request.POST.get("vehicle_type") or "").strip()
            trip.vehicle_name = str(request.POST.get("vehicle_name") or "").strip()
            _apply_trip_transport_fields(trip, request, validate_vehicle=True)
            trip.route_comment = str(request.POST.get("route_comment") or "").strip()
            trip.loading_comment = str(request.POST.get("loading_comment") or "").strip()
            trip.save()
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("logistics:trip-detail", pk=trip.pk)
        messages.success(request, "Параметры рейса обновлены.")
        return redirect("logistics:trip-detail", pk=trip.pk)
    if action == "finalize_trip":
        try:
            _apply_trip_transport_fields(trip, request, validate_vehicle=True)
            if not trip.driver_name:
                raise ValueError("Укажите ФИО водителя перед формированием рейса.")
            if trip.orders.count() <= 0:
                raise ValueError("Нельзя сформировать пустой рейс.")
            if trip.status not in {LogisticsTrip.STATUS_DRAFT, LogisticsTrip.STATUS_PLANNED}:
                raise ValueError("Этот рейс уже сформирован.")
            if _first_active_employee_by_roles("storekeeper") is None:
                raise ValueError("Не найден активный кладовщик для передачи сформированного рейса.")
            trip.number = _assign_public_trip_number(trip)
            trip.status = LogisticsTrip.STATUS_LOADING
            trip.save(
                update_fields=["number", "vehicle_number", "driver_name", "driver_phone", "carrier", "status", "updated_at"]
            )
            _close_trip_tasks_by_role(trip, "logistician")
            _ensure_trip_storekeeper_task(trip, request.user)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("logistics:trip-detail", pk=trip.pk)
        messages.success(request, f"Рейс {_trip_public_number(trip)} сформирован и передан кладовщику.")
        return redirect("logistics:trip-detail", pk=trip.pk)
    if action == "update_sequences":
        ordered_ids = [_parse_int(value) for value in request.POST.getlist("ordered_trip_item_ids") if _parse_int(value) > 0]
        ordered_items = []
        if ordered_ids:
            item_map = {item.id: item for item in trip.orders.all()}
            ordered_items = [item_map[item_id] for item_id in ordered_ids if item_id in item_map]
        if not ordered_items:
            ordered_items = list(trip.orders.all())
        for index, item in enumerate(ordered_items, start=1):
            item.loading_sequence = index
            item.delivery_sequence = index
            item.comment = str(request.POST.get(f"comment_{item.id}") or "").strip()
            item.save(update_fields=["loading_sequence", "delivery_sequence", "comment", "updated_at"])
        messages.success(request, "Порядок заявок в рейсе обновлен.")
        return redirect("logistics:trip-detail", pk=trip.pk)
    if action == "add_orders":
        selected_ids = [_parse_int(value) for value in request.POST.getlist("order_ids") if _parse_int(value) > 0]
        if not selected_ids:
            messages.error(request, "Выберите готовые заявки для добавления в рейс.")
            return redirect("logistics:trip-detail", pk=trip.pk)
        active_links = _active_trip_links(order_ids=selected_ids, exclude_trip_id=trip.pk)
        if active_links:
            linked_orders = ", ".join(active_links[item_id].shipping_order.number for item_id in active_links)
            messages.error(request, f"Некоторые заявки уже включены в другие активные рейсы: {linked_orders}.")
            return redirect("logistics:trip-detail", pk=trip.pk)
        existing_ids = set(trip.orders.values_list("shipping_order_id", flat=True))
        start_position = trip.orders.count()
        candidates = list(
            ShippingOrder.objects.filter(id__in=selected_ids, status=ShippingOrder.STATUS_PACKED).order_by(
                "slot_date", "eta_at", "created_at"
            )
        )
        created = 0
        for order in candidates:
            if order.id in existing_ids:
                continue
            start_position += 1
            LogisticsTripOrder.objects.create(
                trip=trip,
                shipping_order=order,
                loading_sequence=start_position,
                delivery_sequence=start_position,
            )
            created += 1
        if created:
            messages.success(request, f"В рейс добавлено заявок: {created}.")
        else:
            messages.error(request, "Новые заявки для добавления не найдены.")
        return redirect("logistics:trip-detail", pk=trip.pk)
    if action == "remove_order":
        link_id = _parse_int(request.POST.get("trip_order_id"))
        link = trip.orders.filter(id=link_id).first()
        if link:
            link.delete()
            if not trip.orders.exists():
                trip.delete()
                messages.success(request, "Пустой черновик рейса удален.")
                return redirect("logistics:trip-list")
            messages.success(request, "Заявка удалена из рейса.")
        else:
            messages.error(request, "Связь рейса с заявкой не найдена.")
        return redirect("logistics:trip-detail", pk=trip.pk)
    return None


def get_trip_loading_trip(pk: int) -> LogisticsTrip:
    return get_object_or_404(
        LogisticsTrip.objects.select_related("assigned_logistician", "created_by", "carrier"),
        pk=pk,
    )


def build_logistics_trip_loading_context(*, role: str, trip: LogisticsTrip) -> dict:
    rows = _trip_loading_rows(trip)
    progress = _trip_loading_progress(trip, rows)
    can_scan_loading = _can_start_loading(role, trip)
    return {
        "role": role,
        "cabinet_url": resolve_cabinet_url(role),
        "trip": trip,
        "trip_display_number": _trip_public_number(trip),
        "trip_status_label": _trip_status_label(trip),
        "trip_detail_url": f"/logistics/trips/{trip.pk}/",
        "can_scan_loading": can_scan_loading,
        "can_finish_loading": can_scan_loading and bool(progress["all_complete"]),
        "scanner_agents": _scanner_agents_payload(),
        "loading_rows": rows,
        "loading_progress": progress,
        "loading_state_payload": _trip_loading_response_payload(trip, rows, progress),
        "loading_document_rows": _trip_loading_document_rows(rows),
    }


def handle_logistics_trip_loading_post(request, *, role: str, trip: LogisticsTrip):
    rows = _trip_loading_rows(trip)
    progress = _trip_loading_progress(trip, rows)
    can_scan_loading = _can_start_loading(role, trip)
    if not can_scan_loading:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
        return HttpResponseForbidden("Доступ запрещен")
    action = str(request.POST.get("action") or "").strip()
    if action == "finish_loading":
        if not progress["all_complete"]:
            messages.error(request, "Сначала загрузите все паллеты по рейсу.")
            return redirect("logistics:trip-loading", pk=trip.pk)
        _sync_trip_loading_completion_to_warehouse(trip, user=request.user)
        trip.status = LogisticsTrip.STATUS_DEPARTED
        trip.save(update_fields=["status", "updated_at"])
        _close_trip_tasks_by_role(trip, "storekeeper")
        return redirect("logistics:trip-detail", pk=trip.pk)
    scan_value = _normalize_loading_scan(request.POST.get("scan_value"))
    if not scan_value:
        return JsonResponse(
            _trip_loading_response_payload(
                trip,
                rows,
                progress,
                {"ok": False, "status": "empty", "tone": "error", "message": "Отсканируй паллету перед подтверждением."},
            ),
            status=400,
        )
    current_row = progress["current_row"]
    if current_row is None:
        return JsonResponse(
            _trip_loading_response_payload(
                trip,
                rows,
                progress,
                {"ok": False, "status": "complete", "tone": "success", "message": "Все паллеты по рейсу уже загружены."},
            )
        )
    matched_pallet = None
    matched_row = None
    for row in rows:
        for pallet in row["pallets"]:
            if scan_value in pallet["accepted_scans"]:
                matched_pallet = pallet
                matched_row = row
                break
        if matched_pallet:
            break
    if matched_pallet is None or matched_row is None:
        return JsonResponse(
            _trip_loading_response_payload(
                trip,
                rows,
                progress,
                {"ok": False, "status": "unknown", "tone": "error", "message": "Паллета не найдена в этом рейсе."},
            ),
            status=400,
        )
    if matched_pallet["load_key_norm"] in progress["loaded_set"]:
        return JsonResponse(
            _trip_loading_response_payload(
                trip,
                rows,
                progress,
                {
                    "ok": False,
                    "status": "duplicate",
                    "tone": "error",
                    "message": f"Паллета {matched_pallet['pallet_label']} уже загружена.",
                },
            ),
            status=400,
        )
    if matched_row["order"].pk != current_row["order"].pk:
        return JsonResponse(
            _trip_loading_response_payload(
                trip,
                rows,
                progress,
                {
                    "ok": False,
                    "status": "wrong_order",
                    "tone": "error",
                    "message": (
                        f"Сейчас очередь заявки {current_row['display_number']}. "
                        f"Паллета {matched_pallet['pallet_label']} относится к {matched_row['display_number']}."
                    ),
                },
            ),
            status=409,
        )
    next_loaded_keys = progress["loaded_keys"] + [matched_pallet["load_key_norm"]]
    _save_trip_loading_progress(
        trip,
        loaded_keys=next_loaded_keys,
        pallet=matched_pallet,
        order=matched_row["order"],
        user=request.user,
    )
    rows = _trip_loading_rows(trip)
    progress = _trip_loading_progress(trip, rows)
    next_row = progress["current_row"]
    message = f"Паллета {matched_pallet['pallet_label']} загружена."
    if next_row and next_row["order"].pk != matched_row["order"].pk:
        message = f"{message} Следующая очередь: {next_row['display_number']}."
    elif progress["all_complete"]:
        message = f"{message} Все паллеты по рейсу загружены."
    return JsonResponse(
        _trip_loading_response_payload(
            trip,
            rows,
            progress,
            {"ok": True, "status": "loaded", "tone": "success", "message": message},
        )
    )
