from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="sunpeaks_events"),
    path("state/", views.state, name="sunpeaks_events_state"),
    path("refresh/", views.refresh_now, name="sunpeaks_events_refresh"),
]
