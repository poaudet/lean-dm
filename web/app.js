// web/app.js — Front-end client for lean-dm
const $ = id => document.getElementById(id);
let state = null, busy = false, currentTab = 'led', lastSent = '', abortCtrl = null, streamTimer = null;
const sideDirty = new Set();
const sideSaved = {};
const sideSaving = new Set();
let notesRequest = 0;

// Safe markdown parser
function esc(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function md(t) {
  t = esc(t);
  const lines = t.split('\n');
  let out = '', inList = false;
  for (let l of lines) {
    if (/^#{1,3} /.test(l)) {
      if (inList) { out += '</ul>'; inList = false; }
      const n = l.match(/^#+/)[0].length;
      out += `<h${n}>${l.replace(/^#+ /, '')}</h${n}>`;
    } else if (/^[-*] /.test(l)) {
      if (!inList) { out += '<ul>'; inList = true; }
      out += `<li>${l.slice(2)}</li>`;
    } else if (/^---+$/.test(l)) {
      if (inList) { out += '</ul>'; inList = false; }
      out += '<hr>';
    } else if (l.trim() === '') {
      if (inList) { out += '</ul>'; inList = false; }
    } else {
      if (inList) { out += '</ul>'; inList = false; }
      out += `<p>${l}</p>`;
    }
  }
  if (inList) out += '</ul>';
  return out
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/\*(.+?)\*/g, '<i>$1</i>');
}

// Toasts
function toast(msg, type = '') {
  let c = $('toast-container');
  if (!c) {
    c = document.createElement('div');
    c.id = 'toast-container';
    c.className = 'toast-container';
    document.body.appendChild(c);
  }
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  c.appendChild(el);
  setTimeout(() => el.classList.add('show'), 10);
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 250);
  }, 4000);
}

// Log & Messages
function addMsg(cls, text) {
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  if (cls === 'dm') d.innerHTML = md(text);
  else d.textContent = text;
  $('log').appendChild(d);
  toBottom();
  return d;
}

function toBottom() {
  const l = $('log');
  l.scrollTop = l.scrollHeight;
}

function renderLog(s) {
  const log = $('log');
  log.innerHTML = '';
  if (!s.history || !s.history.length) {
    addMsg('dm', 'New campaign — open with your character concept and the scene you want. This first message is pinned as the premise.');
    return;
  }
  const folded = s.folded || 0;
  const historyStart = s.history_start || 0;
  s.history.forEach((m, idx) => {
    const absoluteIdx = historyStart + idx;
    if (folded > 0 && absoluteIdx === folded) {
      const div = document.createElement('div');
      div.className = 'fold-marker';
      div.textContent = 'folded above into chapters';
      log.appendChild(div);
    }
    addMsg(m.role === 'user' ? 'user' : 'dm', m.content);
  });
  toBottom();
}

// Context meter update
function updateMeter(s) {
  const track = $('meter-track'), fill = $('meter-fill'), cached = $('meter-cached');
  if (!track || !fill) return;
  const budget = s.ctx_budget_tok || 350000;
  const est = s.est_tokens || 0;
  const pct = Math.min(100, Math.round((est / budget) * 100));
  fill.style.width = pct + '%';
  if (s.cached && s.prompt_tokens) {
    const cPct = Math.min(100, Math.round((s.cached / budget) * 100));
    cached.style.width = cPct + '%';
  } else {
    cached.style.width = '0%';
  }
}

// Tabs & Sidebar
function setTab(t) {
  if (sideDirty.size && t !== currentTab) {
    toast('Save your sidebar edits before changing tabs', 'err');
    return;
  }
  currentTab = t;
  ['prem', 'sum', 'led', 'notes', 'aud'].forEach(x => {
    const el = $('t-' + x);
    if (el) el.classList.toggle('on', x === t);
  });
  document.querySelectorAll('[data-panel]').forEach(panel => {
    panel.hidden = panel.dataset.panel !== t;
  });
  renderSide();
}

