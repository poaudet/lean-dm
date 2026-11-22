const configMeta = {
  path: 'settings.json', values: {tone: 'warm'}, secret_set: {},
  fields: [{key: 'tone', label: 'Tone', type: 'text', group: "Writer's", default: 'warm'}]
};
window.fetch = url => Promise.resolve({
  ok: true, json: async () => url === '/api/campaigns' ? {campaigns: ['one'], current: 'one'} : configMeta
});
window.addEventListener('load', async () => {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  try {
    await new Promise(resolve => setTimeout(resolve, 10));
    const field = document.getElementById('f_tone');
    field.value = 'changed';
    field.dispatchEvent(new Event('input', {bubbles: true}));
    check(document.getElementById('dirty-tag').style.display === 'inline', 'delegated input marks dirty');
    document.querySelector('[data-reset-field=tone]').click();
    check(field.value === 'warm', 'delegated reset works');
    document.querySelector('[data-group-filter="all"]').click();
    check(document.querySelector('#form fieldset').style.display === 'block', 'delegated group selection works');
    document.body.dataset.testResult = 'PASS';
  } catch (error) {
    document.body.dataset.testResult = 'FAIL: ' + error.message;
  }
});
