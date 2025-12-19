from django.views.generic import TemplateView


class OrdersHomeView(TemplateView):
    template_name = 'orders/index.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['active_tab'] = kwargs.get('tab', 'journal')
        return ctx
