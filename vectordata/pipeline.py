"""Processing workflow for one uploaded dataset: validate → CRS → classify → clip to coverage → tiles → publish."""
from __future__ import annotations

import logging
import shutil
import time
from collections import Counter

import numpy as np
import shapely
from django.utils import timezone
from pyproj import Transformer
from shapely.geometry import box, shape

from . import services
from .classify import AREAS, CATEGORIES, LINES, POINTS, classify, min_zoom
from .coverage import WGS84, mapping_rounded, reproject
from .errors import VectorError
from .models import VectorDataset
from .readers import READERS
from .tiler import TileFeature, to_unit_mercator, write_pyramid

log = logging.getLogger('battle.vectordata')
MAX_KEPT_FEATURES = 1_000_000
GEOM_KIND = {0: POINTS, 1: LINES, 2: AREAS}


class Heartbeat:
    """Writes progress for the Admin page; updated_at also shows the worker is alive."""

    def __init__(self, pk: int):
        self.pk, self.last = pk, 0.0

    def __call__(self, text: str, force: bool = False):
        if force or time.monotonic() - self.last > 3:
            self.last = time.monotonic()
            VectorDataset.objects.filter(pk=self.pk).update(progress=text[:200], updated_at=timezone.now())


def _clean(geom):
    """2-D, finite, valid geometry or None."""
    if geom is None or geom.is_empty:
        return None
    geom = shapely.force_2d(geom)
    if not np.isfinite(shapely.get_coordinates(geom)).all():
        return None
    if not geom.is_valid:
        dim = shapely.get_dimensions(geom)
        geom = shapely.make_valid(geom)
        if geom.geom_type == 'GeometryCollection':
            geom = shapely.union_all([g for g in geom.geoms if shapely.get_dimensions(g) == dim])
    return None if geom.is_empty else geom


def collect(ds: VectorDataset, source, workdir, beat):
    """Read, reproject, classify and clip. Returns (tile features in lon/lat, report)."""
    cover = shape(ds.region.coverage_geometry)
    shapely.prepare(cover)
    cover_box = box(*cover.bounds)
    selected = set(ds.feature_types)
    transforms, crs_names = {}, []
    out, counts = [], Counter()
    total = outside = unmatched = invalid = 0

    for feat in READERS[ds.file_format](source, workdir):
        total += 1
        beat(f'Reading: {total:,} features, {len(out):,} kept')
        if feat.crs not in transforms:
            wgs = feat.crs.equals(WGS84, ignore_axis_order=True) or feat.crs.to_string() == 'OGC:CRS84'
            transforms[feat.crs] = None if wgs else Transformer.from_crs(feat.crs, WGS84, always_xy=True)
            epsg = feat.crs.to_epsg()
            crs_names.append(f'EPSG:{epsg}' if epsg else feat.crs.name)
        geom = _clean(feat.geometry)
        if geom is None:
            invalid += 1
            continue
        if transforms[feat.crs] is not None:
            geom = _clean(reproject(geom, transforms[feat.crs]))
            if geom is None:
                invalid += 1
                continue
        w, s, e, n = geom.bounds
        if w < -180 or e > 180 or s < -90 or n > 90:
            raise VectorError(f'coordinates ({w:.0f}, {s:.0f}) are not longitude/latitude: the file looks projected but '
                              f'declares {crs_names[-1]}. Re-export it with the correct coordinate reference system.')
        kinds = classify(feat.props, feat.layer, GEOM_KIND[int(shapely.get_dimensions(geom))], selected)
        if not kinds:
            unmatched += 1
            continue
        if not geom.intersects(cover_box) or not cover.intersects(geom):
            outside += 1
            continue
        name = str(feat.props.get('name:en') or feat.props.get('name') or feat.props.get('NAME') or '')[:80]
        for category, cls in kinds:
            g = geom
            if category == 'bridges':  # drawn as a marker
                g = g.interpolate(0.5, normalized=True) if shapely.get_dimensions(g) == 1 else g.representative_point()
            elif category == 'boundaries' and shapely.get_dimensions(g) == 2:
                g = g.boundary
            if not cover.contains(g):
                dim = shapely.get_dimensions(g)
                g = g.intersection(cover)
                if g.geom_type == 'GeometryCollection':
                    g = shapely.union_all([p for p in g.geoms if shapely.get_dimensions(p) == dim])
                if g.is_empty or shapely.get_dimensions(g) != dim:
                    continue
            props = {'class': cls}
            if name:
                props['name'] = name
            out.append(TileFeature(g, category, props, min_zoom(category, cls)))
            counts[category] += 1
        if len(out) > MAX_KEPT_FEATURES:
            raise VectorError(f'more than {MAX_KEPT_FEATURES:,} features fall inside the coverage; split the dataset by feature type')

    if not total:
        raise VectorError('the file contains no features')
    if not out:
        why = []
        if outside:
            why.append(f'{outside:,} lie outside the coverage of region "{ds.region.name}"')
        if unmatched:
            why.append(f'{unmatched:,} do not match the selected feature types')
        if invalid:
            why.append(f'{invalid:,} have invalid geometry')
        raise VectorError(f'nothing to display: of {total:,} features, ' + ('; '.join(why) or 'none could be used'))

    missing = [CATEGORIES[c] for c in ds.feature_types if not counts[c]]
    warnings = [f'No features found for: {", ".join(missing)}'] if missing else []
    if invalid:
        warnings.append(f'{invalid:,} features skipped because of invalid geometry')
    report = dict(source_features=total, kept=len(out), outside_coverage=outside, unmatched=unmatched, invalid=invalid,
                  layers=dict(counts), warnings=warnings, crs=', '.join(dict.fromkeys(crs_names)))
    return out, report


