from __future__ import annotations

from django.db import models
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse

from audit.models import log_sku_change
from employees.access import get_request_role, resolve_cabinet_url
from labels.utils import load_available_printers_data, load_label_settings

from .models import Agency, SKU


VIEW_MODES = ("table", "cards")
SORT_FIELDS = {
    "sku_code": "sku_code",
    "name": "name",
    "brand": "brand",
    "agency": "agency__agn_name",
    "market": "market__name",
    "color": "color",
    "color_ref": "color_ref__name",
    "size": "size",
    "name_print": "name_print",
    "code": "code",
    "gender": "gender",
    "season": "season",
    "additional_name": "additional_name",
    "composition": "composition",
    "made_in": "made_in",
    "cr_product_date": "cr_product_date",
    "end_product_date": "end_product_date",
    "sign_akciz": "sign_akciz",
    "tovar_category": "tovar_category",
    "use_nds": "use_nds",
    "vid_tovar": "vid_tovar",
    "type_tovar": "type_tovar",
    "stor_unit": "stor_unit__stor_name",
    "weight_kg": "weight_kg",
    "weight_net_kg": "weight_net_kg",
    "weight_gross_kg": "weight_gross_kg",
    "volume": "volume",
    "length_mm": "length_mm",
    "honest_sign": "honest_sign",
    "source": "source",
    "source_reference": "source_reference",
    "created_at": "created_at",
    "updated_at": "updated_at",
}
FILTER_FIELDS = {
    "sku_code": "sku_code",
    "name": "name",
    "brand": "brand",
    "agency": "agency__agn_name",
    "market": "market__name",
    "color": "color",
    "color_ref": "color_ref__name",
    "size": "size",
    "name_print": "name_print",
    "code": "code",
    "gender": "gender",
    "season": "season",
    "additional_name": "additional_name",
    "composition": "composition",
    "made_in": "made_in",
    "tovar_category": "tovar_category",
    "vid_tovar": "vid_tovar",
    "type_tovar": "type_tovar",
    "stor_unit": "stor_unit__stor_name",
    "source_reference": "source_reference",
}
DEFAULT_SORT = "sku_code"


def normalize_size(value):
    return (value or "").strip()


def build_size_ui(item):
    barcodes = list(item.barcodes.all())
    barcode_sizes = []
    seen_sizes = set()
    for barcode in barcodes:
        normalized_size = normalize_size(barcode.size)
        if normalized_size and normalized_size not in seen_sizes:
            barcode_sizes.append(normalized_size)
            seen_sizes.add(normalized_size)

    if barcode_sizes:
        size_options = barcode_sizes
    else:
        sku_size = normalize_size(item.size)
        size_options = [sku_size] if sku_size else []

    preferred_size = normalize_size(item.size)
    if preferred_size and preferred_size in size_options:
        selected_size = preferred_size
    elif size_options:
        selected_size = size_options[0]
    else:
        selected_size = ""

    has_sized_barcodes = bool(barcode_sizes)
    barcode_rows = []
    for barcode in barcodes:
        barcode_size = normalize_size(barcode.size)
        is_visible = True
        if has_sized_barcodes:
            is_visible = barcode_size == selected_size
        barcode_rows.append(
            {
                "value": barcode.value,
                "size": barcode_size,
                "is_primary": barcode.is_primary,
                "visible": is_visible,
            }
        )

    item.size_options_ui = size_options
    item.selected_size_ui = selected_size
    item.size_display_ui = selected_size or preferred_size
    item.has_size_selector_ui = bool(size_options)
    item.has_sized_barcodes_ui = has_sized_barcodes
    item.barcode_rows_ui = barcode_rows


