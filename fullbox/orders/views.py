import uuid

from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import TemplateView

from audit.models import log_order_action
from sku.models import Agency, SKU


class OrdersHomeView(TemplateView):
    template_name = 'orders/index.html'

    def get(self, request, *args, **kwargs):
        submitted = kwargs.get("submitted") or request.GET.get("ok") == "1"
        error = kwargs.get("error")
        ctx = self.get_context_data(submitted=submitted, error=error, **kwargs)
        return self.render_to_response(ctx)

    def post(self, request, *args, **kwargs):
        active_tab = kwargs.get("tab", "journal")
        if active_tab != "receiving":
            return redirect("/orders/")

        agency_id = request.POST.get("agency_id")
        agency = Agency.objects.filter(pk=agency_id).first()
        if not agency:
            return self.get(request, error="Выберите клиента.")

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
            agency=agency,
            description=f"Заявка на приемку ({action_label})",
            payload=payload,
        )
        return redirect(f"/orders/receiving/?client={agency.id}&ok=1")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        active_tab = kwargs.get("tab", "journal")
        ctx["active_tab"] = active_tab
        ctx["submitted"] = kwargs.get("submitted", False)
        ctx["error"] = kwargs.get("error")

        if active_tab == "receiving":
            client_id = self.request.GET.get("client")
            agency = Agency.objects.filter(pk=client_id).first() if client_id else None
            ctx["agency"] = agency
            ctx["agencies"] = Agency.objects.order_by("agn_name")
            ctx["current_time"] = timezone.localtime()
            ctx["min_past_hours"] = 0

            sku_options = []
            if agency:
                skus = (
                    SKU.objects.filter(agency=agency, deleted=False)
                    .prefetch_related("barcodes")
                    .order_by("sku_code")
                )
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
        return ctx
