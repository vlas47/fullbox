from django.db import models
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import ListView, CreateView, UpdateView, TemplateView, FormView
from django.http import JsonResponse
from django.utils import timezone
import uuid

from sku.models import Agency, SKU
from sku.views import SKUCreateView, SKUUpdateView, SKUDuplicateView
from .forms import AgencyForm
from .services import fetch_party_by_inn
from audit.models import log_order_action


def dashboard(request):
    """Простой кабинет клиента с плейсхолдерами ключевых разделов."""
    client_id = request.GET.get("client")
    selected_client = None
    if client_id:
        selected_client = Agency.objects.filter(pk=client_id).first()
    return render(
        request,
        "client_cabinet/dashboard.html",
        {
            "selected_client": selected_client,
            "client_filter_param": f"?agency={selected_client.id}" if selected_client else "",
        },
    )


def receiving_redirect(request, pk: int):
    if not Agency.objects.filter(pk=pk).exists():
        return redirect("/client/")
    return redirect(f"/orders/receiving/?client={pk}")


class ClientListView(ListView):
    model = Agency
    paginate_by = 20
    template_name = "client_cabinet/clients_list.html"
    context_object_name = "items"
    view_modes = ("table", "cards")
    sort_fields = {
        "name": "agn_name",
        "pref": "pref",
        "inn": "inn",
        "email": "email",
        "phone": "phone",
        "use_nds": "use_nds",
        "sign_oferta": "sign_oferta",
        "id": "id",
    }
    filter_fields = {
        "agn_name": "agn_name",
        "inn": "inn",
        "pref": "pref",
        "email": "email",
        "phone": "phone",
    }
    default_sort = "name"

    def get_queryset(self):
        qs = super().get_queryset()
        archived_param = self.request.GET.get("archived")
        if archived_param == "1":
            qs = qs.filter(archived=True)
        elif archived_param == "0":
            qs = qs.filter(archived=False)

        search = (self.request.GET.get("q") or "").strip()
        if search:
            qs = qs.filter(
                models.Q(agn_name__icontains=search)
                | models.Q(inn__icontains=search)
                | models.Q(pref__icontains=search)
                | models.Q(email__icontains=search)
                | models.Q(phone__icontains=search)
            )

        filter_field = self.request.GET.get("filter_field")
        filter_value = (self.request.GET.get("filter_value") or "").strip()
        if filter_field in self.filter_fields and filter_value:
            lookup = self.filter_fields[filter_field]
            qs = qs.filter(**{f"{lookup}__icontains": filter_value})

        sort_key = self.request.GET.get("sort", self.default_sort)
        direction = self.request.GET.get("dir", "asc")
        sort_field = self.sort_fields.get(sort_key, self.sort_fields[self.default_sort])
        order_by = f"-{sort_field}" if direction == "desc" else sort_field
        return qs.order_by(order_by)

    def build_sort_url(self, field: str, direction: str) -> str:
        params = self.request.GET.copy()
        if "view" not in params:
            params["view"] = "table"
        params["sort"] = field
        params["dir"] = direction
        return f"?{params.urlencode()}"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        view = self.request.GET.get("view", "table")
        if view not in self.view_modes:
            view = "table"
        ctx["view_mode"] = view

        current_sort = self.request.GET.get("sort", self.default_sort)
        current_dir = "desc" if self.request.GET.get("dir") == "desc" else "asc"
        sort_info = {}
        for field in self.sort_fields:
            is_current = current_sort == field
            next_dir = "desc" if is_current and current_dir == "asc" else "asc"
            sort_info[field] = {
                "url": self.build_sort_url(field, next_dir),
                "active": is_current,
                "dir": current_dir if is_current else "",
                "next_dir": next_dir,
            }
        ctx["current_sort"] = current_sort
        ctx["current_dir"] = current_dir
        ctx["sort_info"] = sort_info
        ctx["filter_field"] = self.request.GET.get("filter_field") or ""
        ctx["filter_value"] = self.request.GET.get("filter_value") or ""
        ctx["archived_param"] = self.request.GET.get("archived") or ""
        return ctx