function initSide() {
  const f = $('side-content');
  f.innerHTML = `
    <div data-panel="prem" hidden>
      <h3>Premise</h3>
      <textarea id="prem-text" rows="10" style="width:100%"></textarea>
      <button data-action="save-premise">Save Premise</button>
      <h3 style="margin-top:16px">Style Notes</h3>
      <textarea id="style-notes-text" rows="4" style="width:100%" placeholder="Custom writing style instructions..."></textarea>
      <button class="ghost" data-action="save-style">Save Style Notes</button>
    </div>
    <div data-panel="sum" id="summary-content" hidden></div>
    <div data-panel="led">
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <input id="ledger-search" placeholder="Search entries…" style="flex:1">
        <select id="ledger-filter">
          <option value="all">All kinds</option>
          ${['player','person','place','item','promise','clock','rule','thread'].map(k=>`<option value="${k}">${k}</option>`).join('')}
        </select>
        <button class="ghost" data-action="rebuild">Rebuild</button>
      </div>
      <div id="ledger-items" style="margin-top:10px"></div>
    </div>
    <div data-panel="notes" hidden>
      <div style="display:flex;gap:8px;margin-bottom:8px">
        <button class="ghost" data-action="insert-ooc">Insert // prefix</button>
      </div>
      <div id="notes-items"></div>
    </div>
    <div data-panel="aud" hidden>
      <div style="display:flex;gap:8px;margin-bottom:8px">
        <button class="ghost" data-action="audit">Run Continuity Audit</button>
      </div>
      <div id="audit-content"></div>
    </div>
  `;
}

function renderSide() {
  if (!state) return;
  for (const [id, value] of [['prem-text', state.premise || ''], ['style-notes-text', state.style_notes || '']]) {
    const el = $(id);
    if (!sideDirty.has(id) && document.activeElement !== el) {
      el.value = value;
      sideSaved[id] = value;
    }
  }
  if (!$('summary-content').contains(document.activeElement)) {
    $('summary-content').innerHTML = md(state.summary || '(empty — the narrator sees the full history; chapters start when it passes the fold budget)');
  }
  if (!$('audit-content').contains(document.activeElement)) {
    $('audit-content').innerHTML = md(state.audit || '(none yet — press “run audit”)');
  }
  renderLedgerTab();
  if (currentTab === 'notes') renderNotesTab();
}

