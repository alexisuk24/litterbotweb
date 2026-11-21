const UNKNOWN = '__unknown__';
const PAGE = 100;
const s = {
  meta: null, pets: {}, events: [], total: 0, page: 0,
  active: null, detail: null, clipIdx: 0,
  f: { type: 'PET_VISIT', pet: '', filter: '', from: '', to: '', robot: '' },
};

const petName = id => id === UNKNOWN ? 'Unknown' : (s.pets[id]?.name ?? 'No cat');
const petSlot = id => s.pets[id]?.slot ?? -1;
function catChip(id, extra = '') {
  return `<span class="chip">${catBadge(s.pets[id], petSlot(id), 16)}${esc(petName(id))}${extra}</span>`;
}

// ---------------------------------------------------------------- filters / URL

function readUrl() {
  const p = new URLSearchParams(location.search);
  for (const k of Object.keys(s.f)) if (p.has(k)) s.f[k] = p.get(k);
  return p.get('id');
}
function writeUrl() {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(s.f)) if (v && !(k === 'type' && v === 'PET_VISIT')) p.set(k, v);
  if (s.active && (!narrow.matches || sheetOpen())) p.set('id', s.active);
  history.replaceState(null, '', `${location.pathname}${p.toString() ? '?' + p : ''}`);
}

function buildFilters() {
  const types = s.meta.types.length ? s.meta.types : ['PET_VISIT'];
  el('fType').innerHTML = [...new Set(['PET_VISIT', ...types])].map(t =>
    `<option value="${esc(t)}">${esc(t === 'PET_VISIT' ? 'Pet visits' : typeLabel(t))}</option>`).join('') +
    '<option value="ALL">All activity</option>';
  el('fType').value = s.f.type;
  el('fPet').innerHTML = '<option value="">All cats</option>' +
    s.meta.pets.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('') +
    '<option value="none">No cat assigned</option>';
  el('fPet').value = s.f.pet;
  el('fFrom').value = s.f.from; el('fTo').value = s.f.to;
  if (s.meta.robots.length > 1) {
    el('fRobot').hidden = false;
    el('fRobot').innerHTML = '<option value="">All robots</option>' +
      s.meta.robots.map(r => `<option value="${esc(r.serial)}">${esc(r.name)}</option>`).join('');
    el('fRobot').value = s.f.robot;
  }
  el('fFilter').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.v === s.f.filter));

  const bind = (id, key) => el(id).addEventListener('change', e => { s.f[key] = e.target.value; reload(); });
  bind('fType', 'type'); bind('fPet', 'pet'); bind('fFrom', 'from'); bind('fTo', 'to'); bind('fRobot', 'robot');
  el('filtBtn').addEventListener('click', () => {
    const open = el('moreFilters').classList.toggle('open');
    el('filtBtn').setAttribute('aria-expanded', open);
  });
  el('clearFilters').addEventListener('click', () => {
    Object.assign(s.f, { type: 'PET_VISIT', pet: '', from: '', to: '', robot: '' });
    el('fType').value = 'PET_VISIT'; el('fPet').value = ''; el('fFrom').value = ''; el('fTo').value = '';
    if (!el('fRobot').hidden) el('fRobot').value = '';
    reload();
  });
  el('fFilter').addEventListener('click', e => {
    const v = e.target.closest('button')?.dataset.v;
    if (v === undefined) return;
    s.f.filter = v;
    el('fFilter').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
    reload();
  });
}

// ---------------------------------------------------------------- list

async function fetchPage(page) {
  const p = new URLSearchParams({ page, size: PAGE });
  for (const [k, v] of Object.entries(s.f)) if (v) p.set(k, v);
  return api(`/api/events?${p}`);
}

function paintFilterCount() {
  const n = [s.f.type !== 'PET_VISIT', s.f.pet, s.f.from, s.f.to, s.f.robot].filter(Boolean).length;
  el('filtN').textContent = n;
  el('filtN').hidden = !n;
  el('clearFilters').hidden = !n;
}

