from django.views.generic import TemplateView

from employees.access import RoleRequiredMixin


class ProcessingWorkerDashboard(RoleRequiredMixin, TemplateView):
    template_name = "processing_worker/dashboard.html"
    allowed_roles = ("processing_worker",)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["role"] = "processing_worker"
        context["title"] = "Обработчик"
        return context
