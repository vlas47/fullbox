from django.urls import path

from .views import (
    ProcessingDetailView,
    ProcessingHomeView,
    ProcessingStockPickerView,
    delete_processing_draft,
)

app_name = "processing_app"

urlpatterns = [
    path("", ProcessingHomeView.as_view(), name="processing-home"),
    path("stock/", ProcessingStockPickerView.as_view(), name="processing-stock-picker"),
    path("draft/<str:order_id>/delete/", delete_processing_draft, name="processing-draft-delete"),
    path("<str:order_id>/", ProcessingDetailView.as_view(), name="processing-detail"),
]