async function reload() {
  writeUrl();
  paintFilterCount();
  el('list').innerHTML = '<div class="empty">Loading…</div>';
  try {
    const r = await fetchPage(0);
    s.events = r.events; s.total = r.total; s.page = 0;
  } catch (e) {
    el('list').innerHTML = `<div class="empty">Couldn't load events (${esc(e.message)})</div>`;
    return;
  }
  renderList();
}

async function loadMore() {
  const r = await fetchPage(s.page + 1);
  s.page = r.page; s.events.push(...r.events);
  renderList();
}

const pct = x => `${Math.round(x * 100)}%`;

// The camera's own guess for a visit: just the score when it agrees with the
// assigned cat, the cat's name too (highlighted) when it doesn't.
function cameraTag(v) {
  const c = v.camera;
  if (!c) return '';
  const title = `Camera AI matched ${petName(c.pet)} (${pct(c.similarity)} similar, ${c.count} detection${c.count === 1 ? '' : 's'})`;
  return `<span class="cam${c.agrees ? '' : ' cam-diff'}" title="${esc(title)}">📷 ${c.agrees ? '' : esc(petName(c.pet)) + ' '}${pct(c.similarity)}</span>`;
}

function evRow(v) {
  const isVisit = v.type === 'PET_VISIT';
  const thumb = v.thumb ? `style="background-image:url('${esc(v.thumb)}')"` : '';
  const badges = [];
  if (v.corrected) badges.push('<span class="badge edit">Edited</span>');
  if (v.review) badges.push(`<span class="badge warn" title="${v.pet ? `Weight is closer to ${esc(petName(v.suggested))}` : 'No cat assigned'}">Check</span>`);
  const details = isVisit ? [
    v.weight != null ? fmtW(v.weight) : null,
    v.duration != null ? fmtDur(v.duration) : null,
    v.waste_type || null,
  ].filter(Boolean) : [typeLabel(v.type), v.subtype].filter(Boolean);
  return `<div class="ev${v.id === s.active ? ' active' : ''}" data-id="${esc(v.id)}">
    <div class="thumb" ${thumb}>${v.thumb ? '' : (v.clip_count ? '▶' : '—')}</div>
    <div class="main">
      <div class="row1"><span class="time">${fmtTime(v.ts)}</span>${isVisit ? catChip(v.pet) : `<span class="muted" style="font-size:.85rem">${esc(typeLabel(v.type))}</span>`}</div>
      <div class="row2">${details.map(esc).join('<span>·</span>')}${cameraTag(v)}</div>
    </div>
    <div class="side">${badges.join('')}${v.clip_count > 1 ? `<span class="muted" style="font-size:.7rem">${v.clip_count} clips</span>` : ''}</div>
  </div>`;
}

function renderList() {
  el('count').textContent = `${s.total} event${s.total === 1 ? '' : 's'}`;
  if (!s.events.length) {
    const msg = s.meta.last_activity ? 'No events match these filters' :
      'No activity yet. The collector fills this in on its next run.';
    el('list').innerHTML = `<div class="empty"><div class="empty-icon">🐾</div><div>${msg}</div></div>`;
    return;
  }
  let h = '', day = null;
  const counts = {};
  for (const v of s.events) { const d = fmtDay(v.ts); counts[d] = (counts[d] || 0) + 1; }
  for (const v of s.events) {
    const d = fmtDay(v.ts);
    if (d !== day) { day = d; h += `<div class="day-head"><span>${esc(d)}</span><span>${counts[d]}</span></div>`; }
    h += evRow(v);
  }
  if (s.events.length < s.total) h += `<div class="more"><button class="btn" id="moreBtn">Load more (${s.total - s.events.length} left)</button></div>`;
  el('list').innerHTML = h;
  el('moreBtn')?.addEventListener('click', loadMore);
}