def build_sku_list_queryset(request, *, base_qs):
    qs = base_qs
    show_deleted = request.GET.get("deleted") == "1"
    if show_deleted:
        qs = qs.filter(deleted=True)
    else:
        qs = qs.filter(deleted=False)
    search = request.GET.get("q")
    if search:
        qs = qs.filter(
            models.Q(sku_code__icontains=search)
            | models.Q(name__icontains=search)
            | models.Q(barcodes__value__icontains=search)
        ).distinct()
    filter_field = request.GET.get("filter_field")
    filter_value = (request.GET.get("filter_value") or "").strip()
    if filter_field in FILTER_FIELDS and filter_value:
        lookup = FILTER_FIELDS[filter_field]
        qs = qs.filter(**{f"{lookup}__icontains": filter_value}).distinct()
    agency_filter = request.GET.get("agency")
    if agency_filter:
        qs = qs.filter(agency_id=agency_filter)
    sort_key = request.GET.get("sort", DEFAULT_SORT)
    direction = request.GET.get("dir", "asc")
    sort_field = SORT_FIELDS.get(sort_key, SORT_FIELDS[DEFAULT_SORT])
    order_by = f"-{sort_field}" if direction == "desc" else sort_field
    return qs.order_by(order_by).select_related(
        "market",
        "agency",
        "color_ref",
        "stor_unit",
    ).prefetch_related("barcodes", "photos", "marketplace_bindings")


def build_sku_sort_url(request, field: str, direction: str) -> str:
    params = request.GET.copy()
    if "view" not in params:
        params["view"] = "table"
    params["sort"] = field
    params["dir"] = direction
    if request.GET.get("filter_field"):
        params["filter_field"] = request.GET.get("filter_field")
    if request.GET.get("filter_value"):
        params["filter_value"] = request.GET.get("filter_value")
    return f"?{params.urlencode()}"


def build_sku_list_context(request, *, items) -> dict:
    view = request.GET.get("view", "table")
    if view not in VIEW_MODES:
        view = "table"
    current_sort = request.GET.get("sort", DEFAULT_SORT)
    current_dir = "desc" if request.GET.get("dir") == "desc" else "asc"
    sort_info = {}
    for field in SORT_FIELDS:
        is_current = current_sort == field
        next_dir = "desc" if is_current and current_dir == "asc" else "asc"
        sort_info[field] = {
            "url": build_sku_sort_url(request, field, next_dir),
            "active": is_current,
            "dir": current_dir if is_current else "",
            "next_dir": next_dir,
        }
    agency_filter = request.GET.get("agency") or ""
    available_printers, available_printers_meta = load_available_printers_data()
    request_user = getattr(request, "user", None)
    role = get_request_role(request) if request_user is not None else None
    for item in items:
        build_size_ui(item)
    return {
        "view_mode": view,
        "current_sort": current_sort,
        "current_dir": current_dir,
        "sort_info": sort_info,
        "filter_field": request.GET.get("filter_field") or "",
        "filter_value": request.GET.get("filter_value") or "",
        "show_deleted": request.GET.get("deleted") == "1",
        "agency_filter": agency_filter,
        "hide_client_column": bool(agency_filter),
        "agency_options": Agency.objects.filter(archived=False)
        .only("id", "agn_name", "fio_agn")
        .order_by("agn_name", "fio_agn", "id"),
        "available_printers": available_printers,
        "available_printers_meta": available_printers_meta,
        "label_settings": load_label_settings(),
        "cabinet_url": resolve_cabinet_url(role),
    }


def suggest_sku_payload(query: str) -> dict:
    query = (query or "").strip()
    if len(query) < 2:
        return {"items": []}
    qs = (
        SKU.objects.filter(
            models.Q(sku_code__icontains=query)
            | models.Q(name__icontains=query)
            | models.Q(barcodes__value__icontains=query)
        )
        .distinct()
        .order_by("sku_code")[:10]
    )
    return {
        "items": [{"value": sku.sku_code, "label": f"{sku.sku_code} — {sku.name}"} for sku in qs]
    }


