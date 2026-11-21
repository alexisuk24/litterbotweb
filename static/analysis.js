const s = { days: store.get('lr-range', '30'), robot: '', data: null, charts: [] };
const NONE_COLOR = () => catColor(-1);
const NEUTRAL = '#4ecdc4';

if (window.Chart) {
  Chart.defaults.color = '#b4bfcf';
  Chart.defaults.borderColor = 'rgba(255,255,255,.06)';
  Chart.defaults.font.size = 11;
  Chart.defaults.plugins.legend.labels.boxWidth = 10;
  Chart.defaults.plugins.legend.labels.boxHeight = 10;
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.plugins.tooltip.backgroundColor = '#0b0d13';
  Chart.defaults.plugins.tooltip.borderColor = '#2d3748';
  Chart.defaults.plugins.tooltip.borderWidth = 1;
  Chart.defaults.plugins.tooltip.padding = 8;
  Chart.defaults.maintainAspectRatio = false;
}

const alpha = (hex, a) => {
  const n = parseInt(hex.replace('#', ''), 16);
  return `rgba(${n >> 16 & 255},${n >> 8 & 255},${n & 255},${a})`;
};
const shortDate = ms => new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
const ago = ts => {
  if (!ts) return 'never';
  const h = (Date.now() / 1000 - ts) / 3600;
  return h < 1 ? `${Math.round(h * 60)} min ago` : h < 48 ? `${Math.round(h)} h ago` : `${Math.round(h / 24)} days ago`;
};
const signed = (n, f = x => x.toFixed(1)) => (n > 0 ? '+' : n < 0 ? '−' : '±') + f(Math.abs(n));

function shownPets(d) {
  return d.pets.filter(p => p.active || d.kpis.find(k => k.pet === p.id)?.visits);
}

// ---------------------------------------------------------------- layout

function render() {
  const d = s.data;
  s.charts.forEach(c => c.destroy());
  s.charts = [];
  el('tzNote').textContent = `Times shown in ${d.tz}`;
  if (!d.totals.visits && !d.totals.cycles) {
    el('root').innerHTML = `<div class="card"><div class="empty"><div class="empty-icon">📊</div>
      <div>No activity in this period yet. The collector fills this in on each run.</div></div></div>`;
    return;
  }
  const pets = shownPets(d);
  const corrPct = d.totals.visits ? Math.round(100 * d.totals.corrected / d.totals.visits) : 0;
  el('root').innerHTML = `
  <div class="grid">
    <div class="card panel span-12">
      <h2>Insights</h2>
      <ul class="insights">${d.insights.map(i => `<li class="${i.level}"><span class="ic">${i.level === 'warn' ? '⚠' : 'ℹ'}</span><span>${esc(i.text)}</span></li>`).join('') || '<li><span class="ic">ℹ</span><span>Nothing notable in this period.</span></li>'}</ul>
    </div>

    <div class="span-12 tiles">
      ${tile('Visits', d.totals.visits, `${d.totals.per_day} per day`)}
      ${tile('Clean cycles', d.totals.cycles, `${(d.totals.cycles / d.days).toFixed(1)} per day`)}
      ${tile('No cat assigned', d.totals.unassigned, d.totals.unassigned ? '<a href="/events?pet=none" style="color:inherit">review →</a>' : 'all identified')}
      ${tile('Your corrections', d.totals.corrected, `${corrPct}% of visits`)}
      ${tile('Offline events', d.totals.offline, d.totals.interrupted ? `${d.totals.interrupted} interrupted cycles` : 'no interrupted cycles')}
    </div>

    <div class="span-12 cats">${pets.map(catCard).join('')}</div>

    <div class="card panel span-12">
      <h2>Visits per day</h2><div class="sub">Each bar is one day, split by cat.</div>
      <div class="chart"><canvas id="cDaily"></canvas></div>
    </div>

    <div class="card panel span-8">
      <h2>Weight over time</h2><div class="sub">Line: daily median of visit weights (readings that look like another cat are left out until you review them). Dots: every visit. Dashed: Whisker's weight history.</div>
      <div class="chart"><canvas id="cWeight"></canvas></div>
    </div>
    <div class="card panel span-4">
      <h2>Robot said → actually</h2><div class="sub">Your corrections, most frequent first</div>
      ${mixTable(d)}
    </div>

    <div class="card panel span-6">
      <h2>Time of day</h2><div class="sub">Average visits per day in each hour</div>
      <div class="chart"><canvas id="cHour"></canvas></div>
    </div>
    <div class="card panel span-6">
      <h2>Weekly rhythm</h2><div class="sub">All visits by weekday and hour; brighter cells are busier</div>
      ${heatmap(d.heatmap)}
    </div>

    <div class="card panel span-4">
      <h2>Clean cycles per day</h2>
      <div class="chart short"><canvas id="cCycles"></canvas></div>
    </div>
    <div class="card panel span-4">
      <h2>Litter level</h2><div class="sub">From robot snapshots taken each sync</div>
      <div class="chart short"><canvas id="cLitter"></canvas></div>
    </div>
    <div class="card panel span-4">
      <h2>Waste drawer</h2><div class="sub">Fill level from robot snapshots (%)</div>
      <div class="chart short"><canvas id="cDrawer"></canvas></div>
    </div>
  </div>`;

  if (!window.Chart) {
    document.querySelectorAll('.chart').forEach(c => { c.innerHTML = '<div class="empty">Chart library failed to load</div>'; });
    return;
  }
  dailyChart(d, pets);
  weightChart(d, pets);
  hourChart(d, pets);
  cyclesChart(d);
  snapshotChart('cLitter', d, 'litter');
  snapshotChart('cDrawer', d, 'drawer');
}

