"""Border vector layer: validation, versioning, publish/rollback and permissions (python manage.py test battle.tests_borders)."""
import json

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from . import borders
from .models import BorderLayer

# A short stretch of the Punjab border (inside the 100 km range of the reference line).
PUNJAB = [[74.5, 31.0], [74.6, 31.1], [74.7, 31.2], [74.8, 31.3]]


def gj(*geoms, **props):
    feats = [{'type': 'Feature', 'properties': props or {'name': 'test'}, 'geometry': g} for g in geoms]
    return json.dumps({'type': 'FeatureCollection', 'features': feats}).encode()


def line(coords=PUNJAB):
    return {'type': 'LineString', 'coordinates': coords}


class ValidateTests(TestCase):
    def test_valid_line(self):
        fc, rep = borders.validate(gj(line()))
        self.assertEqual(rep['feature_count'], 1)
        self.assertEqual(fc['features'][0]['properties']['kind'], 'boundary')

    def test_polygon_is_closed_and_kind_is_area(self):
        poly = {'type': 'Polygon', 'coordinates': [[[74.5, 31.0], [74.8, 31.0], [74.8, 31.3]]]}
        fc, _ = borders.validate(gj(poly))
        ring = fc['features'][0]['geometry']['coordinates'][0]
        self.assertEqual(ring[0], ring[-1])
        self.assertEqual(fc['features'][0]['properties']['kind'], 'area')

    def test_loc_is_detected_from_name(self):
        fc, _ = borders.validate(gj(line(), name='Line of Control'))
        self.assertEqual(fc['features'][0]['properties']['kind'], 'loc')

    def test_rejects(self):
        for raw, needle in [
            (b'', 'empty'),
            (b'not json', 'JSON'),
            (b'{"type":"Topology"}', 'not GeoJSON'),
            (json.dumps({'type': 'FeatureCollection', 'features': []}).encode(), 'no features'),
            (gj({'type': 'Point', 'coordinates': [74.5, 31]}), 'not supported'),
            (gj(line([[7400000.0, 3100000.0], [7400100.0, 3100100.0]])), 'projected'),
            (gj(line([[2.3, 48.8], [2.4, 48.9]])), 'over India'),  # Paris
            (json.dumps({'type': 'FeatureCollection', 'crs': {'properties': {'name': 'EPSG:32643'}},
                         'features': [{'type': 'Feature', 'properties': {}, 'geometry': line()}]}).encode(), 'WGS84'),
        ]:
            with self.assertRaises(borders.BorderError, msg=needle) as cm:
                borders.validate(raw)
            self.assertIn(needle, str(cm.exception))

    def test_far_from_border_warns(self):
        far = [[78.0, 25.0], [78.2, 25.2]]  # central India, > 100 km from the western frontier
        _, rep = borders.validate(gj(line(far)))
        self.assertTrue(any('100 km' in w for w in rep['warnings']))

    def test_near_border_has_no_range_warning(self):
        _, rep = borders.validate(gj(line()))
        self.assertFalse(any('100 km' in w for w in rep['warnings']))


class ApiTests(TestCase):
    def setUp(self):
        U = get_user_model()
        self.staff = U.objects.create_user('ctl', password='pass12345', is_staff=True)
        self.client_staff = APIClient()
        self.client_staff.force_authenticate(self.staff)
        self.anon = APIClient()

    def upload(self, raw, name='b.geojson', **extra):
        f = SimpleUploadedFile(name, raw, content_type='application/json')
        return self.client_staff.post('/api/borders/layers/', {'file': f, **extra}, format='multipart')

    def test_fallback_when_nothing_published(self):
        d = self.anon.get('/api/borders/').json()
        self.assertEqual(d['version'], 0)
        self.assertTrue(d['geojson']['features'])

    def test_upload_publish_replace_rollback(self):
        r = self.upload(gj(line()), note='first')
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()['status'], 'DRAFT')
        self.assertEqual(self.anon.get('/api/borders/').json()['version'], 0)  # a draft is not live

        v1 = r.json()['id']
        self.assertEqual(self.client_staff.post(f'/api/borders/layers/{v1}/publish/').status_code, 200)
        live = self.anon.get('/api/borders/').json()
        self.assertEqual((live['version'], live['source']), (1, 'b.geojson'))
        self.assertEqual(self.anon.get('/api/borders/version/').json()['version'], 1)

        v2 = self.upload(gj(line([[74.5, 31.0], [74.9, 31.4]])), name='c.geojson', publish='1').json()
        self.assertEqual((v2['version'], v2['status']), (2, 'PUBLISHED'))
        self.assertEqual(BorderLayer.objects.get(id=v1).status, 'ARCHIVED')
        self.assertEqual(BorderLayer.objects.filter(status='PUBLISHED').count(), 1)
        self.assertEqual(self.anon.get('/api/borders/').json()['source'], 'c.geojson')

        self.client_staff.post(f'/api/borders/layers/{v1}/publish/')  # roll back
        self.assertEqual(self.anon.get('/api/borders/').json()['version'], 1)

    def test_etag_revalidation(self):
        resp = self.anon.get('/api/borders/')
        again = self.anon.get('/api/borders/', HTTP_IF_NONE_MATCH=resp['ETag'])
        self.assertEqual(again.status_code, 304)

    def test_invalid_upload_changes_nothing(self):
        r = self.upload(b'{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},"geometry":{"type":"Point","coordinates":[74,31]}}]}')
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.json()['errors'])
        self.assertEqual(BorderLayer.objects.count(), 0)

    def test_cannot_delete_live_version(self):
        lid = self.upload(gj(line()), publish='1').json()['id']
        self.assertEqual(self.client_staff.delete(f'/api/borders/layers/{lid}/').status_code, 409)
        other = self.upload(gj(line())).json()['id']
        self.assertEqual(self.client_staff.delete(f'/api/borders/layers/{other}/').status_code, 204)

    def test_only_staff_can_manage(self):
        self.assertIn(self.anon.get('/api/borders/layers/').status_code, (401, 403))
        f = SimpleUploadedFile('x.geojson', gj(line()))
        self.assertIn(self.anon.post('/api/borders/layers/', {'file': f}, format='multipart').status_code, (401, 403))
        U = get_user_model()
        player = APIClient()
        player.force_authenticate(U.objects.create_user('p', password='pass12345'))
        self.assertEqual(player.get('/api/borders/layers/').status_code, 403)