function renderLedgerTab() {
  const L = state.ledger || [];
  const filterKind = $('ledger-filter').value;
  const search = $('ledger-search').value.toLowerCase();

  const container = $('ledger-items');
  if (container.contains(document.activeElement)) return;
  container.innerHTML = '';
  const filtered = L.filter(e => {
    if (filterKind !== 'all' && (e.kind || 'thread') !== filterKind) return false;
    if (search && !(e.name||'').toLowerCase().includes(search) && !(e.state||'').toLowerCase().includes(search)) return false;
    return true;
  });

  if (!filtered.length) {
    container.innerHTML = '<p style="color:var(--text-muted)">(No matching ledger entries)</p>';
    return;
  }

  filtered.forEach(e => {
    const item = document.createElement('div');
    item.style.padding = '8px';
    item.style.border = '1px solid var(--border)';
    item.style.borderRadius = '8px';
    item.style.marginBottom = '6px';
    item.style.background = 'var(--bg)';
    item.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center">
        <b>${esc(e.name || e.id)}</b>
        <span style="font-size:0.75em;color:var(--text-muted)">${esc(e.kind || 'thread')}${e.pinned ? ' 📌' : ''}</span>
      </div>
      <div style="font-size:0.85em;margin:4px 0">${esc(e.state || '')}</div>
      <div style="display:flex;gap:4px;font-size:0.75em">
        <button class="ghost" style="padding:2px 6px" data-action="pin-ledger" data-id="${esc(e.id)}" data-pinned="${!e.pinned}">${e.pinned ? 'Unpin' : 'Pin'}</button>
        <button class="ghost" style="padding:2px 6px" data-action="delete-ledger" data-id="${esc(e.id)}">Delete</button>
      </div>
    `;
    container.appendChild(item);
  });
}

function renderNotesTab(force = false) {
  const request = ++notesRequest;
  fetch('/api/notes').then(r => r.json()).then(data => {
    if (request !== notesRequest || currentTab !== 'notes') return;
    if (!force && $('notes-items').contains(document.activeElement)) return;
    const notes = data.notes || [];
    let h = '';
    if (!notes.length) {
      h += '<p style="color:var(--text-muted)">(No OOC notes yet. Use //, ooc:, or note to dm: in the chat input.)</p>';
    } else {
      notes.slice().reverse().forEach(n => {
        h += `
          <div style="padding:10px;border:1px solid var(--border);border-radius:8px;margin-bottom:8px;background:var(--bg)">
            <div style="font-weight:600;font-size:0.85em;color:var(--accent-light)">${esc(n.q)}</div>
            <div style="font-size:0.85em;margin:6px 0">${esc(n.a)}</div>
            <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px">
              <span style="font-size:0.75em;color:var(--text-muted)">${new Date((n.ts || 0) * 1000).toLocaleTimeString()}</span>
              ${n.canon ? '<span style="font-size:0.75em;color:var(--success)">✓ Canon</span>' : `<button class="ghost" style="padding:3px 8px;font-size:0.75em" data-action="make-canon" data-id="${esc(n.id)}">Make Canon</button>`}
            </div>
          </div>
        `;
      });
    }
    $('notes-items').innerHTML = h;
  }).catch(() => toast('Failed to load notes', 'err'));
}

function insertOOC() {
  const i = $('in');
  i.value = '// ' + i.value.replace(/^(\/\/|ooc:|note to dm:)\s*/i, '');
  i.focus();
}

function makeCanon(id) {
  fetch('/api/notes/' + encodeURIComponent(id) + '/canon', { method: 'POST' }).then(r => r.json()).then(d => {
    if (d.ok) {
      toast('Made canon successfully', 'success');
      return refresh(false).then(() => {
        if (currentTab === 'notes') renderNotesTab(true);
      });
    } else {
      toast(d.error || 'Failed to make canon', 'err');
    }
  }).catch(() => toast('Failed to make canon', 'err'));
}

function updateSideDirty(id) {
  if (sideSaving.has(id) || $(id).value !== sideSaved[id]) sideDirty.add(id);
  else sideDirty.delete(id);
}

function savePremise() {
  if (sideSaving.has('prem-text')) return;
  const text = $('prem-text').value;
  sideSaving.add('prem-text');
  updateSideDirty('prem-text');
  fetch('/api/premise', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text })
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      sideSaved['prem-text'] = text;
      toast('Premise saved', 'success');
      return refresh(false);
    }
    else toast(d.error || 'Failed to save', 'err');
  }).catch(() => toast('Failed to save premise', 'err')).finally(() => {
    sideSaving.delete('prem-text');
    updateSideDirty('prem-text');
  });
}

function saveStyleNotes() {
  if (sideSaving.has('style-notes-text')) return;
  const notes = $('style-notes-text').value;
  sideSaving.add('style-notes-text');
  updateSideDirty('style-notes-text');
  fetch('/api/config?scope=campaign', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ changes: { style_notes: notes } })
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      sideSaved['style-notes-text'] = notes;
      toast('Style notes saved', 'success');
      return refresh(false);
    }
    else toast('Failed to save style notes', 'err');
  }).catch(() => toast('Failed to save style notes', 'err')).finally(() => {
    sideSaving.delete('style-notes-text');
    updateSideDirty('style-notes-text');
  });
}

function pinLedger(id, pinned) {
  fetch('/api/ledger', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'pin', id, pinned })
  }).then(r => r.json()).then(d => {
    if (d.ok) refresh(false);
  });
}

function deleteLedger(id) {
  if (!confirm('Delete ledger entry ' + id + '?')) return;
  fetch('/api/ledger', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'delete', id })
  }).then(r => r.json()).then(d => {
    if (d.ok) refresh(false);
  });
}

// Refresh state
function refresh(full = true) {
  return fetch('/api/state').then(r => r.json()).then(s => {
    state = s;
    $('tn').textContent = s.turns || 0;
    $('ctx').textContent = s.ctx || '';
    $('warn').textContent = s.configured ? '' : '⚠ not configured — open ⚙ config';
    if (s.auth_warning) {
      $('auth-warn-banner').style.display = 'block';
      $('auth-warn-banner').textContent = s.auth_warning;
    } else {
      $('auth-warn-banner').style.display = 'none';
    }

    // Arc chip
    const chip = $('arc-chip');
    if (chip) {
      if (s.arc_chip) {
        chip.style.display = 'inline-block';
        chip.textContent = s.arc_chip;
      } else {
        chip.style.display = 'none';
      }
    }

    // Quick controls sync
    if ($('qc-diff') && s.difficulty) $('qc-diff').value = s.difficulty;
    if ($('qc-preset') && s.style_preset) $('qc-preset').value = s.style_preset;

    // Undo / Retry guards
    const btnUndo = $('btn-undo'), btnRetry = $('btn-retry');
    if (btnUndo) {
      btnUndo.disabled = (s.can_undo === false);
      btnUndo.title = s.undo_blocked_reason || '';
    }
    if (btnRetry) {
      btnRetry.disabled = (s.can_retry === false);
      btnRetry.title = s.retry_blocked_reason || '';
    }

    const j = s.job;
    $('job').textContent = j && j.running ? ('⏳ ' + j.name + '…') : (j && j.name ? (j.name + ': ' + j.msg) : '');
    updateMeter(s);
    if (full) renderLog(s);
    renderSide();
  });
}

// Sending turns with SSE streaming
function send() {
  const i = $('in'), t = i.value.trim();
  if (!t || busy) return;
  busy = true;
  lastSent = t;
  i.value = '';
  i.style.height = '44px';

  const u = addMsg('user', t);
  const w = addMsg('dm', '…');
  $('btn-stop').style.display = 'inline-flex';

  abortCtrl = new AbortController();
  let fullText = '';
  let oocTurn = false;     // OOC answers are not in history; keep them on screen
  let committed = false;   // set once the server reports the exchange is saved
  let stageEl = document.createElement('span');
  stageEl.style.fontSize = '0.85em';
  stageEl.style.color = 'var(--text-muted)';
  w.innerHTML = '';
  w.appendChild(stageEl);

  let startT = Date.now();
  streamTimer = setInterval(() => {
    const sec = ((Date.now() - startT) / 1000).toFixed(1);
    $('timer-label').textContent = sec + 's';
  }, 100);

  fetch('/api/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: t }),
    signal: abortCtrl.signal
  }).then(res => {
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    function readChunk() {
      reader.read().then(({ done, value }) => {
        if (done) {
          finishTurn(oocTurn);
          return;
        }
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split('\n\n');
        buffer = parts.pop();
        for (let part of parts) {
          if (!part.startsWith('data: ')) continue;
          const dataStr = part.slice(6).trim();
          if (dataStr === '[DONE]') {
            finishTurn(oocTurn);
            return;
          }
          try {
            const data = JSON.parse(dataStr);
            if (data.stage) {
              stageEl.textContent = '⏳ ' + data.stage + '…';
              if (data.stage === 'recording') {
                committed = true;                       // exchange is already on disk
                $('btn-stop').style.display = 'none';   // stopping now cannot undo it
              }
            } else if (data.error) {
              u.remove();
              w.textContent = '[error: ' + data.error + (committed ? '' : ' — nothing saved; move restored]');
              if (!committed) i.value = t;
              cleanup();
              if (committed) refresh();
              return;
            } else if (data.chunk) {
              if (data.ooc) oocTurn = true;
              if (stageEl.parentNode) stageEl.remove();
              fullText += data.chunk;
              w.innerHTML = md(fullText);
              toBottom();
            }
          } catch (e) {}
        }
        readChunk();
      }).catch(err => {
        if (committed) {              // saved server-side: trust the server, not the optimistic UI
          toast('Connection dropped after the turn was saved - reloading', 'err');
          cleanup();
          refresh();
          return;
        }
        if (err.name === 'AbortError') {
          u.remove();
          w.textContent = '[stopped — nothing was saved]';
          i.value = t;
        } else {
          u.remove();
          w.textContent = '[error: ' + err.message + ' — nothing saved; move restored]';
          i.value = t;
        }
        cleanup();
      });
    }
    readChunk();
  }).catch(err => {
    u.remove();
    w.textContent = '[network error: ' + err.message + ']';
    i.value = t;
    cleanup();
  });
}

function finishTurn(keepLog) {
  cleanup();
  refresh(!keepLog);
}

function cleanup() {
  busy = false;
  $('btn-stop').style.display = 'none';
  clearInterval(streamTimer);
  $('timer-label').textContent = '';
  abortCtrl = null;
}

function stopTurn() {
  if (abortCtrl) abortCtrl.abort();
}

function undo() {
  if (busy) return;
  busy = true;
  fetch('/api/undo', { method: 'POST' }).then(r => r.json()).then(d => {
    if (d.error) { toast(d.error, 'err'); return; }
    $('in').value = d.text || '';
    toast('Undone last turn', 'success');
    return refresh();
  }).finally(() => { busy = false; });
}

function retry() {
  if (busy) return;
  busy = true;
  const w = addMsg('dm', '…');
  fetch('/api/retry', { method: 'POST' }).then(r => r.json()).then(d => {
    if (d.error) w.textContent = '[error: ' + d.error + ']';
    else refresh();
  }).catch(() => { w.textContent = '[network error]'; }).finally(() => { busy = false; });
}

function act(path) {
  fetch(path, { method: 'POST' }).then(r => r.json()).then(d => {
    if (d.error) toast(d.error, 'err');
    else toast('Job started', 'success');
    return refresh(false);
  });
}

// Quick control handlers
function updateQuickControl(field, val) {
  fetch('/api/config?scope=campaign', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ changes: { [field]: val } })
  }).then(r => r.json()).then(d => {
    if (d.ok) toast('Updated ' + field, 'success');
    else toast('Failed to update', 'err');
  });
}

// Input auto-grow and keyboard shortcuts
window.addEventListener('DOMContentLoaded', () => {
  initSide();
  document.addEventListener('click', e => {
    const tab = e.target.closest('[data-tab]');
    if (tab) { setTab(tab.dataset.tab); return; }
    const button = e.target.closest('[data-action]');
    if (!button) return;
    const actions = {
      undo, retry, send, stop: stopTurn,
      'save-premise': savePremise, 'save-style': saveStyleNotes,
      rebuild: () => act('/api/rebuild'), audit: () => act('/api/audit'),
      'insert-ooc': insertOOC, 'make-canon': () => makeCanon(button.dataset.id),
      'pin-ledger': () => pinLedger(button.dataset.id, button.dataset.pinned === 'true'),
      'delete-ledger': () => deleteLedger(button.dataset.id)
    };
    if (actions[button.dataset.action]) actions[button.dataset.action]();
  });
  document.addEventListener('input', e => {
    if (e.target.id === 'ledger-search' && state) renderLedgerTab();
    if (e.target.id === 'prem-text' || e.target.id === 'style-notes-text') {
      updateSideDirty(e.target.id);
    }
  });
  document.addEventListener('change', e => {
    if (e.target.id === 'ledger-filter' && state) renderLedgerTab();
    if (e.target.dataset.control) updateQuickControl(e.target.dataset.control, e.target.value);
  });
  $('side-content').addEventListener('focusout', e => {
    if ((e.target.id === 'prem-text' || e.target.id === 'style-notes-text') && !sideDirty.has(e.target.id)) {
      renderSide();
    } else if (e.target.closest('#ledger-items, #notes-items, #summary-content, #audit-content')) {
      queueMicrotask(() => renderSide());
    }
  });
  const inp = $('in');
  if (inp) {
    inp.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send();
      } else if (e.key === 'ArrowUp' && inp.value === '' && lastSent) {
        inp.value = lastSent;
      }
    });
    inp.addEventListener('input', () => {
      inp.style.height = '44px';
      inp.style.height = Math.min(140, inp.scrollHeight) + 'px';
    });
  }

  // Drawer mobile toggle
  const drawerBtn = $('drawer-toggle');
  if (drawerBtn) {
    drawerBtn.addEventListener('click', () => {
      $('side').classList.toggle('open');
      $('drawer-overlay').classList.toggle('open');
    });
  }
  const overlay = $('drawer-overlay');
  if (overlay) {
    overlay.addEventListener('click', () => {
      $('side').classList.remove('open');
      overlay.classList.remove('open');
    });
  }

  setTab('led');
  refresh();
  setInterval(() => {
    if (!busy && state && state.job && state.job.running) refresh(false);
  }, 5000);
});
