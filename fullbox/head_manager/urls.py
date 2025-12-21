from django.urls import path
from .views import HeadManagerDashboard

urlpatterns = [
    path('', HeadManagerDashboard.as_view(), name='head-manager-dashboard'),
]
