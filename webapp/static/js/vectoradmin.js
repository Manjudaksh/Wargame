// Admin: regions and regional vector datasets.
import { api, csrf, esc, toast } from './api.js';

const $ = (s) => document.querySelector(s);
const BADGE = { QUEUED: 'secondary', PROCESSING: 'info', READY: 'success', FAILED: 'danger' };
const MODE_LABEL = { BORDER: 'border section', GEOMETRY: 'uploaded shape', POINT: 'centre point' };
const S = { regions: [], datasets: [], categories: {}, border: null, replaces: null, editing: null, shown: null, timer: null, html: {} };

async function send(path, formData, method = 'POST') {
  const res = await fetch(`/api/vectors/${path}`, { method, headers: { 'X-CSRFToken': csrf() }, credentials: 'same-origin', body: formData });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || data.detail || `HTTP ${res.status}`);
  return data;
}
const setHtml = (sel, html) => { if (S.html[sel] !== html) { $(sel).innerHTML = html; S.html[sel] = html; } };
const message = (sel, text, cls = 'danger') => { $(sel).innerHTML = text ? `<span class="text-${cls}">${esc(text)}</span>` : ''; };

async function load() {
  clearTimeout(S.timer);
  let delay = 15000;
  try {
    const [regions, data] = await Promise.all([api('vectors/regions/'), api('vectors/datasets/')]);
    Object.assign(S, { regions, datasets: data.datasets, categories: data.categories });
    $('#u-limit').textContent = data.max_upload_mb;
    $('#engine').textContent = `Coverage queries: ${data.postgis ? 'PostGIS' : 'application (PostGIS not enabled)'}`;
    renderTypes(); renderRegions(); renderDatasets();
    $('#page-error').classList.add('d-none');
    if (data.datasets.some((d) => d.processing_status === 'QUEUED' || d.processing_status === 'PROCESSING')) delay = 3000;
  } catch (e) {
    $('#page-error').textContent = `Could not load: ${e.message}`;
    $('#page-error').classList.remove('d-none');
  }
  S.timer = setTimeout(load, delay);
}

function renderTypes() {
  if ($('#u-types').children.length) return;
  $('#u-types').innerHTML = Object.entries(S.categories).map(([k, v]) =>
    `<label class="col-6 form-check mb-0"><input class="form-check-input" type="checkbox" value="${k}"> ${esc(v)}</label>`).join('');
}

function renderRegions() {
  const sel = $('#u-region'), keep = sel.value;
  setHtml('#u-region', '<option value="">Select region…</option>' + S.regions.map((r) => `<option value="${r.id}">${esc(r.name)}</option>`).join(''));
  sel.value = keep;
  setHtml('#regions', S.regions.length ? S.regions.map((r) => `<tr>
      <td><b>${esc(r.name)}</b>${r.description ? `<div class="text-muted2">${esc(r.description)}</div>` : ''}</td>
      <td>${r.range_km} km around ${MODE_LABEL[r.coverage_mode]}<div class="text-muted2 mono">${r.bbox.map((v) => v.toFixed(2)).join(', ')}</div></td>
      <td>${r.active.length ? r.active.map((a) => `<span class="badge text-bg-success me-1">${esc(a.name)} v${a.version}</span>`).join('') : '<span class="text-muted2">none</span>'}</td>
      <td class="text-end text-nowrap">
        <button class="btn btn-sm btn-outline-secondary" data-r="view" data-id="${r.id}" title="Preview coverage"><i class="bi bi-eye"></i></button>
        <button class="btn btn-sm btn-outline-secondary" data-r="edit" data-id="${r.id}" title="Edit region"><i class="bi bi-pencil"></i></button>
        <button class="btn btn-sm btn-outline-danger" data-r="delete" data-id="${r.id}" title="Delete region" ${r.dataset_count ? 'disabled' : ''}><i class="bi bi-trash"></i></button></td></tr>`).join('')
    : '<tr><td colspan="4" class="text-muted2 text-center py-3">No regions yet. Create one first.</td></tr>');
}

function statusCell(d) {
  let html = `<span class="badge text-bg-${BADGE[d.processing_status]}">${d.processing_status}</span>`;
  if (d.is_active) html += ' <span class="badge text-bg-success">ACTIVE</span>';
  if (d.progress) html += `<div class="text-muted2">${esc(d.progress)}</div>`;
  if (d.error_message) html += `<div class="text-danger" style="max-width:340px">${esc(d.error_message)}</div>`;
  if (d.coverage_outdated) html += '<div class="text-warning">Region coverage changed: reprocess to apply it.</div>';
  for (const w of d.stats?.warnings || []) html += `<div class="text-warning" style="max-width:340px">${esc(w)}</div>`;
  return html;
}

