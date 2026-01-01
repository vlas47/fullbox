from django.views.generic import TemplateView

from employees.access import RoleRequiredMixin


class TeamManagerDashboard(RoleRequiredMixin, TemplateView):
    template_name = "teammanager/dashboard.html"
    allowed_roles = ("manager",)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["role"] = "manager"
        ctx["title"] = "Team Manager"
        return ctx
