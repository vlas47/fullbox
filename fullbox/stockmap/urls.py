from django.urls import path

from .views import StockMapPrView, StockMapRowView, StockMapView, StockMapVisualView

app_name = "stockmap"

urlpatterns = [
    path("", StockMapView.as_view(), name="index"),
    path("visual/", StockMapVisualView.as_view(), name="visual"),
    path("pr/", StockMapPrView.as_view(), name="pr-zone"),
    path("os/<int:row>/", StockMapRowView.as_view(), name="os-row"),
]
