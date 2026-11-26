// Regional vector tiles on the Cesium globe. Cesium cannot read MVT itself, so a custom imagery provider
// fetches the tiles in view, decodes them and rasterises them onto draped canvas tiles. All datasets and layers
// share ONE imagery layer, so the cost does not grow with the number of regions or layers.
import { decodeTile } from './mvt.js';

const POLL_MS = 30000;
const TILE_PX = 512;
const MAX_LEVEL = 18; // above a dataset's max zoom its deepest tile is redrawn at the larger scale (stays sharp)
const CACHE_TILES = 256;
const STORE_KEY = 'wg_vector_layers';

// Listed in paint order (bottom → top). min = first map level where the layer is drawn.
export const LAYER_STYLES = {
  water: { label: 'Water bodies', swatch: '#2f7fd8', fill: 'rgba(47,127,216,.55)', stroke: '#6fb4ff', width: 1.5 },
  settlements: { label: 'Settlements', swatch: '#e6b8ff', fill: 'rgba(200,150,230,.14)', stroke: 'rgba(230,184,255,.8)', width: 1.2, labels: true },
  buildings: { label: 'Buildings', swatch: '#d9c3a5', fill: 'rgba(217,195,165,.6)', stroke: '#6b5a45', width: 1 },
  rivers: { label: 'Rivers & canals', swatch: '#3d9bff', stroke: '#3d9bff', width: 3 },
  roads: { label: 'Roads & highways', swatch: '#e8e8e8', stroke: '#d0d0d0', width: 2, casing: 'rgba(20,20,20,.65)' },
  railways: { label: 'Railway tracks', swatch: '#222', stroke: '#111', width: 3, casing: '#f2f2f2', dash: [10, 8] },
  boundaries: { label: 'Boundaries', swatch: '#ff5fd2', stroke: '#ff5fd2', width: 3, casing: 'rgba(0,0,0,.55)', dash: [18, 7, 4, 7] },
  bridges: { label: 'Bridges', swatch: '#ffb000', marker: true },
};
const MAJOR_ROADS = { motorway: ['#ffd36b', 4.5], trunk: ['#ffd36b', 4], primary: ['#ffffff', 3.5], secondary: ['#f0f0f0', 3] };

function loadVisibility() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(STORE_KEY) || '{}'); } catch { /* ignore a corrupt value */ }
  return Object.fromEntries(Object.keys(LAYER_STYLES).map((k) => [k, saved[k] !== false]));
}

const webMercatorBounds = (level, x, y) => {
  const n = 2 ** level;
  const lat = (t) => (Math.atan(Math.sinh(Math.PI * (1 - (2 * t) / n))) * 180) / Math.PI;
  return [(x / n) * 360 - 180, lat(y + 1), ((x + 1) / n) * 360 - 180, lat(y)];
};
const overlaps = (a, b) => a[0] <= b[2] && a[2] >= b[0] && a[1] <= b[3] && a[3] >= b[1];

function trace(ctx, path, map) {
  for (let i = 0; i < path.length; i += 2) {
    const [x, y] = map(path[i], path[i + 1]);
    if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
  }
}

function bridgeMarker(ctx, x, y) {
  ctx.setLineDash([]);
  ctx.beginPath(); ctx.arc(x, y, 9, 0, 2 * Math.PI);
  ctx.fillStyle = '#ffb000'; ctx.fill();
  ctx.lineWidth = 2.5; ctx.strokeStyle = '#1b1200'; ctx.stroke();
  ctx.beginPath(); // arch glyph
  ctx.moveTo(x - 5.5, y + 3); ctx.quadraticCurveTo(x, y - 7, x + 5.5, y + 3);
  ctx.moveTo(x - 6, y - 3); ctx.lineTo(x + 6, y - 3);
  ctx.lineWidth = 2; ctx.stroke();
}

function label(ctx, text, x, y) {
  ctx.setLineDash([]);
  ctx.font = '600 19px Rajdhani, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.lineWidth = 4; ctx.strokeStyle = 'rgba(0,0,0,.8)'; ctx.strokeText(text, x, y);
  ctx.fillStyle = '#fff'; ctx.fillText(text, x, y);
}