function tile(k, v, sub) {
  return `<div class="card tile"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="d">${sub}</div></div>`;
}

function catCard(p) {
  const k = s.data.kpis.find(x => x.pet === p.id) || {};
  const color = catColor(p.slot);
  const trend = k.prev_per_day != null ? `${signed(k.per_day - k.prev_per_day)} vs previous ${s.data.days}d` : '&nbsp;';
  const wch = k.weight_change != null
    ? `${signed(toUnit(k.weight_change), x => x.toFixed(2))} ${UNIT} (${signed(k.weight_change_pct)}%)` : 'not enough data';
  const w = k.waste || {};
  const total = Object.values(w).reduce((a, b) => a + b, 0);
  const split = total ? Object.entries(w).sort((a, b) => b[1] - a[1]).map(([t, n]) => `${esc(t)} ${Math.round(100 * n / total)}%`).join(' · ') : '—';
  const u = usual(k.baseline);
  return `<div class="card cat" style="--c:${color}">
    <div class="name">${catBadge(p, p.slot, 40)}${esc(p.name)}${p.active ? '' : ' <span class="badge off">inactive</span>'}</div>
    <div class="stats">
      <div class="stat"><div class="k">Visits / day</div><div class="v">${k.per_day ?? 0}</div><div class="d">${trend}</div>${u.visits}</div>
      <div class="stat"><div class="k">Weight</div><div class="v">${fmtW(k.weight)}</div><div class="d">${wch}</div>${u.weight}</div>
      <div class="stat"><div class="k">Typical visit</div><div class="v">${fmtDur(k.median_duration)}</div><div class="d">median duration</div>${u.duration}</div>
      <div class="stat"><div class="k">Last visit</div><div class="v" style="font-size:1rem">${ago(k.last_visit)}</div><div class="d">${k.last7 ?? 0} in last 7 days</div></div>
    </div>
    <div class="d muted" style="font-size:.75rem;margin-top:.7rem">${split}</div>
    ${u.note}
  </div>`;
}

// The cat's own normal range under each stat, from the 30 days before the last 7;
// highlighted when the last 7 days fall outside it.
function usual(b) {
  const line = (text, off) => `<div class="d usual${off ? ' off' : ''}">${text}</div>`;
  const out = { visits: '', weight: '', duration: '', note: '' };
  if (!b) return out;
  if (!b.ready) {
    out.note = `<div class="d muted usual-note">Learning normal ranges: ${b.days} of 10 days so far</div>`;
    return out;
  }
  const v = b.visits;
  if (v) out.visits = line(`last 7d ${v.recent.toFixed(1)} · usual ${v.low.toFixed(1)}–${v.high.toFixed(1)}`,
                           v.recent < v.low || v.recent > v.high);
  const w = b.weight;
  if (w) out.weight = line(`usual ${toUnit(w.low).toFixed(2)}–${fmtW(w.high)}`,
                           w.recent != null && (w.recent < w.low || w.recent > w.high));
  const d = b.duration;
  if (d) out.duration = line(d.long.length
    ? `⚠ ${d.long.length} over ${fmtDur(d.long_over)} in last 7d`
    : `usual ${fmtDur(d.low)}–${fmtDur(d.high)}`, d.long.length > 0);
  return out;
}

