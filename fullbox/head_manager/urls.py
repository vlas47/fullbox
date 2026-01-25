from django.urls import path
from .views import HeadManagerDashboard, MarketplaceWarehousesView, MarketplaceWarehousesSyncView

urlpatterns = [
    path('', HeadManagerDashboard.as_view(), name='head-manager-dashboard'),
    path('marketplace-warehouses/', MarketplaceWarehousesView.as_view(), name='head-manager-marketplace-warehouses'),
    path(
        'marketplace-warehouses/sync/',
        MarketplaceWarehousesSyncView.as_view(),
        name='head-manager-marketplace-warehouses-sync',
    ),
]
