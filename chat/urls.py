from django.urls import path

from . import views


urlpatterns = [
    path("settings/", views.developer_settings, name="settings"),
]