function contentCell(d) {
  const counts = d.stats?.layers;
  if (!counts) return `<span class="text-muted2">${d.feature_types.map((t) => esc(S.categories[t] || t)).join(', ')}</span>`;
  return Object.entries(counts).map(([k, n]) => `<div>${esc(S.categories[k] || k)}: ${n.toLocaleString()}</div>`).join('') +
    `<div class="text-muted2">${d.stats.tiles.toLocaleString()} tiles · ${(d.stats.bytes / 1048576).toFixed(1)} MB · zoom ${d.min_zoom}–${d.max_zoom}</div>`;
}

function renderDatasets() {
  const btn = (act, id, label, cls = 'btn-outline-secondary') => `<button class="btn btn-sm ${cls} mb-1" data-d="${act}" data-id="${id}">${label}</button> `;
  setHtml('#datasets', S.datasets.length ? S.datasets.map((d) => {
    const busy = d.processing_status === 'QUEUED' || d.processing_status === 'PROCESSING';
    let actions = '';
    if (d.is_active) actions += btn('deactivate', d.id, 'Deactivate', 'btn-outline-warning');
    else if (d.processing_status === 'READY') actions += btn('activate', d.id, 'Activate', 'btn-tac');
    if (!busy) actions += btn('replace', d.id, 'Replace') + btn('reprocess', d.id, 'Reprocess');
    actions += btn('edit', d.id, '<i class="bi bi-pencil" title="Edit description"></i>');
    if (!d.is_active && d.processing_status !== 'PROCESSING') actions += btn('delete', d.id, '<i class="bi bi-trash" title="Delete"></i>', 'btn-outline-danger');
    return `<tr><td><b>${esc(d.dataset_name)}</b><div class="text-muted2">${esc(d.region_name)}</div>${d.description ? `<div class="text-muted2">${esc(d.description)}</div>` : ''}</td>
      <td>v${d.dataset_version}</td><td>${statusCell(d)}</td>
      <td>${esc(d.original_filename)}<div class="text-muted2">${esc(d.file_format)}${d.coordinate_reference_system ? ` · ${esc(d.coordinate_reference_system)}` : ''}</div></td>
      <td>${contentCell(d)}</td>
      <td>${new Date(d.uploaded_at).toLocaleString()}<div class="text-muted2">${esc(d.uploaded_by || '')}</div></td>
      <td class="text-end" style="min-width:210px">${actions}</td></tr>`;
  }).join('') : '<tr><td colspan="7" class="text-muted2 text-center py-3">No datasets uploaded yet.</td></tr>');
}

// ── coverage preview ─────────────────────────────────────────────────────────
function rings(geom) {
  if (!geom) return [];
  if (geom.type === 'GeometryCollection') return geom.geometries.flatMap(rings);
  const c = geom.coordinates;
  if (geom.type === 'LineString') return [c];
  if (geom.type === 'MultiLineString' || geom.type === 'Polygon') return c;
  if (geom.type === 'MultiPolygon') return c.flat();
  return [geom.type === 'Point' ? [c] : c];
}

async function preview(region) {
  S.shown = region.id;
  if (!S.border) S.border = (await api('borders/')).geojson;
  const c = $('#coverage-preview'), dpr = window.devicePixelRatio || 1;
  c.width = c.clientWidth * dpr; c.height = c.clientHeight * dpr;
  const ctx = c.getContext('2d');
  const [w, s, e, n] = region.bbox;
  const kx = Math.cos(((s + n) / 2) * Math.PI / 180);
  const sc = Math.min((c.width - 20) / ((e - w) * kx), (c.height - 20) / (n - s));
  const ox = (c.width - (e - w) * kx * sc) / 2, oy = (c.height - (n - s) * sc) / 2;
  const draw = (list, stroke, fill) => {
    ctx.beginPath();
    for (const r of list) r.forEach((p, i) => ctx[i ? 'lineTo' : 'moveTo'](ox + (p[0] - w) * kx * sc, oy + (n - p[1]) * sc));
    if (fill) { ctx.fillStyle = fill; ctx.fill('evenodd'); }
    ctx.strokeStyle = stroke; ctx.lineWidth = 1.5 * dpr; ctx.stroke();
  };
  draw(rings(region.coverage_geometry), '#36b8ff', 'rgba(54,184,255,.15)');
  for (const f of S.border.features) draw(rings(f.geometry), 'rgba(255,209,102,.7)');
  draw(rings(region.source_geometry), '#fff');
  $('#coverage-cap').textContent = `${region.name}: ${region.range_km} km coverage (blue), reference shape (white), border layer (yellow).`;
}

