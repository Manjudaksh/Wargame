# Regional vector data tiles

Geographic layers (rivers, water bodies, roads, bridges, railways, buildings, settlements, boundaries) drawn on the
CesiumJS map. For visualisation only: the battle simulation does not read this data.

## Admin workflow (**Vector Data** in the top menu, staff only)

1. **Create a region.** Coverage = a reference shape plus a range in km (default 100): a section of the live border
   layer (lon/lat box), an uploaded GeoJSON line or area (range 0 uses an area as drawn), or a centre point.
2. **Upload a dataset:** region, name, the feature types it contains, and the file.
3. Processing runs in a separate process. **Ready + Active** means it is on the map; **Failed** shows the reason.
   Open maps pick up changes within about 30 seconds.
4. **Replace** uploads a new version. The old one stays live until the new one succeeds. **Activate** on an older
   version rolls back. A live version must be deactivated before it can be deleted.
5. After changing a region's coverage, press **Reprocess** on its datasets (the page flags them).

A map shows every active dataset whose coverage intersects the battle area (on the battle, scenario setup and replay
maps). Players switch layers in the battle screen's layer menu. Layers appear on the Cesium map view only, not in the
Three.js ground view.

## Input formats

| Format | Notes |
|---|---|
| `.geojson` | FeatureCollection, up to 200 MB. WGS84 unless a `crs` member names another system |
| Shapefile | `.zip`, or `.shp` `.shx` `.dbf` `.prj` selected together. `.prj` is required. Several layers per zip are fine |
| `.gpkg` | Every feature layer is read, each in its own CRS |
| `.osm.pbf` | Use a district or state extract, not a whole country |

Layers are recognised from OSM tags, Geofabrik `fclass` columns or the layer/file name (e.g. `district_roads`).
If nothing matches and exactly one ticked type fits the geometry, that type is used. Only features inside the region
coverage are kept. OpenStreetMap data requires credit: the map adds it for `.osm.pbf` uploads; for OSM data supplied
in another format, record the source in the description.

## Storage and API

- Metadata: PostgreSQL (`vectordata_region`, `vectordata_vectordataset`).
- Files: `VECTOR_ROOT` (default `DATA_DIR/vectors`): `dataset_<id>/source/`, `dataset_<id>/tiles/{z}/{x}/{y}.pbf`.
  **Back this folder up together with the database.**
- Output: Mapbox Vector Tiles, zoom 7–14 (10–15 selectable maximum). The map redraws the deepest tiles when zoomed
  further in. 3D Tiles are not generated.
- `GET /api/vectors/active/?bbox=w,s,e,n` and `GET /api/vectors/tiles/<id>/<z>/<x>/<y>.pbf` are public (inactive
  datasets are staff only). Everything else is staff only; see `/api/docs/`.

## Settings

- `VECTOR_ROOT`, `VECTOR_MAX_UPLOAD_MB` (default 300). A reverse proxy needs a matching upload limit.
- A worker that dies is marked Failed after 20 minutes without progress. Its log is `dataset_<id>/processing.log`.

## Optional: PostGIS

Without PostGIS, coverage is filtered by bounding box in SQL and by geometry in Python. To use PostGIS, a PostgreSQL
administrator runs `psql -U postgres -d wargame -c "CREATE EXTENSION postgis;"`, then run
`python manage.py enable_postgis`. The automated tests cover only the non-PostGIS path.

## Tests

`python manage.py test vectordata`