el('list').addEventListener('click', e => {
  const row = e.target.closest('.ev');
  if (row) select(row.dataset.id);
});

function updateRow(v) {
  const i = s.events.findIndex(x => x.id === v.id);
  if (i === -1) return;
  s.events[i] = { ...s.events[i], ...v, thumb: s.events[i].thumb, clip_count: s.events[i].clip_count };
  const node = el('list').querySelector(`.ev[data-id="${CSS.escape(v.id)}"]`);
  if (node) node.outerHTML = evRow(s.events[i]);
}

// ---------------------------------------------------------------- detail

// ---------------------------------------------------------------- phone sheet

// On narrow screens the detail opens as a full-screen sheet over the list. Opening
// it adds a history entry so the phone's Back gesture closes it instead of leaving.
const narrow = matchMedia('(max-width: 860px)');
const sheetOpen = () => document.body.classList.contains('sheet-open');
let sheetEntry = false;

function openSheet() {
  if (!narrow.matches || sheetOpen()) return;
  document.body.classList.add('sheet-open');
  history.pushState(null, '', location.href);
  sheetEntry = true;
}
function closeSheet() {
  if (!sheetOpen()) return;
  document.body.classList.remove('sheet-open');
  el('detailPane').querySelector('video')?.pause();
  writeUrl();
  // Prev/next may have moved on from the row that was tapped; bring that one into view.
  el('list').querySelector(`.ev[data-id="${CSS.escape(s.active)}"]`)?.scrollIntoView({ block: 'nearest' });
}
function paintSheetHead() {
  const v = s.detail.event;
  el('sheetTitle').textContent = `${fmtDay(v.ts)} · ${fmtTime(v.ts)}`;
  const i = s.events.findIndex(x => x.id === s.active);
  el('sheetPrev').disabled = i <= 0;
  el('sheetNext').disabled = i === -1 || i >= s.events.length - 1;
}
el('sheetClose').addEventListener('click', () => sheetEntry ? history.back() : closeSheet());
el('sheetPrev').addEventListener('click', () => step(-1));
el('sheetNext').addEventListener('click', () => step(1));
window.addEventListener('popstate', () => { sheetEntry = false; closeSheet(); });
narrow.addEventListener('change', () => { if (!narrow.matches) document.body.classList.remove('sheet-open'); });

async function select(id, { scroll = true } = {}) {
  s.active = id;
  openSheet();
  writeUrl();
  el('list').querySelectorAll('.ev').forEach(n => n.classList.toggle('active', n.dataset.id === id));
  if (scroll) el('list').querySelector(`.ev[data-id="${CSS.escape(id)}"]`)?.scrollIntoView({ block: 'nearest' });
  try {
    s.detail = await api(`/api/events/${encodeURIComponent(id)}`);
  } catch (e) {
    el('detail').innerHTML = `<div class="empty">Couldn't load event (${esc(e.message)})</div>`;
    return;
  }
  if (s.active !== id) return;
  s.clipIdx = Math.max(0, s.detail.clips.findIndex(c => c.video));
  const busyClip = s.detail.clips.find(dlBusy);
  if (busyClip) watchDownload(busyClip.id);
  renderDetail();
  paintSheetHead();
  el('detailPane').scrollTop = 0;
}

const dlBusy = c => c && c.download && (c.download.status === 'pending' || c.download.status === 'sending');

function downloadStatus(c) {
  const d = c.download;
  if (!d) return '';
  if (dlBusy(c)) return '<span class="dl-status busy">⟳ Downloading from Whisker…</span>';
  if (d.status === 'error') return `<span class="dl-status err">⚠ ${esc(d.error || 'Download failed')}</span>`;
  if (Date.now() / 1000 - d.updated < 600) return '<span class="dl-status ok">✓ Downloaded again</span>';
  return '';
}

