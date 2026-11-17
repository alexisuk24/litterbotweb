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
  return `<span class="chip"><span class="cat-dot" style="background:${catColor(petSlot(id))}"></span>${esc(petName(id))}${extra}</span>`;
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
  if (s.active) p.set('id', s.active);
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
  el('fFilter').addEventListener('click', e => {
    const v = e.target.dataset.v;
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

async function reload() {
  writeUrl();
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

function evRow(v) {
  const isVisit = v.type === 'PET_VISIT';
  const thumb = v.thumb ? `style="background-image:url('${esc(v.thumb)}')"` : '';
  const badges = [];
  if (v.ignored) badges.push('<span class="badge off">Ignored</span>');
  else if (v.corrected) badges.push('<span class="badge edit">Edited</span>');
  if (v.review && !v.ignored) badges.push(`<span class="badge warn" title="${v.pet ? `Weight is closer to ${esc(petName(v.suggested))}` : 'No cat assigned'}">Check</span>`);
  const details = isVisit ? [
    v.weight != null ? fmtW(v.weight) : null,
    v.duration != null ? fmtDur(v.duration) : null,
    v.waste_type || null,
  ].filter(Boolean) : [typeLabel(v.type), v.subtype].filter(Boolean);
  return `<div class="ev${v.id === s.active ? ' active' : ''}${v.ignored ? ' ignored' : ''}" data-id="${esc(v.id)}">
    <div class="thumb" ${thumb}>${v.thumb ? '' : (v.clip_count ? '▶' : '—')}</div>
    <div class="main">
      <div class="row1"><span class="time">${fmtTime(v.ts)}</span>${isVisit ? catChip(v.pet) : `<span class="muted" style="font-size:.85rem">${esc(typeLabel(v.type))}</span>`}</div>
      <div class="row2">${details.map(esc).join('<span>·</span>')}</div>
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

async function select(id, { scroll = true } = {}) {
  s.active = id;
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
  const wasIdle = el('detail').classList.contains('idle');
  el('detail').classList.remove('idle');
  renderDetail();
  if (matchMedia('(max-width: 860px)').matches && (scroll || wasIdle)) el('detail').scrollIntoView({ behavior: 'smooth' });
}

function keepBar(c) {
  if (!c || !c.video) return '';
  const when = c.keep ? 'Kept forever' : c.expires ? `Auto-deletes ${expiryText(c.expires)}` : '';
  return `<div class="keepbar"><button class="btn keep-btn${c.keep ? ' on' : ''}" id="keepBtn" data-id="${esc(c.id)}">📌 ${c.keep ? 'Kept' : 'Keep forever'}</button><span class="expiry">${esc(when)}</span></div>`;
}

function renderPlayer() {
  const c = s.detail.clips[s.clipIdx];
  let body;
  if (!c) body = `<div class="novideo"><div class="empty-icon">📹</div>No camera clip recorded near this event</div>`;
  else if (c.deleted) body = `<div class="novideo"><div class="empty-icon">🗑</div>This clip was deleted by the retention schedule</div>`;
  else if (!c.video) body = `<div class="novideo"><div class="empty-icon">⏳</div>This clip hasn't been downloaded yet<br><span style="font-size:.75rem">The downloader picks it up on its next run.</span></div>`;
  else body = `<video controls autoplay muted playsinline preload="metadata" src="${esc(c.video)}"></video>`;
  return `<div id="playerWrap">${body}${keepBar(c)}</div>`;
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

function renderDetail() {
  const { event: v, nearby } = s.detail;
  const isVisit = v.type === 'PET_VISIT';
  const facts = [
    ['When', `${fmtDay(v.ts)} ${fmtTime(v.ts)}`],
    isVisit && ['Weight', fmtW(v.weight)],
    isVisit && ['Duration', fmtDur(v.duration)],
    isVisit && ['Waste', v.waste_type || '—'],
    !isVisit && ['Type', typeLabel(v.type)],
    v.subtype && ['Detail', v.subtype],
    s.meta.robots.length > 1 && ['Robot', v.robot_name || v.robot],
  ].filter(Boolean);

  let pick = '';
  if (isVisit) {
    const ordered = [...s.meta.pets].sort((a, b) => (b.active - a.active) || (a.slot - b.slot));
    const btn = (id, label, sub, key) => {
      const on = id === UNKNOWN ? !v.pet : v.pet === id;
      const dot = id === UNKNOWN ? '' : `<span class="cat-dot" style="background:${catColor(petSlot(id))}"></span>`;
      return `<button data-pet="${esc(id)}" class="${on ? 'on' : ''}">${dot}<span>${esc(label)}${sub ? `<span class="sub">${sub}</span>` : ''}</span>${key ? `<kbd>${key}</kbd>` : ''}</button>`;
    };
    const buttons = ordered.map((p, i) => {
      const subs = [];
      if (p.ref_weight) subs.push(`usually ${fmtW(p.ref_weight)}`);
      if (p.id === v.suggested && v.weight) subs.push('closest weight');
      if (p.id === v.api_pet && v.corrected) subs.push('robot said');
      return btn(p.id, p.name, subs.join(' · '), i < 9 ? i + 1 : '');
    }).join('') + btn(UNKNOWN, 'Unknown / other', '', '0');

    let callout = '';
    if (v.review && !v.ignored) {
      callout = v.pet
        ? `<div class="callout">This visit weighed ${fmtW(v.weight)}, which is closer to ${esc(petName(v.suggested))} than ${esc(petName(v.pet))}. Check the clip.</div>`
        : `<div class="callout">No cat was assigned to this visit.${v.suggested ? ` By weight it looks like ${esc(petName(v.suggested))}.` : ''}</div>`;
    }
    pick = `<div class="section">
      <h3><span>Who was this?</span><span class="saved" id="saved">Saved</span></h3>
      ${callout}
      <div class="pick" id="pick">${buttons}</div>
      <div class="actions">
        <button class="btn" id="ignoreBtn">${v.ignored ? 'Un-ignore' : 'Ignore this visit'} <kbd>x</kbd></button>
        ${(v.corrected || v.ignored || v.note) ? `<button class="btn" id="revertBtn">Revert to robot's data <kbd>u</kbd></button>` : ''}
        <span class="muted" style="font-size:.78rem">Robot recorded: ${esc(v.api_pet ? petName(v.api_pet) : 'no cat')}</span>
      </div>
      <div style="margin-top:.75rem"><textarea class="note" id="note" placeholder="Note (optional), e.g. 'both cats in at once'">${esc(v.note)}</textarea></div>
      <div class="hint">Edits are stored locally in this app only; nothing is sent back to Whisker. Ignored visits are left out of the analysis.</div>
    </div>`;
  }

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
  el('pick')?.addEventListener('click', e => {
    const b = e.target.closest('button[data-pet]');
    if (b) assign(b.dataset.pet);
  });
  el('ignoreBtn')?.addEventListener('click', toggleIgnore);
  el('revertBtn')?.addEventListener('click', revert);
  el('note')?.addEventListener('change', e => save({ note: e.target.value }));
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
  const vid = el('detail').querySelector('video');
  const keep = vid && { t: vid.currentTime, paused: vid.paused };
  const scroll = el('strip')?.scrollLeft;
  renderDetail();
  const nv = el('detail').querySelector('video');
  if (keep && nv) { nv.currentTime = keep.t; if (keep.paused) nv.pause(); }
  if (scroll && el('strip')) el('strip').scrollLeft = scroll;
  el('saved')?.classList.add('show');
  setTimeout(() => el('saved')?.classList.remove('show'), 1200);
}

async function assign(petId) {
  const v = s.detail?.event;
  if (!v || v.type !== 'PET_VISIT') return;
  await save({ pet_id: petId === v.api_pet ? null : petId });
  if (s.f.filter === 'review') step(1);
}
const toggleIgnore = () => s.detail?.event.type === 'PET_VISIT' && save({ ignored: !s.detail.event.ignored });
const revert = () => s.detail?.event.type === 'PET_VISIT' && save(null, 'DELETE');

function step(d) {
  if (!s.events.length) return;
  const i = s.events.findIndex(v => v.id === s.active);
  const next = s.events[Math.min(s.events.length - 1, Math.max(0, i === -1 ? 0 : i + d))];
  if (next && next.id !== s.active) select(next.id);
}

document.addEventListener('keydown', e => {
  if (e.target.matches('input, textarea, select') || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === 'ArrowDown' || e.key === 'j') { e.preventDefault(); step(1); }
  else if (e.key === 'ArrowUp' || e.key === 'k') { e.preventDefault(); step(-1); }
  else if (e.key === 'x') toggleIgnore();
  else if (e.key === 'u') { if (s.detail?.event.corrected || s.detail?.event.ignored || s.detail?.event.note) revert(); }
  else if (e.key === '0') assign(UNKNOWN);
  else if (/^[1-9]$/.test(e.key)) {
    const ordered = [...s.meta.pets].sort((a, b) => (b.active - a.active) || (a.slot - b.slot));
    const p = ordered[+e.key - 1];
    if (p) assign(p.id);
  }
});

onUnitChange.push(() => { renderList(); if (s.detail) renderDetail(); });
el('detail').addEventListener('click', e => { if (e.target.closest('#keepBtn')) toggleKeep(); });

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
