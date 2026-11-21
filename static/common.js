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

// Viewers (lr_auth) can look but not change anything; the server enforces it, this just hides the controls
const CAN_EDIT = !window.LR_USER || window.LR_USER.role === 'admin';
const UNIT = window.LR_UNIT === 'lb' ? 'lb' : 'kg';
function fmtW(lb, digits) {
  if (lb == null) return '—';
  return UNIT === 'kg' ? `${(lb * 0.453592).toFixed(digits ?? 2)} kg` : `${lb.toFixed(digits ?? 2)} lb`;
}
const toUnit = lb => lb == null ? null : (UNIT === 'kg' ? lb * 0.453592 : lb);

function catColor(slot) {
  if (slot == null || slot < 0) return getComputedStyle(document.documentElement).getPropertyValue('--cat-none').trim();
  return getComputedStyle(document.documentElement).getPropertyValue(`--cat-${slot % 8}`).trim();
}

// Cat photo ringed in the cat's identity color, or the plain color dot without a photo.
function catBadge(pet, slot, size = 18) {
  const c = catColor(slot);
  if (pet && pet.image) {
    return `<img class="avatar" src="${esc(pet.image)}" alt="" loading="lazy" style="--c:${c};width:${size}px;height:${size}px" onerror="this.outerHTML='<span class=&quot;cat-dot&quot; style=&quot;background:${c}&quot;></span>'">`;
  }
  return `<span class="cat-dot" style="background:${c}"></span>`;
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
// What retention does to a clip next, from the API's keep fields.
function retentionNote(c) {
  const tag = c.small ? 'Small copy' : '';
  if (c.keep) return 'Kept forever';
  if (!c.expires) return tag;
  const step = c.next === 'small' ? `Becomes a small copy ${expiryText(c.expires)}`
                                  : `Video removed ${expiryText(c.expires)} (picture kept)`;
  return tag ? `${tag} · ${step}` : step;
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

