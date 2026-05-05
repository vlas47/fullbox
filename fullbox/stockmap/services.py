from __future__ import annotations

import json

from django.http import Http404
from django.shortcuts import redirect

from employees.access import get_request_role, resolve_cabinet_url
from employees.models import Employee
from labels.utils import shorten_client_label
from reachtruck.models import MoveTask
from reachtruck.services.putaway_planner import normalize_putaway_location, parse_putaway_destinations


_OS_LINE_TONES = (
    {
        "group_from": "#3e434e",
        "group_to": "#252932",
        "cell_from": "#cb45ff",
        "cell_to": "#8f0ce3",
        "free_bg": "#fff5d4",
        "free_ink": "#725f20",
        "unavailable_bg": "#f2efe8",
    },
    {
        "group_from": "#424651",
        "group_to": "#292d36",
        "cell_from": "#c43eff",
        "cell_to": "#890add",
        "free_bg": "#fff0cc",
        "free_ink": "#765820",
        "unavailable_bg": "#f0ede6",
    },
    {
        "group_from": "#464853",
        "group_to": "#2d3038",
        "cell_from": "#bf3dff",
        "cell_to": "#8509d6",
        "free_bg": "#fff7dd",
        "free_ink": "#6f5d23",
        "unavailable_bg": "#f3efe9",
    },
    {
        "group_from": "#3d444f",
        "group_to": "#252b33",
        "cell_from": "#c642ff",
        "cell_to": "#8c0de0",
        "free_bg": "#ffefc3",
        "free_ink": "#77581d",
        "unavailable_bg": "#f1ede5",
    },
    {
        "group_from": "#414751",
        "group_to": "#272d35",
        "cell_from": "#c948ff",
        "cell_to": "#8f11e0",
        "free_bg": "#fff4d0",
        "free_ink": "#715d22",
        "unavailable_bg": "#f2eee7",
    },
    {
        "group_from": "#444953",
        "group_to": "#2a2f37",
        "cell_from": "#be39ff",
        "cell_to": "#8208d0",
        "free_bg": "#ffeecc",
        "free_ink": "#76571d",
        "unavailable_bg": "#efebe4",
    },
    {
        "group_from": "#3f4550",
        "group_to": "#262c35",
        "cell_from": "#c847ff",
        "cell_to": "#8b0edb",
        "free_bg": "#fff6d9",
        "free_ink": "#705d22",
        "unavailable_bg": "#f2eee8",
    },
    {
        "group_from": "#434853",
        "group_to": "#2a2e37",
        "cell_from": "#c13fff",
        "cell_to": "#8709d7",
        "free_bg": "#fff1c8",
        "free_ink": "#77581e",
        "unavailable_bg": "#f0ece5",
    },
    {
        "group_from": "#3d434e",
        "group_to": "#242932",
        "cell_from": "#ca49ff",
        "cell_to": "#8f10e1",
        "free_bg": "#fff7e2",
        "free_ink": "#6e5d24",
        "unavailable_bg": "#f3f0ea",
    },
)

_OS_SECTION_TONES = (
    {"free_bg": "#fff4c8", "unavailable_bg": "#efe7d8", "axis_bg": "#f7e8c2"},
    {"free_bg": "#f9eb9f", "unavailable_bg": "#e6dcc0", "axis_bg": "#efdaa0"},
)

_OS_LINE_DISPLAY_LABELS = {
    1: "0",
    2: "A",
    3: "B",
    4: "C",
    5: "D",
    6: "E",
    7: "F",
    8: "G",
    9: "I",
}


def _views():
    from . import views as stockmap_views

    return stockmap_views


def _create_batch_move_tasks(*args, **kwargs):
    from reachtruck.services import create_batch_move_tasks

    return create_batch_move_tasks(*args, **kwargs)


def _os_line_tone(index: int) -> dict[str, str]:
    return dict(_OS_LINE_TONES[index % len(_OS_LINE_TONES)])


