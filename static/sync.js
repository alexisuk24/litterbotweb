// Manual refresh button + sync status in the top bar. Fires a window 'lr-synced'
// event when a sync finishes so each page can reload its data.
(function () {
  const btn = document.getElementById('syncBtn');
  const label = document.getElementById('syncStatus');
  if (!btn) return;
  let st = null, timer = null, skew = 0, lastUpdated = null;

  const ago = s => {
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  };
  const now = () => Date.now() / 1000 - skew;

  function paint() {
    if (!st) return;
    const busy = st.running || st.pending;
    const cooldown = st.finished ? Math.max(0, 5 - (now() - st.finished)) : 0;
    btn.disabled = busy || cooldown > 0;
    btn.classList.toggle('spinning', busy);
    btn.querySelector('span').textContent =
      busy ? 'Refreshing…' : cooldown > 0 ? `Refresh (${Math.ceil(cooldown)})` : 'Refresh';
    let text = '', cls = '';
    if (st.pending && !st.running) text = st.collector_seen ? 'Starting…' : 'Waiting for collector…';
    else if (st.running) text = `Syncing since ${new Date(st.started * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
    else if (st.error) { text = 'Last sync failed'; cls = 'err'; }
    else if (st.updated) text = `Updated ${ago(now() - st.updated)}`;
    label.textContent = text;
    label.className = cls;
    label.title = 'Show what was updated when';
    paintRetention();
    paintDetails();
  }

  // Tapping the status opens a breakdown of each kind of update and when it last ran.
  label.setAttribute('role', 'button');
  label.tabIndex = 0;
  let details = null;
  const clock = t => {
    const d = new Date(t * 1000);
    const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return d.toDateString() === new Date().toDateString() ? time : `${d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' })} ${time}`;
  };
  const rel = t => t > now() ? `in ${ago(t - now()).replace(' ago', '').replace('just now', 'under a minute')}` : ago(now() - t);
  const TRIGGERS = { scheduled: 'on schedule', manual: 'from Refresh', startup: 'at startup' };

  function detailRows() {
    const rows = [];
    const row = (what, t, note, cls) => rows.push({ what, when: t ? `${rel(t)} · ${clock(t)}` : 'not yet', note, cls });
    row('Robot status', st.status_checked, 'Checked about every minute, to catch each clean cycle as it ends');
    row('New visits and clips', st.data_updated, 'Fetched right after each clean cycle, then again a few minutes later for clips and cat detections');
    if (st.running) rows.push({ what: 'Full sync', when: `running since ${clock(st.started)}`, note: 'Everything: activity, clips, pets and settings' });
    else row('Last full sync', st.finished, st.error ? `Failed: ${st.error}` : `Everything: activity, clips, pets and settings${TRIGGERS[st.trigger] ? ` (${TRIGGERS[st.trigger]})` : ''}`, st.error ? 'err' : '');
    if (st.next_sync && !st.running) row('Next full sync', Math.max(st.next_sync, now()), 'Or sooner with Refresh, or by pulling down on a phone');
    return rows;
  }

  function paintDetails() {
    if (!details || !details.open) return;
    const list = details.querySelector('dl');
    list.replaceChildren(...detailRows().flatMap(r => {
      const dt = document.createElement('dt'), dd = document.createElement('dd'), note = document.createElement('div');
      dt.textContent = r.what;
      dd.textContent = r.when;
      note.textContent = r.note;
      note.className = `note ${r.cls || ''}`;
      dd.append(note);
      return [dt, dd];
    }));
  }

  let byKeyboard = false;
  function openDetails() {
    if (!st) return;
    if (!details) {
      details = document.createElement('dialog');
      details.className = 'confirm sync-details';
      details.setAttribute('aria-labelledby', 'syncDetailsTitle');
      details.innerHTML = '<form method="dialog"><h3 id="syncDetailsTitle">Updates</h3><dl></dl>'
        + '<div class="btns"><button class="btn">Close</button></div></form>';
      details.addEventListener('click', e => { if (e.target === details) details.close(); });  // tap outside
      // closing hands focus back to the label; after a tap that leaves Safari's focus ring on it
      details.addEventListener('close', () => { if (!byKeyboard) label.blur(); });
      document.body.appendChild(details);
    }
    details.showModal();
    paintDetails();
  }
  label.addEventListener('click', () => { byKeyboard = false; openDetails(); });
  label.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); byKeyboard = true; openDetails(); }
  });

  function paintRetention() {
    const r = st.retention || {};
    let note = document.getElementById('retentionNote');
    const show = r.days && r.starts_at && r.starts_at > now();
    let dismissed = null;
    try { dismissed = localStorage.getItem('lr-retention-dismissed'); } catch {}
    if (!show || dismissed === String(r.starts_at)) { if (note) note.remove(); return; }
    if (!note) {
      note = document.createElement('div');
      note.id = 'retentionNote';
      note.className = 'retention-note';
      note.innerHTML = '<span></span><button aria-label="Dismiss" title="Dismiss">✕</button>';
      note.querySelector('button').addEventListener('click', () => {
        try { localStorage.setItem('lr-retention-dismissed', String(r.starts_at)); } catch {}
        note.remove();
      });
      document.querySelector('.topnav').after(note);
    }
    const when = new Date(r.starts_at * 1000).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
    const what = r.small === null ? 'lose their video (a picture is kept)' : 'are shrunk to a small copy';
    note.querySelector('span').textContent = `From ${when}, clips older than ${r.days} days ${what}. Tap 📌 Keep forever on any you want to keep at full quality.`;
  }

  function schedule() {
    clearTimeout(timer);
    const fast = st && (st.running || st.pending || (st.finished && now() - st.finished < 6));
    timer = setTimeout(poll, fast ? 1000 : 30000);
  }

  function apply(next) {
    skew = Date.now() / 1000 - next.now;
    // .updated moves with each full sync and with the fetches after each clean cycle
    if (lastUpdated !== null && next.updated && next.updated !== lastUpdated && !next.running) {
      window.dispatchEvent(new CustomEvent('lr-synced', { detail: next }));
    }
    lastUpdated = next.updated;
    st = next;
    paint();
    schedule();
  }

  async function poll() {
    try { apply(await (await fetch('/api/sync')).json()); }
    catch { schedule(); }
  }

  async function requestSync() {
    btn.disabled = true;
    try {
      const r = await fetch('/api/sync', { method: 'POST' });
      apply(await r.json());
    } catch { poll(); }
  }
  btn.addEventListener('click', requestSync);

  // Pull to refresh on touch screens (the installed app has no browser one): reloads the
  // page's data at once, and starts a sync as the Refresh button does unless one has just run.
  const PULL = 70, MAX = 110;
  let startY = null, pull = 0, ptr = null;

  // a touch in a scrolling box or an overlay (e.g. the Events detail sheet) is left to it
  function scrollsInside(el) {
    for (; el && el !== document.body && el !== document.documentElement; el = el.parentElement) {
      const cs = getComputedStyle(el);
      if (cs.position === 'fixed') return true;
      if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) return true;
    }
    return false;
  }
  function showPull(d, armed) {
    if (!ptr) {
      ptr = document.createElement('div');
      ptr.className = 'ptr';
      ptr.setAttribute('aria-hidden', 'true');
      ptr.innerHTML = '<svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.4M21 3.5V9h-5.5"/></svg>';
      document.body.appendChild(ptr);
    }
    ptr.classList.remove('done', 'spin');
    ptr.classList.toggle('armed', armed);
    ptr.style.transform = `translate(-50%, ${d}px) rotate(${d * 3}deg)`;
    ptr.style.opacity = Math.min(1, d / PULL);
  }
  function hidePull() {
    if (!ptr) return;
    ptr.classList.remove('spin', 'armed');
    ptr.classList.add('done');
    ptr.style.transform = 'translate(-50%, 0)';
    ptr.style.opacity = 0;
  }

  document.addEventListener('touchstart', e => {
    startY = e.touches.length === 1 && window.scrollY <= 0 && !scrollsInside(e.target)
      ? e.touches[0].clientY : null;
    pull = 0;
  }, { passive: true });
  document.addEventListener('touchmove', e => {
    if (startY === null) return;
    const dy = e.touches[0].clientY - startY;
    if (dy <= 0 || window.scrollY > 0) { if (pull) hidePull(); startY = null; pull = 0; return; }
    pull = Math.min(MAX, dy * 0.5);
    if (e.cancelable) e.preventDefault();  // no rubber-band scroll while pulling
    showPull(pull, pull >= PULL);
  }, { passive: false });
  document.addEventListener('touchend', () => {
    if (startY === null) return;
    startY = null;
    if (pull < PULL) { hidePull(); return; }
    ptr.classList.add('spin');
    ptr.style.transform = `translate(-50%, ${PULL}px)`;
    window.dispatchEvent(new CustomEvent('lr-synced', { detail: st }));
    if (st && st.can_refresh) requestSync(); else poll();
    setTimeout(hidePull, 800);
  });
  document.addEventListener('touchcancel', () => { startY = null; hidePull(); });

  setInterval(paint, 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  poll();
})();
