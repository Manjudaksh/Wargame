"""Mapbox Vector Tile (MVT) pyramid writer.

Geometries are held in "unit Web Mercator" (x, y in 0..1, y growing south). The pyramid is walked top-down,
clipping each child from its parent's already-clipped geometry, and only non-empty tiles are written."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Callable, NamedTuple

import mapbox_vector_tile
import numpy as np
import shapely

from .errors import VectorError

EXTENT = 4096
BUFFER = 160 / EXTENT  # fraction of a tile drawn beyond its edge, so lines and markers join cleanly
MAX_LAT = 85.0511


class TileFeature(NamedTuple):
    geometry: shapely.Geometry  # unit Web Mercator
    layer: str
    props: dict
    min_zoom: int


def to_unit_mercator(geom):
    def apply(a):
        lat = np.radians(np.clip(a[:, 1], -MAX_LAT, MAX_LAT))
        y = 0.5 - np.log(np.tan(math.pi / 4 + lat / 2)) / (2 * math.pi)
        return np.column_stack([(a[:, 0] + 180) / 360, y])
    return shapely.transform(geom, apply)


def _parts(geom, dim: int):
    """Keep only the pieces of a clip result with the original dimension (0 point, 1 line, 2 area)."""
    if geom.is_empty:
        return None
    if geom.geom_type == 'GeometryCollection':
        keep = [g for g in geom.geoms if shapely.get_dimensions(g) == dim]
        if not keep:
            return None
        return shapely.union_all(keep) if len(keep) > 1 else keep[0]
    return geom if shapely.get_dimensions(geom) == dim else None


def _clip(item, z: int, x: int, y: int):
    geom, bounds, dim, feature = item
    size = 1 / 2 ** z
    pad = size * BUFFER
    w, s, e, n = x * size - pad, y * size - pad, (x + 1) * size + pad, (y + 1) * size + pad
    if bounds[2] < w or bounds[0] > e or bounds[3] < s or bounds[1] > n:
        return None
    if bounds[0] >= w and bounds[2] <= e and bounds[1] >= s and bounds[3] <= n:
        return item
    cut = _parts(shapely.clip_by_rect(geom, w, s, e, n), dim)
    return None if cut is None else (cut, cut.bounds, dim, feature)


def _encode(items, z: int, x: int, y: int) -> bytes | None:
    n = 2 ** z
    pixel = 1 / (n * EXTENT)
    layers: dict[str, list] = {}
    for geom, bounds, dim, feature in items:
        if feature.min_zoom > z:
            continue
        # drop detail smaller than about half a screen pixel (a 256 px tile has 16 extent units per pixel)
        if dim == 2 and (bounds[2] - bounds[0]) < 8 * pixel and (bounds[3] - bounds[1]) < 8 * pixel:
            continue
        if dim:
            geom = geom.simplify(6 * pixel, preserve_topology=dim == 2)
        local = shapely.transform(geom, lambda a: np.round((a * n - (x, y)) * EXTENT))
        if dim == 2:
            local = _parts(shapely.make_valid(local), 2) if not local.is_valid else local
        if local is None or local.is_empty or (dim == 1 and local.length == 0) or (dim == 2 and local.area == 0):
            continue
        layers.setdefault(feature.layer, []).append({'geometry': local, 'properties': feature.props})
    if not layers:
        return None
    return mapbox_vector_tile.encode([{'name': k, 'features': v} for k, v in sorted(layers.items())],
                                     default_options={'extents': EXTENT, 'y_coord_down': True})


def write_pyramid(features: list[TileFeature], out_dir: Path, min_zoom: int, max_zoom: int,
                  max_tiles: int = 80_000, progress: Callable[[int], None] | None = None) -> dict:
    """Write out_dir/{z}/{x}/{y}.pbf and return counts."""
    items = [(f.geometry, f.geometry.bounds, int(shapely.get_dimensions(f.geometry)), f) for f in features]
    roots: dict[tuple[int, int], list] = {}
    n = 2 ** min_zoom
    for item in items:
        w, s, e, nn = item[1]
        pad = BUFFER / n
        for x in range(max(0, int((w - pad) * n)), min(n - 1, int((e + pad) * n)) + 1):
            for y in range(max(0, int((s - pad) * n)), min(n - 1, int((nn + pad) * n)) + 1):
                cut = _clip(item, min_zoom, x, y)
                if cut:
                    roots.setdefault((x, y), []).append(cut)

    stats = {'tiles': 0, 'bytes': 0, 'max_tile_bytes': 0}
    stack = [(min_zoom, x, y, v) for (x, y), v in roots.items()]
    while stack:
        z, x, y, here = stack.pop()
        data = _encode(here, z, x, y)
        if data:
            stats['tiles'] += 1
            if stats['tiles'] > max_tiles:
                raise VectorError(f'the dataset needs more than {max_tiles:,} tiles; lower the maximum zoom or split the dataset')
            target = out_dir / str(z) / str(x) / f'{y}.pbf'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            stats['bytes'] += len(data)
            stats['max_tile_bytes'] = max(stats['max_tile_bytes'], len(data))
            if progress and stats['tiles'] % 50 == 0:
                progress(stats['tiles'])
        if z < max_zoom:
            for cx in (2 * x, 2 * x + 1):
                for cy in (2 * y, 2 * y + 1):
                    child = [c for c in (_clip(i, z + 1, cx, cy) for i in here) if c]
                    if child:
                        stack.append((z + 1, cx, cy, child))
    return stats
