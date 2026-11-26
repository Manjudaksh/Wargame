"""Map source attributes (OSM tags, Geofabrik/Natural Earth columns, layer names) to display layers."""
from __future__ import annotations

# Display layers, in the order the Admin sees them.
CATEGORIES = {
    'rivers': 'Rivers & canals',
    'water': 'Water bodies',
    'roads': 'Roads & highways',
    'bridges': 'Bridges',
    'railways': 'Railway tracks',
    'buildings': 'Buildings',
    'settlements': 'Settlements',
    'boundaries': 'Boundaries',
}
LINES, AREAS, POINTS = 'line', 'area', 'point'
ACCEPTS = {
    'rivers': {LINES}, 'water': {AREAS}, 'roads': {LINES}, 'bridges': {LINES, AREAS, POINTS},
    'railways': {LINES}, 'buildings': {AREAS}, 'settlements': {POINTS, AREAS}, 'boundaries': {LINES, AREAS},
}
# Checked in order: "waterway" must win over "water", "railway" over anything containing "way".
NAME_HINTS = [
    ('bridge', 'bridges'), ('rail', 'railways'), ('waterway', 'rivers'), ('river', 'rivers'), ('stream', 'rivers'),
    ('canal', 'rivers'), ('water', 'water'), ('lake', 'water'), ('reservoir', 'water'), ('building', 'buildings'),
    ('road', 'roads'), ('highway', 'roads'), ('street', 'roads'), ('settlement', 'settlements'), ('place', 'settlements'),
    ('village', 'settlements'), ('boundar', 'boundaries'), ('border', 'boundaries'),
]
FALSE_VALUES = {'', 'no', 'f', 'false', '0', 'none', 'null'}
PLACES = {'city', 'town', 'village', 'hamlet', 'suburb', 'isolated_dwelling'}
RIVER_VALUES = {'river', 'stream', 'canal', 'drain', 'ditch'}
ROAD_CLASSES = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified', 'residential', 'service',
                'living_street', 'track', 'path', 'footway', 'road', 'pedestrian', 'cycleway', 'bridleway', 'steps'}
WATER_CLASSES = {'water', 'reservoir', 'riverbank', 'lake', 'pond', 'wetland_water', 'dock', 'basin'}
DEAD_RAIL = {'abandoned', 'razed', 'dismantled', 'proposed'}


def _truthy(v) -> bool:
    return v is not None and str(v).strip().lower() not in FALSE_VALUES


def classify(props: dict, source_layer: str, geom_kind: str, selected: set[str]) -> list[tuple[str, str]]:
    """Return [(category, feature class)] for the categories this feature belongs to, limited to `selected`.

    A road on a bridge yields both a road and a bridge."""
    p = {str(k).lower(): v for k, v in props.items()}
    val = lambda k: str(p.get(k) or '').strip().lower()
    highway, railway, waterway = val('highway'), val('railway'), val('waterway')
    fclass = val('fclass') or val('featurecla')
    found: dict[str, str] = {}

    if val('man_made') == 'bridge' or (_truthy(p.get('bridge')) and geom_kind != AREAS):
        found['bridges'] = highway or railway or fclass or 'bridge'
    if highway:
        found['roads'] = highway
    if railway and railway not in DEAD_RAIL:
        found['railways'] = railway
    if waterway:
        if geom_kind == AREAS:
            found['water'] = waterway
        elif waterway in RIVER_VALUES:
            found['rivers'] = waterway
    if geom_kind == AREAS and (val('natural') == 'water' or _truthy(p.get('water')) or val('landuse') in ('reservoir', 'basin')):
        found['water'] = val('water') if _truthy(p.get('water')) else 'water'
    if _truthy(p.get('building')) and geom_kind == AREAS:
        found['buildings'] = val('building')
    if val('place') in PLACES:
        found['settlements'] = val('place')
    elif val('landuse') == 'residential' and geom_kind == AREAS:
        found['settlements'] = 'residential'
    if val('boundary') or _truthy(p.get('admin_level')):
        found['boundaries'] = val('boundary') or 'administrative'

    if not found.keys() - {'bridges'} and fclass:  # Geofabrik shapefiles / Natural Earth
        if fclass in RIVER_VALUES:
            found['water' if geom_kind == AREAS else 'rivers'] = fclass
        elif fclass in WATER_CLASSES and geom_kind == AREAS:
            found['water'] = fclass
        elif fclass in PLACES:
            found['settlements'] = fclass
        elif fclass.replace('_link', '') in ROAD_CLASSES or fclass.startswith('track'):
            found['roads'] = fclass
        elif 'river' in fclass:
            found['water' if geom_kind == AREAS else 'rivers'] = 'river'
        elif 'boundary' in fclass or 'line of control' in fclass:
            found['boundaries'] = fclass[:40]

    if not found:  # no recognised attributes: use the layer / file name
        name = source_layer.lower()
        hint = next((cat for key, cat in NAME_HINTS if key in name), None)
        if hint:
            found[hint] = fclass or hint
        else:  # last resort: the Admin ticked exactly one type that fits this geometry
            fits = [c for c in selected if geom_kind in ACCEPTS[c]]
            if len(fits) == 1:
                found[fits[0]] = fclass or fits[0]

    return [(c, k[:40]) for c, k in found.items() if c in selected and geom_kind in ACCEPTS[c]]


def min_zoom(category: str, cls: str) -> int:
    """First zoom level at which a feature is written into tiles (keeps low-zoom tiles small)."""
    base = cls.replace('_link', '')
    if category == 'roads':
        return {'motorway': 7, 'trunk': 7, 'primary': 8, 'secondary': 9, 'tertiary': 11, 'unclassified': 12,
                'residential': 12, 'roads': 9}.get(base, 13)
    if category == 'rivers':
        return {'river': 7, 'rivers': 7, 'canal': 10}.get(base, 12)
    if category == 'settlements':
        return {'city': 7, 'town': 9, 'village': 11, 'suburb': 12, 'residential': 11}.get(base, 13)
    return {'water': 7, 'railways': 8, 'bridges': 11, 'buildings': 13, 'boundaries': 7}[category]
