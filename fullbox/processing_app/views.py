import json
import re
import uuid
from datetime import timedelta

from django.shortcuts import redirect
from django.http import HttpResponseForbidden
from django.utils import timezone
from django.views.generic import TemplateView

from audit.models import OrderAuditEntry, log_order_action
from employees.access import RoleRequiredMixin, get_request_role, resolve_cabinet_url, is_staff_role
from employees.models import Employee
from orders.views import OrdersDetailView
from sku.models import Agency, SKU
from sklad.models import InventoryState
from todo.models import Task

GOODS_TYPE_LABELS = {
    "op": "Оптовый",
    "gv": "Готовый",
    "br": "Брак",
    "vz": "Возврат",
    "rh": "Расходный",
    "no": "Не обработанный",
}


def _format_payload_value(value):
    if value is None or value == "":
        return "-"
    return str(value).strip()


def _format_payload_list(value):
    if value is None or value == "":
        return "-"
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(items) if items else "-"
    text = str(value).strip()
    return text if text else "-"


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


def _is_draft_payload(payload: dict | None) -> bool:
    payload = payload or {}
    status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
    status_label = (payload.get("status_label") or "").lower()
    return status_value == "draft" or "черновик" in status_label


def _normalize_goods_type(value):
    text = str(value or "").strip()
    if not text or text == "-":
        return ""
    lowered = text.lower()
    if lowered in GOODS_TYPE_LABELS:
        return GOODS_TYPE_LABELS[lowered].lower()
    return lowered


def _processing_reserve_maps(
    agency: Agency | None,
    exclude_order_id: str | None = None,
) -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str], int]]:
    if not agency:
        return {}, {}
    reserves = InventoryState.objects.filter(agency=agency, state="processing")
    if exclude_order_id:
        reserves = reserves.exclude(order_type="processing", order_id=str(exclude_order_id))
    reserve_map: dict[tuple[str, str, str], int] = {}
    reserve_any: dict[tuple[str, str], int] = {}
    for entry in reserves:
        sku = (entry.sku or "").strip()
        if not sku:
            continue
        size = (entry.size or "").strip()
        goods_key = _normalize_goods_type(entry.goods_type)
        key = (sku.lower(), size.lower(), goods_key)
        reserve_map[key] = reserve_map.get(key, 0) + (entry.qty or 0)
        any_key = (sku.lower(), size.lower())
        reserve_any[any_key] = reserve_any.get(any_key, 0) + (entry.qty or 0)
    return reserve_map, reserve_any


def _reserved_qty(
    reserve_map: dict[tuple[str, str, str], int],
    reserve_any: dict[tuple[str, str], int],
    sku: str,
    size: str,
    goods_type: str | None = None,
) -> int:
    sku_key = (sku or "").strip().lower()
    if not sku_key:
        return 0
    size_key = (size or "").strip().lower()
    goods_key = _normalize_goods_type(goods_type)
    if not goods_key:
        return reserve_any.get((sku_key, size_key), 0)
    return reserve_map.get((sku_key, size_key, goods_key), 0) + reserve_map.get(
        (sku_key, size_key, ""),
        0,
    )


def _next_order_number(order_type: str = "receiving") -> str:
    order_ids = (
        OrderAuditEntry.objects.filter(order_type=order_type)
        .values_list("order_id", flat=True)
        .distinct()
    )
    max_number = 0
    for order_id in order_ids:
        if not order_id:
            continue
        candidate = str(order_id).strip()
        if not re.fullmatch(r"\d+", candidate):
            continue
        try:
            number = int(candidate)
        except (TypeError, ValueError):
            continue
        if number > max_number:
            max_number = number
    next_number = max_number + 1
    while OrderAuditEntry.objects.filter(order_type=order_type, order_id=str(next_number)).exists():
        next_number += 1
    return str(next_number)


def _manager_due_date(submitted_at):
    cutoff = submitted_at.replace(hour=14, minute=0, second=0, microsecond=0)
    if submitted_at <= cutoff:
        return submitted_at.replace(hour=18, minute=0, second=0, microsecond=0)
    next_day = submitted_at + timedelta(days=1)
    return next_day.replace(hour=13, minute=0, second=0, microsecond=0)


def _create_processing_manager_task(order_id, agency, request, submitted_at):
    if not agency:
        return
    manager = (
        Employee.objects.filter(role="manager", is_active=True)
        .order_by("full_name")
        .first()
    )
    if not manager:
        return
    route = f"/orders/processing/{order_id}/"
    existing = Task.objects.filter(route=route, assigned_to=manager).exclude(status="done")
    if existing.exists():
        return
    description = f"Клиент: {agency.agn_name or agency.inn or agency.id}"
    Task.objects.create(
        title=f"Подтвердите заявку на обработку №{order_id}",
        description=description,
        route=route,
        assigned_to=manager,
        created_by=request.user if request.user.is_authenticated else None,
        due_date=_manager_due_date(submitted_at),
    )


