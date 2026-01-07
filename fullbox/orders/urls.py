from django.urls import path

from .views import (
    OrdersDetailView,
    OrdersHomeView,
    OrdersPackingView,
    PackingDetailView,
    ReceivingActView,
    PlacementActView,
    download_receiving_template,
    download_sku_upload_template,
    download_receiving_act_doc,
    download_receiving_act_mx1,
    print_receiving_act,
    print_receiving_act_mx1,
)

urlpatterns = [
    path('', OrdersHomeView.as_view(), name='orders-journal'),
    path('receiving/', OrdersHomeView.as_view(), {'tab': 'receiving'}, name='orders-receiving'),
    path('receiving/<str:order_id>/', OrdersDetailView.as_view(), name='orders-receiving-detail'),
    path('receiving/<str:order_id>/act/', ReceivingActView.as_view(), name='orders-receiving-act'),
    path('receiving/<str:order_id>/placement/', PlacementActView.as_view(), name='orders-receiving-placement'),
    path('receiving/<str:order_id>/act/document/', download_receiving_act_doc, name='orders-receiving-act-doc'),
    path('receiving/<str:order_id>/act/print/', print_receiving_act, name='orders-receiving-act-print'),
    path('receiving/<str:order_id>/act/mx1/', download_receiving_act_mx1, name='orders-receiving-act-mx1'),
    path('receiving/<str:order_id>/act/mx1/print/', print_receiving_act_mx1, name='orders-receiving-act-mx1-print'),
    path('packing/', OrdersPackingView.as_view(), {'tab': 'packing'}, name='orders-packing'),
    path('packing/<str:order_id>/', PackingDetailView.as_view(), name='orders-packing-detail'),
    path('processing/', OrdersHomeView.as_view(), {'tab': 'processing'}, name='orders-processing'),
    path('shipping/', OrdersHomeView.as_view(), {'tab': 'shipping'}, name='orders-shipping'),
    path('other/', OrdersHomeView.as_view(), {'tab': 'other'}, name='orders-other'),
    path('templates/receiving/', download_receiving_template, name='orders-template-receiving'),
    path('templates/sku-upload/', download_sku_upload_template, name='orders-template-sku-upload'),
]
