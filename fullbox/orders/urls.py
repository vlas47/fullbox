from django.urls import path

from .views import OrdersDetailView, OrdersHomeView, ReceivingActView, PlacementActView

urlpatterns = [
    path('', OrdersHomeView.as_view(), name='orders-journal'),
    path('receiving/', OrdersHomeView.as_view(), {'tab': 'receiving'}, name='orders-receiving'),
    path('receiving/<str:order_id>/', OrdersDetailView.as_view(), name='orders-receiving-detail'),
    path('receiving/<str:order_id>/act/', ReceivingActView.as_view(), name='orders-receiving-act'),
    path('receiving/<str:order_id>/placement/', PlacementActView.as_view(), name='orders-receiving-placement'),
    path('processing/', OrdersHomeView.as_view(), {'tab': 'processing'}, name='orders-processing'),
    path('shipping/', OrdersHomeView.as_view(), {'tab': 'shipping'}, name='orders-shipping'),
    path('other/', OrdersHomeView.as_view(), {'tab': 'other'}, name='orders-other'),
]
