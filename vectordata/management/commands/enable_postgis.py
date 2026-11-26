from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection

from vectordata.models import VectorDataset


class Command(BaseCommand):
    help = 'Use PostGIS for dataset coverage queries (adds a generated geometry column and spatial index).'

    def handle(self, *args, **options):
        if connection.vendor != 'postgresql':
            raise CommandError('PostGIS needs PostgreSQL')
        table = connection.ops.quote_name(VectorDataset._meta.db_table)
        with connection.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'postgis'")
            if not cur.fetchone():
                try:
                    cur.execute('CREATE EXTENSION postgis')
                except DatabaseError as e:
                    name = connection.settings_dict['NAME']
                    raise CommandError(
                        f'could not create the PostGIS extension ({str(e).strip()}). A PostgreSQL administrator must run:\n'
                        f'  psql -U postgres -d {name} -c "CREATE EXTENSION postgis;"\nthen run this command again.')
            cur.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS coverage_geom geometry(Geometry, 4326) '
                        'GENERATED ALWAYS AS (ST_SetSRID(ST_GeomFromGeoJSON(coverage_geometry::text), 4326)) STORED')
            cur.execute(f'CREATE INDEX IF NOT EXISTS vector_coverage_gist ON {table} USING gist (coverage_geom)')
        self.stdout.write(self.style.SUCCESS('PostGIS coverage column and spatial index are ready'))
