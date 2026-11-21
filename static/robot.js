const SERIAL = el('root').dataset.serial;
const ACTIONS = [
  { cmd: 'clean', button: 'Clean', title: 'Start a clean cycle?', confirm: 'Start cycle',
    icon: '<path d="M21 12a9 9 0 1 1-2.6-6.4M21 3.5V9h-5.5"/>',
    desc: 'Rotates the globe now and sifts waste into the drawer. Make sure no cat is inside.' },
  { cmd: 'reset_drawer', button: 'Tare', title: 'Tare the scale?', confirm: 'Tare',
    icon: '<path d="M12 4v16M7 20h10M4 7h16M7 7l-3 6.5a3 3 0 0 0 6 0L7 7zM17 7l-3 6.5a3 3 0 0 0 6 0L17 7z"/>',
    desc: 'Resets the scale. Do this after emptying the waste drawer or filling the hopper with litter.' },
  { cmd: 'remote_reset', button: 'Reset', title: 'Reset the robot?', confirm: 'Reset robot',
    icon: '<path d="M12 3v9M17.7 6.3a8 8 0 1 1-11.4 0"/>',
    desc: 'Clears errors, e.g. after a jam or a pinch detection. The robot may run a cycle afterwards.' },
];
const s = { data: null, confirming: null, poll: null, autoRefreshed: false };

