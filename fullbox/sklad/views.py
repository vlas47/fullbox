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

def _parse_qty_value(raw: object | None) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _parse_int_value(raw) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


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
    order_ids = None
    client_labels = {}
    if client_agency:
        order_ids = list(
            OrderAuditEntry.objects.filter(order_type="receiving", agency=client_agency)
            .values_list("order_id", flat=True)
            .distinct()
        )
        if order_ids:
            for entry in (
                OrderAuditEntry.objects.filter(order_type="receiving", order_id__in=order_ids)
                .select_related("agency")
                .order_by("-created_at")
            ):
                if entry.order_id in client_labels or not entry.agency:
                    continue
                base = entry.agency.agn_name or entry.agency.fio_agn or str(entry.agency)
                client_labels[entry.order_id] = _shorten_ip_name(base)

    entries = OrderAuditEntry.objects.filter(order_type="receiving")
    if order_ids:
        entries = entries.filter(order_id__in=order_ids)
    entries = entries.select_related("agency").order_by("-created_at")
    latest_by_order = {}
    blocked_orders = set()
    for entry in entries:
        if entry.order_id in latest_by_order or entry.order_id in blocked_orders:
            continue
        payload = entry.payload or {}
        if payload.get("act") != "placement":
            continue
        state = (payload.get("act_state") or "closed").lower()
        if state != "closed":
            blocked_orders.add(entry.order_id)
            continue
        latest_by_order[entry.order_id] = entry

    rows = []
    default_location = "PR"

    def normalize_location(pallet):
        if not pallet:
            return default_location
        location_value = (pallet or {}).get("location")
        def normalize_zone(value: str) -> str:
            text = (value or "").strip()
            if not text:
                return ""
            if re.search(r"^pr$", text, re.IGNORECASE) or re.search(
                r"зона приемки|поле приемки", text, re.IGNORECASE
            ):
                return "PR"
            if re.search(r"^otg?$", text, re.IGNORECASE) or re.search(
                r"зона отгрузки|отгрузк", text, re.IGNORECASE
            ):
                return "OTG"
            if re.search(r"^mr$", text, re.IGNORECASE) or re.search(
                r"между ряд", text, re.IGNORECASE
            ):
                return "MR"
            if re.search(r"^os$", text, re.IGNORECASE) or re.search(
                r"основн|стеллаж|ряд|полк|секци|ярус|ячейк", text, re.IGNORECASE
            ):
                return "OS"
            return text.upper()

        def format_label(zone, row=0, section=0, tier=0, cell=0):
            if zone == "PR":
                return "PR · Зона приемки"
            if zone == "OTG":
                return "OTG · Зона отгрузки"
            if zone == "MR":
                return f"MR · Между рядами · Ряд {row}" if row else "MR · Между рядами"
            if zone == "OS":
                if row and section and tier and cell:
                    return f"OS · Ряд {row} · Секция {section} · Ярус {tier} · Ячейка {cell}"
                if row:
                    return f"OS · Ряд {row}"
                return "OS · Основной склад"
            return zone or default_location

        zone = ""
        row = section = tier = cell = 0
        if isinstance(location_value, str):
            zone = normalize_zone(location_value)
        elif isinstance(location_value, dict):
            zone = normalize_zone(location_value.get("zone") or "")
            row = _parse_int_value(location_value.get("row") or pallet.get("row"))
            section = _parse_int_value(location_value.get("section"))
            tier = _parse_int_value(location_value.get("tier"))
            cell = _parse_int_value(location_value.get("cell"))
            if not zone:
                rack = (location_value.get("rack") or pallet.get("rack") or "").strip()
                row_text = (location_value.get("row") or pallet.get("row") or "").strip()
                section_text = (location_value.get("section") or "").strip()
                tier_text = (location_value.get("tier") or "").strip()
                shelf = (location_value.get("shelf") or pallet.get("shelf") or "").strip()
                cell_text = (location_value.get("cell") or "").strip()
                if rack or row_text or section_text or tier_text or shelf or cell_text:
                    zone = "OS"
        if not zone:
            zone = normalize_zone(pallet.get("zone") or "")
        if not zone:
            rack = (pallet.get("rack") or "").strip()
            row_text = (pallet.get("row") or "").strip()
            shelf = (pallet.get("shelf") or "").strip()
            if rack or row_text or shelf:
                zone = "OS"
        zone = zone or default_location
        return format_label(zone, row=row, section=section, tier=tier, cell=cell)

    for entry in latest_by_order.values():
        payload = entry.payload or {}
        boxes = payload.get("act_boxes") or []
        pallets = payload.get("act_pallets") or []
        client_name = client_labels.get(entry.order_id, "-")
        if entry.agency and client_name == "-":
            base = entry.agency.agn_name or entry.agency.fio_agn or str(entry.agency)
            client_name = _shorten_ip_name(base)
        box_to_pallet = {}
        pallet_locations = {}
        for pallet in pallets:
            pallet_code = (pallet or {}).get("code") or ""
            if pallet_code:
                pallet_locations[pallet_code] = normalize_location(pallet)
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
            location = (
                pallet_locations.get(pallet_code)
                if pallet_code and pallet_code != "-"
                else default_location
            )
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
                    "location": location or default_location,
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

    if not staff_view:
        grouped = {}
        for row in rows:
            key = (row.get("order_id"), row.get("sku"), row.get("name"), row.get("size"))
            qty_value = _parse_qty_value(row.get("qty")) or 0
            existing = grouped.get(key)
            if existing:
                existing["qty"] += qty_value
                if row.get("created_at") and row["created_at"] > existing.get("created_at"):
                    existing["created_at"] = row["created_at"]
            else:
                item = dict(row)
                item["qty"] = qty_value
                grouped[key] = item
        rows = list(grouped.values())
    rows.sort(key=lambda item: item["created_at"], reverse=True)
    role = get_request_role(request)
    if not staff_view:
        template_name = "client_cabinet/inventory_journal.html"
    elif role == "manager":
        template_name = "teammanager/inventory_journal.html"
    else:
        template_name = "sklad/inventory_journal.html"
    return render(
        request,
        template_name,
        {
            "rows": rows,
            "client_agency": client_agency,
            "staff_view": staff_view,
        },
    )
