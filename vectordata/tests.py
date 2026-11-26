"""Regional vector data: formats, validation, tiling, versioning and permissions (python manage.py test vectordata)."""
import io
import json
import shutil
import sqlite3
import struct
import tempfile
import zipfile
from pathlib import Path

import mapbox_vector_tile
import osmium
import shapefile
import shapely
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from pyproj import CRS, Transformer
from rest_framework.test import APIClient
from shapely.geometry import LineString, Point, Polygon, shape

from . import coverage, services
from .classify import classify
from .models import Region, VectorDataset
from .tiler import to_unit_mercator

LON, LAT = 74.55, 31.15  # centre of the test region
BBOX = f'{LON - .1},{LAT - .1},{LON + .1},{LAT + .1}'
ALL = ['rivers', 'water', 'roads', 'bridges', 'railways', 'buildings', 'settlements', 'boundaries']


def feature(geometry, **props):
    return {'type': 'Feature', 'properties': props, 'geometry': geometry}


def line(dy=0.0):
    return {'type': 'LineString', 'coordinates': [[LON - .05, LAT + dy], [LON + .05, LAT + dy + .02]]}


def square(dx=0.0, d=0.004):
    x, y = LON + dx, LAT - .03
    return {'type': 'Polygon', 'coordinates': [[[x, y], [x + d, y], [x + d, y + d], [x, y + d], [x, y]]]}


def sample_features():
    return [
        feature(line(0), highway='primary', name='Test Road', bridge='yes'),
        feature(line(.01), waterway='river', name='Test River'),
        feature(line(.02), railway='rail'),
        feature(square(0), building='yes'),
        feature(square(.02), natural='water'),
        feature({'type': 'Point', 'coordinates': [LON, LAT]}, place='town', name='Testpur'),
        feature(line(-.05), boundary='administrative'),
    ]


def geojson(features, **extra):
    return json.dumps(dict({'type': 'FeatureCollection', 'features': features}, **extra)).encode()


def upload_file(name, data):
    return SimpleUploadedFile(name, data)


