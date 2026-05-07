"""URL routes for the pages app."""

from __future__ import annotations

from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("healthz/", views.healthz, name="healthz"),
]
