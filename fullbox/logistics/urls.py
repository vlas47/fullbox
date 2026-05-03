from django.urls import path

from . import views

app_name = "logistics"

urlpatterns = [
    path("", views.logistics_dashboard, name="dashboard"),
    path("trips/", views.logistics_trip_list, name="trip-list"),
    path("trips/<int:pk>/", views.logistics_trip_detail, name="trip-detail"),
    path("trips/<int:pk>/loading/", views.logistics_trip_loading, name="trip-loading"),
]
