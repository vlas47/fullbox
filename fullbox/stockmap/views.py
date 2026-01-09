import re

from django.http import Http404
from django.views.generic import TemplateView

from audit.models import OrderAuditEntry
from employees.access import RoleRequiredMixin, get_request_role, resolve_cabinet_url

_OS_ROW_SECTIONS = {
    1: 10,
    2: 10,
    3: 10,
    4: 10,
    5: 10,
    6: 10,
    7: 6,
    8: 6,
    9: 5,
}
_OS_TIERS = 4
_OS_CELLS_PER_TIER = 3


class StockMapView(RoleRequiredMixin, TemplateView):
    template_name = "stockmap/stockmap.html"
    allowed_roles = ("storekeeper", "head_manager", "director", "admin")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cells = [
            {"zone": "PR", "row": "", "section": "", "tier": "", "cell": 150},
            {"zone": "OTG", "row": "", "section": "", "tier": "", "cell": 150},
            {"zone": "MR", "row": 1, "section": "", "tier": "", "cell": 50},
            {"zone": "MR", "row": 2, "section": "", "tier": "", "cell": 50},
            {"zone": "MR", "row": 3, "section": "", "tier": "", "cell": 50},
            {"zone": "MR", "row": 4, "section": "", "tier": "", "cell": 50},
        ]
        for row_num, sections in _OS_ROW_SECTIONS.items():
            cells.append(
                {
                    "zone": "OS",
                    "row": row_num,
                    "section": sections,
                    "tier": _OS_TIERS,
                    "cell": _OS_CELLS_PER_TIER,
                }
            )
        occupied_os = set()
        occupied_mr = {}
        occupied_pr = 0
        occupied_otg = 0
        for entry in _latest_closed_placement_entries():
            payload = entry.payload or {}
            for pallet in payload.get("act_pallets") or []:
                zone, row_num, section_num, tier_num, cell_num = _location_parts(pallet)
                if zone == "OS" and row_num and section_num and tier_num and cell_num:
                    occupied_os.add((row_num, section_num, tier_num, cell_num))
                elif zone == "PR":
                    occupied_pr += 1
                elif zone == "OTG":
                    occupied_otg += 1
                elif zone == "MR" and row_num:
                    occupied_mr[row_num] = occupied_mr.get(row_num, 0) + 1

        os_row_counts = {}
        for row_num, section_num, tier_num, cell_num in occupied_os:
            os_row_counts[row_num] = os_row_counts.get(row_num, 0) + 1

        for row in cells:
            section = _int_value(row.get("section"))
            tier = _int_value(row.get("tier"))
            cell = _int_value(row.get("cell"))
            total = section * tier * cell if section and tier and cell else cell
            if row.get("zone") == "OS" and row.get("row"):
                occupied = os_row_counts.get(_int_value(row.get("row")), 0)
                row["occupied"] = occupied
                row["free"] = max(0, total - occupied)
            elif row.get("zone") == "PR":
                row["occupied"] = occupied_pr
                row["free"] = max(0, total - occupied_pr)
            elif row.get("zone") == "OTG":
                row["occupied"] = occupied_otg
                row["free"] = max(0, total - occupied_otg)
            elif row.get("zone") == "MR" and row.get("row"):
                occupied = occupied_mr.get(_int_value(row.get("row")), 0)
                row["occupied"] = occupied
                row["free"] = max(0, total - occupied)
            else:
                row["free"] = total
                row["occupied"] = 0
        context["cells"] = cells
        role = get_request_role(self.request)
        context["cabinet_url"] = resolve_cabinet_url(role)
        return context


class StockMapRowView(RoleRequiredMixin, TemplateView):
    template_name = "stockmap/stockmap_row.html"
    allowed_roles = ("storekeeper", "head_manager", "director", "admin")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        row_number = _int_value(kwargs.get("row"))
        section_count = _OS_ROW_SECTIONS.get(row_number)
        if not section_count:
            raise Http404("Ряд не найден")

        occupied_cells = set()
        for entry in _latest_closed_placement_entries():
            payload = entry.payload or {}
            for pallet in payload.get("act_pallets") or []:
                zone, row_num, section_num, tier_num, cell_num = _location_parts(pallet)
                if zone != "OS" or row_num != row_number:
                    continue
                if section_num and tier_num and cell_num:
                    occupied_cells.add((section_num, tier_num, cell_num))
        sections = []
        for section_number in range(1, section_count + 1):
            tiers = []
            for tier_number in range(1, _OS_TIERS + 1):
                cells = []
                for cell_number in range(1, _OS_CELLS_PER_TIER + 1):
                    key = (section_number, tier_number, cell_number)
                    cells.append(
                        {
                            "number": cell_number,
                            "occupied": key in occupied_cells,
                        }
                    )
                tiers.append(
                    {
                        "number": tier_number,
                        "cells": cells,
                    }
                )
            sections.append(
                {
                    "number": section_number,
                    "tiers": tiers,
                }
            )
        role = get_request_role(self.request)
        context["cabinet_url"] = resolve_cabinet_url(role)
        context["row_number"] = row_number
        context["sections"] = sections
        context["cells_per_tier"] = _OS_CELLS_PER_TIER
        context["tiers_total"] = _OS_TIERS
        return context


def _int_value(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_zone(value: str) -> str:
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


def _location_parts(pallet):
    pallet = pallet or {}
    location_value = pallet.get("location")
    zone = ""
    row = section = tier = cell = 0
    if isinstance(location_value, dict):
        zone = _normalize_zone(location_value.get("zone") or "")
        row = _int_value(location_value.get("row") or pallet.get("row"))
        section = _int_value(location_value.get("section"))
        tier = _int_value(location_value.get("tier"))
        cell = _int_value(location_value.get("cell"))
        if not zone:
            rack = (location_value.get("rack") or pallet.get("rack") or "").strip()
            row_text = (location_value.get("row") or pallet.get("row") or "").strip()
            section_text = (location_value.get("section") or "").strip()
            tier_text = (location_value.get("tier") or "").strip()
            shelf = (location_value.get("shelf") or pallet.get("shelf") or "").strip()
            cell_text = (location_value.get("cell") or "").strip()
            if rack or row_text or section_text or tier_text or shelf or cell_text:
                zone = "OS"
    elif isinstance(location_value, str):
        zone = _normalize_zone(location_value)
    if not zone:
        zone = _normalize_zone(pallet.get("zone") or "")
    if zone == "OS":
        row = row or _int_value(pallet.get("row"))
        section = section or _int_value(pallet.get("section"))
        tier = tier or _int_value(pallet.get("tier"))
        cell = cell or _int_value(pallet.get("cell"))
    if zone == "MR":
        row = row or _int_value(pallet.get("row"))
    return zone, row, section, tier, cell


def _latest_closed_placement_entries():
    entries = OrderAuditEntry.objects.filter(order_type="receiving").order_by("-created_at")
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
    return latest_by_order.values()
