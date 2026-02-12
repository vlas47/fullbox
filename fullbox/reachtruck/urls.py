from django.urls import path

from .views import ReachtruckDashboardView, lookup_pallet_location, lookup_item_pallets

app_name = "reachtruck"

urlpatterns = [
    path("", ReachtruckDashboardView.as_view(), name="dashboard"),
    path("lookup/", lookup_pallet_location, name="lookup"),
    path("lookup-item/", lookup_item_pallets, name="lookup-item"),
]