def _os_line_style(tone: dict[str, str]) -> str:
    return "; ".join(
        [
            f"--line-group-from: {tone['group_from']}",
            f"--line-group-to: {tone['group_to']}",
            f"--line-cell-from: {tone['cell_from']}",
            f"--line-cell-to: {tone['cell_to']}",
            f"--line-free-bg: {tone['free_bg']}",
            f"--line-free-ink: {tone['free_ink']}",
            f"--line-unavailable-bg: {tone['unavailable_bg']}",
            "line-height: 1",
        ]
    )


def _os_section_style(section_number: int) -> str:
    tone = _OS_SECTION_TONES[(max(section_number, 1) - 1) % len(_OS_SECTION_TONES)]
    return "; ".join(
        [
            f"--section-free-bg: {tone['free_bg']}",
            f"--section-unavailable-bg: {tone['unavailable_bg']}",
            f"--section-axis-bg: {tone['axis_bg']}",
        ]
    )


def _os_line_display_label(section_number: int) -> str:
    return _OS_LINE_DISPLAY_LABELS.get(section_number, str(section_number))


def _support_zone_client_label(*, agency, fallback_name: str, agency_id) -> str:
    short_name = _views()._clean_text(getattr(agency, "short_name", ""))
    full_name = _views()._clean_text(getattr(agency, "agn_name", ""))
    base_name = short_name or full_name or fallback_name or f"Клиент {agency_id}"
    return shorten_client_label(base_name)


def _support_zone_order_label(*, order_type: str, order_id: str) -> str:
    return _views()._process_label(_views()._clean_text(order_type), _views()._clean_text(order_id))


def _support_zone_order_code(*, zone: str, row_num: int, order_id: str, order_type: str) -> str:
    order_text = _views()._clean_text(order_id)
    zone_code = f"MR{row_num}" if zone == "MR" and row_num else zone
    if order_text and zone_code:
        return f"{order_text}_{zone_code}"
    return _support_zone_order_label(order_type=order_type, order_id=order_id)


def _support_zone_detail_map() -> dict[str, dict]:
    grouped: dict[str, dict] = {}
    rows = _views()._active_stockmap_rows()
    for stock_row in rows:
        zone = _views()._normalize_zone(stock_row.get("zone") or "")
        row_num = _views()._int_value(stock_row.get("row"))
        if zone not in {"PR", "OTG", "OBR", "MR"}:
            continue
        if zone == "MR" and not row_num:
            continue
        pallet_code = _views()._clean_text(stock_row.get("pallet_code"))
        if not pallet_code:
            continue
        detail_key = f"MR:{row_num}" if zone == "MR" else zone
        if zone == "MR":
            title = f"MR · Ряд {row_num}"
        else:
            title = {
                "PR": "PR · Приемка",
                "OTG": "OTG · Отгрузка",
                "OBR": "OBR · Обработка",
            }.get(zone, zone)
        bucket = grouped.setdefault(
            detail_key,
            {
                "title": title,
                "items": {},
            },
        )
        fallback_name = _views()._clean_text(getattr(stock_row.get("agency"), "agn_name", "")) or f"Клиент {stock_row.get('agency_id')}"
        item = bucket["items"].setdefault(
            pallet_code,
            {
                "pallet_code": pallet_code,
                "client_name": _support_zone_client_label(
                    agency=stock_row.get("agency"),
                    fallback_name=fallback_name,
                    agency_id=stock_row.get("agency_id"),
                ),
                "order_label": _support_zone_order_label(
                    order_type=_views()._clean_text(stock_row.get("order_type")),
                    order_id=_views()._clean_text(stock_row.get("order_id")),
                ),
                "order_code": _support_zone_order_code(
                    zone=zone,
                    row_num=row_num,
                    order_id=_views()._clean_text(stock_row.get("order_id")),
                    order_type=_views()._clean_text(stock_row.get("order_type")),
                ),
            },
        )
        if not item.get("client_name"):
            item["client_name"] = _support_zone_client_label(
                agency=stock_row.get("agency"),
                fallback_name=fallback_name,
                agency_id=stock_row.get("agency_id"),
            )
        if not item.get("order_label"):
            item["order_label"] = _support_zone_order_label(
                order_type=_views()._clean_text(stock_row.get("order_type")),
                order_id=_views()._clean_text(stock_row.get("order_id")),
            )
        if not item.get("order_code"):
            item["order_code"] = _support_zone_order_code(
                zone=zone,
                row_num=row_num,
                order_id=_views()._clean_text(stock_row.get("order_id")),
                order_type=_views()._clean_text(stock_row.get("order_type")),
            )

    result: dict[str, dict] = {}
    for detail_key, bucket in grouped.items():
        items = sorted(bucket["items"].values(), key=lambda item: (item["client_name"], item["pallet_code"]))
        result[detail_key] = {
            "title": bucket["title"],
            "items": items,
        }
    return result