function keepBar(c) {
  if (!c) return '';
  const busy = dlBusy(c);
  const label = c.video ? '↻ Re-download' : '⬇ Download now';
  const dl = `<button class="btn" id="dlBtn" ${busy ? 'disabled' : ''} title="Fetch a fresh copy of this clip from Whisker">${busy ? 'Downloading…' : label}</button>`;
  const keep = c.video
    ? `<button class="btn keep-btn${c.keep ? ' on' : ''}" id="keepBtn" data-id="${esc(c.id)}">📌 ${c.keep ? 'Kept' : 'Keep forever'}</button>` : '';
  const when = c.video ? (c.keep ? 'Kept forever' : c.expires ? `Auto-deletes ${expiryText(c.expires)}` : '') : '';
  return `<div class="keepbar">${keep}${dl}<span class="expiry">${esc(when)}</span>${downloadStatus(c)}</div>`;
}

function renderPlayer() {
  const c = s.detail.clips[s.clipIdx];
  let body;
  if (!c) body = `<div class="novideo"><div class="empty-icon">📹</div>No camera clip recorded near this event</div>`;
  else if (c.deleted && !c.video) body = `<div class="novideo"><div class="empty-icon">🗑</div>This clip was deleted by the retention schedule</div>`;
  else if (!c.video) body = `<div class="novideo"><div class="empty-icon">⏳</div>This clip hasn't been downloaded yet<br><span style="font-size:.75rem">The downloader picks it up on its next run, or use Download now.</span></div>`;
  else body = `<video controls autoplay muted playsinline preload="metadata" src="${esc(c.video)}"></video>`;
  return `<div id="playerWrap">${body}${keepBar(c)}</div>`;
}

let dlPoll = null;
async function requestDownload() {
  const c = s.detail?.clips[s.clipIdx];
  if (!c || dlBusy(c)) return;
  try {
    const r = await fetch(`/api/clips/${encodeURIComponent(c.id)}/download`, { method: 'POST' });
    const body = await r.json();
    c.download = body.download || { status: 'error', error: "Couldn't start the download", updated: Date.now() / 1000 };
  } catch {
    c.download = { status: 'error', error: "Couldn't reach the server", updated: Date.now() / 1000 };
  }
  el('playerWrap').querySelector('.keepbar').outerHTML = keepBar(c);
  watchDownload(c.id);
}

// Poll until the download finishes, then swap in the fresh video (its URL changes with the file).
function watchDownload(clipId) {
  clearTimeout(dlPoll);
  const eventId = s.active;
  dlPoll = setTimeout(async () => {
    if (s.active !== eventId) return;
    let r;
    try { r = await api(`/api/events/${encodeURIComponent(eventId)}`); } catch { return watchDownload(clipId); }
    if (s.active !== eventId) return;
    const fresh = r.clips.find(x => x.id === clipId);
    const i = s.detail.clips.findIndex(x => x.id === clipId);
    if (!fresh || i === -1) return;
    s.detail.clips[i] = fresh;
    if (dlBusy(fresh)) {
      if (i === s.clipIdx) el('playerWrap').querySelector('.keepbar').outerHTML = keepBar(fresh);
      return watchDownload(clipId);
    }
    if (i === s.clipIdx) el('playerWrap').outerHTML = renderPlayer();
    const strip = el('strip')?.querySelector(`.c[data-i="${i}"] .img`);
    if (strip && fresh.thumb) { strip.style.backgroundImage = `url('${fresh.thumb}')`; strip.textContent = ''; }
  }, 2000);
}

async function toggleKeep() {
  const c = s.detail?.clips[s.clipIdx];
  if (!c) return;
  const r = await api(`/api/clips/${encodeURIComponent(c.id)}/keep`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ keep: !c.keep }),
  });
  Object.assign(c, r);
  const bar = el('playerWrap').querySelector('.keepbar');
  if (bar) bar.outerHTML = keepBar(c);
  el('strip')?.querySelector(`.c[data-i="${s.clipIdx}"] .pin`)?.toggleAttribute('hidden', !c.keep);
}

