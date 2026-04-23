/**
 * Drop Cat Go Studio — Add Transitions (Video Bridges)
 * Pick session videos → arrange sequence → AI generates bridge clips between each pair.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js?v=20260414';
import { createProgressCard, createVideoPlayer, createSlider, el, formatDuration } from './components.js?v=20260414';
import { toast } from './shell/toast.js?v=20260421c';
import { handoff } from './handoff.js?v=20260422a';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260419o';

let _items      = [];   // { path, name, kind, duration, analysis, prompt }
let _activeMode = 'cinematic';

const TRANSITION_MODES = [
  { id: 'cinematic',   label: 'Cinematic',   sub: 'Camera moves & atmosphere' },
  { id: 'continuity',  label: 'Continuity',  sub: 'Invisible matched cut' },
  { id: 'kinetic',     label: 'Kinetic',     sub: 'High energy, velocity' },
  { id: 'surreal',     label: 'Surreal',     sub: 'Dreamlike, impossible' },
  { id: 'meld',        label: 'Meld',        sub: 'Textures liquefy' },
  { id: 'morph',       label: 'Morph',       sub: 'Shapes transform' },
  { id: 'shape_match', label: 'Shape Match', sub: 'Geometry aligned' },
  { id: 'fade',        label: 'Fade',        sub: 'Clean dissolve' },
];

function outputPathToUrl(p) {
  if (!p || p.startsWith('/') || p.startsWith('http')) return p || '';
  const norm = p.replace(/\\/g, '/');
  const idx  = norm.toLowerCase().indexOf('/output/');
  return idx !== -1 ? norm.substring(idx) : `/output/${norm.split('/').pop()}`;
}

function _addItem(item) {
  if (item.path && _items.find(i => i.path === item.path)) return false;
  _items.push({ ...item, analysis: null });
  return true;
}

export function receiveHandoff(data) {
  if (data.type === 'video' && data.path) {
    const name = data.path.split(/[\\/]/).pop();
    if (_addItem({ path: data.path, name, kind: 'video' })) {
      toast(`Added "${name}" to sequence`, 'success');
      _renderItems?.();
    }
  }
}

// renderItems is set during init so receiveHandoff can call it even before tab visit
let _renderItems = null;

export function init(panel) {
  panel.innerHTML = '';

  const root = el('div', { style: 'display:flex; flex-direction:column; gap:14px; padding:16px; max-width:900px; margin:0 auto;' });
  panel.appendChild(root);

  // ── Clip picker ──────────────────────────────────────────────────────────
  const pickerCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(pickerCard);

  pickerCard.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:4px;', text: 'Add Clips' }));
  pickerCard.appendChild(el('div', { style: 'font-size:.75rem; color:var(--text-3); margin-bottom:10px;', text: 'Add 2 or more video clips. A bridge transition will be generated between each adjacent pair.' }));

  const fileInput = el('input', { type: 'file', accept: 'video/*,image/*', multiple: 'true', style: 'display:none' });
  pickerCard.appendChild(fileInput);

  async function _uploadFiles(files) {
    try {
      const data = await apiUpload('/api/bridges/upload', files);
      let added = 0;
      for (const f of data.files || []) { if (_addItem(f)) added++; }
      if (added) { _renderItems(); toast(`Added ${added} clip${added !== 1 ? 's' : ''}`, 'success'); }
    } catch (e) { toast(e.message, 'error'); }
  }

  const dropZone = el('div', { style: 'border:2px dashed var(--border-2); border-radius:8px; padding:28px 16px; text-align:center; cursor:pointer; transition:border-color .15s;' }, [
    el('div', { style: 'font-size:.82rem; color:var(--text-3); margin-bottom:10px;', text: 'Drop video clips here' }),
    el('button', { class: 'btn btn-sm', text: 'Open files...' }),
  ]);
  pickerCard.appendChild(dropZone);

  dropZone.addEventListener('click', () => fileInput.click());
  dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.style.borderColor = 'var(--accent)'; });
  dropZone.addEventListener('dragleave', () => { dropZone.style.borderColor = 'var(--border-2)'; });
  dropZone.addEventListener('drop', async e => {
    e.preventDefault();
    dropZone.style.borderColor = 'var(--border-2)';
    const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('video/') || f.type.startsWith('image/'));
    if (files.length) await _uploadFiles(files);
  });
  fileInput.addEventListener('change', async () => {
    if (!fileInput.files?.length) return;
    await _uploadFiles(Array.from(fileInput.files));
    fileInput.value = '';
  });

  // ── Sequence ──────────────────────────────────────────────────────────────
  const seqCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(seqCard);

  const seqHeader = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-bottom:10px;' });
  seqCard.appendChild(seqHeader);
  const seqTitle = el('span', { style: 'font-size:.85rem; font-weight:600; flex:1;', text: 'Sequence' });
  seqHeader.appendChild(seqTitle);

  const analyzeBtn = el('button', { class: 'btn btn-sm', text: 'Analyze clips' });
  seqHeader.appendChild(analyzeBtn);

  analyzeBtn.addEventListener('click', async () => {
    const toAnalyze = _items.filter(it => !it.analysis && it.kind !== 'text');
    if (!toAnalyze.length) { toast('Nothing to analyze', 'info'); return; }
    analyzeBtn.disabled = true;
    for (let i = 0; i < _items.length; i++) {
      if (_items[i].analysis || _items[i].kind === 'text') continue;
      try {
        toast(`Analyzing clip ${i + 1}/${_items.length}…`, 'info');
        _items[i].analysis = await api('/api/bridges/analyze', {
          method: 'POST', body: JSON.stringify({ path: _items[i].path }),
        });
        _renderItems();
      } catch (e) { toast(`Analysis failed: ${e.message}`, 'error'); }
    }
    analyzeBtn.disabled = false;
    toast('Analysis done', 'success');
  });

  const itemList = el('div', { style: 'display:flex; flex-direction:column; gap:0;' });
  seqCard.appendChild(itemList);

  // Text scene toggle
  const textToggleRow = el('div', { style: 'margin-top:10px; border-top:1px solid var(--border-2); padding-top:10px;' });
  seqCard.appendChild(textToggleRow);
  const textToggle = el('button', { class: 'btn btn-sm', text: '+ Add text scene (text → video clip)' });
  textToggleRow.appendChild(textToggle);
  const textInput = el('div', { style: 'display:none; margin-top:8px; display:none;' });
  textToggleRow.appendChild(textInput);
  const textTA = el('input', { type: 'text', placeholder: 'Describe a scene…', style: 'flex:1;' });
  const textAddBtn = el('button', { class: 'btn btn-sm btn-primary', text: 'Add' });
  textInput.appendChild(el('div', { style: 'display:flex; gap:6px;' }, [textTA, textAddBtn]));
  let textOpen = false;
  textToggle.addEventListener('click', () => {
    textOpen = !textOpen;
    textInput.style.display = textOpen ? '' : 'none';
    if (textOpen) textTA.focus();
  });
  const addText = () => {
    const txt = textTA.value.trim();
    if (!txt) return;
    _items.push({ path: null, name: txt.slice(0, 50), kind: 'text', prompt: txt, analysis: null });
    textTA.value = '';
    _renderItems();
    toast('Text scene added', 'success');
  };
  textAddBtn.addEventListener('click', addText);
  textTA.addEventListener('keydown', e => { if (e.key === 'Enter') addText(); });

  _renderItems = function renderItems() {
    itemList.innerHTML = '';

    const count = _items.length;
    seqTitle.textContent = count
      ? `Sequence — ${count} clip${count !== 1 ? 's' : ''}, ${Math.max(0, count - 1)} bridge${count - 1 !== 1 ? 's' : ''}`
      : 'Sequence';

    if (!count) {
      itemList.appendChild(el('div', {
        style: 'text-align:center; padding:28px 0; color:var(--text-3); font-size:.82rem;',
        text: 'Add videos from above — you need at least 2 clips to generate bridges.',
      }));
      return;
    }

    _items.forEach((item, i) => {
      const icon = item.kind === 'text' ? 'T' : item.kind === 'image' ? 'IMG' : 'VID';
      const meta = item.kind === 'text'
        ? 'text → video'
        : item.analysis
          ? `${item.analysis.mood || ''} · ${formatDuration(item.duration)}`
          : formatDuration(item.duration) || item.kind;

      const row = el('div', { style: 'display:flex; align-items:center; gap:10px; padding:8px 10px; border-radius:6px; background:var(--bg-raised); border:1px solid var(--border-2);' });

      row.appendChild(el('span', { style: 'font-size:.65rem; font-weight:700; color:var(--text-3); flex-shrink:0; width:28px; text-align:center;', text: icon }));
      row.appendChild(el('div', { style: 'flex:1; min-width:0;' }, [
        el('div', { style: 'font-size:.8rem; font-weight:600; color:var(--text-2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;', text: `${i + 1}. ${item.name}` }),
        el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-top:1px;', text: meta }),
      ]));

      const actions = el('div', { style: 'display:flex; gap:4px; flex-shrink:0;' });
      if (i > 0) {
        const upBtn = el('button', { class: 'btn-icon-xs', text: '↑', title: 'Move up',
          onclick() { [_items[i-1], _items[i]] = [_items[i], _items[i-1]]; _renderItems(); } });
        actions.appendChild(upBtn);
      }
      if (i < _items.length - 1) {
        const downBtn = el('button', { class: 'btn-icon-xs', text: '↓', title: 'Move down',
          onclick() { [_items[i], _items[i+1]] = [_items[i+1], _items[i]]; _renderItems(); } });
        actions.appendChild(downBtn);
      }
      const removeBtn = el('button', { class: 'btn-icon-xs remove', text: '✕', title: 'Remove',
        onclick() { _items.splice(i, 1); _renderItems(); } });
      actions.appendChild(removeBtn);
      row.appendChild(actions);
      itemList.appendChild(row);

      if (i < _items.length - 1) {
        const connector = el('div', { style: 'display:flex; align-items:center; gap:8px; padding:4px 12px;' }, [
          el('div', { style: 'flex:1; height:1px; background:var(--border-2);' }),
          el('span', { style: 'font-size:.68rem; font-weight:700; letter-spacing:.08em; color:var(--accent); opacity:.7; white-space:nowrap;', text: 'BRIDGE' }),
          el('div', { style: 'flex:1; height:1px; background:var(--border-2);' }),
        ]);
        itemList.appendChild(connector);
      }
    });
  };

  _renderItems();

  // ── Transition style ──────────────────────────────────────────────────────
  const styleCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(styleCard);
  styleCard.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:10px;', text: 'Transition Style' }));

  const modeGrid = el('div', { style: 'display:grid; grid-template-columns:repeat(4,1fr); gap:6px;' });
  styleCard.appendChild(modeGrid);

  const modeBtns = {};
  for (const m of TRANSITION_MODES) {
    const tile = el('div', {
      style: `display:flex; flex-direction:column; align-items:center; gap:3px; padding:10px 6px; border-radius:8px; border:1px solid var(--border-2); cursor:pointer; text-align:center; transition:border-color .15s, background .15s; background:var(--bg-raised);`,
    });
    tile.appendChild(el('span', { style: 'font-size:.75rem; font-weight:600; color:var(--text-2);', text: m.label }));
    tile.appendChild(el('span', { style: 'font-size:.65rem; color:var(--text-3); line-height:1.3;', text: m.sub }));

    if (m.id === _activeMode) {
      tile.style.borderColor = 'var(--accent)';
      tile.style.background = 'color-mix(in srgb, var(--accent) 15%, var(--bg-raised))';
    }

    tile.addEventListener('click', () => {
      _activeMode = m.id;
      for (const [id, t] of Object.entries(modeBtns)) {
        const on = id === m.id;
        t.style.borderColor = on ? 'var(--accent)' : 'var(--border-2)';
        t.style.background  = on ? 'color-mix(in srgb, var(--accent) 15%, var(--bg-raised))' : 'var(--bg-raised)';
      }
    });

    modeBtns[m.id] = tile;
    modeGrid.appendChild(tile);
  }

  // ── Settings ──────────────────────────────────────────────────────────────
  const settingsCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(settingsCard);
  settingsCard.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:12px;', text: 'Settings' }));

  const settingsGrid = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:10px;' });
  settingsCard.appendChild(settingsGrid);
  const durSlider        = createSlider(settingsGrid, { label: 'Bridge length (s)', min: 3, max: 20, step: 0.5, value: 10 });
  const creativitySlider = createSlider(settingsGrid, { label: 'Creativity',         min: 1, max: 10, step: 1,   value: 9  });
  const stepsSlider      = createSlider(settingsCard, { label: 'Steps',              min: 4, max: 50, step: 1,   value: 20 });

  // ── Generate ──────────────────────────────────────────────────────────────
  const genBtn = el('button', {
    class: 'btn btn-primary btn-generate',
    text: 'Generate Bridges',
    style: 'width:100%; font-size:1.1rem; padding:14px; font-weight:700;',
  });
  root.appendChild(genBtn);

  const progWrap = el('div');
  root.appendChild(progWrap);
  const prog = createProgressCard(progWrap);

  const vidWrap = el('div');
  root.appendChild(vidWrap);
  const player = createVideoPlayer(vidWrap);
  let _lastOutput = null;

  const sendCard = el('div', { class: 'card', style: 'display:none; padding:14px; text-align:center;' });
  root.appendChild(sendCard);
  sendCard.appendChild(el('div', { style: 'font-size:.82rem; color:var(--text-3); margin-bottom:10px;', text: 'Next step:' }));
  sendCard.appendChild(el('button', {
    class: 'btn btn-primary', text: '→ Audio & Export',
    style: 'padding:9px 24px; font-size:.95rem;',
    onclick() {
      if (!_lastOutput) return;
      handoff('video-tools', { type: 'video', path: _lastOutput });
      document.querySelector('[data-tab="video-tools"]')?.click();
    },
  }));

  genBtn.addEventListener('click', async () => {
    if (_items.length < 2) {
      dropZone.style.outline = '2px solid var(--red)';
      dropZone.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      setTimeout(() => { dropZone.style.outline = ''; }, 2000);
      toast('Add at least 2 clips above first', 'error');
      return;
    }
    genBtn.disabled = true;
    prog.show();
    prog.update(0, 'Submitting…');
    player.hide();
    sendCard.style.display = 'none';

    try {
      const data = await api('/api/bridges/generate', {
        method: 'POST',
        body: JSON.stringify({
          items: _items.map(it => ({ path: it.path, kind: it.kind, prompt: it.prompt, analysis: it.analysis })),
          settings: {
            model:           'LTX-2 Dev19B Distilled',
            resolution:      '480p',
            transition_mode: _activeMode,
            prompt_mode:     'ai_informed',
            duration:        durSlider.value,
            steps:           stepsSlider.value,
            guidance:        10,
            creativity:      creativitySlider.value,
          },
        }),
      });

      prog.onCancel(async () => { await stopJob(data.job_id).catch(() => {}); genBtn.disabled = false; });
      pollJob(data.job_id,
        j => prog.update(j.progress || 0, j.message || 'Working…'),
        j => {
          prog.hide();
          genBtn.disabled = false;
          if (j.output) {
            _lastOutput = j.output;
            player.show(outputPathToUrl(j.output), j.output);
            pushToGallery('bridges', j.output, `${_activeMode} transition (${_items.length} clips)`, null, {
              transition_mode: _activeMode,
              creativity:      Number(creativitySlider.value),
              bridge_length:   Number(durSlider.value),
              steps:           Number(stepsSlider.value),
              clip_count:      _items.length,
            });
            sendCard.style.display = '';
            toast('Bridges generated!', 'success');
          }
        },
        err => { prog.hide(); genBtn.disabled = false; toast(err, 'error'); },
      );
    } catch (e) { prog.hide(); genBtn.disabled = false; toast(e.message, 'error'); }
  });

  player.onStartOver(() => { player.hide(); sendCard.style.display = 'none'; _lastOutput = null; });

  // ── Palette AI intent ─────────────────────────────────────────────────────
  import('./shell/ai-intent.js?v=20260419e').then(({ registerTabAI }) => {
    registerTabAI('bridges', {
      getContext: () => ({
        transition_mode: _activeMode,
        creativity:      Number(creativitySlider.value) || 0,
        bridge_length:   Number(durSlider.value)        || 0,
        steps:           Number(stepsSlider.value)      || 0,
      }),
      applySettings: (s) => {
        if (typeof s.transition_mode === 'string' && modeBtns[s.transition_mode]) modeBtns[s.transition_mode].click();
        if (typeof s.creativity    === 'number') creativitySlider.value = Math.max(1,  Math.min(10, s.creativity));
        if (typeof s.bridge_length === 'number') durSlider.value        = Math.max(3,  Math.min(20, s.bridge_length));
        if (typeof s.steps         === 'number') stepsSlider.value      = Math.max(4,  Math.min(50, s.steps));
      },
    });
  }).catch(() => {});
}
