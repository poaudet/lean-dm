const webState = {
  premise: 'original', style_notes: 'style', configured: true,
  ledger: [
    {id: "sword'\"1", name: 'Sword', state: 'bright', kind: 'item'},
    {id: 'coin', name: 'Coin', state: 'old', kind: 'item'}
  ]
};
let webNotes = [{id: 'note-1', q: 'question', a: 'answer', canon: false}];
let finishSave, finishNotes, holdNotes = false, lastLedgerAction, lastConfig;
window.fetch = (url, opts = {}) => {
  const json = data => Promise.resolve({json: async () => structuredClone(data)});
  if (url === '/api/state') return json(webState);
  if (url === '/api/notes') {
    if (holdNotes) return new Promise(resolve => {
      finishNotes = () => resolve({json: async () => ({notes: [{q: 'stale response'}]})});
    });
    return json({notes: webNotes});
  }
  if (url === '/api/notes/note-1/canon') {
    webNotes[0].canon = true;
    webState.ledger[0].state = 'canon fact';
    return json({ok: true});
  }
  if (url === '/api/premise') return new Promise(resolve => {
    finishSave = () => {
      webState.premise = JSON.parse(opts.body).text;
      resolve({json: async () => ({ok: true})});
    };
  });
  if (url === '/api/config?scope=campaign') {
    lastConfig = JSON.parse(opts.body).changes;
    if ('style_notes' in lastConfig) webState.style_notes = lastConfig.style_notes;
    return json({ok: true});
  }
  if (url === '/api/ledger') {
    lastLedgerAction = JSON.parse(opts.body);
    return json({ok: true});
  }
  throw new Error('Unexpected request: ' + url);
};

window.addEventListener('load', async () => {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const tick = () => new Promise(resolve => setTimeout(resolve, 10));
  const edit = (element, value) => {
    element.value = value;
    element.dispatchEvent(new Event('input', {bubbles: true}));
  };
  try {
    await refresh();
    const search = document.getElementById('ledger-search');
    search.focus();
    edit(search, 'swo');
    check(document.querySelectorAll('#ledger-items > div').length === 1, 'focused search filters entries');
    webState.ledger[0].state = 'changed';
    await refresh(false);
    check(document.getElementById('ledger-search') === search && document.activeElement === search &&
      search.value === 'swo' && document.getElementById('ledger-items').textContent.includes('changed'),
      'refresh preserves search shell and focus while updating entries');
    const pin = document.querySelector('[data-action=pin-ledger]');
    pin.focus();
    webState.ledger[0].state = 'updated while focused';
    await refresh(false);
    check(document.querySelector('[data-action=pin-ledger]') === pin, 'refresh preserves focused ledger action');
    pin.blur();
    await tick();
    check(document.getElementById('ledger-items').textContent.includes('updated while focused'),
      'deferred ledger update applies on blur');
    document.querySelector('[data-action=pin-ledger]').click();
    await tick();
    check(lastLedgerAction.id === "sword'\"1" && lastLedgerAction.pinned, 'delegated pin preserves ID');
    const difficulty = document.getElementById('qc-diff');
    difficulty.value = 'hard';
    difficulty.dispatchEvent(new Event('change', {bubbles: true}));
    await tick();
    check(lastConfig.difficulty === 'hard', 'delegated quick control updates configuration');

    document.getElementById('t-prem').click();
    const premise = document.getElementById('prem-text');
    const style = document.getElementById('style-notes-text');
    premise.focus();
    edit(premise, 'draft');
    edit(style, 'style draft');
    webState.premise = 'remote';
    await refresh(false);
    check(premise.value === 'draft' && style.value === 'style draft', 'both dirty edits survive refresh');
    document.querySelector('[data-action=save-premise]').click();
    edit(premise, 'newer draft');
    finishSave();
    await tick();
    check(premise.value === 'newer draft' && style.value === 'style draft', 'in-flight and other edits survive save');
    document.getElementById('t-led').click();
    check(!document.querySelector('[data-panel=prem]').hidden, 'dirty edits block tab change');
    document.querySelector('[data-action=save-premise]').click();
    finishSave();
    await tick();
    document.querySelector('[data-action=save-style]').click();
    await tick();
    document.getElementById('t-notes').click();
    await tick();
    let canon = document.querySelector('[data-action=make-canon]');
    canon.focus();
    webNotes[0].a = 'updated while focused';
    await refresh(false);
    await tick();
    check(document.querySelector('[data-action=make-canon]') === canon, 'refresh preserves focused note action');
    canon.blur();
    await tick();
    check(document.getElementById('notes-items').textContent.includes('updated while focused'),
      'deferred notes update applies on blur');
    canon = document.querySelector('[data-action=make-canon]');
    canon.focus();
    canon.click();
    await tick();
    check(document.getElementById('notes-items').textContent.includes('✓ Canon') &&
      !document.querySelector('[data-action=make-canon]'), 'focused Make Canon updates after success');
    document.getElementById('t-led').click();
    check(document.getElementById('ledger-items').textContent.includes('canon fact'), 'canon refreshes ledger state');
    holdNotes = true;
    document.getElementById('t-notes').click();
    document.getElementById('t-sum').click();
    finishNotes();
    await tick();
    check(!document.querySelector('[data-panel=sum]').hidden &&
      !document.getElementById('notes-items').textContent.includes('stale response'), 'stale notes response ignored');
    holdNotes = false;
    document.getElementById('t-prem').click();
    premise.focus();
    webState.premise = 'remote updated';
    await refresh(false);
    check(premise.value === 'newer draft', 'focused clean field remains untouched');
    premise.blur();
    check(premise.value === 'remote updated', 'deferred update applies on blur');
    document.body.dataset.testResult = 'PASS';
  } catch (error) {
    document.body.dataset.testResult = 'FAIL: ' + error.message;
  }
});
