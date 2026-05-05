from django.http import JsonResponse, HttpResponseRedirect
from django.views.generic import ListView, CreateView, UpdateView
from django.shortcuts import redirect

from .models import Agency, SKU
from .forms import SKUForm
from audit.models import log_sku_change
from .services import (
    get_catalog_mode,
    DEFAULT_SORT,
    FILTER_FIELDS,
    SORT_FIELDS,
    VIEW_MODES,
    build_sku_duplicate_initial,
    build_sku_form_context,
    build_sku_list_context,
    build_sku_list_queryset,
    build_temporary_nomenclature_queryset,
    build_sku_sort_url,
    clone_sku_to_admin,
    mark_sku_deleted,
    suggest_sku_payload,
)


class SKUListView(ListView):
    model = SKU
    paginate_by = 20
    template_name = 'sku/sku_list.html'
    context_object_name = 'items'
    view_modes = VIEW_MODES
    sort_fields = SORT_FIELDS
    filter_fields = FILTER_FIELDS
    default_sort = DEFAULT_SORT

    def get_queryset(self):
        if get_catalog_mode(self.request) == "temporary":
            from sklad.models import WarehouseTemporaryNomenclature

            return build_temporary_nomenclature_queryset(
                self.request,
                base_qs=WarehouseTemporaryNomenclature.objects.all(),
            )
        return build_sku_list_queryset(self.request, base_qs=super().get_queryset())

    def build_sort_url(self, field: str, direction: str) -> str:
        return build_sku_sort_url(self.request, field, direction)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(build_sku_list_context(self.request, items=ctx["items"]))
        return ctx


def suggest_sku(request):
    """Возвращает подсказки для поля поиска SKU."""
    return JsonResponse(
        suggest_sku_payload(
            request.GET.get("q"),
            catalog_mode=get_catalog_mode(request),
        )
    )


def clone_sku(request, pk: int):
    """Создает копию SKU и отправляет в админку для редактирования."""
    return redirect(clone_sku_to_admin(pk=pk, user=request.user))


class SKUFormMixin:
    model = SKU
    form_class = SKUForm
    template_name = "sku/sku_form.html"
    success_url = "/sku/"

    def form_valid(self, form):
        response = super().form_valid(form)
        action = getattr(self, "audit_action", "update")
        log_sku_change(
            action,
            self.object,
            user=self.request.user if self.request.user.is_authenticated else None,
            description=f"{action} через UI",
        )
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            build_sku_form_context(
                mode=getattr(self, "mode", "edit"),
                title=getattr(self, "title", "SKU"),
                submit_label=getattr(self, "submit_label", "Сохранить"),
            )
        )
        return ctx


class SKUCreateView(SKUFormMixin, CreateView):
    mode = "create"
    title = "Создание SKU"
    submit_label = "Создать"
    audit_action = "create"


class SKUUpdateView(SKUFormMixin, UpdateView):
    mode = "edit"
    title = "Редактирование SKU"
    submit_label = "Сохранить"
    audit_action = "update"


class SKUDuplicateView(SKUFormMixin, CreateView):
    mode = "duplicate"
    title = "Копирование SKU"
    submit_label = "Создать копию"
    audit_action = "clone"

    def get_initial(self):
        return build_sku_duplicate_initial(pk=self.kwargs["pk"])


def mark_deleted(request, pk: int):
    return mark_sku_deleted(pk=pk, user=request.user)
