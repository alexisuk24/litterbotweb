#!/usr/bin/env python3
import os
import re
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template_string, request, send_file

app = Flask(__name__)
CLIPS = Path(os.environ.get("CLIPS_PATH", "/clips"))
_DATE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})_")
_TIME_RE = re.compile(r"_(\d{2}-\d{2}-\d{2})\.mp4$")

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Litter Robot Clips</title>
<style>
:root {
  --bg: #0f1117; --surface: #1a1d27; --surface2: #252836;
  --accent: #7c6af7; --teal: #4ecdc4;
  --text: #e2e8f0; --muted: #718096; --border: #2d3748; --r: 12px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }

header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 1rem 1.5rem; display: flex; align-items: center; gap: .75rem; }
header h1 { font-size: 1.15rem; font-weight: 600; }

#robotTabs { display: flex; gap: .5rem; padding: 1rem 1.5rem .5rem; overflow-x: auto; scrollbar-width: none; }
#robotTabs::-webkit-scrollbar { display: none; }
.tab { padding: .45rem 1.2rem; border-radius: 2rem; border: 1px solid var(--border); background: var(--surface); color: var(--muted); cursor: pointer; font-size: .875rem; white-space: nowrap; transition: all .15s; }
.tab.active { background: var(--accent); border-color: var(--accent); color: #fff; }

.layout { display: grid; grid-template-columns: 300px 1fr; gap: 1.25rem; padding: 1rem 1.5rem 2rem; max-width: 1100px; margin: 0 auto; align-items: start; }
.sidebar { order: 1; }
.player { order: 2; }
@media (max-width: 720px) { .layout { grid-template-columns: 1fr; } .sidebar { order: 2; } .player { order: 1; position: static; } }

.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--r); }

