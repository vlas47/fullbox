from django.views.generic import TemplateView


class HeadManagerDashboard(TemplateView):
    template_name = 'head_manager/dashboard.html'