def _create_processing_head_task(order_id, agency, request, submitted_at):
    if not agency:
        return
    head = (
        Employee.objects.filter(role="processing_head", is_active=True)
        .order_by("full_name")
        .first()
    )
    if not head:
        return
    route = f"/orders/processing/{order_id}/"
    existing = Task.objects.filter(route=route, assigned_to=head).exclude(status="done")
    if existing.exists():
        return
    description = f"Клиент: {agency.agn_name or agency.inn or agency.id}"
    Task.objects.create(
        title=f"Заявка на обработку №{order_id}",
        description=description,
        route=route,
        assigned_to=head,
        created_by=request.user if request.user.is_authenticated else None,
        due_date=submitted_at or timezone.localtime(),
    )


def _client_agency_from_request(request):
    if not request.user.is_authenticated:
        return None
    role = get_request_role(request)
    if is_staff_role(role):
        return None
    client_id = request.GET.get("client") or request.GET.get("agency")
    if client_id:
        return Agency.objects.filter(pk=client_id, portal_user=request.user).first()
    return Agency.objects.filter(portal_user=request.user).first()


def _barcode_value_for_sku(sku, size: str | None) -> str:
    if not sku:
        return "-"
    barcodes = list(getattr(sku, "barcodes", []).all())
    if not barcodes:
        return "-"
    size_value = (size or "").strip()
    if size_value:
        for barcode in barcodes:
            if (barcode.size or "").strip() == size_value:
                return barcode.value
    primary = next((barcode for barcode in barcodes if barcode.is_primary), None)
    return primary.value if primary else barcodes[0].value


def _inventory_items_for_agency(
    agency: Agency | None,
    exclude_order_id: str | None = None,
) -> list[dict]:
    if not agency:
        return []
    order_ids = list(
        OrderAuditEntry.objects.filter(order_type="receiving", agency=agency)
        .values_list("order_id", flat=True)
        .distinct()
    )
    if not order_ids:
        return []
    entries = list(
        OrderAuditEntry.objects.filter(order_type="receiving", order_id__in=order_ids)
        .select_related("agency")
        .order_by("-created_at")
    )
    goods_type_labels = GOODS_TYPE_LABELS
    goods_type_by_order = {}
    for entry in entries:
        if entry.order_id in goods_type_by_order:
            continue
        payload = entry.payload or {}
        goods_type = (payload.get("goods_type") or "").strip().lower()
        goods_label = (payload.get("goods_type_label") or "").strip()
        if not goods_label and goods_type in goods_type_labels:
            goods_label = goods_type_labels[goods_type]
        if goods_label or goods_type:
            goods_type_by_order[entry.order_id] = goods_label or goods_type
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

    totals = {}

    def add_item(item, goods_label: str):
        sku = (item.get("sku") or item.get("sku_code") or "").strip()
        name = (item.get("name") or "").strip()
        size = (item.get("size") or "").strip()
        qty = _parse_qty_value(item.get("qty"))
        if qty is None:
            qty = _parse_qty_value(item.get("actual_qty")) or 0
        if not any((sku, name, size)):
            return
        goods_label = goods_label or "-"
        key = (sku, name, size, goods_label)
        entry = totals.setdefault(
            key,
            {"sku": sku, "name": name, "size": size, "qty": 0, "goods_type": goods_label},
        )
        entry["qty"] += qty

    for entry in latest_by_order.values():
        payload = entry.payload or {}
        goods_label = goods_type_by_order.get(entry.order_id, "-")
        boxes = payload.get("act_boxes") or []
        pallets = payload.get("act_pallets") or []
        for box in boxes:
            for item in (box or {}).get("items") or []:
                add_item(item, goods_label)
        for pallet in pallets:
            for item in (pallet or {}).get("items") or []:
                add_item(item, goods_label)
        if not boxes and not pallets:
            for item in payload.get("act_items") or []:
                add_item(item, goods_label)

    sku_codes = {item["sku"] for item in totals.values() if item.get("sku")}
    sku_map = {}
    if sku_codes:
        for sku in (
            SKU.objects.filter(agency=agency, deleted=False, sku_code__in=sku_codes)
            .prefetch_related("barcodes", "photos")
        ):
            sku_map[sku.sku_code] = sku

    def normalize_photo_url(url: str) -> str:
        if not url:
            return ""
        if url.startswith(("http://", "https://", "/")):
            return url
        return f"/{url}"

    def sku_photo_url(sku_obj: SKU | None) -> str:
        if not sku_obj:
            return ""
        url = (sku_obj.img or "").strip()
        if url:
            return normalize_photo_url(url)
        photos = list(getattr(sku_obj, "photos", []).all())
        if photos:
            url = (photos[0].url or "").strip()
            return normalize_photo_url(url)
        return ""

    reserve_map, reserve_any = _processing_reserve_maps(agency, exclude_order_id=exclude_order_id)
    items = []
    for item in totals.values():
        sku_obj = sku_map.get(item.get("sku"))
        barcode = _barcode_value_for_sku(sku_obj, item.get("size")) if sku_obj else "-"
        photo_url = sku_photo_url(sku_obj)
        reserved_qty = _reserved_qty(
            reserve_map,
            reserve_any,
            item.get("sku") or "",
            item.get("size") or "",
            item.get("goods_type") or "",
        )
        available_qty = max((item.get("qty") or 0) - reserved_qty, 0)
        if available_qty <= 0:
            continue
        items.append(
            {
                "sku": item.get("sku") or "",
                "name": item.get("name") or "",
                "size": item.get("size") or "",
                "barcode": barcode or "-",
                "qty": available_qty,
                "goods_type": item.get("goods_type") or "-",
                "photo": photo_url,
            }
        )
    items.sort(
        key=lambda row: (
            row.get("name") or "",
            row.get("goods_type") or "",
            row.get("size") or "",
            row.get("sku") or "",
        )
    )
    return items