class AgencyFormMixin:
    model = Agency
    form_class = AgencyForm
    template_name = "client_cabinet/clients_form.html"
    success_url = "/client/"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["mode"] = getattr(self, "mode", "edit")
        ctx["title"] = getattr(self, "title", "Клиент")
        ctx["submit_label"] = getattr(self, "submit_label", "Сохранить")
        return ctx


class ClientCreateView(AgencyFormMixin, CreateView):
    mode = "create"
    title = "Создание клиента"
    submit_label = "Создать"


class ClientUpdateView(AgencyFormMixin, UpdateView):
    mode = "edit"
    title = "Редактирование клиента"
    submit_label = "Сохранить"


def archive_toggle(request, pk: int):
    agency = get_object_or_404(Agency, pk=pk)
    agency.archived = not agency.archived
    agency.save(update_fields=["archived"])
    next_url = request.GET.get("next") or reverse("client-list")
    return redirect(next_url)


def fetch_by_inn(request):
    inn = (request.GET.get("inn") or "").strip()
    if not inn:
        return JsonResponse({"ok": False, "error": "ИНН обязателен"}, status=400)
    try:
        data = fetch_party_by_inn(inn)
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=502)
    if not data:
        return JsonResponse({"ok": False, "error": "Данные не найдены"}, status=404)
    return JsonResponse({"ok": True, "data": data})


