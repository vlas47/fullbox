from django.db import models
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import ListView, CreateView, UpdateView, TemplateView, FormView
from django.http import JsonResponse

from sku.models import Agency, SKU
from sku.views import SKUCreateView, SKUUpdateView, SKUDuplicateView
from .forms import AgencyForm
from .services import fetch_party_by_inn


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
        initial = super().get_initial()
        initial["agency"] = self.agency.id
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


class ClientReceivingCreateView(TemplateView):
    template_name = "client_cabinet/client_receiving_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        # TODO: сохранить заявку; пока только отображаем успех
        return self.get(request, submitted=True)

    def get(self, request, *args, **kwargs):
        submitted = kwargs.get("submitted") or request.GET.get("ok") == "1"
        ctx = self.get_context_data(submitted=submitted)
        return self.render_to_response(ctx)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["agency"] = self.agency
        ctx["submitted"] = kwargs.get("submitted", False)
        ctx["skus"] = SKU.objects.filter(agency=self.agency, deleted=False).order_by("sku_code")
        return ctx