def _replace_processing_reserves(order_id: str, agency: Agency, stock_rows: list[dict]):
    if not order_id or not agency:
        return
    InventoryState.objects.filter(
        agency=agency,
        order_type="processing",
        order_id=str(order_id),
        state="processing",
    ).delete()
    reserves: dict[tuple[str, str, str, str], int] = {}
    for row in stock_rows or []:
        if not isinstance(row, dict):
            continue
        sku = (row.get("article") or row.get("sku") or "").strip()
        if not sku:
            continue
        qty_value = _parse_qty_value(row.get("qty"))
        if qty_value is None or qty_value <= 0:
            continue
        size = (row.get("size") or "").strip()
        barcode = (row.get("barcode") or "").strip()
        goods_type = (row.get("goods_type") or "").strip()
        key = (sku, size, barcode, goods_type)
        reserves[key] = reserves.get(key, 0) + qty_value
    if not reserves:
        return
    InventoryState.objects.bulk_create(
        [
            InventoryState(
                agency=agency,
                order_type="processing",
                order_id=str(order_id),
                sku=sku,
                size=size,
                barcode=barcode,
                goods_type=goods_type,
                qty=qty,
                state="processing",
            )
            for (sku, size, barcode, goods_type), qty in reserves.items()
        ]
    )


