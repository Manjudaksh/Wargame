// Admin: upload, validate, preview, publish and roll back the border vector layer.
import { api, csrf, esc, toast } from './api.js';

const $ = (s) => document.querySelector(s);
const BADGE = { PUBLISHED: 'success', DRAFT: 'warning', ARCHIVED: 'secondary' };
const fmtSize = (b) => (b > 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`);
const fmtDate = (s) => (s ? new Date(s).toLocaleString() : '—');

async function load() {
  const [rows, live] = await Promise.all([api('borders/layers/'), api('borders/')]);
  const pub = rows.find((r) => r.status === 'PUBLISHED');
  $('#live').innerHTML = pub
    ? `<div><span class="badge text-bg-success">v${pub.version}</span> <b>${esc(pub.filename)}</b></div>
       <div class="text-muted2">Published ${fmtDate(pub.published_at)}</div>
       <div class="text-muted2">${pub.feature_count} features · ${pub.vertex_count.toLocaleString()} vertices · ${fmtSize(pub.size_bytes)}</div>`
    : `<div><span class="badge text-bg-secondary">built-in</span> ${esc(live.source)}</div>
       <div class="text-muted2">No upload published yet. Maps show the bundled Natural Earth data.</div>`;
  $('#rows').innerHTML = rows.length ? rows.map((r) => {
    const w = (r.report && r.report.warnings) || [];
    return `<tr>
      <td>v${r.version}</td>
      <td>${esc(r.filename)}${r.note ? `<div class="text-muted2">${esc(r.note)}</div>` : ''}</td>
      <td><span class="badge text-bg-${BADGE[r.status]}">${r.status}</span></td>
      <td>${r.feature_count}</td><td>${fmtSize(r.size_bytes)}</td>
      <td>${fmtDate(r.created)}<div class="text-muted2">${esc(r.uploaded_by || '')}</div></td>
      <td>${w.length ? `<span class="text-warning" title="${esc(w.join('\n'))}"><i class="bi bi-exclamation-triangle"></i> ${w.length}</span>` : '<span class="text-success"><i class="bi bi-check-circle"></i></span>'}</td>
      <td class="text-nowrap text-end">
        <button class="btn btn-sm btn-outline-secondary" data-act="view" data-id="${r.id}"><i class="bi bi-eye"></i></button>
        ${r.status === 'PUBLISHED' ? '' : `<button class="btn btn-sm btn-tac" data-act="publish" data-id="${r.id}">${r.status === 'ARCHIVED' ? 'Roll back' : 'Publish'}</button>
        <button class="btn btn-sm btn-outline-danger" data-act="del" data-id="${r.id}"><i class="bi bi-trash"></i></button>`}
      </td></tr>`;
  }).join('') : '<tr><td colspan="8" class="text-muted2 text-center py-3">No uploads yet.</td></tr>';
  if (pub) preview(pub.id); else preview(null, live.geojson, 'Built-in data');
}

async function preview(id, gj, caption) {
  if (id != null) {
    const l = await api(`borders/layers/${id}/`);
    gj = l.geojson;
    caption = `v${l.version} · ${l.filename} · bbox ${l.bbox.join(', ')}`;
  }
  $('#preview-cap').textContent = caption;
  draw(gj);
}

function draw(gj) {
  const c = $('#preview');
  const dpr = window.devicePixelRatio || 1;
  c.width = c.clientWidth * dpr; c.height = c.clientHeight * dpr;
  const ctx = c.getContext('2d');
  const parts = [];
  for (const f of gj.features) {
    const g = f.geometry;
    const rings = g.type === 'LineString' ? [g.coordinates] : g.coordinates;
    rings.forEach((r) => parts.push({ kind: f.properties.kind, area: g.type === 'Polygon', pts: r }));
  }
  let w = 180, s = 90, e = -180, n = -90;
  parts.forEach((p) => p.pts.forEach(([x, y]) => { w = Math.min(w, x); e = Math.max(e, x); s = Math.min(s, y); n = Math.max(n, y); }));
  if (!parts.length) return;
  const kx = Math.cos(((s + n) / 2) * Math.PI / 180);
  const pad = 14 * dpr;
  const sc = Math.min((c.width - 2 * pad) / ((e - w) * kx || 1), (c.height - 2 * pad) / ((n - s) || 1));
  const ox = (c.width - (e - w) * kx * sc) / 2, oy = (c.height - (n - s) * sc) / 2;
  const px = (p) => [ox + (p[0] - w) * kx * sc, oy + (n - p[1]) * sc];
  ctx.lineJoin = 'round';
  for (const p of parts) {
    ctx.beginPath();
    p.pts.forEach((q, i) => { const [a, b] = px(q); i ? ctx.lineTo(a, b) : ctx.moveTo(a, b); });
    if (p.area) { ctx.fillStyle = 'rgba(255,209,102,.10)'; ctx.fill(); }
    ctx.strokeStyle = p.kind === 'loc' ? '#ff8c42' : p.kind === 'area' ? '#7fd1ae' : '#ffd166';
    ctx.lineWidth = 1.4 * dpr; ctx.setLineDash(p.kind === 'loc' ? [6 * dpr, 4 * dpr] : []); ctx.stroke();
  }
}

$('#upload').onclick = async () => {
  const f = $('#file').files[0];
  const out = $('#result');
  if (!f) { out.innerHTML = '<span class="text-danger">Choose a file first.</span>'; return; }
  const fd = new FormData();
  fd.append('file', f);
  fd.append('note', $('#note').value);
  if ($('#pub').checked) fd.append('publish', '1');
  $('#upload').disabled = true;
  out.textContent = 'Validating…';
  try {
    const res = await fetch('/api/borders/layers/', { method: 'POST', headers: { 'X-CSRFToken': csrf() }, credentials: 'same-origin', body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const list = data.errors || [data.error || data.detail || `HTTP ${res.status}`];
      out.innerHTML = `<div class="text-danger fw-bold">Rejected, nothing was changed:</div><ul class="text-danger ps-3 mb-0">${list.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>`;
      return;
    }
    const w = (data.report && data.report.warnings) || [];
    out.innerHTML = `<div class="text-success">Valid: ${data.feature_count} features, ${data.vertex_count.toLocaleString()} vertices. Saved as v${data.version} (${data.status.toLowerCase()}).</div>` +
      (w.length ? `<ul class="text-warning ps-3 mb-0">${w.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : '');
    toast(data.status === 'PUBLISHED' ? `v${data.version} published, maps update automatically` : `v${data.version} saved as draft`, 'ok');
    $('#file').value = ''; $('#note').value = '';
    await load();
    if (data.status !== 'PUBLISHED') preview(data.id);
  } catch (e) {
    out.innerHTML = `<span class="text-danger">${esc(e.message)}</span>`;
  } finally {
    $('#upload').disabled = false;
  }
};

$('#rows').onclick = async (ev) => {
  const b = ev.target.closest('[data-act]');
  if (!b) return;
  const id = b.dataset.id;
  try {
    if (b.dataset.act === 'view') return await preview(id);
    if (b.dataset.act === 'publish') {
      if (!confirm('Make this version the live border layer for every map?')) return;
      const r = await api(`borders/layers/${id}/publish/`, { method: 'POST', body: {} });
      toast(`v${r.version} is now live`, 'ok');
    }
    if (b.dataset.act === 'del') {
      if (!confirm('Delete this version permanently?')) return;
      await api(`borders/layers/${id}/`, { method: 'DELETE' });
    }
    await load();
  } catch (e) { toast(e.message, 'crit'); }
};

load().catch((e) => { $('#live').textContent = e.message; });
