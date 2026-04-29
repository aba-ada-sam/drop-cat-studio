/**
 * Drop Cat Go Studio — Wildcard Manager panel.
 * AI-powered wildcard file curation: prune, expand, merge, audit.
 */
import { api } from './api.js?v=20260414';
import { createSlider, createSelect, el } from './components.js?v=20260429b';
import { toast } from './shell/toast.js?v=20260429d';

export function init(panel) {
  panel.innerHTML = '';
  const layout = el('div', { style: 'max-width:800px; margin:0 auto' });
  panel.appendChild(layout);

  // ── File List ─────────────────────────────────────────────────────────
  const listCard = el('div', { class: 'card', style: 'margin-bottom:16px' });
  layout.appendChild(listCard);
  listCard.appendChild(el('h3', { text: 'Wildcard Files' }));

  const fileListEl = el('div', { class: 'file-list' });
  listCard.appendChild(fileListEl);

  const refreshBtn = el('button', { class: 'btn btn-sm', text: 'Refresh', style: 'margin-top:8px' });
  listCard.appendChild(refreshBtn);

  let wildcardFiles = [];
  let selectedFile = null;

  refreshBtn.addEventListener('click', loadFiles);

  async function loadFiles() {
    try {
      const data = await api('/api/prompts/wildcards');
      wildcardFiles = data.files || [];
      renderFiles();
    } catch (e) { toast(e.message, 'error'); }
  }

  function renderFiles() {
    fileListEl.innerHTML = '';
    if (!wildcardFiles.length) {
      fileListEl.appendChild(el('p', { class: 'text-muted', text: 'No wildcard files found. Configure the wildcards directory in Settings.' }));
      return;
    }
    wildcardFiles.forEach(f => {
      const item = el('div', {
        class: `file-item${selectedFile === f.token ? ' selected' : ''}`,
        style: 'cursor:pointer',
        onclick() { selectedFile = f.token; renderFiles(); },
      }, [
        el('span', { class: 'name', text: f.token }),
        el('span', { class: 'meta', text: `${f.count} entries` }),
      ]);
      fileListEl.appendChild(item);
    });
  }

  // ── Operations ────────────────────────────────────────────────────────
  const opsCard = el('div', { class: 'card' });
  layout.appendChild(opsCard);

  const opTabs = el('div', { class: 'audio-tabs' });
  opsCard.appendChild(opTabs);

  const opContents = el('div');
  opsCard.appendChild(opContents);

  const ops = ['Prune', 'Expand', 'Audit'];
  let activeOp = 0;

  // ── Prune panel ───────────────────────────────────────────────────────
  const prunePanel = el('div', { class: 'audio-content active' });
  const pruneLevel = createSlider(prunePanel, { label: 'Aggressiveness', min: 1, max: 5, step: 1, value: 3 });
  const pruneBtn = el('button', { class: 'btn btn-primary', text: 'Prune Selected File' });
  prunePanel.appendChild(pruneBtn);
  const pruneResult = el('div', { style: 'margin-top:12px; display:none' });
  prunePanel.appendChild(pruneResult);

  pruneBtn.addEventListener('click', async () => {
    if (!selectedFile) { toast('Select a wildcard file first', 'error'); return; }
    pruneBtn.disabled = true;
    try {
      // Find file path from token
      const f = wildcardFiles.find(x => x.token === selectedFile);
      const data = await api('/api/prompts/prune', {
        method: 'POST',
        body: JSON.stringify({ path: f?.path || selectedFile, level: pruneLevel.value }),
      });
      pruneResult.style.display = '';
      pruneResult.innerHTML = `
        <p style="color:var(--green)">Kept: ${data.kept?.length || 0} entries</p>
        <p style="color:var(--red)">Removed: ${data.removed?.length || 0} entries</p>
        <pre style="font-size:.75rem; color:var(--text-3); max-height:200px; overflow:auto">${data.notes || ''}</pre>
      `;
    } catch (e) { toast(e.message, 'error'); }
    pruneBtn.disabled = false;
  });

  // ── Expand panel ──────────────────────────────────────────────────────
  const expandPanel = el('div', { class: 'audio-content' });
  const expandCount = createSlider(expandPanel, { label: 'New Entries', min: 5, max: 50, step: 5, value: 20 });
  const expandBtn = el('button', { class: 'btn btn-primary', text: 'Expand Selected File' });
  expandPanel.appendChild(expandBtn);
  const expandResult = el('div', { style: 'margin-top:12px; display:none' });
  expandPanel.appendChild(expandResult);

  expandBtn.addEventListener('click', async () => {
    if (!selectedFile) { toast('Select a wildcard file first', 'error'); return; }
    expandBtn.disabled = true;
    try {
      const f = wildcardFiles.find(x => x.token === selectedFile);
      const data = await api('/api/prompts/expand', {
        method: 'POST',
        body: JSON.stringify({ path: f?.path || selectedFile, count: expandCount.value }),
      });
      expandResult.style.display = '';
      expandResult.innerHTML = `
        <p style="color:var(--green)">${data.count} new entries generated:</p>
        <pre style="font-size:.75rem; color:var(--text-2); max-height:200px; overflow:auto">${(data.new_entries || []).join('\n')}</pre>
      `;
    } catch (e) { toast(e.message, 'error'); }
    expandBtn.disabled = false;
  });

  // ── Audit panel ───────────────────────────────────────────────────────
  const auditPanel = el('div', { class: 'audio-content' });
  const auditBtn = el('button', { class: 'btn btn-primary', text: 'Audit Entire Library' });
  auditPanel.appendChild(auditBtn);
  const auditResult = el('div', { style: 'margin-top:12px; display:none' });
  auditPanel.appendChild(auditResult);

  auditBtn.addEventListener('click', async () => {
    auditBtn.disabled = true;
    auditBtn.textContent = 'Auditing...';
    try {
      const data = await api('/api/prompts/audit', { method: 'POST', body: '{}' });
      auditResult.style.display = '';
      auditResult.innerHTML = `<pre style="font-size:.8rem; color:var(--text-2); white-space:pre-wrap; max-height:400px; overflow:auto">${data.report || 'No report generated'}</pre>`;
    } catch (e) { toast(e.message, 'error'); }
    auditBtn.disabled = false;
    auditBtn.textContent = 'Audit Entire Library';
  });

  // Wire tabs
  const panels = [prunePanel, expandPanel, auditPanel];
  ops.forEach((name, i) => {
    const tab = el('button', { class: `audio-tab${i === 0 ? ' active' : ''}`, text: name });
    tab.addEventListener('click', () => {
      activeOp = i;
      opTabs.querySelectorAll('.audio-tab').forEach((t, j) => t.classList.toggle('active', j === i));
      panels.forEach((p, j) => p.classList.toggle('active', j === i));
    });
    opTabs.appendChild(tab);
    opContents.appendChild(panels[i]);
  });

  // Load files on init
  loadFiles();
}