const ago = ts => {
  if (!ts) return 'never';
  const m = (Date.now() / 1000 - ts) / 60;
  if (m < 1) return 'just now';
  if (m < 60) return `${Math.round(m)} min ago`;
  if (m < 48 * 60) return `${Math.round(m / 60)} h ago`;
  return `${Math.round(m / 1440)} days ago`;
};
const when = ts => ts ? new Date(ts * 1000).toLocaleString([], { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—';
const pct = v => v == null ? '—' : `${Math.round(v)}%`;
// UPDATE_SUCCESSFUL, UpdateSuccessful and OPTIMAL all become "Update successful" / "Optimal"
const titleCase = t => t ? t.replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase().replace(/_/g, ' ').replace(/^./, c => c.toUpperCase()) : '—';
function wifi(rssi) {
  if (rssi == null) return ['—', ''];
  const q = rssi >= -60 ? 'Excellent' : rssi >= -70 ? 'Good' : rssi >= -80 ? 'Fair' : 'Weak';
  return [q, `${rssi} dBm`];
}
const busy = c => c && (c.status === 'pending' || c.status === 'sending');

// ---------------------------------------------------------------- render

function render() {
  const d = s.data, i = d.info || {};
  const tabs = d.robots.length > 1
    ? `<div class="tabs">${d.robots.map(r => `<a href="/robots/${encodeURIComponent(r.serial)}" class="${r.serial === SERIAL ? 'on' : ''}">${esc(r.name)}</a>`).join('')}</div>` : '';
  if (!d.info) {
    show(tabs + `<div class="card"><div class="empty"><div class="empty-icon">🤖</div>No status for this robot yet. It's collected on the next sync.</div></div>`);
    return;
  }
  const faulty = (i.faults || []).length > 0;
  const stateCls = i.online ? 'ok' : 'warn';
  const stateText = !i.online ? 'Offline' : (i.status || 'Unknown');
  const refreshing = busy(d.latest.refresh);
  const [wq, wd] = wifi(i.wifi_rssi);
  const drawerWarn = (i.drawer_level ?? 0) >= 80;
  const litterWarn = ['LOW', 'EMPTY', 'REFILL'].includes(i.litter_state);

  show(tabs + `
  <div class="card">
    <div class="head">
      <div><h1>${esc(d.robot.name)}</h1><div class="model">${esc(i.model || d.robot.model || '')}${i.setup_date ? ` · set up ${new Date(i.setup_date * 1000).toLocaleDateString()}` : ''}</div></div>
      <span class="state ${stateCls}" role="status"><span class="dot"></span>${esc(stateText)}${i.sleeping ? ' · sleeping' : ''}</span>
      <div class="right">Status as of ${ago(d.info_at)}<br>${refreshing ? 'Updating…' : '<a id="refreshNow">Update now</a>'}</div>
    </div>
    ${faulty ? `<div class="faults">${i.faults.map(f => `<div>⚠ ${esc(f)}</div>`).join('')}</div>` : ''}
    ${d.clips_stale ? `<div class="faults"><div>⚠ Whisker hasn't listed any new videos since ${esc(when(d.clips_stale.newest_clip))}, though ${d.clips_stale.visits_since} visit${d.clips_stale.visits_since === 1 ? ' has' : 's have'} happened since. They'll download automatically if Whisker lists them later.</div></div>` : ''}
  </div>
  <div class="card panel">
    <h2>Actions</h2>
    <div class="acts">${ACTIONS.map(actionButton).join('')}</div>
    ${ACTIONS.map(actionError).join('')}
  </div>
  <div class="tiles">
    ${tile('Waste drawer', pct(i.drawer_level), `${i.cycles_since_empty ?? '—'} cycles since emptied`, i.drawer_level, drawerWarn)}
    ${tile('Litter level', pct(i.litter_level), titleCase(i.litter_state), i.litter_level, litterWarn)}
    ${tile('Last 24 hours', `${d.last_24h.visits} visits`, `${d.last_24h.cycles} clean cycles · last visit ${ago(d.last_visit)}`)}
    ${tile('Clean delay', i.wait_minutes != null ? `${i.wait_minutes} min` : '—', 'after a cat leaves')}
    ${tile('Filter', i.next_filter ? new Date(i.next_filter * 1000).toLocaleDateString([], { day: 'numeric', month: 'short' }) : '—', `next replacement · ${i.filter_cycles ?? '—'} cycles on this one`)}
    ${tile('Total cycles', i.total_cycles ?? '—', i.scoops_saved != null ? `${i.scoops_saved.toLocaleString()} scoops saved` : '')}
    ${tile('Wi-Fi', wq, wd)}
    ${tile('Last seen', ago(i.last_seen), when(i.last_seen))}
  </div>`, `
      ${iqCard(d.iq)}
      <div class="card panel">
        <h2>Firmware</h2>
        <div class="fw">
          ${firmwareTable(d.firmware, i.firmware)}
          <div style="margin-top:.6rem">Update status: ${i.firmware_updating ? '<b>updating now</b>' : esc(titleCase(i.firmware_status))}</div>
          ${firmwareHistory(d.firmware)}
          <div class="note">Firmware updates have to be installed from the Whisker app. Whisker's API for the Litter-Robot 5 can't start an update or report whether one is available.</div>
        </div>
      </div>
      `, d.robot.camera, `
      <div class="card panel">
        <h2>Recent robot activity</h2>
        <div class="list">${d.recent.length ? d.recent.map(a => `<div><span class="t">${esc(when(a.ts))}</span><span>${esc(typeLabel(a.type))}${a.subtype && !/^robotCycleState/.test(a.subtype) ? ` <span class="muted">(${esc(a.subtype)})</span>` : ''}</span></div>`).join('') : '<div class="muted">Nothing recorded yet</div>'}</div>
      </div>`);
}

// The live view card is static markup (with #below under it, beside #side) so re-rendering
// never touches its <video>.
function show(top, side = '', camera = false, below = '') {
  el('top').innerHTML = top;
  el('side').innerHTML = side;
  el('below').innerHTML = below;
  el('live').hidden = !camera;
  el('cols').hidden = !side && !camera && !below;
  el('cols').classList.toggle('solo', !camera);
}

// Robot IQ: when the drawer will be full and the hopper need refilling
function iqCard(iq) {
  if (!iq || (!iq.drawer && !iq.hopper)) return '';
  const inDays = d => d < 1 ? 'within a day' : `in about ${Math.round(d)} day${Math.round(d) === 1 ? '' : 's'}`;
  const on = ts => new Date(ts * 1000).toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
  const row = (label, value, detail, warn) => `<div class="iq-row"><div class="t">${esc(label)}</div>
    <div><div class="iq-v${warn ? ' warn' : ''}">${esc(value)}</div>${detail ? `<div class="iq-d">${esc(detail)}</div>` : ''}</div></div>`;
  const rows = [];
  const d = iq.drawer;
  if (d) {
    const facts = [d.per_cycle ? `+${d.per_cycle.toFixed(2)}% per clean cycle` : '',
                   d.cycles_per_day ? `${d.cycles_per_day} cycles/day` : '',
                   d.full_level < 100 ? `reports full at ${Math.round(d.full_level)}%` : ''].filter(Boolean).join(' · ');
    if (d.state === 'full') rows.push(row('Waste drawer', 'Full now', `at ${Math.round(d.level)}%`, true));
    else if (d.state === 'ok') rows.push(row('Waste drawer', `Full ${inDays(d.days)}`, `around ${on(d.eta)} · ${facts}`, d.days <= 2));
    else rows.push(row('Waste drawer', 'Learning', `${Math.round(d.level)}% now. A forecast needs a few syncs and about 10 clean cycles since it was last emptied.`));
  }
  const h = iq.hopper;
  if (h) {
    const facts = [h.capacity ? `about ${Math.round(h.capacity)} dispenses per fill` : '',
                   h.since_refill != null ? `${h.since_refill} since refilled` : '',
                   h.per_day ? `${h.per_day}/day` : ''].filter(Boolean).join(' · ');
    if (h.state === 'empty') rows.push(row('Litter hopper', 'Empty', 'refill it', true));
    else if (h.state === 'low') rows.push(row('Litter hopper', 'Litter low', 'refill it soon', true));
    else if (h.state === 'fault') rows.push(row('Litter hopper', titleCase(h.status), '', true));
    else if (h.state === 'ok') rows.push(row('Litter hopper', `Refill ${inDays(h.days)}`, `around ${on(h.eta)} · ${facts}`, h.days <= 2));
    else rows.push(row('Litter hopper', 'Learning', 'The robot only reports Ready, Litter low or Empty. A forecast starts once a refill has been seen running down to Litter low.' + (facts ? ` ${facts}.` : '')));
  }
  return `<div class="card panel"><h2>Robot IQ</h2><div class="iq">${rows.join('')}</div></div>`;
}

const FW_NAMES = { ESP: 'Wi-Fi module', MCU: 'Main controller', CAM: 'Camera', EDGE: 'Edge processor', AI: 'AI model' };
const fwName = c => FW_NAMES[c] || c;
const day = ts => new Date(ts * 1000).toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' });

function firmwareTable(history, raw) {
  const current = {};
  for (const f of history) if (!current[f.component]) current[f.component] = f;  // newest first
  const rows = Object.values(current);
  if (!rows.length) return `<div><code>${esc(raw || 'unknown')}</code></div>`;
  return `<table class="fwt">${rows.map(f => `<tr><td>${esc(fwName(f.component))}
    <div class="when">${f.previous ? `updated ${esc(day(f.at))}` : `since ${esc(day(f.at))} or earlier`}</div></td>
    <td><code>${esc(f.version)}</code></td></tr>`).join('')}</table>`;
}

function firmwareHistory(history) {
  const updates = history.filter(f => f.previous);
  if (!history.length) return '';
  const since = Math.min(...history.map(f => f.at));
  if (!updates.length) return `<div class="fwh-empty">No updates seen since tracking began on ${esc(day(since))}.</div>`;
  return `<div class="fwh"><div class="fwh-title">Update history</div>${updates.map(f =>
    `<div><span class="t">${esc(when(f.at))}</span><span>${esc(fwName(f.component))} <code>${esc(f.previous)}</code> → <code>${esc(f.version)}</code></span></div>`
  ).join('')}<div class="fwh-empty">Tracking since ${esc(day(since))}.</div></div>`;
}

function tile(k, v, sub, bar, warn) {
  const b = bar == null ? '' : `<div class="bar${warn ? ' warn' : ''}"><i style="width:${Math.max(0, Math.min(100, bar))}%"></i></div>`;
  return `<div class="card tile"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="d">${esc(sub || '')}</div>${b}</div>`;
}

function actionButton(a) {
  const last = s.data.latest[a.cmd];
  const inFlight = busy(last);
  let res = '';
  if (inFlight) res = `<div class="res busy">Sending…</div>`;
  else if (last && last.status === 'done') res = `<div class="res ok">✓ Sent ${esc(ago(last.updated))}</div>`;
  return `<div class="act"><button class="btn act-btn" data-act="${a.cmd}" title="${esc(a.desc)}" ${inFlight ? 'disabled' : ''}>
    <svg viewBox="0 0 24 24" aria-hidden="true">${a.icon}</svg><span>${esc(a.button)}</span></button>${res}</div>`;
}

function actionError(a) {
  const last = s.data.latest[a.cmd];
  if (!last || busy(last) || last.status !== 'error') return '';
  return `<div class="act-err">⚠ ${esc(a.button)} failed ${esc(ago(last.updated))}: ${esc(last.error || 'unknown error')}</div>`;
}

function confirmAction(cmd) {
  const a = ACTIONS.find(x => x.cmd === cmd);
  s.confirming = cmd;
  el('confirmTitle').textContent = a.title;
  el('confirmDesc').textContent = a.desc;
  el('confirmGo').textContent = a.confirm;
  el('confirm').returnValue = '';
  el('confirm').showModal();
}

// ---------------------------------------------------------------- data

async function load() {
  if (!SERIAL) {
    show(`<div class="card"><div class="empty"><div class="empty-icon">🤖</div>No robots yet. They appear after the first sync.</div></div>`);
    return;
  }
  try { s.data = await api(`/api/robot/${encodeURIComponent(SERIAL)}`); }
  catch (e) { show(`<div class="card"><div class="empty">Couldn't load robot (${esc(e.message)})</div></div>`); return; }
  render();
  schedule();
  if (!s.autoRefreshed && (!s.data.info_at || Date.now() / 1000 - s.data.info_at > 60)) {
    s.autoRefreshed = true;
    send('refresh');
  }
}

function schedule() {
  clearTimeout(s.poll);
  const anyBusy = Object.values(s.data.latest).some(busy);
  s.poll = setTimeout(load, anyBusy ? 1500 : 30000);
}

async function send(cmd) {
  try {
    const r = await fetch(`/api/robot/${encodeURIComponent(SERIAL)}/command`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ command: cmd }),
    });
    const body = await r.json();
    s.data.latest[cmd] = body.command;
  } catch {}
  render();
  schedule();
}

el('root').addEventListener('click', e => {
  const t = e.target;
  if (t.id === 'refreshNow') { send('refresh'); return; }
  const act = t.closest('[data-act]');
  if (act) confirmAction(act.dataset.act);
});
el('confirm').addEventListener('close', () => {
  if (el('confirm').returnValue === 'ok' && s.confirming) send(s.confirming);
  s.confirming = null;
});
// a click on the dimmed backdrop lands on the <dialog> itself (the form fills its inside)
el('confirm').addEventListener('click', e => { if (e.target === el('confirm')) el('confirm').close(); });
window.addEventListener('lr-synced', load);
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
load();
