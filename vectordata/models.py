from django.conf import settings
from django.db import models
from django.db.models import Q


class Region(models.Model):
    """An independently managed map region. Its coverage = source geometry buffered by range_km."""
    MODES = [('BORDER', 'Section of the live border layer'), ('GEOMETRY', 'Uploaded boundary line or area'),
             ('POINT', 'Centre point and radius')]

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    coverage_mode = models.CharField(max_length=10, choices=MODES)
    source_geometry = models.JSONField()  # GeoJSON geometry, WGS84
    range_km = models.FloatField(default=100)
    coverage_geometry = models.JSONField()  # computed (Multi)Polygon
    bbox = models.JSONField()  # [west, south, east, north]
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class VectorDataset(models.Model):
    """One processed version of a named dataset in a region. Files live under VECTOR_ROOT; only paths are stored."""
    STATUS = [('QUEUED', 'Waiting'), ('PROCESSING', 'Processing'), ('READY', 'Ready'), ('FAILED', 'Failed')]

    region = models.ForeignKey(Region, on_delete=models.PROTECT, related_name='datasets')
    dataset_name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    dataset_version = models.PositiveIntegerField()
    feature_types = models.JSONField()  # categories the Admin selected
    file_format = models.CharField(max_length=10)
    original_filename = models.CharField(max_length=200)
    source_file_path = models.CharField(max_length=300, blank=True)  # relative to VECTOR_ROOT
    processed_tile_path = models.CharField(max_length=300, blank=True)
    coordinate_reference_system = models.CharField(max_length=200, blank=True)

    coverage_geometry = models.JSONField(null=True, blank=True)  # region coverage ∩ data extent
    min_lon = models.FloatField(null=True, blank=True)
    min_lat = models.FloatField(null=True, blank=True)
    max_lon = models.FloatField(null=True, blank=True)
    max_lat = models.FloatField(null=True, blank=True)
    region_revision = models.CharField(max_length=40, blank=True)  # hash of the coverage used to clip

    min_zoom = models.PositiveSmallIntegerField(default=7)
    max_zoom = models.PositiveSmallIntegerField(default=14)
    processing_status = models.CharField(max_length=10, choices=STATUS, default='QUEUED')
    progress = models.CharField(max_length=200, blank=True)
    error_message = models.TextField(blank=True)
    stats = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=False)
    activate_when_ready = models.BooleanField(default=True)

    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['region__name', 'dataset_name', '-dataset_version']
        constraints = [
            models.UniqueConstraint(fields=['region', 'dataset_name', 'dataset_version'], name='vector_unique_version'),
            models.UniqueConstraint(fields=['region', 'dataset_name'], condition=Q(is_active=True), name='vector_one_active_version'),
        ]
        indexes = [models.Index(fields=['is_active', 'min_lon', 'max_lon'], name='vector_active_bbox')]

    def __str__(self):
        return f'{self.region} / {self.dataset_name} v{self.dataset_version}'
