"""REST API: public read access for the map, staff-only management."""
from __future__ import annotations

import hashlib
import json

from django.db import IntegrityError
from django.db.models import ProtectedError
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from battle import borders
from battle.api import IsStaff

from . import coverage, services
from .classify import CATEGORIES
from .errors import VectorError
from .models import Region, VectorDataset


def endpoint(methods, summary, perm=IsStaff):
    def wrap(fn):
        def guarded(request, *args, **kwargs):
            try:
                return fn(request, *args, **kwargs)
            except VectorError as e:
                return Response({'error': str(e)}, status=400)
        guarded.__name__ = guarded.__qualname__ = fn.__name__
        guarded.__doc__ = summary
        view = api_view(methods)(permission_classes([perm])(guarded))
        return extend_schema(tags=['vector data'], summary=summary, request=OpenApiTypes.OBJECT,
                             responses=OpenApiTypes.OBJECT)(view)
    return wrap


def _flag(value, default=False) -> bool:
    return default if value is None else str(value).lower() in ('1', 'true', 'yes', 'on')


def region_json(r: Region) -> dict:
    live = [dict(id=d.id, name=d.dataset_name, version=d.dataset_version)
            for d in r.datasets.all() if d.is_active]
    return dict(id=r.id, name=r.name, description=r.description, coverage_mode=r.coverage_mode, range_km=r.range_km,
                bbox=r.bbox, source_geometry=r.source_geometry, coverage_geometry=r.coverage_geometry,
                dataset_count=len(r.datasets.all()), active=live)


def dataset_json(d: VectorDataset) -> dict:
    stale = d.processing_status == 'READY' and d.region_revision != services.coverage_revision(d.region)
    return dict(id=d.id, dataset_name=d.dataset_name, region_id=d.region_id, region_name=d.region.name,
                description=d.description, dataset_version=d.dataset_version, feature_types=d.feature_types,
                file_format=d.file_format, original_filename=d.original_filename,
                coordinate_reference_system=d.coordinate_reference_system, source_file_path=d.source_file_path,
                processed_tile_path=d.processed_tile_path, processing_status=d.processing_status, progress=d.progress,
                error_message=d.error_message, stats=d.stats, is_active=d.is_active, coverage_outdated=stale,
                bbox=None if d.min_lon is None else [d.min_lon, d.min_lat, d.max_lon, d.max_lat],
                min_zoom=d.min_zoom, max_zoom=d.max_zoom,
                uploaded_by=d.uploaded_by.get_username() if d.uploaded_by else None,
                uploaded_at=d.uploaded_at.isoformat(), updated_at=d.updated_at.isoformat())


# ── public: what the map needs ───────────────────────────────────────────────

@endpoint(['GET'], 'Active vector datasets intersecting ?bbox=west,south,east,north (supports If-None-Match)',
          perm=permissions.AllowAny)
def active(request):
    try:
        w, s, e, n = (float(v) for v in request.query_params.get('bbox', '').split(','))
    except ValueError:
        raise VectorError('bbox=west,south,east,north is required')
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise VectorError('bbox is out of range')
    rows = [dict(id=d.id, name=d.dataset_name, region=d.region.name, version=d.dataset_version,
                 layers=sorted(d.stats.get('layers', {})), bbox=[d.min_lon, d.min_lat, d.max_lon, d.max_lat],
                 attribution='Map features © OpenStreetMap contributors (ODbL)' if d.file_format == 'OSM_PBF' else '',
                 min_zoom=d.min_zoom, max_zoom=d.max_zoom, tiles=f'/api/vectors/tiles/{d.id}/{{z}}/{{x}}/{{y}}.pbf')
            for d in services.active_datasets((w, s, e, n))]
    revision = hashlib.sha1(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:16]
    etag = f'"v{revision}"'
    if request.headers.get('If-None-Match') == etag:
        resp = HttpResponse(status=304)
    else:
        resp = Response(dict(revision=revision, categories=CATEGORIES, datasets=rows))
    resp['ETag'] = etag
    resp['Cache-Control'] = 'no-cache'
    return resp


@require_GET
def tile(request, did: int, z: int, x: int, y: int):
    """One MVT tile. A plain Django view so that panning the map is not rate limited like API calls."""
    d = VectorDataset.objects.filter(pk=did, processing_status='READY').only(
        'is_active', 'processed_tile_path', 'min_zoom', 'max_zoom').first()
    staff = request.user.is_authenticated and request.user.is_staff
    if not d or not (d.is_active or staff):
        raise Http404('no such vector dataset')
    if not (d.min_zoom <= z <= d.max_zoom and x < 2 ** z and y < 2 ** z):
        return HttpResponse(status=204)
    path = services.storage_root() / d.processed_tile_path / str(z) / str(x) / f'{y}.pbf'
    if not path.is_file():
        return HttpResponse(status=204)  # inside the pyramid but nothing to draw
    resp = FileResponse(open(path, 'rb'), content_type='application/vnd.mapbox-vector-tile')
    resp['Cache-Control'] = 'private, max-age=86400'  # a dataset id's tiles never change
    return resp


# ── staff: regions ───────────────────────────────────────────────────────────