class Base(TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='vectest-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        override = override_settings(VECTOR_DATA=dict(settings.VECTOR_DATA, ROOT=self.root, INLINE=True))
        override.enable()
        self.addCleanup(override.disable)
        U = get_user_model()
        self.staff = APIClient()
        self.staff.force_authenticate(U.objects.create_user('vadmin', password='pass12345', is_staff=True))
        self.player = APIClient()
        self.player.force_authenticate(U.objects.create_user('vplayer', password='pass12345'))
        self.anon = APIClient()
        self.region = self.make_region('Region A')

    def make_region(self, name, lon=LON, lat=LAT, km=100):
        r = self.staff.post('/api/vectors/regions/', {'name': name, 'coverage_mode': 'POINT', 'lon': lon, 'lat': lat,
                                                      'range_km': km})
        self.assertEqual(r.status_code, 201, r.content)
        return Region.objects.get(pk=r.json()['id'])

    def upload(self, data=None, name='Base map', filename='map.geojson', types=ALL, region=None, files=None, **extra):
        files = files or [upload_file(filename, data if data is not None else geojson(sample_features()))]
        return self.staff.post('/api/vectors/datasets/', dict(region_id=(region or self.region).id, dataset_name=name,
                               feature_types=types, files=files, **extra), format='multipart')

    def ok(self, response) -> VectorDataset:
        self.assertEqual(response.status_code, 202, response.content)
        ds = VectorDataset.objects.get(pk=response.json()['id'])
        self.assertEqual(ds.processing_status, 'READY', ds.error_message)
        return ds

    def failed(self, response, needle):
        self.assertEqual(response.status_code, 202, response.content)
        ds = VectorDataset.objects.get(pk=response.json()['id'])
        self.assertEqual(ds.processing_status, 'FAILED')
        self.assertIn(needle, ds.error_message)
        self.assertFalse(ds.is_active)
        return ds

    def active(self, bbox=BBOX):
        return self.anon.get(f'/api/vectors/active/?bbox={bbox}').json()['datasets']

    def tile_layers(self, ds, zoom):
        """Union of layer names → features over all stored tiles at one zoom."""
        out = {}
        for p in (self.root / ds.processed_tile_path / str(zoom)).rglob('*.pbf'):
            for k, v in mapbox_vector_tile.decode(p.read_bytes()).items():
                out.setdefault(k, []).extend(v['features'])
        return out


class CoverageTests(TestCase):
    def test_100_km_buffer_is_metric(self):
        geom, bbox = coverage.build_coverage({'type': 'Point', 'coordinates': [LON, LAT]}, 100)
        cover = shape(geom)
        # 1° latitude ≈ 111 km, 1° longitude ≈ 95 km at 31°N
        self.assertTrue(cover.contains(Point(LON, LAT + 0.88)))
        self.assertFalse(cover.contains(Point(LON, LAT + 0.93)))
        self.assertTrue(cover.contains(Point(LON + 1.03, LAT)))
        self.assertFalse(cover.contains(Point(LON + 1.07, LAT)))
        self.assertAlmostEqual(bbox[3] - LAT, 0.9, delta=0.01)

    def test_classification(self):
        kinds = lambda props, geom, layer='x': dict(classify(props, layer, geom, set(ALL)))
        self.assertEqual(set(kinds({'highway': 'primary', 'bridge': 'yes'}, 'line')), {'roads', 'bridges'})
        self.assertEqual(set(kinds({'fclass': 'primary', 'bridge': 'F'}, 'line', 'gis_osm_roads_free_1')), {'roads'})
        self.assertEqual(set(kinds({'fclass': 'river'}, 'line')), {'rivers'})
        self.assertEqual(set(kinds({}, 'line', 'district_railways')), {'railways'})
        self.assertEqual(kinds({'highway': 'bus_stop'}, 'point'), {})
        self.assertEqual(classify({}, 'x', 'line', {'rivers'}), [('rivers', 'rivers')])  # only one type fits
        self.assertEqual(classify({'highway': 'primary'}, 'x', 'line', {'rivers'}), [])  # a road, not selected


class WorkflowTests(Base):
    def test_geojson_upload_produces_tiles_for_every_layer(self):
        ds = self.ok(self.upload())
        self.assertTrue(ds.is_active)
        self.assertEqual(set(ds.stats['layers']), set(ALL))
        self.assertEqual(ds.dataset_version, 1)
        self.assertTrue((self.root / ds.source_file_path).is_file())
        deep = self.tile_layers(ds, 14)
        self.assertEqual(set(deep), set(ALL))
        self.assertIn('Test Road', {f['properties'].get('name') for f in deep['roads']})
        low = self.tile_layers(ds, 7)  # zoom filtering: no buildings or bridges when zoomed out
        self.assertIn('rivers', low)
        self.assertNotIn('buildings', low)
        self.assertNotIn('bridges', low)

        [meta] = self.active()
        self.assertEqual((meta['id'], meta['region'], meta['version']), (ds.id, 'Region A', 1))
        # fetch the tile over the town and check its position survives the projection
        merc = to_unit_mercator(Point(LON, LAT))
        n = 2 ** 14
        x, y = int(merc.x * n), int(merc.y * n)
        r = self.anon.get(f'/api/vectors/tiles/{ds.id}/14/{x}/{y}.pbf')
        self.assertEqual(r.status_code, 200)
        town = mapbox_vector_tile.decode(b''.join(r.streaming_content),
                                         default_options={'y_coord_down': True})['settlements']['features'][0]
        px, py = town['geometry']['coordinates']
        self.assertAlmostEqual(px, (merc.x * n - x) * 4096, delta=1)
        self.assertAlmostEqual(py, (merc.y * n - y) * 4096, delta=1)
        self.assertEqual(self.anon.get(f'/api/vectors/tiles/{ds.id}/14/0/0.pbf').status_code, 204)

    def test_only_features_inside_coverage_are_kept(self):
        near = feature(line(0), highway='primary')
        far = feature({'type': 'LineString', 'coordinates': [[78.0, 25.0], [78.2, 25.1]]}, highway='primary')
        ds = self.ok(self.upload(geojson([near, far])))
        self.assertEqual((ds.stats['kept'], ds.stats['outside_coverage']), (1, 1))
        self.assertLess(ds.max_lon, 75)
        small = self.make_region('Small', km=2)
        clipped = self.ok(self.upload(geojson([near]), region=small))
        self.assertLess(clipped.max_lon - clipped.min_lon, 0.05)  # the 10 km road is cut at the 2 km edge

    def test_regions_are_independent_and_filtered_by_view(self):
        other = self.make_region('Region B', lon=70.0, lat=27.58)
        a = self.ok(self.upload())
        b = self.ok(self.upload(geojson([feature({'type': 'LineString', 'coordinates': [[69.9, 27.58], [70.1, 27.6]]},
                                                 highway='primary')]), region=other))
        self.assertEqual([d['id'] for d in self.active()], [a.id])
        self.assertEqual([d['id'] for d in self.active('69.9,27.5,70.1,27.7')], [b.id])
        self.assertEqual(self.active('80,10,80.2,10.2'), [])

    def test_replace_keeps_old_version_until_success(self):
        v1 = self.ok(self.upload())
        bad = self.staff.post('/api/vectors/datasets/', {'replaces': v1.id, 'feature_types': ALL,
                              'files': [upload_file('bad.geojson', b'not json')]}, format='multipart')
        v2 = self.failed(bad, 'GeoJSON')
        self.assertEqual(v2.dataset_version, 2)
        self.assertEqual([d['id'] for d in self.active()], [v1.id])
        good = self.staff.post('/api/vectors/datasets/', {'replaces': v1.id, 'feature_types': ['roads'],
                               'files': [upload_file('new.geojson', geojson(sample_features()))]}, format='multipart')
        v3 = self.ok(good)
        self.assertEqual([(d['id'], d['version']) for d in self.active()], [(v3.id, 3)])
        self.assertEqual(self.anon.get(f'/api/vectors/tiles/{v1.id}/7/0/0.pbf').status_code, 404)
        # roll back, deactivate, delete
        self.assertEqual(self.staff.post(f'/api/vectors/datasets/{v1.id}/activate/').status_code, 200)
        self.assertEqual([d['id'] for d in self.active()], [v1.id])
        self.assertEqual(self.staff.delete(f'/api/vectors/datasets/{v1.id}/').status_code, 409)
        self.staff.post(f'/api/vectors/datasets/{v1.id}/deactivate/')
        self.assertEqual(self.active(), [])
        folder = services.dataset_dir(v1)
        self.assertEqual(self.staff.delete(f'/api/vectors/datasets/{v1.id}/').status_code, 204)
        self.assertFalse(folder.exists())
        self.assertEqual(self.staff.post(f'/api/vectors/datasets/{v2.id}/activate/').status_code, 400)

    def test_duplicate_name_needs_replace(self):
        self.ok(self.upload())
        r = self.upload()
        self.assertEqual(r.status_code, 400)
        self.assertIn('Replace', r.json()['error'])

    def test_draft_upload_stays_inactive(self):
        ds = self.ok(self.upload(activate='0'))
        self.assertFalse(ds.is_active)
        self.assertEqual(self.active(), [])

    def test_failures_have_meaningful_messages(self):
        projected = feature({'type': 'LineString', 'coordinates': [[8300000, 3640000], [8300100, 3640100]]}, highway='primary')
        self.failed(self.upload(geojson([projected]), name='a'), 'projected')
        paris = feature({'type': 'LineString', 'coordinates': [[2.3, 48.8], [2.4, 48.9]]}, highway='primary')
        self.failed(self.upload(geojson([paris]), name='b'), 'outside the coverage')
        self.failed(self.upload(geojson([]), name='c'), 'no features')
        self.failed(self.upload(b'PK-not-a-zip', filename='x.zip', name='d'), 'ZIP')
        self.failed(self.upload(b'garbage', filename='x.gpkg', name='e'), 'GeoPackage')
        self.failed(self.upload(b'garbage', filename='x.osm.pbf', name='f'), 'OSM PBF')
        self.assertEqual(self.active(), [])

    def test_rejected_before_anything_is_stored(self):
        for kwargs, needle in [(dict(filename='x.kml'), 'unsupported'), (dict(types=[]), 'feature type'),
                               (dict(types=['tanks']), 'unknown'), (dict(name=''), 'name'),
                               (dict(files=[upload_file('a.shp', b'1'), upload_file('a.dbf', b'1')]), 'missing')]:
            r = self.upload(**kwargs)
            self.assertEqual(r.status_code, 400, kwargs)
            self.assertIn(needle, r.json()['error'])
        with override_settings(VECTOR_DATA=dict(settings.VECTOR_DATA, MAX_UPLOAD_MB=0)):
            self.assertIn('limit', self.upload().json()['error'])
        self.assertEqual(VectorDataset.objects.count(), 0)

    def test_coverage_change_flags_and_reprocess_applies_it(self):
        v1 = self.ok(self.upload())
        r = self.staff.patch(f'/api/vectors/regions/{self.region.id}/', {'range_km': 2}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        [row] = self.staff.get('/api/vectors/datasets/').json()['datasets']
        self.assertTrue(row['coverage_outdated'])
        v2 = VectorDataset.objects.get(pk=self.staff.post(f'/api/vectors/datasets/{v1.id}/reprocess/').json()['id'])
        self.assertEqual((v2.processing_status, v2.is_active, v2.dataset_version), ('READY', True, 2))
        self.assertLess(v2.max_lon - v2.min_lon, v1.max_lon - v1.min_lon)

    def test_region_from_border_layer_and_delete_protection(self):
        r = self.staff.post('/api/vectors/regions/', {'name': 'Border sector', 'coverage_mode': 'BORDER',
                                                      'border_bbox': '74,30.8,75,32', 'range_km': 100})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertIn('LineString', r.json()['source_geometry']['type'])
        self.assertEqual(self.staff.delete(f"/api/vectors/regions/{r.json()['id']}/").status_code, 204)
        self.ok(self.upload())
        self.assertEqual(self.staff.delete(f'/api/vectors/regions/{self.region.id}/').status_code, 409)

    def test_permissions(self):
        ds = self.ok(self.upload(activate='0'))
        for client in (self.anon, self.player):
            for method, url in [('get', '/api/vectors/datasets/'), ('post', '/api/vectors/datasets/'),
                                ('get', '/api/vectors/regions/'), ('post', '/api/vectors/regions/'),
                                ('delete', f'/api/vectors/datasets/{ds.id}/'),
                                ('post', f'/api/vectors/datasets/{ds.id}/activate/'),
                                ('patch', f'/api/vectors/regions/{self.region.id}/')]:
                self.assertIn(getattr(client, method)(url).status_code, (401, 403), (method, url))
            self.assertEqual(client.get(f'/api/vectors/active/?bbox={BBOX}').status_code, 200)
            self.assertEqual(client.get(f'/api/vectors/tiles/{ds.id}/7/0/0.pbf').status_code, 404)  # not published

    def test_etag(self):
        self.ok(self.upload())
        first = self.anon.get(f'/api/vectors/active/?bbox={BBOX}')
        again = self.anon.get(f'/api/vectors/active/?bbox={BBOX}', HTTP_IF_NONE_MATCH=first['ETag'])
        self.assertEqual(again.status_code, 304)


class FormatTests(Base):
    def shapefile_parts(self, prj=True):
        """A road in UTM zone 43N, so the upload must be reprojected."""
        utm = CRS.from_epsg(32643)
        fwd = Transformer.from_crs(4326, utm, always_xy=True)
        buffers = {k: io.BytesIO() for k in ('shp', 'shx', 'dbf')}
        with shapefile.Writer(shp=buffers['shp'], shx=buffers['shx'], dbf=buffers['dbf'], shapeType=shapefile.POLYLINE) as w:
            w.field('fclass', 'C')
            w.line([[list(fwd.transform(*p)) for p in line()['coordinates']]])
            w.record('primary')
        parts = {f'roads.{k}': v.getvalue() for k, v in buffers.items()}
        if prj:
            parts['roads.prj'] = utm.to_wkt('WKT1_ESRI').encode()
        return parts

    def check_road(self, ds):
        self.assertEqual(ds.stats['layers'], {'roads': 1})
        self.assertAlmostEqual(ds.min_lon, LON - .05, places=3)
        self.assertAlmostEqual(ds.min_lat, LAT, places=3)

    def test_zipped_and_loose_shapefile_with_reprojection(self):
        blob = io.BytesIO()
        with zipfile.ZipFile(blob, 'w') as z:
            for name, data in self.shapefile_parts().items():
                z.writestr(f'folder/{name}', data)
        ds = self.ok(self.upload(blob.getvalue(), filename='roads.zip', types=['roads']))
        self.assertIn('32643', ds.coordinate_reference_system)
        self.check_road(ds)
        loose = [upload_file(n, d) for n, d in self.shapefile_parts().items()]
        self.check_road(self.ok(self.upload(name='loose', files=loose, types=['roads'])))

    def test_shapefile_without_prj_in_zip_fails(self):
        blob = io.BytesIO()
        with zipfile.ZipFile(blob, 'w') as z:
            for name, data in self.shapefile_parts(prj=False).items():
                z.writestr(name, data)
        self.failed(self.upload(blob.getvalue(), filename='roads.zip'), '.prj')

    def test_geopackage(self):
        path = self.root / 'in.gpkg'
        db = sqlite3.connect(path)
        db.executescript('''
            CREATE TABLE gpkg_spatial_ref_sys (srs_id INTEGER PRIMARY KEY, organization TEXT, organization_coordsys_id INTEGER, definition TEXT);
            CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT);
            CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, srs_id INTEGER);
            CREATE TABLE "my roads" (fid INTEGER PRIMARY KEY, geom BLOB, highway TEXT);
            INSERT INTO gpkg_spatial_ref_sys VALUES (3857, 'EPSG', 3857, '');
            INSERT INTO gpkg_contents VALUES ('my roads', 'features');
            INSERT INTO gpkg_geometry_columns VALUES ('my roads', 'geom', 3857);''')
        fwd = Transformer.from_crs(4326, 3857, always_xy=True)
        road = LineString([fwd.transform(*p) for p in line()['coordinates']])
        blob = b'GP\x00\x01' + struct.pack('<i', 3857) + shapely.to_wkb(road)
        db.execute('INSERT INTO "my roads" VALUES (1, ?, ?)', (blob, 'primary'))
        db.commit()
        db.close()
        ds = self.ok(self.upload(path.read_bytes(), filename='in.gpkg', types=['roads']))
        self.assertIn('3857', ds.coordinate_reference_system)
        self.check_road(ds)

    def test_osm_pbf(self):
        path = self.root / 'in.osm.pbf'
        nodes = {1: (LON - .02, LAT), 2: (LON + .02, LAT), 3: (LON, LAT - .02), 4: (LON + .004, LAT - .02),
                 5: (LON + .004, LAT - .016), 6: (LON, LAT - .016)}
        with osmium.SimpleWriter(str(path)) as w:
            for i, loc in nodes.items():
                w.add_node(osmium.osm.mutable.Node(id=i, location=loc))
            w.add_node(osmium.osm.mutable.Node(id=7, location=(LON, LAT + .01), tags={'place': 'village', 'name': 'Gaon'}))
            w.add_way(osmium.osm.mutable.Way(id=10, nodes=[1, 2], tags={'highway': 'primary', 'bridge': 'yes'}))
            w.add_way(osmium.osm.mutable.Way(id=11, nodes=[3, 4, 5, 6, 3], tags={'building': 'yes'}))
        ds = self.ok(self.upload(path.read_bytes(), filename='in.osm.pbf'))
        self.assertEqual(ds.stats['layers'], {'roads': 1, 'bridges': 1, 'buildings': 1, 'settlements': 1})


class PolygonTests(TestCase):
    def test_polygon_coverage_used_as_drawn_with_zero_range(self):
        square_ = Polygon([(74, 31), (75, 31), (75, 32), (74, 32)])
        geom, bbox = coverage.build_coverage(shapely.geometry.mapping(square_), 0)
        self.assertEqual(bbox, [74, 31, 75, 32])