function renderStrip() {
  const clips = s.detail.clips;
  if (clips.length <= 1) return '';
  let h = '', sawFar = false;
  clips.forEach((c, i) => {
    if (!c.match && !sawFar) { sawFar = true; if (i) h += '<span class="sep">nearby</span>'; }
    h += `<div class="c${i === s.clipIdx ? ' on' : ''}${c.match ? '' : ' far'}" data-i="${i}" title="${esc(new Date(c.ts * 1000).toLocaleString())}">
      <div class="img" ${c.thumb ? `style="background-image:url('${esc(c.thumb)}')"` : ''}>${c.thumb ? '' : (c.video ? '▶' : c.deleted ? 'deleted' : 'not downloaded')}</div>
      <div class="lbl"><span>${fmtOffset(c.offset_s)}<span class="pin" title="Kept forever" ${c.keep ? '' : 'hidden'}> 📌</span></span><span>${c.duration_s ? fmtDur(c.duration_s) : ''}</span></div>
    </div>`;
  });
  return `<div class="strip" id="strip">${h}</div>`;
}

function pushStatus(v) {
  const p = v.push;
  if (!p) return '';
  const target = esc(p.to_pet ? petName(p.to_pet) : 'no cat');
  if (p.status === 'pending' || p.status === 'sending')
    return `<div class="push pending">⟳ Sending to Whisker: ${target}…</div>`;
  if (p.status === 'error' && !v.whisker_editable)
    return `<div class="push err">⚠ Not changed in Whisker: it only accepts changes to visits from the last 7 days. Your correction is saved here.</div>`;
  if (p.status === 'error')
    return `<div class="push err">⚠ Whisker update failed: ${esc(p.error || 'unknown error')} <button class="btn" id="retryBtn">Retry</button></div>`;
  return `<div class="push ok">✓ Updated in the Whisker app (${target})</div>`;
}

function pickHtml(v) {
  const ordered = [...s.meta.pets].sort((a, b) => (b.active - a.active) || (a.slot - b.slot));
  const btn = (id, label, sub, key) => {
    const on = id === UNKNOWN ? !v.pet : v.pet === id;
    const dot = id === UNKNOWN ? '' : catBadge(s.pets[id], petSlot(id), 30);
    return `<button data-pet="${esc(id)}" class="${on ? 'on' : ''}">${dot}<span>${esc(label)}${sub ? `<span class="sub">${sub}</span>` : ''}</span>${key ? `<kbd>${key}</kbd>` : ''}</button>`;
  };
  const buttons = ordered.map((p, i) => {
    const subs = [];
    if (p.ref_weight) subs.push(`usually ${fmtW(p.ref_weight)}`);
    if (p.id === v.suggested && v.weight) subs.push('closest weight');
    if (p.id === v.api_pet && v.corrected) subs.push('robot said');
    if (v.camera && p.id === v.camera.pet) subs.push(`camera ${pct(v.camera.similarity)}`);
    return btn(p.id, p.name, subs.join(' · '), i < 9 ? i + 1 : '');
  }).join('') + btn(UNKNOWN, 'Unknown / other', '', '0');

  let callout = '';
  if (v.review) {
    callout = v.pet
      ? `<div class="callout">This visit weighed ${fmtW(v.weight)}, which is closer to ${esc(petName(v.suggested))} than ${esc(petName(v.pet))}. Check the clip.</div>`
      : `<div class="callout">No cat was assigned to this visit.${v.suggested ? ` By weight it looks like ${esc(petName(v.suggested))}.` : ''}</div>`;
  }
  const camLine = (v.camera_detections || []).length
    ? `<div class="camline">📷 Camera AI: ${v.camera_detections.map(d =>
        `<span title="${esc(new Date(d.ts * 1000).toLocaleTimeString())}">${esc(petName(d.pet))} ${pct(d.similarity)}</span>`).join(', ')}
       <span class="muted">(similarity to each cat's profile; higher is a closer match)</span></div>`
    : '';
  const recorded = `Robot recorded: ${esc(v.api_pet ? petName(v.api_pet) : 'no cat')}` +
    (v.whisker_pet !== v.api_pet ? ` · Whisker now: ${esc(v.whisker_pet ? petName(v.whisker_pet) : 'no cat')}` : '');
  const wb = s.meta.writeback;
  const tooOld = wb && v.whisker_editable === false;
  return `<div class="section">
    <h3><span>Who was this?</span><span class="saved" id="saved">Saved</span></h3>
    ${callout}
    <div class="pick" id="pick">${buttons}</div>
    <div class="actions">
      ${(v.corrected || v.note) ? `<button class="btn" id="revertBtn">Revert to what the robot recorded <kbd>u</kbd></button>` : ''}
      <span class="muted" style="font-size:.78rem">${recorded}</span>
    </div>
    ${camLine}
    ${pushStatus(v)}
    <div style="margin-top:.75rem"><textarea class="note" id="note" placeholder="Note (optional), e.g. 'both cats in at once'">${esc(v.note)}</textarea></div>
    <div class="hint">${tooOld
      ? 'This visit is more than 7 days old, so Whisker won\'t accept changes to it. Corrections are saved in this app only.'
      : wb ? 'Cat changes are also sent to the Whisker app; notes stay here.'
      : 'Edits are kept in this app only; Whisker sync is off.'}
      <a href="/settings" style="color:inherit">Change in Settings</a>.</div>
  </div>`;
}