def build_stock_map_context(*, request) -> dict:
    cells = [
        {"zone": "PR", "row": "", "section": "", "tier": "", "cell": 150},
        {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": 20},
        {"zone": "OTG", "row": "", "section": "", "tier": "", "cell": 150},
        {"zone": "MR", "row": 1, "section": "", "tier": "", "cell": 50},
        {"zone": "MR", "row": 2, "section": "", "tier": "", "cell": 50},
        {"zone": "MR", "row": 3, "section": "", "tier": "", "cell": 50},
        {"zone": "MR", "row": 4, "section": "", "tier": "", "cell": 50},
    ]
    for row_num, sections in _views()._OS_ROW_SECTIONS.items():
        cells.append(
            {
                "zone": "OS",
                "row": row_num,
                "section": sections,
                "tier": _views()._os_max_tiers_for_row(row_num),
                "cell": _views()._OS_CELLS_PER_TIER,
                "total_slots": _views()._os_total_slots_for_row(row_num),
            }
        )
    occupied_os = set()
    occupied_mr = {}
    occupied_pr = 0
    occupied_obr = 0
    occupied_otg = 0
    for pallet in _views()._warehouse_pallet_locations():
        zone = _views()._normalize_zone(pallet.get("zone"))
        row_num = _views()._int_value(pallet.get("row"))
        section_num = _views()._int_value(pallet.get("section"))
        tier_num = _views()._int_value(pallet.get("tier"))
        cell_num = _views()._int_value(pallet.get("cell"))
        if zone == "OS" and row_num and section_num and tier_num and cell_num:
            occupied_os.add((row_num, section_num, tier_num, cell_num))
        elif zone == "PR":
            occupied_pr += 1
        elif zone == "OBR":
            occupied_obr += 1
        elif zone == "OTG":
            occupied_otg += 1
        elif zone == "MR" and row_num:
            occupied_mr[row_num] = occupied_mr.get(row_num, 0) + 1

    os_row_counts = {}
    for row_num, section_num, tier_num, cell_num in occupied_os:
        os_row_counts[row_num] = os_row_counts.get(row_num, 0) + 1

    for row in cells:
        section = _views()._int_value(row.get("section"))
        tier = _views()._int_value(row.get("tier"))
        cell = _views()._int_value(row.get("cell"))
        total = _views()._int_value(row.get("total_slots")) or (section * tier * cell if section and tier and cell else cell)
        if row.get("zone") == "OS" and row.get("row"):
            occupied = os_row_counts.get(_views()._int_value(row.get("row")), 0)
            row["occupied"] = occupied
            row["free"] = max(0, total - occupied)
            row.update(_views()._os_row_badge_style(occupied=occupied, total=total))
        elif row.get("zone") == "PR":
            row["occupied"] = occupied_pr
            row["free"] = max(0, total - occupied_pr)
        elif row.get("zone") == "OBR":
            row["occupied"] = occupied_obr
            row["free"] = max(0, total - occupied_obr)
        elif row.get("zone") == "OTG":
            row["occupied"] = occupied_otg
            row["free"] = max(0, total - occupied_otg)
        elif row.get("zone") == "MR" and row.get("row"):
            occupied = occupied_mr.get(_views()._int_value(row.get("row")), 0)
            row["occupied"] = occupied
            row["free"] = max(0, total - occupied)
        else:
            row["free"] = total
            row["occupied"] = 0
    role = get_request_role(request)
    return {
        "cells": cells,
        "cabinet_url": resolve_cabinet_url(role),
        "picker_mode": request.GET.get("picker") == "1",
    }


def build_stock_map_visual_context(*, request) -> dict:
    picker_mode = request.GET.get("picker") == "1"
    pallet_locations = _views()._warehouse_pallet_locations()
    occupied_os_counts: dict[tuple[int, int, int, int], int] = {}
    occupied_mr: dict[int, int] = {}
    occupied_pr = 0
    occupied_obr = 0
    occupied_otg = 0

    for pallet in pallet_locations:
        zone = _views()._normalize_zone(pallet.get("zone") or "")
        row_num = _views()._int_value(pallet.get("row"))
        section_num = _views()._int_value(pallet.get("section"))
        tier_num = _views()._int_value(pallet.get("tier"))
        cell_num = _views()._int_value(pallet.get("cell"))
        if zone == "OS" and row_num and section_num and tier_num and cell_num:
            key = (row_num, section_num, tier_num, cell_num)
            occupied_os_counts[key] = occupied_os_counts.get(key, 0) + 1
        elif zone == "PR":
            occupied_pr += 1
        elif zone == "OBR":
            occupied_obr += 1
        elif zone == "OTG":
            occupied_otg += 1
        elif zone == "MR" and row_num:
            occupied_mr[row_num] = occupied_mr.get(row_num, 0) + 1

    os_rows = []
    os_cell_details_by_row: dict[str, dict] = {}
    os_total_occupied = 0
    os_total_free = 0
    os_total_anomaly_cells = 0
    for row_num, sections_total in _views()._OS_ROW_SECTIONS.items():
        sections = []
        unique_occupied = 0
        stacked_cells = 0
        cell_details = _views()._os_row_cell_details(row_num)
        os_cell_details_by_row[str(row_num)] = cell_details
        for section_num in range(1, sections_total + 1):
            tiers = []
            section_occupied = 0
            tiers_total_for_section = _views()._os_tiers_for_position(row_num, section_num)
            for tier_num in range(1, tiers_total_for_section + 1):
                cells = []
                for cell_num in range(1, _views()._OS_CELLS_PER_TIER + 1):
                    key = (row_num, section_num, tier_num, cell_num)
                    pallet_count = occupied_os_counts.get(key, 0)
                    is_passage = _views()._os_is_passage_position(row_num, section_num, tier_num)
                    if pallet_count > 0:
                        unique_occupied += 1
                        section_occupied += 1
                    if pallet_count > 1:
                        stacked_cells += 1
                    detail_key = _views()._os_cell_key(section_num, tier_num, cell_num)
                    location = {
                        "zone": "OS",
                        "row": row_num,
                        "section": section_num,
                        "tier": tier_num,
                        "cell": cell_num,
                    }
                    cells.append(
                        {
                            "number": cell_num,
                            "pallet_count": pallet_count,
                            "state": "passage" if is_passage else ("stacked" if pallet_count > 1 else ("occupied" if pallet_count else "free")),
                            "detail_key": detail_key,
                            "detail_summary": (cell_details.get(detail_key) or {}).get("summary") or "",
                            "display_marker": (cell_details.get(detail_key) or {}).get("display_marker") or "",
                            "location": location,
                            "location_label": _views()._location_label(location),
                        }
                    )
                tiers.append({"number": tier_num, "cells": cells})
            sections.append(
                {
                    "number": section_num,
                    "tiers": tiers,
                    "occupied": section_occupied,
                    "total": sum(
                        _views()._OS_CELLS_PER_TIER
                        for tier_number in range(1, tiers_total_for_section + 1)
                        if not _views()._os_is_passage_position(row_num, section_num, tier_number)
                    ),
                }
            )
        total_slots = _views()._os_total_slots_for_row(row_num)
        free_slots = max(0, total_slots - unique_occupied)
        os_total_occupied += unique_occupied
        os_total_free += free_slots
        os_total_anomaly_cells += stacked_cells
        os_rows.append(
            {
                "row": row_num,
                "sections_total": sections_total,
                "tiers_total": _views()._os_max_tiers_for_row(row_num),
                "cells_per_tier": _views()._OS_CELLS_PER_TIER,
                "occupied": unique_occupied,
                "free": free_slots,
                "stacked_cells": stacked_cells,
                "total_slots": total_slots,
                "sections": sections,
                "cell_details": cell_details,
                **_views()._os_row_badge_style(occupied=unique_occupied, total=total_slots),
            }
        )

    max_sections_total = max(_views()._OS_ROW_SECTIONS.values(), default=0)
    os_row_numbers = sorted(_views()._OS_ROW_SECTIONS.keys())
    os_columns = []
    os_column_styles: dict[int, str] = {}
    for index, row_num in enumerate(os_row_numbers):
        tone = _os_line_tone(index)
        tone_style = _os_line_style(tone)
        os_columns.append(
            {
                "row": row_num,
                "label": f"Стеллаж {row_num}",
                "tone_style": tone_style,
            }
        )
        os_column_styles[row_num] = tone_style

    os_lower_columns = []
    for visible_row_num in os_row_numbers:
        actual_row_num = visible_row_num - 4
        os_lower_columns.append(
            {
                "row": visible_row_num,
                "actual_row": actual_row_num if actual_row_num in _views()._OS_ROW_SECTIONS else None,
                "label": f"Стеллаж {actual_row_num}" if actual_row_num in _views()._OS_ROW_SECTIONS else "",
                "tone_style": os_column_styles.get(visible_row_num, ""),
            }
        )
    os_matrix_rows = []
    for section_num in range(1, max_sections_total + 1):
        max_tiers_for_line = _views()._os_max_tiers_for_section(section_num)
        for tier_num in range(1, max_tiers_for_line + 1):
            matrix_cells = []
            use_shifted_layout = section_num >= 7
            for row_num in os_row_numbers:
                actual_row_num = row_num - 4 if use_shifted_layout else row_num
                tone_style = os_column_styles.get(row_num, "")
                if actual_row_num not in _views()._OS_ROW_SECTIONS:
                    for cell_num in range(1, _views()._OS_CELLS_PER_TIER + 1):
                        matrix_cells.append(
                            {
                                "row": 0,
                                "visual_row": row_num,
                                "section": section_num,
                                "tier": tier_num,
                                "cell": cell_num,
                                "state": "unavailable",
                                "pallet_count": 0,
                                "detail_key": "",
                                "detail_summary": "",
                                "location_code": "",
                                "location_label": "",
                                "line_start": cell_num == 1,
                                "tone_style": tone_style,
                            }
                        )
                    continue
                sections_total = _views()._OS_ROW_SECTIONS.get(actual_row_num, 0)
                for cell_num in range(1, _views()._OS_CELLS_PER_TIER + 1):
                    tiers_total_for_position = _views()._os_tiers_for_position(actual_row_num, section_num)
                    if section_num > sections_total or tier_num > tiers_total_for_position:
                        matrix_cells.append(
                            {
                                "row": 0,
                                "visual_row": row_num,
                                "section": section_num,
                                "tier": tier_num,
                                "cell": cell_num,
                                "state": "unavailable",
                                "pallet_count": 0,
                                "detail_key": "",
                                "detail_summary": "",
                                "location_code": "",
                                "location_label": "",
                                "line_start": cell_num == 1,
                                "tone_style": tone_style,
                            }
                        )
                        continue
                    key = (actual_row_num, section_num, tier_num, cell_num)
                    pallet_count = occupied_os_counts.get(key, 0)
                    is_passage = _views()._os_is_passage_position(actual_row_num, section_num, tier_num)
                    detail_key = _views()._os_cell_key(section_num, tier_num, cell_num)
                    details = os_cell_details_by_row.get(str(actual_row_num), {})
                    location = {
                        "zone": "OS",
                        "row": actual_row_num,
                        "section": section_num,
                        "tier": tier_num,
                        "cell": cell_num,
                    }
                    matrix_cells.append(
                        {
                            "row": actual_row_num,
                            "visual_row": row_num,
                            "section": section_num,
                            "tier": tier_num,
                            "cell": cell_num,
                            "state": "passage" if is_passage else ("stacked" if pallet_count > 1 else ("occupied" if pallet_count else "free")),
                            "pallet_count": pallet_count,
                            "detail_key": detail_key,
                            "detail_summary": (details.get(detail_key) or {}).get("summary") or "",
                            "display_marker": (details.get(detail_key) or {}).get("display_marker") or "",
                            "location_code": _views()._os_location_code(
                                row=actual_row_num,
                                section=section_num,
                                tier=tier_num,
                                cell=cell_num,
                            ),
                            "location_label": _views()._location_label(location),
                            "line_start": cell_num == 1,
                            "tone_style": tone_style,
                        }
                    )
            os_matrix_rows.append(
                {
                    "section": section_num,
                    "section_label": _os_line_display_label(section_num),
                    "tier": tier_num,
                    "cells": matrix_cells,
                    "section_start": tier_num == 1,
                    "section_end": tier_num == max_tiers_for_line,
                    "show_subheader": section_num == 7 and tier_num == 1,
                    "band_style": _os_section_style(section_num),
                }
            )

    support_zones = [
        {
            "zone": "PR",
            "title": "Приемка",
            "subtitle": "Временное размещение приемки",
            "occupied": occupied_pr,
            "free": max(0, 150 - occupied_pr),
            "total": 150,
            "detail_key": "PR",
            "pick_location": {"zone": "PR"},
            "pick_label": "PR · Зона приемки",
        },
        {
            "zone": "OTG",
            "title": "Отгрузка",
            "subtitle": "Буфер перед отправкой",
            "occupied": occupied_otg,
            "free": max(0, 150 - occupied_otg),
            "total": 150,
            "detail_key": "OTG",
            "pick_location": {"zone": "OTG"},
            "pick_label": "OTG · Зона отгрузки",
        },
        {
            "zone": "OBR",
            "title": "Обработка",
            "subtitle": "Подготовка и внутренние работы",
            "occupied": occupied_obr,
            "free": max(0, 20 - occupied_obr),
            "total": 20,
            "detail_key": "OBR",
            "pick_location": {"zone": "OBR"},
            "pick_label": "OBR · Зона обработки",
        },
    ]
    mr_rows = []
    for row_num in range(1, 5):
        occupied = occupied_mr.get(row_num, 0)
        mr_rows.append(
            {
                "zone": "MR",
                "row": row_num,
                "title": f"MR · Ряд {row_num}",
                "subtitle": "Между рядами",
                "occupied": occupied,
                "free": max(0, 50 - occupied),
                "total": 50,
                "detail_key": f"MR:{row_num}",
                "pick_location": {"zone": "MR", "row": row_num},
                "pick_label": _views()._location_label({"zone": "MR", "row": row_num}),
            }
        )
    support_zone_details = _support_zone_detail_map()
    for zone in support_zones:
        zone["has_details"] = bool(support_zone_details.get(zone["detail_key"], {}).get("items"))
    for zone in mr_rows:
        zone["has_details"] = bool(support_zone_details.get(zone["detail_key"], {}).get("items"))

    role = get_request_role(request)
    return {
        "cabinet_url": resolve_cabinet_url(role),
        "picker_mode": picker_mode,
        "os_rows": os_rows,
        "os_columns": os_columns,
        "os_lower_columns": os_lower_columns,
        "os_row_numbers": os_row_numbers,
        "os_matrix_rows": os_matrix_rows,
        "os_max_sections_total": max_sections_total,
        "os_cell_details_by_row": os_cell_details_by_row,
        "os_total_occupied": os_total_occupied,
        "os_total_free": os_total_free,
        "os_total_anomaly_cells": os_total_anomaly_cells,
        "support_zones": support_zones,
        "support_zone_details": support_zone_details,
        "mr_rows": mr_rows,
        "support_total_occupied": occupied_pr + occupied_obr + occupied_otg + sum(item["occupied"] for item in mr_rows),
        "support_total_free": (
            max(0, 150 - occupied_pr)
            + max(0, 20 - occupied_obr)
            + max(0, 150 - occupied_otg)
            + sum(item["free"] for item in mr_rows)
        ),
    }


def build_stock_map_row_context(*, request, row_number: int) -> dict:
    section_count = _views()._OS_ROW_SECTIONS.get(row_number)
    if not section_count:
        raise Http404("Линия не найдена")

    occupied_cells = set()
    for pallet in _views()._warehouse_pallet_locations():
        zone = _views()._normalize_zone(pallet.get("zone"))
        row_num = _views()._int_value(pallet.get("row"))
        section_num = _views()._int_value(pallet.get("section"))
        tier_num = _views()._int_value(pallet.get("tier"))
        cell_num = _views()._int_value(pallet.get("cell"))
        if zone != "OS" or row_num != row_number:
            continue
        if section_num and tier_num and cell_num:
            occupied_cells.add((section_num, tier_num, cell_num))
    cell_details = _views()._os_row_cell_details(row_number)
    sections = []
    for section_number in range(1, section_count + 1):
        tiers = []
        tiers_total_for_section = _views()._os_tiers_for_position(row_number, section_number)
        for tier_number in range(1, tiers_total_for_section + 1):
            cells = []
            for cell_number in range(1, _views()._OS_CELLS_PER_TIER + 1):
                key = (section_number, tier_number, cell_number)
                key_token = _views()._os_cell_key(section_number, tier_number, cell_number)
                cells.append(
                    {
                        "number": cell_number,
                        "is_passage": _views()._os_is_passage_position(row_number, section_number, tier_number),
                        "occupied": key in occupied_cells,
                        "detail_key": key_token,
                        "detail_summary": (cell_details.get(key_token) or {}).get("summary") or "",
                        "display_marker": (cell_details.get(key_token) or {}).get("display_marker") or "",
                    }
                )
            tiers.append({"number": tier_number, "cells": cells})
        sections.append({"number": section_number, "line_label": _os_line_display_label(section_number), "tiers": tiers})
    role = get_request_role(request)
    return {
        "cabinet_url": resolve_cabinet_url(role),
        "row_number": row_number,
        "sections": sections,
        "cells_per_tier": _views()._OS_CELLS_PER_TIER,
        "tiers_total": _views()._os_max_tiers_for_row(row_number),
        "picker_mode": request.GET.get("picker") == "1",
        "cell_details": cell_details,
    }


def parse_pr_destinations_json(raw: str) -> dict[str, dict]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        source = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(source, list):
        return {}
    result: dict[str, dict] = {}
    seen_os: set[tuple[int, int, int, int]] = set()
    for item in source:
        if not isinstance(item, dict):
            continue
        pallet_code = str(item.get("pallet_code") or item.get("palletCode") or "").strip()
        if not pallet_code:
            continue
        destination = item.get("destination") if isinstance(item.get("destination"), dict) else item
        normalized = normalize_putaway_location(destination)
        if normalized.get("zone") != "OS":
            continue
        row = _views()._int_value(normalized.get("row"))
        section = _views()._int_value(normalized.get("section"))
        tier = _views()._int_value(normalized.get("tier"))
        cell = _views()._int_value(normalized.get("cell"))
        if not (row and section and tier and cell):
            continue
        if _views()._os_is_passage_position(row, section, tier):
            continue
        os_key = (row, section, tier, cell)
        if os_key in seen_os:
            continue
        seen_os.add(os_key)
        result[pallet_code] = {
            "zone": "OS",
            "row": row,
            "section": section,
            "tier": tier,
            "cell": cell,
        }
    return result


def build_stock_map_pr_context(*, request) -> dict:
    destinations_override = parse_pr_destinations_json(request.GET.get("destinations_json") or "")
    rows = _views()._pr_zone_rows(destinations_override=destinations_override)
    role = get_request_role(request)
    return {
        "cabinet_url": resolve_cabinet_url(role),
        "rows": rows,
        "created_count": _views()._int_value(request.GET.get("created")),
        "skipped_count": _views()._int_value(request.GET.get("skipped")),
        "error_message": (request.GET.get("error") or "").strip(),
        "destinations_json": json.dumps(
            [
                {"pallet_code": row["pallet_code"], "destination": row["destination"]}
                for row in rows
                if isinstance(row, dict)
            ],
            ensure_ascii=True,
        ),
    }


def submit_stock_map_pr_moves(*, request):
    selected_codes = [
        str(value or "").strip()
        for value in request.POST.getlist("selected_pallets")
        if str(value or "").strip()
    ]
    if not selected_codes:
        return redirect("/stockmap/pr/?error=Выберите хотя бы одну паллету.")
    destinations_override = parse_pr_destinations_json(request.POST.get("destinations_json") or "")
    rows = _views()._pr_zone_rows(destinations_override=destinations_override)
    selected_set = set(selected_codes)
    selected_rows = [row for row in rows if str(row.get("pallet_code") or "") in selected_set]
    if not selected_rows:
        return redirect("/stockmap/pr/?error=Не удалось определить выбранные паллеты.")

    actor = Employee.objects.filter(user=request.user, is_active=True).first()
    if actor and actor.full_name:
        actor_name = actor.full_name
    elif request.user.is_authenticated:
        actor_name = request.user.get_full_name().strip() or request.user.username or str(request.user)
    else:
        actor_name = "Сотрудник"
    actor_role = get_request_role(request)

    grouped_specs: dict[tuple[int, str, int, int, int, int], dict] = {}
    skipped_count = 0
    for row in selected_rows:
        if not row.get("can_move_to_os"):
            skipped_count += 1
            continue
        agency = row.get("agency")
        if agency is None:
            skipped_count += 1
            continue
        destination = row.get("destination") or {}
        destination_key = (
            int(getattr(agency, "id", 0) or 0),
            str(destination.get("zone") or "PR"),
            _views()._int_value(destination.get("row")),
            _views()._int_value(destination.get("section")),
            _views()._int_value(destination.get("tier")),
            _views()._int_value(destination.get("cell")),
        )
        group = grouped_specs.setdefault(
            destination_key,
            {
                "agency": agency,
                "destination": destination,
                "task_specs": [],
            },
        )
        pallet_code = str(row.get("pallet_code") or "").strip()
        from_location = {"zone": "PR", "row": "", "section": "", "tier": "", "cell": ""}
        to_location = {
            "zone": str(destination.get("zone") or "PR"),
            "row": _views()._int_value(destination.get("row")) or "",
            "section": _views()._int_value(destination.get("section")) or "",
            "tier": _views()._int_value(destination.get("tier")) or "",
            "cell": _views()._int_value(destination.get("cell")) or "",
        }
        payload = {
            "status": "created",
            "status_label": "Ожидает перевозки",
            "pallet_code": pallet_code,
            "from_location": from_location,
            "to_location": to_location,
            "from_label": "PR · Зона приемки",
            "to_label": _views()._location_label(to_location),
            "requested_by_name": actor_name,
            "requested_by_role": actor_role or "",
            "pick_mode": "full",
            "move_mode": MoveTask.MODE_PALLET_FULL,
            "requested_qty": "",
            "requested_sku": "",
            "requested_barcodes": [],
            "requested_goods_type": "",
            "available_qty": "",
            "processing_order_id": "",
            "receiving_order_id": "",
            "stockmap_zone": "PR",
            "stockmap_source_order_type": str(row.get("order_type") or ""),
            "stockmap_source_order_id": str(row.get("order_id") or ""),
        }
        group["task_specs"].append(
            {
                "description": f"Задание на перемещение паллеты {pallet_code} из PR в основной склад",
                "payload": payload,
            }
        )

    created_count = 0
    for group in grouped_specs.values():
        _move_request, move_ids = _create_batch_move_tasks(
            context_type="manual",
            context_id="stockmap-pr",
            agency=group["agency"],
            user=request.user if request.user.is_authenticated else None,
            requested_by_name=actor_name,
            requested_by_role=actor_role or "",
            destination=group["destination"],
            comment="Автосоздание задания ричтраку из карты склада для зоны PR",
            task_specs=group["task_specs"],
        )
        created_count += len(move_ids)

    return redirect(f"/stockmap/pr/?created={created_count}&skipped={skipped_count}")