function mixTable(d) {
  if (!d.mixups.length) return '<div class="empty" style="padding:1.5rem .5rem">No corrections in this period.<br>Fix a visit on the Events page and it shows up here.</div>';
  const name = id => d.pets.find(p => p.id === id)?.name ?? (id === 'none' ? 'No cat' : 'Unknown');
  return `<table class="mix"><thead><tr><th>Robot said</th><th>Actually</th><th style="text-align:right">Times</th></tr></thead><tbody>${
    d.mixups.map(m => `<tr><td>${esc(name(m.from))}</td><td>${esc(name(m.to))}</td><td class="n">${m.count}</td></tr>`).join('')}</tbody></table>`;
}

function heatmap(grid) {
  const max = Math.max(1, ...grid.flat());
  const lo = [0x1f, 0x33, 0x55], hi = [0x9e, 0xc5, 0xf4];
  const shade = n => {
    if (!n) return 'var(--surface2)';
    const t = 0.15 + 0.85 * (n / max);
    return `rgb(${lo.map((c, i) => Math.round(c + (hi[i] - c) * t)).join(',')})`;
  };
  const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const head = '<tr><th></th>' + Array.from({ length: 24 }, (_, h) => `<th>${h % 3 === 0 ? h : ''}</th>`).join('') + '</tr>';
  const body = grid.map((row, i) => `<tr><th>${days[i]}</th>${row.map((n, h) =>
    `<td style="background:${shade(n)}" title="${days[i]} ${pad(h)}:00 · ${n} visit${n === 1 ? '' : 's'}"></td>`).join('')}</tr>`).join('');
  return `<div class="heat"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>
    <div class="heat-legend">fewer <span class="sw" style="background:${shade(1)}"></span><span class="sw" style="background:${shade(max / 2)}"></span><span class="sw" style="background:${shade(max)}"></span> more (max ${max})</div>`;
}

// ---------------------------------------------------------------- charts

function dailyChart(d, pets) {
  const labels = d.dates.map(x => shortDate(x + 'T12:00:00'));
  const ds = pets.map(p => ({ label: p.name, data: d.daily[p.id], backgroundColor: catColor(p.slot) }));
  if (d.daily.none.some(Boolean)) ds.push({ label: 'No cat', data: d.daily.none, backgroundColor: NONE_COLOR() });
  ds.forEach(x => Object.assign(x, { borderColor: '#1a1d27', borderWidth: { top: 2 }, borderRadius: 2, borderSkipped: 'bottom' }));
  s.charts.push(new Chart(el('cDaily'), {
    type: 'bar', data: { labels, datasets: ds },
    options: {
      interaction: { mode: 'index', intersect: false },
      scales: { x: { stacked: true, grid: { display: false }, ticks: { maxRotation: 0, autoSkipPadding: 12 } },
                y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } } },
      plugins: { legend: { position: 'top', align: 'end' },
                 tooltip: { callbacks: { footer: items => `Total: ${items.reduce((a, i) => a + i.parsed.y, 0)}` } } },
      datasets: { bar: { categoryPercentage: .85, barPercentage: .9 } },
    },
  }));
}

function weightChart(d, pets) {
  const ds = [];
  for (const p of pets) {
    const w = d.weights[p.id];
    if (!w) continue;
    const c = catColor(p.slot);
    const pts = arr => arr.map(([x, y]) => ({ x, y: toUnit(y) }));
    ds.push({ type: 'line', label: p.name, data: pts(w.daily), borderColor: c, backgroundColor: c, borderWidth: 2,
              pointRadius: 0, pointHoverRadius: 4, tension: .25, order: 1 });
    ds.push({ type: 'scatter', label: `${p.name} (visit)`, data: pts(w.visits), backgroundColor: alpha(c, .3),
              borderColor: 'transparent', pointRadius: 2.5, pointHoverRadius: 5, order: 2, _aux: true });
    if (w.official.length)
      ds.push({ type: 'line', label: `${p.name} (Whisker)`, data: pts(w.official), borderColor: c, backgroundColor: c,
                borderDash: [5, 4], borderWidth: 1.5, pointRadius: 3, order: 0, _aux: true });
  }
  s.charts.push(new Chart(el('cWeight'), {
    data: { datasets: ds },
    options: {
      interaction: { mode: 'nearest', intersect: false },
      scales: {
        x: { type: 'linear', min: d.start, max: Date.now(), ticks: { callback: shortDate, maxRotation: 0, autoSkipPadding: 16 } },
        y: { title: { display: true, text: UNIT }, ticks: { callback: v => v.toFixed(1) } },
      },
      plugins: {
        legend: { position: 'top', align: 'end', labels: { filter: (item, data) => !data.datasets[item.datasetIndex]._aux } },
        tooltip: { callbacks: {
          title: items => items.length ? new Date(items[0].parsed.x).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '',
          label: i => `${i.dataset.label}: ${i.parsed.y.toFixed(2)} ${UNIT}`,
        } },
      },
    },
  }));
}

