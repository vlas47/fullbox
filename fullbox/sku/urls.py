from django.urls import path

from .views import SKUListView, suggest_sku, clone_sku

urlpatterns = [
    path('', SKUListView.as_view(), name='sku-list'),
    path('suggest/', suggest_sku, name='sku-suggest'),
    path('clone/<int:pk>/', clone_sku, name='sku-clone'),
]
