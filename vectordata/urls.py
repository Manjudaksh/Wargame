from django.urls import path

from . import api

urlpatterns = [
    path('active/', api.active),
    path('tiles/<int:did>/<int:z>/<int:x>/<int:y>.pbf', api.tile),
    path('regions/', api.regions),
    path('regions/<int:rid>/', api.region),
    path('datasets/', api.datasets),
    path('datasets/<int:did>/', api.dataset),
    path('datasets/<int:did>/<str:action>/', api.dataset_action),
]
