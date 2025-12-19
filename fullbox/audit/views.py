from django.db import models
from django.views.generic import ListView

from .models import AuditEntry, get_sku_journal


class AuditListView(ListView):
    template_name = "audit/list.html"
    model = AuditEntry
    context_object_name = "entries"
    paginate_by = 25

    def get_queryset(self):
        qs = (
            AuditEntry.objects.select_related("sku", "user", "journal")
            .filter(journal=get_sku_journal())
            .order_by("-created_at")
        )
        action = self.request.GET.get("action")
        search = (self.request.GET.get("q") or "").strip()
        if action:
            qs = qs.filter(action=action)
        if search:
            qs = qs.filter(
                models.Q(sku__sku_code__icontains=search)
                | models.Q(sku__name__icontains=search)
                | models.Q(description__icontains=search)
                | models.Q(user__username__icontains=search)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["current_action"] = self.request.GET.get("action", "")
        ctx["search_value"] = self.request.GET.get("q", "")
        ctx["actions"] = dict(AuditEntry.ACTION_CHOICES)
        return ctx

# Create your views here.
