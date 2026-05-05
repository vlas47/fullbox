from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.db.models import Count, Q

from audit.models import OrderAuditEntry, log_order_action, log_staff_overaction
from employees.access import get_request_role, resolve_cabinet_url
from employees.models import Employee
from marking.models import MarkingCode
from marking.utils import extract_processing_items
from sklad.services.stock_operations import OperationalStockService
from sklad.services.stock_availability import StockAvailabilityService
from sklad.services.warehouse_policy import WarehouseActionPolicy
from sklad.services.warehouse_state import WarehouseGoodsStateResolver
from sklad.services.warehouse_transitions import WarehouseStateCode
from sklad.services.warehouse_write_path import WarehouseWritePathService
from todo.models import Task
from labels.utils import shorten_client_label, split_printers_by_kind
from .models import ProcessingFlowSession, ProcessingPrintJob


@dataclass
class ProcessingWarehouseMoveActionResult:
    status: str
    error_message: str = ""
    created_count: int = 0
    skipped_existing_count: int = 0
    skipped_missing_count: int = 0
    canceled_count: int = 0
    skipped_count: int = 0


@dataclass
class ProcessingFinishResult:
    status: str
    error_message: str = ""


@dataclass
class ProcessingFlowDraftResult:
    status: str
    session_id: int | None = None


@dataclass
class ProcessingFlowReopenResult:
    status: str


@dataclass
class ProcessingFlowSessionResult:
    status: str
    session_id: int | None = None
    flow_state: dict | None = None
    updated_at: str = ""


@dataclass
class ProcessingFlowSharedResult:
    status: str
    flow_state: dict


@dataclass
class ProcessingFlowBoxActionResult:
    status: str


@dataclass
class ProcessingFlowCompletionResult:
    status: str
    error_code: str = ""


@dataclass
class ProcessingCardActionResult:
    status: str
    redirect_to: str


@dataclass
class ProcessingPackagingAssignmentResult:
    status: str
    redirect_to: str


@dataclass
class ProcessingMarkingScanResult:
    status: str
    http_status: int
    payload: dict


@dataclass
class ProcessingDetailActionResult:
    status: str
    redirect_to: str


@dataclass
class ProcessingJsonResult:
    status: str
    http_status: int
    payload: dict


