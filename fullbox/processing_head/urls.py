from django.urls import path

from .views import ProcessingHeadDashboard

urlpatterns = [
    path("", ProcessingHeadDashboard.as_view(), name="processing-head-dashboard"),
]
