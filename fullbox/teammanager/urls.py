from django.urls import path

from .views import TeamManagerDashboard

urlpatterns = [
    path("", TeamManagerDashboard.as_view(), name="team-manager-dashboard"),
]