function hourChart(d, pets) {
  const ds = pets.map(p => ({
    label: p.name, data: d.hourly[p.id].map(n => +(n / d.days).toFixed(2)),
    borderColor: catColor(p.slot), backgroundColor: catColor(p.slot), borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: .35,
  }));
  s.charts.push(new Chart(el('cHour'), {
    type: 'line', data: { labels: Array.from({ length: 24 }, (_, h) => `${pad(h)}:00`), datasets: ds },
    options: {
      interaction: { mode: 'index', intersect: false },
      scales: { x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkipPadding: 10 } }, y: { beginAtZero: true } },
      plugins: { legend: { position: 'top', align: 'end' } },
    },
  }));
}

function cyclesChart(d) {
  s.charts.push(new Chart(el('cCycles'), {
    type: 'bar',
    data: { labels: d.dates.map(x => shortDate(x + 'T12:00:00')),
            datasets: [{ label: 'Cycles', data: d.cycles_daily, backgroundColor: NEUTRAL, borderRadius: 2, borderSkipped: 'bottom' }] },
    options: {
      scales: { x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkipPadding: 12 } }, y: { beginAtZero: true, ticks: { precision: 0 } } },
      plugins: { legend: { display: false } },
    },
  }));
}

function snapshotChart(id, d, key) {
  const robots = Object.values(d.snapshots).filter(r => r[key].length);
  if (!robots.length) {
    el(id).parentElement.innerHTML = '<div class="empty" style="padding:1rem">No snapshots yet. One is recorded per sync.</div>';
    return;
  }
  const many = robots.length > 1;
  s.charts.push(new Chart(el(id), {
    type: 'line',
    data: { datasets: robots.map((r, i) => ({
      label: r.name, data: r[key].map(([x, y]) => ({ x, y })),
      borderColor: many ? catColor(i) : NEUTRAL, backgroundColor: many ? catColor(i) : NEUTRAL,
      borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, stepped: true,
    })) },
    options: {
      interaction: { mode: 'nearest', intersect: false },
      scales: { x: { type: 'linear', min: d.start, max: Date.now(), ticks: { callback: shortDate, maxRotation: 0, autoSkipPadding: 16 } },
                y: { beginAtZero: true } },
      plugins: { legend: { display: many },
                 tooltip: { callbacks: { title: items => items.length ? new Date(items[0].parsed.x).toLocaleString() : '' } } },
    },
  }));
}

// ---------------------------------------------------------------- data

async function load() {
  el('range').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.v === s.days));
  const p = new URLSearchParams({ days: s.days });
  if (s.robot) p.set('robot', s.robot);
  try {
    s.data = await api(`/api/analysis?${p}`);
  } catch (e) {
    el('root').innerHTML = `<div class="card"><div class="empty">Couldn't load analysis (${esc(e.message)})</div></div>`;
    return;
  }
  render();
}

el('range').addEventListener('click', e => {
  const v = e.target.dataset.v;
  if (!v) return;
  s.days = v; store.set('lr-range', v); load();
});
el('robot').addEventListener('change', e => { s.robot = e.target.value; load(); });
window.addEventListener('lr-synced', load);

(async function init() {
  try {
    const meta = await api('/api/meta');
    if (meta.robots.length > 1) {
      el('robot').hidden = false;
      el('robot').innerHTML = '<option value="">All robots</option>' +
        meta.robots.map(r => `<option value="${esc(r.serial)}">${esc(r.name)}</option>`).join('');
    }
  } catch {}
  load();
})();