class ClientSKUListView(ListView):
    model = SKU
    paginate_by = 20
    template_name = "client_cabinet/client_sku_list.html"
    context_object_name = "items"
    view_modes = ("cards", "table")

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        qs = (
            SKU.objects.filter(deleted=False, agency=self.agency)
            .select_related("market", "agency")
            .prefetch_related("barcodes", "photos")
        )
        search = (self.request.GET.get("q") or "").strip()
        if search:
            qs = qs.filter(
                models.Q(sku_code__icontains=search)
                | models.Q(name__icontains=search)
                | models.Q(barcodes__value__icontains=search)
            ).distinct()
        return qs.order_by("-updated_at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        view = self.request.GET.get("view", "cards")
        if view not in self.view_modes:
            view = "cards"
        ctx["agency"] = self.agency
        ctx["search_value"] = self.request.GET.get("q", "")
        ctx["view_mode"] = view
        return ctx


class ClientSKUFormMixin:
    template_name = "client_cabinet/client_sku_form.html"

    def get_success_url(self):
        client_id = self.kwargs.get("pk")
        return f"/client/{client_id}/sku/"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["agency"] = getattr(self, "agency", None)
        return ctx


class ClientSKUCreateView(ClientSKUFormMixin, SKUCreateView):
    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        initial = super().get_initial()
        initial["agency"] = self.agency.id
        return initial

    def form_valid(self, form):
        form.instance.agency = self.agency
        return super().form_valid(form)


class ClientSKUUpdateView(ClientSKUFormMixin, SKUUpdateView):
    pk_url_kwarg = "sku_id"

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.filter(agency=self.agency)


class ClientSKUDuplicateView(ClientSKUFormMixin, SKUDuplicateView):
    pk_url_kwarg = "sku_id"

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        sku_id = self.kwargs.get("sku_id")
        orig = get_object_or_404(SKU, pk=sku_id, agency=self.agency)

        initial = {
            "name": orig.name,
            "brand": orig.brand,
            "agency": self.agency.id,
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

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.filter(agency=self.agency)


class ClientOrderFormView(TemplateView):
    template_name = "client_cabinet/client_order_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        # Пока без сохранения: имитация отправки.
        return self.get(request, submitted=True)

    def get(self, request, *args, **kwargs):
        submitted = kwargs.get("submitted") or request.GET.get("ok") == "1"
        ctx = self.get_context_data(submitted=submitted)
        return self.render_to_response(ctx)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["agency"] = self.agency
        ctx["submitted"] = kwargs.get("submitted", False)
        return ctx


class ClientPackingCreateView(TemplateView):
    template_name = "client_cabinet/client_packing_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        payload = {
            "email": request.POST.get("email"),
            "fio": request.POST.get("fio"),
            "org": request.POST.get("org"),
            "plan_date": request.POST.get("plan_date"),
            "marketplaces": request.POST.getlist("mp[]"),
            "mp_other": request.POST.get("mp_other"),
            "subject": request.POST.get("subject"),
            "total_qty": request.POST.get("total_qty"),
            "box_mode": request.POST.get("box_mode"),
            "box_mode_other": request.POST.get("box_mode_other"),
            "tasks": request.POST.getlist("tasks[]"),
            "tasks_other": request.POST.get("tasks_other"),
            "marking": request.POST.get("marking"),
            "marking_other": request.POST.get("marking_other"),
            "ship_as": request.POST.get("ship_as"),
            "ship_other": request.POST.get("ship_other"),
            "has_distribution": request.POST.get("has_distribution"),
            "comments": request.POST.get("comments"),
            "files_report": [f.name for f in request.FILES.getlist("files_report")],
            "files_distribution": [f.name for f in request.FILES.getlist("files_distribution")],
            "files_cz": [f.name for f in request.FILES.getlist("files_cz")],
        }
        order_id = f"pack-{uuid.uuid4().hex[:8]}"
        log_order_action(
            "create",
            order_id=order_id,
            order_type="packing",
            user=request.user if request.user.is_authenticated else None,
            agency=self.agency,
            description="Заявка на упаковку",
            payload=payload,
        )
        return self.get(request, submitted=True)

    def get(self, request, *args, **kwargs):
        submitted = kwargs.get("submitted") or request.GET.get("ok") == "1"
        ctx = self.get_context_data(submitted=submitted)
        return self.render_to_response(ctx)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["agency"] = self.agency
        ctx["submitted"] = kwargs.get("submitted", False)
        ctx["current_time"] = timezone.now()
        return ctx


class ClientReceivingCreateView(TemplateView):
    template_name = "client_cabinet/client_receiving_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        # TODO: сохранить заявку; пока только отображаем успех и пишем в аудит
        sku_codes = request.POST.getlist("sku_code[]")
        sku_ids = request.POST.getlist("sku_id[]")
        names = request.POST.getlist("item_name[]")
        qtys = request.POST.getlist("qty[]")
        position_comments = request.POST.getlist("position_comment[]")
        items = []
        row_count = max(len(sku_codes), len(qtys), len(names), len(position_comments), len(sku_ids))
        for idx in range(row_count):
            sku_code = sku_codes[idx] if idx < len(sku_codes) else ""
            qty = qtys[idx] if idx < len(qtys) else ""
            if not sku_code and not qty:
                continue
            items.append(
                {
                    "sku_id": sku_ids[idx] if idx < len(sku_ids) else "",
                    "sku_code": sku_code,
                    "name": names[idx] if idx < len(names) else "",
                    "qty": qty,
                    "comment": position_comments[idx] if idx < len(position_comments) else "",
                }
            )
        payload = {
            "eta_at": request.POST.get("eta_at"),
            "expected_boxes": request.POST.get("expected_boxes"),
            "comment": request.POST.get("comment"),
            "submit_action": request.POST.get("submit_action"),
            "items": items,
            "documents": [f.name for f in request.FILES.getlist("documents")],
        }
        action_label = "черновик" if request.POST.get("submit_action") == "draft" else "заявка"
        order_id = f"rcv-{uuid.uuid4().hex[:8]}"
        log_order_action(
            "create",
            order_id=order_id,
            order_type="receiving",
            user=request.user if request.user.is_authenticated else None,
            agency=self.agency,
            description=f"Заявка на приемку ({action_label})",
            payload=payload,
        )
        return self.get(request, submitted=True)

    def get(self, request, *args, **kwargs):
        submitted = kwargs.get("submitted") or request.GET.get("ok") == "1"
        ctx = self.get_context_data(submitted=submitted)
        return self.render_to_response(ctx)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["agency"] = self.agency
        ctx["submitted"] = kwargs.get("submitted", False)
        skus = (
            SKU.objects.filter(agency=self.agency, deleted=False)
            .prefetch_related("barcodes")
            .order_by("sku_code")
        )
        sku_options = []
        for sku in skus:
            barcodes = [barcode.value for barcode in sku.barcodes.all()]
            sku_options.append(
                {
                    "id": sku.id,
                    "code": sku.sku_code,
                    "name": sku.name,
                    "barcodes_joined": "|".join(barcodes),
                }
            )
        ctx["sku_options"] = sku_options
        ctx["current_time"] = timezone.localtime()
        ctx["min_past_hours"] = 0
        return ctx
