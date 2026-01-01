from django.views.generic import TemplateView

from employees.access import RoleRequiredMixin


class HeadManagerDashboard(RoleRequiredMixin, TemplateView):
    template_name = 'head_manager/dashboard.html'
    allowed_roles = ("head_manager",)
