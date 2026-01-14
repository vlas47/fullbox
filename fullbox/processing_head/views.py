from django.views.generic import TemplateView

from employees.access import RoleRequiredMixin


class ProcessingHeadDashboard(RoleRequiredMixin, TemplateView):
    template_name = "processing_head/dashboard.html"
    allowed_roles = ("processing_head",)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["role"] = "processing_head"
        context["title"] = "Руководитель обработки"
        return context
