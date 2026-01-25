from django.urls import path

from .views import ReachtruckDashboardView

app_name = "reachtruck"

urlpatterns = [
    path("", ReachtruckDashboardView.as_view(), name="dashboard"),
]
