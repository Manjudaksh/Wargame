"""Region coverage: turn a boundary line / area / centre point plus a range in km into a WGS84 polygon."""
from __future__ import annotations

import math

import numpy as np
import shapely
from pyproj import CRS, Transformer
from shapely.geometry import GeometryCollection, Point, box, mapping, shape
from shapely.ops import unary_union

from .errors import VectorError

MAX_RANGE_KM = 500.0
MAX_SPAN_DEG = 30.0  # one region, not a whole continent
WGS84 = CRS.from_epsg(4326)


def reproject(geom, transformer: Transformer):
    def apply(xy):
        x, y = transformer.transform(xy[:, 0], xy[:, 1])
        return np.column_stack([x, y])
    return shapely.transform(geom, apply)


def parse_geojson_geometry(data) -> shapely.Geometry:
    """FeatureCollection | Feature | geometry (lon/lat) → one shapely geometry."""
    if not isinstance(data, dict):
        raise VectorError('coverage geometry must be a GeoJSON object')
    if data.get('type') == 'FeatureCollection':
        parts = [f.get('geometry') for f in data.get('features') or [] if isinstance(f, dict)]
    elif data.get('type') == 'Feature':
        parts = [data.get('geometry')]
    else:
        parts = [data]
    try:
        geoms = [shape(p) for p in parts if p]
    except Exception as e:  # shapely raises several types for malformed input
        raise VectorError(f'invalid coverage geometry ({e})')
    geom = unary_union(geoms) if geoms else GeometryCollection()
    if geom.is_empty:
        raise VectorError('the coverage geometry is empty')
    w, s, e, n = geom.bounds
    if not all(map(math.isfinite, (w, s, e, n))) or w < -180 or e > 180 or s < -85 or n > 85:
        raise VectorError('coverage coordinates must be WGS84 longitude/latitude')
    if e - w > MAX_SPAN_DEG or n - s > MAX_SPAN_DEG:
        raise VectorError(f'a region may span at most {MAX_SPAN_DEG:.0f}° in each direction; split it into smaller regions')
    return geom


def build_coverage(source: dict, range_km: float) -> tuple[dict, list]:
    """Buffer the source geometry by range_km (metric, local azimuthal projection) → (GeoJSON geometry, bbox).

    A polygon source with range 0 is used exactly as drawn."""
    geom = parse_geojson_geometry(source)
    if not 0 <= range_km <= MAX_RANGE_KM:
        raise VectorError(f'range must be between 0 and {MAX_RANGE_KM:.0f} km')
    if range_km == 0:
        if geom.geom_type not in ('Polygon', 'MultiPolygon'):
            raise VectorError('a line or point needs a range greater than 0 km')
        out = geom
    else:
        c = geom.envelope.centroid
        local = CRS.from_proj4(f'+proj=aeqd +lat_0={c.y} +lon_0={c.x} +datum=WGS84 +units=m')
        fwd = Transformer.from_crs(WGS84, local, always_xy=True)
        back = Transformer.from_crs(local, WGS84, always_xy=True)
        metric = reproject(shapely.segmentize(geom, 0.05), fwd)
        buffered = metric.buffer(range_km * 1000, quad_segs=12).simplify(500)
        out = reproject(buffered, back)
    out = shapely.make_valid(out)
    return mapping_rounded(out), [round(v, 5) for v in out.bounds]


def mapping_rounded(geom) -> dict:
    return mapping(shapely.transform(geom, lambda a: np.round(a, 5)))


def circle_source(lon: float, lat: float) -> dict:
    return mapping(Point(lon, lat))


def line_source_from_borders(border_fc: dict, bbox: list) -> dict:
    """Cut the part of a border layer that lies inside bbox → the reference line for a new region."""
    try:
        w, s, e, n = (float(v) for v in bbox)
    except (TypeError, ValueError):
        raise VectorError('the border section box needs four numbers: west, south, east, north')
    if not (w < e and s < n):
        raise VectorError('the box must have west < east and south < north')
    clip = box(w, s, e, n)
    parts = []
    for f in border_fc.get('features', []):
        g = shape(f['geometry'])
        if g.geom_type in ('Polygon', 'MultiPolygon'):
            g = g.boundary
        cut = g.intersection(clip)
        parts += [p for p in getattr(cut, 'geoms', [cut]) if p.geom_type == 'LineString' and not p.is_empty]
    if not parts:
        raise VectorError('no border line passes through that box')
    return mapping_rounded(unary_union(parts))
