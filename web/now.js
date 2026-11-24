// web/now.js — Now panel (A6): player-facing steering only.
//
// Kept separate from app.js so the panel can be added without touching the
// transcript/streaming code. It observes the /api/state responses that app.js
// already requests, so it adds no extra round trips and needs no access to
// app.js internals (its `state` and `$` are lexical, not on window).
(function () {
  const origFetch = window.fetch;

  function renderNow(s) {
    const panel = document.getElementById('now-panel');
    if (!panel) return;
    const st = s && s.steering;
    if (st && (st.objective || st.open_question)) {
      document.getElementById('now-objective').textContent = st.objective || '—';
      document.getElementById('now-question').textContent = st.open_question || '—';
      panel.style.display = 'block';
    } else {
      panel.style.display = 'none';
    }
  }

  window.fetch = function (url, opts) {
    const result = origFetch.apply(this, arguments);
    if (typeof url === 'string' && url === '/api/state') {
      result.then(res => {
        res.clone().json().then(renderNow).catch(() => {});
      }).catch(() => {});
    }
    return result;
  };
})();
