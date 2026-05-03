import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.views.decorators.http import require_GET, require_POST

from audit.models import OrderAuditEntry
from employees.access import get_request_role
from sku.models import SKU
from .services import (
    processing_marking_import_response,
    processing_marking_print_response,
    processing_marking_reset_printed_response,
    processing_marking_scan_response,
    processing_marking_summary_response,
    receiving_marking_scan_response,
)

ALLOWED_PROCESSING_ROLES = {"storekeeper", "processing_head", "head_manager", "director", "admin"}
ALLOWED_RECEIVING_ROLES = {"storekeeper", "manager", "head_manager", "director", "admin"}


def _parse_json_body(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _normalize_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    return text


def _get_processing_order(order_id: str):
    if not order_id:
        return None, None, None
    latest = (
        OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
        .select_related("agency")
        .order_by("-created_at")
        .first()
    )
    if not latest:
        return None, None, None
    return latest, latest.payload or {}, latest.agency


def _get_receiving_order(order_id: str):
    if not order_id:
        return None, None, None
    latest = (
        OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
        .select_related("agency")
        .order_by("-created_at")
        .first()
    )
    if not latest:
        return None, None, None
    return latest, latest.payload or {}, latest.agency


def _resolve_sku(agency, sku_code: str):
    if not sku_code:
        return None
    qs = SKU.objects.filter(sku_code=sku_code)
    if agency:
        sku = qs.filter(agency=agency).first()
        if sku:
            return sku
        return qs.filter(agency__isnull=True).first()
    return qs.first()


def _require_processing_role(request):
    role = get_request_role(request)
    if role not in ALLOWED_PROCESSING_ROLES:
        return False, HttpResponseForbidden("Доступ запрещен")
    return True, None


def _require_receiving_role(request):
    role = get_request_role(request)
    if role not in ALLOWED_RECEIVING_ROLES:
        return False, HttpResponseForbidden("Доступ запрещен")
    return True, None


def _extract_receiving_items(payload: dict) -> list[dict]:
    payload = payload or {}
    result = {}
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        sku_code = str(item.get("sku_code") or item.get("sku") or "").strip()
        if not sku_code:
            continue
        size = str(item.get("size") or "").strip()
        barcode = str(item.get("barcode") or "").strip()
        key = (sku_code, size)
        if key not in result:
            result[key] = {
                "sku_code": sku_code,
                "size": size,
                "barcode": barcode,
                "qty": 0,
            }
        qty_raw = item.get("qty") or item.get("actual_qty")
        qty = _normalize_cell(qty_raw)
        try:
            qty_value = int(qty) if qty != "" else 0
        except ValueError:
            qty_value = 0
        result[key]["qty"] += qty_value
        if not result[key]["barcode"] and barcode:
            result[key]["barcode"] = barcode
    return list(result.values())


@login_required
@require_GET
def processing_marking_summary(request, order_id: str):
    return processing_marking_summary_response(request=request, order_id=order_id)


@login_required
@require_POST
def processing_marking_scan(request, order_id: str):
    return processing_marking_scan_response(request=request, order_id=order_id)


@login_required
@require_POST
def receiving_marking_scan(request, order_id: str):
    return receiving_marking_scan_response(request=request, order_id=order_id)


@login_required
@require_POST
def processing_marking_print(request, order_id: str):
    return processing_marking_print_response(request=request, order_id=order_id)


@login_required
@require_POST
def processing_marking_reset_printed(request, order_id: str):
    return processing_marking_reset_printed_response(request=request, order_id=order_id)


@login_required
@require_POST
def processing_marking_import(request, order_id: str):
    return processing_marking_import_response(request=request, order_id=order_id)
