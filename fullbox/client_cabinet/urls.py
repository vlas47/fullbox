from django.urls import path

from .views import (
    ClientListView,
    ClientCreateView,
    ClientUpdateView,
    archive_toggle,
    dashboard,
    fetch_by_inn,
    ClientSKUListView,
    ClientSKUCreateView,
    ClientSKUUpdateView,
    ClientSKUDuplicateView,
    ClientOrderFormView,
    ClientReceivingCreateView,
    ClientPackingCreateView,
    receiving_redirect,
)

urlpatterns = [
    path('', ClientListView.as_view(), name='client-list'),
    path('dashboard/', dashboard, name='client-cabinet'),
    path('new/', ClientCreateView.as_view(), name='client-create'),
    path('<int:pk>/edit/', ClientUpdateView.as_view(), name='client-edit'),
    path('<int:pk>/archive/', archive_toggle, name='client-archive'),
    path('<int:pk>/sku/', ClientSKUListView.as_view(), name='client-sku-list'),
    path('<int:pk>/sku/new/', ClientSKUCreateView.as_view(), name='client-sku-create'),
    path('<int:pk>/sku/<int:sku_id>/edit/', ClientSKUUpdateView.as_view(), name='client-sku-edit'),
    path('<int:pk>/sku/<int:sku_id>/duplicate/', ClientSKUDuplicateView.as_view(), name='client-sku-duplicate'),
    path('<int:pk>/orders/new/', ClientOrderFormView.as_view(), name='client-order-new'),
    path('<int:pk>/receiving/new/', receiving_redirect, name='client-receiving-new'),
    path('<int:pk>/packing/new/', ClientPackingCreateView.as_view(), name='client-packing-new'),
    path('fetch-by-inn/', fetch_by_inn, name='client-fetch-inn'),
]
