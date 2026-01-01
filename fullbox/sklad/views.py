import re

from django.http import HttpResponseForbidden
from django.shortcuts import render

from audit.models import OrderAuditEntry
from employees.access import get_request_role, is_staff_role, role_required
from sku.models import Agency


_IP_PREFIX_RE = re.compile(r"\bиндивидуальный предприниматель\b", re.IGNORECASE)


def _shorten_ip_name(name: str) -> str:
    if not name:
        return "-"
    normalized = _IP_PREFIX_RE.sub("ИП", name)
    return " ".join(normalized.split())


def _client_agency_for_request(request):
    if not request.user.is_authenticated:
        return None
    return Agency.objects.filter(portal_user=request.user).first()


@role_required("storekeeper")
def dashboard(request):
    return render(request, "sklad/dashboard.html")


def inventory_journal(request):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    role = get_request_role(request)
    staff_view = request.user.is_staff or is_staff_role(role)
    client_agency = None
    if staff_view:
        client_id = request.GET.get("client") or request.GET.get("agency")
        if client_id:
            client_agency = Agency.objects.filter(pk=client_id).first()
    else:
        client_agency = _client_agency_for_request(request)
        if not client_agency:
            return HttpResponseForbidden("Доступ запрещен")
    entries = (
        OrderAuditEntry.objects.filter(order_type="receiving")
        .select_related("agency")
        .order_by("-created_at")
    )
    latest_by_order = {}
    for entry in entries:
        payload = entry.payload or {}
        if payload.get("act") != "placement":
            continue
        if client_agency and entry.agency_id != client_agency.id:
            continue
        if entry.order_id in latest_by_order:
            continue
        latest_by_order[entry.order_id] = entry

    rows = []
    for entry in latest_by_order.values():
        payload = entry.payload or {}
        boxes = payload.get("act_boxes") or []
        pallets = payload.get("act_pallets") or []
        client_name = "-"
        if entry.agency:
            base = entry.agency.agn_name or entry.agency.fio_agn or str(entry.agency)
            client_name = _shorten_ip_name(base)
        box_to_pallet = {}
        for pallet in pallets:
            pallet_code = (pallet or {}).get("code") or ""
            for box_code in (pallet or {}).get("boxes") or []:
                if box_code and pallet_code and box_code not in box_to_pallet:
                    box_to_pallet[box_code] = pallet_code

        def append_row(item, box_code="-", pallet_code="-"):
            sku = (item.get("sku") or item.get("sku_code") or "").strip()
            name = (item.get("name") or "-").strip()
            size = (item.get("size") or "-").strip()
            qty = item.get("qty")
            if qty in (None, ""):
                qty = item.get("actual_qty") or 0
            rows.append(
                {
                    "created_at": entry.created_at,
                    "order_id": entry.order_id,
                    "client_label": client_name,
                    "sku": sku or "-",
                    "name": name or "-",
                    "size": size or "-",
                    "qty": qty,
                    "box_code": box_code or "-",
                    "pallet_code": pallet_code or "-",
                    "rack": "-",
                    "row": "-",
                    "shelf": "-",
                }
            )

        for box in boxes:
            box_code = (box or {}).get("code") or "-"
            pallet_code = box_to_pallet.get(box_code, "-")
            for item in (box or {}).get("items") or []:
                append_row(item, box_code=box_code, pallet_code=pallet_code)

        for pallet in pallets:
            pallet_code = (pallet or {}).get("code") or "-"
            for item in (pallet or {}).get("items") or []:
                append_row(item, box_code="-", pallet_code=pallet_code)

        if not boxes and not pallets:
            for item in payload.get("act_items") or []:
                append_row(item, box_code="-", pallet_code="-")

    rows.sort(key=lambda item: item["created_at"], reverse=True)
    return render(
        request,
        "sklad/inventory_journal.html",
        {
            "rows": rows,
            "client_agency": client_agency,
            "staff_view": staff_view,
        },
    )