/** Paint one layer of one decoded tile. map(x, y) converts tile units → canvas pixels. */
function paintLayer(ctx, name, layer, map, labels) {
  const st = LAYER_STYLES[name];
  for (const f of layer.features) {
    if (st.marker || (f.type === 1)) {
      for (const p of f.paths) {
        const [x, y] = map(p[0], p[1]);
        if (st.marker) bridgeMarker(ctx, x, y);
        else if (st.labels && f.props.name) labels.push([String(f.props.name), x, y]);
        else { ctx.beginPath(); ctx.arc(x, y, 4, 0, 2 * Math.PI); ctx.fillStyle = st.swatch; ctx.fill(); }
      }
      continue;
    }
    ctx.beginPath();
    for (const p of f.paths) trace(ctx, p, map);
    if (f.type === 3 && st.fill) { ctx.fillStyle = st.fill; ctx.fill('evenodd'); }
    let stroke = st.stroke, width = st.width;
    if (name === 'roads') {
      const major = MAJOR_ROADS[String(f.props.class || '').replace('_link', '')];
      if (major) [stroke, width] = major;
    }
    if (f.type === 3 && st.fill) { // outline polygons only where the edge is a real edge, not a tile clip
      ctx.beginPath();
      for (const p of f.paths) {
        for (let i = 2; i < p.length; i += 2) {
          const ax = p[i - 2], ay = p[i - 1], bx = p[i], by = p[i + 1];
          const clipped = (ax === bx && (ax < 0 || ax > layer.extent)) || (ay === by && (ay < 0 || ay > layer.extent));
          if (!clipped) { ctx.moveTo(...map(ax, ay)); ctx.lineTo(...map(bx, by)); }
        }
      }
    }
    if (st.casing) { ctx.setLineDash([]); ctx.strokeStyle = st.casing; ctx.lineWidth = width + 2.5; ctx.stroke(); }
    ctx.setLineDash(st.dash || []); ctx.strokeStyle = stroke; ctx.lineWidth = width; ctx.stroke();
  }
  ctx.setLineDash([]);
}

class VectorImageryProvider {
  constructor(manager, datasets, visible) {
    this.manager = manager; this.datasets = datasets; this.visible = visible;
    this.tilingScheme = new Cesium.WebMercatorTilingScheme();
    const w = Math.min(...datasets.map((d) => d.bbox[0])), s = Math.min(...datasets.map((d) => d.bbox[1]));
    const e = Math.max(...datasets.map((d) => d.bbox[2])), n = Math.max(...datasets.map((d) => d.bbox[3]));
    this.rectangle = Cesium.Rectangle.fromDegrees(w, Math.max(-85, s), e, Math.min(85, n));
    this.tileWidth = TILE_PX; this.tileHeight = TILE_PX;
    this.minimumLevel = 0; this.maximumLevel = MAX_LEVEL;
    this.errorEvent = new Cesium.Event();
    this.hasAlphaChannel = true;
    this.credit = undefined; this.proxy = undefined; this.tileDiscardPolicy = undefined;
    this.blank = document.createElement('canvas'); this.blank.width = this.blank.height = 1;
  }
  getTileCredits() { return undefined; }
  pickFeatures() { return undefined; }

  async requestImage(x, y, level) {
    const bounds = webMercatorBounds(level, x, y);
    const jobs = this.datasets.filter((d) => level >= d.min_zoom && overlaps(bounds, d.bbox)).map(async (d) => {
      const z = Math.min(level, d.max_zoom), shift = level - z;
      const tx = Math.floor(x / 2 ** shift), ty = Math.floor(y / 2 ** shift);
      const tile = await this.manager.tile(d, z, tx, ty);
      // which part of the parent tile this canvas shows
      return tile && { tile, scale: 2 ** shift, ox: x - tx * 2 ** shift, oy: y - ty * 2 ** shift };
    });
    const parts = (await Promise.all(jobs)).filter(Boolean);
    if (!parts.length) return this.blank;
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = TILE_PX;
    const ctx = canvas.getContext('2d');
    ctx.lineJoin = 'round'; ctx.lineCap = 'butt';
    const labels = []; // drawn last so no line crosses a place name
    for (const name of Object.keys(LAYER_STYLES)) {
      if (!this.visible[name]) continue;
      for (const { tile, scale, ox, oy } of parts) {
        const layer = tile[name];
        if (!layer) continue;
        const k = (TILE_PX * scale) / layer.extent;
        paintLayer(ctx, name, layer, (px, py) => [px * k - ox * TILE_PX, py * k - oy * TILE_PX], labels);
      }
    }
    for (const [text, lx, ly] of labels) label(ctx, text, lx, ly);
    return canvas;
  }
}

class VectorLayers {
  constructor(g, bbox) {
    this.g = g; this.bbox = bbox;
    this.visible = loadVisibility();
    this.datasets = []; this.revision = null; this.layer = null;
    this.cache = new Map();
    this.status = 'loading'; this.failedTiles = 0;
    this.listeners = new Set();
    this.refresh();
    this.timer = setInterval(() => (g.viewer.isDestroyed() ? this.destroy() : this.refresh()), POLL_MS);
  }

  onChange(fn) { this.listeners.add(fn); fn(this); return () => this.listeners.delete(fn); }
  emit() { for (const fn of this.listeners) fn(this); }

  /** Layers present in at least one active dataset of this area. */
  available() { return Object.keys(LAYER_STYLES).filter((k) => this.datasets.some((d) => d.layers.includes(k))); }

