from django.urls import path

from .views import dashboard

app_name = "ui"

urlpatterns = [path("", dashboard, name="dashboard")]
