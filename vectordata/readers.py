"""Streaming readers for the supported GIS formats. Each yields SourceFeature(geometry, props, layer, crs)."""
from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Iterator, NamedTuple

import shapely
import shapefile
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.geometry import shape

from .errors import VectorError

FORMATS = {'GEOJSON': 'GeoJSON', 'SHAPEFILE': 'Shapefile (zipped)', 'GPKG': 'GeoPackage', 'OSM_PBF': 'OpenStreetMap PBF'}
MAX_UNZIPPED_BYTES = 2 * 1024 ** 3
MAX_ZIP_MEMBERS = 400
MAX_GEOJSON_BYTES = 200 * 1024 ** 2  # GeoJSON is parsed in memory
SHP_REQUIRED = ('.shx', '.dbf', '.prj')
OSM_KEYS = ('highway', 'waterway', 'natural', 'water', 'landuse', 'railway', 'building', 'place', 'boundary', 'man_made')


class SourceFeature(NamedTuple):
    geometry: shapely.Geometry
    props: dict
    layer: str
    crs: CRS


def detect_format(filename: str) -> str:
    name = filename.lower()
    if name.endswith(('.geojson', '.json')):
        return 'GEOJSON'
    if name.endswith('.zip'):
        return 'SHAPEFILE'
    if name.endswith('.gpkg'):
        return 'GPKG'
    if name.endswith('.osm.pbf') or name.endswith('.pbf'):
        return 'OSM_PBF'
    raise VectorError('unsupported file type: use .geojson, .gpkg, .osm.pbf, or a Shapefile (.zip, or .shp with .shx .dbf .prj)')


def _crs(value, what: str) -> CRS:
    try:
        return CRS.from_user_input(value)
    except CRSError:
        raise VectorError(f'{what}: the coordinate reference system "{str(value)[:80]}" is not recognised')


def read_geojson(path: Path, workdir: Path) -> Iterator[SourceFeature]:
    if path.stat().st_size > MAX_GEOJSON_BYTES:
        raise VectorError(f'GeoJSON larger than {MAX_GEOJSON_BYTES // 1024 ** 2} MB; use GeoPackage or OSM PBF instead')
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise VectorError(f'not valid UTF-8 GeoJSON ({e})')
    if not isinstance(data, dict) or data.get('type') != 'FeatureCollection' or not isinstance(data.get('features'), list):
        raise VectorError('the GeoJSON must be a FeatureCollection with a "features" list')
    named = ((data.get('crs') or {}).get('properties') or {}).get('name')
    crs = _crs(named or 'OGC:CRS84', 'GeoJSON')
    layer = str(data.get('name') or path.stem)
    for i, f in enumerate(data['features']):
        if not isinstance(f, dict) or not f.get('geometry'):
            continue
        try:
            geom = shape(f['geometry'])
        except Exception as e:
            raise VectorError(f'feature {i}: invalid geometry ({e})')
        yield SourceFeature(geom, f.get('properties') or {}, layer, crs)


def _extract_shapefiles(path: Path, workdir: Path) -> list[Path]:
    """Extract only Shapefile members, flattening paths (no zip-slip), with size limits."""
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise VectorError('the Shapefile upload is not a valid ZIP archive')
    keep = {'.shp', '.shx', '.dbf', '.prj', '.cpg'}
    with z:
        members = [m for m in z.infolist() if not m.is_dir() and Path(m.filename).suffix.lower() in keep]
        if len(members) > MAX_ZIP_MEMBERS:
            raise VectorError('the ZIP has too many files')
        if sum(m.file_size for m in members) > MAX_UNZIPPED_BYTES:
            raise VectorError('the ZIP expands to more than 2 GB')
        seen = set()
        for m in members:
            base = Path(m.filename.replace('\\', '/')).name
            stem, suffix = Path(base).stem, Path(base).suffix.lower()
            target = workdir / f'{stem}{suffix}'
            if target.name.lower() in seen:
                raise VectorError(f'the ZIP contains two files called {target.name}')
            seen.add(target.name.lower())
            with z.open(m) as src, open(target, 'wb') as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
    shps = sorted(workdir.glob('*.shp'))
    if not shps:
        raise VectorError('the ZIP contains no .shp file')
    for shp in shps:
        missing = [s for s in SHP_REQUIRED if not shp.with_suffix(s).exists()]
        if missing:
            raise VectorError(f'{shp.name} is missing {", ".join(missing)} (.prj is required to know the coordinate system)')
    return shps


