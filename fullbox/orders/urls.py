from django.urls import path

from .views import OrdersHomeView

urlpatterns = [
    path('', OrdersHomeView.as_view(), name='orders-journal'),
    path('receiving/', OrdersHomeView.as_view(), {'tab': 'receiving'}, name='orders-receiving'),
    path('processing/', OrdersHomeView.as_view(), {'tab': 'processing'}, name='orders-processing'),
    path('shipping/', OrdersHomeView.as_view(), {'tab': 'shipping'}, name='orders-shipping'),
    path('other/', OrdersHomeView.as_view(), {'tab': 'other'}, name='orders-other'),
]
