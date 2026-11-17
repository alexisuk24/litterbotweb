const el = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const pad = n => String(n).padStart(2, '0');

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
}

const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

let UNIT = store.get('lr-unit', 'lb');
const onUnitChange = [];
function fmtW(lb, digits) {
  if (lb == null) return '—';
  return UNIT === 'kg' ? `${(lb * 0.453592).toFixed(digits ?? 2)} kg` : `${lb.toFixed(digits ?? 2)} lb`;
}
const toUnit = lb => lb == null ? null : (UNIT === 'kg' ? lb * 0.453592 : lb);
function initUnitToggle() {
  const t = el('unitToggle');
  if (!t) return;
  const paint = () => t.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.unit === UNIT));
  t.addEventListener('click', e => {
    const u = e.target.dataset.unit;
    if (!u || u === UNIT) return;
    UNIT = u; store.set('lr-unit', u); paint();
    onUnitChange.forEach(f => f());
  });
  paint();
}

function catColor(slot) {
  if (slot == null || slot < 0) return getComputedStyle(document.documentElement).getPropertyValue('--cat-none').trim();
  return getComputedStyle(document.documentElement).getPropertyValue(`--cat-${slot % 8}`).trim();
}

function fmtDur(s) {
  if (s == null) return '—';
  s = Math.round(s);
  return s >= 60 ? `${Math.floor(s / 60)}m ${pad(s % 60)}s` : `${s}s`;
}
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fmtDay(ts) {
  return new Date(ts * 1000).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}
function fmtOffset(s) {
  const sign = s < 0 ? '−' : '+';
  s = Math.abs(Math.round(s));
  return s >= 60 ? `${sign}${Math.floor(s / 60)}m${s % 60 ? ` ${s % 60}s` : ''}` : `${sign}${s}s`;
}
function expiryText(ts) {
  const days = Math.ceil((ts - Date.now() / 1000) / 86400);
  const date = new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  return days <= 0 ? 'at the next sync' : days === 1 ? `tomorrow (${date})` : `in ${days} days (${date})`;
}
const TYPE_LABELS = {
  PET_VISIT: 'Pet visit', CYCLE_COMPLETED: 'Clean cycle', CAT_DETECT: 'Cat detected',
  CYCLE_INTERRUPTED: 'Cycle interrupted', OFFLINE: 'Offline', LITTER_LOW: 'Litter low',
};
const typeLabel = t => TYPE_LABELS[t] || t.replace(/_/g, ' ').toLowerCase().replace(/^./, c => c.toUpperCase());

document.addEventListener('DOMContentLoaded', initUnitToggle);
