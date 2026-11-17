const SERIAL = el('root').dataset.serial;
const ACTIONS = [
  { cmd: 'clean', label: 'Start a clean cycle', button: 'Clean now', confirm: 'Yes, start cycle',
    desc: 'Rotates the globe now and sifts waste into the drawer. Make sure no cat is inside.' },
  { cmd: 'reset_drawer', label: 'Reset waste drawer gauge', button: 'Drawer emptied', confirm: 'Yes, reset gauge',
    desc: 'Do this after emptying the drawer so the fill level and cycle count start from zero.' },
  { cmd: 'remote_reset', label: 'Remote reset', button: 'Reset robot', confirm: 'Yes, reset robot',
    desc: 'Clears errors, e.g. after a jam or a pinch detection. The robot may run a cycle afterwards.' },
];
const s = { data: null, armed: null, armTimer: null, poll: null, autoRefreshed: false };

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
const titleCase = t => t ? t.toLowerCase().replace(/_/g, ' ').replace(/^./, c => c.toUpperCase()) : '—';
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
    el('root').innerHTML = tabs + `<div class="card"><div class="empty"><div class="empty-icon">🤖</div>No status for this robot yet. It's collected on the next sync.</div></div>`;
    return;
  }
  const faulty = (i.faults || []).length > 0;
  const stateCls = i.online ? 'ok' : 'warn';
  const stateText = !i.online ? 'Offline' : (i.status || 'Unknown');
  const refreshing = busy(d.latest.refresh);
  const [wq, wd] = wifi(i.wifi_rssi);
  const drawerWarn = (i.drawer_level ?? 0) >= 80;
  const litterWarn = ['LOW', 'EMPTY', 'REFILL'].includes(i.litter_state);

  el('root').innerHTML = tabs + `
  <div class="card">
    <div class="head">
      <div><h1>${esc(d.robot.name)}</h1><div class="model">${esc(i.model || d.robot.model || '')}${i.setup_date ? ` · set up ${new Date(i.setup_date * 1000).toLocaleDateString()}` : ''}</div></div>
      <span class="state ${stateCls}" role="status"><span class="dot"></span>${esc(stateText)}${i.sleeping ? ' · sleeping' : ''}</span>
      <div class="right">Status as of ${ago(d.info_at)}<br>${refreshing ? 'Updating…' : '<a id="refreshNow">Update now</a>'}</div>
    </div>
    ${faulty ? `<div class="faults">${i.faults.map(f => `<div>⚠ ${esc(f)}</div>`).join('')}</div>` : ''}
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
  </div>

  <div class="cols">
    <div class="card panel">
      <h2>Actions</h2>
      ${ACTIONS.map(actionRow).join('')}
    </div>
    <div style="display:grid;gap:1rem">
      <div class="card panel">
        <h2>Firmware</h2>
        <div class="fw">
          ${firmwareTable(d.firmware, i.firmware)}
          <div style="margin-top:.6rem">Update status: ${i.firmware_updating ? '<b>updating now</b>' : esc(titleCase(i.firmware_status))}</div>
          ${firmwareHistory(d.firmware)}
          <div class="note">Firmware updates have to be installed from the Whisker app. Whisker's API for the Litter-Robot 5 can't start an update or report whether one is available.</div>
        </div>
      </div>
      <div class="card panel">
        <h2>Recent robot activity</h2>
        <div class="list">${d.recent.length ? d.recent.map(a => `<div><span class="t">${esc(when(a.ts))}</span><span>${esc(typeLabel(a.type))}${a.subtype && !/^robotCycleState/.test(a.subtype) ? ` <span class="muted">(${esc(a.subtype)})</span>` : ''}</span></div>`).join('') : '<div class="muted">Nothing recorded yet</div>'}</div>
      </div>
    </div>
  </div>`;
}

const FW_NAMES = { ESP: 'Wi-Fi module', MCU: 'Main controller', CAM: 'Camera', EDGE: 'Edge processor', AI: 'AI model' };
const fwName = c => FW_NAMES[c] || c;
const day = ts => new Date(ts * 1000).toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' });

function firmwareTable(history, raw) {
  const current = {};
  for (const f of history) if (!current[f.component]) current[f.component] = f;  // newest first
  const rows = Object.values(current);
  if (!rows.length) return `<div><code>${esc(raw || 'unknown')}</code></div>`;
  return `<table class="fwt">${rows.map(f => `<tr><td>${esc(fwName(f.component))}</td><td><code>${esc(f.version)}</code></td>
    <td class="muted">${f.previous ? `updated ${esc(day(f.at))}` : `since ${esc(day(f.at))} or earlier`}</td></tr>`).join('')}</table>`;
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

function actionRow(a) {
  const last = s.data.latest[a.cmd];
  const inFlight = busy(last);
  let res = '';
  if (inFlight) res = `<div class="res busy">⟳ Sending to the robot…</div>`;
  else if (last && last.status === 'error') res = `<div class="res err">⚠ Failed ${esc(ago(last.updated))}: ${esc(last.error || 'unknown error')}</div>`;
  else if (last && last.status === 'done') res = `<div class="res ok">✓ Sent ${esc(ago(last.updated))}</div>`;
  const armed = s.armed === a.cmd;
  const btns = armed
    ? `<button class="btn go" data-go="${a.cmd}">${esc(a.confirm)}</button><button class="btn" data-cancel>Cancel</button>`
    : `<button class="btn" data-arm="${a.cmd}" ${inFlight ? 'disabled' : ''}>${inFlight ? 'Sending…' : esc(a.button)}</button>`;
  return `<div class="act"><div><div class="label">${esc(a.label)}</div><div class="desc">${esc(a.desc)}</div>${res}</div><div class="btns">${btns}</div></div>`;
}

// ---------------------------------------------------------------- data

async function load() {
  if (!SERIAL) {
    el('root').innerHTML = `<div class="card"><div class="empty"><div class="empty-icon">🤖</div>No robots yet. They appear after the first sync.</div></div>`;
    return;
  }
  try { s.data = await api(`/api/robot/${encodeURIComponent(SERIAL)}`); }
  catch (e) { el('root').innerHTML = `<div class="card"><div class="empty">Couldn't load robot (${esc(e.message)})</div></div>`; return; }
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
  const arm = t.closest('[data-arm]'), go = t.closest('[data-go]');
  if (arm) {
    s.armed = arm.dataset.arm;
    clearTimeout(s.armTimer);
    s.armTimer = setTimeout(() => { s.armed = null; render(); }, 6000);
    render();
    el('root').querySelector('[data-go]')?.focus();
  } else if (go) {
    s.armed = null;
    clearTimeout(s.armTimer);
    send(go.dataset.go);
  } else if (t.closest('[data-cancel]')) {
    s.armed = null;
    render();
  }
});
window.addEventListener('lr-synced', load);
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
load();