let pushPoll = null;
function renderPick() {
  const v = s.detail.event;
  const wrap = el('pickWrap');
  if (wrap) wrap.innerHTML = pickHtml(v);
  clearTimeout(pushPoll);
  if (v.push && (v.push.status === 'pending' || v.push.status === 'sending')) {
    const id = v.id;
    pushPoll = setTimeout(async () => {
      if (s.active !== id) return;
      try {
        const r = await api(`/api/events/${encodeURIComponent(id)}`);
        if (s.active !== id) return;
        s.detail.event = r.event;
        updateRow(r.event);
        renderPick();
      } catch {}
    }, 2000);
  }
}

function renderDetail() {
  const { event: v, nearby } = s.detail;
  const isVisit = v.type === 'PET_VISIT';
  const facts = [
    !narrow.matches && ['When', `${fmtDay(v.ts)} ${fmtTime(v.ts)}`],  // the sheet title shows it on phones
    isVisit && ['Weight', fmtW(v.weight)],
    isVisit && ['Duration', fmtDur(v.duration)],
    isVisit && ['Waste', v.waste_type || '—'],
    !isVisit && ['Type', typeLabel(v.type)],
    v.subtype && ['Detail', v.subtype],
    s.meta.robots.length > 1 && ['Robot', v.robot_name || v.robot],
  ].filter(Boolean);

  const pick = isVisit ? `<div id="pickWrap">${pickHtml(v)}</div>` : '';

  const near = nearby.length ? `<div class="section"><h3>Within 30 minutes</h3><div class="nearby">${
    nearby.map(n => `<div><span>${fmtTime(n.ts)}</span><a href="#" data-id="${esc(n.id)}">${esc(typeLabel(n.type))}</a>${
      n.type === 'PET_VISIT' ? `<span>${esc(petName(n.pet))}</span><span>${n.weight != null ? fmtW(n.weight) : ''}</span>` : ''}</div>`).join('')
  }</div></div>` : '';

  el('detail').innerHTML = renderPlayer() + renderStrip() +
    `<div class="facts">${facts.map(([k, val]) => `<div class="fact"><div class="k">${esc(k)}</div><div class="v">${esc(val)}</div></div>`).join('')}</div>` +
    pick + near;

  el('strip')?.addEventListener('click', e => {
    const c = e.target.closest('.c');
    if (!c) return;
    s.clipIdx = +c.dataset.i;
    el('playerWrap').outerHTML = renderPlayer();
    el('strip').querySelectorAll('.c').forEach(n => n.classList.toggle('on', +n.dataset.i === s.clipIdx));
  });
  renderPick();
  el('detail').querySelectorAll('.nearby a').forEach(a => a.addEventListener('click', e => {
    e.preventDefault(); select(a.dataset.id);
  }));
}

