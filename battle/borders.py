"""Border vector data: validation, normalisation and the built-in fallback layer.

Accepted upload: GeoJSON (FeatureCollection | Feature | bare geometry) in WGS84 lon/lat with LineString,
MultiLineString, Polygon or MultiPolygon geometry. The normalised result is a plain FeatureCollection whose
features carry only {kind, name}; the map paints it as-is."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import BorderLayer

MAX_BYTES = 25 * 1024 * 1024
MAX_FEATURES = 50_000
MAX_VERTICES = 1_500_000
RANGE_KM = 100.0
RANGE_TOLERANCE_KM = 5.0
# Broad India extent (incl. Andaman & Nicobar, Arunachal, Ladakh); data must overlap it.
INDIA_BOX = (66.0, 5.0, 99.0, 38.5)
LINE_TYPES = {'LineString', 'MultiLineString'}
AREA_TYPES = {'Polygon', 'MultiPolygon'}
STATIC_FALLBACK = Path(settings.BASE_DIR) / 'webapp' / 'static' / 'data' / 'borders.geojson'


class BorderError(ValueError):
    def __init__(self, errors):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__('; '.join(self.errors))


# ── parsing / validation ─────────────────────────────────────────────────────

def _pos(p, errors, where):
    if not isinstance(p, (list, tuple)) or len(p) < 2 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p[:2]):
        errors.append(f'{where}: a coordinate must be [longitude, latitude]')
        return None
    lon, lat = float(p[0]), float(p[1])
    if not (math.isfinite(lon) and math.isfinite(lat)):
        errors.append(f'{where}: coordinate is not a finite number')
        return None
    if abs(lon) > 180 or abs(lat) > 90:
        errors.append(f'{where}: coordinate {lon:g}, {lat:g} is outside lon/lat range; the file looks projected, re-export it in WGS84 (EPSG:4326)')
        return None
    return [round(lon, 5), round(lat, 5)]


def _line(coords, errors, where):
    if not isinstance(coords, list):
        errors.append(f'{where}: coordinates must be a list')
        return None
    pts = [q for q in (_pos(p, errors, where) for p in coords) if q]
    out = [pts[0]] if pts else []
    for q in pts[1:]:
        if q != out[-1]:
            out.append(q)
    return out if len(out) >= 2 else None


def _ring(coords, errors, where):
    r = _line(coords, errors, where)
    if r is None:
        return None
    if r[0] != r[-1]:
        r.append(r[0])
    return r if len(r) >= 4 else None


def _geometries(g, errors, where):
    """Yield (type, coordinates) for every supported part of a geometry."""
    if not isinstance(g, dict) or 'type' not in g:
        errors.append(f'{where}: missing geometry')
        return
    t = g['type']
    if t == 'GeometryCollection':
        for k, sub in enumerate(g.get('geometries') or []):
            yield from _geometries(sub, errors, f'{where}[{k}]')
        return
    if t in ('Point', 'MultiPoint'):
        errors.append(f'{where}: {t} is not supported, use line or polygon geometry')
        return
    if t not in LINE_TYPES | AREA_TYPES:
        errors.append(f'{where}: unsupported geometry type "{t}"')
        return
    c = g.get('coordinates')
    if t == 'LineString':
        l = _line(c, errors, where)
        if l:
            yield 'LineString', l
    elif t == 'MultiLineString':
        for part in c or []:
            l = _line(part, errors, where)
            if l:
                yield 'LineString', l
    elif t == 'Polygon':
        rings = [r for r in (_ring(x, errors, where) for x in c or []) if r]
        if rings:
            yield 'Polygon', rings
    else:
        for poly in c or []:
            rings = [r for r in (_ring(x, errors, where) for x in poly or []) if r]
            if rings:
                yield 'Polygon', rings


def _kind(props: dict, gtype: str) -> str:
    text = ' '.join(str(props.get(k, '')) for k in ('kind', 'type', 'name', 'NAME', 'featurecla', 'FEATURECLA')).lower()
    if re.search(r'line of (actual )?control|(loc|lac|ajk)', text):
        return 'loc'
    return 'area' if gtype == 'Polygon' else 'boundary'


def _vertices(features):
    for f in features:
        g = f['geometry']
        rings = [g['coordinates']] if g['type'] == 'LineString' else g['coordinates']
        for r in rings:
            yield from r


def validate(raw: bytes) -> tuple[dict, dict]:
    """Return (normalised FeatureCollection, report). Raises BorderError with every problem found."""
    if not raw:
        raise BorderError('the file is empty')
    if len(raw) > MAX_BYTES:
        raise BorderError(f'file is {len(raw) / 1048576:.1f} MB; the limit is {MAX_BYTES // 1048576} MB')
    try:
        data = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BorderError(f'not valid UTF-8 JSON/GeoJSON ({e})')
    if not isinstance(data, dict):
        raise BorderError('top level must be a GeoJSON object')
    crs = (((data.get('crs') or {}).get('properties') or {}).get('name') or '')
    if crs and not any(s in crs.upper() for s in ('4326', 'CRS84', 'WGS84')):
        raise BorderError(f'coordinate system "{crs}" is not WGS84; re-export as EPSG:4326 lon/lat')

    t = data.get('type')
    if t == 'FeatureCollection':
        raw_feats = data.get('features')
        if not isinstance(raw_feats, list):
            raise BorderError('FeatureCollection has no "features" list')
    elif t == 'Feature':
        raw_feats = [data]
    elif t in LINE_TYPES | AREA_TYPES | {'GeometryCollection'}:
        raw_feats = [{'type': 'Feature', 'properties': {}, 'geometry': data}]
    else:
        raise BorderError('not GeoJSON: expected FeatureCollection, Feature or a geometry (Shapefile/KML must be converted to GeoJSON first)')
    if not raw_feats:
        raise BorderError('the file contains no features')
    if len(raw_feats) > MAX_FEATURES:
        raise BorderError(f'{len(raw_feats)} features; the limit is {MAX_FEATURES}')

    errors: list[str] = []
    feats = []
    for i, f in enumerate(raw_feats):
        if not isinstance(f, dict) or f.get('type') != 'Feature':
            errors.append(f'feature {i}: not a GeoJSON Feature')
            continue
        props = f.get('properties') if isinstance(f.get('properties'), dict) else {}
        name = str(props.get('name') or props.get('NAME') or '')[:80]
        for gtype, coords in _geometries(f.get('geometry'), errors, f'feature {i}'):
            feats.append({'type': 'Feature', 'properties': {'kind': _kind(props, gtype), 'name': name},
                          'geometry': {'type': gtype, 'coordinates': coords}})
        if len(errors) >= 12:
            errors.append('… more problems omitted')
            break
    if errors:
        raise BorderError(dict.fromkeys(errors))  # de-duplicated, order kept
    if not feats:
        raise BorderError('no usable line or polygon geometry found')

    pts = np.array(list(_vertices(feats)), dtype=np.float64)
    if len(pts) > MAX_VERTICES:
        raise BorderError(f'{len(pts)} vertices; the limit is {MAX_VERTICES}')
    bbox = [round(float(pts[:, 0].min()), 4), round(float(pts[:, 1].min()), 4), round(float(pts[:, 0].max()), 4), round(float(pts[:, 1].max()), 4)]
    inside = ((pts[:, 0] >= INDIA_BOX[0]) & (pts[:, 0] <= INDIA_BOX[2]) & (pts[:, 1] >= INDIA_BOX[1]) & (pts[:, 1] <= INDIA_BOX[3])).mean()
    if inside < 0.5:
        raise BorderError(f'only {inside * 100:.0f}% of the data lies over India ({INDIA_BOX[0]}–{INDIA_BOX[2]}°E, {INDIA_BOX[1]}–{INDIA_BOX[3]}°N); '
                          'check the file is in lon/lat order and covers the Indian border')

    warnings = []
    if inside < 0.98:
        warnings.append(f'{(1 - inside) * 100:.0f}% of the vertices fall outside the Indian region')
    far = range_check(pts)
    if far is not None and far > 0.10:
        warnings.append(f'{far * 100:.0f}% of the vertices are more than {RANGE_KM:.0f} km from the reference border; '
                        f'the layer is meant to cover a {RANGE_KM:.0f} km range')
    report = dict(warnings=warnings, outside_range_pct=None if far is None else round(far * 100, 1),
                  kinds={k: sum(1 for f in feats if f['properties']['kind'] == k) for k in ('boundary', 'loc', 'area')})
    fc = {'type': 'FeatureCollection', 'range_km': RANGE_KM, 'features': feats}
    return fc, dict(report, feature_count=len(feats), vertex_count=int(len(pts)), bbox=bbox)


# ── 100 km range check against the reference border ──────────────────────────

def _reference_segments():
    try:
        d = json.loads(STATIC_FALLBACK.read_text(encoding='utf8'))
    except (OSError, ValueError):
        return None
    segs = []
    for f in d.get('features', []):
        c = f['geometry']['coordinates']
        for a, b in zip(c[:-1], c[1:]):
            segs.append((a[0], a[1], b[0], b[1]))
    return np.array(segs, dtype=np.float64) if segs else None


_REF = None


def range_check(pts: np.ndarray) -> float | None:
    """Fraction of (sampled) vertices farther than RANGE_KM from the reference border; None if unknown."""
    global _REF
    if _REF is None:
        _REF = _reference_segments()
    if _REF is None or not len(pts):
        return None
    if len(pts) > 4000:
        pts = pts[np.linspace(0, len(pts) - 1, 4000).astype(int)]
    lat0 = math.radians(float(pts[:, 1].mean()))
    kx, ky = 111.32 * math.cos(lat0), 110.57
    ax, ay, bx, by = (_REF[:, 0] * kx, _REF[:, 1] * ky, _REF[:, 2] * kx, _REF[:, 3] * ky)
    dx, dy = bx - ax, by - ay
    l2 = np.maximum(dx * dx + dy * dy, 1e-12)
    px, py = pts[:, 0] * kx, pts[:, 1] * ky
    best = np.full(len(pts), np.inf)
    for s in range(0, len(pts), 500):
        x, y = px[s:s + 500, None], py[s:s + 500, None]
        t = np.clip(((x - ax) * dx + (y - ay) * dy) / l2, 0, 1)
        d = np.hypot(x - (ax + t * dx), y - (ay + t * dy)).min(axis=1)
        best[s:s + 500] = d
    return float((best > RANGE_KM + RANGE_TOLERANCE_KM).mean())


# ── persistence ──────────────────────────────────────────────────────────────

def create_draft(raw: bytes, filename: str, user, note: str = '') -> BorderLayer:
    fc, rep = validate(raw)
    with transaction.atomic():
        last = BorderLayer.objects.order_by('-version').first()
        return BorderLayer.objects.create(
            version=(last.version + 1) if last else 1, filename=filename[:200], status='DRAFT', geojson=fc,
            feature_count=rep.pop('feature_count'), vertex_count=rep.pop('vertex_count'), bbox=rep.pop('bbox'),
            size_bytes=len(raw), range_km=RANGE_KM, report=rep, note=note[:300],
            uploaded_by=user if getattr(user, 'is_authenticated', False) else None)


def publish(layer: BorderLayer) -> BorderLayer:
    with transaction.atomic():
        BorderLayer.objects.filter(status='PUBLISHED').exclude(pk=layer.pk).update(status='ARCHIVED')
        layer.status = 'PUBLISHED'
        layer.published_at = timezone.now()
        layer.save(update_fields=['status', 'published_at'])
    return layer


def live() -> dict:
    """What the map shows: the published layer, or the bundled Natural Earth fallback (version 0)."""
    row = BorderLayer.objects.filter(status='PUBLISHED').first()
    if row:
        return dict(version=row.version, source=row.filename, published_at=row.published_at.isoformat(),
                    range_km=row.range_km, bbox=row.bbox, geojson=row.geojson)
    try:
        d = json.loads(STATIC_FALLBACK.read_text(encoding='utf8'))
    except (OSError, ValueError):
        d = {'type': 'FeatureCollection', 'features': []}
    return dict(version=0, source='Built-in (Natural Earth)', published_at=None, range_km=None, bbox=[64, 20, 84, 38], geojson=d)