// ── region form ──────────────────────────────────────────────────────────────
function showMode() {
  const mode = $('#r-mode').value;
  document.querySelectorAll('[data-mode]').forEach((el) => el.classList.toggle('d-none', el.dataset.mode !== mode));
}
function editRegion(r) {
  S.editing = r;
  $('#region-title').textContent = r ? `EDIT REGION: ${r.name}` : 'NEW REGION';
  $('#r-submit').textContent = r ? 'Save region' : 'Create region';
  $('#r-cancel').classList.toggle('d-none', !r);
  $('#region-form').reset();
  $('#r-mode').querySelector('[value="KEEP"]').hidden = !r;
  if (r) { $('#r-name').value = r.name; $('#r-desc').value = r.description; $('#r-range').value = r.range_km; $('#r-mode').value = 'KEEP'; }
  showMode(); message('#r-result', '');
}
$('#r-mode').onchange = showMode;
$('#r-cancel').onclick = () => editRegion(null);
$('#region-form').onsubmit = async (ev) => {
  ev.preventDefault();
  const fd = new FormData();
  fd.append('name', $('#r-name').value); fd.append('description', $('#r-desc').value);
  const mode = $('#r-mode').value, r = S.editing;
  if (!r || mode !== 'KEEP' || +$('#r-range').value !== r.range_km) fd.append('range_km', $('#r-range').value);
  if (mode !== 'KEEP') {
    fd.append('coverage_mode', mode);
    fd.append('border_bbox', $('#r-bbox').value);
    fd.append('lon', $('#r-lon').value); fd.append('lat', $('#r-lat').value);
    if ($('#r-file').files[0]) fd.append('geometry_file', $('#r-file').files[0]);
  }
  $('#r-submit').disabled = true;
  try {
    const saved = await send(r ? `regions/${r.id}/` : 'regions/', fd, r ? 'PATCH' : 'POST');
    toast(`Region "${saved.name}" saved`, 'ok');
    editRegion(null);
    await load();
    preview(saved);
  } catch (e) { message('#r-result', e.message); } finally { $('#r-submit').disabled = false; }
};

$('#regions').onclick = async (ev) => {
  const b = ev.target.closest('[data-r]');
  if (!b) return;
  const r = S.regions.find((x) => x.id === +b.dataset.id);
  try {
    if (b.dataset.r === 'view') await preview(r);
    if (b.dataset.r === 'edit') { editRegion(r); $('#region-form').scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }
    if (b.dataset.r === 'delete' && confirm(`Delete region "${r.name}"?`)) { await api(`vectors/regions/${r.id}/`, { method: 'DELETE' }); await load(); }
  } catch (e) { toast(e.message, 'crit'); }
};

// ── upload / replace ─────────────────────────────────────────────────────────
function setReplace(d) {
  S.replaces = d;
  $('#replace-note').classList.toggle('d-none', !d);
  $('#u-region').disabled = $('#u-name').disabled = !!d;
  if (!d) return;
  $('#replace-note span').textContent = `Uploading a new version of "${d.dataset_name}" (${d.region_name}). The current version stays live until this one succeeds.`;
  $('#u-region').value = d.region_id; $('#u-name').value = d.dataset_name; $('#u-desc').value = d.description;
  $('#u-types').querySelectorAll('input').forEach((i) => (i.checked = d.feature_types.includes(i.value)));
  $('#u-activate').checked = true;
  $('#upload-form').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
$('#replace-cancel').onclick = () => { setReplace(null); $('#upload-form').reset(); };

$('#upload-form').onsubmit = async (ev) => {
  ev.preventDefault();
  const fd = new FormData();
  if (S.replaces) fd.append('replaces', S.replaces.id);
  else { fd.append('region_id', $('#u-region').value); fd.append('dataset_name', $('#u-name').value); }
  fd.append('description', $('#u-desc').value);
  $('#u-types').querySelectorAll('input:checked').forEach((i) => fd.append('feature_types', i.value));
  for (const f of $('#u-files').files) fd.append('files', f);
  fd.append('max_zoom', $('#u-zoom').value);
  fd.append('activate', $('#u-activate').checked ? '1' : '0');
  $('#u-submit').disabled = true;
  message('#u-result', 'Uploading…', 'info');
  try {
    const d = await send('datasets/', fd);
    message('#u-result', `Received v${d.dataset_version}. Processing runs in the background; watch its status in the table.`, 'success');
    setReplace(null); $('#upload-form').reset();
    await load();
  } catch (e) { message('#u-result', `Rejected, nothing was changed: ${e.message}`); } finally { $('#u-submit').disabled = false; }
};

$('#datasets').onclick = async (ev) => {
  const b = ev.target.closest('[data-d]');
  if (!b) return;
  const d = S.datasets.find((x) => x.id === +b.dataset.id), act = b.dataset.d, name = `"${d.dataset_name}" v${d.dataset_version}`;
  try {
    if (act === 'replace') return setReplace(d);
    if (act === 'edit') {
      const text = prompt(`Description for ${name}`, d.description);
      if (text === null) return;
      await api(`vectors/datasets/${d.id}/`, { method: 'PATCH', body: { description: text } });
    } else if (act === 'delete') {
      if (!confirm(`Permanently delete ${name} and its files?`)) return;
      await api(`vectors/datasets/${d.id}/`, { method: 'DELETE' });
    } else {
      if (act === 'activate' && !confirm(`Make ${name} the live version?`)) return;
      if (act === 'deactivate' && !confirm(`Remove ${name} from every map?`)) return;
      await api(`vectors/datasets/${d.id}/${act}/`, { method: 'POST', body: {} });
    }
    await load();
  } catch (e) { toast(e.message, 'crit'); }
};

load();
