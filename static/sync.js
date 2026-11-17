// Manual refresh button + sync status in the top bar. Fires a window 'lr-synced'
// event when a sync finishes so each page can reload its data.
(function () {
  const btn = document.getElementById('syncBtn');
  const label = document.getElementById('syncStatus');
  if (!btn) return;
  let st = null, timer = null, skew = 0, lastFinished = null;

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
    else if (st.finished) text = `Updated ${ago(now() - st.finished)}`;
    label.textContent = text;
    label.className = cls;
    label.title = st.error || (st.finished ? new Date(st.finished * 1000).toLocaleString() : '');
    paintRetention();
  }

  function paintRetention() {
    const r = st.retention || {};
    let note = document.getElementById('retentionNote');
    const show = r.days && r.starts_at && r.starts_at > now();
    if (!show) { if (note) note.remove(); return; }
    if (!note) {
      note = document.createElement('div');
      note.id = 'retentionNote';
      note.className = 'retention-note';
      document.querySelector('.topnav').after(note);
    }
    const when = new Date(r.starts_at * 1000).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
    note.textContent = `From ${when}, clips older than ${r.days} days will be deleted automatically. Mark any you want to keep with 📌 Keep forever.`;
  }

  function schedule() {
    clearTimeout(timer);
    const fast = st && (st.running || st.pending || (st.finished && now() - st.finished < 6));
    timer = setTimeout(poll, fast ? 1000 : 30000);
  }

  function apply(next) {
    skew = Date.now() / 1000 - next.now;
    if (lastFinished !== null && next.finished && next.finished !== lastFinished && !next.running) {
      window.dispatchEvent(new CustomEvent('lr-synced', { detail: next }));
    }
    lastFinished = next.finished;
    st = next;
    paint();
    schedule();
  }

  async function poll() {
    try { apply(await (await fetch('/api/sync')).json()); }
    catch { schedule(); }
  }

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const r = await fetch('/api/sync', { method: 'POST' });
      apply(await r.json());
    } catch { poll(); }
  });

  setInterval(paint, 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  poll();
})();