  async refresh() {
    if (this.busy) return;
    this.busy = true;
    try {
      const res = await fetch(`/api/vectors/active/?bbox=${this.bbox.map((v) => v.toFixed(5)).join(',')}`, { cache: 'no-cache' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (this.g.viewer.isDestroyed()) return;
      const changed = data.revision !== this.revision;
      const recovered = this.status === 'error';
      this.status = 'ok';
      if (changed) {
        this.revision = data.revision;
        this.datasets = data.datasets;
        const live = new Set(this.datasets.map((d) => d.id));
        for (const key of this.cache.keys()) if (!live.has(Number(key.split('/')[0]))) this.cache.delete(key);
        this.failedTiles = 0;
        this.rebuild();
        this.showCredits();
      }
      if (changed || recovered) this.emit();
    } catch (e) { // keep what is already on the map; try again at the next poll
      console.warn('vector datasets', e);
      if (this.status !== 'error') { this.status = 'error'; this.emit(); }
    } finally {
      this.busy = false;
    }
  }

  tile(dataset, z, x, y) {
    const key = `${dataset.id}/${z}/${x}/${y}`;
    let hit = this.cache.get(key);
    if (hit) { this.cache.delete(key); this.cache.set(key, hit); return hit; } // LRU: move to newest
    hit = fetch(dataset.tiles.replace('{z}', z).replace('{x}', x).replace('{y}', y), { credentials: 'same-origin' })
      .then(async (res) => {
        if (res.status === 204) return null;
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return decodeTile(await res.arrayBuffer());
      })
      .catch((e) => {
        this.cache.delete(key);
        if (this.failedTiles++ === 0) { console.warn('vector tile', key, e); this.emit(); }
        return null;
      });
    this.cache.set(key, hit);
    if (this.cache.size > CACHE_TILES) this.cache.delete(this.cache.keys().next().value);
    return hit;
  }

  /** Data licences (e.g. OpenStreetMap) require a visible credit next to the map's other attributions. */
  showCredits() {
    const text = [...new Set(this.datasets.map((d) => d.attribution).filter(Boolean))].join(' · ');
    const line = this.g.viewer.container.parentElement?.querySelector('.attrib') || this.g.viewer.container;
    let el = line.querySelector('.vector-credit');
    if (!el) {
      if (!text) return;
      el = document.createElement('span');
      el.className = line.classList.contains('attrib') ? 'vector-credit' : 'vector-credit vector-credit-corner';
      line.appendChild(el);
    }
    el.textContent = text && ` · ${text}`;
  }

  /** Swap in a new imagery layer (the old one and its GPU textures are destroyed). */
  rebuild() {
    const layers = this.g.viewer.imageryLayers;
    const old = this.layer;
    this.layer = null;
    if (this.datasets.length && this.available().some((k) => this.visible[k])) {
      const provider = new VectorImageryProvider(this, this.datasets, { ...this.visible });
      this.layer = new Cesium.ImageryLayer(provider);
      layers.add(this.layer, Math.min(1, layers.length)); // above the base imagery, below zones / grid / borders
    }
    if (old) layers.remove(old, true);
  }

  setVisible(name, on) {
    this.visible[name] = on;
    localStorage.setItem(STORE_KEY, JSON.stringify(this.visible));
    this.rebuild();
  }

  destroy() {
    clearInterval(this.timer);
    this.cache.clear();
    this.listeners.clear();
    if (this.layer && !this.g.viewer.isDestroyed()) this.g.viewer.imageryLayers.remove(this.layer, true);
    this.layer = null;
  }
}

/** Show the active datasets that intersect the battle area of this globe. */
export function attachVectorLayers(g) {
  const [w, s] = g.frame.toGeo(-g.halfM, -g.halfM), [e, n] = g.frame.toGeo(g.halfM, g.halfM);
  return new VectorLayers(g, [w, s, e, n]);
}

/** Layer switches for the layer menu; re-rendered whenever the published datasets change. */
export function mountLayerToggles(container, vectors) {
  if (!container || !vectors) return;
  vectors.onChange((v) => {
    container.replaceChildren();
    const note = (text, cls = 'text-muted2') => {
      const el = document.createElement('div');
      el.className = `${cls} small`; el.textContent = text; container.appendChild(el);
    };
    if (v.status === 'loading') return note('Loading geographic layers…');
    if (v.status === 'error') note('Geographic layers could not be refreshed; retrying.', 'text-warning');
    if (!v.datasets.length) return v.status === 'ok' ? note('No geographic datasets for this area.') : undefined;
    for (const name of v.available()) {
      const wrap = document.createElement('div');
      wrap.className = 'form-check form-switch';
      wrap.innerHTML = '<input class="form-check-input" type="checkbox"><label class="form-check-label"><span class="vector-swatch"></span> </label>';
      const input = wrap.querySelector('input'), lab = wrap.querySelector('label');
      input.id = `v-${name}`; lab.htmlFor = input.id; input.checked = v.visible[name];
      input.onchange = () => v.setVisible(name, input.checked);
      wrap.querySelector('.vector-swatch').style.background = LAYER_STYLES[name].swatch;
      lab.appendChild(document.createTextNode(LAYER_STYLES[name].label));
      container.appendChild(wrap);
    }
    note([...new Set(v.datasets.map((d) => `${d.region}: ${d.name} v${d.version}`))].join(' · '));
    for (const credit of new Set(v.datasets.map((d) => d.attribution).filter(Boolean))) note(credit);
    if (v.failedTiles) note('Some map tiles failed to load.', 'text-warning');
  });
}
