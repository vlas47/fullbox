from django.urls import path

from .views import AuditListView, OrderAuditListView

urlpatterns = [
    path('', AuditListView.as_view(), name='audit-list'),
    path('orders/', OrderAuditListView.as_view(), name='audit-orders'),
]