def process(pk: int) -> bool:
    """Run the workflow. The previous active version is only replaced after everything succeeded."""
    claimed = VectorDataset.objects.filter(pk=pk, processing_status='QUEUED').update(
        processing_status='PROCESSING', progress='Starting', updated_at=timezone.now())
    if not claimed:
        return False
    ds = VectorDataset.objects.select_related('region').get(pk=pk)
    folder = services.dataset_dir(ds)
    work, building, final = folder / 'work', folder / 'tiles.building', folder / 'tiles'
    beat = Heartbeat(pk)
    try:
        for d in (work, building, final):
            shutil.rmtree(d, ignore_errors=True)
        work.mkdir(parents=True)
        source = services.storage_root() / ds.source_file_path
        if not source.is_file():
            raise VectorError('the uploaded source file is missing from storage')
        features, report = collect(ds, source, work, beat)

        beat('Preparing tiles', force=True)
        w, s, e, n = shapely.total_bounds(np.array([f.geometry for f in features], dtype=object))
        # extent actually served: data bounds (padded so a single point still has area) within the region coverage
        extent = shape(ds.region.coverage_geometry).intersection(box(w - 1e-4, s - 1e-4, e + 1e-4, n + 1e-4))
        merc = [f._replace(geometry=to_unit_mercator(f.geometry)) for f in features]
        tile_stats = write_pyramid(merc, building, ds.min_zoom, ds.max_zoom,
                                   progress=lambda t: beat(f'Writing tiles: {t:,}'))
        if not tile_stats['tiles']:
            raise VectorError('no tiles were produced: the features are too small to show at the chosen zoom levels')
        building.rename(final)

        b = extent.bounds
        ds.coverage_geometry = mapping_rounded(extent)
        ds.min_lon, ds.min_lat, ds.max_lon, ds.max_lat = (round(v, 5) for v in b)
        ds.processed_tile_path = final.relative_to(services.storage_root()).as_posix()
        ds.coordinate_reference_system = report['crs'][:200]
        ds.region_revision = services.coverage_revision(ds.region)
        ds.stats = dict(report, **tile_stats)
        ds.processing_status, ds.progress, ds.error_message = 'READY', '', ''
        # only the fields the worker owns: the Admin may edit or deactivate the row while this runs
        ds.save(update_fields=['coverage_geometry', 'min_lon', 'min_lat', 'max_lon', 'max_lat', 'processed_tile_path',
                               'coordinate_reference_system', 'region_revision', 'stats', 'processing_status',
                               'progress', 'error_message', 'updated_at'])
        ds.refresh_from_db()
        if ds.activate_when_ready:
            services.activate_if_newest(ds)
        return True
    except Exception as e:  # every failure must be visible to the Admin
        if not isinstance(e, VectorError):
            log.exception('vector dataset %s failed', pk)
        message = str(e) if isinstance(e, VectorError) else f'unexpected processing error: {type(e).__name__}: {e}'
        shutil.rmtree(building, ignore_errors=True)
        shutil.rmtree(final, ignore_errors=True)
        VectorDataset.objects.filter(pk=pk).update(processing_status='FAILED', error_message=message[:2000], progress='',
                                                   processed_tile_path='', is_active=False, updated_at=timezone.now())
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)