.cal-head { display: flex; align-items: center; justify-content: space-between; padding: .875rem 1rem; border-bottom: 1px solid var(--border); }
.cal-title { font-size: .9rem; font-weight: 600; }
.cal-btn { background: none; border: 1px solid var(--border); color: var(--text); width: 28px; height: 28px; border-radius: 6px; cursor: pointer; font-size: 1.1rem; line-height: 1; display: flex; align-items: center; justify-content: center; }
.cal-btn:hover { background: var(--surface2); }
.cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; padding: .75rem; }
.dow { text-align: center; font-size: .65rem; font-weight: 700; color: var(--muted); text-transform: uppercase; padding: .2rem 0 .5rem; }
.day { aspect-ratio: 1; display: flex; align-items: center; justify-content: center; font-size: .8rem; border-radius: 8px; color: var(--muted); position: relative; user-select: none; }
.day.cur { cursor: pointer; }
.day.cur:hover { background: var(--surface2); }
.day.has-video { font-weight: 700; color: var(--text); }
.day.has-video::after { content: ''; position: absolute; bottom: 3px; width: 4px; height: 4px; border-radius: 50%; background: var(--teal); }
.day.sel { background: var(--accent) !important; color: #fff !important; }
.day.sel::after { background: #fff !important; }
.day.today { outline: 1.5px solid var(--accent); outline-offset: -1.5px; }

.clip-head { padding: .65rem 1rem; font-size: .7rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; border-top: 1px solid var(--border); }
.clip-item { display: flex; align-items: center; gap: .75rem; padding: .65rem 1rem; cursor: pointer; transition: background .1s; }
.clip-item:last-child { border-radius: 0 0 var(--r) var(--r); }
.clip-item:hover { background: var(--surface2); }
.clip-item.active { background: var(--surface2); border-left: 3px solid var(--accent); padding-left: calc(1rem - 3px); }
.clip-time { font-size: .9rem; font-weight: 600; font-variant-numeric: tabular-nums; }
.play-icon { color: var(--muted); font-size: .7rem; }
.clip-item.active .play-icon { color: var(--accent); }

.player { position: sticky; top: 1rem; }
video { width: 100%; aspect-ratio: 16/9; background: #000; display: block; border-radius: var(--r) var(--r) 0 0; }
.player-meta { padding: .75rem 1rem; display: flex; align-items: center; gap: .5rem; }
.player-info { flex: 1; text-align: center; }
.player-name { font-size: .95rem; font-weight: 600; }
.player-sub { font-size: .8rem; color: var(--muted); margin-top: .2rem; }
.nav-btn { background: none; border: 1px solid var(--border); color: var(--text); width: 36px; height: 36px; border-radius: 8px; cursor: pointer; font-size: 1.5rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; transition: background .1s; }
.nav-btn:hover:not(:disabled) { background: var(--surface2); }
.nav-btn:disabled { opacity: .25; cursor: default; }
.share-btn { background: none; border: 1px solid var(--border); color: var(--muted); font-size: .75rem; cursor: pointer; padding: .2rem .6rem; border-radius: 4px; margin-top: .4rem; display: inline-flex; align-items: center; gap: .25rem; transition: all .1s; }
.share-btn:hover { color: var(--text); border-color: var(--text); }

.empty { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 2.5rem 1rem; gap: .5rem; color: var(--muted); font-size: .875rem; text-align: center; }
.empty-icon { font-size: 2rem; }
</style>
</head>
<body>
<header><span style="font-size:1.4rem">🐱</span><h1>Litter Robot Clips</h1></header>
<div id="robotTabs"></div>
<div class="layout">
  <div class="card player" id="player">
    <div class="empty"><div class="empty-icon">📹</div><div>Select a clip to play</div></div>
  </div>
  <div class="sidebar">
    <div class="card">
      <div class="cal-head">
        <button class="cal-btn" id="prev">&#8249;</button>
        <span class="cal-title" id="calTitle"></span>
        <button class="cal-btn" id="next">&#8250;</button>
      </div>
      <div class="cal-grid" id="calGrid"></div>
    </div>
    <div class="card" id="clipList" style="margin-top:1rem;display:none"></div>
  </div>
</div>
<script>
const MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];
const DOW = ['Su','Mo','Tu','We','Th','Fr','Sa'];

let s = { robot:null, dates:new Set(), year:0, month:0, selDate:null, videos:[], activeIdx:-1 };

const el = id => document.getElementById(id);
const enc = s => encodeURIComponent(s);
const pad = n => String(n).padStart(2,'0');
const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const today = () => { const d=new Date(); return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`; };

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

async function init() {
  let robots = [];
  try { robots = await api('/api/robots'); } catch {}
  const tabs = el('robotTabs');
  if (!robots.length) {
    tabs.innerHTML = '<span style="color:var(--muted);font-size:.875rem;padding:.5rem 0">No robots found — clips appear here once downloaded</span>';
    return;
  }
  tabs.innerHTML = robots.map(r =>
    `<button class="tab" onclick="selectRobot('${esc(r)}')">${esc(r)}</button>`
  ).join('');
  const params = new URLSearchParams(location.search);
  const pr = params.get('robot'), pd = params.get('date'), pc = params.get('clip');
  await selectRobot((pr && robots.includes(pr)) ? pr : robots[0]);
  if (pr && pd) {
    await selectDate(pd);
    if (pc) { const i = s.videos.findIndex(v => v.filename === pc); if (i !== -1) playVideo(i); }
  }
}

async function selectRobot(robot) {
  s.robot = robot;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.textContent === robot));
  try { s.dates = new Set(await api(`/api/dates/${enc(robot)}`)); } catch { s.dates = new Set(); }
  const now = new Date();
  s.year = now.getFullYear();
  s.month = now.getMonth();
  renderCal();
  const t = today();
  const pick = s.dates.has(t) ? t : [...s.dates].sort().at(-1);
  if (pick) await selectDate(pick);
  else { el('clipList').style.display='none'; }
}

async function selectDate(d) {
  s.selDate = d;
  const [y,m] = d.split('-').map(Number);
  s.year = y; s.month = m-1;
  renderCal();
  try { s.videos = await api(`/api/videos/${enc(s.robot)}/${d}`); } catch { s.videos = []; }
  s.activeIdx = -1;
  renderClipList();
  if (s.videos.length) playVideo(0);
}

function renderCal() {
  el('calTitle').textContent = `${MONTHS[s.month]} ${s.year}`;
  const first = new Date(s.year, s.month, 1).getDay();
  const total = new Date(s.year, s.month+1, 0).getDate();
  const t = today();
  let h = DOW.map(d => `<div class="dow">${d}</div>`).join('');
  for (let i=0; i<first; i++) h += '<div class="day"></div>';
  for (let d=1; d<=total; d++) {
    const ds = `${s.year}-${pad(s.month+1)}-${pad(d)}`;
    const c = ['day','cur', s.dates.has(ds)?'has-video':'', ds===s.selDate?'sel':'', ds===t?'today':''].filter(Boolean).join(' ');
    h += `<div class="${c}" onclick="selectDate('${ds}')">${d}</div>`;
  }
  el('calGrid').innerHTML = h;
}

function renderClipList() {
  const e = el('clipList');
  if (!s.videos.length) { e.style.display='none'; return; }
  const [y,m,d] = s.selDate.split('-');
  e.style.display = '';
  e.innerHTML = `<div class="clip-head">${MONTHS[+m-1]} ${+d}, ${y} &mdash; ${s.videos.length} clip${s.videos.length!==1?'s':''}</div>` +
    s.videos.map((v,i) => `
      <div class="clip-item${i===s.activeIdx?' active':''}" onclick="playVideo(${i})">
        <span class="play-icon">&#9654;</span>
        <span class="clip-time">${v.time}</span>
      </div>`).join('');
}

function playVideo(idx) {
  s.activeIdx = idx;
  renderClipList();
  const v = s.videos[idx];
  const url = `/video/${enc(s.robot)}/${enc(v.filename)}`;
  const [y,m,d] = s.selDate.split('-');
  el('player').innerHTML = `
    <video controls autoplay playsinline preload="metadata" src="${url}"></video>
    <div class="player-meta">
      <button class="nav-btn" onclick="playVideo(${idx-1})" ${idx === 0 ? 'disabled' : ''}>&#8249;</button>
      <div class="player-info">
        <div class="player-name">${esc(s.robot)} &mdash; ${v.time}</div>
        <div class="player-sub">${MONTHS[+m-1]} ${+d}, ${y}</div>
        <button class="share-btn" id="shareBtn" onclick="shareClip()">Share &#8599;</button>
      </div>
      <button class="nav-btn" onclick="playVideo(${idx+1})" ${idx + 1 >= s.videos.length ? 'disabled' : ''}>&#8250;</button>
    </div>`;
  const vid = el('player').querySelector('video');
  if (vid && idx + 1 < s.videos.length) {
    vid.addEventListener('ended', () => playVideo(idx + 1));
  }
}

async function shareClip() {
  const v = s.videos[s.activeIdx];
  if (!v) return;
  const videoUrl = `/video/${enc(s.robot)}/${enc(v.filename)}`;
  const btn = el('shareBtn');
  const origText = btn ? btn.textContent : '';

  // Mobile: fetch file and hand it to the native share sheet
  if (navigator.canShare) {
    if (btn) { btn.textContent = 'Loading…'; btn.disabled = true; }
    try {
      const res = await fetch(videoUrl);
      const blob = await res.blob();
      const file = new File([blob], v.filename, { type: 'video/mp4' });
      if (navigator.canShare({ files: [file] })) {
        await navigator.share({ files: [file], title: `${s.robot} — ${v.time}` });
        return;
      }
    } catch(e) {
      if (e.name === 'AbortError') return;
    } finally {
      if (btn) { btn.textContent = origText; btn.disabled = false; }
    }
  }

  // Fallback: trigger a download
  const a = document.createElement('a');
  a.href = videoUrl;
  a.download = v.filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

el('prev').onclick = () => { if (--s.month < 0) { s.month=11; s.year--; } renderCal(); };
el('next').onclick = () => { if (++s.month > 11) { s.month=0; s.year++; } renderCal(); };

init();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/robots")
def list_robots():
    if not CLIPS.exists():
        return jsonify([])
    return jsonify(sorted(p.name for p in CLIPS.iterdir() if p.is_dir()))


@app.route("/api/dates/<robot>")
def list_dates(robot):
    d = CLIPS / robot
    if not d.is_dir():
        abort(404)
    dates = set()
    for f in d.glob("*.mp4"):
        m = _DATE_RE.search(f.name)
        if m:
            dates.add(m.group(1))
    return jsonify(sorted(dates))


@app.route("/api/videos/<robot>/<date>")
def list_videos(robot, date):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(400)
    d = CLIPS / robot
    if not d.is_dir():
        abort(404)
    videos = []
    for f in sorted(d.glob(f"*_{date}_*.mp4"), reverse=True):
        m = _TIME_RE.search(f.name)
        videos.append({"filename": f.name, "time": m.group(1).replace("-", ":") if m else ""})
    return jsonify(videos)


@app.route("/video/<robot>/<filename>")
def serve_video(robot, filename):
    filepath = (CLIPS / robot / filename).resolve()
    if not filepath.is_relative_to(CLIPS.resolve()):
        abort(403)
    if not filepath.is_file():
        abort(404)

    size = filepath.stat().st_size
    range_header = request.headers.get("Range")

    if not range_header:
        resp = send_file(filepath, mimetype="video/mp4")
        resp.headers["Accept-Ranges"] = "bytes"
        return resp

    byte_range = range_header.replace("bytes=", "").split("-")
    start = int(byte_range[0]) if byte_range[0] else 0
    end = int(byte_range[1]) if len(byte_range) > 1 and byte_range[1] else size - 1
    end = min(end, size - 1)
    length = end - start + 1

    def stream():
        with open(filepath, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": "video/mp4",
    }
    return Response(stream(), status=206, headers=headers, direct_passthrough=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=2287, debug=False)