def _submit_processing(request):
    submit_action = (request.POST.get("submit_action") or "send").strip().lower()
    is_draft = submit_action == "draft"
    draft_order_id = (request.POST.get("draft_order_id") or "").strip()
    edit_order_id = (request.POST.get("edit_order_id") or "").strip()
    client_agency = getattr(request, "_client_agency", None) or _client_agency_from_request(request)
    if client_agency:
        agency = client_agency
    else:
        agency_id = request.POST.get("agency_id")
        agency = Agency.objects.filter(pk=agency_id).first()
    if not agency:
        return ProcessingHomeView().get(request, error="Выберите клиента.")

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
            return ProcessingHomeView().get(request, error="Заявка не найдена.")
        latest_payload = existing_entries[-1].payload or {}
        preserved_status = (latest_payload.get("status") or latest_payload.get("submit_action") or "").strip()
        preserved_label = (latest_payload.get("status_label") or "").strip()
        preserved_submit_action = (latest_payload.get("submit_action") or "").strip()
        status_lower = preserved_status.lower()
        label_lower = preserved_label.lower()
        if status_lower in {"done", "completed", "closed", "finished"} or "выполн" in label_lower:
            return ProcessingHomeView().get(
                request,
                error="Заявка уже утверждена и недоступна для редактирования.",
            )
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
        if not latest_draft or not _is_draft_payload(latest_draft.payload or {}):
            draft_order_id = ""
            existing_draft_entries = []

    cards_payload = []
    cards_json = (request.POST.get("cards_json") or "").strip()
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

    product_name = (request.POST.get("product_name") or "").strip()
    if cards_payload:
        product_name = cards_payload[0].get("product_name") or product_name
    if not is_draft:
        if cards_payload:
            if not any(card.get("product_name") for card in cards_payload):
                return ProcessingHomeView().get(request, error="Укажите наименование товара.")
        elif not product_name:
            return ProcessingHomeView().get(request, error="Укажите наименование товара.")

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
    global_article = (request.POST.get("article") or "").strip()
    if cards_payload:
        stock_rows = []
        for card in cards_payload:
            base_article = (card.get("article") or "").strip()
            goods_type = (card.get("goods_type") or "").strip()
            for row in card.get("rows") or []:
                article_value = (row.get("article") or base_article).strip()
                stock_rows.append(
                    {
                        "article": article_value,
                        "size": (row.get("size") or "").strip(),
                        "barcode": (row.get("barcode") or "").strip(),
                        "qty": row.get("qty"),
                        "goods_type": goods_type,
                    }
                )
    elif global_article:
        for row in stock_rows:
            if not (row.get("article") or "").strip():
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
        for item in _inventory_items_for_agency(agency, exclude_order_id=edit_order_id or None):
            sku_key = (item.get("sku") or "").strip().lower()
            size_key = (item.get("size") or "").strip().lower()
            qty_value = _parse_qty_value(item.get("qty")) or 0
            if sku_key:
                key = (sku_key, size_key)
                available_map[key] = available_map.get(key, 0) + qty_value
            barcode_value = (item.get("barcode") or "").strip()
            if barcode_value:
                barcode_map[barcode_value] = barcode_map.get(barcode_value, 0) + qty_value
        for row in stock_rows:
            qty_value = _parse_qty_value(row.get("qty"))
            if qty_value is None:
                continue
            sku_key = (row.get("article") or "").strip().lower()
            size_key = (row.get("size") or "").strip().lower()
            barcode_value = (row.get("barcode") or "").strip()
            max_qty = None
            if sku_key:
                max_qty = available_map.get((sku_key, size_key))
            if max_qty is None and barcode_value:
                max_qty = barcode_map.get(barcode_value)
            if max_qty is None:
                max_qty = 0
            if qty_value > max_qty:
                sku_label = row.get("article") or "-"
                size_label = row.get("size") or "-"
                return ProcessingHomeView().get(
                    request,
                    error=(
                        f"Количество для {sku_label} ({size_label}) превышает остаток: {max_qty}."
                    ),
                )

    primary_article = (request.POST.get("article") or "").strip()
    primary_photo_url = (request.POST.get("product_photo_url") or "").strip()
    if cards_payload:
        primary_article = cards_payload[0].get("article") or primary_article
        primary_photo_url = cards_payload[0].get("photo_url") or primary_photo_url

    payload = {
        "email": (request.POST.get("email") or "").strip(),
        "fio": (request.POST.get("fio") or "").strip(),
        "org": (request.POST.get("org") or "").strip(),
        "product_name": product_name,
        "product_photo_url": primary_photo_url,
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
            draft_order_id = f"draft-{uuid.uuid4().hex[:12]}"
        order_id = draft_order_id
        action = "update" if existing_draft_entries else "create"
        description = "Черновик заявки на обработку"
    else:
        order_id = _next_order_number(order_type="processing")
        action = "create"
        description = f"Заявка на обработку №{order_id}"
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
        _replace_processing_reserves(order_id, agency, stock_rows)
    if not is_draft and draft_order_id and not edit_order_id:
        OrderAuditEntry.objects.filter(
            order_id=draft_order_id,
            order_type="processing",
            agency=agency,
        ).delete()
    if edit_order_id:
        return redirect(f"/orders/processing/{order_id}/")
    if not is_draft and client_agency:
        _create_processing_manager_task(order_id, agency, request, timezone.localtime())
    return redirect(
        f"/orders/processing/?client={agency.id}&ok=1&status={status_value}&order={order_id}"
    )


class ProcessingHomeView(RoleRequiredMixin, TemplateView):
    template_name = "processing/processing.html"
    allowed_roles = ("manager", "storekeeper", "head_manager", "director", "admin")

    def _use_manager_template(self) -> bool:
        if getattr(self.request, "_client_agency", None):
            return False
        role = get_request_role(self.request)
        if role not in {"manager", "head_manager", "director", "admin"}:
            return False
        edit_flag = (self.request.GET.get("edit") or "").strip().lower()
        return edit_flag in {"1", "true", "yes"}

    def get_template_names(self):
        if self._use_manager_template():
            return ["processing/processing_manager.html"]
        return [self.template_name]

    def dispatch(self, request, *args, **kwargs):
        client_agency = _client_agency_from_request(request)
        if client_agency:
            request._client_agency = client_agency
            return TemplateView.dispatch(self, request, *args, **kwargs)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        status = (request.GET.get("status") or "").lower()
        ok = request.GET.get("ok") == "1"
        submitted = kwargs.get("submitted") or (ok and status != "draft")
        draft_saved = ok and status == "draft"
        error = kwargs.get("error")
        order_id = request.GET.get("order")
        if order_id and not getattr(request, "_client_agency", None):
            latest_entry = (
                OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
                .order_by("-created_at")
                .first()
            )
            if latest_entry and _is_draft_payload(latest_entry.payload or {}):
                return HttpResponseForbidden("Доступ запрещен")
        ctx = self.get_context_data(
            submitted=submitted,
            draft_saved=draft_saved,
            error=error,
            **kwargs,
        )
        return self.render_to_response(ctx)

    def post(self, request, *args, **kwargs):
        return _submit_processing(request)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["submitted"] = kwargs.get("submitted", False)
        ctx["draft_saved"] = kwargs.get("draft_saved", False)
        ctx["error"] = kwargs.get("error")
        ctx["cabinet_url"] = resolve_cabinet_url(get_request_role(self.request))

        def resolve_status_label(status_value: str | None, fallback_label: str | None = None) -> str:
            if fallback_label:
                return fallback_label
            value = (status_value or "").strip().lower()
            if value == "draft":
                return "Черновик"
            if value in {"sent_unconfirmed", "send", "submitted"}:
                return "Ждет подтверждения"
            return "Подготовка заявки" if not value else value

        status = self.request.GET.get("status")
        status_label = resolve_status_label(status)
        ctx["order_number"] = self.request.GET.get("order", "")

        client_id = self.request.GET.get("client")
        agency_id = self.request.GET.get("agency")
        agency_key = client_id or agency_id
        client_agency = getattr(self.request, "_client_agency", None) or _client_agency_from_request(self.request)
        agency = client_agency or (Agency.objects.filter(pk=agency_key).first() if agency_key else None)
        ctx["agency"] = agency
        ctx["client_view"] = bool(client_agency)
        ctx["draft_order_id"] = ""
        ctx["edit_order_id"] = ""
        order_id = self.request.GET.get("order")
        draft_payload = None
        role = get_request_role(self.request)
        edit_flag = (self.request.GET.get("edit") or "").strip().lower()
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
                status_value = (payload.get("submit_action") or payload.get("status") or "").lower()
                status_label = (payload.get("status_label") or "").lower()
                is_draft = status_value == "draft" or "черновик" in status_label
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
            payload_label = (draft_payload.get("status_label") or "").strip()
            status_label = resolve_status_label(payload_status, payload_label)
        ctx["status_label"] = status_label
        return ctx


class ProcessingStockPickerView(RoleRequiredMixin, TemplateView):
    template_name = "processing/stock_picker.html"
    allowed_roles = ("manager", "storekeeper", "head_manager", "director", "admin")

    def dispatch(self, request, *args, **kwargs):
        client_agency = _client_agency_from_request(request)
        if client_agency:
            request._client_agency = client_agency
            return TemplateView.dispatch(self, request, *args, **kwargs)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["cabinet_url"] = resolve_cabinet_url(get_request_role(self.request))

        client_id = self.request.GET.get("client")
        agency_id = self.request.GET.get("agency")
        agency_key = client_id or agency_id
        client_agency = getattr(self.request, "_client_agency", None) or _client_agency_from_request(self.request)
        agency = client_agency or (Agency.objects.filter(pk=agency_key).first() if agency_key else None)
        ctx["agency"] = agency
        ctx["client_view"] = bool(client_agency)
        exclude_order_id = (self.request.GET.get("order") or "").strip() or None
        ctx["inventory_items_json"] = json.dumps(
            _inventory_items_for_agency(agency, exclude_order_id=exclude_order_id),
            ensure_ascii=True,
        )
        ctx["return_url"] = f"/orders/processing/?client={agency.id}" if agency else "/orders/processing/"
        return ctx


def delete_processing_draft(request, order_id: str):
    if request.method != "POST":
        return HttpResponseForbidden("Доступ запрещен")
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    client_agency = _client_agency_from_request(request)
    if not client_agency:
        return HttpResponseForbidden("Доступ запрещен")
    entries = OrderAuditEntry.objects.filter(
        order_id=order_id,
        order_type="processing",
        agency=client_agency,
    ).order_by("-created_at")
    latest = entries.first()
    if not latest or not _is_draft_payload(latest.payload or {}):
        return HttpResponseForbidden("Доступ запрещен")
    entries.delete()
    return redirect(f"/client/dashboard/?client={client_agency.id}")


class ProcessingDetailView(OrdersDetailView):
    order_type = "processing"

    def dispatch(self, request, *args, **kwargs):
        order_id = kwargs.get("order_id")
        client_agency = _client_agency_from_request(request)
        if client_agency:
            request._client_agency = client_agency
        if order_id and not client_agency:
            latest_entry = (
                OrderAuditEntry.objects.filter(order_id=order_id, order_type=self.order_type)
                .order_by("-created_at")
                .first()
            )
            if latest_entry and _is_draft_payload(latest_entry.payload or {}):
                return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        order_id = kwargs.get("order_id")
        entries_list = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type=self.order_type)
            .select_related("user", "agency")
            .order_by("created_at")
        )
        payload = self._payload_from_entries(entries_list)
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

        def _non_empty(value) -> str:
            if value is None:
                return ""
            if isinstance(value, bool):
                return "Да" if value else ""
            if isinstance(value, (int, float)):
                return "" if value == 0 else str(value)
            text = str(value).strip()
            if not text or text in {"-", "0", "0.0"}:
                return ""
            if text.isdigit() and int(text) == 0:
                return ""
            return text

        def _format_list_value(value) -> str:
            if value is None:
                return ""
            if isinstance(value, (list, tuple, set)):
                items = [str(item).strip() for item in value if str(item).strip()]
                return ", ".join(items)
            return _non_empty(value)

        def _add_param(rows: list[dict], label: str, value: str | None, extras: list[str] | None = None) -> None:
            base = _non_empty(value)
            extra_values = [item for item in (extras or []) if _non_empty(item)]
            if not base and not extra_values:
                return
            if base and extra_values:
                value_text = f"{base}; " + "; ".join(extra_values)
            elif base:
                value_text = base
            else:
                value_text = "; ".join(extra_values)
            rows.append({"label": label, "value": value_text})

        insert_types = payload.get("insert_types") or []
        if isinstance(insert_types, str):
            insert_types = [insert_types] if insert_types else []
        insert_other = (payload.get("insert_other") or "").strip()
        if insert_other:
            insert_types = list(insert_types) + [insert_other]

        marking_stickers = payload.get("marking_stickers") or []
        if isinstance(marking_stickers, str):
            marking_stickers = [marking_stickers] if marking_stickers else []
        marking_sizes = payload.get("marking_sizes") or []
        if isinstance(marking_sizes, str):
            marking_sizes = [marking_sizes] if marking_sizes else []

        processing_params = []
        defect_percent = _non_empty(payload.get("defect_percent"))
        if defect_percent and defect_percent.isdigit():
            defect_percent = f"{defect_percent}%"
        _add_param(processing_params, "Проверка на брак", defect_percent)
        _add_param(processing_params, "Маркировка 58/40", payload.get("marking_5840_qty"))
        _add_param(processing_params, "Маркировка 58/40 (шт/чз)", payload.get("marking_5840_each_qty"))
        _add_param(processing_params, "Замена бирок", payload.get("tag_owner"))

        def _add_pack_param(label: str, base_value: str | None, prefix: str) -> None:
            extras = []
            type_value = _non_empty(payload.get(f"{prefix}_type"))
            size_value = _non_empty(payload.get(f"{prefix}_size"))
            qty_value = _non_empty(payload.get(f"{prefix}_qty"))
            supply_value = _non_empty(payload.get(f"{prefix}_supply"))
            if type_value:
                extras.append(f"Тип: {type_value}")
            if size_value:
                extras.append(f"Размер: {size_value}")
            if qty_value:
                extras.append(f"Кол-во: {qty_value}")
            if supply_value:
                extras.append(f"Закупка: {supply_value}")
            _add_param(
                processing_params,
                label,
                base_value or payload.get(f"{prefix}_needed"),
                extras=extras,
            )

        _add_pack_param("Замена пакета", payload.get("bag_replace_type"), "bag_replace")
        _add_pack_param("Упаковка в Бабл пленку", payload.get("bubble_wrap_size"), "bubble_wrap")
        _add_pack_param("Упаковка в термо пленку", payload.get("shrink_wrap_size"), "shrink_wrap")
        _add_pack_param("Замена гофрокороба", payload.get("box_replace_type"), "box_replace")
        set_qty = _non_empty(payload.get("set_qty"))
        _add_param(
            processing_params,
            "Сборка набора",
            set_qty and f"Кол-во: {set_qty}" or payload.get("set_build"),
        )
        insert_qty = _non_empty(payload.get("insert_qty"))
        insert_types_label = _format_list_value(insert_types)
        if insert_other and insert_other not in insert_types:
            insert_types_label = _format_list_value(list(insert_types) + [insert_other])
        _add_param(
            processing_params,
            "Вложение",
            insert_qty and f"Кол-во: {insert_qty}" or payload.get("insert_needed"),
            extras=[f"Типы: {insert_types_label}" if insert_types_label else ""],
        )
        direction_file = _non_empty(payload.get("direction_file"))
        _add_param(
            processing_params,
            "Распределение по направлениям",
            direction_file and f"Файл: {direction_file}" or payload.get("direction_needed"),
        )
        box_forming = _non_empty(payload.get("box_forming"))
        if box_forming == "other":
            box_forming = _non_empty(payload.get("box_forming_other"))
        _add_param(processing_params, "Формирование короба", box_forming)
        _add_param(processing_params, "Прочие", payload.get("comments"))
        _add_param(processing_params, "Маркировка", _format_list_value(marking_stickers))
        _add_param(processing_params, "Размеры стикеров", _format_list_value(marking_sizes))
        _add_param(processing_params, "Информационный", payload.get("marking_info"))
        _add_param(processing_params, "Вытянуть из мешка и наклеить ЧЗ", payload.get("pull_from_bag"))
        _add_param(processing_params, "Проверка на брак (кол-во)", payload.get("defect_qty"))
        _add_param(processing_params, "Обрезание ниток (кол-во)", payload.get("trim_threads_qty"))
        _add_param(processing_params, "Скрепление скотчем (кол-во)", payload.get("tape_qty"))
        _add_param(processing_params, "Удаление бирки", payload.get("remove_tag"))
        _add_param(processing_params, "Удаление бирки (кол-во)", payload.get("remove_tag_qty"))
        _add_param(processing_params, "Скрепление бирки", payload.get("attach_tag"))
        _add_param(processing_params, "Скрепление бирки (кол-во)", payload.get("attach_tag_qty"))

        def format_pack(prefix: str, title: str):
            needed = _format_payload_value(payload.get(f"{prefix}_needed"))
            parts = []
            type_value = (payload.get(f"{prefix}_type") or "").strip()
            size_value = (payload.get(f"{prefix}_size") or "").strip()
            qty_value = (payload.get(f"{prefix}_qty") or "").strip()
            supply_value = (payload.get(f"{prefix}_supply") or "").strip()
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
            {"label": "Наименование товара", "value": _format_payload_value(payload.get("product_name"))},
            {"label": "Поставщик", "value": _format_payload_value(payload.get("supplier"))},
            {"label": "Бренд", "value": _format_payload_value(payload.get("brand"))},
            {"label": "Предмет", "value": _format_payload_value(payload.get("subject"))},
            {"label": "Артикул", "value": _format_payload_value(payload.get("article"))},
            {"label": "Артикул ВБ", "value": _format_payload_value(payload.get("wb_article"))},
            {"label": "Цвет", "value": _format_payload_value(payload.get("color"))},
            {"label": "Состав", "value": _format_payload_value(payload.get("composition"))},
            {"label": "Пол", "value": _format_payload_value(payload.get("gender"))},
            {"label": "Сезон", "value": _format_payload_value(payload.get("season"))},
            {"label": "Заказ №", "value": _format_payload_value(payload.get("order_no"))},
            {"label": "Приобретение в 1С №", "value": _format_payload_value(payload.get("purchase_1c_no"))},
            {"label": "Приобретение в 1С от", "value": _format_payload_value(payload.get("purchase_1c_date"))},
            {"label": "Менеджер проекта", "value": _format_payload_value(payload.get("project_manager"))},
            {"label": "Склад приемка", "value": _format_payload_value(payload.get("warehouse_receiving"))},
            {"label": "Склад упаковка", "value": _format_payload_value(payload.get("warehouse_packing"))},
            {"label": "Склад раскоробовка", "value": _format_payload_value(payload.get("warehouse_unpacking"))},
            {"label": "Замер в упаковке", "value": _format_payload_value(payload.get("measure_needed"))},
            {"label": "Вес (грамм)", "value": _format_payload_value(payload.get("measure_weight"))},
            {"label": "Ширина (см)", "value": _format_payload_value(payload.get("measure_width"))},
            {"label": "Высота (см)", "value": _format_payload_value(payload.get("measure_height"))},
            {"label": "Глубина (см)", "value": _format_payload_value(payload.get("measure_depth"))},
            {"label": "Проверка на брак (%)", "value": _format_payload_value(payload.get("defect_percent"))},
            {"label": "Проверка на брак (кол-во)", "value": _format_payload_value(payload.get("defect_qty"))},
            {"label": "Обрезание ниток (кол-во)", "value": _format_payload_value(payload.get("trim_threads_qty"))},
            {"label": "Скрепление скотчем (кол-во)", "value": _format_payload_value(payload.get("tape_qty"))},
            {"label": "Удаление бирки", "value": _format_payload_value(payload.get("remove_tag"))},
            {"label": "Удаление бирки (кол-во)", "value": _format_payload_value(payload.get("remove_tag_qty"))},
            {"label": "Скрепление бирки", "value": _format_payload_value(payload.get("attach_tag"))},
            {"label": "Скрепление бирки (кол-во)", "value": _format_payload_value(payload.get("attach_tag_qty"))},
            {"label": "Маркировка", "value": _format_payload_list(marking_stickers)},
            {"label": "Размеры стикеров", "value": _format_payload_list(marking_sizes)},
            {"label": "Информационный", "value": _format_payload_value(payload.get("marking_info"))},
            {"label": "Сборка набора", "value": _format_payload_value(payload.get("set_build"))},
            {"label": "Кол-во ед. в наборе", "value": _format_payload_value(payload.get("set_qty"))},
            {"label": "Доп. вложение", "value": _format_payload_value(payload.get("insert_needed"))},
            {"label": "Типы вложений", "value": _format_payload_list(insert_types)},
            {"label": "Вытянуть из мешка и наклеить ЧЗ", "value": _format_payload_value(payload.get("pull_from_bag"))},
            format_pack("bubble_wrap", "Упаковка в бабл пленку"),
            format_pack("bag_replace", "Замена пакета"),
            format_pack("box_replace", "Замена гофрокороба"),
            format_pack("shrink_wrap", "Термоусадочная упаковка"),
            {"label": "Кол-во оптовых мест", "value": _format_payload_value(payload.get("wholesale_places_qty"))},
            {"label": "Счет №", "value": _format_payload_value(payload.get("invoice_no"))},
            {"label": "Дата выставления", "value": _format_payload_value(payload.get("invoice_date"))},
            {"label": "Дата оплаты", "value": _format_payload_value(payload.get("payment_date"))},
            {"label": "Бухгалтер", "value": _format_payload_value(payload.get("accountant"))},
            {"label": "В архив (дата)", "value": _format_payload_value(payload.get("archive_date"))},
            {"label": "Исполнитель", "value": _format_payload_value(payload.get("executor_name"))},
            {"label": "Дата начала", "value": _format_payload_value(payload.get("start_date"))},
            {"label": "Дата окончания", "value": _format_payload_value(payload.get("end_date"))},
            {"label": "Дата приема заказа", "value": _format_payload_value(payload.get("receive_date"))},
            {"label": "Ответственный", "value": _format_payload_value(payload.get("responsible_name"))},
            {"label": "Комментарий", "value": _format_payload_value(payload.get("comments"))},
        ]

        ctx["processing_fields"] = processing_fields
        ctx["processing_size_rows"] = size_rows
        ctx["processing_unboxing_rows"] = unboxing_rows
        ctx["processing_params"] = processing_params
        ctx["processing_meta"] = {
            "product_name": _format_payload_value(payload.get("product_name")),
            "order_no": _format_payload_value(payload.get("order_no")),
            "supplier": _format_payload_value(payload.get("supplier")),
            "brand": _format_payload_value(payload.get("brand")),
            "subject": _format_payload_value(payload.get("subject")),
        }
        ctx["items"] = []
        ctx["can_edit_order"] = False
        ctx["can_send_to_warehouse"] = False
        ctx["can_create_receiving_act"] = False
        ctx["has_receiving_act"] = False
        ctx["has_placement_act"] = False
        ctx["act_label"] = ""
        ctx["placement_act_label"] = ""
        ctx["can_send_act_to_client"] = False
        status_payload = entries_list[-1].payload if entries_list else {}
        status_value = (status_payload.get("status") or status_payload.get("submit_action") or "").lower()
        status_label = (status_payload.get("status_label") or "").lower()
        is_done = (
            status_value in {"done", "completed", "closed", "finished", "processing_head"}
            or "выполн" in status_label
            or "утверж" in status_label
            or "передан" in status_label
        )
        is_waiting = status_value in {"sent_unconfirmed", "send", "submitted"} or "подтверждени" in status_label
        role = get_request_role(self.request)
        can_manage = role in {"manager", "head_manager", "director", "admin"}
        client_view = bool(ctx.get("client_view"))
        ctx["can_approve_processing"] = bool(can_manage and not client_view and is_waiting and not is_done)
        ctx["can_edit_processing"] = bool(can_manage and not client_view and not is_done)
        if ctx["can_edit_processing"]:
            agency = ctx.get("agency")
            if agency and getattr(agency, "id", None):
                ctx["processing_edit_url"] = (
                    f"/orders/processing/?order={order_id}&agency={agency.id}&edit=1"
                )
            else:
                ctx["processing_edit_url"] = f"/orders/processing/?order={order_id}&edit=1"
        return ctx

    def post(self, request, *args, **kwargs):
        order_id = kwargs.get("order_id")
        if not order_id:
            return redirect("/orders/")
        action = (request.POST.get("action") or "").strip().lower()
        if action == "approve_processing":
            role = get_request_role(request)
            if role not in {"manager", "head_manager", "director", "admin"}:
                return HttpResponseForbidden("Доступ запрещен")
            entries = list(
                OrderAuditEntry.objects.filter(order_id=order_id, order_type=self.order_type)
                .select_related("agency")
                .order_by("created_at")
            )
            if not entries:
                return redirect("/orders/")
            latest = entries[-1]
            status_payload = latest.payload or {}
            status_value = (status_payload.get("status") or status_payload.get("submit_action") or "").lower()
            status_label = (status_payload.get("status_label") or "").lower()
            if status_value in {"done", "completed", "closed", "finished"} or "выполн" in status_label:
                return redirect(f"/orders/processing/{order_id}/")
            payload = dict(self._payload_from_entries(entries))
            payload["status"] = "processing_head"
            payload["status_label"] = "Передано в обработку"
            payload["approved_at"] = timezone.localtime().isoformat()
            log_order_action(
                "status",
                order_id=order_id,
                order_type=self.order_type,
                user=request.user if request.user.is_authenticated else None,
                agency=latest.agency if latest else None,
                description="Заявка на обработку утверждена менеджером и передана в обработку",
                payload=payload,
            )
            Task.objects.filter(
                route=f"/orders/processing/{order_id}/",
                assigned_to__role="manager",
            ).exclude(status="done").update(status="done")
            _create_processing_head_task(
                order_id,
                latest.agency if latest else None,
                request,
                timezone.localtime(),
            )
            return redirect(f"/orders/processing/{order_id}/")
        comment = (request.POST.get("comment") or "").strip()
        if comment:
            latest = (
                OrderAuditEntry.objects.filter(order_id=order_id, order_type=self.order_type)
                .select_related("agency")
                .order_by("-created_at")
                .first()
            )
            log_order_action(
                "comment",
                order_id=order_id,
                order_type=self.order_type,
                user=request.user if request.user.is_authenticated else None,
                agency=latest.agency if latest else None,
                description=comment,
                payload={"comment": comment},
            )
        return redirect(f"/orders/processing/{order_id}/")
