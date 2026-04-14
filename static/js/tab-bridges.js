/**
 * Drop Cat Go Studio — Video Bridges tab.
 * AI-powered transitions between clips. Accepts text, images, videos, or any mix.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js';
import { toast, createDropZone, createProgressCard, createVideoPlayer, createSlider, createSelect, el, formatDuration } from './components.js';
import { handoff } from './handoff.js';

let items = [];   // {path, name, kind, duration, analysis}
let activeMode = 'cinematic';

const TRANSITION_MODES = [
  { id: 'cinematic',   icon: '🎬', label: 'Cinematic',   sub: 'Camera moves, atmosphere' },
  { id: 'continuity',  icon: '🔄', label: 'Continuity',  sub: 'Invisible cut, matched' },
  { id: 'kinetic',     icon: '⚡', label: 'Kinetic',     sub: 'High energy, velocity' },
  { id: 'surreal',     icon: '🌀', label: 'Surreal',     sub: 'Dreamlike, impossible' },
  { id: 'meld',        icon: '🌊', label: 'Meld',        sub: 'Textures liquefy' },
  { id: 'morph',       icon: '🔮', label: 'Morph',       sub: 'Shapes transform' },
  { id: 'shape_match', icon: '🔷', label: 'Shape Match', sub: 'Geometry aligned' },
  { id: 'fade',        icon: '🌅', label: 'Fade',        sub: 'Clean dissolve' },
];

function outputPathToUrl(p) {
  if (!p || p.startsWith('/') || p.startsWith('http')) return p || '';
  const norm = p.replace(/\\/g, '/');
  const idx = norm.toLowerCase().indexOf('/output/');
  return idx !== -1 ? norm.substring(idx) : `/output/${norm.split('/').pop()}`;
}

export function init(panel) {
  panel.innerHTML = '';
  const layout = el('div', { class: 'wide-layout' });
  panel.appendChild(layout);

  const sidebar   = el('div', { class: 'sidebar' });
  const mainArea  = el('div', { class: 'main-area' });
  const infoPanel = el('div', { class: 'info-panel' });
  layout.appendChild(sidebar);
  layout.appendChild(mainArea);
  layout.appendChild(infoPanel);

  // ── Info panel (ultrawide) ────────────────────────────────────────────
  infoPanel.appendChild(el('div', { class: 'card' }, [
    el('h3', { text: 'Session Files' }),
    el('p', { class: 'text-muted', style: 'font-size:.85rem', text: 'Click to add to your clip sequence.' }),
    el('div', { id: 'bridges-session-list', class: 'file-list', style: 'max-height:300px' }),
  ]));
  infoPanel.appendChild(el('div', { class: 'card' }, [
    el('h3', { text: 'Tips' }),
    el('div', { style: 'font-size:.82rem; color:var(--text-2); line-height:1.8' }, [
      el('div', { text: '• Mix videos, images, and text freely' }),
      el('div', { text: '• Order matters — drag clips to reorder' }),
      el('div', { text: '• Cinematic works great for most content' }),
      el('div', { text: '• Surreal + high creativity = wild results' }),
      el('div', { text: '• Text-only clips become T2V segments' }),
    ]),
  ]));

  // ── Clip sequence ─────────────────────────────────────────────────────
  const clipsCard = el('div', { class: 'card bridges-clips-card' });
  sidebar.appendChild(clipsCard);

  clipsCard.appendChild(el('h3', { text: '🎞  Clip Sequence' }));
  clipsCard.appendChild(el('p', { class: 'text-muted', style: 'font-size:.8rem; margin-bottom:10px',
    text: 'Drop videos, images, or type a scene description. Need 2+ clips.' }));

  // Text clip input (add a scene from description)
  const textClipRow = el('div', { class: 'text-clip-row' });
  const textClipInput = el('input', { type: 'text', placeholder: '📝  Describe a scene… (text-to-video clip)', class: 'text-clip-input' });
  const textClipBtn   = el('button', { class: 'btn btn-sm', text: '+ Add' });
  textClipRow.appendChild(textClipInput);
  textClipRow.appendChild(textClipBtn);
  clipsCard.appendChild(textClipRow);

  textClipBtn.addEventListener('click', () => {
    const txt = textClipInput.value.trim();
    if (!txt) return;
    items.push({ path: null, name: txt.slice(0, 40), kind: 'text', prompt: txt, analysis: null });
    textClipInput.value = '';
    renderItems();
    toast('Text clip added', 'success');
  });
  textClipInput.addEventListener('keydown', e => { if (e.key === 'Enter') textClipBtn.click(); });

  // File drop zone
  createDropZone(clipsCard, {
    accept: 'video/*,image/*',
    label: 'Drop videos or images here',
    async onFiles(files) {
      try {
        const data = await apiUpload('/api/bridges/upload', files);
        for (const f of data.files || []) items.push({ ...f, analysis: null });
        renderItems();
        toast(`Added ${data.files?.length || 0} file(s)`, 'success');
      } catch (e) { toast(e.message, 'error'); }
    },
  });

  // Path paste
  const pathInput = el('input', { type: 'text', placeholder: 'Or paste file paths (comma-separated)…', style: 'margin-top:8px; width:100%' });
  clipsCard.appendChild(pathInput);
  pathInput.addEventListener('keydown', async e => {
    if (e.key !== 'Enter') return;
    const paths = pathInput.value.split(',').map(p => p.trim()).filter(Boolean);
    if (!paths.length) return;
    try {
      const data = await api('/api/bridges/add-paths', { method: 'POST', body: JSON.stringify({ paths }) });
      for (const f of data.files || []) {
        if (!f.error) items.push({ ...f, analysis: null });
        else toast(`Skipped: ${f.name}`, 'info');
      }
      renderItems();
      pathInput.value = '';
      toast(`Added ${data.files?.filter(f => !f.error).length || 0} file(s)`, 'success');
    } catch (e) { toast(e.message, 'error'); }
  });

  // Session picker
  const sessionRow = el('div', { style: 'display:flex; gap:6px; margin-top:8px' });
  const sessionBtn  = el('button', { class: 'btn btn-sm', text: '+ From Session' });
  const analyzeBtn  = el('button', { class: 'btn btn-sm', text: '🔍 Analyze All' });
  sessionRow.appendChild(sessionBtn);
  sessionRow.appendChild(analyzeBtn);
  clipsCard.appendChild(sessionRow);

  const sessionPicker = el('div', { class: 'session-picker', style: 'display:none; margin-top:8px' });
  clipsCard.appendChild(sessionPicker);

  async function refreshSessionPicker() {
    try {
      const data = await api('/api/session/videos');
      sessionPicker.innerHTML = '';
      const vids = data.videos || [];
      if (!vids.length) { sessionPicker.textContent = 'No session videos yet.'; return; }
      for (const v of vids) {
        const row = el('div', { class: 'file-item', style: 'cursor:pointer', onclick() {
          if (!items.find(i => i.path === v.path))
            items.push({ path: v.path, name: v.filename, kind: 'video', analysis: null });
          renderItems();
          sessionPicker.style.display = 'none';
        }}, [
          el('span', { class: 'name', text: v.filename }),
          el('span', { class: 'meta', text: v.source }),
        ]);
        sessionPicker.appendChild(row);
      }
    } catch (e) { toast(e.message, 'error'); }
  }

  sessionBtn.addEventListener('click', async () => {
    const show = sessionPicker.style.display === 'none';
    sessionPicker.style.display = show ? '' : 'none';
    if (show) refreshSessionPicker();
  });

  // Auto-refresh if visible when a job completes elsewhere
  window.addEventListener('session-updated', () => {
    if (sessionPicker.style.display !== 'none') refreshSessionPicker();
  });

  analyzeBtn.addEventListener('click', async () => {
    analyzeBtn.disabled = true;
    for (let i = 0; i < items.length; i++) {
      if (items[i].analysis || items[i].kind === 'text') continue;
      try {
        toast(`Analyzing clip ${i + 1}/${items.length}…`, 'info');
        items[i].analysis = await api('/api/bridges/analyze', {
          method: 'POST', body: JSON.stringify({ path: items[i].path }),
        });
      } catch (e) { toast(`Analysis failed: ${e.message}`, 'error'); }
    }
    renderItems();
    analyzeBtn.disabled = false;
    toast('Done', 'success');
  });

  // Clip list render
  const itemList = el('div', { class: 'bridges-item-list' });
  clipsCard.appendChild(itemList);

  function renderItems() {
    itemList.innerHTML = '';
    if (!items.length) {
      itemList.appendChild(el('p', { class: 'text-muted', style: 'text-align:center; padding:14px; font-size:.8rem',
        text: 'No clips yet — drop files or type a scene above' }));
      return;
    }
    items.forEach((item, i) => {
      const icon = item.kind === 'video' ? '🎬' : item.kind === 'image' ? '📷' : '📝';
      const meta = item.kind === 'text'
        ? 'text-to-video'
        : item.analysis
          ? `${item.analysis.mood || ''} · ${formatDuration(item.duration)}`
          : formatDuration(item.duration) || item.kind;

      const row = el('div', { class: 'bridge-clip-row' }, [
        el('span', { class: 'bridge-clip-icon', text: icon }),
        el('div', { class: 'bridge-clip-info' }, [
          el('span', { class: 'bridge-clip-name', text: `${i + 1}. ${item.name || 'clip'}` }),
          el('span', { class: 'bridge-clip-meta', text: meta }),
        ]),
        el('div', { class: 'bridge-clip-actions' }, [
          i > 0 ? el('button', { class: 'btn-icon-xs', text: '↑', title: 'Move up', onclick() { [items[i-1], items[i]] = [items[i], items[i-1]]; renderItems(); } }) : el('span'),
          i < items.length-1 ? el('button', { class: 'btn-icon-xs', text: '↓', title: 'Move down', onclick() { [items[i], items[i+1]] = [items[i+1], items[i]]; renderItems(); } }) : el('span'),
          el('button', { class: 'btn-icon-xs remove', text: '✕', onclick() { items.splice(i, 1); renderItems(); } }),
        ]),
      ]);
      itemList.appendChild(row);

      if (i < items.length - 1) {
        const bridge = el('div', { class: 'bridge-connector' }, [
          el('span', { class: 'bridge-connector-line' }),
          el('span', { class: 'bridge-connector-label', text: '⚡ BRIDGE' }),
          el('span', { class: 'bridge-connector-line' }),
        ]);
        itemList.appendChild(bridge);
      }
    });
  }

  // ── Transition mode tiles ─────────────────────────────────────────────
  const modeCard = el('div', { class: 'card bridges-mode-card' });
  sidebar.appendChild(modeCard);
  modeCard.appendChild(el('h3', { text: '✦  Transition Style' }));

  const modeGrid = el('div', { class: 'mode-grid' });
  const modeBtns = {};
  for (const m of TRANSITION_MODES) {
    const btn = el('div', { class: `mode-tile${m.id === 'cinematic' ? ' on' : ''}` }, [
      el('span', { class: 'mode-icon', text: m.icon }),
      el('span', { class: 'mode-label', text: m.label }),
      el('span', { class: 'mode-sub', text: m.sub }),
    ]);
    btn.addEventListener('click', () => {
      activeMode = m.id;
      Object.values(modeBtns).forEach(b => b.classList.remove('on'));
      btn.classList.add('on');
    });
    modeBtns[m.id] = btn;
    modeGrid.appendChild(btn);
  }
  modeCard.appendChild(modeGrid);

  // ── Generation settings (compact) ────────────────────────────────────
  const settingsCard = el('div', { class: 'card' });
  sidebar.appendChild(settingsCard);
  settingsCard.appendChild(el('h3', { text: '⚙  Settings' }));

  const settingsGrid = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:10px' });
  settingsCard.appendChild(settingsGrid);
  const model      = createSelect(settingsGrid, { label: 'Model', options: ['LTX-2 Dev19B Distilled', 'Wan2.1-I2V-14B-480P', 'Wan2.1-I2V-14B-720P'], value: 'LTX-2 Dev19B Distilled' });
  const resolution = createSelect(settingsGrid, { label: 'Resolution', options: ['480p', '580p', '720p'], value: '480p' });
  const duration   = createSlider(settingsCard, { label: 'Bridge Length', min: 3, max: 20, step: 0.5, value: 10, unit: 's' });
  const creativity = createSlider(settingsCard, { label: 'Creativity', min: 1, max: 10, step: 1, value: 9 });
  const steps      = createSlider(settingsCard, { label: 'Steps', min: 4, max: 50, step: 1, value: 20 });

  // ── Generate button ───────────────────────────────────────────────────
  const genBtn = el('button', {
    class: 'btn btn-primary',
    text: '★  Generate Bridges',
    style: 'font-size:1.15rem; padding:14px 32px; margin-top:14px; width:100%',
  });
  sidebar.appendChild(genBtn);

  // ── Main area ─────────────────────────────────────────────────────────
  const progress = createProgressCard(mainArea);
  const player   = createVideoPlayer(mainArea);
  let lastOutputPath = null;

  // "Send to Post Processing" -- shown after generation completes
  const sendCard = el('div', { class: 'card', style: 'display:none; text-align:center; padding:14px 20px' }, [
  ]);
  mainArea.appendChild(sendCard);

  const placeholder = el('div', { class: 'card', style: 'text-align:center; padding:60px 20px; color:var(--text-3)' }, [
    el('div', { style: 'font-size:5rem; margin-bottom:16px', text: '🎞️' }),
    el('p', { style: 'font-size:1.1rem', text: 'Build your sequence on the left' }),
    el('p', { style: 'font-size:.88rem; margin-top:8px', text: 'Mix text, images and videos — AI stitches them together with cinematic transitions' }),
  ]);
  mainArea.appendChild(placeholder);

  genBtn.addEventListener('click', async () => {
    if (items.length < 2) { toast('Need at least 2 clips', 'error'); return; }
    genBtn.disabled = true;
    placeholder.style.display = 'none';
    progress.show();
    player.hide();

    try {
      const data = await api('/api/bridges/generate', {
        method: 'POST',
        body: JSON.stringify({
          items: items.map(it => ({ path: it.path, kind: it.kind, prompt: it.prompt, analysis: it.analysis })),
          settings: {
            model: model.value,
            resolution: resolution.value,
            transition_mode: activeMode,
            prompt_mode: 'ai_informed',
            duration: duration.value,
            steps: steps.value,
            guidance: 10,
            creativity: creativity.value,
          },
        }),
      });

      progress.onCancel(async () => { await stopJob(data.job_id); genBtn.disabled = false; });
      pollJob(data.job_id,
        job => progress.update(job.progress, job.message),
        job => {
          progress.hide();
          genBtn.disabled = false;
          if (job.output) {
            lastOutputPath = job.output;
            player.show(outputPathToUrl(job.output));
            sendCard.style.display = '';
            toast('Bridges generated!', 'success');
          }
        },
        err => { progress.hide(); genBtn.disabled = false; toast(err, 'error'); },
      );
    } catch (e) { progress.hide(); genBtn.disabled = false; toast(e.message, 'error'); }
  });

  player.onStartOver(() => { player.hide(); sendCard.style.display = 'none'; lastOutputPath = null; items = []; renderItems(); });
}

export function receiveHandoff(_data) {
  // Reserved for cross-tab handoff (e.g. session file dropped onto this tab)
}
