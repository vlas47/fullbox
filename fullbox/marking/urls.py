from django.urls import path

from .views import (
    processing_marking_summary,
    processing_marking_scan,
    processing_marking_import,
)

app_name = "marking"

urlpatterns = [
    path("processing/<str:order_id>/summary/", processing_marking_summary, name="processing-summary"),
    path("processing/<str:order_id>/scan/", processing_marking_scan, name="processing-scan"),
    path("processing/<str:order_id>/import/", processing_marking_import, name="processing-import"),
]