class ProcessingWorkflowService:
    _PROCESSING_WAREHOUSE_STARTED_CODES = {
        WarehouseStateCode.RESERVED_FOR_PROCESSING,
        WarehouseStateCode.MOVING_TO_PROCESSING,
        WarehouseStateCode.IN_PROCESSING_ZONE,
        WarehouseStateCode.PROCESSING_IN_PROGRESS,
        WarehouseStateCode.PLACED_AFTER_PROCESSING,
        WarehouseStateCode.STORED,
    }

    @staticmethod
    def _views():
        from . import views as processing_views

        return processing_views

    @staticmethod
    def _processing_home_status_label(
        status_value: str | None,
        fallback_label: str | None = None,
    ) -> str:
        if fallback_label:
            return fallback_label
        value = str(status_value or "").strip().lower()
        if value == "draft":
            return "Черновик"
        if value in {"sent_unconfirmed", "send", "submitted"}:
            return "Ждет подтверждения"
        return "Подготовка заявки" if not value else value

    @staticmethod
    def _barcodes_from_rows(rows) -> list[str]:
        values: list[str] = []
        if not isinstance(rows, list):
            return values
        for row in rows:
            if not isinstance(row, dict):
                continue
            barcode_value = str(row.get("barcode") or row.get("barcode_value") or "").strip()
            if barcode_value:
                values.append(barcode_value)
        return values

    @classmethod
    def _printed_cz_total_for_rows(cls, printed_cz_base_qs, rows) -> int:
        if printed_cz_base_qs is None:
            return 0
        barcodes = cls._barcodes_from_rows(rows)
        qs = printed_cz_base_qs
        if barcodes:
            qs = qs.filter(barcode__in=barcodes)
        return qs.count()

    @classmethod
    def _build_processing_warehouse_move_rows(
        cls,
        *,
        placement_pallets,
        suggested_destinations: dict[str, dict],
        warehouse_move_progress: dict,
    ) -> list[dict]:
        processing_views = cls._views()
        moves_by_pallet = warehouse_move_progress.get("moves_by_pallet") or {}
        implicit_done_codes = set(warehouse_move_progress.get("implicit_done_codes") or set())
        warehouse_move_rows: list[dict] = []
        for pallet in placement_pallets:
            if not isinstance(pallet, dict):
                continue
            pallet_code = str(pallet.get("code") or "").strip()
            if not pallet_code:
                continue
            from_location = processing_views._normalize_move_location(pallet.get("location"), pallet)
            from_label = processing_views._move_location_label(from_location)
            move_payload = moves_by_pallet.get(pallet_code) or {}
            move_status = str(move_payload.get("status") or "").strip().lower()
            move_status_label = str(move_payload.get("status_label") or "").strip()
            is_implicit_done = pallet_code in implicit_done_codes
            status_class = "pending"
            status_text = "Задание не создано"
            raw_to_zone = processing_views._normalize_move_zone(str(move_payload.get("to_zone") or "").strip())
            to_row = processing_views._parse_int_value(move_payload.get("to_row"))
            to_section = processing_views._parse_int_value(move_payload.get("to_section"))
            to_tier = processing_views._parse_int_value(move_payload.get("to_tier"))
            to_cell = processing_views._parse_int_value(move_payload.get("to_cell"))
            placement_destination = processing_views._normalize_move_location(pallet.get("location"), pallet)
            suggested_destination = suggested_destinations.get(pallet_code) or {
                "zone": "PR",
                "row": "",
                "section": "",
                "tier": "",
                "cell": "",
            }
            default_destination = placement_destination
            if processing_views._normalize_move_zone(default_destination.get("zone")) not in {"MR", "OS"}:
                default_destination = suggested_destination
            if raw_to_zone:
                to_location = processing_views._normalize_move_location(
                    {
                        "zone": raw_to_zone,
                        "row": to_row,
                        "section": to_section,
                        "tier": to_tier,
                        "cell": to_cell,
                    }
                )
            else:
                to_location = default_destination
            to_label = str(move_payload.get("to_label") or "").strip() or processing_views._move_location_label(
                to_location
            )
            destination_note = ""
            if raw_to_zone:
                destination_note = "Место уже подтверждено в задании ричтракеру."
            elif processing_views._normalize_move_zone(placement_destination.get("zone")) in {"MR", "OS"}:
                destination_note = "Место взято из акта размещения."
            elif processing_views._normalize_move_zone(suggested_destination.get("zone")) in {"MR", "OS"}:
                destination_note = "Сервис предложил место хранения. Его можно изменить вручную."
            else:
                destination_note = "Выберите место хранения вручную."
            selectable = True
            if move_status == "done" or is_implicit_done:
                status_text = "Доставлено на склад"
                status_class = "done"
                selectable = False
            elif move_status in {"canceled", "cancelled"}:
                status_text = move_status_label or "Задание отменено, сформируйте новое"
                status_class = "canceled"
            elif move_status == "in_progress":
                status_text = move_status_label or "В работе у ричтракера"
                status_class = "in_progress"
                selectable = False
            elif move_status == "created":
                status_text = move_status_label or "Передано ричтракеру"
                status_class = "created"
                selectable = False
            elif move_status:
                status_text = move_status_label or move_status
                status_class = "created"
            warehouse_move_rows.append(
                {
                    "pallet_code": pallet_code,
                    "from_label": from_label,
                    "to_label": to_label,
                    "status": move_status,
                    "status_label": status_text,
                    "status_class": status_class,
                    "move_order_id": str(move_payload.get("order_id") or "").strip(),
                    "selectable": selectable,
                    "destination": to_location,
                    "destination_note": destination_note,
                }
            )
        return warehouse_move_rows

    @staticmethod
    def _warehouse_destination_summary(warehouse_move_rows: list[dict], *, canceled_count: int, not_created_count: int) -> str:
        destination_counts: dict[str, int] = {}
        for row in warehouse_move_rows:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").strip().lower()
            if status in {"canceled", "cancelled"}:
                continue
            if not str(row.get("move_order_id") or "").strip():
                continue
            to_label = str(row.get("to_label") or "").strip()
            if not to_label:
                continue
            destination_counts[to_label] = destination_counts.get(to_label, 0) + 1
        if destination_counts:
            destination_parts = []
            for label, count in destination_counts.items():
                if count > 1:
                    destination_parts.append(f"{label} ({count} пал.)")
                else:
                    destination_parts.append(label)
            return "; ".join(destination_parts)
        if canceled_count > 0:
            return "не задано (последние задания отменены)"
        if not_created_count > 0:
            return "место хранения не подтверждено"
        return "место хранения не выбрано"

    @classmethod
    def build_processing_flow_page_context(
        cls,
        *,
        order_id: str,
        entries,
        request,
        can_finish_flow: bool,
        can_finish_flow_mismatch: bool,
        can_reassign_boxes: bool,
        is_directional_unboxing_payload,
        items_from_placement_act,
        normalize_flow_state,
        find_flow_state,
        placement_act_entry,
        ok: bool = False,
        error: str | None = None,
    ) -> dict:
        from django.db import DatabaseError
        from datetime import timedelta

        from agent.models import DeviceAgent
        from labels.utils import SCANNER_EOLS, load_scanner_settings
        from sku.models import SKU, SKUBarcode

        processing_views = cls._views()
        ctx: dict = {}
        role = get_request_role(request)
        ctx["can_finish_flow"] = can_finish_flow
        ctx["can_finish_flow_mismatch"] = can_finish_flow_mismatch
        ctx["can_reassign_boxes"] = can_reassign_boxes
        latest = entries[-1] if entries else None
        status_entry = processing_views._current_status_entry(entries)
        payload = processing_views._latest_payload_from_entries(entries)
        ctx["directional_unboxing"] = is_directional_unboxing_payload(payload)
        ctx["flow_direction_data"] = processing_views._processing_direction_flow_data(payload)
        processed_cards, placed_cards = processing_views._processing_card_sets(payload)
        ready_cards = processed_cards - placed_cards if processed_cards else set()
        items = processing_views._processing_receiving_items(
            payload,
            latest.agency_id if latest else None,
            ready_cards if ready_cards else None,
        )
        if not items:
            items = items_from_placement_act(entries)
        display_items = []
        for item in items:
            display_items.append(
                {
                    "sku_code": item.get("sku_code") or "",
                    "name": item.get("name") or "",
                    "size": item.get("size") or "",
                    "qty": processing_views._parse_qty_value(item.get("actual_qty")) or 0,
                    "comment": "",
                }
            )
        client_label = "-"
        client_prefix = ""
        if latest and latest.agency:
            name = latest.agency.agn_name or latest.agency.fio_agn or str(latest.agency)
            client_label = shorten_client_label(name)
            client_prefix = (latest.agency.pref or "").strip()
        status_payload = status_entry.payload or {} if status_entry else {}
        goods_type = (status_payload.get("goods_type") or payload.get("goods_type") or "").strip().lower()
        cz_required = bool(processing_views._parse_qty_value(payload.get("marking_5840_each_qty")))
        catalog_items = []
        barcode_map = {}
        if latest and latest.agency_id:
            for sku in SKU.objects.filter(agency_id=latest.agency_id, deleted=False).only("sku_code", "name", "size"):
                catalog_items.append(
                    {
                        "sku_code": sku.sku_code,
                        "name": sku.name,
                        "size": sku.size,
                    }
                )
            for barcode in (
                SKUBarcode.objects.select_related("sku")
                .filter(sku__agency_id=latest.agency_id, sku__deleted=False)
            ):
                value = (barcode.value or "").strip()
                if not value:
                    continue
                sku = barcode.sku
                barcode_map[value] = {
                    "sku_code": sku.sku_code,
                    "name": sku.name,
                    "size": (barcode.size or sku.size or "").strip(),
                }
        flow_locked = processing_views._flow_closed_from_entries(entries)
        flow_reopened = False
        for entry in reversed(entries or []):
            payload_entry = entry.payload or {}
            if payload_entry.get("flow_reopened"):
                flow_reopened = True
                break
            if payload_entry.get("flow_closed"):
                break
        flow_state: dict = {}
        flow_state_shared: dict = {"boxes": [], "pallets": []}
        if flow_locked:
            flow_state = find_flow_state(entries) or {}
            if flow_state and (flow_state.get("boxes") or flow_state.get("pallets")):
                flow_state = normalize_flow_state(
                    flow_state.get("boxes") or [],
                    flow_state.get("pallets") or [],
                    flow_state.get("activeBox") or "",
                    flow_state.get("activePallet") or "",
                )
            if not flow_state or not (flow_state.get("boxes") or flow_state.get("pallets")):
                placement_entry = placement_act_entry(entries)
                if placement_entry:
                    placement_payload = placement_entry.payload or {}
                    placement_boxes = placement_payload.get("act_boxes") or []
                    placement_pallets = placement_payload.get("act_pallets") or []
                    if placement_boxes or placement_pallets:
                        active_box = next((box.get("code") for box in placement_boxes if not box.get("sealed")), "")
                        active_pallet = next((pallet.get("code") for pallet in placement_pallets if not pallet.get("sealed")), "")
                        flow_state = normalize_flow_state(
                            placement_boxes,
                            placement_pallets,
                            active_box,
                            active_pallet,
                        )
        else:
            agent_id = (request.GET.get("agent_id") or "").strip()
            current_session = None
            if agent_id:
                current_session = processing_views._flow_session_for_request(order_id, agent_id, request, create=False)
                if current_session and isinstance(current_session.flow_state, dict):
                    flow_state = processing_views._sanitize_flow_state_for_session(current_session.flow_state, current_session)
            shared_sessions = list(
                ProcessingFlowSession.objects.filter(
                    order_id=order_id,
                    order_type="processing",
                    status=ProcessingFlowSession.STATUS_OPEN,
                )
            )
            if shared_sessions:
                boxes_data, pallets_data = processing_views._merge_flow_sessions(shared_sessions)
                flow_state_shared = {"boxes": boxes_data, "pallets": pallets_data}
            if not (flow_state_shared.get("boxes") or flow_state_shared.get("pallets")):
                reopened = any((entry.payload or {}).get("flow_reopened") for entry in entries or [])
                if reopened:
                    placement_entry = placement_act_entry(entries)
                    if placement_entry:
                        placement_payload = placement_entry.payload or {}
                        placement_boxes = placement_payload.get("act_boxes") or []
                        placement_pallets = placement_payload.get("act_pallets") or []
                        if isinstance(placement_boxes, list) or isinstance(placement_pallets, list):
                            flow_state_shared = {
                                "boxes": placement_boxes if isinstance(placement_boxes, list) else [],
                                "pallets": placement_pallets if isinstance(placement_pallets, list) else [],
                            }
        act_print_url = ""
        placement_entry = placement_act_entry(entries)
        scanner_agents = []
        scanner_ports = []
        scanner_default = {}
        scanner_ports_info = []
        agent_devices_map: dict[str, list[dict]] = {}
        if placement_entry and flow_locked:
            act_print_url = f"/orders/processing/{order_id}/placement/?return=/orders/processing/{order_id}/flow/"
        try:
            online_threshold = timezone.now() - timedelta(seconds=30)
            ports_pool = set()
            ports_map: dict[str, list[dict]] = {}
            devices_map: dict[str, list[dict]] = {}
            for agent in DeviceAgent.objects.all().order_by("-last_seen", "-updated_at"):
                is_online = bool(agent.last_seen and agent.last_seen >= online_threshold)
                meta = agent.meta if isinstance(agent.meta, dict) else {}
                ports = meta.get("com_ports") or meta.get("ports")
                devices = meta.get("com_devices") if isinstance(meta.get("com_devices"), list) else []
                agent_devices_map[agent.agent_id] = devices if isinstance(devices, list) else []
                for device in devices:
                    if not isinstance(device, dict):
                        continue
                    port_value = str(device.get("port") or "").strip()
                    if not port_value:
                        continue
                    ports_pool.add(port_value)
                    devices_map.setdefault(port_value.lower(), []).append(device)
                com_config = meta.get("com") if isinstance(meta.get("com"), dict) else {}
                com_status = meta.get("com_status") if isinstance(meta.get("com_status"), dict) else {}
                if isinstance(ports, str):
                    ports = [ports]
                if not isinstance(ports, list):
                    ports = []
                ports = [str(port).strip() for port in ports if str(port).strip()]
                seen_ports = set()
                unique_ports = []
                for port in ports:
                    key = port.lower()
                    if key in seen_ports:
                        continue
                    seen_ports.add(key)
                    unique_ports.append(port)
                ports = unique_ports
                for port in ports:
                    ports_pool.add(port)
                com_port = str(
                    com_status.get("port")
                    or com_config.get("port")
                    or com_config.get("port_name")
                    or ""
                ).strip()
                com_enabled = bool(com_status.get("enabled") if "enabled" in com_status else com_config.get("enabled"))
                com_connected = bool(com_status.get("connected"))
                com_error = str(com_status.get("error") or "").strip()
                com_error_type = str(com_status.get("error_type") or "").strip()
                com_error_code = com_status.get("error_code")
                com_last_scan = str(com_status.get("last_scan_at") or "").strip()
                com_last_chunk = str(com_status.get("last_chunk_at") or "").strip()
                com_baud = com_status.get("baud") or com_config.get("baud")
                com_eol = com_status.get("eol") or com_config.get("eol")
                com_idle = com_status.get("idle_ms") or com_config.get("idle_ms")
                scanner_agents.append(
                    {
                        "agent_id": agent.agent_id,
                        "title": agent.name or agent.host or agent.agent_id,
                        "status": "онлайн" if is_online else "нет связи",
                        "is_online": is_online,
                        "host": agent.host,
                        "version": agent.version,
                        "last_seen": agent.last_seen.isoformat() if agent.last_seen else "",
                        "ports": ports,
                        "com_port": com_port,
                        "com_enabled": com_enabled,
                        "com_connected": com_connected,
                        "com_error": com_error,
                        "com_error_type": com_error_type,
                        "com_error_code": com_error_code,
                        "com_last_scan": com_last_scan,
                        "com_last_chunk": com_last_chunk,
                        "com_baud": com_baud,
                        "com_eol": com_eol,
                        "com_idle": com_idle,
                    }
                )
                if com_port:
                    key = com_port.lower()
                    ports_map.setdefault(key, []).append(scanner_agents[-1])
            scanner_ports = sorted(ports_pool)
            settings_payload = load_scanner_settings()
            if isinstance(settings_payload, dict):
                scanner_default = settings_payload.get("default") if isinstance(settings_payload.get("default"), dict) else {}
            scanner_ports_info = []
            for port in scanner_ports:
                key = port.lower()
                owners = ports_map.get(key) or []
                device_items = devices_map.get(key) or []
                if not owners:
                    if device_items:
                        device_lines = []
                        for device in device_items:
                            name = str(device.get("name") or device.get("caption") or device.get("description") or "").strip()
                            status = str(device.get("status") or "").strip()
                            error_code = device.get("error_code")
                            service = str(device.get("service") or "").strip()
                            line_parts = [name] if name else []
                            if status:
                                line_parts.append(status)
                            if error_code is not None and error_code != "":
                                line_parts.append(f"код {error_code}")
                            if service:
                                line_parts.append(f"drv {service}")
                            device_lines.append(" · ".join(line_parts) if line_parts else "устройство")
                        scanner_ports_info.append({"port": port, "status": " | ".join(device_lines)})
                    else:
                        scanner_ports_info.append({"port": port, "status": "свободен (по данным агентов)"})
                    continue
                parts = []
                if device_items:
                    for device in device_items:
                        name = str(device.get("name") or device.get("caption") or device.get("description") or "").strip()
                        status = str(device.get("status") or "").strip()
                        error_code = device.get("error_code")
                        service = str(device.get("service") or "").strip()
                        line_parts = [name] if name else []
                        if status:
                            line_parts.append(status)
                        if error_code is not None and error_code != "":
                            line_parts.append(f"код {error_code}")
                        if service:
                            line_parts.append(f"drv {service}")
                        if line_parts:
                            parts.append(" · ".join(line_parts))
                for owner in owners:
                    title = owner.get("title") or owner.get("agent_id") or "агент"
                    state = "нет связи"
                    if owner.get("is_online"):
                        if owner.get("com_connected"):
                            state = "подключен"
                        elif owner.get("com_enabled"):
                            state = "включен"
                        else:
                            state = "выключен"
                    label = f"{title} · {state}"
                    if owner.get("com_error"):
                        label += f" · ошибка: {owner.get('com_error')}"
                        if owner.get("com_error_type"):
                            label += f" ({owner.get('com_error_type')})"
                        if owner.get("com_error_code") is not None:
                            label += f" [{owner.get('com_error_code')}]"
                    parts.append(label)
                scanner_ports_info.append({"port": port, "status": "; ".join(parts)})
        except DatabaseError:
            scanner_agents = []
            scanner_ports = []
            scanner_default = {}
            scanner_ports_info = []
            agent_devices_map = {}
        ctx.update(
            {
                "order_id": order_id,
                "client_label": client_label,
                "client_prefix": client_prefix,
                "status_label": processing_views._status_label_from_entry(status_entry) if status_entry else "-",
                "cabinet_url": resolve_cabinet_url(get_request_role(request)),
                "goods_type": goods_type,
                "goods_type_label": processing_views.GOODS_TYPE_LABELS.get(goods_type, ""),
                "cz_required": cz_required,
                "items": display_items,
                "barcode_map": barcode_map,
                "catalog_items": catalog_items,
                "flow_state": flow_state,
                "flow_state_shared": flow_state_shared,
                "flow_locked": flow_locked,
                "flow_reopened": flow_reopened,
                "act_print_url": act_print_url,
                "scanner_agents": scanner_agents,
                "scanner_ports": scanner_ports,
                "scanner_ports_info": scanner_ports_info,
                "scanner_agent_devices_json": json.dumps(agent_devices_map, ensure_ascii=False),
                "scanner_default": scanner_default,
                "scanner_eols": SCANNER_EOLS,
                "ok": ok,
                "error": error,
            }
        )
        return ctx

    @classmethod
    def build_processing_work_page_context(cls, *, order_id, entries, payload, agency, request, error: str | None = None) -> dict:
        processing_views = cls._views()
        ctx: dict = {}
        work_payload = dict(payload or {})
        marking_qty_value = processing_views._parse_qty_value(work_payload.get("marking_5840_qty")) or 0
        marking_each_qty_value = processing_views._parse_qty_value(work_payload.get("marking_5840_each_qty")) or 0
        printed_cz_base_qs = None
        if order_id and marking_each_qty_value:
            printed_cz_base_qs = MarkingCode.objects.filter(
                order_type="processing",
                order_id=order_id,
                printed_at__isnull=False,
            )
            if agency:
                printed_cz_base_qs = printed_cz_base_qs.filter(agency=agency)

        cards_payload = work_payload.get("cards")
        if isinstance(cards_payload, list) and cards_payload:
            enriched_cards = []
            for raw_card in cards_payload:
                if not isinstance(raw_card, dict):
                    enriched_cards.append(raw_card)
                    continue
                card_payload = dict(raw_card)
                card_rows = card_payload.get("rows") if isinstance(card_payload.get("rows"), list) else []
                card_payload["printed_cz_total"] = cls._printed_cz_total_for_rows(printed_cz_base_qs, card_rows)
                card_payload["marking_5840_qty"] = marking_qty_value
                card_payload["marking_5840_each_qty"] = marking_each_qty_value
                enriched_cards.append(card_payload)
            work_payload["cards"] = enriched_cards
        else:
            fallback_rows = work_payload.get("stock_rows") if isinstance(work_payload.get("stock_rows"), list) else []
            work_payload["printed_cz_total"] = cls._printed_cz_total_for_rows(printed_cz_base_qs, fallback_rows)
            work_payload["marking_5840_qty"] = marking_qty_value
            work_payload["marking_5840_each_qty"] = marking_each_qty_value
        payload = work_payload

        status_label = WarehouseGoodsStateResolver.resolve_for_processing_order(
            order_id=str(order_id or ""),
            agency=agency,
            payload=payload,
        ).label_for("processing")
        ctx["submitted"] = False
        ctx["draft_saved"] = False
        ctx["error"] = error or request.GET.get("error")
        ctx["cabinet_url"] = resolve_cabinet_url(get_request_role(request))
        ctx["order_number"] = order_id or ""
        ctx["agency"] = agency
        ctx["client_view"] = False
        ctx["draft_order_id"] = ""
        ctx["edit_order_id"] = ""
        ctx["draft_payload"] = payload
        ctx["draft_payload_json"] = json.dumps(payload or {}, ensure_ascii=True)
        ctx["status_label"] = status_label or "Обработка товара"

        processed_cards, placed_cards = processing_views._processing_card_sets(payload)
        ready_cards = processed_cards - placed_cards
        cards_total_count = processing_views._processing_cards_total(payload)
        cards_done_count = len(processed_cards)
        all_cards_processed = cards_total_count <= 0 or cards_done_count >= cards_total_count
        results_ready_for_flow = processing_views._processing_results_are_ready(payload, include_shipping=False)
        results_ready_card_ids = processing_views._processing_results_ready_card_ids(payload, include_shipping=False)
        cards_payload_for_flags = payload.get("cards")
        if isinstance(cards_payload_for_flags, list):
            for card in cards_payload_for_flags:
                if not isinstance(card, dict):
                    continue
                card_key = processing_views._processing_card_id(card).strip().lower()
                card["results_ready_for_flow"] = bool(card_key and card_key in results_ready_card_ids)
        results = payload.get("processing_results") or []
        has_ready_results = False
        if isinstance(results, list):
            for row in results:
                if not isinstance(row, dict):
                    continue
                processed_qty = processing_views._parse_qty_value(row.get("processed")) or 0
                shipped_qty = processing_views._parse_qty_value(row.get("shipped_qty")) or 0
                if processed_qty - shipped_qty > 0:
                    has_ready_results = True
                    break
        ctx["can_place_processed"] = bool(ready_cards) or has_ready_results
        ctx["processed_cards_count"] = len(processed_cards)
        ctx["placed_cards_count"] = len(placed_cards)
        ctx["ready_cards_count"] = len(ready_cards)
        ctx["processing_cards_total_count"] = cards_total_count
        ctx["processing_cards_done_count"] = cards_done_count
        ctx["processing_all_cards_processed"] = all_cards_processed
        ctx["processing_results_ready_for_flow"] = results_ready_for_flow
        ctx["processing_results_ready_card_ids"] = sorted(results_ready_card_ids)

        marking_items = extract_processing_items(payload)
        counts = (
            MarkingCode.objects.filter(order_type="processing", order_id=order_id, used_at__isnull=False)
            .values("sku_code", "size")
            .annotate(count=Count("id"))
        )
        counts_map = {(item["sku_code"], item["size"] or ""): item["count"] for item in counts}
        total_count = 0
        for item in marking_items:
            key = (item["sku_code"], item["size"] or "")
            scanned = counts_map.get(key, 0)
            expected = item.get("qty") or 0
            item["scanned"] = scanned
            item["remaining"] = max(expected - scanned, 0)
            total_count += scanned
        ctx["marking_items"] = marking_items
        ctx["marking_total_count"] = total_count
        ctx["marking_api_base"] = f"/marking/processing/{order_id}/"
        ctx["can_assign_packaging"] = get_request_role(request) in {
            "processing_head",
            "head_manager",
            "director",
            "admin",
        }
        ctx["processing_workers"] = list(
            Employee.objects.filter(role="processing_worker", is_active=True).order_by("full_name")
        )
        assign_status = request.GET.get("assign")
        assign_error = request.GET.get("assign_error")
        placement_completed = processing_views._flow_closed_from_entries(entries)
        ctx["placement_completed"] = placement_completed
        ctx["placement_change_url"] = f"/orders/processing/{order_id}/flow/"
        processing_flow_blockers: list[str] = []
        if not all_cards_processed:
            if cards_total_count > 0:
                processing_flow_blockers.append(
                    f"Карты обработки завершены не полностью: {cards_done_count} из {cards_total_count}."
                )
            else:
                processing_flow_blockers.append("Завершите карту обработки по товарам.")
        if not results_ready_for_flow:
            processing_flow_blockers.append(
                "Заполните результаты карты обработки по всем строкам (кроме этапа раскоробовки)."
            )
        ctx["processing_flow_blockers"] = processing_flow_blockers
        ctx["can_open_processing_flow"] = bool(order_id and not placement_completed and not processing_flow_blockers)
        pending_packaging_assignment, auto_assign_status = processing_views._processing_auto_dispatch_pending_packaging(
            order_id=str(order_id),
            entries=entries,
            can_open_processing_flow=bool(ctx["can_open_processing_flow"]),
            placement_completed=placement_completed,
        )
        if auto_assign_status == "auto_dispatched" and not assign_status:
            assign_status = "auto_dispatched"
        if auto_assign_status == "invalid_worker" and not assign_error:
            assign_error = "invalid_worker"
        ctx["assign_status"] = assign_status
        ctx["assign_error"] = assign_error
        ctx["pending_packaging_assignment"] = pending_packaging_assignment
        ctx["packaging_tasks"] = list(
            Task.objects.select_related("assigned_to")
            .filter(
                route=processing_views._processing_packaging_task_route(str(order_id)),
                assigned_to__role="processing_worker",
            )
            .exclude(status="done")
            .order_by("-created_at")
        )

        finish_checks = processing_views._processing_finish_checks(str(order_id), payload, entries)
        finish_blockers = finish_checks.get("blockers") or []
        warehouse_move_progress = finish_checks.get("warehouse_move_progress") or {}
        placement_payload = finish_checks.get("placement_payload") or {}
        placement_pallets = placement_payload.get("act_pallets") or []
        if not isinstance(placement_pallets, list):
            placement_pallets = []
        suggested_destinations = processing_views._suggest_processing_warehouse_destinations(
            placement_pallets,
            exclude_order_type="processing",
            exclude_order_id=order_id,
            agency_id=agency.id if agency else None,
        )
        warehouse_move_rows = cls._build_processing_warehouse_move_rows(
            placement_pallets=placement_pallets,
            suggested_destinations=suggested_destinations,
            warehouse_move_progress=warehouse_move_progress,
        )
        role = get_request_role(request)
        ctx["finish_blockers"] = finish_blockers
        ctx["finish_ready"] = not finish_blockers
        ctx["warehouse_move_progress"] = warehouse_move_progress
        ctx["warehouse_total_pallets"] = int(warehouse_move_progress.get("total_pallets") or 0)
        ctx["warehouse_done_pallets"] = int(warehouse_move_progress.get("done_count") or 0)
        ctx["warehouse_created_pallets"] = int(warehouse_move_progress.get("created_count") or 0)
        ctx["warehouse_in_progress_pallets"] = int(warehouse_move_progress.get("in_progress_count") or 0)
        ctx["warehouse_active_pallets"] = int(warehouse_move_progress.get("active_count") or 0)
        ctx["warehouse_canceled_pallets"] = int(warehouse_move_progress.get("canceled_count") or 0)
        ctx["warehouse_not_created_pallets"] = int(warehouse_move_progress.get("not_created_count") or 0)
        ctx["warehouse_pending_pallets"] = int(warehouse_move_progress.get("pending_count") or 0)
        ctx["warehouse_assigned_pallets"] = max(0, ctx["warehouse_total_pallets"] - ctx["warehouse_not_created_pallets"])
        ctx["warehouse_move_rows"] = warehouse_move_rows
        ctx["warehouse_destination_summary"] = cls._warehouse_destination_summary(
            warehouse_move_rows,
            canceled_count=ctx["warehouse_canceled_pallets"],
            not_created_count=ctx["warehouse_not_created_pallets"],
        )
        ctx["warehouse_move_created"] = bool(finish_checks.get("warehouse_move_created"))
        ctx["warehouse_move_completed"] = bool(finish_checks.get("warehouse_move_completed"))
        ctx["can_send_to_warehouse_action"] = bool(
            role in {"storekeeper", "processing_head"}
            and finish_checks.get("placement_closed")
            and finish_checks.get("has_boxes")
            and finish_checks.get("has_pallets")
            and not finish_checks.get("warehouse_move_completed")
            and int(warehouse_move_progress.get("not_created_count") or 0) > 0
        )
        ctx["can_cancel_warehouse_action"] = bool(
            role in {"storekeeper", "processing_head"}
            and finish_checks.get("placement_closed")
            and not finish_checks.get("warehouse_move_completed")
            and int(warehouse_move_progress.get("active_count") or 0) > 0
        )
        ctx["warehouse_move_status"] = (request.GET.get("warehouse_move") or "").strip().lower()
        ctx["warehouse_move_created_count"] = processing_views._parse_int_value(request.GET.get("warehouse_created")) or 0
        ctx["warehouse_move_skipped_count"] = processing_views._parse_int_value(request.GET.get("warehouse_skipped")) or 0
        ctx["warehouse_move_missing_count"] = processing_views._parse_int_value(request.GET.get("warehouse_missing")) or 0
        ctx["warehouse_move_canceled_count"] = processing_views._parse_int_value(request.GET.get("warehouse_canceled")) or 0
        return ctx

    @classmethod
    def send_processing_to_warehouse(cls, *, order_id: str, entries, request) -> ProcessingWarehouseMoveActionResult:
        processing_views = cls._views()
        payload = dict(entries[-1].payload or {})
        finish_checks = processing_views._processing_finish_checks(str(order_id), payload, entries)
        if (
            not finish_checks.get("placement_closed")
            or not finish_checks.get("has_boxes")
            or not finish_checks.get("has_pallets")
        ):
            return ProcessingWarehouseMoveActionResult(
                status="not_ready",
                error_message=(
                    "Нельзя отправить на склад: сначала завершите раскоробовку "
                    "и закройте акт размещения с палетами."
                ),
            )
        destinations_by_pallet, destination_error = processing_views._parse_processing_warehouse_destinations(
            request.POST.get("warehouse_destinations_json")
        )
        if destination_error:
            return ProcessingWarehouseMoveActionResult(
                status="invalid_destination",
                error_message=destination_error,
            )
        created, skipped_existing, skipped_missing = processing_views._create_processing_warehouse_moves(
            str(order_id),
            entries,
            request,
            destinations_by_pallet=destinations_by_pallet,
        )
        if created <= 0:
            if skipped_missing > 0 and skipped_existing <= 0:
                return ProcessingWarehouseMoveActionResult(
                    status="missing_destination",
                    error_message="Для выбранных палет не подтверждено место хранения.",
                    skipped_existing_count=skipped_existing,
                    skipped_missing_count=skipped_missing,
                )
            if skipped_existing > 0 and skipped_missing <= 0:
                return ProcessingWarehouseMoveActionResult(
                    status="exists",
                    error_message="Задания на склад по выбранным палетам уже существуют.",
                    skipped_existing_count=skipped_existing,
                    skipped_missing_count=skipped_missing,
                )
            return ProcessingWarehouseMoveActionResult(
                status="blocked",
                error_message=(
                    "Не удалось сформировать задания: часть палет уже имеет задание, "
                    "для остальных не выбрано место хранения."
                ),
                skipped_existing_count=skipped_existing,
                skipped_missing_count=skipped_missing,
            )
        return ProcessingWarehouseMoveActionResult(
            status="ok",
            created_count=created,
            skipped_existing_count=skipped_existing,
            skipped_missing_count=skipped_missing,
        )

    @classmethod
    def cancel_processing_warehouse_moves(cls, *, order_id: str, entries, request) -> ProcessingWarehouseMoveActionResult:
        processing_views = cls._views()
        canceled, skipped = processing_views._cancel_processing_warehouse_moves(str(order_id), entries, request)
        if canceled <= 0:
            return ProcessingWarehouseMoveActionResult(
                status="cancel_none",
                canceled_count=0,
                skipped_count=skipped,
            )
        return ProcessingWarehouseMoveActionResult(
            status="canceled",
            canceled_count=canceled,
            skipped_count=skipped,
        )

    @classmethod
    def finish_processing(cls, *, order_id: str, entries, request, role: str) -> ProcessingFinishResult:
        processing_views = cls._views()
        latest = entries[-1] if entries else None
        payload = processing_views._processing_work_payload_from_entries(entries)
        status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
        status_label = (payload.get("status_label") or "").lower()
        if status_value in {"done", "completed", "closed", "finished"} or "выполн" in status_label:
            return ProcessingFinishResult(status="already_done")

        finish_checks = processing_views._processing_finish_checks(str(order_id), payload, entries)
        finish_blockers = finish_checks.get("blockers") or []
        if finish_blockers:
            blockers_text = " ".join(f"{idx + 1}) {message}" for idx, message in enumerate(finish_blockers))
            return ProcessingFinishResult(
                status="blocked",
                error_message=f"Нельзя завершить заявку. Не выполнены условия: {blockers_text}",
            )

        placement_payload = finish_checks.get("placement_payload") or {}
        payload["goods_type"] = "gv"
        payload["goods_type_label"] = processing_views.GOODS_TYPE_LABELS.get("gv", "Готовый")
        mismatch_rows = processing_views._processing_discrepancy_rows(payload, placement_payload)
        if mismatch_rows:
            now_iso = timezone.localtime().isoformat()
            current_discrepancy_status = str(payload.get("discrepancy_status") or "").strip().lower()
            can_approve_discrepancy = role == "processing_head" and current_discrepancy_status == "reported"
            payload["discrepancy_detected"] = True
            payload["discrepancy_items"] = mismatch_rows
            payload["discrepancy_act_label"] = "Акт разногласий по обработке"
            payload["discrepancy_act_created_at"] = payload.get("discrepancy_act_created_at") or now_iso
            if not can_approve_discrepancy:
                payload["discrepancy_status"] = "reported"
                payload["discrepancy_reported_at"] = payload.get("discrepancy_reported_at") or now_iso
                payload["status"] = "processing_in_work"
                payload["status_label"] = "Разногласия — на утверждении руководителя обработки"
                processing_views._create_processing_discrepancy_task(order_id, latest.agency, request, mismatch_rows)
                log_order_action(
                    "status",
                    order_id=order_id,
                    order_type="processing",
                    user=request.user if request.user.is_authenticated else None,
                    agency=latest.agency if latest else None,
                    description="Разногласия по обработке переданы на утверждение руководителю",
                    payload=payload,
                )
                log_order_action(
                    "update",
                    order_id=order_id,
                    order_type="processing",
                    user=request.user if request.user.is_authenticated else None,
                    agency=latest.agency if latest else None,
                    description="Создан акт разногласий по обработке",
                    payload={
                        "act": "processing_discrepancy",
                        "act_label": "Акт разногласий по обработке",
                        "order_id": str(order_id),
                        "discrepancy_items": mismatch_rows,
                        "created_at": now_iso,
                    },
                )
                if role == "processing_head":
                    return ProcessingFinishResult(
                        status="discrepancy_reported_head",
                        error_message=(
                            "Выявлены разногласия. Отчет сформирован. "
                            "Подтвердите разногласия и повторите завершение заявки."
                        ),
                    )
                return ProcessingFinishResult(
                    status="discrepancy_reported_wait",
                    error_message=(
                        "Выявлены разногласия. Отчет отправлен руководителю обработки. "
                        "Заявка закроется после его утверждения."
                    ),
                )
            payload["discrepancy_status"] = "approved"
            payload["discrepancy_approved_at"] = now_iso
            if request.user and request.user.is_authenticated:
                payload["discrepancy_approved_by"] = (
                    request.user.get_full_name().strip() or request.user.username or str(request.user)
                )
            log_order_action(
                "update",
                order_id=order_id,
                order_type="processing",
                user=request.user if request.user.is_authenticated else None,
                agency=latest.agency if latest else None,
                description="Разногласия по обработке утверждены руководителем обработки",
                payload={
                    "act": "processing_discrepancy_approval",
                    "order_id": str(order_id),
                    "approved_at": now_iso,
                    "discrepancy_items": mismatch_rows,
                },
            )
            Task.objects.filter(
                route=f"/orders/processing/{order_id}/",
                assigned_to__role="processing_head",
                title=f"Разногласие по обработке №{order_id}",
            ).exclude(status="done").update(status="done")

        MarkingCode.objects.filter(
            order_type="processing",
            order_id=order_id,
            used_at__isnull=True,
        ).update(order_id="", printed_at=None, printed_by=None)
        if latest and latest.agency:
            WarehouseWritePathService.replace_processing_reserves(
                agency=latest.agency,
                order_id=str(order_id),
                items=[],
            )
        payload["status"] = "done"
        payload["status_label"] = "Выполнена"
        payload["completed_at"] = timezone.localtime().isoformat()
        log_order_action(
            "status",
            order_id=order_id,
            order_type="processing",
            user=request.user if request.user.is_authenticated else None,
            agency=latest.agency if latest else None,
            description="Обработка завершена",
            payload=payload,
        )
        Task.objects.filter(route=f"/orders/processing/{order_id}/").exclude(status="done").update(status="done")
        if latest and latest.agency:
            WarehouseWritePathService.complete_processing_if_started(
                agency=latest.agency,
                order_id=str(order_id),
                performed_by=request.user if request.user.is_authenticated else None,
                performed_by_role=role,
            )
        return ProcessingFinishResult(status="done")

    @classmethod
    def save_processing_flow_draft(
        cls,
        *,
        order_id: str,
        entries,
        request,
        normalize_flow_state,
        can_reassign_boxes: bool,
    ) -> ProcessingFlowDraftResult:
        processing_views = cls._views()
        role = get_request_role(request)
        if role not in {"storekeeper", "processing_head", "processing_worker", "head_manager", "director", "admin", "manager"}:
            return ProcessingFlowDraftResult(status="forbidden")
        if processing_views._flow_closed_from_entries(entries):
            return ProcessingFlowDraftResult(status="closed")
        if not processing_views.ProcessingFlowView()._can_start(entries):
            return ProcessingFlowDraftResult(status="not_allowed")

        boxes_raw = request.POST.get("boxes_json") or "[]"
        pallets_raw = request.POST.get("pallets_json") or "[]"
        active_box = request.POST.get("active_box") or ""
        active_pallet = request.POST.get("active_pallet") or ""
        try:
            boxes_data = json.loads(boxes_raw)
            pallets_data = json.loads(pallets_raw)
        except json.JSONDecodeError:
            return ProcessingFlowDraftResult(status="invalid_json")
        if not isinstance(boxes_data, list):
            boxes_data = []
        if not isinstance(pallets_data, list):
            pallets_data = []
        agent_id = (request.POST.get("agent_id") or "").strip()
        if not agent_id:
            return ProcessingFlowDraftResult(status="missing_agent_id")
        session = processing_views._flow_session_for_request(order_id, agent_id, request, create=True)
        if not session:
            return ProcessingFlowDraftResult(status="session_not_found")
        boxes_data = processing_views._filter_flow_values_for_session(boxes_data, session)
        pallets_data = processing_views._filter_flow_values_for_session(pallets_data, session)
        boxes_data = processing_views._apply_flow_owner(boxes_data, session)
        pallets_data = processing_views._apply_flow_owner(pallets_data, session)
        flow_state = normalize_flow_state(boxes_data, pallets_data, active_box, active_pallet)
        flow_state = processing_views._sanitize_flow_state_for_session(flow_state, session)
        if not can_reassign_boxes:
            sessions = list(
                ProcessingFlowSession.objects.filter(
                    order_id=order_id,
                    order_type="processing",
                    status=ProcessingFlowSession.STATUS_OPEN,
                )
            )
            merged_boxes, merged_pallets = processing_views._merge_flow_sessions(sessions) if sessions else ([], [])
            candidate_boxes = processing_views._merge_flow_values_by_code(merged_boxes, flow_state.get("boxes") or [])
            candidate_pallets = processing_views._merge_flow_values_by_code(merged_pallets, flow_state.get("pallets") or [])
            candidate_boxes, candidate_pallets = processing_views._dedupe_pallet_box_links(candidate_boxes, candidate_pallets)
            if processing_views._has_box_reassignment_between_pallets(merged_pallets, candidate_pallets):
                return ProcessingFlowDraftResult(status="box_move_head_only")
        session.flow_state = flow_state
        session.last_seen = timezone.localtime()
        session.status = ProcessingFlowSession.STATUS_OPEN
        session.save(update_fields=["flow_state", "last_seen", "status", "updated_at"])
        return ProcessingFlowDraftResult(status="ok", session_id=session.id)

    @classmethod
    def reopen_processing_flow(
        cls,
        *,
        order_id: str,
        entries,
        request,
        normalize_flow_state,
    ) -> ProcessingFlowReopenResult:
        processing_views = cls._views()
        can_finish = (
            request.user.is_authenticated
            and Employee.objects.filter(user=request.user, is_active=True, role="processing_head").exists()
        )
        if not can_finish:
            return ProcessingFlowReopenResult(status="forbidden")
        if not processing_views._flow_closed_from_entries(entries):
            return ProcessingFlowReopenResult(status="not_closed")
        latest = entries[-1] if entries else None
        closed_entry = next((entry for entry in reversed(entries) if (entry.payload or {}).get("flow_closed")), None)
        closed_payload = closed_entry.payload if closed_entry else {}
        snapshot = {
            "order_id": order_id,
            "flow_closed_at": closed_payload.get("flow_closed_at"),
        }
        log_staff_overaction(
            "update",
            user=request.user if request.user.is_authenticated else None,
            agency=latest.agency if latest else None,
            description=f"Избыточное действие: повторное открытие размещения после обработки (заявка {order_id})",
            snapshot=snapshot,
        )
        placement_entry = processing_views.ProcessingFlowView()._placement_act_entry(entries)
        placement_payload = placement_entry.payload if placement_entry else {}
        placement_boxes = placement_payload.get("act_boxes") if isinstance(placement_payload, dict) else []
        placement_pallets = placement_payload.get("act_pallets") if isinstance(placement_payload, dict) else []
        if not isinstance(placement_boxes, list):
            placement_boxes = []
        if not isinstance(placement_pallets, list):
            placement_pallets = []
        reopen_flow_state = normalize_flow_state(placement_boxes, placement_pallets, "", "")
        reopen_now = timezone.localtime()
        with transaction.atomic():
            ProcessingFlowSession.objects.filter(
                order_id=order_id,
                order_type="processing",
                status=ProcessingFlowSession.STATUS_OPEN,
            ).update(
                status=ProcessingFlowSession.STATUS_CLOSED,
                last_seen=reopen_now,
            )
            seed_session, created = ProcessingFlowSession.objects.get_or_create(
                order_id=order_id,
                order_type="processing",
                agent_id="__reopen_snapshot__",
                defaults={
                    "status": ProcessingFlowSession.STATUS_OPEN,
                    "flow_state": reopen_flow_state,
                    "last_seen": reopen_now,
                },
            )
            if not created:
                seed_session.flow_state = reopen_flow_state
                seed_session.status = ProcessingFlowSession.STATUS_OPEN
                seed_session.last_seen = reopen_now
                seed_session.save(update_fields=["flow_state", "status", "last_seen", "updated_at"])
        log_order_action(
            "update",
            order_id=order_id,
            order_type="processing",
            user=request.user if request.user.is_authenticated else None,
            agency=latest.agency if latest else None,
            description="Повторное открытие размещения после обработки",
            payload={
                "flow_reopened": True,
                "flow_reopened_at": timezone.localtime().isoformat(),
                "flow_state": reopen_flow_state,
            },
        )
        return ProcessingFlowReopenResult(status="ok")

    @classmethod
    def get_processing_flow_session(cls, *, order_id: str, request) -> ProcessingFlowSessionResult:
        processing_views = cls._views()
        agent_id = (request.GET.get("agent_id") or "").strip()
        if not agent_id:
            return ProcessingFlowSessionResult(status="missing_agent_id")
        session = processing_views._flow_session_for_request(order_id, agent_id, request, create=True)
        if not session:
            return ProcessingFlowSessionResult(status="session_not_found")
        flow_state = processing_views._sanitize_flow_state_for_session(session.flow_state, session)
        return ProcessingFlowSessionResult(
            status="ok",
            session_id=session.id,
            flow_state=flow_state,
            updated_at=session.updated_at.isoformat() if session.updated_at else "",
        )

    @classmethod
    def get_processing_flow_shared_state(cls, *, order_id: str) -> ProcessingFlowSharedResult:
        sessions = list(
            ProcessingFlowSession.objects.filter(
                order_id=order_id,
                order_type="processing",
                status=ProcessingFlowSession.STATUS_OPEN,
            )
        )
        processing_views = cls._views()
        boxes_data, pallets_data = processing_views._merge_flow_sessions(sessions) if sessions else ([], [])
        if not boxes_data and not pallets_data:
            entries = list(
                OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").order_by("created_at")
            )
            reopened = any((entry.payload or {}).get("flow_reopened") for entry in entries or [])
            if reopened:
                placement_entry = next(
                    (entry for entry in reversed(entries) if (entry.payload or {}).get("act") == "placement"),
                    None,
                )
                if placement_entry:
                    placement_payload = placement_entry.payload or {}
                    placement_boxes = placement_payload.get("act_boxes") or []
                    placement_pallets = placement_payload.get("act_pallets") or []
                    if isinstance(placement_boxes, list):
                        boxes_data = placement_boxes
                    if isinstance(placement_pallets, list):
                        pallets_data = placement_pallets
        return ProcessingFlowSharedResult(
            status="ok",
            flow_state={"boxes": boxes_data, "pallets": pallets_data},
        )

    @classmethod
    def log_processing_flow_box_action(cls, *, order_id: str, request) -> ProcessingFlowBoxActionResult:
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = request.POST.dict()
        if not isinstance(payload, dict):
            payload = {}
        action = (payload.get("action") or "").lower()
        if action not in {"edit", "delete"}:
            return ProcessingFlowBoxActionResult(status="invalid_action")
        box_code = (payload.get("box_code") or "").strip()
        if not box_code:
            return ProcessingFlowBoxActionResult(status="missing_box")
        entry = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .select_related("agency")
            .order_by("-created_at")
            .first()
        )
        snapshot = {
            "order_id": order_id,
            "box_code": box_code,
            "pallet_code": (payload.get("pallet_code") or "").strip(),
            "pallet_index": cls._views()._parse_int_value(payload.get("pallet_index")),
            "box_index": cls._views()._parse_int_value(payload.get("box_index")),
            "total_qty": cls._views()._parse_int_value(payload.get("total_qty")),
            "items": payload.get("items") if isinstance(payload.get("items"), list) else [],
            "previous_active_box": (payload.get("previous_active_box") or "").strip(),
            "new_active_box": (payload.get("new_active_box") or "").strip(),
        }
        action_label = "Редактирование короба" if action == "edit" else "Удаление короба"
        description = f"{action_label} {box_code} (заявка {order_id})"
        if snapshot["pallet_index"] and snapshot["box_index"]:
            description += f", палета {snapshot['pallet_index']}, короб {snapshot['box_index']}"
        log_staff_overaction(
            "update" if action == "edit" else "delete",
            user=request.user if request.user.is_authenticated else None,
            agency=entry.agency if entry else None,
            description=description,
            snapshot=snapshot,
        )
        return ProcessingFlowBoxActionResult(status="ok")

    @classmethod
    def complete_processing_flow(
        cls,
        *,
        order_id: str,
        entries,
        request,
        normalize_flow_state,
        items_from_placement_act,
        can_start: bool,
        can_finish_with_mismatch: bool,
        can_reassign_boxes: bool,
        order_type: str = "processing",
    ) -> ProcessingFlowCompletionResult:
        processing_views = cls._views()
        if not can_start:
            return ProcessingFlowCompletionResult(status="error", error_code="cannot_start")

        status_entry = processing_views._current_status_entry(entries)
        latest = entries[-1]
        base_payload = dict((status_entry.payload or latest.payload or {}))
        processed_cards, placed_cards = processing_views._processing_card_sets(base_payload)
        ready_cards = processed_cards - placed_cards if processed_cards else set()
        act_items = processing_views._processing_receiving_items(
            base_payload,
            latest.agency_id,
            ready_cards if ready_cards else None,
        )
        if not act_items:
            act_items = items_from_placement_act(entries)
        if not act_items:
            return ProcessingFlowCompletionResult(status="error", error_code="no_act_items")

        boxes_raw = request.POST.get("boxes_json") or "[]"
        pallets_raw = request.POST.get("pallets_json") or "[]"
        try:
            boxes_data = json.loads(boxes_raw)
            pallets_data = json.loads(pallets_raw)
        except json.JSONDecodeError:
            return ProcessingFlowCompletionResult(status="error", error_code="invalid_json")
        if not isinstance(boxes_data, list):
            boxes_data = []
        if not isinstance(pallets_data, list):
            pallets_data = []
        submitted_box_directions: dict[str, str] = {}
        for raw_box in boxes_data:
            if not isinstance(raw_box, dict):
                continue
            raw_code = str(raw_box.get("code") or "").strip()
            if not raw_code:
                continue
            raw_direction = str(raw_box.get("direction") or raw_box.get("direction_name") or "").strip()
            if raw_direction:
                submitted_box_directions[raw_code] = raw_direction
        submitted_pallet_directions: dict[str, str] = {}
        for raw_pallet in pallets_data:
            if not isinstance(raw_pallet, dict):
                continue
            raw_code = str(raw_pallet.get("code") or "").strip()
            if not raw_code:
                continue
            raw_direction = str(raw_pallet.get("direction") or raw_pallet.get("direction_name") or "").strip()
            if raw_direction:
                submitted_pallet_directions[raw_code] = raw_direction

        sessions = list(
            ProcessingFlowSession.objects.filter(
                order_id=order_id,
                order_type="processing",
                status=ProcessingFlowSession.STATUS_OPEN,
            )
        )
        merged_boxes = []
        merged_pallets = []
        if sessions:
            merged_boxes, merged_pallets = processing_views._merge_flow_sessions(sessions)
            boxes_data = processing_views._merge_flow_values_by_code(merged_boxes, boxes_data)
            pallets_data = processing_views._merge_flow_values_by_code(merged_pallets, pallets_data)
        boxes_data, pallets_data = processing_views._dedupe_pallet_box_links(boxes_data, pallets_data)

        if processing_views._state_has_open_box_with_items({"boxes": boxes_data}):
            return ProcessingFlowCompletionResult(status="error", error_code="open_boxes")

        def normalize_items(raw_items):
            items = []
            for raw in raw_items or []:
                if not isinstance(raw, dict):
                    continue
                qty = processing_views._parse_qty_value(raw.get("qty")) or 0
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

        cleaned_boxes = []
        seen_box_codes = set()
        for idx, box in enumerate(boxes_data):
            if not isinstance(box, dict):
                continue
            items = normalize_items(box.get("items") or [])
            if not items:
                continue
            code = str(box.get("code") or "").strip() or f"BOX-{idx + 1}"
            if code in seen_box_codes:
                code = f"{code}-{idx + 1}"
            seen_box_codes.add(code)
            owner_agent_id = str(box.get("owner_agent_id") or "").strip()
            owner_user_id = box.get("owner_user_id") or ""
            owner_user_label = str(box.get("owner_user_label") or "").strip()
            fixed_label = str(box.get("fixed_label") or "").strip()
            direction_name = str(box.get("direction") or box.get("direction_name") or "").strip()
            if not direction_name:
                direction_name = submitted_box_directions.get(code, "")
            cleaned_boxes.append(
                {
                    "code": code,
                    "items": items,
                    "sealed": True,
                    "fixed_label": fixed_label,
                    "direction": direction_name,
                    "owner_agent_id": owner_agent_id,
                    "owner_user_id": owner_user_id,
                    "owner_user_label": owner_user_label,
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
            items = normalize_items(pallet.get("items") or [])
            if not boxes and not items:
                continue
            location = pallet.get("location")
            if isinstance(location, dict):
                location = dict(location)
            elif isinstance(location, str):
                location = {"zone": location}
            else:
                location = {}
            location["zone"] = "OBR"
            owner_agent_id = str(pallet.get("owner_agent_id") or "").strip()
            owner_user_id = pallet.get("owner_user_id") or ""
            owner_user_label = processing_views._resolve_actor_label(
                pallet.get("owner_user_label"),
                owner_agent_id,
                owner_user_id,
                unknown="",
            )
            closed_by_agent_id = str(pallet.get("closed_by_agent_id") or "").strip()
            closed_by_user_id = pallet.get("closed_by_user_id") or ""
            closed_by_user_label = processing_views._resolve_actor_label(
                pallet.get("closed_by_user_label"),
                closed_by_agent_id,
                closed_by_user_id,
                unknown="",
            )
            if not closed_by_user_label:
                closed_by_user_label = processing_views._resolve_actor_label(
                    owner_user_label,
                    owner_agent_id,
                    owner_user_id,
                    unknown="",
                )
            direction_name = str(pallet.get("direction") or pallet.get("direction_name") or "").strip()
            if not direction_name:
                direction_name = submitted_pallet_directions.get(code, "")
            cleaned_pallets.append(
                {
                    "code": code,
                    "boxes": boxes,
                    "items": items,
                    "sealed": True,
                    "direction": direction_name,
                    "location": location,
                    "owner_agent_id": owner_agent_id,
                    "owner_user_id": owner_user_id,
                    "owner_user_label": owner_user_label,
                    "closed_by_agent_id": closed_by_agent_id,
                    "closed_by_user_id": closed_by_user_id,
                    "closed_by_user_label": closed_by_user_label,
                }
            )
        box_direction_by_code = {
            str(entry.get("code") or "").strip(): str(entry.get("direction") or "").strip()
            for entry in cleaned_boxes
            if isinstance(entry, dict) and str(entry.get("code") or "").strip()
        }
        for pallet in cleaned_pallets:
            if not isinstance(pallet, dict):
                continue
            pallet_direction = str(pallet.get("direction") or "").strip()
            if not pallet_direction:
                for box_code in pallet.get("boxes") or []:
                    box_direction = box_direction_by_code.get(str(box_code or "").strip(), "")
                    if box_direction:
                        pallet_direction = box_direction
                        break
                if pallet_direction:
                    pallet["direction"] = pallet_direction
            if not pallet_direction:
                continue
            for box_code in pallet.get("boxes") or []:
                code = str(box_code or "").strip()
                if not code:
                    continue
                if not box_direction_by_code.get(code):
                    box_direction_by_code[code] = pallet_direction
        for entry in cleaned_boxes:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("code") or "").strip()
            if not code:
                continue
            if not str(entry.get("direction") or "").strip() and box_direction_by_code.get(code):
                entry["direction"] = box_direction_by_code.get(code, "")

        if not cleaned_boxes:
            return ProcessingFlowCompletionResult(status="error", error_code="no_boxes")
        if not cleaned_pallets:
            return ProcessingFlowCompletionResult(status="error", error_code="no_pallets")
        if not can_reassign_boxes:
            tracked_box_codes = {
                str(box.get("code") or "").strip()
                for box in cleaned_boxes
                if str(box.get("code") or "").strip()
            }
            if processing_views._has_box_reassignment_between_pallets(merged_pallets, cleaned_pallets, tracked_box_codes):
                return ProcessingFlowCompletionResult(status="error", error_code="box_move_head_only")
        box_label_map = processing_views._build_box_label_map(cleaned_boxes)
        for box in cleaned_boxes:
            if not isinstance(box, dict):
                continue
            box_code = str(box.get("code") or "").strip()
            if not box_code:
                continue
            if not str(box.get("fixed_label") or "").strip():
                box["fixed_label"] = box_label_map.get(box_code, "")

        agent_id = (request.POST.get("agent_id") or "").strip()
        if agent_id:
            session = processing_views._flow_session_for_request(order_id, agent_id, request, create=True)
            if session:
                flow_state = normalize_flow_state(cleaned_boxes, cleaned_pallets, "", "")
                session.flow_state = flow_state
                session.last_seen = timezone.localtime()
                session.status = ProcessingFlowSession.STATUS_OPEN
                session.save(update_fields=["flow_state", "last_seen", "status", "updated_at"])

        pallet_box_codes = set()
        for pallet in cleaned_pallets:
            for box_code in pallet.get("boxes") or []:
                if box_code:
                    pallet_box_codes.add(box_code)
        unassigned_boxes = [box for box in cleaned_boxes if box["code"] not in pallet_box_codes]
        if unassigned_boxes:
            return ProcessingFlowCompletionResult(status="error", error_code="unassigned_boxes")

        totals = {}
        totals_by_sku_size: dict[tuple[str, str], dict] = {}

        def _sku_size_key(item) -> tuple[str, str]:
            sku_value = str(item.get("sku_code") or item.get("sku") or "").strip().lower()
            size_value = str(item.get("size") or "").strip().lower()
            return sku_value, size_value

        def _ensure_total_entry(store, key):
            return store.setdefault(
                key,
                {
                    "total": 0,
                    "box_codes": set(),
                    "pallet_codes": set(),
                },
            )

        def _add_total_qty(item, qty: int):
            if qty <= 0:
                return
            full_key = processing_views._item_key(item.get("sku_code") or item.get("sku"), item.get("name"), item.get("size"))
            sku_size_key = _sku_size_key(item)
            if full_key:
                _ensure_total_entry(totals, full_key)["total"] += qty
            if sku_size_key[0]:
                _ensure_total_entry(totals_by_sku_size, sku_size_key)["total"] += qty

        def _add_container_code(item, container_field: str, code: str):
            code_value = str(code or "").strip()
            if not code_value:
                return
            full_key = processing_views._item_key(item.get("sku_code") or item.get("sku"), item.get("name"), item.get("size"))
            sku_size_key = _sku_size_key(item)
            if full_key:
                _ensure_total_entry(totals, full_key)[container_field].add(code_value)
            if sku_size_key[0]:
                _ensure_total_entry(totals_by_sku_size, sku_size_key)[container_field].add(code_value)

        box_items_by_code: dict[str, list[dict]] = {}
        for box in cleaned_boxes:
            box_code = str(box.get("code") or "").strip()
            box_items = box.get("items") or []
            if box_code:
                box_items_by_code[box_code] = box_items if isinstance(box_items, list) else []
            for item in box.get("items") or []:
                qty = processing_views._parse_qty_value(item.get("qty")) or 0
                if qty <= 0:
                    continue
                _add_total_qty(item, qty)
                _add_container_code(item, "box_codes", box_code)
        for pallet in cleaned_pallets:
            pallet_code = str(pallet.get("code") or "").strip()
            pallet_items = pallet.get("items") or []
            has_direct_items = bool(isinstance(pallet_items, list) and pallet_items)
            if has_direct_items:
                for item in pallet_items:
                    qty = processing_views._parse_qty_value(item.get("qty")) or 0
                    if qty <= 0:
                        continue
                    _add_total_qty(item, qty)
                    _add_container_code(item, "pallet_codes", pallet_code)
                continue
            for box_code in pallet.get("boxes") or []:
                code = str(box_code or "").strip()
                if not code:
                    continue
                for item in box_items_by_code.get(code, []):
                    qty = processing_views._parse_qty_value(item.get("qty")) or 0
                    if qty <= 0:
                        continue
                    _add_container_code(item, "pallet_codes", pallet_code)

        allow_mismatch = str(request.POST.get("allow_mismatch") or "").strip() == "1"
        has_qty_mismatch = False
        placement_items = []
        mismatch_rows = []
        for item in act_items:
            key = processing_views._item_key(item.get("sku_code"), item.get("name"), item.get("size"))
            sku_size_key = (
                str(item.get("sku_code") or item.get("sku") or "").strip().lower(),
                str(item.get("size") or "").strip().lower(),
            )
            entry = totals.get(key)
            if not entry and sku_size_key[0]:
                entry = totals_by_sku_size.get(sku_size_key)
            if not entry:
                entry = {"total": 0, "box_codes": set(), "pallet_codes": set()}
            expected_qty = processing_views._parse_qty_value(item.get("actual_qty")) or 0
            factual_qty = int(entry.get("total") or 0)
            box_count = len(entry.get("box_codes") or ())
            pallet_count = len(entry.get("pallet_codes") or ())
            if factual_qty != expected_qty:
                has_qty_mismatch = True
                mismatch_rows.append(
                    {
                        "sku_code": item.get("sku_code") or "",
                        "name": item.get("name") or "",
                        "size": item.get("size") or "",
                        "expected_qty": expected_qty,
                        "factual_qty": factual_qty,
                        "delta_qty": factual_qty - expected_qty,
                    }
                )
            accepted_qty = factual_qty if allow_mismatch else expected_qty
            placement_item = {
                "sku_code": item.get("sku_code"),
                "name": item.get("name"),
                "size": item.get("size"),
                "actual_qty": accepted_qty,
                "box_qty": box_count,
                "pallet_qty": pallet_count,
            }
            if factual_qty != expected_qty:
                placement_item["expected_qty"] = expected_qty
                placement_item["factual_qty"] = factual_qty
            placement_items.append(placement_item)
        if has_qty_mismatch and not can_finish_with_mismatch:
            processing_views._create_processing_discrepancy_task(order_id, latest.agency, request, mismatch_rows)
            return ProcessingFlowCompletionResult(status="error", error_code="qty_mismatch_head_only")
        if has_qty_mismatch and not allow_mismatch:
            return ProcessingFlowCompletionResult(status="error", error_code="qty_mismatch")
        marking_boxes_snapshot = []
        marking_qs = MarkingCode.objects.filter(
            order_type="processing",
            order_id=order_id,
            used_at__isnull=False,
        )
        missing_marking_boxes = False
        unknown_marking_boxes = False
        for row in marking_qs.values("code", "sku_code", "size", "barcode", "box_barcode"):
            box_code = str(row.get("box_barcode") or "").strip()
            if not box_code:
                missing_marking_boxes = True
                continue
            box_label = box_label_map.get(box_code)
            if not box_label:
                unknown_marking_boxes = True
                continue
            marking_boxes_snapshot.append(
                {
                    "code": str(row.get("code") or "").strip(),
                    "sku_code": str(row.get("sku_code") or "").strip(),
                    "size": str(row.get("size") or "").strip(),
                    "barcode": str(row.get("barcode") or "").strip(),
                    "box_barcode": box_code,
                    "box_label": box_label,
                }
            )
        if missing_marking_boxes or unknown_marking_boxes:
            return ProcessingFlowCompletionResult(status="error", error_code="marking_box_mismatch")

        placed_cards_list = base_payload.get("placed_cards") or []
        if not isinstance(placed_cards_list, list):
            placed_cards_list = []
        for card_id in ready_cards:
            if card_id and card_id not in placed_cards_list:
                placed_cards_list.append(card_id)
        base_payload["placed_cards"] = placed_cards_list
        now = timezone.localtime().isoformat()
        for card in base_payload.get("cards") or []:
            card_id = processing_views._processing_card_id(card)
            if card_id and card_id in ready_cards:
                card["placed_at"] = card.get("placed_at") or now
                card["placed_done"] = True

        act_payload = dict(base_payload)
        act_payload["act"] = "placement"
        act_payload["act_label"] = "Акт размещения"
        act_payload["act_state"] = "closed"
        act_payload["act_items"] = placement_items
        act_payload["act_boxes"] = cleaned_boxes
        act_payload["act_pallets"] = cleaned_pallets
        act_payload["act_marking_boxes"] = marking_boxes_snapshot
        act_payload["flow_state"] = normalize_flow_state(cleaned_boxes, cleaned_pallets, "", "")
        act_payload["flow_closed"] = True
        act_payload["flow_closed_at"] = timezone.localtime().isoformat()
        act_payload["goods_type"] = "gv"
        act_payload["goods_type_label"] = processing_views.GOODS_TYPE_LABELS.get("gv", "Готовый")
        if has_qty_mismatch:
            discrepancy_items = mismatch_rows or processing_views._processing_discrepancy_rows_from_payload(act_payload)
            act_payload["discrepancy_detected"] = True
            act_payload["discrepancy_items"] = discrepancy_items
            act_payload["discrepancy_act_label"] = "Акт разногласий по обработке"
            act_payload["discrepancy_act_created_at"] = timezone.localtime().isoformat()
            act_payload["discrepancy_status"] = "reported"
        log_order_action(
            "update",
            order_id=order_id,
            order_type=order_type,
            user=request.user if request.user.is_authenticated else None,
            agency=latest.agency,
            description="Создан акт размещения после обработки (поток)",
            payload=act_payload,
        )
        OperationalStockService.replace_order_placement(
            latest.agency,
            order_type,
            order_id,
            act_payload,
        )
        if latest and latest.agency:
            reserve_rows = processing_views._processing_reserve_rows_for_order(order_id, latest.agency)
            remaining_rows = processing_views._remaining_processing_stock_rows_for_reserve(
                reserve_rows or (base_payload.get("stock_rows") if isinstance(base_payload.get("stock_rows"), list) else []),
                act_payload,
            )
            processing_views._replace_processing_reserves(order_id, latest.agency, remaining_rows)
        if has_qty_mismatch:
            processing_views._create_processing_discrepancy_task(order_id, latest.agency, request, mismatch_rows)
            log_order_action(
                "update",
                order_id=order_id,
                order_type=order_type,
                user=request.user if request.user.is_authenticated else None,
                agency=latest.agency,
                description="Создан акт разногласий по обработке",
                payload={
                    "act": "processing_discrepancy",
                    "act_label": "Акт разногласий по обработке",
                    "order_id": str(order_id),
                    "discrepancy_items": mismatch_rows,
                    "created_at": timezone.localtime().isoformat(),
                },
            )
        ProcessingFlowSession.objects.filter(
            order_id=order_id,
            order_type="processing",
            status=ProcessingFlowSession.STATUS_OPEN,
        ).update(
            status=ProcessingFlowSession.STATUS_CLOSED,
            last_seen=timezone.localtime(),
        )
        return ProcessingFlowCompletionResult(status="ok")

    @staticmethod
    def _sanitize_local_return_url(raw_value: str, default: str) -> str:
        return_url = str(raw_value or "").strip()
        if return_url:
            parsed = urlparse(return_url)
            if parsed.scheme or parsed.netloc:
                return_url = ""
            else:
                return_url = parsed.path or ""
                if parsed.query:
                    return_url = f"{return_url}?{parsed.query}"
                if parsed.fragment:
                    return_url = f"{return_url}#{parsed.fragment}"
        return return_url or default

    @staticmethod
    def _processing_card_redirect_target(request) -> str:
        return_to = str(request.POST.get("return_to") or request.POST.get("return") or "").strip()
        if return_to.startswith("/"):
            return return_to
        return request.get_full_path()

    @classmethod
    def _collect_processing_card_results(cls, request, *, card_id: str) -> tuple[list[dict], bool]:
        articles = request.POST.getlist("result_article[]")
        sizes = request.POST.getlist("result_size[]")
        destinations = request.POST.getlist("result_destination[]")
        received_list = request.POST.getlist("result_received[]")
        processed_list = request.POST.getlist("result_processed[]")
        defect_list = request.POST.getlist("result_defect[]")
        shortage_list = request.POST.getlist("result_shortage[]")
        labels_printed_list = request.POST.getlist("result_labels_printed[]")
        labels_unboxed_list = request.POST.getlist("result_labels_unboxed[]")
        shipped_list = request.POST.getlist("result_shipped_qty[]")
        tags_replaced_list = request.POST.getlist("result_tags_replaced[]")
        total_rows = max(
            len(articles),
            len(sizes),
            len(destinations),
            len(received_list),
            len(processed_list),
            len(defect_list),
            len(shortage_list),
            len(labels_printed_list),
            len(labels_unboxed_list),
            len(shipped_list),
            len(tags_replaced_list),
        )
        if total_rows <= 0:
            return [], False
        results: list[dict] = []
        for idx in range(total_rows):
            article = articles[idx].strip() if idx < len(articles) else ""
            size = sizes[idx].strip() if idx < len(sizes) else ""
            destination = destinations[idx].strip() if idx < len(destinations) else ""
            if not any((article, size, destination)):
                continue
            shipped_value = shipped_list[idx].strip() if idx < len(shipped_list) else ""
            labels_unboxed_value = labels_unboxed_list[idx].strip() if idx < len(labels_unboxed_list) else ""
            if not labels_unboxed_value:
                labels_unboxed_value = shipped_value
            processed_value = processed_list[idx].strip() if idx < len(processed_list) else ""
            tags_replaced_value = tags_replaced_list[idx].strip() if idx < len(tags_replaced_list) else ""
            if not tags_replaced_value:
                tags_replaced_value = processed_value
            results.append(
                {
                    "card_id": card_id,
                    "article": article,
                    "size": size,
                    "destination": destination,
                    "received": received_list[idx].strip() if idx < len(received_list) else "",
                    "processed": processed_value,
                    "defect": defect_list[idx].strip() if idx < len(defect_list) else "",
                    "shortage": shortage_list[idx].strip() if idx < len(shortage_list) else "",
                    "labels_printed": labels_printed_list[idx].strip() if idx < len(labels_printed_list) else "",
                    "labels_unboxed": labels_unboxed_value,
                    "shipped_qty": shipped_value,
                    "tags_replaced": tags_replaced_value,
                }
            )
        return results, True

    @classmethod
    def _merge_processing_card_results(cls, payload: dict, current_results: list[dict]) -> list[dict]:
        processing_views = cls._views()
        current_keys = {
            processing_views._processing_result_key(item)
            for item in (current_results or [])
            if isinstance(item, dict)
        }
        replaced_direction_groups = {
            (key[0], key[1], key[2])
            for key in current_keys
            if key[3] == "-"
        }
        merged: list[dict] = []
        seen_keys: set[tuple[str, str, str, str]] = set()
        existing_results = payload.get("processing_results") or []
        if isinstance(existing_results, list):
            for item in existing_results:
                if not isinstance(item, dict):
                    continue
                key = processing_views._processing_result_key(item)
                if key in current_keys or key in seen_keys:
                    continue
                if key[3] != "-" and (key[0], key[1], key[2]) in replaced_direction_groups:
                    continue
                merged.append(item)
                seen_keys.add(key)
        for item in current_results or []:
            if not isinstance(item, dict):
                continue
            key = processing_views._processing_result_key(item)
            if key in seen_keys:
                continue
            merged.append(item)
            seen_keys.add(key)
        return merged

    @classmethod
    def _mark_processing_card_processed(cls, payload: dict, *, card_id: str, request) -> bool:
        processing_views = cls._views()
        if not card_id:
            return False
        cards = payload.get("cards") or []
        target_card = None
        for card in cards:
            if not isinstance(card, dict):
                continue
            if processing_views._processing_card_id(card) == card_id:
                target_card = card
                break
        if not target_card and len(cards) == 1 and isinstance(cards[0], dict):
            target_card = cards[0]
        changed = False
        if target_card:
            if not target_card.get("processed_at"):
                changed = True
            target_card["processed_at"] = timezone.localtime().isoformat()
            if request.user and request.user.is_authenticated:
                target_card["processed_by"] = (
                    request.user.get_full_name().strip()
                    or request.user.username
                    or str(request.user)
                )
            if not target_card.get("processed_done"):
                changed = True
            target_card["processed_done"] = True
        processed_cards = payload.get("processed_cards") or []
        if not isinstance(processed_cards, list):
            processed_cards = []
        if card_id and card_id not in processed_cards:
            processed_cards.append(card_id)
            changed = True
        payload["processed_cards"] = processed_cards
        payload["cards"] = cards
        return changed

    @classmethod
    def build_processing_card_page_context(
        cls,
        *,
        order_id: str,
        card_id: str,
        request,
        payload: dict,
        agency,
    ) -> dict:
        processing_views = cls._views()
        status_label = WarehouseGoodsStateResolver.resolve_for_processing_order(
            order_id=str(order_id or ""),
            agency=agency,
            payload=payload,
        ).label_for("processing")
        return_url = cls._sanitize_local_return_url(
            request.GET.get("return"),
            f"/orders/processing/{order_id}/work/",
        )
        ctx: dict = {
            "order_id": order_id,
            "return_url": return_url,
            "cabinet_url": resolve_cabinet_url(get_request_role(request)),
            "status_label": status_label or "-",
            "client_label": (agency.agn_name or agency.fio_agn or str(agency)) if agency else "-",
            "card_id": card_id,
        }
        printers, printers_meta = processing_views.load_available_printers_data()
        label_printers, pdf_printers = split_printers_by_kind(printers)
        ctx["available_printers"] = label_printers
        ctx["available_label_printers"] = label_printers
        ctx["available_pdf_printers"] = pdf_printers
        ctx["available_printers_all"] = printers
        ctx["available_printers_meta"] = printers_meta
        ctx["label_settings"] = processing_views.load_label_settings()
        label_sizes: dict[str, dict] = {}
        for entry in processing_views.LABEL_SIZES:
            key = entry.get("key")
            if not key:
                continue
            label_sizes[key] = {
                "width_mm": entry.get("width_mm", 58),
                "height_mm": entry.get("height_mm", 40),
                "preview_scale": entry.get("preview_scale", 1),
            }
        ctx["label_sizes_json"] = json.dumps(label_sizes, ensure_ascii=True)

        cards = payload.get("cards") or []
        article_param = str(request.GET.get("article") or "").strip()
        selected_card = None
        for card in cards:
            if not isinstance(card, dict):
                continue
            if card_id and str(card.get("id") or "") == card_id:
                selected_card = card
                break
        if not selected_card and article_param:
            for card in cards:
                if not isinstance(card, dict):
                    continue
                if str(card.get("article") or "").strip() == article_param:
                    selected_card = card
                    break
        if not selected_card and cards:
            selected_card = cards[0] if isinstance(cards[0], dict) else None

        fallback_rows = payload.get("stock_rows") or payload.get("size_rows") or []
        selected_card = selected_card or {
            "article": payload.get("article") or "",
            "product_name": payload.get("product_name") or "",
            "photo_url": payload.get("product_photo_url") or "",
            "goods_type": payload.get("goods_type") or "",
            "rows": fallback_rows,
        }

        article_value = str(selected_card.get("article") or payload.get("article") or "").strip()
        product_name = str(selected_card.get("product_name") or payload.get("product_name") or "").strip()
        goods_type = str(selected_card.get("goods_type") or payload.get("goods_type") or "").strip().lower()
        goods_type_label = processing_views.GOODS_TYPE_LABELS.get(goods_type, goods_type) if goods_type else ""
        photo_url = str(
            selected_card.get("photo_url")
            or selected_card.get("product_photo_url")
            or payload.get("product_photo_url")
            or ""
        ).strip()
        supplier_value = str(payload.get("supplier") or "").strip()
        if not supplier_value and agency:
            supplier_value = (agency.agn_name or agency.fio_agn or str(agency) or "").strip()
        product_name_value = processing_views._normalize_org_name(product_name)
        brand_value = processing_views._normalize_org_name(payload.get("brand") or "")
        subject_value = processing_views._normalize_org_name(payload.get("subject") or "")
        color_value = processing_views._normalize_org_name(payload.get("color") or "")
        composition_value = processing_views._normalize_org_name(payload.get("composition") or "")
        country_value = processing_views._normalize_org_name(payload.get("made_in") or "")
        supplier_value = processing_views._normalize_org_name(supplier_value)
        label_base = {
            "article": article_value,
            "name": product_name_value,
            "brand": brand_value,
            "subject": subject_value,
            "color": color_value,
            "composition": composition_value,
            "supplier": supplier_value,
            "country": country_value,
            "barcode_extra": (payload.get("source_reference") or article_value or ""),
        }
        ctx["label_base_json"] = json.dumps(label_base, ensure_ascii=True)

        card_fields: list[dict] = []

        def add_field(label: str, value) -> None:
            text = processing_views._non_empty_text(value)
            if not text:
                return
            card_fields.append({"label": label, "value": text})

        add_field("Наименование товара", product_name_value)
        add_field("Артикул", article_value)
        add_field("Тип товара", goods_type_label)
        add_field("Поставщик", supplier_value)
        add_field("Бренд", brand_value)
        add_field("Предмет", subject_value)
        add_field("Цвет", color_value)
        add_field("Состав", composition_value)
        add_field("Пол", payload.get("gender"))
        add_field("Сезон", payload.get("season"))
        add_field("Заказ №", payload.get("order_no"))
        add_field("Артикул WB", payload.get("wb_article"))

        rows_source = selected_card.get("rows") or []
        if not rows_source:
            rows_source = fallback_rows
        card_rows: list[dict] = []
        for row in rows_source:
            if not isinstance(row, dict):
                continue
            size_value = row.get("size") or row.get("size_value") or row.get("size_no") or ""
            barcode_value = row.get("barcode") or row.get("barcode_value") or ""
            qty_value = row.get("qty")
            if qty_value in (None, ""):
                qty_value = row.get("recount_qty")
            if qty_value in (None, ""):
                qty_value = row.get("processing_qty")
            if not (size_value or barcode_value or qty_value):
                continue
            card_rows.append(
                {
                    "article": str(row.get("article") or article_value or "").strip(),
                    "size": size_value,
                    "barcode": barcode_value,
                    "qty": qty_value if qty_value not in (None, "") else "-",
                }
            )

        label_print_buttons: list[dict] = []
        marking_qty = processing_views._parse_qty_value(payload.get("marking_5840_qty"))
        marking_each_qty = processing_views._parse_qty_value(payload.get("marking_5840_each_qty"))
        if marking_qty or marking_each_qty:
            card_path_id = card_id or processing_views._processing_card_id(selected_card) or article_value or ""
            base_path = f"/orders/processing/{order_id}/card/{card_path_id}/labels/"
            base_params: dict[str, str] = {}
            if article_param:
                base_params["article"] = article_param
            if return_url:
                base_params["return"] = return_url

            def build_label_print_url(mode: str) -> str:
                params = dict(base_params)
                params["label_print"] = "1"
                params["label_mode"] = mode
                return f"{base_path}?{urlencode(params)}"

            if marking_qty:
                label_print_buttons.append(
                    {"label": "Распечатать этикетки", "mode": "no-cz", "url": build_label_print_url("no-cz")}
                )
            if marking_each_qty:
                label_print_buttons.append(
                    {"label": "Распечатать этикетки ЧЗ", "mode": "cz", "url": build_label_print_url("cz")}
                )

        ctx["card"] = {
            "article": article_value,
            "product_name": product_name,
            "photo_url": photo_url,
            "goods_type": goods_type_label,
        }
        ctx["card_fields"] = card_fields
        ctx["card_rows"] = card_rows
        ctx["label_print_buttons"] = label_print_buttons

        printed_no_cz_total = 0
        printed_cz_total = 0
        if order_id:
            barcodes = [
                str(row.get("barcode") or "").strip()
                for row in card_rows
                if isinstance(row, dict)
            ]
            barcodes = [value for value in barcodes if value]
            printed_jobs_qs = ProcessingPrintJob.objects.filter(
                order_id=str(order_id),
                status=ProcessingPrintJob.STATUS_PRINTED,
            )
            if barcodes:
                printed_jobs_qs = printed_jobs_qs.filter(barcode__in=barcodes)
            printed_no_cz_total = printed_jobs_qs.count()
            qs = MarkingCode.objects.filter(
                order_type="processing",
                order_id=order_id,
                printed_at__isnull=False,
            )
            if agency:
                qs = qs.filter(agency=agency)
            if barcodes:
                qs = qs.filter(barcode__in=barcodes)
            printed_cz_total = qs.count()
        ctx["printed_no_cz_total"] = printed_no_cz_total
        ctx["printed_cz_total"] = printed_cz_total

        card_id_value = processing_views._processing_card_id(selected_card) or card_id or article_value
        card_path_id = card_id or processing_views._processing_card_id(selected_card) or article_value or "card"
        technical_params: dict[str, str] = {}
        if article_param:
            technical_params["article"] = article_param
        if return_url:
            technical_params["return"] = return_url
        technical_path = f"/orders/processing/{order_id}/card/{card_path_id}/technical/"
        if technical_params:
            technical_path = f"{technical_path}?{urlencode(technical_params)}"
        ctx["technical_card_url"] = technical_path

        processed_cards, placed_cards = processing_views._processing_card_sets(payload)
        ctx["card_processed"] = bool(card_id_value and card_id_value in processed_cards)
        ctx["card_placed"] = bool(card_id_value and card_id_value in placed_cards)
        ctx["card_processed_at"] = selected_card.get("processed_at") if isinstance(selected_card, dict) else ""
        ctx["card_processed_by"] = selected_card.get("processed_by") if isinstance(selected_card, dict) else ""
        role = get_request_role(request)
        ctx["can_finish_card"] = role in {"storekeeper", "processing_head"} and not ctx["card_processed"]

        processing_params = processing_views._processing_params_from_payload(payload)
        direction_plan = processing_views._parse_json_value(payload.get("direction_plan_json"), {})
        direction_tables: list[dict] = []
        plan_rows = []
        if isinstance(direction_plan, dict):
            plan_rows = direction_plan.get("rows") or []
        if not isinstance(plan_rows, list):
            plan_rows = []
        filtered_rows = []
        if article_value:
            article_lower = article_value.lower()
            for row in plan_rows:
                if not isinstance(row, dict):
                    continue
                row_article = str(row.get("article") or "").strip().lower()
                if row_article and row_article != article_lower:
                    continue
                filtered_rows.append(row)
        elif product_name:
            product_lower = product_name.lower()
            for row in plan_rows:
                if not isinstance(row, dict):
                    continue
                row_product = str(row.get("product_name") or "").strip().lower()
                if row_product and row_product != product_lower:
                    continue
                filtered_rows.append(row)
        if not filtered_rows and plan_rows:
            filtered_rows = [row for row in plan_rows if isinstance(row, dict)]
        barcode_map: dict[str, str] = {}
        for row in card_rows:
            size_key = str(row.get("size") or "").strip().lower()
            barcode_value = str(row.get("barcode") or "").strip()
            if size_key and barcode_value and size_key not in barcode_map:
                barcode_map[size_key] = barcode_value

        def parse_dir_qty(value) -> int:
            qty = processing_views._parse_qty_value(value)
            return qty if qty is not None else 0

        def plan_row_total_qty(row: dict) -> int:
            total = 0
            quantities = row.get("quantities") or []
            if isinstance(quantities, (list, tuple)):
                for value in quantities:
                    total += parse_dir_qty(value)
            if total > 0:
                return total
            return (
                parse_dir_qty(row.get("qty"))
                or parse_dir_qty(row.get("recount_qty"))
                or parse_dir_qty(row.get("processing_qty"))
            )

        selected_card_id_key = str(card_id or processing_views._processing_card_id(selected_card) or "").strip().lower()
        results_map: dict[tuple[str, str, str, str], dict] = {}
        legacy_results_map: dict[tuple[str, str, str], dict] = {}
        results_by_card_article_size: dict[tuple[str, str, str], list[dict]] = {}
        results_by_article_size: dict[tuple[str, str], list[dict]] = {}
        saved_results = payload.get("processing_results") or []
        if isinstance(saved_results, list):
            for item in saved_results:
                if not isinstance(item, dict):
                    continue
                key = processing_views._processing_result_key(item)
                results_map[key] = item
                if not key[0]:
                    legacy_results_map[(key[1], key[2], key[3])] = item
                if key[0]:
                    results_by_card_article_size.setdefault((key[0], key[1], key[2]), []).append(item)
                results_by_article_size.setdefault((key[1], key[2]), []).append(item)

        def aggregate_saved_rows(rows: list[dict], article: str, size: str) -> dict:
            metric_fields = (
                "processed",
                "defect",
                "shortage",
                "labels_printed",
                "labels_unboxed",
                "shipped_qty",
                "tags_replaced",
            )
            aggregated: dict[str, str] = {
                "card_id": selected_card_id_key,
                "article": article,
                "size": size,
                "destination": "-",
            }
            has_metric = False
            for field_name in metric_fields:
                total = 0
                has_value = False
                for row in rows:
                    qty = processing_views._parse_qty_value((row or {}).get(field_name))
                    if qty is None:
                        continue
                    total += qty
                    has_value = True
                if has_value:
                    aggregated[field_name] = str(total)
                    has_metric = True
            return aggregated if has_metric else {}

        def saved_result_for(article: str, size: str, destination: str):
            article_key = str(article or "").strip().lower()
            size_key = str(size or "").strip().lower()
            dest_key = str(destination or "").strip().lower() or "-"
            key = (selected_card_id_key, article_key, size_key, dest_key)
            saved = results_map.get(key)
            if saved:
                return saved
            legacy_saved = legacy_results_map.get((article_key, size_key, dest_key))
            if legacy_saved:
                return legacy_saved
            if dest_key != "-":
                return {}
            card_specific_rows = results_by_card_article_size.get((selected_card_id_key, article_key, size_key)) or []
            if card_specific_rows:
                aggregated = aggregate_saved_rows(card_specific_rows, article, size)
                if aggregated:
                    return aggregated
            rows = results_by_article_size.get((article_key, size_key)) or []
            if rows:
                aggregated = aggregate_saved_rows(rows, article, size)
                if aggregated:
                    return aggregated
            return {}

        results_rows: list[dict] = []
        if filtered_rows:
            for row in filtered_rows:
                if not isinstance(row, dict):
                    continue
                size_value = str(row.get("size") or "").strip()
                row_article = str(row.get("article") or row.get("product_name") or article_value or "").strip()
                barcode_value = barcode_map.get(size_value.lower(), "") if size_value else ""
                qty = plan_row_total_qty(row)
                if qty <= 0:
                    continue
                saved = saved_result_for(row_article, size_value, "-")
                results_rows.append(
                    {
                        "article": row_article or "-",
                        "size": size_value or "-",
                        "barcode": barcode_value or "-",
                        "received": qty,
                        "destination": "-",
                        "processed": saved.get("processed") or "",
                        "defect": saved.get("defect") or "",
                        "shortage": saved.get("shortage") or "",
                        "labels_printed": saved.get("labels_printed") or "",
                        "labels_unboxed": saved.get("labels_unboxed") or (saved.get("shipped_qty") or ""),
                        "shipped_qty": saved.get("shipped_qty") or "",
                        "tags_replaced": saved.get("tags_replaced") or "",
                    }
                )
        if not results_rows:
            for row in card_rows:
                if not isinstance(row, dict):
                    continue
                row_article = str(row.get("article") or article_value or "").strip()
                size_value = str(row.get("size") or "").strip()
                barcode_value = str(row.get("barcode") or "").strip()
                qty = processing_views._parse_qty_value(row.get("qty")) or 0
                if qty <= 0:
                    continue
                saved = saved_result_for(row_article, size_value, "-")
                results_rows.append(
                    {
                        "article": row_article or "-",
                        "size": size_value or "-",
                        "barcode": barcode_value or "-",
                        "received": qty,
                        "destination": "-",
                        "processed": saved.get("processed") or "",
                        "defect": saved.get("defect") or "",
                        "shortage": saved.get("shortage") or "",
                        "labels_printed": saved.get("labels_printed") or "",
                        "labels_unboxed": saved.get("labels_unboxed") or (saved.get("shipped_qty") or ""),
                        "shipped_qty": saved.get("shipped_qty") or "",
                        "tags_replaced": saved.get("tags_replaced") or "",
                    }
                )

        def metric_or_none(value):
            return processing_views._parse_qty_value(value)

        def metric_or_zero(value) -> int:
            parsed = metric_or_none(value)
            return parsed if parsed is not None else 0

        job_barcode_map: dict[tuple[str, str], int] = {}
        job_sku_map: dict[tuple[str, str], int] = {}
        marking_barcode_map: dict[tuple[str, str], int] = {}
        marking_sku_map: dict[tuple[str, str], int] = {}
        if order_id:
            printed_jobs_qs = (
                ProcessingPrintJob.objects.filter(
                    order_id=str(order_id),
                    status=ProcessingPrintJob.STATUS_PRINTED,
                )
                .values("article", "barcode", "size")
                .annotate(count=Count("id"))
            )
            for item in printed_jobs_qs:
                barcode_key = (
                    str(item.get("barcode") or "").strip(),
                    str(item.get("size") or "").strip().lower(),
                )
                sku_key = (
                    str(item.get("article") or "").strip().lower(),
                    str(item.get("size") or "").strip().lower(),
                )
                count_value = int(item.get("count") or 0)
                if barcode_key[0]:
                    job_barcode_map[barcode_key] = max(job_barcode_map.get(barcode_key, 0), count_value)
                if sku_key[0]:
                    job_sku_map[sku_key] = max(job_sku_map.get(sku_key, 0), count_value)
            marking_qs = MarkingCode.objects.filter(
                order_type="processing",
                order_id=order_id,
                printed_at__isnull=False,
            )
            if agency:
                marking_qs = marking_qs.filter(agency=agency)
            marking_rows = marking_qs.values("sku_code", "barcode", "size").annotate(count=Count("id"))
            for item in marking_rows:
                barcode_key = (
                    str(item.get("barcode") or "").strip(),
                    str(item.get("size") or "").strip().lower(),
                )
                sku_key = (
                    str(item.get("sku_code") or "").strip().lower(),
                    str(item.get("size") or "").strip().lower(),
                )
                count_value = int(item.get("count") or 0)
                if barcode_key[0]:
                    marking_barcode_map[barcode_key] = max(marking_barcode_map.get(barcode_key, 0), count_value)
                if sku_key[0]:
                    marking_sku_map[sku_key] = max(marking_sku_map.get(sku_key, 0), count_value)

        entries_qs = OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").order_by("created_at")
        if agency:
            entries_qs = entries_qs.filter(agency=agency)
        entries = list(entries_qs)
        placement_map: dict[tuple[str, str], dict[str, int]] = {}
        placement_entry = next(
            (
                entry
                for entry in reversed(entries)
                if (entry.payload or {}).get("act") == "placement"
            ),
            None,
        )
        placement_payload = placement_entry.payload or {} if placement_entry else {}
        placement_items = placement_payload.get("act_items") or []
        placement_boxes = placement_payload.get("act_boxes") or []
        placement_pallets = placement_payload.get("act_pallets") or []
        placement_box_items_by_code: dict[str, list[dict]] = {}
        placement_box_codes_by_key: dict[tuple[str, str], set[str]] = {}
        placement_box_codes_total: set[str] = set()
        if isinstance(placement_boxes, list):
            for box in placement_boxes:
                if not isinstance(box, dict):
                    continue
                box_code = str(box.get("code") or "").strip()
                if not box_code:
                    continue
                placement_box_codes_total.add(box_code)
                box_items = box.get("items") or []
                placement_box_items_by_code[box_code] = box_items if isinstance(box_items, list) else []
                for item in placement_box_items_by_code[box_code]:
                    if not isinstance(item, dict):
                        continue
                    sku_key = str(item.get("sku_code") or item.get("sku") or "").strip().lower()
                    size_key = str(item.get("size") or "").strip().lower()
                    if not sku_key:
                        continue
                    placement_box_codes_by_key.setdefault((sku_key, size_key), set()).add(box_code)
        placement_pallet_codes_by_key: dict[tuple[str, str], set[str]] = {}
        placement_pallet_codes_total: set[str] = set()
        if isinstance(placement_pallets, list):
            for pallet in placement_pallets:
                if not isinstance(pallet, dict):
                    continue
                pallet_code = str(pallet.get("code") or "").strip()
                if not pallet_code:
                    continue
                placement_pallet_codes_total.add(pallet_code)
                direct_items = pallet.get("items") or []
                if isinstance(direct_items, list) and direct_items:
                    source_items = direct_items
                else:
                    source_items = []
                    for box_code in pallet.get("boxes") or []:
                        code = str(box_code or "").strip()
                        if not code:
                            continue
                        source_items.extend(placement_box_items_by_code.get(code, []))
                for item in source_items:
                    if not isinstance(item, dict):
                        continue
                    sku_key = str(item.get("sku_code") or item.get("sku") or "").strip().lower()
                    size_key = str(item.get("size") or "").strip().lower()
                    if not sku_key:
                        continue
                    qty = metric_or_zero(item.get("qty") or item.get("actual_qty"))
                    if qty <= 0:
                        continue
                    placement_pallet_codes_by_key.setdefault((sku_key, size_key), set()).add(pallet_code)
        if isinstance(placement_items, list):
            for item in placement_items:
                if not isinstance(item, dict):
                    continue
                sku_key = str(item.get("sku_code") or item.get("sku") or "").strip().lower()
                size_key = str(item.get("size") or "").strip().lower()
                if not sku_key:
                    continue
                row = placement_map.setdefault((sku_key, size_key), {"qty": 0, "boxes": 0, "pallets": 0})
                row["qty"] += metric_or_zero(item.get("actual_qty") or item.get("qty"))
                row["boxes"] += metric_or_zero(item.get("box_qty"))
                row["pallets"] += metric_or_zero(item.get("pallet_qty"))
        for key, codes in placement_box_codes_by_key.items():
            row = placement_map.setdefault(key, {"qty": 0, "boxes": 0, "pallets": 0})
            if codes:
                row["boxes"] = len(codes)
        for key, codes in placement_pallet_codes_by_key.items():
            row = placement_map.setdefault(key, {"qty": 0, "boxes": 0, "pallets": 0})
            if codes:
                row["pallets"] = len(codes)

        for row in results_rows:
            if not isinstance(row, dict):
                continue
            article_key = str(row.get("article") or "").strip().lower()
            size_key = str(row.get("size") or "").strip().lower()
            barcode_key = (str(row.get("barcode") or "").strip(), size_key)
            sku_key = (article_key, size_key)
            auto_printed = max(
                job_barcode_map.get(barcode_key, 0),
                job_sku_map.get(sku_key, 0),
                marking_barcode_map.get(barcode_key, 0),
                marking_sku_map.get(sku_key, 0),
            )
            placement_data = placement_map.get(sku_key, {"qty": 0, "boxes": 0, "pallets": 0})
            auto_unboxed = int(placement_data.get("qty") or 0)
            auto_shipped = auto_unboxed
            labels_printed_value = metric_or_none(row.get("labels_printed"))
            labels_unboxed_value = metric_or_none(row.get("labels_unboxed"))
            shipped_value = metric_or_none(row.get("shipped_qty"))
            row["labels_printed"] = labels_printed_value if labels_printed_value is not None else auto_printed
            row["labels_unboxed"] = labels_unboxed_value if labels_unboxed_value is not None else auto_unboxed
            row["shipped_qty"] = shipped_value if shipped_value is not None else auto_shipped
            row["boxes_count"] = int(placement_data.get("boxes") or 0)
            row["pallets_count"] = int(placement_data.get("pallets") or 0)

        received_by_key: dict[tuple[str, str], int] = {}
        barcode_by_key: dict[tuple[str, str], str] = {}
        size_label_by_key: dict[tuple[str, str], str] = {}
        for row in results_rows:
            if not isinstance(row, dict):
                continue
            article_key = str(row.get("article") or "").strip().lower()
            size_key = str(row.get("size") or "").strip().lower()
            if not article_key:
                continue
            key = (article_key, size_key)
            received_by_key[key] = received_by_key.get(key, 0) + metric_or_zero(row.get("received"))
            barcode_value = str(row.get("barcode") or "").strip()
            if barcode_value and key not in barcode_by_key:
                barcode_by_key[key] = barcode_value
            size_label = str(row.get("size") or "").strip()
            if size_label and key not in size_label_by_key:
                size_label_by_key[key] = size_label

        label_summary_rows: list[dict] = []
        for key in sorted(received_by_key.keys()):
            article_key, size_key = key
            barcode_value = barcode_by_key.get(key, "-")
            received_qty = received_by_key.get(key, 0)
            printed_no_cz = job_sku_map.get(key, 0)
            if barcode_value and barcode_value != "-":
                printed_no_cz = max(printed_no_cz, job_barcode_map.get((barcode_value, size_key), 0))
            required_no_cz = received_qty * (marking_qty or 0)
            if (marking_qty or 0) > 0 or printed_no_cz > 0:
                label_summary_rows.append(
                    {
                        "article": article_key,
                        "size": size_label_by_key.get(key) or size_key or "-",
                        "barcode": barcode_value or "-",
                        "label_type": "58/40",
                        "required_qty": required_no_cz,
                        "printed_qty": printed_no_cz,
                    }
                )
            printed_cz = marking_sku_map.get(key, 0)
            if barcode_value and barcode_value != "-":
                printed_cz = max(printed_cz, marking_barcode_map.get((barcode_value, size_key), 0))
            required_cz = received_qty * (marking_each_qty or 0)
            if (marking_each_qty or 0) > 0 or printed_cz > 0:
                label_summary_rows.append(
                    {
                        "article": article_key,
                        "size": size_label_by_key.get(key) or size_key or "-",
                        "barcode": barcode_value or "-",
                        "label_type": "58/40 (шт/чз)",
                        "required_qty": required_cz,
                        "printed_qty": printed_cz,
                    }
                )

        label_summary_exact_map: dict[tuple[str, str, str], list[dict]] = {}
        label_summary_key_map: dict[tuple[str, str], list[dict]] = {}
        for item in label_summary_rows:
            article_key = str(item.get("article") or "").strip().lower()
            size_key = str(item.get("size") or "").strip().lower()
            barcode_key = str(item.get("barcode") or "").strip()
            mode = "cz" if str(item.get("label_type") or "").strip() == "58/40 (шт/чз)" else "no-cz"
            normalized_item = {
                "mode": mode,
                "label_type": str(item.get("label_type") or "58/40").strip() or "58/40",
                "required_qty": int(item.get("required_qty") or 0),
                "printed_qty": int(item.get("printed_qty") or 0),
                "barcode": barcode_key or "-",
                "size": str(item.get("size") or "").strip() or "-",
            }
            exact_key = (article_key, size_key, barcode_key)
            label_summary_exact_map.setdefault(exact_key, []).append(normalized_item)
            label_summary_key_map.setdefault((article_key, size_key), []).append(normalized_item)
        for key, items in label_summary_exact_map.items():
            label_summary_exact_map[key] = sorted(
                items,
                key=lambda value: (0 if value.get("mode") == "no-cz" else 1, value.get("label_type") or ""),
            )
        for key, items in label_summary_key_map.items():
            label_summary_key_map[key] = sorted(
                items,
                key=lambda value: (0 if value.get("mode") == "no-cz" else 1, value.get("label_type") or ""),
            )

        placement_summary_rows: list[dict] = []
        placement_keys = set(received_by_key.keys()) | set(placement_map.keys())
        for key in sorted(placement_keys):
            article_key, size_key = key
            barcode_value = barcode_by_key.get(key, "-")
            placement_data = placement_map.get(key, {"qty": 0, "boxes": 0, "pallets": 0})
            placement_summary_rows.append(
                {
                    "article": article_key,
                    "size": size_label_by_key.get(key) or size_key or "-",
                    "barcode": barcode_value or "-",
                    "qty": int(placement_data.get("qty") or 0),
                    "boxes": int(placement_data.get("boxes") or 0),
                    "pallets": int(placement_data.get("pallets") or 0),
                }
            )
        placement_total_boxes = len(placement_box_codes_total)
        placement_total_pallets = len(placement_pallet_codes_total)
        if placement_total_boxes <= 0:
            placement_total_boxes = sum(int((item or {}).get("boxes") or 0) for item in placement_summary_rows)
        if placement_total_pallets <= 0:
            placement_total_pallets = sum(int((item or {}).get("pallets") or 0) for item in placement_summary_rows)
        for row in results_rows:
            if not isinstance(row, dict):
                continue
            article_key = str(row.get("article") or "").strip().lower()
            size_key = str(row.get("size") or "").strip().lower()
            barcode_key = str(row.get("barcode") or "").strip()
            row_print_items = label_summary_exact_map.get((article_key, size_key, barcode_key), [])
            if not row_print_items:
                row_print_items = label_summary_key_map.get((article_key, size_key), [])
            row["print_items"] = [dict(item) for item in row_print_items]

        has_direction_distribution = False
        has_defect_values = False
        has_label_values = False
        has_tag_values = False
        for row in results_rows:
            if not isinstance(row, dict):
                continue
            if (processing_views._parse_qty_value(row.get("defect")) or 0) > 0 or (
                processing_views._parse_qty_value(row.get("shortage")) or 0
            ) > 0:
                has_defect_values = True
            if (processing_views._parse_qty_value(row.get("labels_printed")) or 0) > 0 or (
                processing_views._parse_qty_value(row.get("labels_unboxed")) or 0
            ) > 0:
                has_label_values = True
            if (processing_views._parse_qty_value(row.get("tags_replaced")) or 0) > 0:
                has_tag_values = True
        result_requirements = processing_views._processing_result_requirements(
            payload,
            has_direction_distribution=has_direction_distribution,
        )
        quality_required = bool(result_requirements.get("quality"))
        labels_required = bool(result_requirements.get("labels"))
        tags_required = bool(result_requirements.get("tags"))

        ctx["processing_params"] = processing_params
        ctx["results_rows"] = results_rows
        ctx["label_summary_rows"] = label_summary_rows
        ctx["placement_summary_rows"] = placement_summary_rows
        ctx["placement_total_boxes"] = max(0, int(placement_total_boxes or 0))
        ctx["placement_total_pallets"] = max(0, int(placement_total_pallets or 0))
        ctx["results_quality_required"] = quality_required
        ctx["results_labels_required"] = labels_required
        ctx["results_shipping_required"] = True
        ctx["results_tags_required"] = tags_required
        ctx["results_quality_visible"] = bool(quality_required or has_defect_values)
        ctx["results_labels_visible"] = bool(labels_required or has_label_values or label_summary_rows)
        ctx["results_shipping_visible"] = True
        ctx["results_tags_visible"] = bool(tags_required or has_tag_values)
        ctx["result_tags_owner"] = str(payload.get("tag_owner") or "").strip()
        ctx["direction_tables"] = direction_tables
        ctx["has_direction_distribution"] = has_direction_distribution
        ctx["can_edit_results"] = role in {"processing_head", "head_manager", "director", "admin"}
        return ctx

    @classmethod
    def build_processing_technical_card_page_context(
        cls,
        *,
        ctx: dict,
        order_id: str,
        card_id: str,
        request,
        payload: dict,
    ) -> dict:
        processing_views = cls._views()
        article_param = str(request.GET.get("article") or "").strip()
        card_params: dict[str, str] = {}
        if article_param:
            card_params["article"] = article_param
        return_url = str(ctx.get("return_url") or "").strip()
        if return_url:
            card_params["return"] = return_url
        card_url = f"/orders/processing/{order_id}/card/{card_id or 'card'}/"
        if card_params:
            card_url = f"{card_url}?{urlencode(card_params)}"

        def raw_value(key: str) -> str:
            return str(payload.get(key) or "").strip()

        def is_yes(value) -> bool:
            text = str(value or "").strip().lower()
            return text in {"да", "yes", "true", "1", "file", "set", "comment"}

        def normalize_size_code(value: str) -> str:
            text = str(value or "").strip().upper()
            text = text.replace("Х", "X")
            text = text.replace("*", "X")
            text = text.replace("MM", "")
            text = text.replace("ММ", "")
            text = text.replace(" ", "")
            return text

        def contains_keyword(value, keywords: tuple[str, ...]) -> bool:
            text = str(value or "").strip().lower()
            if not text:
                return False
            return any(keyword in text for keyword in keywords)

        def has_payload_value(key: str) -> bool:
            if key not in payload:
                return False
            value = payload.get(key)
            if value is None:
                return False
            if isinstance(value, bool):
                return value
            if isinstance(value, (list, tuple, set, dict)):
                return len(value) > 0
            if isinstance(value, (int, float)):
                return value != 0
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    return False
                if text in {"0", "0.0", "-", "нет", "no", "false"}:
                    return False
                return True
            return True

        marking_sizes = payload.get("marking_sizes") or []
        if isinstance(marking_sizes, str):
            marking_sizes = [marking_sizes] if marking_sizes else []
        size_codes = {normalize_size_code(item) for item in marking_sizes}
        if not size_codes and (raw_value("marking_5840_qty") or raw_value("marking_5840_each_qty")):
            size_codes.add("58X40")

        insert_types = payload.get("insert_types") or []
        if isinstance(insert_types, str):
            insert_types = [insert_types] if insert_types else []
        insert_other = raw_value("insert_other")
        if insert_other:
            insert_types = list(insert_types) + [insert_other]

        tech = {
            "supplier": raw_value("supplier") or raw_value("warehouse_receiving"),
            "brand": raw_value("brand"),
            "subject": raw_value("subject"),
            "article": (ctx.get("card") or {}).get("article") or raw_value("article"),
            "wb_article": raw_value("wb_article"),
            "order_no": raw_value("order_no"),
            "purchase_1c_no": raw_value("purchase_1c_no"),
            "purchase_1c_date": raw_value("purchase_1c_date"),
            "color": raw_value("color"),
            "composition": raw_value("composition"),
            "gender": raw_value("gender"),
            "season": raw_value("season"),
            "size_count": len(ctx.get("card_rows") or []),
            "measure_weight": raw_value("measure_weight"),
            "measure_width": raw_value("measure_width"),
            "measure_height": raw_value("measure_height"),
            "measure_depth": raw_value("measure_depth"),
            "defect_percent": raw_value("defect_percent"),
            "defect_qty": raw_value("defect_qty"),
            "trim_threads_qty": raw_value("trim_threads_qty"),
            "tape_qty": raw_value("tape_qty"),
            "remove_tag_qty": raw_value("remove_tag_qty"),
            "attach_tag_qty": raw_value("attach_tag_qty"),
            "marking_5840_qty": raw_value("marking_5840_qty"),
            "marking_5840_each_qty": raw_value("marking_5840_each_qty"),
            "marking_info": raw_value("marking_info"),
            "set_qty": raw_value("set_qty"),
            "insert_qty": raw_value("insert_qty"),
            "insert_types_text": ", ".join([str(item).strip() for item in insert_types if str(item).strip()]),
            "bubble_wrap_type": raw_value("bubble_wrap_type"),
            "bubble_wrap_size": raw_value("bubble_wrap_size"),
            "bubble_wrap_qty": raw_value("bubble_wrap_qty"),
            "bubble_wrap_supply": raw_value("bubble_wrap_supply"),
            "bag_replace_type": raw_value("bag_replace_type"),
            "bag_replace_size": raw_value("bag_replace_size"),
            "bag_replace_qty": raw_value("bag_replace_qty"),
            "bag_replace_supply": raw_value("bag_replace_supply"),
            "box_replace_type": raw_value("box_replace_type"),
            "box_replace_size": raw_value("box_replace_size"),
            "box_replace_qty": raw_value("box_replace_qty"),
            "box_replace_supply": raw_value("box_replace_supply"),
            "shrink_wrap_type": raw_value("shrink_wrap_type"),
            "shrink_wrap_size": raw_value("shrink_wrap_size"),
            "shrink_wrap_qty": raw_value("shrink_wrap_qty"),
            "invoice_no": raw_value("invoice_no"),
            "invoice_date": raw_value("invoice_date"),
            "payment_date": raw_value("payment_date"),
            "wholesale_places_qty": raw_value("wholesale_places_qty"),
            "project_manager": raw_value("project_manager"),
            "warehouse_receiving": raw_value("warehouse_receiving"),
            "warehouse_packing": raw_value("warehouse_packing"),
            "warehouse_unpacking": raw_value("warehouse_unpacking"),
            "responsible_name": raw_value("responsible_name"),
            "start_date": raw_value("start_date"),
            "end_date": raw_value("end_date"),
            "receive_date": raw_value("receive_date"),
            "accountant": raw_value("accountant"),
            "archive_date": raw_value("archive_date"),
        }
        tech_checks = {
            "measure_yes": is_yes(payload.get("measure_needed")),
            "measure_no": not is_yes(payload.get("measure_needed")),
            "remove_tag_yes": is_yes(payload.get("remove_tag")),
            "remove_tag_no": not is_yes(payload.get("remove_tag")),
            "attach_tag_yes": is_yes(payload.get("attach_tag")),
            "attach_tag_no": not is_yes(payload.get("attach_tag")),
            "marking_30_20": "30X20" in size_codes,
            "marking_58_40": "58X40" in size_codes,
            "marking_75_120": "75X120" in size_codes,
            "set_yes": is_yes(payload.get("set_build")),
            "set_no": not is_yes(payload.get("set_build")),
            "pull_from_bag": is_yes(payload.get("pull_from_bag")),
            "insert_yes": is_yes(payload.get("insert_needed")),
            "insert_no": not is_yes(payload.get("insert_needed")),
            "bubble_wrap_yes": is_yes(payload.get("bubble_wrap_needed")),
            "bubble_wrap_no": not is_yes(payload.get("bubble_wrap_needed")),
            "bag_replace_yes": is_yes(payload.get("bag_replace_needed")),
            "bag_replace_no": not is_yes(payload.get("bag_replace_needed")),
            "box_replace_yes": is_yes(payload.get("box_replace_needed")),
            "box_replace_no": not is_yes(payload.get("box_replace_needed")),
            "shrink_wrap_yes": is_yes(payload.get("shrink_wrap_needed")),
            "shrink_wrap_no": not is_yes(payload.get("shrink_wrap_needed")),
        }
        defect_percent_raw = processing_views.re.sub(r"[^0-9.,]", "", raw_value("defect_percent")).replace(",", ".")
        defect_key = ""
        if defect_percent_raw:
            try:
                defect_value = int(float(defect_percent_raw))
                if defect_value in {0, 10, 20, 100}:
                    defect_key = str(defect_value)
            except (TypeError, ValueError):
                defect_key = ""
        tech_checks["defect_0"] = defect_key == "0"
        tech_checks["defect_10"] = defect_key == "10"
        tech_checks["defect_20"] = defect_key == "20"
        tech_checks["defect_100"] = defect_key == "100"

        marking_sticker_raw = str(payload.get("marking_5840_qty") or "").strip()
        if not marking_sticker_raw:
            marking_sticker_raw = str(payload.get("marking_5840_each_qty") or "").strip()
        marking_sticker_count = 0
        if marking_sticker_raw:
            match = processing_views.re.search(r"\d+", marking_sticker_raw)
            if match:
                try:
                    marking_sticker_count = int(match.group(0))
                except (TypeError, ValueError):
                    marking_sticker_count = 0
        tech_checks["marking_sticker_1"] = marking_sticker_count == 1
        tech_checks["marking_sticker_2"] = marking_sticker_count == 2
        tech_checks["marking_sticker_3"] = marking_sticker_count == 3
        tech_checks["marking_cz"] = bool(raw_value("marking_5840_each_qty"))
        tech_checks["marking_info"] = bool(raw_value("marking_info"))
        tech_checks["marking_block_visible"] = (
            tech_checks["marking_sticker_1"]
            or tech_checks["marking_sticker_2"]
            or tech_checks["marking_sticker_3"]
            or tech_checks["marking_cz"]
            or tech_checks["marking_info"]
            or bool(size_codes)
        )
        tech_checks["bubble_wrap_supply_client"] = contains_keyword(payload.get("bubble_wrap_supply"), ("клиент", "client"))
        tech_checks["bubble_wrap_supply_fullbox"] = contains_keyword(payload.get("bubble_wrap_supply"), ("фул", "full"))
        tech_checks["bag_replace_supply_client"] = contains_keyword(payload.get("bag_replace_supply"), ("клиент", "client"))
        tech_checks["bag_replace_supply_fullbox"] = contains_keyword(payload.get("bag_replace_supply"), ("фул", "full"))
        tech_checks["box_replace_supply_client"] = contains_keyword(payload.get("box_replace_supply"), ("клиент", "client"))
        tech_checks["box_replace_supply_fullbox"] = contains_keyword(payload.get("box_replace_supply"), ("фул", "full"))
        tech_checks["set_block_visible"] = any(has_payload_value(key) for key in ("set_build", "set_qty", "pull_from_bag"))
        tech_checks["insert_block_visible"] = any(
            has_payload_value(key) for key in ("insert_needed", "insert_types", "insert_other", "insert_qty")
        )
        tech_checks["bubble_wrap_block_visible"] = any(
            has_payload_value(key)
            for key in ("bubble_wrap_needed", "bubble_wrap_type", "bubble_wrap_size", "bubble_wrap_qty", "bubble_wrap_supply")
        )
        tech_checks["bag_replace_block_visible"] = any(
            has_payload_value(key)
            for key in ("bag_replace_needed", "bag_replace_type", "bag_replace_size", "bag_replace_qty", "bag_replace_supply")
        )
        tech_checks["box_replace_block_visible"] = any(
            has_payload_value(key)
            for key in ("box_replace_needed", "box_replace_type", "box_replace_size", "box_replace_qty", "box_replace_supply")
        )
        tech_checks["shrink_wrap_block_visible"] = any(
            has_payload_value(key)
            for key in ("shrink_wrap_needed", "shrink_wrap_type", "shrink_wrap_size", "shrink_wrap_qty")
        )
        table_rows = []
        for index, row in enumerate([row for row in (ctx.get("card_rows") or []) if isinstance(row, dict)]):
            table_rows.append(
                {
                    "number": str(index + 1),
                    "size": str(row.get("size") or "").strip() or "-",
                    "barcode": str(row.get("barcode") or "").strip() or "-",
                }
            )
        return {
            "card_page_url": card_url,
            "tech": tech,
            "tech_checks": tech_checks,
            "tech_table_rows": table_rows,
        }

    @classmethod
    def build_processing_label_print_page_context(
        cls,
        *,
        ctx: dict,
        order_id: str,
        card_id: str,
        request,
        payload: dict,
        agency,
    ) -> dict:
        processing_views = cls._views()
        label_base: dict = {}
        try:
            label_base = json.loads(ctx.get("label_base_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            label_base = {}
        card_rows = ctx.get("card_rows") or []
        barcodes = [str(row.get("barcode") or "").strip() for row in card_rows if isinstance(row, dict)]
        barcodes = [value for value in barcodes if value]
        codes_map: dict[tuple[str, str], list[str]] = {}
        if order_id and barcodes:
            base_qs = MarkingCode.objects.filter(
                order_type="processing",
                used_at__isnull=True,
                printed_at__isnull=True,
                barcode__in=barcodes,
            )
            if agency:
                base_qs = base_qs.filter(agency=agency)
            reserved_qs = base_qs.filter(order_id=order_id).order_by("created_at")
            free_qs = base_qs.filter(processing_views.Q(order_id__isnull=True) | processing_views.Q(order_id="")).order_by(
                "created_at"
            )
            codes_qs = list(reserved_qs.values("barcode", "size", "code")) + list(free_qs.values("barcode", "size", "code"))
            for entry in codes_qs:
                barcode = str(entry.get("barcode") or "").strip()
                if not barcode:
                    continue
                size_key = str(entry.get("size") or "").strip().lower()
                code = str(entry.get("code") or "").strip()
                if not code:
                    continue
                codes_map.setdefault((barcode, size_key), []).append(code)

        marking_qty = processing_views._parse_qty_value(payload.get("marking_5840_qty")) or 0
        marking_each_qty = processing_views._parse_qty_value(payload.get("marking_5840_each_qty")) or 0
        multiplier_no_cz = marking_qty if marking_qty > 0 else 1
        multiplier_cz = marking_each_qty if marking_each_qty > 0 else 1
        for row in card_rows:
            if not isinstance(row, dict):
                continue
            barcode = str(row.get("barcode") or "").strip()
            size_key = str(row.get("size") or "").strip().lower()
            qty_value = processing_views._parse_qty_value(row.get("qty"))
            base_qty = qty_value if qty_value is not None else 0
            row["qty_value"] = base_qty
            row["print_qty_no_cz"] = base_qty * multiplier_no_cz
            row["print_qty_cz"] = base_qty * multiplier_cz
            codes = codes_map.get((barcode, size_key)) or codes_map.get((barcode, "")) or [] if barcode else []
            row["cz_codes"] = codes
            row["cz_codes_json"] = json.dumps(codes, ensure_ascii=True)

        default_row = None
        for row in card_rows:
            if isinstance(row, dict) and row.get("barcode"):
                default_row = row
                break
        if default_row is None and card_rows:
            default_row = card_rows[0] if isinstance(card_rows[0], dict) else {}
        if default_row is None:
            default_row = {}
        default_size = str(default_row.get("size") or "").strip()
        default_barcode = str(default_row.get("barcode") or "").strip()
        default_cz = ""
        if isinstance(default_row, dict):
            cz_codes = default_row.get("cz_codes") or []
            if cz_codes:
                default_cz = str(cz_codes[0] or "").strip()

        label_sample = {
            "article": label_base.get("article") or "",
            "name": label_base.get("name") or "",
            "size": default_size,
            "brand": label_base.get("brand") or "",
            "subject": label_base.get("subject") or "",
            "color": label_base.get("color") or "",
            "composition": label_base.get("composition") or "",
            "supplier": label_base.get("supplier") or "",
            "country": label_base.get("country") or "",
            "barcode_extra": label_base.get("barcode_extra") or "",
            "cz_code": default_cz,
        }
        label_sizes = [entry for entry in processing_views.LABEL_SIZES if entry.get("key") in {"item", "item_cz"}]

        agent_status = processing_views.load_print_agent_status()
        agent_name = str(agent_status.get("agent") or "").strip() or "неизвестно"
        last_seen_raw = agent_status.get("last_seen")
        last_seen_text = "нет данных"
        is_online = False
        if last_seen_raw:
            try:
                last_seen = processing_views.datetime.fromisoformat(str(last_seen_raw))
                if timezone.is_naive(last_seen):
                    last_seen = timezone.make_aware(last_seen)
                last_seen_text = timezone.localtime(last_seen).strftime("%d.%m.%Y %H:%M:%S")
                is_online = (timezone.now() - last_seen) <= processing_views.timedelta(seconds=20)
            except (TypeError, ValueError):
                last_seen_text = str(last_seen_raw)

        pending_count = ProcessingPrintJob.objects.filter(status=ProcessingPrintJob.STATUS_PENDING).count()
        printing_count = ProcessingPrintJob.objects.filter(status=ProcessingPrintJob.STATUS_PRINTING).count()
        failed_count = ProcessingPrintJob.objects.filter(status=ProcessingPrintJob.STATUS_FAILED).count()
        last_job = ProcessingPrintJob.objects.order_by("-updated_at").first()
        last_error = ""
        last_job_time = ""
        if last_job:
            last_job_time = timezone.localtime(last_job.updated_at).strftime("%d.%m.%Y %H:%M:%S")
            if last_job.status == ProcessingPrintJob.STATUS_FAILED:
                last_error = last_job.error or "ошибка без описания"
        paused = bool(agent_status.get("paused"))
        if paused:
            print_status = "Печать остановлена"
        elif pending_count:
            print_status = f"В очереди: {pending_count}"
            if not is_online:
                print_status = f"{print_status} (агент не активен)"
        elif last_job and last_job.status == ProcessingPrintJob.STATUS_FAILED:
            print_status = "Ошибка печати"
        elif last_job and last_job.status == ProcessingPrintJob.STATUS_PRINTING:
            print_status = "Печать выполняется"
        else:
            print_status = "Готов к печати"
        agent_line = f"{agent_name} · {last_seen_text}" if last_seen_text else agent_name

        article_param = str(request.GET.get("article") or "").strip()
        card_article = (ctx.get("card") or {}).get("article") or ""
        card_path_id = card_id or card_article or ""
        processing_card_url = f"/orders/processing/{order_id}/card/{card_path_id}/"
        if article_param:
            processing_card_url = f"{processing_card_url}?{urlencode({'article': article_param})}"

        return {
            "label_sample": label_sample,
            "label_sample_barcode": default_barcode,
            "label_rows": card_rows,
            "label_sizes": label_sizes,
            "print_status_line": print_status,
            "print_agent_line": agent_line,
            "print_last_error": last_error,
            "print_last_job_time": last_job_time,
            "print_queue_pending": pending_count,
            "print_queue_printing": printing_count,
            "print_queue_failed": failed_count,
            "print_paused": paused,
            "processing_card_url": processing_card_url,
        }

    @classmethod
    def handle_processing_card_action(
        cls,
        *,
        order_id: str,
        request,
        action: str,
    ) -> ProcessingCardActionResult:
        entries = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .select_related("agency")
            .order_by("created_at")
        )
        if not entries:
            return ProcessingCardActionResult(status="missing_order", redirect_to="/orders/")
        latest = entries[-1]
        payload = dict(latest.payload or {})
        card_id = str(request.POST.get("card_id") or "").strip()
        redirect_to = cls._processing_card_redirect_target(request)
        role = get_request_role(request)
        processing_views = cls._views()

        if action == "finish_card":
            if role not in {"storekeeper", "processing_head"}:
                return ProcessingCardActionResult(status="forbidden", redirect_to=redirect_to)
            cards = payload.get("cards") or []
            if not card_id and len(cards) == 1 and isinstance(cards[0], dict):
                card_id = processing_views._processing_card_id(cards[0])
            cls._mark_processing_card_processed(payload, card_id=card_id, request=request)
            log_order_action(
                "update",
                order_id=order_id,
                order_type="processing",
                user=request.user if request.user.is_authenticated else None,
                agency=latest.agency if latest else None,
                description="Обработка завершена по карте товара",
                payload=payload,
            )
            return ProcessingCardActionResult(status="ok", redirect_to=request.get_full_path())

        if action not in {"save_results", "return_to_processing"}:
            return ProcessingCardActionResult(status="forbidden", redirect_to=redirect_to)

        if action == "save_results" and role not in {"processing_head", "head_manager", "director", "admin"}:
            return ProcessingCardActionResult(status="forbidden", redirect_to=redirect_to)
        if action == "return_to_processing" and role not in {
            "storekeeper",
            "processing_head",
            "head_manager",
            "director",
            "admin",
        }:
            return ProcessingCardActionResult(status="forbidden", redirect_to=redirect_to)

        results, has_results = cls._collect_processing_card_results(request, card_id=card_id)
        if has_results and action in {"save_results", "return_to_processing"}:
            payload["processing_results"] = cls._merge_processing_card_results(payload, results)
            payload["processing_results_updated_at"] = timezone.localtime().isoformat()
            log_order_action(
                "result",
                order_id=order_id,
                order_type="processing",
                user=request.user if request.user.is_authenticated else None,
                agency=latest.agency if latest else None,
                description="Результаты обработки",
                payload=payload,
            )

        if action == "save_results":
            ready_card_ids = processing_views._processing_results_ready_card_ids(payload, include_shipping=False)
            if card_id and card_id.strip().lower() in ready_card_ids:
                card_marked = cls._mark_processing_card_processed(payload, card_id=card_id, request=request)
                if card_marked:
                    log_order_action(
                        "update",
                        order_id=order_id,
                        order_type="processing",
                        user=request.user if request.user.is_authenticated else None,
                        agency=latest.agency if latest else None,
                        description="Карта обработки автоматически отмечена как выполненная после сохранения результатов",
                        payload=payload,
                    )
            return ProcessingCardActionResult(status="ok", redirect_to=redirect_to)

        changed = cls._mark_processing_card_processed(payload, card_id=card_id, request=request)
        if changed:
            log_order_action(
                "update",
                order_id=order_id,
                order_type="processing",
                user=request.user if request.user.is_authenticated else None,
                agency=latest.agency if latest else None,
                description="Карта обработки отмечена как выполненная",
                payload=payload,
            )
        return ProcessingCardActionResult(status="ok", redirect_to=redirect_to)

    @classmethod
    def scan_processing_flow_marking(cls, *, order_id: str, request) -> ProcessingMarkingScanResult:
        processing_views = cls._views()
        role = get_request_role(request)
        if role not in {"storekeeper", "processing_head", "processing_worker", "head_manager", "director", "admin", "manager"}:
            return ProcessingMarkingScanResult(
                status="forbidden",
                http_status=403,
                payload={"ok": False, "error": "Доступ запрещен."},
            )
        entries = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .select_related("agency")
            .order_by("created_at")
        )
        if not entries:
            return ProcessingMarkingScanResult(
                status="missing_order",
                http_status=404,
                payload={"ok": False, "error": "Заявка не найдена."},
            )
        latest = entries[-1]
        payload = processing_views._latest_payload_from_entries(entries)
        marking_each_qty = processing_views._parse_qty_value(payload.get("marking_5840_each_qty")) or 0
        if marking_each_qty <= 0:
            return ProcessingMarkingScanResult(
                status="not_required",
                http_status=400,
                payload={"ok": False, "error": "ЧЗ не требуется."},
            )
        data = processing_views._parse_json_body(request)
        if data is None:
            return ProcessingMarkingScanResult(
                status="invalid_json",
                http_status=400,
                payload={"ok": False, "error": "Некорректный JSON."},
            )
        code = processing_views._normalize_marking_code(data.get("code") or "")
        box_barcode = str(data.get("box_barcode") or "").strip()
        agent_id = str(data.get("agent_id") or data.get("agentId") or "").strip()
        if not code:
            return ProcessingMarkingScanResult(
                status="missing_code",
                http_status=400,
                payload={"ok": False, "error": "Код ЧЗ не указан."},
            )
        if not box_barcode:
            return ProcessingMarkingScanResult(
                status="missing_box",
                http_status=400,
                payload={"ok": False, "error": "Не выбран короб."},
            )
        if not agent_id:
            return ProcessingMarkingScanResult(
                status="missing_agent",
                http_status=400,
                payload={"ok": False, "error": "Не указан агент."},
            )
        session = processing_views._flow_session_for_request(order_id, agent_id, request, create=False)
        if not session:
            return ProcessingMarkingScanResult(
                status="missing_session",
                http_status=409,
                payload={"ok": False, "error": "Контекст агента не найден."},
            )
        processed_cards, placed_cards = processing_views._processing_card_sets(payload)
        ready_cards = processed_cards - placed_cards if processed_cards else set()
        items = processing_views._processing_receiving_items(
            payload,
            latest.agency_id,
            ready_cards if ready_cards else None,
        )
        if not items:
            return ProcessingMarkingScanResult(
                status="no_items",
                http_status=400,
                payload={"ok": False, "error": "Нет товаров для размещения."},
            )
        allowed_map: dict[tuple[str, str], dict] = {}
        for item in items:
            sku_value = str(item.get("sku_code") or "").strip()
            size_value = str(item.get("size") or "").strip()
            if not sku_value:
                continue
            allowed_map[(sku_value.lower(), size_value.lower())] = item
        if not allowed_map:
            return ProcessingMarkingScanResult(
                status="no_allowed_items",
                http_status=400,
                payload={"ok": False, "error": "Нет товаров для размещения."},
            )
        now = timezone.localtime()
        code_variants = {code}
        if "\x1d" in code:
            code_variants.add(code.replace("\x1d", "_x001D_"))
        with transaction.atomic():
            existing = MarkingCode.objects.select_for_update().filter(code__in=list(code_variants)).first()
            if not existing:
                return ProcessingMarkingScanResult(
                    status="missing_marking",
                    http_status=404,
                    payload={"ok": False, "error": "Код ЧЗ не найден."},
                )
            if existing.used_at:
                return ProcessingMarkingScanResult(
                    status="already_used",
                    http_status=409,
                    payload={"ok": False, "error": "Код уже использован."},
                )
            if latest.agency_id and existing.agency_id and existing.agency_id != latest.agency_id:
                return ProcessingMarkingScanResult(
                    status="other_agency",
                    http_status=409,
                    payload={"ok": False, "error": "Код принадлежит другому клиенту."},
                )
            if existing.order_type and existing.order_type != "processing":
                return ProcessingMarkingScanResult(
                    status="other_process",
                    http_status=409,
                    payload={"ok": False, "error": "Код закреплен в другом процессе."},
                )
            if existing.order_id and existing.order_id != order_id:
                return ProcessingMarkingScanResult(
                    status="other_order",
                    http_status=409,
                    payload={"ok": False, "error": "Код закреплен за другой заявкой."},
                )
            sku_code = str(existing.sku_code or "").strip()
            size = str(existing.size or "").strip()
            if not sku_code:
                return ProcessingMarkingScanResult(
                    status="missing_sku",
                    http_status=409,
                    payload={"ok": False, "error": "У кода нет артикула."},
                )
            sku_key = sku_code.lower()
            if not size:
                candidate_sizes = {
                    str(item.get("size") or "").strip()
                    for item in items
                    if str(item.get("sku_code") or "").strip().lower() == sku_key
                }
                if len(candidate_sizes) == 1:
                    size = candidate_sizes.pop()
            allowed_key = (sku_key, size.lower())
            if allowed_key not in allowed_map:
                return ProcessingMarkingScanResult(
                    status="missing_position",
                    http_status=409,
                    payload={"ok": False, "error": "Позиция не найдена в заявке."},
                )
            allowed_item = allowed_map[allowed_key]
            allowed_qty = processing_views._parse_qty_value(allowed_item.get("actual_qty")) or 0
            used_qty = MarkingCode.objects.filter(
                order_type="processing",
                order_id=order_id,
                sku_code=sku_code,
                size=size,
                used_at__isnull=False,
            ).count()
            if allowed_qty and used_qty >= allowed_qty:
                return ProcessingMarkingScanResult(
                    status="limit_reached",
                    http_status=409,
                    payload={"ok": False, "error": "Количество ЧЗ уже закрыто."},
                )
            if existing.box_barcode and existing.box_barcode != box_barcode:
                return ProcessingMarkingScanResult(
                    status="other_box",
                    http_status=409,
                    payload={"ok": False, "error": "Код закреплен за другим коробом."},
                )
            update_fields: list[str] = []
            if not existing.order_id:
                existing.order_id = order_id
                update_fields.append("order_id")
            if not existing.order_type:
                existing.order_type = "processing"
                update_fields.append("order_type")
            if not existing.size and size:
                existing.size = size
                update_fields.append("size")
            if box_barcode and not existing.box_barcode:
                existing.box_barcode = box_barcode
                update_fields.append("box_barcode")
            if not existing.sku:
                sku_qs = processing_views.SKU.objects.filter(sku_code=sku_code)
                if latest.agency_id:
                    sku = sku_qs.filter(agency_id=latest.agency_id).first() or sku_qs.filter(
                        agency__isnull=True
                    ).first()
                else:
                    sku = sku_qs.first()
                if sku:
                    existing.sku = sku
                    update_fields.append("sku")
            existing.used_at = now
            existing.used_by = request.user if request.user.is_authenticated else None
            update_fields.extend(["used_at", "used_by"])
            existing.save(update_fields=update_fields)
        return ProcessingMarkingScanResult(
            status="ok",
            http_status=200,
            payload={
                "ok": True,
                "sku_code": allowed_item.get("sku_code") or sku_code,
                "size": allowed_item.get("size") or size,
                "name": allowed_item.get("name") or "",
                "box_barcode": box_barcode,
            },
        )

    @classmethod
    def assign_processing_packaging(cls, *, order_id: str, request) -> ProcessingPackagingAssignmentResult:
        processing_views = cls._views()
        employee_id = str(request.POST.get("assignee_id") or request.POST.get("worker_id") or "").strip()
        if not employee_id:
            return ProcessingPackagingAssignmentResult(
                status="missing_worker",
                redirect_to=f"/orders/processing/{order_id}/work/?assign_error=missing_worker",
            )
        assignee = Employee.objects.filter(pk=employee_id, role="processing_worker", is_active=True).first()
        if not assignee:
            return ProcessingPackagingAssignmentResult(
                status="invalid_worker",
                redirect_to=f"/orders/processing/{order_id}/work/?assign_error=invalid_worker",
            )
        entries = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .select_related("agency")
            .order_by("created_at")
        )
        if not entries:
            return ProcessingPackagingAssignmentResult(
                status="invalid_order",
                redirect_to=f"/orders/processing/{order_id}/work/?assign_error=invalid_order",
            )
        latest = entries[-1]
        if processing_views._processing_packaging_task_exists(str(order_id), assignee):
            return ProcessingPackagingAssignmentResult(
                status="already_assigned",
                redirect_to=f"/orders/processing/{order_id}/work/?assign_error=already_assigned",
            )
        status_entry = processing_views._current_status_entry(entries) or latest
        payload = status_entry.payload or {}
        cards_total_count = processing_views._processing_cards_total(payload)
        processed_cards, _ = processing_views._processing_card_sets(payload)
        all_cards_processed = cards_total_count <= 0 or len(processed_cards) >= cards_total_count
        results_ready_for_flow = processing_views._processing_results_are_ready(payload, include_shipping=False)
        placement_completed = processing_views._flow_closed_from_entries(entries)
        can_open_processing_flow = bool(
            str(order_id)
            and not placement_completed
            and all_cards_processed
            and results_ready_for_flow
        )
        if can_open_processing_flow:
            processing_views._create_processing_packaging_task(str(order_id), assignee, request.user)
            log_order_action(
                "update",
                order_id=order_id,
                order_type="processing",
                user=request.user if request.user.is_authenticated else None,
                agency=latest.agency if latest else None,
                description=f"Поручена упаковка в короба: {assignee.full_name}",
                payload={
                    "packing_assignment_state": "dispatched",
                    "packing_assignee_id": assignee.id,
                    "packing_assignee": assignee.full_name,
                    "packing_assignee_role": assignee.role,
                    "packing_assignment_dispatch_mode": "direct",
                },
            )
            return ProcessingPackagingAssignmentResult(
                status="ok",
                redirect_to=f"/orders/processing/{order_id}/work/?assign=ok",
            )
        log_order_action(
            "update",
            order_id=order_id,
            order_type="processing",
            user=request.user if request.user.is_authenticated else None,
            agency=latest.agency if latest else None,
            description=f"Поручение на упаковку сохранено до выполнения условий раскоробовки: {assignee.full_name}",
            payload={
                "packing_assignment_state": "pending",
                "packing_assignee_id": assignee.id,
                "packing_assignee": assignee.full_name,
                "packing_assignee_role": assignee.role,
                "packing_assignment_dispatch_mode": "deferred",
            },
        )
        return ProcessingPackagingAssignmentResult(
            status="deferred",
            redirect_to=f"/orders/processing/{order_id}/work/?assign=deferred",
        )

    @classmethod
    def build_processing_detail_page_context(
        cls,
        *,
        ctx: dict,
        order_id: str,
        entries_list,
        request,
        payload_from_entries,
    ) -> dict:
        processing_views = cls._views()
        payload = payload_from_entries(entries_list)
        audience = "default"
        role = get_request_role(request)
        if role in {"processing_head", "processing_worker"}:
            audience = "processing"
        elif role == "storekeeper":
            audience = "storekeeper"
        updates: dict = {
            "status_label": WarehouseGoodsStateResolver.resolve_for_processing_order(
                order_id=str(order_id or ""),
                agency=entries_list[-1].agency if entries_list else None,
                payload=payload,
            ).label_for(audience)
        }

        stock_rows_raw = payload.get("stock_rows") or []
        size_rows_raw = payload.get("size_rows") or []
        unboxing_rows_raw = payload.get("unboxing_rows") or []
        size_rows = []
        if isinstance(stock_rows_raw, list) and stock_rows_raw:
            for row in stock_rows_raw:
                if not isinstance(row, dict):
                    continue
                size_rows.append(
                    {
                        "article": row.get("article") or "",
                        "size": row.get("size") or "",
                        "barcode": row.get("barcode") or "",
                        "qty": row.get("qty") or "",
                    }
                )
        elif isinstance(size_rows_raw, list):
            for row in size_rows_raw:
                if not isinstance(row, dict):
                    continue
                size_rows.append(
                    {
                        "article": row.get("size_no") or "",
                        "size": row.get("size_value") or "",
                        "barcode": row.get("barcode") or "",
                        "qty": row.get("recount_qty") or "",
                    }
                )
        unboxing_rows = []
        if isinstance(unboxing_rows_raw, list):
            for row in unboxing_rows_raw:
                if not isinstance(row, dict):
                    continue
                unboxing_rows.append(
                    {
                        "date": row.get("date") or "",
                        "box_size": row.get("box_size") or "",
                        "multiple": row.get("multiple") or "",
                        "box_qty": row.get("box_qty") or "",
                        "pallet_qty": row.get("pallet_qty") or "",
                        "storage_zone": row.get("storage_zone") or "",
                    }
                )

        insert_types = payload.get("insert_types") or []
        if isinstance(insert_types, str):
            insert_types = [insert_types] if insert_types else []
        insert_other = str(payload.get("insert_other") or "").strip()
        if insert_other:
            insert_types = list(insert_types) + [insert_other]

        marking_stickers = payload.get("marking_stickers") or []
        if isinstance(marking_stickers, str):
            marking_stickers = [marking_stickers] if marking_stickers else []
        marking_sizes = payload.get("marking_sizes") or []
        if isinstance(marking_sizes, str):
            marking_sizes = [marking_sizes] if marking_sizes else []
        processing_params = processing_views._processing_params_from_payload(payload)

        def format_pack(prefix: str, title: str):
            needed = processing_views._format_payload_value(payload.get(f"{prefix}_needed"))
            parts = []
            type_value = str(payload.get(f"{prefix}_type") or "").strip()
            size_value = str(payload.get(f"{prefix}_size") or "").strip()
            qty_value = str(payload.get(f"{prefix}_qty") or "").strip()
            supply_value = str(payload.get(f"{prefix}_supply") or "").strip()
            if type_value:
                parts.append(f"Тип: {type_value}")
            if size_value:
                parts.append(f"Размер: {size_value}")
            if qty_value:
                parts.append(f"Кол-во: {qty_value}")
            if supply_value:
                parts.append(f"Закупка: {supply_value}")
            value = needed
            if parts:
                joiner = ", ".join(parts)
                if value == "-":
                    value = joiner
                else:
                    value = f"{value}; {joiner}"
            return {"label": title, "value": value}

        processing_fields = [
            {"label": "Наименование товара", "value": processing_views._format_payload_value(payload.get("product_name"))},
            {"label": "Маркетплейс", "value": processing_views._format_payload_value(payload.get("marketplace"))},
            {"label": "Поставщик", "value": processing_views._format_payload_value(payload.get("supplier"))},
            {"label": "Бренд", "value": processing_views._format_payload_value(payload.get("brand"))},
            {"label": "Предмет", "value": processing_views._format_payload_value(payload.get("subject"))},
            {"label": "Артикул", "value": processing_views._format_payload_value(payload.get("article"))},
            {"label": "Артикул ВБ", "value": processing_views._format_payload_value(payload.get("wb_article"))},
            {"label": "Цвет", "value": processing_views._format_payload_value(payload.get("color"))},
            {"label": "Состав", "value": processing_views._format_payload_value(payload.get("composition"))},
            {"label": "Пол", "value": processing_views._format_payload_value(payload.get("gender"))},
            {"label": "Сезон", "value": processing_views._format_payload_value(payload.get("season"))},
            {"label": "Заказ №", "value": processing_views._format_payload_value(payload.get("order_no"))},
            {"label": "Приобретение в 1С №", "value": processing_views._format_payload_value(payload.get("purchase_1c_no"))},
            {"label": "Приобретение в 1С от", "value": processing_views._format_payload_value(payload.get("purchase_1c_date"))},
            {"label": "Менеджер проекта", "value": processing_views._format_payload_value(payload.get("project_manager"))},
            {"label": "Склад приемка", "value": processing_views._format_payload_value(payload.get("warehouse_receiving"))},
            {"label": "Склад упаковка", "value": processing_views._format_payload_value(payload.get("warehouse_packing"))},
            {"label": "Склад раскоробовка", "value": processing_views._format_payload_value(payload.get("warehouse_unpacking"))},
            {"label": "Замер в упаковке", "value": processing_views._format_payload_value(payload.get("measure_needed"))},
            {"label": "Вес (грамм)", "value": processing_views._format_payload_value(payload.get("measure_weight"))},
            {"label": "Ширина (см)", "value": processing_views._format_payload_value(payload.get("measure_width"))},
            {"label": "Высота (см)", "value": processing_views._format_payload_value(payload.get("measure_height"))},
            {"label": "Глубина (см)", "value": processing_views._format_payload_value(payload.get("measure_depth"))},
            {"label": "Проверка на брак (%)", "value": processing_views._format_payload_value(payload.get("defect_percent"))},
            {"label": "Проверка на брак (кол-во)", "value": processing_views._format_payload_value(payload.get("defect_qty"))},
            {"label": "Обрезание ниток (кол-во)", "value": processing_views._format_payload_value(payload.get("trim_threads_qty"))},
            {"label": "Скрепление скотчем (кол-во)", "value": processing_views._format_payload_value(payload.get("tape_qty"))},
            {"label": "Удаление бирки", "value": processing_views._format_payload_value(payload.get("remove_tag"))},
            {"label": "Удаление бирки (кол-во)", "value": processing_views._format_payload_value(payload.get("remove_tag_qty"))},
            {"label": "Скрепление бирки", "value": processing_views._format_payload_value(payload.get("attach_tag"))},
            {"label": "Скрепление бирки (кол-во)", "value": processing_views._format_payload_value(payload.get("attach_tag_qty"))},
            {"label": "Маркировка", "value": processing_views._format_payload_list(marking_stickers)},
            {"label": "Размеры стикеров", "value": processing_views._format_payload_list(marking_sizes)},
            {"label": "Информационный", "value": processing_views._format_payload_value(payload.get("marking_info"))},
            {"label": "Сборка набора", "value": processing_views._format_payload_value(payload.get("set_build"))},
            {"label": "Кол-во ед. в наборе", "value": processing_views._format_payload_value(payload.get("set_qty"))},
            {"label": "Доп. вложение", "value": processing_views._format_payload_value(payload.get("insert_needed"))},
            {"label": "Типы вложений", "value": processing_views._format_payload_list(insert_types)},
            {"label": "Вытянуть из мешка и наклеить ЧЗ", "value": processing_views._format_payload_value(payload.get("pull_from_bag"))},
            format_pack("bubble_wrap", "Упаковка в бабл пленку"),
            format_pack("bag_replace", "Замена пакета"),
            format_pack("box_replace", "Замена гофрокороба"),
            format_pack("shrink_wrap", "Термоусадочная упаковка"),
            {"label": "Кол-во оптовых мест", "value": processing_views._format_payload_value(payload.get("wholesale_places_qty"))},
            {"label": "Счет №", "value": processing_views._format_payload_value(payload.get("invoice_no"))},
            {"label": "Дата выставления", "value": processing_views._format_payload_value(payload.get("invoice_date"))},
            {"label": "Дата оплаты", "value": processing_views._format_payload_value(payload.get("payment_date"))},
            {"label": "Бухгалтер", "value": processing_views._format_payload_value(payload.get("accountant"))},
            {"label": "В архив (дата)", "value": processing_views._format_payload_value(payload.get("archive_date"))},
            {"label": "Исполнитель", "value": processing_views._format_payload_value(payload.get("executor_name"))},
            {"label": "Дата начала", "value": processing_views._format_payload_value(payload.get("start_date"))},
            {"label": "Дата окончания", "value": processing_views._format_payload_value(payload.get("end_date"))},
            {"label": "Дата приема заказа", "value": processing_views._format_payload_value(payload.get("receive_date"))},
            {"label": "Ответственный", "value": processing_views._format_payload_value(payload.get("responsible_name"))},
            {"label": "Комментарий", "value": processing_views._format_payload_value(payload.get("comments"))},
        ]

        updates["processing_fields"] = processing_fields
        updates["processing_size_rows"] = size_rows
        updates["processing_unboxing_rows"] = unboxing_rows
        updates["processing_params"] = processing_params
        updates["processing_meta"] = {
            "product_name": processing_views._format_payload_value(payload.get("product_name")),
            "order_no": processing_views._format_payload_value(payload.get("order_no")),
            "supplier": processing_views._format_payload_value(payload.get("supplier")),
            "brand": processing_views._format_payload_value(payload.get("brand")),
            "subject": processing_views._format_payload_value(payload.get("subject")),
        }
        updates["items"] = []
        updates["can_edit_order"] = False
        updates["can_send_to_warehouse"] = False
        updates["can_create_receiving_act"] = False
        updates["has_receiving_act"] = False
        updates["has_placement_act"] = False
        updates["act_label"] = ""
        updates["placement_act_label"] = ""
        updates["can_send_act_to_client"] = False

        status_payload = entries_list[-1].payload if entries_list else {}
        status_value = str(status_payload.get("status") or status_payload.get("submit_action") or "").lower()
        status_label = str(status_payload.get("status_label") or "").lower()
        can_manage = role in {"manager", "head_manager", "director", "admin"}
        client_view = bool(ctx.get("client_view"))
        processing_result = WarehouseGoodsStateResolver.resolve_for_processing_order(
            order_id=str(order_id or ""),
            agency=ctx.get("agency"),
            payload=status_payload,
        )
        warehouse_started = processing_result.code in cls._PROCESSING_WAREHOUSE_STARTED_CODES
        if warehouse_started or status_value == "processing_in_work" or "взята" in status_label:
            Task.objects.filter(route=f"/orders/processing/{order_id}/", assigned_to__role="processing_head").update(
                status="in_progress"
            )
        is_done = (
            warehouse_started
            or status_value in {"done", "completed", "closed", "finished", "processing_head", "processing_in_work"}
            or "выполн" in status_label
            or "утверж" in status_label
            or "передан" in status_label
            or "взята" in status_label
        )
        is_waiting = not warehouse_started and (
            status_value in {"sent_unconfirmed", "send", "submitted"} or "подтверждени" in status_label
        )
        updates["can_approve_processing"] = bool(can_manage and not client_view and is_waiting and not is_done)
        updates["can_edit_processing"] = bool(can_manage and not client_view and not is_done)
        updates["can_take_processing"] = WarehouseActionPolicy.can_take_processing(
            processing_result,
            role=role,
            client_view=client_view,
        ).allowed
        packers = []
        if order_id:
            packing_tasks = (
                Task.objects.filter(route=f"/orders/processing/{order_id}/flow/", assigned_to__role="processing_worker")
                .exclude(status="done")
                .select_related("assigned_to")
                .order_by("-created_at")
            )
            seen = set()
            for task in packing_tasks:
                assignee = task.assigned_to
                if not assignee:
                    continue
                label = assignee.full_name or str(assignee)
                if label in seen:
                    continue
                seen.add(label)
                packers.append(label)
        updates["processing_packers"] = packers
        updates["processing_packers_label"] = ", ".join(packers)
        updates["processing_work_url"] = f"/orders/processing/{order_id}/work/"
        if updates["can_edit_processing"]:
            agency = ctx.get("agency")
            if agency and getattr(agency, "id", None):
                updates["processing_edit_url"] = f"/orders/processing/?order={order_id}&agency={agency.id}&edit=1"
            else:
                updates["processing_edit_url"] = f"/orders/processing/?order={order_id}&edit=1"
        return updates

    @classmethod
    def handle_processing_detail_action(
        cls,
        *,
        order_id: str,
        request,
        order_type: str,
        payload_from_entries,
    ) -> ProcessingDetailActionResult:
        processing_views = cls._views()
        action = str(request.POST.get("action") or "").strip().lower()
        entries = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type=order_type)
            .select_related("agency")
            .order_by("created_at")
        )
        if not entries:
            return ProcessingDetailActionResult(status="missing_order", redirect_to="/orders/")
        latest = entries[-1]

        if action == "take_processing":
            role = get_request_role(request)
            if role not in {"storekeeper", "processing_head"}:
                return ProcessingDetailActionResult(status="forbidden", redirect_to=f"/orders/processing/{order_id}/")
            status_payload = latest.payload or {}
            command_result = processing_views.WarehouseCommandService.take_processing(
                order_id=str(order_id or ""),
                agency=latest.agency if latest else None,
                role=role,
                status_payload=status_payload,
                started_by=request.user if request.user.is_authenticated else None,
            )
            if command_result.status == "already_in_progress":
                return ProcessingDetailActionResult(status="already_in_progress", redirect_to=f"/orders/processing/{order_id}/work/")
            if command_result.status == "denied":
                return ProcessingDetailActionResult(status="denied", redirect_to=f"/orders/processing/{order_id}/")
            payload = dict(payload_from_entries(entries))
            payload["status"] = "processing_in_work"
            payload["status_label"] = "Взята в работу"
            payload["work_started_at"] = timezone.localtime().isoformat()
            log_order_action(
                "status",
                order_id=order_id,
                order_type=order_type,
                user=request.user if request.user.is_authenticated else None,
                agency=latest.agency if latest else None,
                description="Заявка на обработку принята в работу",
                payload=payload,
            )
            Task.objects.filter(route=f"/orders/processing/{order_id}/", assigned_to__role="processing_head").update(
                status="in_progress"
            )
            return ProcessingDetailActionResult(status="ok", redirect_to=f"/orders/processing/{order_id}/work/")

        if action == "approve_processing":
            role = get_request_role(request)
            if role not in {"manager", "head_manager", "director", "admin"}:
                return ProcessingDetailActionResult(status="forbidden", redirect_to=f"/orders/processing/{order_id}/")
            status_payload = latest.payload or {}
            status_value = str(status_payload.get("status") or status_payload.get("submit_action") or "").lower()
            status_label = str(status_payload.get("status_label") or "").lower()
            processing_result = WarehouseGoodsStateResolver.resolve_for_processing_order(
                order_id=str(order_id or ""),
                agency=latest.agency if latest else None,
                payload=status_payload,
            )
            if processing_result.code in cls._PROCESSING_WAREHOUSE_STARTED_CODES:
                return ProcessingDetailActionResult(status="done", redirect_to=f"/orders/processing/{order_id}/")
            if status_value in {"done", "completed", "closed", "finished"} or "выполн" in status_label:
                return ProcessingDetailActionResult(status="done", redirect_to=f"/orders/processing/{order_id}/")
            payload = dict(payload_from_entries(entries))
            payload["status"] = "processing_head"
            payload["status_label"] = "Передано в обработку"
            payload["approved_at"] = timezone.localtime().isoformat()
            log_order_action(
                "status",
                order_id=order_id,
                order_type=order_type,
                user=request.user if request.user.is_authenticated else None,
                agency=latest.agency if latest else None,
                description="Заявка на обработку утверждена менеджером и передана в обработку",
                payload=payload,
            )
            Task.objects.filter(route=f"/orders/processing/{order_id}/", assigned_to__role="manager").exclude(status="done").update(status="done")
            processing_views._create_processing_head_task(order_id, latest.agency if latest else None, request, timezone.localtime())
            return ProcessingDetailActionResult(status="ok", redirect_to=f"/orders/processing/{order_id}/")

        comment = str(request.POST.get("comment") or "").strip()
        if comment:
            latest_comment = (
                OrderAuditEntry.objects.filter(order_id=order_id, order_type=order_type)
                .select_related("agency")
                .order_by("-created_at")
                .first()
            )
            log_order_action(
                "comment",
                order_id=order_id,
                order_type=order_type,
                user=request.user if request.user.is_authenticated else None,
                agency=latest_comment.agency if latest_comment else None,
                description=comment,
                payload={"comment": comment},
            )
        return ProcessingDetailActionResult(status="ok", redirect_to=f"/orders/processing/{order_id}/")

    @classmethod
    def processing_marking_availability(
        cls,
        *,
        request,
        data: dict,
    ) -> ProcessingJsonResult:
        processing_views = cls._views()
        items = data.get("items") or []
        order_id = str(data.get("order_id") or "").strip()
        client_agency = processing_views._client_agency_from_request(request)
        agency = client_agency
        if not agency:
            role = get_request_role(request)
            if role not in {"manager", "storekeeper", "head_manager", "director", "admin"}:
                return ProcessingJsonResult(
                    status="forbidden",
                    http_status=403,
                    payload={"ok": False, "error": "Доступ запрещен"},
                )
            agency_id = data.get("agency_id") or request.GET.get("client") or request.GET.get("agency")
            agency = processing_views.Agency.objects.filter(pk=agency_id).first() if agency_id else None
        if not agency:
            return ProcessingJsonResult(
                status="missing_agency",
                http_status=400,
                payload={"ok": False, "error": "Клиент не выбран"},
            )
        required: dict[str, int] = {}
        required_total = 0
        missing_barcodes = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            qty = processing_views._parse_qty_value(item.get("qty"))
            if not qty or qty <= 0:
                continue
            barcode = str(item.get("barcode") or "").strip()
            if not barcode:
                missing_barcodes += qty
                continue
            required_total += qty
            required[barcode] = required.get(barcode, 0) + qty
        available_map = processing_views._marking_available_by_barcode(agency, order_id)
        free_map = processing_views._marking_free_by_barcode(agency)
        details = []
        covered_total = 0
        missing_total = 0
        free_total = 0
        for barcode, need in required.items():
            have = available_map.get(barcode, 0)
            free_count = free_map.get(barcode, 0)
            covered = min(need, have)
            missing = max(need - have, 0)
            covered_total += covered
            missing_total += missing
            free_total += free_count
            details.append({"barcode": barcode, "required": need, "available": have, "missing": missing})
        return ProcessingJsonResult(
            status="ok",
            http_status=200,
            payload={
                "ok": True,
                "required": required_total,
                "available": covered_total,
                "missing": missing_total,
                "free": free_total,
                "missing_barcodes": missing_barcodes,
                "items": details,
            },
        )

    @classmethod
    def processing_marking_import(
        cls,
        *,
        request,
        cz_file,
        cards_payload,
        order_id: str,
    ) -> ProcessingJsonResult:
        processing_views = cls._views()
        client_agency = processing_views._client_agency_from_request(request)
        agency = client_agency
        if not agency:
            role = get_request_role(request)
            if role not in {"manager", "storekeeper", "head_manager", "director", "admin"}:
                return ProcessingJsonResult(
                    status="forbidden",
                    http_status=403,
                    payload={"ok": False, "error": "Доступ запрещен"},
                )
            agency_id = request.POST.get("agency_id") or request.GET.get("client") or request.GET.get("agency")
            agency = processing_views.Agency.objects.filter(pk=agency_id).first() if agency_id else None
        if not agency:
            return ProcessingJsonResult(
                status="missing_agency",
                http_status=400,
                payload={"ok": False, "error": "Клиент не выбран"},
            )
        payload = {}
        if cards_payload:
            payload["cards"] = cards_payload
        ok, import_result = processing_views._import_marking_codes(cz_file, payload, order_id, agency, request.user)
        if not ok:
            message = import_result.get("error") or "Ошибка импорта ЧЗ."
            return ProcessingJsonResult(
                status="error",
                http_status=400,
                payload={"ok": False, "error": message},
            )
        return ProcessingJsonResult(status="ok", http_status=200, payload={"ok": True, **import_result})

    @classmethod
    def enqueue_processing_print_job(
        cls,
        *,
        request,
        data: dict,
    ) -> ProcessingJsonResult:
        barcode = str(data.get("barcode") or "").strip()
        if not barcode:
            return ProcessingJsonResult(status="missing_barcode", http_status=400, payload={"ok": False, "error": "Barcode is required"})
        label_png_base64 = str(data.get("label_png_base64") or "").strip()
        if not label_png_base64:
            return ProcessingJsonResult(status="missing_label", http_status=400, payload={"ok": False, "error": "Label image is required"})
        try:
            label_width_mm = int(data.get("label_width_mm") or 58)
        except (TypeError, ValueError):
            label_width_mm = 58
        try:
            label_height_mm = int(data.get("label_height_mm") or 40)
        except (TypeError, ValueError):
            label_height_mm = 40
        job = ProcessingPrintJob.objects.create(
            order_id=str(data.get("order_id") or "").strip(),
            card_id=str(data.get("card_id") or "").strip(),
            article=str(data.get("article") or "").strip(),
            barcode=barcode,
            size=str(data.get("size") or "").strip(),
            printer_name=str(data.get("printer_name") or "").strip(),
            label_png_base64=label_png_base64,
            label_width_mm=label_width_mm,
            label_height_mm=label_height_mm,
            requested_by=request.user.username if request.user.is_authenticated else "",
            agent=str(data.get("agent_id") or "").strip(),
        )
        return ProcessingJsonResult(status="ok", http_status=200, payload={"ok": True, "job_id": job.id})

    @staticmethod
    def _resolve_print_agent_targets(agent_name: str) -> list[str]:
        value = str(agent_name or "").strip()
        if not value:
            return []
        targets: list[str] = [value]
        try:
            from agent.models import DeviceAgent

            agent = (
                DeviceAgent.objects.filter(
                    Q(agent_id=value) | Q(name=value) | Q(host=value)
                )
                .order_by("-last_seen", "-updated_at")
                .first()
            )
            if agent:
                for candidate in (agent.agent_id, agent.name, agent.host):
                    text = str(candidate or "").strip()
                    if text and text not in targets:
                        targets.append(text)
        except Exception:
            pass
        return targets

    @classmethod
    def _scoped_print_jobs_qs(cls, *, agent_name: str = "", statuses: list[str] | tuple[str, ...] | None = None):
        qs = ProcessingPrintJob.objects.all()
        if statuses:
            qs = qs.filter(status__in=list(statuses))
        targets = cls._resolve_print_agent_targets(agent_name)
        if targets:
            qs = qs.filter(agent__in=targets)
        return qs

    @classmethod
    def _print_queue_counts_for_agent(cls, *, agent_name: str = "") -> dict[str, int]:
        return {
            "pending": cls._scoped_print_jobs_qs(
                agent_name=agent_name,
                statuses=[ProcessingPrintJob.STATUS_PENDING],
            ).count(),
            "printing": cls._scoped_print_jobs_qs(
                agent_name=agent_name,
                statuses=[ProcessingPrintJob.STATUS_PRINTING],
            ).count(),
            "failed": cls._scoped_print_jobs_qs(
                agent_name=agent_name,
                statuses=[ProcessingPrintJob.STATUS_FAILED],
            ).count(),
        }

    @classmethod
    def processing_print_jobs_status(cls, *, agent_name: str = "") -> ProcessingJsonResult:
        processing_views = cls._views()
        agent_status = processing_views.load_print_agent_status()
        paused = bool(agent_status.get("paused"))
        counts = cls._print_queue_counts_for_agent(agent_name=agent_name)
        last_job = cls._scoped_print_jobs_qs(agent_name=agent_name).order_by("-updated_at").first()
        last_error = ""
        last_job_time = ""
        if last_job and last_job.updated_at:
            last_job_time = timezone.localtime(last_job.updated_at).strftime("%d.%m.%Y %H:%M:%S")
            if last_job.status == ProcessingPrintJob.STATUS_FAILED:
                last_error = last_job.error or "ошибка без описания"
        if paused:
            print_status = "Печать остановлена"
        elif counts["pending"]:
            print_status = f"В очереди: {counts['pending']}"
        elif last_job and last_job.status == ProcessingPrintJob.STATUS_FAILED:
            print_status = "Ошибка печати"
        elif counts["printing"]:
            print_status = "Печать выполняется"
        else:
            print_status = "Готов к печати"
        return ProcessingJsonResult(
            status="ok",
            http_status=200,
            payload={
                "ok": True,
                "paused": paused,
                "counts": counts,
                "status_line": print_status,
                "last_error": last_error,
                "last_job_time": last_job_time,
                "agent_id": str(agent_name or "").strip(),
            },
        )

    @classmethod
    def processing_print_jobs_next(cls, *, agent_name: str) -> ProcessingJsonResult:
        processing_views = cls._views()
        processing_views.save_print_agent_status(agent_name)
        if processing_views.load_print_agent_status().get("paused"):
            return ProcessingJsonResult(
                status="paused",
                http_status=200,
                payload={"ok": True, "has_job": False, "hasJob": False, "paused": True},
            )
        agent_targets = cls._resolve_print_agent_targets(agent_name)
        with transaction.atomic():
            pending_qs = ProcessingPrintJob.objects.select_for_update().filter(status=ProcessingPrintJob.STATUS_PENDING)
            if agent_targets:
                targeted_job = pending_qs.filter(agent__in=agent_targets).order_by("created_at").first()
                job = targeted_job or pending_qs.filter(agent="").order_by("created_at").first()
            else:
                job = pending_qs.filter(agent="").order_by("created_at").first()
            if not job:
                return ProcessingJsonResult(
                    status="empty",
                    http_status=200,
                    payload={"ok": True, "has_job": False, "hasJob": False},
                )
            job.status = ProcessingPrintJob.STATUS_PRINTING
            if not str(job.agent or "").strip() and agent_targets:
                job.agent = agent_targets[0]
            job.save(update_fields=["status", "agent", "updated_at"])
        return ProcessingJsonResult(
            status="ok",
            http_status=200,
            payload={
                "ok": True,
                "has_job": True,
                "hasJob": True,
                "job": processing_views._serialize_print_job(job),
            },
        )

    @classmethod
    def processing_print_jobs_complete(cls, *, data) -> ProcessingJsonResult:
        job_id = data.get("job_id") or data.get("id")
        if not job_id:
            return ProcessingJsonResult(status="missing_job_id", http_status=400, payload={"ok": False, "error": "job_id is required"})
        job = ProcessingPrintJob.objects.filter(pk=job_id).first()
        if not job:
            return ProcessingJsonResult(status="missing_job", http_status=404, payload={"ok": False, "error": "Job not found"})
        status_value = str(data.get("status") or "").strip().lower()
        error_text = str(data.get("error") or "").strip()
        if status_value not in {
            ProcessingPrintJob.STATUS_PRINTED,
            ProcessingPrintJob.STATUS_FAILED,
            ProcessingPrintJob.STATUS_PENDING,
        }:
            status_value = ProcessingPrintJob.STATUS_PRINTED
        job.status = status_value
        job.error = error_text
        job.save(update_fields=["status", "error", "updated_at"])
        return ProcessingJsonResult(status="ok", http_status=200, payload={"ok": True})

    @classmethod
    def processing_print_jobs_pause(cls, *, request, data: dict) -> ProcessingJsonResult:
        processing_views = cls._views()
        printer = str(data.get("printer") or request.POST.get("printer") or "").strip()
        target_agent = str(data.get("agent_id") or request.POST.get("agent_id") or "").strip()
        username = request.user.get_full_name().strip() if request.user.is_authenticated else ""
        if not username:
            username = request.user.username if request.user.is_authenticated else ""
        processing_views.set_print_agent_pause(True, by=username)
        if printer:
            processing_views._enqueue_agent_command("printer.pause", {"printer": printer}, agent_id=target_agent)
        return ProcessingJsonResult(
            status="ok",
            http_status=200,
            payload={
                "ok": True,
                "paused": True,
                "counts": cls._print_queue_counts_for_agent(agent_name=target_agent),
            },
        )

    @classmethod
    def processing_print_jobs_resume(cls, *, request, data: dict) -> ProcessingJsonResult:
        processing_views = cls._views()
        printer = str(data.get("printer") or request.POST.get("printer") or "").strip()
        target_agent = str(data.get("agent_id") or request.POST.get("agent_id") or "").strip()
        username = request.user.get_full_name().strip() if request.user.is_authenticated else ""
        if not username:
            username = request.user.username if request.user.is_authenticated else ""
        processing_views.set_print_agent_pause(False, by=username)
        if printer:
            processing_views._enqueue_agent_command("printer.resume", {"printer": printer}, agent_id=target_agent)
        return ProcessingJsonResult(
            status="ok",
            http_status=200,
            payload={
                "ok": True,
                "paused": False,
                "counts": cls._print_queue_counts_for_agent(agent_name=target_agent),
            },
        )

    @classmethod
    def processing_print_jobs_clear(cls, *, request, data: dict) -> ProcessingJsonResult:
        processing_views = cls._views()
        printer = str(data.get("printer") or request.POST.get("printer") or "").strip()
        target_agent = str(data.get("agent_id") or request.POST.get("agent_id") or "").strip()
        scope = str(data.get("scope") or request.POST.get("scope") or "pending").strip().lower()
        if scope not in {"pending", "failed", "all"}:
            return ProcessingJsonResult(status="invalid_scope", http_status=400, payload={"ok": False, "error": "invalid_scope"})
        qs = cls._scoped_print_jobs_qs(agent_name=target_agent)
        if scope == "pending":
            qs = qs.filter(status=ProcessingPrintJob.STATUS_PENDING)
        elif scope == "failed":
            qs = qs.filter(status=ProcessingPrintJob.STATUS_FAILED)
        else:
            qs = qs.filter(status__in=[ProcessingPrintJob.STATUS_PENDING, ProcessingPrintJob.STATUS_FAILED])
        deleted_count = qs.count()
        qs.delete()
        if printer:
            processing_views._enqueue_agent_command("printer.clear", {"printer": printer}, agent_id=target_agent)
        return ProcessingJsonResult(
            status="ok",
            http_status=200,
            payload={
                "ok": True,
                "deleted": deleted_count,
                "counts": cls._print_queue_counts_for_agent(agent_name=target_agent),
            },
        )

    @classmethod
    def processing_print_jobs_reset(cls, *, request, data: dict) -> ProcessingJsonResult:
        processing_views = cls._views()
        printer = str(data.get("printer") or request.POST.get("printer") or "").strip()
        target_agent = str(data.get("agent_id") or request.POST.get("agent_id") or "").strip()
        mode = str(data.get("mode") or request.POST.get("mode") or "pending").strip().lower()
        if mode not in {"pending", "failed"}:
            return ProcessingJsonResult(status="invalid_mode", http_status=400, payload={"ok": False, "error": "invalid_mode"})
        qs = cls._scoped_print_jobs_qs(
            agent_name=target_agent,
            statuses=[ProcessingPrintJob.STATUS_PRINTING],
        )
        if mode == "failed":
            updated = qs.update(status=ProcessingPrintJob.STATUS_FAILED, error="Сброшено вручную")
        else:
            updated = qs.update(status=ProcessingPrintJob.STATUS_PENDING, error="")
        if printer:
            processing_views._enqueue_agent_command("printer.clear", {"printer": printer}, agent_id=target_agent)
        return ProcessingJsonResult(
            status="ok",
            http_status=200,
            payload={
                "ok": True,
                "updated": updated,
                "counts": cls._print_queue_counts_for_agent(agent_name=target_agent),
            },
        )

    @classmethod
    def processing_print_jobs_recover(cls, *, request, data: dict) -> ProcessingJsonResult:
        processing_views = cls._views()
        printer = str(data.get("printer") or request.POST.get("printer") or "").strip()
        target_agent = str(data.get("agent_id") or request.POST.get("agent_id") or "").strip()
        username = request.user.get_full_name().strip() if request.user.is_authenticated else ""
        if not username:
            username = request.user.username if request.user.is_authenticated else ""
        processing_views.set_print_agent_pause(False, by=username)
        qs = cls._scoped_print_jobs_qs(
            agent_name=target_agent,
            statuses=[
                ProcessingPrintJob.STATUS_PENDING,
                ProcessingPrintJob.STATUS_PRINTING,
                ProcessingPrintJob.STATUS_FAILED,
            ],
        )
        deleted_count = qs.count()
        qs.delete()
        if printer:
            processing_views._enqueue_agent_command("printer.clear", {"printer": printer}, agent_id=target_agent)
            processing_views._enqueue_agent_command("printer.resume", {"printer": printer}, agent_id=target_agent)
        return ProcessingJsonResult(
            status="ok",
            http_status=200,
            payload={
                "ok": True,
                "paused": False,
                "deleted": deleted_count,
                "counts": cls._print_queue_counts_for_agent(agent_name=target_agent),
            },
        )

    @classmethod
    def build_processing_home_page_context(
        cls,
        *,
        request,
        submitted: bool,
        draft_saved: bool,
        error: str | None,
    ) -> dict:
        processing_views = cls._views()
        ctx: dict = {
            "submitted": submitted,
            "draft_saved": draft_saved,
            "error": error,
            "cabinet_url": resolve_cabinet_url(get_request_role(request)),
            "can_assign_packaging": get_request_role(request) in {
                "processing_head",
                "head_manager",
                "director",
                "admin",
            },
        }
        status = request.GET.get("status")
        status_label = cls._processing_home_status_label(status)
        ctx["order_number"] = request.GET.get("order", "")

        client_id = request.GET.get("client")
        agency_id = request.GET.get("agency")
        agency_key = client_id or agency_id
        client_agency = getattr(request, "_client_agency", None) or processing_views._client_agency_from_request(request)
        agency = client_agency or (processing_views.Agency.objects.filter(pk=agency_key).first() if agency_key else None)
        ctx["agency"] = agency
        ctx["client_view"] = bool(client_agency)
        ctx["draft_order_id"] = ""
        ctx["edit_order_id"] = ""
        order_id = request.GET.get("order")
        draft_payload = None
        role = get_request_role(request)
        edit_flag = (request.GET.get("edit") or "").strip().lower()
        edit_mode = edit_flag in {"1", "true", "yes"} and role in {
            "manager",
            "head_manager",
            "director",
            "admin",
        }
        if order_id:
            draft_entries = OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            if client_agency:
                draft_entries = draft_entries.filter(agency=client_agency)
            elif agency:
                draft_entries = draft_entries.filter(agency=agency)
            draft_entry = draft_entries.order_by("-created_at").first()
            if draft_entry:
                payload = draft_entry.payload or {}
                status_value = str(payload.get("submit_action") or payload.get("status") or "").lower()
                status_label_value = str(payload.get("status_label") or "").lower()
                is_draft = status_value == "draft" or "черновик" in status_label_value
                if is_draft:
                    ctx["draft_order_id"] = order_id
                    draft_payload = payload
                elif edit_mode:
                    ctx["edit_order_id"] = order_id
                    draft_payload = payload
        ctx["draft_payload"] = draft_payload or {}
        ctx["draft_payload_json"] = json.dumps(draft_payload or {}, ensure_ascii=True)
        if draft_payload:
            payload_status = draft_payload.get("status") or draft_payload.get("submit_action")
            payload_label = str(draft_payload.get("status_label") or "").strip()
            if ctx.get("draft_order_id"):
                status_label = cls._processing_home_status_label(payload_status, payload_label)
            else:
                warehouse_label = WarehouseGoodsStateResolver.resolve_for_processing_order(
                    order_id=str(order_id or ""),
                    agency=agency,
                    payload=draft_payload,
                ).label_for("default")
                status_label = warehouse_label or cls._processing_home_status_label(payload_status, payload_label)
        elif order_id and agency:
            latest_payload = draft_entry.payload or {} if 'draft_entry' in locals() and draft_entry else {}
            warehouse_label = WarehouseGoodsStateResolver.resolve_for_processing_order(
                order_id=str(order_id or ""),
                agency=agency,
                payload=latest_payload,
            ).label_for("default")
            status_label = warehouse_label or status_label
        ctx["status_label"] = status_label
        return ctx

    @classmethod
    def build_processing_directions_page_context(
        cls,
        *,
        request,
    ) -> dict:
        return {
            "cabinet_url": resolve_cabinet_url(get_request_role(request)),
            "return_url": (request.GET.get("return") or "").strip(),
            "return_url_json": json.dumps((request.GET.get("return") or "").strip(), ensure_ascii=True),
        }

    @classmethod
    def build_processing_stock_picker_page_context(
        cls,
        *,
        request,
    ) -> dict:
        processing_views = cls._views()
        client_id = request.GET.get("client")
        agency_id = request.GET.get("agency")
        agency_key = client_id or agency_id
        client_agency = getattr(request, "_client_agency", None) or processing_views._client_agency_from_request(request)
        agency = client_agency or (processing_views.Agency.objects.filter(pk=agency_key).first() if agency_key else None)
        exclude_order_id = str(request.GET.get("order") or "").strip()
        if not exclude_order_id:
            referer = str(request.META.get("HTTP_REFERER") or "").strip()
            if referer:
                referer_path = urlparse(referer).path or ""
                match = re.search(r"/orders/processing/([^/]+)/", referer_path)
                if match:
                    exclude_order_id = str(match.group(1) or "").strip()
        exclude_order_id = exclude_order_id or None
        return {
            "cabinet_url": resolve_cabinet_url(get_request_role(request)),
            "agency": agency,
            "client_view": bool(client_agency),
            "inventory_items_json": json.dumps(
                processing_views._inventory_items_for_agency(agency, exclude_order_id=exclude_order_id),
                ensure_ascii=True,
            ),
            "return_url": f"/orders/processing/?client={agency.id}" if agency else "/orders/processing/",
        }

    @classmethod
    def delete_processing_draft(
        cls,
        *,
        request,
        order_id: str,
    ):
        processing_views = cls._views()
        if request.method != "POST":
            return HttpResponseForbidden("Доступ запрещен")
        if not request.user.is_authenticated:
            return HttpResponseForbidden("Доступ запрещен")
        client_agency = processing_views._client_agency_from_request(request)
        if not client_agency:
            return HttpResponseForbidden("Доступ запрещен")
        entries = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="processing",
            agency=client_agency,
        ).order_by("-created_at")
        latest = entries.first()
        if not latest or not processing_views._is_draft_payload(latest.payload or {}):
            return HttpResponseForbidden("Доступ запрещен")
        MarkingCode.objects.filter(
            order_type="processing",
            order_id=order_id,
            agency=client_agency,
            used_at__isnull=True,
        ).update(order_id="")
        entries.delete()
        return redirect(f"/client/dashboard/?client={client_agency.id}")

    @classmethod
    def submit_processing(cls, *, request):
        processing_views = cls._views()
        autosave = str(request.POST.get("draft_autosave") or "").strip() == "1"

        def autosave_error(message: str):
            if not autosave:
                return None
            return JsonResponse({"ok": False, "error": message}, status=400)

        def render_home_error(message: str):
            error_response = autosave_error(message)
            if error_response:
                return error_response
            view = processing_views.ProcessingHomeView()
            view.setup(request)
            return view.get(request, error=message)

        submit_action = str(request.POST.get("submit_action") or "send").strip().lower()
        is_draft = submit_action == "draft"
        draft_order_id = str(request.POST.get("draft_order_id") or "").strip()
        edit_order_id = str(request.POST.get("edit_order_id") or "").strip()
        client_agency = getattr(request, "_client_agency", None) or processing_views._client_agency_from_request(request)
        if client_agency:
            agency = client_agency
        else:
            agency_id = request.POST.get("agency_id")
            agency = processing_views.Agency.objects.filter(pk=agency_id).first()
        if not agency:
            return render_home_error("Выберите клиента.")

        role = get_request_role(request)
        existing_entries = []
        preserved_status = ""
        preserved_label = ""
        preserved_submit_action = ""
        if edit_order_id:
            if role not in {"manager", "head_manager", "director", "admin"}:
                return HttpResponseForbidden("Доступ запрещен")
            existing_entries = list(
                OrderAuditEntry.objects.filter(order_id=edit_order_id, order_type="processing")
                .order_by("created_at")
            )
            if not existing_entries:
                return render_home_error("Заявка не найдена.")
            latest_payload = existing_entries[-1].payload or {}
            preserved_status = str(latest_payload.get("status") or latest_payload.get("submit_action") or "").strip()
            preserved_label = str(latest_payload.get("status_label") or "").strip()
            preserved_submit_action = str(latest_payload.get("submit_action") or "").strip()
            status_lower = preserved_status.lower()
            label_lower = preserved_label.lower()
            if status_lower in {"done", "completed", "closed", "finished"} or "выполн" in label_lower:
                message = "Заявка уже утверждена и недоступна для редактирования."
                return render_home_error(message)
            processing_result = WarehouseGoodsStateResolver.resolve_for_processing_order(
                order_id=str(edit_order_id or ""),
                agency=existing_entries[-1].agency if existing_entries else agency,
                payload=latest_payload,
            )
            if processing_result.code in cls._PROCESSING_WAREHOUSE_STARTED_CODES:
                message = "Заявка уже передана в обработку и недоступна для редактирования."
                return render_home_error(message)
            is_draft = False

        existing_draft_entries = []
        if draft_order_id:
            draft_qs = OrderAuditEntry.objects.filter(
                order_id=draft_order_id,
                order_type="processing",
                agency=agency,
            ).order_by("created_at")
            existing_draft_entries = list(draft_qs)
            latest_draft = existing_draft_entries[-1] if existing_draft_entries else None
            if not latest_draft or not processing_views._is_draft_payload(latest_draft.payload or {}):
                draft_order_id = ""
                existing_draft_entries = []

        cards_payload = []
        cards_json = str(request.POST.get("cards_json") or "").strip()
        if cards_json:
            try:
                parsed_cards = json.loads(cards_json)
            except json.JSONDecodeError:
                parsed_cards = []
            if isinstance(parsed_cards, list):
                for entry in parsed_cards:
                    if not isinstance(entry, dict):
                        continue
                    card_id = str(entry.get("id") or "").strip()
                    article = str(entry.get("article") or "").strip()
                    product_name = str(entry.get("product_name") or "").strip()
                    photo_url = str(entry.get("photo_url") or "").strip()
                    goods_type = str(entry.get("goods_type") or "").strip()
                    rows = []
                    for row in entry.get("rows") or []:
                        if not isinstance(row, dict):
                            continue
                        rows.append(
                            {
                                "article": str(row.get("article") or row.get("sku") or "").strip(),
                                "size": str(row.get("size") or "").strip(),
                                "barcode": str(row.get("barcode") or "").strip(),
                                "qty": row.get("qty"),
                            }
                        )
                    photo_field = str(entry.get("photo_field") or "").strip()
                    card_payload = {
                        "id": card_id,
                        "article": article,
                        "product_name": product_name,
                        "photo_url": photo_url,
                        "goods_type": goods_type,
                        "rows": rows,
                    }
                    if photo_field:
                        photo_file = request.FILES.get(photo_field)
                        if photo_file and getattr(photo_file, "name", ""):
                            card_payload["product_photo"] = photo_file.name
                    cards_payload.append(card_payload)

        product_name = str(request.POST.get("product_name") or "").strip()
        if cards_payload:
            product_name = cards_payload[0].get("product_name") or product_name
        if not is_draft:
            if cards_payload:
                if not any(card.get("product_name") for card in cards_payload):
                    return render_home_error("Укажите наименование товара.")
            elif not product_name:
                return render_home_error("Укажите наименование товара.")

        def collect_rows(field_map: dict) -> list[dict]:
            row_count = 0
            for values in field_map.values():
                row_count = max(row_count, len(values))
            rows = []
            for idx in range(row_count):
                row = {}
                for key, values in field_map.items():
                    row[key] = values[idx] if idx < len(values) else ""
                if any(str(value).strip() for value in row.values()):
                    rows.append(row)
            return rows

        size_rows = collect_rows(
            {
                "size_no": request.POST.getlist("size_no[]"),
                "size_value": request.POST.getlist("size_value[]"),
                "barcode": request.POST.getlist("size_barcode[]"),
                "recount_qty": request.POST.getlist("size_recount[]"),
                "processing_qty": request.POST.getlist("size_processing[]"),
                "unboxing_qty": request.POST.getlist("size_unpacking[]"),
                "defect_qty": request.POST.getlist("size_defect[]"),
            }
        )
        stock_rows = collect_rows(
            {
                "article": request.POST.getlist("stock_article[]"),
                "size": request.POST.getlist("stock_size[]"),
                "barcode": request.POST.getlist("stock_barcode[]"),
                "qty": request.POST.getlist("stock_qty[]"),
            }
        )
        global_article = str(request.POST.get("article") or "").strip()
        if cards_payload:
            stock_rows = []
            for card in cards_payload:
                base_article = str(card.get("article") or "").strip()
                goods_type = str(card.get("goods_type") or "").strip()
                for row in card.get("rows") or []:
                    article_value = str(row.get("article") or base_article).strip()
                    stock_rows.append(
                        {
                            "article": article_value,
                            "size": str(row.get("size") or "").strip(),
                            "barcode": str(row.get("barcode") or "").strip(),
                            "qty": row.get("qty"),
                            "goods_type": goods_type,
                        }
                    )
        elif global_article:
            for row in stock_rows:
                if not str(row.get("article") or "").strip():
                    row["article"] = global_article
        unboxing_rows = collect_rows(
            {
                "date": request.POST.getlist("unboxing_date[]"),
                "box_size": request.POST.getlist("unboxing_box_size[]"),
                "multiple": request.POST.getlist("unboxing_multiple[]"),
                "box_qty": request.POST.getlist("unboxing_box_qty[]"),
                "pallet_qty": request.POST.getlist("unboxing_pallet_qty[]"),
                "storage_zone": request.POST.getlist("unboxing_storage_zone[]"),
            }
        )
        if not is_draft and stock_rows:
            available_map = {}
            barcode_map = {}
            for item in processing_views._inventory_items_for_agency(agency, exclude_order_id=edit_order_id or None):
                sku_key = str(item.get("sku") or "").strip().lower()
                size_key = str(item.get("size") or "").strip().lower()
                qty_value = processing_views._parse_qty_value(item.get("qty")) or 0
                if sku_key:
                    key = (sku_key, size_key)
                    available_map[key] = available_map.get(key, 0) + qty_value
                barcode_value = str(item.get("barcode") or "").strip()
                if barcode_value:
                    barcode_map[barcode_value] = barcode_map.get(barcode_value, 0) + qty_value
            requested_map: dict[tuple[str, str], int] = {}
            requested_barcode_map: dict[str, int] = {}
            for row in stock_rows:
                qty_value = processing_views._parse_qty_value(row.get("qty"))
                if qty_value is None or qty_value <= 0:
                    continue
                sku_key = str(row.get("article") or "").strip().lower()
                size_key = str(row.get("size") or "").strip().lower()
                barcode_value = str(row.get("barcode") or "").strip()
                sku_label = row.get("article") or "-"
                size_label = row.get("size") or "-"
                if sku_key:
                    key = (sku_key, size_key)
                    requested_map[key] = requested_map.get(key, 0) + qty_value
                    max_qty = available_map.get(key, 0)
                    if requested_map[key] > max_qty:
                        return render_home_error(
                            f"Количество для {sku_label} ({size_label}) превышает доступный остаток: {max_qty}."
                        )
                    continue
                if barcode_value:
                    requested_barcode_map[barcode_value] = requested_barcode_map.get(barcode_value, 0) + qty_value
                    max_qty = barcode_map.get(barcode_value, 0)
                    if requested_barcode_map[barcode_value] > max_qty:
                        return render_home_error(
                            f"Количество для {sku_label} ({size_label}) превышает доступный остаток: {max_qty}."
                        )
                    continue
                return render_home_error(
                    f"Не удалось проверить остаток для {sku_label} ({size_label}): нет артикула или штрихкода."
                )

        primary_article = str(request.POST.get("article") or "").strip()
        primary_photo_url = str(request.POST.get("product_photo_url") or "").strip()
        if cards_payload:
            primary_article = cards_payload[0].get("article") or primary_article
            primary_photo_url = cards_payload[0].get("photo_url") or primary_photo_url

        payload = {
            "email": request.POST.get("email"),
            "fio": request.POST.get("fio"),
            "org": request.POST.get("org"),
            "product_name": product_name,
            "product_photo_url": primary_photo_url,
            "marketplace": request.POST.get("marketplace"),
            "supplier": request.POST.get("supplier"),
            "brand": request.POST.get("brand"),
            "subject": request.POST.get("subject"),
            "article": primary_article,
            "wb_article": request.POST.get("wb_article"),
            "color": request.POST.get("color"),
            "composition": request.POST.get("composition"),
            "gender": request.POST.get("gender"),
            "season": request.POST.get("season"),
            "order_no": request.POST.get("order_no"),
            "purchase_1c_no": request.POST.get("purchase_1c_no"),
            "purchase_1c_date": request.POST.get("purchase_1c_date"),
            "project_manager": request.POST.get("project_manager"),
            "warehouse_receiving": request.POST.get("warehouse_receiving"),
            "warehouse_packing": request.POST.get("warehouse_packing"),
            "warehouse_unpacking": request.POST.get("warehouse_unpacking"),
            "size_rows": size_rows,
            "stock_rows": stock_rows,
            "measure_needed": request.POST.get("measure_needed"),
            "measure_weight": request.POST.get("measure_weight"),
            "measure_width": request.POST.get("measure_width"),
            "measure_height": request.POST.get("measure_height"),
            "measure_depth": request.POST.get("measure_depth"),
            "defect_check": request.POST.get("defect_check"),
            "defect_percent": request.POST.get("defect_percent"),
            "defect_qty": request.POST.get("defect_qty"),
            "trim_threads_qty": request.POST.get("trim_threads_qty"),
            "tape_qty": request.POST.get("tape_qty"),
            "remove_tag": request.POST.get("remove_tag"),
            "remove_tag_qty": request.POST.get("remove_tag_qty"),
            "attach_tag": request.POST.get("attach_tag"),
            "attach_tag_qty": request.POST.get("attach_tag_qty"),
            "tag_replace_needed": request.POST.get("tag_replace_needed"),
            "tag_owner": request.POST.get("tag_owner"),
            "marking_stickers": request.POST.getlist("marking_stickers[]"),
            "marking_sizes": request.POST.getlist("marking_sizes[]"),
            "marking_info": request.POST.get("marking_info"),
            "marking_5840_needed": request.POST.get("marking_5840_needed"),
            "marking_5840_qty": request.POST.get("marking_5840_qty"),
            "marking_5840_each_needed": request.POST.get("marking_5840_each_needed"),
            "marking_5840_each_qty": request.POST.get("marking_5840_each_qty"),
            "set_build": request.POST.get("set_build"),
            "set_qty": request.POST.get("set_qty"),
            "insert_needed": request.POST.get("insert_needed"),
            "insert_types": request.POST.getlist("insert_types[]"),
            "insert_other": request.POST.get("insert_other"),
            "insert_qty": request.POST.get("insert_qty"),
            "pull_from_bag": request.POST.get("pull_from_bag"),
            "bubble_wrap_needed": request.POST.get("bubble_wrap_needed"),
            "bubble_wrap_type": request.POST.get("bubble_wrap_type"),
            "bubble_wrap_size": request.POST.get("bubble_wrap_size"),
            "bubble_wrap_qty": request.POST.get("bubble_wrap_qty"),
            "bubble_wrap_supply": request.POST.get("bubble_wrap_supply"),
            "bag_replace_needed": request.POST.get("bag_replace_needed"),
            "bag_replace_type": request.POST.get("bag_replace_type"),
            "bag_replace_size": request.POST.get("bag_replace_size"),
            "bag_replace_qty": request.POST.get("bag_replace_qty"),
            "bag_replace_supply": request.POST.get("bag_replace_supply"),
            "box_replace_needed": request.POST.get("box_replace_needed"),
            "box_replace_type": request.POST.get("box_replace_type"),
            "box_replace_size": request.POST.get("box_replace_size"),
            "box_replace_qty": request.POST.get("box_replace_qty"),
            "box_replace_supply": request.POST.get("box_replace_supply"),
            "shrink_wrap_needed": request.POST.get("shrink_wrap_needed"),
            "shrink_wrap_type": request.POST.get("shrink_wrap_type"),
            "shrink_wrap_size": request.POST.get("shrink_wrap_size"),
            "shrink_wrap_qty": request.POST.get("shrink_wrap_qty"),
            "shrink_wrap_supply": request.POST.get("shrink_wrap_supply"),
            "unboxing_rows": unboxing_rows,
            "wholesale_places_qty": request.POST.get("wholesale_places_qty"),
            "invoice_no": request.POST.get("invoice_no"),
            "invoice_date": request.POST.get("invoice_date"),
            "payment_date": request.POST.get("payment_date"),
            "accountant": request.POST.get("accountant"),
            "archive_date": request.POST.get("archive_date"),
            "executor_name": request.POST.get("executor_name"),
            "start_date": request.POST.get("start_date"),
            "end_date": request.POST.get("end_date"),
            "receive_date": request.POST.get("receive_date"),
            "responsible_name": request.POST.get("responsible_name"),
            "direction_needed": request.POST.get("direction_needed"),
            "direction_count": request.POST.get("direction_count"),
            "direction_addresses_json": request.POST.get("direction_addresses_json"),
            "direction_plan_json": request.POST.get("direction_plan_json"),
            "box_forming": request.POST.get("box_forming"),
            "box_forming_other": request.POST.get("box_forming_other"),
            "comments": request.POST.get("comments"),
        }
        if cards_payload:
            payload["cards"] = cards_payload
        photo_file = request.FILES.get("product_photo")
        if photo_file and getattr(photo_file, "name", ""):
            payload["product_photo"] = photo_file.name
        direction_file = request.FILES.get("direction_file")
        if direction_file and getattr(direction_file, "name", ""):
            payload["direction_file"] = direction_file.name
        if is_draft:
            status_value = "draft"
            status_label = "Черновик"
            payload["submit_action"] = "draft"
        else:
            status_value = "sent_unconfirmed"
            status_label = "Ждет подтверждения"
            payload["submit_action"] = "submitted"
        if edit_order_id and preserved_status:
            status_value = preserved_status
            if preserved_label:
                status_label = preserved_label
            if preserved_submit_action:
                payload["submit_action"] = preserved_submit_action
        payload["status"] = status_value
        payload["status_label"] = status_label
        if edit_order_id:
            order_id = edit_order_id
            action = "update"
            description = f"Исправление заявки на обработку №{order_id}"
        elif is_draft:
            if not draft_order_id:
                draft_order_id = f"draft-{processing_views.uuid.uuid4().hex[:12]}"
            order_id = draft_order_id
            action = "update" if existing_draft_entries else "create"
            description = "Черновик заявки на обработку"
        else:
            order_id = processing_views._next_order_number(order_type="processing")
            action = "create"
            description = f"Заявка на обработку №{order_id}"
        marking_each = processing_views._non_empty_text(payload.get("marking_5840_each_qty"))
        cz_file = request.FILES.get("marking_cz_file")
        if cz_file and getattr(cz_file, "name", ""):
            payload["marking_cz_file"] = cz_file.name
        if marking_each:
            required_map, required_total, missing_barcodes = processing_views._marking_required_by_barcode(payload)
            if not is_draft and required_total <= 0:
                return render_home_error("Добавьте товары для проверки ЧЗ.")
            if not is_draft and missing_barcodes > 0:
                message = "Для маркировки ЧЗ заполните штрихкоды товара."
                return render_home_error(message)
            if cz_file and getattr(cz_file, "name", ""):
                ok, import_result = processing_views._import_marking_codes(cz_file, payload, order_id, agency, request.user)
                if not ok:
                    message = import_result.get("error") or "Ошибка импорта ЧЗ."
                    return render_home_error(message)
                payload["marking_cz_import"] = import_result
            if not is_draft:
                available_map = processing_views._marking_available_by_barcode(agency, order_id)
                missing_total = 0
                for barcode, required_qty in required_map.items():
                    available_qty = available_map.get(barcode, 0)
                    missing_total += max(required_qty - available_qty, 0)
                if missing_total > 0:
                    message = f"Не хватает ЧЗ: {missing_total}. Загрузите файл с ЧЗ."
                    return render_home_error(message)
                ok, reserve_error = processing_views._reserve_marking_codes(agency, order_id, required_map)
                if not ok:
                    message = reserve_error or "Не удалось забронировать ЧЗ."
                    return render_home_error(message)
        try:
            with transaction.atomic():
                log_order_action(
                    action,
                    order_id=order_id,
                    order_type="processing",
                    user=request.user if request.user.is_authenticated else None,
                    agency=agency,
                    description=description,
                    payload=payload,
                )
                if not is_draft:
                    processing_views._replace_processing_reserves(order_id, agency, stock_rows)
                if not is_draft and draft_order_id and not edit_order_id:
                    OrderAuditEntry.objects.filter(
                        order_id=draft_order_id,
                        order_type="processing",
                        agency=agency,
                    ).delete()
                if not edit_order_id and not is_draft and client_agency:
                    processing_views._create_processing_manager_task(order_id, agency, request, timezone.localtime())
        except ValueError as exc:
            if not is_draft and not edit_order_id:
                MarkingCode.objects.filter(
                    agency=agency,
                    order_type="processing",
                    order_id=order_id,
                    used_at__isnull=True,
                ).update(order_id="")
            message = str(exc).strip()
            if message.startswith("No stored snapshots with enough available qty for "):
                sku_code = message.rsplit(" for ", 1)[-1].strip()
                message = f"Не удалось забронировать товар на складе: для {sku_code} не хватает доступного остатка."
            elif message.startswith("No stored snapshot with enough available qty for "):
                sku_code = message.rsplit(" for ", 1)[-1].strip()
                message = f"Не удалось забронировать товар на складе: для {sku_code} не хватает доступного остатка."
            elif not message:
                message = "Не удалось синхронизировать заявку со складом."
            return render_home_error(message)
        if edit_order_id:
            if autosave:
                return JsonResponse(
                    {
                        "ok": True,
                        "order_id": order_id,
                        "draft_order_id": "",
                        "status": status_value,
                        "status_label": status_label,
                    }
                )
            return redirect(f"/orders/processing/{order_id}/")
        if autosave:
            return JsonResponse(
                {
                    "ok": True,
                    "order_id": order_id,
                    "draft_order_id": order_id if is_draft else "",
                    "status": status_value,
                    "status_label": status_label,
                }
            )
        return redirect(f"/orders/processing/?client={agency.id}&ok=1&status={status_value}&order={order_id}")
