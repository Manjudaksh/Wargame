"""Dataset lifecycle: storage, upload, activation, replacement, deletion and spatial lookup."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import sys
import zipfile
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone
from shapely.geometry import box, shape

from .classify import CATEGORIES
from .errors import VectorError
from .models import Region, VectorDataset
from .readers import detect_format

log = logging.getLogger('battle.vectordata')
STALE_AFTER = timedelta(minutes=20)
SHP_PARTS = {'.shp', '.shx', '.dbf', '.prj', '.cpg'}


def config() -> dict:
    return settings.VECTOR_DATA


def storage_root() -> Path:
    return Path(config()['ROOT'])


def dataset_dir(ds: VectorDataset) -> Path:
    return storage_root() / f'dataset_{ds.pk}'


def coverage_revision(region: Region) -> str:
    return hashlib.sha1(json.dumps(region.coverage_geometry, sort_keys=True).encode()).hexdigest()


# ── upload ───────────────────────────────────────────────────────────────────

def _check_types(feature_types) -> list[str]:
    types = [t for t in dict.fromkeys(feature_types or [])]
    unknown = [t for t in types if t not in CATEGORIES]
    if unknown:
        raise VectorError(f'unknown feature type: {", ".join(unknown)}')
    if not types:
        raise VectorError('select at least one feature type the dataset contains')
    return types


def _plan_upload(files) -> tuple[str, str]:
    """Validate names and sizes before anything is stored → (stored filename, format)."""
    if not files:
        raise VectorError('choose a file to upload')
    limit = config()['MAX_UPLOAD_MB']
    if sum(f.size for f in files) > limit * 1024 ** 2:
        raise VectorError(f'the upload is larger than the {limit} MB limit')
    if any(f.size == 0 for f in files):
        raise VectorError('one of the files is empty')
    names = [Path(f.name.replace('\\', '/')).name for f in files]
    if len(files) == 1 and not names[0].lower().endswith('.shp'):
        fmt = detect_format(names[0])
        stored = {'GEOJSON': 'source.geojson', 'SHAPEFILE': 'source.zip', 'GPKG': 'source.gpkg', 'OSM_PBF': 'source.osm.pbf'}
        return stored[fmt], fmt
    suffixes = [Path(n).suffix.lower() for n in names]
    if not set(suffixes) <= SHP_PARTS or '.shp' not in suffixes:
        raise VectorError('several files are only accepted for a Shapefile: select the .shp, .shx, .dbf and .prj together')
    missing = {'.shx', '.dbf', '.prj'} - set(suffixes)
    if missing:
        raise VectorError(f'the Shapefile is missing {", ".join(sorted(missing))}')
    return 'source.zip', 'SHAPEFILE'


def _store(files, folder: Path, stored_name: str):
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / stored_name
    if len(files) == 1 and not files[0].name.lower().endswith('.shp'):
        with open(target, 'wb') as out:
            for chunk in files[0].chunks():
                out.write(chunk)
        return
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in files:
            with z.open(Path(f.name.replace('\\', '/')).name, 'w', force_zip64=True) as out:
                for chunk in f.chunks():
                    out.write(chunk)


def _new_row(region: Region, name: str, **fields) -> VectorDataset:
    with transaction.atomic():
        Region.objects.select_for_update().get(pk=region.pk)  # serialises version numbering
        last = VectorDataset.objects.filter(region=region, dataset_name=name).aggregate(v=Max('dataset_version'))['v']
        return VectorDataset.objects.create(region=region, dataset_name=name, dataset_version=(last or 0) + 1, **fields)


def create_upload(region: Region, name: str, description: str, feature_types, files, user,
                  activate: bool = True, max_zoom: int | None = None) -> VectorDataset:
    name = (name or '').strip()[:100]
    if not name:
        raise VectorError('enter a dataset name')
    types = _check_types(feature_types)
    stored, fmt = _plan_upload(files)
    zoom = int(max_zoom or config()['MAX_ZOOM'])
    if not 10 <= zoom <= 15:
        raise VectorError('maximum zoom must be between 10 and 15')
    ds = _new_row(region, name, description=(description or '')[:2000], feature_types=types, file_format=fmt,
                  original_filename=', '.join(Path(f.name).name for f in files)[:200], activate_when_ready=activate,
                  min_zoom=config()['MIN_ZOOM'], max_zoom=zoom,
                  uploaded_by=user if getattr(user, 'is_authenticated', False) else None)
    try:
        folder = dataset_dir(ds)
        shutil.rmtree(folder, ignore_errors=True)
        _store(files, folder / 'source', stored)
    except OSError as e:
        shutil.rmtree(dataset_dir(ds), ignore_errors=True)
        ds.delete()
        raise VectorError(f'the file could not be saved to storage ({e})')
    ds.source_file_path = (folder / 'source' / stored).relative_to(storage_root()).as_posix()
    ds.save(update_fields=['source_file_path', 'updated_at'])
    start_processing(ds)
    ds.refresh_from_db()
    return ds


def reprocess(old: VectorDataset, user) -> VectorDataset:
    """New version from the stored source, e.g. after the region coverage changed. The old one stays live meanwhile."""
    source = storage_root() / old.source_file_path
    if not old.source_file_path or not source.is_file():
        raise VectorError('the original source file is no longer in storage; upload it again')
    ds = _new_row(old.region, old.dataset_name, description=old.description, feature_types=old.feature_types,
                  file_format=old.file_format, original_filename=old.original_filename,
                  activate_when_ready=old.is_active or (old.processing_status == 'FAILED' and old.activate_when_ready),
                  min_zoom=old.min_zoom, max_zoom=old.max_zoom,
                  uploaded_by=user if getattr(user, 'is_authenticated', False) else None)
    target = dataset_dir(ds) / 'source' / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    ds.source_file_path = target.relative_to(storage_root()).as_posix()
    ds.save(update_fields=['source_file_path', 'updated_at'])
    start_processing(ds)
    ds.refresh_from_db()
    return ds


def start_processing(ds: VectorDataset):
    """GIS work runs in its own OS process so it never competes with the battle simulation for the GIL."""
    if config()['INLINE']:
        from .pipeline import process
        process(ds.pk)
        return
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        with open(dataset_dir(ds) / 'processing.log', 'ab') as logfile:
            subprocess.Popen([sys.executable, str(Path(settings.BASE_DIR) / 'manage.py'), 'process_vector_dataset', str(ds.pk)],
                             cwd=settings.BASE_DIR, stdin=subprocess.DEVNULL, stdout=logfile, stderr=logfile,
                             creationflags=flags, start_new_session=not flags)
    except OSError as e:
        VectorDataset.objects.filter(pk=ds.pk).update(processing_status='FAILED',
                                                      error_message=f'could not start the processing worker ({e})')


def fail_stale():
    """A worker that died (crash, reboot) must not leave a dataset 'processing' forever."""
    VectorDataset.objects.filter(processing_status__in=('QUEUED', 'PROCESSING'),
                                 updated_at__lt=timezone.now() - STALE_AFTER).update(
        processing_status='FAILED', progress='', updated_at=timezone.now(),
        error_message='processing stopped without finishing (server restart or crash). Use Reprocess to try again.')


# ── activation / deletion ────────────────────────────────────────────────────

def activate(ds: VectorDataset) -> VectorDataset:
    """Make this version live, atomically replacing the live version with the same name in the same region."""
    with transaction.atomic():
        Region.objects.select_for_update().get(pk=ds.region_id)
        ds.refresh_from_db()
        if ds.processing_status != 'READY':
            raise VectorError('only a successfully processed dataset can be activated')
        if not (storage_root() / ds.processed_tile_path).is_dir():
            raise VectorError('the tile files for this dataset are missing; use Reprocess')
        VectorDataset.objects.filter(region_id=ds.region_id, dataset_name=ds.dataset_name, is_active=True) \
            .exclude(pk=ds.pk).update(is_active=False, updated_at=timezone.now())
        ds.is_active = True
        ds.save(update_fields=['is_active', 'updated_at'])
    return ds


def activate_if_newest(ds: VectorDataset):
    """Automatic publish after processing; a slow older upload must not override a newer live one."""
    newer = VectorDataset.objects.filter(region_id=ds.region_id, dataset_name=ds.dataset_name, is_active=True,
                                         dataset_version__gt=ds.dataset_version)
    if not newer.exists():
        activate(ds)


def deactivate(ds: VectorDataset) -> VectorDataset:
    ds.is_active, ds.activate_when_ready = False, False
    ds.save(update_fields=['is_active', 'activate_when_ready', 'updated_at'])
    return ds


def delete(ds: VectorDataset):
    ds.refresh_from_db()
    if ds.is_active:
        raise VectorError('this dataset is live; deactivate it or activate another version first')
    if ds.processing_status == 'PROCESSING':
        raise VectorError('this dataset is still being processed')
    folder = dataset_dir(ds)
    ds.delete()
    shutil.rmtree(folder, ignore_errors=True)


# ── spatial lookup ───────────────────────────────────────────────────────────

def postgis_ready() -> bool:
    """True once `manage.py enable_postgis` has added the geometry column."""
    if connection.vendor != 'postgresql':
        return False
    with connection.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                    "AND table_name = %s AND column_name = 'coverage_geom'", [VectorDataset._meta.db_table])
        return cur.fetchone() is not None


def active_datasets(bbox: tuple[float, float, float, float]) -> list[VectorDataset]:
    """Live datasets whose coverage intersects bbox (west, south, east, north)."""
    w, s, e, n = bbox
    qs = VectorDataset.objects.select_related('region').filter(
        is_active=True, processing_status='READY', min_lon__lte=e, max_lon__gte=w, min_lat__lte=n, max_lat__gte=s
    ).order_by('region__name', 'dataset_name')
    if postgis_ready():
        return list(qs.extra(where=['ST_Intersects(coverage_geom, ST_MakeEnvelope(%s, %s, %s, %s, 4326))'], params=[w, s, e, n]))
    view = box(w, s, e, n)
    return [d for d in qs if shape(d.coverage_geometry).intersects(view)]