// ---------------------------------------------------------------- edits

async function save(body, method = 'POST') {
  const id = s.active;
  const r = await api(`/api/events/${encodeURIComponent(id)}/override`, {
    method, headers: { 'Content-Type': 'application/json' }, body: method === 'POST' ? JSON.stringify(body) : undefined,
  });
  if (s.active !== id) return;
  s.detail.event = r.event;
  updateRow(r.event);
  renderPick();
  el('saved')?.classList.add('show');
  setTimeout(() => el('saved')?.classList.remove('show'), 1200);
}

async function assign(petId) {
  const v = s.detail?.event;
  if (!v || v.type !== 'PET_VISIT') return;
  await save({ pet_id: petId });
  if (s.f.filter === 'review') step(1);
}
const revert = () => s.detail?.event.type === 'PET_VISIT' && save(null, 'DELETE');

function step(d) {
  if (!s.events.length) return;
  const i = s.events.findIndex(v => v.id === s.active);
  const next = s.events[Math.min(s.events.length - 1, Math.max(0, i === -1 ? 0 : i + d))];
  if (next && next.id !== s.active) select(next.id);
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && sheetOpen()) { e.preventDefault(); el('sheetClose').click(); return; }
  if (e.target.matches('input, textarea, select') || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === 'ArrowDown' || e.key === 'j') { e.preventDefault(); step(1); }
  else if (e.key === 'ArrowUp' || e.key === 'k') { e.preventDefault(); step(-1); }
  else if (e.key === 'u') { if (s.detail?.event.corrected || s.detail?.event.note) revert(); }
  else if (e.key === '0') assign(UNKNOWN);
  else if (/^[1-9]$/.test(e.key)) {
    const ordered = [...s.meta.pets].sort((a, b) => (b.active - a.active) || (a.slot - b.slot));
    const p = ordered[+e.key - 1];
    if (p) assign(p.id);
  }
});

el('detail').addEventListener('click', async e => {
  const t = e.target;
  if (t.closest('#keepBtn')) toggleKeep();
  else if (t.closest('#dlBtn')) requestDownload();
  else if (t.closest('#revertBtn')) revert();
  else if (t.closest('#retryBtn')) {
    const id = s.active;
    const r = await api(`/api/events/${encodeURIComponent(id)}/push`, { method: 'POST' });
    if (s.active === id) { s.detail.event = r.event; renderPick(); }
  } else {
    const b = t.closest('#pick button[data-pet]');
    if (b) assign(b.dataset.pet);
  }
});
el('detail').addEventListener('change', async e => {
  if (e.target.id === 'note') save({ note: e.target.value });
});

window.addEventListener('lr-synced', async () => {
  try {
    s.meta = await api('/api/meta');
    s.meta.pets.forEach(p => { s.pets[p.id] = p; });
    const r = await fetchPage(0);
    if (s.page === 0) { s.events = r.events; s.total = r.total; renderList(); }
    else { s.total = r.total; el('count').textContent = `${s.total} events`; }
  } catch {}
});

(async function init() {
  const id = readUrl();
  try { s.meta = await api('/api/meta'); }
  catch (e) { el('list').innerHTML = `<div class="empty">Couldn't reach the server (${esc(e.message)})</div>`; return; }
  s.meta.pets.forEach(p => { s.pets[p.id] = p; });
  buildFilters();
  await reload();
  if (id) select(id);
  else if (s.events.length && matchMedia('(min-width: 861px)').matches) select(s.events[0].id);
})();
