// Live camera view: the browser is the WebRTC peer; the server only relays signaling
// to the camera (lr_live.py), so video comes straight from the camera.
(() => {
  const MAX_MS = 10 * 60 * 1000;  // don't leave a forgotten tab streaming all night
  const live = { id: null, pc: null, poll: null, cap: null, after: 0, view: null };
  const post = (path, body) => fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}),
  }).then(async r => {
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `${r.status}`);
    return data;
  });

  function msg(text, err = false) {
    el('liveMsg').textContent = text;
    el('liveMsg').className = 'msg' + (err ? ' err' : '');
  }

  function paint() {
    const on = live.id != null;
    el('liveStart').hidden = on;
    el('liveStop').hidden = !on;
    el('liveVideo').hidden = !on;
    el('liveView').hidden = !on || !live.view;
    el('liveSound').hidden = !on;
    el('liveSound').textContent = el('liveEl').muted ? 'Unmute' : 'Mute';
    el('liveView').querySelectorAll('button').forEach(b => {
      b.classList.toggle('on', b.dataset.view === live.view);
      b.setAttribute('aria-checked', b.dataset.view === live.view);
    });
  }

  async function start() {
    el('liveStart').disabled = true;
    msg('Waking the camera…');
    try {
      const s = await post(`/api/live/${encodeURIComponent(SERIAL)}`);
      live.id = s.id;
      live.view = s.view;
      live.after = 0;
      paint();
      // ?relay=1 forces the connection through Whisker's TURN servers, to check their credentials
      const relayOnly = new URLSearchParams(location.search).has('relay');
      const pc = live.pc = new RTCPeerConnection({ iceServers: s.ice_servers,
                                                   iceTransportPolicy: relayOnly ? 'relay' : 'all' });
      pc.addTransceiver('video', { direction: 'recvonly' });
      pc.addTransceiver('audio', { direction: 'recvonly' });
      pc.ontrack = e => {
        if (el('liveEl').srcObject !== e.streams[0]) el('liveEl').srcObject = e.streams[0];
      };
      pc.onicecandidate = e => {
        if (e.candidate && live.id === s.id) {
          post(`/api/live/session/${s.id}/candidates`, e.candidate.toJSON()).catch(() => {});
        }
      };
      pc.onconnectionstatechange = () => {
        if (live.pc !== pc) return;
        const state = pc.connectionState;
        el('liveBadge').hidden = state !== 'connected';
        if (state === 'connected') { msg(''); logRoute(pc); }
        else if (state === 'disconnected') msg('Connection interrupted, waiting…');
        else if (state === 'failed') stop("Couldn't keep a connection to the camera. Start again to retry.", true);
      };
      pollCandidates(s.id);
      await pc.setLocalDescription(await pc.createOffer());
      msg('Connecting to the camera… this can take up to 30 seconds');
      const answer = await post(`/api/live/session/${s.id}/offer`, { sdp: pc.localDescription.sdp });
      if (live.pc !== pc) return;  // stopped while waiting
      await pc.setRemoteDescription({ type: 'answer', sdp: answer.sdp });
      msg('Connecting…');
      clearTimeout(live.cap);
      live.cap = setTimeout(() => stop('Stopped after 10 minutes. Start again to keep watching.'), MAX_MS);
    } catch (e) {
      stop(e.message || "Couldn't start live view", true);
    } finally {
      el('liveStart').disabled = false;
    }
  }

  // Logs how the connection was made: "relay" means through a TURN server.
  async function logRoute(pc) {
    try {
      const stats = await pc.getStats();
      const pair = [...stats.values()].find(r => r.type === 'candidate-pair' && r.nominated && r.state === 'succeeded');
      const local = pair && stats.get(pair.localCandidateId), remote = pair && stats.get(pair.remoteCandidateId);
      if (local) console.info(`Live view connected: this browser via ${local.candidateType}`
        + `${local.url ? ` (${local.url})` : ''}, camera via ${remote?.candidateType}`);
    } catch {}
  }

  // Fetches the camera's ICE candidates; also keeps the server-side session alive.
  async function pollCandidates(id) {
    if (live.id !== id) return;
    try {
      const r = await api(`/api/live/session/${id}/candidates?after=${live.after}`);
      live.after += r.candidates.length;
      for (const c of r.candidates) {
        try { await live.pc?.addIceCandidate(c); } catch {}
      }
    } catch {}
    live.poll = setTimeout(() => pollCandidates(id), 1000);
  }

  function stop(text = '', err = false) {
    const id = live.id;
    clearTimeout(live.poll);
    clearTimeout(live.cap);
    live.pc?.close();
    Object.assign(live, { id: null, pc: null, view: null });
    el('liveEl').srcObject = null;
    el('liveBadge').hidden = true;
    if (id) navigator.sendBeacon(`/api/live/session/${id}/close`);
    msg(text, err);
    paint();
  }

  el('liveStart').addEventListener('click', start);
  el('liveStop').addEventListener('click', () => stop());
  el('liveSound').addEventListener('click', () => {
    el('liveEl').muted = !el('liveEl').muted;
    paint();
  });
  el('liveView').addEventListener('click', async e => {
    const view = e.target.dataset.view;
    if (!view || view === live.view || !live.id) return;
    const before = live.view;
    live.view = view;
    paint();
    try { await post(`/api/live/${encodeURIComponent(SERIAL)}/view`, { view }); }
    catch { live.view = before; paint(); msg("Couldn't switch cameras", true); }
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden && live.id) stop('Paused while the page was hidden.'); });
  window.addEventListener('pagehide', () => { if (live.id) stop(); });
  paint();
})();