def clone_sku_to_admin(*, pk: int, user=None):
    orig = get_object_or_404(SKU, pk=pk)
    base_code = f"{orig.sku_code}-copy"
    new_code = base_code
    counter = 1
    while SKU.objects.filter(sku_code=new_code).exists():
        new_code = f"{base_code}{counter}"
        counter += 1

    clone = SKU.objects.create(
        sku_code=new_code,
        name=orig.name,
        brand=orig.brand,
        market=orig.market,
        agency=orig.agency,
        color=orig.color,
        color_ref=orig.color_ref,
        size=orig.size,
        name_print=orig.name_print,
        code=None,
        img=orig.img,
        img_comment=orig.img_comment,
        gender=orig.gender,
        season=orig.season,
        additional_name=orig.additional_name,
        composition=orig.composition,
        made_in=orig.made_in,
        cr_product_date=orig.cr_product_date,
        end_product_date=orig.end_product_date,
        sign_akciz=orig.sign_akciz,
        tovar_category=orig.tovar_category,
        use_nds=orig.use_nds,
        vid_tovar=orig.vid_tovar,
        type_tovar=orig.type_tovar,
        stor_unit=orig.stor_unit,
        weight_kg=orig.weight_kg,
        weight_net_kg=orig.weight_net_kg,
        weight_gross_kg=orig.weight_gross_kg,
        volume=orig.volume,
        length_mm=orig.length_mm,
        width_mm=orig.width_mm,
        height_mm=orig.height_mm,
        honest_sign=orig.honest_sign,
        description=orig.description,
        source=orig.source,
        source_reference=None,
    )
    for photo in orig.photos.all():
        clone.photos.create(url=photo.url, sort_order=photo.sort_order)
    log_sku_change(
        "clone",
        clone,
        user=user if getattr(user, "is_authenticated", False) else None,
        description=f"Копия из {orig.sku_code}",
    )
    return reverse("admin:sku_sku_change", args=[clone.pk])


def build_sku_form_context(*, mode: str, title: str, submit_label: str) -> dict:
    return {
        "mode": mode,
        "title": title,
        "submit_label": submit_label,
        "label_settings": load_label_settings(),
    }


def build_sku_duplicate_initial(*, pk: int) -> dict:
    orig = get_object_or_404(SKU, pk=pk)
    initial = {
        "name": orig.name,
        "brand": orig.brand,
        "agency": orig.agency_id,
        "market": orig.market_id,
        "color": orig.color,
        "color_ref": orig.color_ref_id,
        "size": orig.size,
        "name_print": orig.name_print,
        "img": orig.img,
        "img_comment": orig.img_comment,
        "gender": orig.gender,
        "season": orig.season,
        "additional_name": orig.additional_name,
        "composition": orig.composition,
        "made_in": orig.made_in,
        "cr_product_date": orig.cr_product_date,
        "end_product_date": orig.end_product_date,
        "sign_akciz": orig.sign_akciz,
        "tovar_category": orig.tovar_category,
        "use_nds": orig.use_nds,
        "vid_tovar": orig.vid_tovar,
        "type_tovar": orig.type_tovar,
        "stor_unit": orig.stor_unit_id,
        "weight_kg": orig.weight_kg,
        "weight_net_kg": orig.weight_net_kg,
        "weight_gross_kg": orig.weight_gross_kg,
        "volume": orig.volume,
        "length_mm": orig.length_mm,
        "width_mm": orig.width_mm,
        "height_mm": orig.height_mm,
        "honest_sign": orig.honest_sign,
        "description": orig.description,
        "source": orig.source,
        "source_reference": None,
        "deleted": False,
    }
    base_code = f"{orig.sku_code}-copy"
    candidate = base_code
    counter = 1
    while SKU.objects.filter(sku_code=candidate).exists():
        candidate = f"{base_code}{counter}"
        counter += 1
    initial["sku_code"] = candidate
    return initial


def mark_sku_deleted(*, pk: int, user=None):
    sku = get_object_or_404(SKU, pk=pk)
    if not sku.deleted:
        sku.deleted = True
        sku.save(update_fields=["deleted"])
        log_sku_change(
            "delete",
            sku,
            user=user if getattr(user, "is_authenticated", False) else None,
            description="Пометка как удаленный",
        )
    return HttpResponseRedirect("/sku/")