def _region_geometry(data, files) -> tuple[str, dict]:
    mode = data.get('coverage_mode')
    if mode == 'BORDER':
        box = str(data.get('border_bbox', '')).split(',')
        return mode, coverage.line_source_from_borders(borders.live()['geojson'], box)
    if mode == 'POINT':
        try:
            lon, lat = float(data.get('lon')), float(data.get('lat'))
        except (TypeError, ValueError):
            raise VectorError('enter the centre longitude and latitude')
        return mode, coverage.circle_source(lon, lat)
    if mode == 'GEOMETRY':
        f = files.get('geometry_file')
        raw = data.get('geometry')
        if f:
            if f.size > 10 * 1024 ** 2:
                raise VectorError('the boundary file is larger than 10 MB')
            raw = f.read().decode('utf-8-sig', errors='replace')
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raise VectorError('the boundary is not valid GeoJSON')
        if not raw:
            raise VectorError('choose a GeoJSON boundary file')
        return mode, coverage.mapping_rounded(coverage.parse_geojson_geometry(raw))
    raise VectorError('choose how the coverage is defined')


def _range(data, default=100.0) -> float:
    try:
        return float(data.get('range_km', default))
    except (TypeError, ValueError):
        raise VectorError('range must be a number of kilometres')


@endpoint(['GET', 'POST'], 'List regions / create a region with configurable coverage')
def regions(request):
    if request.method == 'GET':
        return Response([region_json(r) for r in Region.objects.prefetch_related('datasets')])
    name = str(request.data.get('name', '')).strip()[:100]
    if not name:
        raise VectorError('enter a region name')
    mode, source = _region_geometry(request.data, request.FILES)
    km = _range(request.data)
    geom, bbox = coverage.build_coverage(source, km)
    try:
        r = Region.objects.create(name=name, description=str(request.data.get('description', ''))[:2000],
                                  coverage_mode=mode, source_geometry=source, range_km=km,
                                  coverage_geometry=geom, bbox=bbox)
    except IntegrityError:
        raise VectorError(f'a region called "{name}" already exists')
    return Response(region_json(r), status=201)


@endpoint(['PATCH', 'DELETE'], 'Update a region (name, description, range, coverage) or delete an empty region')
def region(request, rid: int):
    r = get_object_or_404(Region, pk=rid)
    if request.method == 'DELETE':
        try:
            r.delete()
        except ProtectedError:
            return Response({'error': 'delete the datasets of this region first'}, status=409)
        return Response(status=204)
    data = request.data
    if 'name' in data:
        r.name = str(data['name']).strip()[:100] or r.name
    if 'description' in data:
        r.description = str(data['description'])[:2000]
    if 'coverage_mode' in data or 'range_km' in data:
        if 'coverage_mode' in data:
            r.coverage_mode, r.source_geometry = _region_geometry(data, request.FILES)
        r.range_km = _range(data, r.range_km)
        r.coverage_geometry, r.bbox = coverage.build_coverage(r.source_geometry, r.range_km)
    try:
        r.save()
    except IntegrityError:
        raise VectorError('another region already has that name')
    return Response(region_json(r))


# ── staff: datasets ──────────────────────────────────────────────────────────

@endpoint(['GET', 'POST'], 'List all dataset versions / upload (multipart: region_id, dataset_name, description, '
                           'feature_types, files, activate, max_zoom)')
def datasets(request):
    if request.method == 'GET':
        services.fail_stale()
        qs = VectorDataset.objects.select_related('region', 'uploaded_by')
        return Response(dict(categories=CATEGORIES, max_upload_mb=services.config()['MAX_UPLOAD_MB'],
                             postgis=services.postgis_ready(), datasets=[dataset_json(d) for d in qs]))
    replaces = request.data.get('replaces')
    if replaces:  # new version of an existing dataset: region and name are inherited
        old = get_object_or_404(VectorDataset, pk=int(replaces) if str(replaces).isdigit() else 0)
        reg, name = old.region, old.dataset_name
    else:
        rid = str(request.data.get('region_id', ''))
        reg = Region.objects.filter(pk=int(rid)).first() if rid.isdigit() else None
        if not reg:
            raise VectorError('select a region')
        name = str(request.data.get('dataset_name', '')).strip()
        if VectorDataset.objects.filter(region=reg, dataset_name=name[:100]).exists():
            raise VectorError(f'"{name}" already exists in this region; use Replace on it to upload a new version')
    types = request.data.getlist('feature_types') if hasattr(request.data, 'getlist') else request.data.get('feature_types')
    zoom = str(request.data.get('max_zoom') or '')
    if zoom and not zoom.isdigit():
        raise VectorError('maximum zoom must be a whole number')
    ds = services.create_upload(reg, name, str(request.data.get('description', '')), types,
                                request.FILES.getlist('files'), request.user,
                                activate=_flag(request.data.get('activate'), True), max_zoom=int(zoom) if zoom else None)
    return Response(dataset_json(ds), status=202)


@endpoint(['PATCH', 'DELETE'], 'Edit the description of a dataset / delete an inactive version and its files')
def dataset(request, did: int):
    d = get_object_or_404(VectorDataset.objects.select_related('region'), pk=did)
    if request.method == 'DELETE':
        try:
            services.delete(d)
        except VectorError as e:
            return Response({'error': str(e)}, status=409)
        return Response(status=204)
    if 'description' in request.data:
        d.description = str(request.data['description'])[:2000]
        d.save(update_fields=['description', 'updated_at'])
    return Response(dataset_json(d))


@endpoint(['POST'], 'activate | deactivate | reprocess a dataset version')
def dataset_action(request, did: int, action: str):
    d = get_object_or_404(VectorDataset.objects.select_related('region'), pk=did)
    if action == 'activate':
        d = services.activate(d)
    elif action == 'deactivate':
        d = services.deactivate(d)
    elif action == 'reprocess':
        d = services.reprocess(d, request.user)
    else:
        raise Http404('unknown action')
    return Response(dataset_json(d))
