"""
URL configuration for fullbox project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView
from django.views.generic import TemplateView
from fullbox import views

urlpatterns = [
    path('', TemplateView.as_view(template_name='landing.html'), name='home'),
    path('dev/', TemplateView.as_view(template_name='developer.html'), name='dev-home'),
    path('login-menu/', views.login_menu, name='login-menu'),
    path('dev-login/<str:username>/', views.dev_login, name='dev-login'),
    path('cabinet/<str:role>/', views.role_cabinet, name='role-cabinet'),
    path('admin/audit/', RedirectView.as_view(pattern_name='admin:audit_auditjournal_changelist', permanent=False)),
    path('admin/', admin.site.urls),
    path('scanner-test/', TemplateView.as_view(template_name='scanner_test.html'), name='scanner-test'),
    path('orders/', include('orders.urls')),
    path('head-manager/', include('head_manager.urls')),
    path('client/', include('client_cabinet.urls')),
    path('sku/', include('sku.urls')),
    path('audit/', include('audit.urls')),
]
