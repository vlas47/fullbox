import json
import re
import uuid

from django.shortcuts import redirect
from django.http import HttpResponseForbidden
from django.views.generic import TemplateView

from audit.models import OrderAuditEntry, log_order_action
from employees.access import RoleRequiredMixin, get_request_role, resolve_cabinet_url
from orders.views import OrdersDetailView
from sku.models import Agency, SKU


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


def _client_agency_from_request(request):
    if not request.user.is_authenticated:
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


def _inventory_items_for_agency(agency: Agency | None) -> list[dict]:
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
    goods_type_labels = {
        "op": "Оптовый",
        "gv": "Готовый",
        "br": "Брак",
        "vz": "Возврат",
        "rh": "Расходный",
        "no": "Не обработанный",
    }
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

    items = []
    for item in totals.values():
        sku_obj = sku_map.get(item.get("sku"))
        barcode = _barcode_value_for_sku(sku_obj, item.get("size")) if sku_obj else "-"
        photo_url = sku_photo_url(sku_obj)
        items.append(
            {
                "sku": item.get("sku") or "",
                "name": item.get("name") or "",
                "size": item.get("size") or "",
                "barcode": barcode or "-",
                "qty": item.get("qty") or 0,
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


def _submit_processing(request):
    submit_action = (request.POST.get("submit_action") or "send").strip().lower()
    is_draft = submit_action == "draft"
    draft_order_id = (request.POST.get("draft_order_id") or "").strip()
    client_agency = getattr(request, "_client_agency", None) or _client_agency_from_request(request)
    if client_agency:
        agency = client_agency
    else:
        agency_id = request.POST.get("agency_id")
        agency = Agency.objects.filter(pk=agency_id).first()
    if not agency:
        return ProcessingHomeView().get(request, error="Выберите клиента.")

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
            for row in card.get("rows") or []:
                article_value = (row.get("article") or base_article).strip()
                stock_rows.append(
                    {
                        "article": article_value,
                        "size": (row.get("size") or "").strip(),
                        "barcode": (row.get("barcode") or "").strip(),
                        "qty": row.get("qty"),
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
        for item in _inventory_items_for_agency(agency):
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
            if max_qty is not None and qty_value > max_qty:
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
    payload["status"] = status_value
    payload["status_label"] = status_label
    if is_draft:
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
    if not is_draft and draft_order_id:
        OrderAuditEntry.objects.filter(
            order_id=draft_order_id,
            order_type="processing",
            agency=agency,
        ).delete()
    return redirect(
        f"/orders/processing/?client={agency.id}&ok=1&status={status_value}&order={order_id}"
    )


class ProcessingHomeView(RoleRequiredMixin, TemplateView):
    template_name = "processing/processing.html"
    allowed_roles = ("manager", "storekeeper", "head_manager", "director", "admin")

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

        status = self.request.GET.get("status")
        status_label = "Подготовка заявки"
        if status == "draft":
            status_label = "Черновик"
        elif status in {"sent_unconfirmed", "send", "submitted"}:
            status_label = "Ждет подтверждения"
        ctx["status_label"] = status_label
        ctx["order_number"] = self.request.GET.get("order", "")

        client_id = self.request.GET.get("client")
        agency_id = self.request.GET.get("agency")
        agency_key = client_id or agency_id
        client_agency = getattr(self.request, "_client_agency", None) or _client_agency_from_request(self.request)
        agency = client_agency or (Agency.objects.filter(pk=agency_key).first() if agency_key else None)
        ctx["agency"] = agency
        ctx["client_view"] = bool(client_agency)
        ctx["draft_order_id"] = ""
        order_id = self.request.GET.get("order")
        draft_payload = None
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
                if status_value == "draft" or "черновик" in status_label:
                    ctx["draft_order_id"] = order_id
                    draft_payload = {
                        "product_name": payload.get("product_name") or "",
                        "article": payload.get("article") or "",
                        "product_photo_url": payload.get("product_photo_url") or "",
                        "cards": payload.get("cards") or [],
                        "stock_rows": payload.get("stock_rows") or [],
                        "defect_check": payload.get("defect_check") or "",
                        "defect_percent": payload.get("defect_percent") or "",
                        "marking_5840_needed": payload.get("marking_5840_needed") or "",
                        "marking_5840_qty": payload.get("marking_5840_qty") or "",
                        "marking_5840_each_needed": payload.get("marking_5840_each_needed") or "",
                        "marking_5840_each_qty": payload.get("marking_5840_each_qty") or "",
                        "tag_replace_needed": payload.get("tag_replace_needed") or "",
                        "tag_owner": payload.get("tag_owner") or "",
                        "bag_replace_needed": payload.get("bag_replace_needed") or "",
                        "bag_replace_type": payload.get("bag_replace_type") or "",
                        "bag_replace_supply": payload.get("bag_replace_supply") or "",
                        "bubble_wrap_needed": payload.get("bubble_wrap_needed") or "",
                        "bubble_wrap_size": payload.get("bubble_wrap_size") or "",
                        "shrink_wrap_needed": payload.get("shrink_wrap_needed") or "",
                        "shrink_wrap_size": payload.get("shrink_wrap_size") or "",
                        "set_build": payload.get("set_build") or "",
                        "set_qty": payload.get("set_qty") or "",
                        "insert_needed": payload.get("insert_needed") or "",
                        "insert_qty": payload.get("insert_qty") or "",
                        "direction_needed": payload.get("direction_needed") or "",
                        "direction_file": payload.get("direction_file") or "",
                        "box_forming": payload.get("box_forming") or "",
                        "box_forming_other": payload.get("box_forming_other") or "",
                        "comments": payload.get("comments") or "",
                    }
        ctx["draft_payload"] = draft_payload or {}
        ctx["draft_payload_json"] = json.dumps(draft_payload or {}, ensure_ascii=True)
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
        ctx["inventory_items_json"] = json.dumps(
            _inventory_items_for_agency(agency),
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
        return ctx
