from django.http import Http404
from django.views.generic import TemplateView

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
        for row in cells:
            section = _int_value(row.get("section"))
            tier = _int_value(row.get("tier"))
            cell = _int_value(row.get("cell"))
            total = section * tier * cell if section and tier and cell else cell
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
