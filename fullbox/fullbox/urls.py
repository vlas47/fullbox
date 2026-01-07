"""
URL configuration for fullbox project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from django.urls import path
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView, TemplateView

from fullbox import views

urlpatterns = [
    path('favicon.ico', views.favicon, name='favicon'),
    path('', TemplateView.as_view(template_name='landing.html'), name='home'),
    path('dev/', TemplateView.as_view(template_name='developer.html'), name='dev-home'),
    path('login-menu/', views.login_menu, name='login-menu'),
    path('dev-login/<str:username>/', views.dev_login, name='dev-login'),
    path('login/', views.sign_in, name='login'),
    path('logout/', views.sign_out, name='logout'),
    path('cabinet/<str:role>/', views.role_cabinet, name='role-cabinet'),
    path('project-description/', views.project_description, name='project-description'),
    path('project-description/file/', views.project_description_file, name='project-description-file'),
    path('development-journal/', views.development_journal, name='development-journal'),
    path('development-journal/file/', views.development_journal_file, name='development-journal-file'),
    path('admin/audit/', RedirectView.as_view(pattern_name='admin:audit_auditjournal_changelist', permanent=False)),
    path('admin/', admin.site.urls),
    path('scanner-test/', TemplateView.as_view(template_name='scanner_test.html'), name='scanner-test'),
    path('scanner-settings/', TemplateView.as_view(template_name='scanner_settings.html'), name='scanner-settings'),
    path('scanner/ims-2290hd/', TemplateView.as_view(template_name='scanner_ims_2290hd.html'), name='scanner-ims-2290hd'),
    path('orders/', include('orders.urls')),
    path('head-manager/', include('head_manager.urls')),
    path('client/', include('client_cabinet.urls')),
    path('sku/', include('sku.urls')),
    path('audit/', include('audit.urls')),
    path('todo/', include('todo.urls')),
    path('employees/', include('employees.urls')),
    path('market-sync/', include('market_sync.urls')),
    path('team-manager/', include('teammanager.urls')),
    path('sklad/', include('sklad.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
