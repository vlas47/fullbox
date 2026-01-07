from django.urls import path

from .views import AuditListView, ClientAuditListView, OrderAuditListView

urlpatterns = [
    path('', AuditListView.as_view(), name='audit-list'),
    path('orders/', OrderAuditListView.as_view(), name='audit-orders'),
    path('clients/', ClientAuditListView.as_view(), name='audit-clients'),
]