def read_shapefile(path: Path, workdir: Path) -> Iterator[SourceFeature]:
    for shp in _extract_shapefiles(path, workdir):
        crs = _crs(shp.with_suffix('.prj').read_text(errors='replace').strip(), shp.name)
        cpg = shp.with_suffix('.cpg')
        encoding = cpg.read_text(errors='ignore').strip() if cpg.exists() else 'utf-8'
        try:
            with shapefile.Reader(str(shp), encoding=encoding or 'utf-8', encodingErrors='replace') as r:
                for rec in r.iterShapeRecords():
                    if rec.shape.shapeType == shapefile.NULL:
                        continue
                    yield SourceFeature(shape(rec.shape.__geo_interface__), rec.record.as_dict(), shp.stem, crs)
        except VectorError:
            raise
        except Exception as e:
            raise VectorError(f'{shp.name} could not be read ({e})')


def _gpkg_wkb(blob: bytes) -> bytes | None:
    """Strip the GeoPackage binary header, leaving standard WKB."""
    if len(blob) < 8 or blob[:2] != b'GP':
        raise VectorError('the GeoPackage contains a geometry that is not in GeoPackage binary form')
    flags = blob[3]
    if flags & 0x10:  # empty geometry
        return None
    envelope = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get((flags >> 1) & 7)
    if envelope is None:
        raise VectorError('the GeoPackage contains a corrupt geometry header')
    return blob[8 + envelope:]


def read_gpkg(path: Path, workdir: Path) -> Iterator[SourceFeature]:
    """A GeoPackage is a SQLite file; it is opened read-only purely as an input format."""
    quote = lambda s: '"' + s.replace('"', '""') + '"'
    try:
        db = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)
    except sqlite3.Error as e:
        raise VectorError(f'the GeoPackage cannot be opened ({e})')
    try:
        layers = db.execute(
            "SELECT g.table_name, g.column_name, g.srs_id FROM gpkg_geometry_columns g "
            "JOIN gpkg_contents c ON c.table_name = g.table_name WHERE c.data_type = 'features'").fetchall()
        if not layers:
            raise VectorError('the GeoPackage has no vector feature layers')
        for table, column, srs_id in layers:
            row = db.execute('SELECT organization, organization_coordsys_id, definition FROM gpkg_spatial_ref_sys '
                             'WHERE srs_id = ?', (srs_id,)).fetchone()
            if srs_id in (0, -1) or not row:
                raise VectorError(f'layer "{table}" has no defined coordinate reference system')
            org, code, definition = row
            crs = _crs(f'EPSG:{code}' if str(org).upper() == 'EPSG' else definition, f'layer "{table}"')
            cur = db.execute(f'SELECT * FROM {quote(table)}')
            names = [d[0] for d in cur.description]
            gi = names.index(column)
            for rec in cur:
                if rec[gi] is None:
                    continue
                wkb = _gpkg_wkb(rec[gi])
                if wkb is None:
                    continue
                props = {n: v for i, (n, v) in enumerate(zip(names, rec)) if i != gi and not isinstance(v, bytes)}
                try:
                    geom = shapely.from_wkb(wkb)
                except shapely.errors.GEOSException as e:
                    raise VectorError(f'layer "{table}": unsupported geometry ({e})')
                yield SourceFeature(geom, props, table, crs)
    except sqlite3.Error as e:
        raise VectorError(f'not a valid GeoPackage ({e})')
    finally:
        db.close()


def read_osm_pbf(path: Path, workdir: Path) -> Iterator[SourceFeature]:
    import osmium

    crs = CRS.from_epsg(4326)
    # Closed ways with these tags are areas: take them from the assembled area, not the outline way.
    def is_area_tags(t):
        return ('building' in t or t.get('natural') == 'water' or 'water' in t
                or t.get('landuse') in ('residential', 'reservoir', 'basin') or t.get('waterway') == 'riverbank'
                or t.get('man_made') == 'bridge')

    try:
        processor = (osmium.FileProcessor(str(path))
                     .with_locations()
                     .with_areas(osmium.filter.KeyFilter(*OSM_KEYS))
                     .with_filter(osmium.filter.KeyFilter(*OSM_KEYS))
                     .with_filter(osmium.filter.GeoInterfaceFilter()))
        for obj in processor:
            gi = getattr(obj, '__geo_interface__', None)
            if not gi or not gi.get('geometry'):
                continue
            tags = {t.k: t.v for t in obj.tags}
            if obj.is_way() and obj.is_closed() and is_area_tags(tags):
                continue
            if obj.is_area() and not (is_area_tags(tags) or 'place' in tags or 'boundary' in tags):
                continue  # e.g. a roundabout: already taken as a line
            if obj.is_area() and obj.from_way() and 'boundary' in tags and not is_area_tags(tags):
                continue
            yield SourceFeature(shape(gi['geometry']), tags, 'osm', crs)
    except RuntimeError as e:
        raise VectorError(f'the OSM PBF file could not be read ({e})')


READERS = {'GEOJSON': read_geojson, 'SHAPEFILE': read_shapefile, 'GPKG': read_gpkg, 'OSM_PBF': read_osm_pbf}
