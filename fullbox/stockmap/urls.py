from django.urls import path

from .views import StockMapRowView, StockMapView

app_name = "stockmap"

urlpatterns = [
    path("", StockMapView.as_view(), name="index"),
    path("os/<int:row>/", StockMapRowView.as_view(), name="os-row"),
]
