/**
 * Drop Cat Go Studio -- SD Studio (Canvas-First, Collapsible Sections)
 *
 * Layout:
 * 1. Canvas (full width, dominates viewport)
 * 2. Step 1 front door (prompt shape + source + enhance/paste)
 * 3. Collapsible Sections (Prompt, Regional, Refinements, Advanced, Gallery)
 * 4. Sections state persists to localStorage
 */
import { api, apiUpload } from './api.js?v=20260414';
import { toast, createDropZone, createSlider, el } from './components.js?v=20260414';
import { handoff } from './handoff.js?v=20260415';
import { RegionEditor } from './components/region-editor.js?v=20260416d';
import { pushFromTab as pushToGallery, setPreview } from './shell/gallery.js?v=20260419o';

// ── State ───────────────────────────────────────────────────────────────────
let sessionId        = 'default';
let sessionActive    = false;
let forgeStatus      = null;
let generatedImages  = [];
let currentIdx       = -1;
let lastFocusedTA    = null;
let refImagePath     = null;
let fcRows = 1, fcCols = 3;
let fcDirection  = 'Horizontal';
let fcBackground = 'First Line';
let fcEnabled = false;
let fcQuickToggle = null;
let _pendingHandoff = null;
let _applyHandoff   = null;
let _pendingConcept = null;   // concept text waiting for ideaTA to exist
let _applyIdea      = null;   // wired by buildStep1Panel

export function receiveHandoff(data) {
  if (data.type === 'image' && data.path) {
    if (_applyHandoff) _applyHandoff(data.path);
    else _pendingHandoff = data.path;
  }
  // Concept handoff from Studio Home: pre-fill the idea textarea
  if (data.type === 'concept' && data.text) {
    if (_applyIdea) _applyIdea(data.text);
    else _pendingConcept = data.text;
  }
}

// ── Utils ────────────────────────────────────────────────────────────────────
function getSectionState() {
  try { return JSON.parse(localStorage.getItem('sd_sections') || '{}'); }
  catch { return {}; }
}

function setSectionState(key, open) {
  const state = getSectionState();
  state[key] = open;
  localStorage.setItem('sd_sections', JSON.stringify(state));
}

function createSection(title, icon, key, defaultOpen = true) {
  const state = getSectionState();
  const isOpen = state[key] !== undefined ? state[key] : defaultOpen;

  const section = el('div', { class: 'collapsible-section', style: 'margin-bottom:14px' });

  const header = el('div', {
    class: 'section-header',
    style: `display:flex; align-items:center; gap:10px; padding:12px 16px; cursor:pointer; user-select:none; background:var(--surface); border-radius:var(--r-md); border:1px solid var(--border);`,
    onclick() { toggleSection(!isOpen); }
  });
  section.appendChild(header);

  const titleEl = el('span', { style: 'flex:1; font-weight:600; font-size:.9rem', text: `${icon} ${title}` });
  header.appendChild(titleEl);

  const chevron = el('span', { style: 'color:var(--text-3); transition:transform .2s', text: '▼' });
  header.appendChild(chevron);

  const body = el('div', {
    class: 'section-body',
    style: `${isOpen ? '' : 'display:none;'} padding:14px 16px; background:var(--surface-2); border-radius:0 0 var(--r-md) var(--r-md); border:1px solid var(--border); border-top:none;`
  });
  section.appendChild(body);

  let sectionOpen = isOpen;
  function toggleSection(open) {
    sectionOpen = open;
    body.style.display = open ? '' : 'none';
    chevron.style.transform = open ? 'rotate(180deg)' : '';
    setSectionState(key, open);
  }

  return { section, body, header, chevron, toggle: toggleSection };
}

// ── Init ─────────────────────────────────────────────────────────────────────
export function init(panel) {
  panel.innerHTML = '';

  const root = el('div', { style: 'display:flex; flex-direction:column; gap:0; height:100%;' });
  panel.appendChild(root);

  // ════════════════════════════════════════════════════════════════════════════
  // COLLAPSIBLE SECTIONS
  // ════════════════════════════════════════════════════════════════════════════

  const sectionsContainer = el('div', {
    style: 'flex:1; overflow-y:auto; padding:14px 16px; background:var(--bg);'
  });
  root.appendChild(sectionsContainer);

  // Status bar (Forge connection + checkpoint picker)
  const statusBar = el('div', {
    class: 'card',
    style: 'padding:8px 14px; display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:10px;'
  });
  sectionsContainer.appendChild(statusBar);

  const forgeDot = el('span', { class: 'dot' });
  const forgeMsg = el('span', { style: 'font-size:.85rem; flex-shrink:0', text: 'Checking Forge...' });
  statusBar.appendChild(forgeDot);
  statusBar.appendChild(forgeMsg);
  statusBar.appendChild(el('div', { style: 'width:1px; height:18px; background:var(--border-2); flex-shrink:0' }));
  statusBar.appendChild(el('label', { text: 'Checkpoint', style: 'font-size:.8rem; color:var(--text-3); flex-shrink:0' }));

  const modelSel = el('select', { style: 'flex:1; min-width:180px' });
  modelSel.appendChild(el('option', { text: 'Forge not connected', value: '' }));
  statusBar.appendChild(modelSel);
  modelSel.addEventListener('change', async () => {
    try {
      await api('/api/prompts/forge/set-model', {
        method: 'POST',
        body: JSON.stringify({ model: modelSel.value })
      });
      toast(`Loading: ${modelSel.value}`, 'info');
    }
    catch (e) { toast(e.message, 'error'); }
  });

  // ── Generate Controls (always visible) ──────────────────────────────────

  const genControlsBar = el('div', {
    style: 'display:flex; gap:8px; padding:10px 0 4px; align-items:center; flex-wrap:wrap;'
  });
  sectionsContainer.appendChild(genControlsBar);

  const genBtn = el('button', {
    class: 'btn btn-primary btn-generate',
    disabled: true,
    text: '⚡ Generate',
    style: 'white-space:nowrap; padding:10px 28px; font-weight:700; flex:1; min-width:140px; font-size:1rem;'
  });
  genControlsBar.appendChild(genBtn);

  fcQuickToggle = el('button', {
    class: 'btn btn-sm',
    text: '🎭 Regions',
    style: 'white-space:nowrap; padding:8px 12px; font-size:.8rem',
    title: 'Toggle Regional Prompting',
    onclick() { toggleFC(!fcEnabled); }
  });
  genControlsBar.appendChild(fcQuickToggle);

  const settingsBtn = el('button', {
    class: 'btn btn-sm',
    text: '⚙️',
    style: 'white-space:nowrap; padding:8px 12px; font-size:.9rem',
    title: 'Open Advanced Settings'
  });
  genControlsBar.appendChild(settingsBtn);

  const stopBtn = el('button', {
    class: 'btn btn-sm',
    text: '⏹ Stop',
    style: 'white-space:nowrap; padding:8px 12px; font-size:.8rem',
    async onclick() {
      await api('/api/prompts/forge/interrupt', { method: 'POST' }).catch(() => {});
      toast('Stop requested', 'info');
    }
  });
  genControlsBar.appendChild(stopBtn);

  // ── Prompt Section (Advanced — view/edit after Step 1) ───────────────────
  // Step 1 (front door) writes here. Users can also edit directly.
  const promptSec = createSection('Advanced Prompt (edit raw)', '🖊️', 'prompt', false);
  sectionsContainer.appendChild(promptSec.section);

  const baseTA = el('textarea', { rows: '3', placeholder: 'The full prompt sent to Forge. Step 1 fills this automatically; you can edit it here.', style: 'width:100%; resize:vertical; margin-bottom:8px; font-size:.9rem;' });
  promptSec.body.appendChild(baseTA);

  const suffixIn = el('input', { type: 'text', style: 'width:100%; margin-top:8px;' });
  suffixIn.value = '(depth blur)';  // default; overridden from Step 1 panel on every enhance/use
  promptSec.body.appendChild(el('div', { style: 'margin-top:8px' }, [
    el('label', { text: 'Style Suffix (appended to prompt on Generate)', style: 'display:block; margin-bottom:4px; font-size:.85rem; color:var(--text-3)' }),
    suffixIn
  ]));

  // ── Regional Prompting Section ────────────────────────────────────────────
  const regionalSec = createSection('Regional Prompting', '🎭', 'regional-prompting', false);
  sectionsContainer.appendChild(regionalSec.section);

  regionalSec.body.appendChild(el('div', { style: 'margin-bottom:12px; padding:8px; background:var(--surface-3); border-radius:var(--r-sm); font-size:.8rem; color:var(--text-2)' }, [
    el('strong', { text: 'Divide your canvas into regions. ' }),
    el('span', { text: 'Each region gets its own prompt, combined with a global prompt. Great for detailed scene control.' })
  ]));

  const fcToggleRow = el('div', { style: 'display:flex; gap:8px; align-items:center; margin-bottom:12px;' });
  const fcToggleChk = el('input', { type: 'checkbox', id: 'fc-toggle' });
  fcToggleRow.appendChild(fcToggleChk);
  fcToggleRow.appendChild(el('label', { for: 'fc-toggle', text: 'Enable Forge Couple (regional prompting)', style: 'cursor:pointer; font-weight:600; flex:1' }));
  regionalSec.body.appendChild(fcToggleRow);
  fcToggleChk.addEventListener('change', () => {
    fcEnabled = fcToggleChk.checked;
    fcConfigRow.style.display = fcEnabled ? '' : 'none';
    fcGridWrap.style.display = fcEnabled ? '' : 'none';
    fcBgRow.style.display = fcEnabled ? '' : 'none';
    fcViewToggleRow.style.display = fcEnabled ? 'flex' : 'none';
    if (fcQuickToggle) fcQuickToggle.classList.toggle('btn-primary', fcEnabled);
  });

  // Config row (Rows, Cols, H/V, Background)
  const fcConfigRow = el('div', { style: 'display:none; gap:10px; margin-bottom:10px; flex-wrap:wrap; align-items:center' });
  regionalSec.body.appendChild(fcConfigRow);

  const fcRowsIn = el('input', { type: 'number', value: '1', min: '1', max: '4', style: 'width:60px;' });
  const fcColsIn = el('input', { type: 'number', value: '3', min: '1', max: '4', style: 'width:60px;' });
  fcConfigRow.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:center;' }, [
    el('label', { text: 'Grid:', style: 'font-size:.85rem; color:var(--text-3)' }),
    fcRowsIn,
    el('span', { text: '×' }),
    fcColsIn
  ]));
  fcRowsIn.addEventListener('change', () => { fcRows = parseInt(fcRowsIn.value); renderFCGrid(); });
  fcColsIn.addEventListener('change', () => { fcCols = parseInt(fcColsIn.value); renderFCGrid(); });

  const fcHVSel = el('select', { style: 'font-size:.85rem;' });
  ['Horizontal', 'Vertical'].forEach(opt => fcHVSel.appendChild(el('option', { value: opt, text: opt })));
  fcHVSel.addEventListener('change', () => { fcDirection = fcHVSel.value; });
  fcConfigRow.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:center;' }, [
    el('label', { text: 'Direction:', style: 'font-size:.85rem; color:var(--text-3)' }),
    fcHVSel
  ]));

  const fcBgPosSel = el('select', { style: 'font-size:.85rem;' });
  ['First Line', 'Last Line', 'None'].forEach(opt => fcBgPosSel.appendChild(el('option', { value: opt, text: opt })));
  fcBgPosSel.addEventListener('change', () => { fcBackground = fcBgPosSel.value; });
  fcConfigRow.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:center;' }, [
    el('label', { text: 'Background:', style: 'font-size:.85rem; color:var(--text-3)' }),
    fcBgPosSel
  ]));

  // Region grid (text mode)
  const fcGridWrap = el('div', { class: 'fc-grid-wrap', style: 'display:none; margin-bottom:10px;' });
  regionalSec.body.appendChild(fcGridWrap);
  let fcCells = [];

  function renderFCGrid() {
    const prevValues = fcCells.map(ta => ta?.value || '');
    fcGridWrap.style.gridTemplateColumns = `repeat(${fcCols}, 1fr)`;
    fcGridWrap.innerHTML = '';
    fcCells = [];
    const total = fcRows * fcCols;
    for (let r = 0; r < fcRows; r++) {
      for (let c = 0; c < fcCols; c++) {
        const idx = r * fcCols + c;
        const regionLabel = total === 1 ? 'Region' :
          fcRows === 1 ? (['Left', 'Center', 'Right', 'Far Right'][c] || `Col ${c+1}`) :
          fcCols === 1 ? (['Top', 'Middle', 'Bottom', 'Bottom'][r] || `Row ${r+1}`) :
          `R${r+1} C${c+1}`;
        const ta = el('textarea', { rows: '2', style: 'width:100%; resize:vertical; font-size:.8rem',
          placeholder: `${regionLabel} region...` });
        ta.addEventListener('focus', () => lastFocusedTA = ta);
        if (prevValues[idx]) ta.value = prevValues[idx];
        const cell = el('div', { class: 'fc-cell' }, [
          el('label', { text: regionLabel, style: 'display:block; margin-bottom:4px; font-size:.75rem; color:var(--text-3)' }),
          ta,
        ]);
        fcGridWrap.appendChild(cell);
        fcCells[idx] = ta;
      }
    }
  }

  // Background anchor region influence
  const fcBgRow = el('div', { style: 'display:none; align-items:center; gap:10px; margin-top:8px; flex-wrap:wrap' });
  regionalSec.body.appendChild(fcBgRow);
  fcBgRow.appendChild(el('label', { title: 'How much the anchor region influences the global generation (0=local only, 1=strong global)', text: 'Anchor Region Influence', style: 'font-size:.78rem; color:var(--text-3); white-space:nowrap; cursor:help' }));
  const fcBgLabel  = el('span', { text: '0.5', style: 'font-size:.78rem; color:var(--accent); min-width:26px' });
  const fcBgSlider = el('input', { type: 'range', min: '0.1', max: '1', step: '0.05', value: '0.5', style: 'flex:1; min-width:120px' });
  fcBgSlider.addEventListener('input', () => { fcBgLabel.textContent = fcBgSlider.value; });
  fcBgRow.appendChild(fcBgSlider);
  fcBgRow.appendChild(fcBgLabel);

  // Visual region editor toggle
  const fcViewToggleRow = el('div', { style: 'display:none; gap:6px; margin-top:8px; align-items:center' });
  regionalSec.body.appendChild(fcViewToggleRow);
  const fcVisualBtn = el('button', { class: 'btn btn-sm', text: 'Visual Editor', style: 'font-size:.72rem' });
  const fcTextBtn = el('button', { class: 'btn btn-sm btn-primary', text: 'Text Grid', style: 'font-size:.72rem' });
  fcViewToggleRow.appendChild(el('span', { text: 'Mode:', style: 'font-size:.72rem; color:var(--text-3)' }));
  fcViewToggleRow.appendChild(fcVisualBtn);
  fcViewToggleRow.appendChild(fcTextBtn);

  let _regionEditor = null;
  fcVisualBtn.addEventListener('click', () => {
    fcGridWrap.style.display = 'none';
    fcVisualBtn.classList.add('btn-primary');
    fcTextBtn.classList.remove('btn-primary');
    if (!_regionEditor) {
      _regionEditor = new RegionEditor(el('div'), {
        rows: fcRows, cols: fcCols, direction: fcDirection,
        onChange: (regions) => {
          regions.forEach((r, i) => { if (fcCells[i]) fcCells[i].value = r.prompt; });
        },
      });
    }
  });
  fcTextBtn.addEventListener('click', () => {
    fcGridWrap.style.display = '';
    fcVisualBtn.classList.remove('btn-primary');
    fcTextBtn.classList.add('btn-primary');
  });

  // ── Refinements Section ───────────────────────────────────────────────────
  const refinementsSec = createSection('Refinements', '✨', 'refinements', false);
  sectionsContainer.appendChild(refinementsSec.section);

  const refTabsRow = el('div', { style: 'display:flex; gap:6px; margin-bottom:10px; flex-wrap:wrap;' });
  refinementsSec.body.appendChild(refTabsRow);

  const paneAD   = el('div', { style: 'display:none;' });
  const paneHR   = el('div', { style: 'display:none;' });
  const paneI2I  = el('div', { style: 'display:none;' });

  ['ADetailer', 'HiRes Fix', 'Img2Img'].forEach((name, i) => {
    const panes = [paneAD, paneHR, paneI2I];
    const btn = el('button', {
      class: `btn btn-sm ${i===0?' btn-primary':''}`,
      text: name,
      style: 'font-size:.8rem',
      onclick() {
        refTabsRow.querySelectorAll('.ref-tab').forEach(b => b.classList.remove('btn-primary'));
        btn.classList.add('btn-primary');
        panes.forEach((p, j) => p.style.display = j===i ? '' : 'none');
      }
    });
    btn.classList.add('ref-tab');
    refTabsRow.appendChild(btn);
  });

  refinementsSec.body.appendChild(paneAD);
  refinementsSec.body.appendChild(paneHR);
  refinementsSec.body.appendChild(paneI2I);
  paneAD.style.display = '';

  // HiRes Fix
  const hrEnabled = el('input', { type: 'checkbox', id: 'sd-hr' });
  paneHR.appendChild(el('div', { style: 'display:flex; align-items:center; gap:6px; margin-bottom:8px' }, [
    hrEnabled,
    el('label', { for: 'sd-hr', text: 'Enable HiRes Fix', style: 'cursor:pointer; font-weight:600' })
  ]));
  const hrScale   = createSlider(paneHR, { label: 'Scale',   min: 1, max: 4, step: 0.25, value: 2 });
  const hrSteps   = createSlider(paneHR, { label: 'Steps',   min: 0, max: 40, step: 1, value: 10 });
  const hrDenoise = createSlider(paneHR, { label: 'Denoise', min: 0.1, max: 1, step: 0.05, value: 0.35 });
  const hrUpSel   = el('select', { style: 'width:100%' });
  hrUpSel.appendChild(el('option', { value: 'ESRGAN_4x', text: 'ESRGAN_4x' }));
  paneHR.appendChild(el('div', { class: 'form-group' }, [el('label', { text: 'Upscaler' }), hrUpSel]));

  // ADetailer (3 passes)
  const adEnabled = el('input', { type: 'checkbox', id: 'sd-ad' });
  const rfEnabled = el('input', { type: 'checkbox', id: 'sd-rf' });
  paneAD.appendChild(el('div', { style: 'margin-bottom:10px; padding:8px; background:var(--surface-3); border-radius:var(--r-sm); font-size:.8rem; color:var(--text-2)' }, [
    el('strong', { text: 'Enhance specific body parts. ' }),
    el('span', { text: 'Pick target (face/hands/person), set denoise strength, add custom prompts. Up to 3 passes.' })
  ]));
  paneAD.appendChild(el('div', { style: 'display:flex; align-items:center; justify-content:space-between; margin-bottom:10px;' }, [
    el('div', { style: 'display:flex; align-items:center; gap:6px' }, [
      adEnabled,
      el('label', { for: 'sd-ad', text: 'Enable ADetailer', style: 'cursor:pointer; font-weight:600' }),
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:6px' }, [
      rfEnabled,
      el('label', { for: 'sd-rf', text: 'Restore Faces', style: 'font-size:.82rem; color:var(--text-3)' }),
    ]),
  ]));

  const AD_TARGETS = {
    'Face':           'face_yolov8n.pt',
    'Face (precise)': 'mediapipe_face_full',
    'Hands':          'hand_yolov8n.pt',
    'Person':         'person_yolov8n-seg.pt',
  };

  const adPasses = [
    { enabled: true,  target: 'Face',  denoise: 0.4, model: 'face_yolov8n.pt',  confidence: 0.3, mask_blur: 4, inpaint_only_masked: true, padding: 32, min_ratio: 0.0, max_ratio: 1.0, prompt: '', negative_prompt: '' },
    { enabled: false, target: 'Hands', denoise: 0.35, model: 'hand_yolov8n.pt', confidence: 0.3, mask_blur: 4, inpaint_only_masked: true, padding: 16, min_ratio: 0.0, max_ratio: 1.0, prompt: '', negative_prompt: '' },
    { enabled: false, target: 'Face (precise)', denoise: 0.45, model: 'mediapipe_face_full', confidence: 0.5, mask_blur: 6, inpaint_only_masked: true, padding: 40, min_ratio: 0.0, max_ratio: 1.0, prompt: '', negative_prompt: '' },
  ];

  function getAdSweeps() {
    return adPasses.filter(p => p.enabled).map(p => ({
      model: AD_TARGETS[p.target] || p.model,
      confidence: p.confidence,
      mask_blur: p.mask_blur,
      denoise: p.denoise,
      inpaint_only_masked: p.inpaint_only_masked,
      padding: p.padding,
      min_ratio: p.min_ratio,
      max_ratio: p.max_ratio,
      prompt: p.prompt,
      negative_prompt: p.negative_prompt,
    }));
  }

  let adSweeps = [];

  const passLabels = ['Pass 1', 'Pass 2', 'Pass 3'];
  adPasses.forEach((pass, i) => {
    const row = el('div', { style: `display:grid; grid-template-columns:auto 1fr auto; gap:8px; align-items:center; padding:8px 0; border-top:1px solid var(--border);` });

    const chk = el('input', { type: 'checkbox', id: `ad-p${i}`, ...(pass.enabled ? { checked: 'true' } : {}) });
    chk.addEventListener('change', () => {
      pass.enabled = chk.checked;
      adSweeps = getAdSweeps();
    });
    const chkLabel = el('label', { for: `ad-p${i}`, style: 'font-size:.8rem; font-weight:600; color:var(--text-3); cursor:pointer; white-space:nowrap;', text: passLabels[i] });

    const leftCol2 = el('div', { style: 'display:flex; align-items:center; gap:6px;' });
    leftCol2.appendChild(chk);
    leftCol2.appendChild(chkLabel);
    row.appendChild(leftCol2);

    const mid = el('div', { style: 'display:flex; gap:6px; align-items:center; flex-wrap:wrap;' });
    const targetSel = el('select', { style: 'font-size:.82rem; flex:1; min-width:120px;' });
    Object.keys(AD_TARGETS).forEach(t => {
      targetSel.appendChild(el('option', { value: t, text: t, ...(t === pass.target ? { selected: 'true' } : {}) }));
    });
    targetSel.addEventListener('change', () => {
      pass.target = targetSel.value;
      pass.model  = AD_TARGETS[targetSel.value];
      adSweeps = getAdSweeps();
    });

    const denoiseVal = el('span', { text: pass.denoise.toFixed(2), style: 'font-size:.78rem; color:var(--accent); font-family:monospace; min-width:28px; text-align:right;' });
    const denoiseSlider = el('input', { type: 'range', min: '0.05', max: '1', step: '0.05', value: String(pass.denoise), style: 'flex:1; min-width:80px;' });
    denoiseSlider.addEventListener('input', () => {
      pass.denoise = parseFloat(denoiseSlider.value);
      denoiseVal.textContent = pass.denoise.toFixed(2);
      adSweeps = getAdSweeps();
    });

    mid.appendChild(targetSel);
    mid.appendChild(denoiseSlider);
    mid.appendChild(denoiseVal);
    row.appendChild(mid);

    const moreBtn = el('button', { class: 'btn btn-sm', text: '⋯', title: 'Custom prompt for this pass', style: 'padding:4px 8px; font-size:.72rem;' });
    row.appendChild(moreBtn);

    paneAD.appendChild(row);

    const promptRow = el('div', { style: 'display:none; padding:6px 0 4px 28px; gap:6px; flex-direction:column;' });
    const swPromptTA = el('textarea', { rows: '2', placeholder: 'Custom positive prompt for this pass', style: 'width:100%; font-size:.78rem; resize:vertical;' });
    swPromptTA.value = pass.prompt;
    swPromptTA.addEventListener('input', () => { pass.prompt = swPromptTA.value; adSweeps = getAdSweeps(); });
    promptRow.appendChild(swPromptTA);
    paneAD.appendChild(promptRow);

    moreBtn.addEventListener('click', () => {
      const open = promptRow.style.display !== 'none';
      promptRow.style.display = open ? 'none' : 'flex';
      moreBtn.textContent = open ? '⋯' : '▲';
    });
  });

  adSweeps = getAdSweeps();

  // Img2Img
  const i2iPathIn   = el('input', { type: 'text', placeholder: 'Image path (auto-filled after generation)' });
  const i2iPromptIn = el('textarea', { rows: '2', placeholder: 'Prompt (blank = use main prompt)' });
  const i2iDenoise  = createSlider(paneI2I, { label: 'Denoise strength', min: 0.05, max: 1, step: 0.05, value: 0.5 });
  paneI2I.appendChild(el('div', { class: 'form-group' }, [el('label', { text: 'Input Image Path' }), i2iPathIn]));
  paneI2I.appendChild(el('div', { class: 'form-group' }, [el('label', { text: 'Prompt' }), i2iPromptIn]));
  const i2iBtn = el('button', { class: 'btn btn-primary', text: 'Run img2img', style: 'margin-top:6px; width:100%' });
  paneI2I.appendChild(i2iBtn);

  // ── Advanced Settings Section ─────────────────────────────────────────────
  const advancedSec = createSection('Advanced Settings', '⚙️', 'advanced-settings', false);
  sectionsContainer.appendChild(advancedSec.section);

  const samplerSel = el('select', { style: 'width:100%' });
  const schedSel   = el('select', { style: 'width:100%' });
  advancedSec.body.appendChild(el('div', { class: 'settings-2col' }, [
    el('div', { class: 'form-group' }, [el('label', { text: 'Sampler' }), samplerSel]),
    el('div', { class: 'form-group' }, [el('label', { text: 'Scheduler' }), schedSel]),
  ]));

  const wIn = el('input', { type: 'number', value: '1440', min: '64', max: '5120', step: '8', style: 'width:100%' });
  const hIn = el('input', { type: 'number', value: '810',  min: '64', max: '5120', step: '8', style: 'width:100%' });
  advancedSec.body.appendChild(el('div', { class: 'settings-2col' }, [
    el('div', { class: 'form-group' }, [el('label', { text: 'Width' }),  wIn]),
    el('div', { class: 'form-group' }, [el('label', { text: 'Height' }), hIn]),
  ]));

  const presetsWrap = el('div', { style: 'display:flex; flex-wrap:wrap; gap:3px; margin-bottom:10px' });
  advancedSec.body.appendChild(presetsWrap);
  [{ l:'1:1',w:1024,h:1024},{ l:'4:3',w:1440,h:1080},{ l:'3:2',w:1440,h:960},
   { l:'16:9',w:1440,h:810},{ l:'9:16',w:810,h:1440},{ l:'1080p',w:1920,h:1080}].forEach(p => {
    presetsWrap.appendChild(el('button', { class: 'btn btn-sm', text: p.l,
      style: 'font-size:.68rem; padding:2px 7px; opacity:.8',
      onclick() { wIn.value = p.w; hIn.value = p.h; },
    }));
  });
  presetsWrap.appendChild(el('button', { class: 'btn btn-sm', text: 'W↔H',
    style: 'font-size:.68rem; padding:2px 7px; opacity:.8',
    onclick() { const t = wIn.value; wIn.value = hIn.value; hIn.value = t; },
  }));

  const stepsSlider = createSlider(advancedSec.body, { label: 'Steps', min: 1, max: 60, step: 1, value: 28 });
  const cfgSlider   = createSlider(advancedSec.body, { label: 'CFG Scale', min: 1, max: 20, step: 0.5, value: 7 });

  const seedIn = el('input', { type: 'number', value: '-1', style: 'flex:1' });
  advancedSec.body.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:flex-end; margin-bottom:8px' }, [
    el('div', { class: 'form-group', style: 'flex:1' }, [el('label', { text: 'Seed (-1 = random)' }), seedIn]),
    el('button', { class: 'btn btn-sm', text: 'Rnd', style: 'margin-bottom:8px', onclick() { seedIn.value = '-1'; } }),
  ]));

  const batchCntIn  = el('input', { type: 'number', value: '1', min: '1', max: '8', step: '1', style: 'width:100%' });
  const batchSizeIn = el('input', { type: 'number', value: '1', min: '1', max: '8', step: '1', style: 'width:100%' });
  advancedSec.body.appendChild(el('div', { class: 'settings-2col' }, [
    el('div', { class: 'form-group' }, [el('label', { text: 'Batch Count' }), batchCntIn]),
    el('div', { class: 'form-group' }, [el('label', { text: 'Batch Size' }), batchSizeIn]),
  ]));

  // ── Gallery Section ───────────────────────────────────────────────────────
  const gallerySec = createSection('Gallery', '🖼️', 'gallery', false);
  sectionsContainer.appendChild(gallerySec.section);

  const galleryGrid  = el('div', { style: 'display:grid; grid-template-columns:repeat(auto-fill,minmax(80px,1fr)); gap:6px' });
  const galleryEmpty = el('p', { style: 'font-size:.82rem; color:var(--text-3)', text: 'Generated images appear here.' });
  gallerySec.body.appendChild(galleryEmpty);
  gallerySec.body.appendChild(galleryGrid);

  // ════════════════════════════════════════════════════════════════════════════
  // FUNCTIONS
  // ════════════════════════════════════════════════════════════════════════════

  function buildFullPrompt() {
    const base = baseTA.value.trim();
    const sfx  = suffixIn.value.trim();
    return base + (sfx ? ', ' + sfx : '');
  }

  function buildFullPromptWithFC() {
    const full = buildFullPrompt();
    const cellValues = fcCells.map(ta => ta?.value?.trim() || '').filter(Boolean);
    if (!fcEnabled || !cellValues.length) return full;
    return [full, ...cellValues].join('\n');
  }

  function showImage(idx) {
    if (idx < 0 || idx >= generatedImages.length) return;
    currentIdx = idx;
    galleryGrid.querySelectorAll('.sd-thumb').forEach((t, i) => {
      t.style.outline = i === idx ? '2px solid var(--accent)' : 'none';
    });
  }

  function addToGallery(entry) {
    galleryEmpty.style.display = 'none';
    const wrap = el('div', { style: 'position:relative; cursor:pointer;' });
    const thumb = el('img', {
      class: 'sd-thumb',
      src: entry.src,
      style: 'width:100%; aspect-ratio:1; object-fit:cover; border-radius:var(--r-sm); border:2px solid transparent; display:block;',
      title: `Seed: ${entry.seed}`,
      onclick() { showImage(generatedImages.indexOf(entry)); },
    });
    const vidBtn = el('button', {
      text: 'Make Video',
      style: 'display:none; position:absolute; bottom:4px; left:50%; transform:translateX(-50%); font-size:.65rem; padding:2px 6px; white-space:nowrap; background:var(--accent); color:#fff; border:none; border-radius:var(--r-sm); cursor:pointer;',
      onclick(e) {
        e.stopPropagation();
        handoff('fun-videos', { type: 'image', url: entry.src, path: entry.path || '' });
        document.querySelector('[data-tab="fun-videos"]')?.click();
      },
    });
    wrap.addEventListener('mouseenter', () => { vidBtn.style.display = ''; thumb.style.border = '2px solid var(--accent)'; });
    wrap.addEventListener('mouseleave', () => { vidBtn.style.display = 'none'; thumb.style.border = '2px solid transparent'; });
    wrap.appendChild(thumb);
    wrap.appendChild(vidBtn);
    galleryGrid.appendChild(wrap);
  }

  function toggleFC(enable) {
    fcEnabled = enable;
    fcToggleChk.checked = enable;
    fcConfigRow.style.display = enable ? '' : 'none';
    fcGridWrap.style.display = enable ? '' : 'none';
    fcBgRow.style.display = enable ? '' : 'none';
    fcViewToggleRow.style.display = enable ? 'flex' : 'none';
    if (fcQuickToggle) fcQuickToggle.classList.toggle('btn-primary', enable);
    if (enable) regionalSec.toggle(true);
  }

  // ── Forge status ──────────────────────────────────────────────────────────
  let _retryTimer = null;
  async function checkForge() {
    try {
      forgeStatus = await api('/api/prompts/forge/status');
      if (forgeStatus.alive) {
        if (_retryTimer) { clearInterval(_retryTimer); _retryTimer = null; }
        forgeDot.className = 'dot running';
        forgeMsg.textContent = `Forge SD — ${forgeStatus.current_model || 'ready'}`;
        genBtn.disabled = false;
        modelSel.innerHTML = (forgeStatus.models || [])
          .map(m => `<option value="${m.title||m.name}"${(m.title||m.name).includes(forgeStatus.current_model||'') ? ' selected' : ''}>${m.title||m.name}</option>`).join('');
        const ds = forgeStatus.default_sampler || 'DPM++ 2M SDE';
        samplerSel.innerHTML = (forgeStatus.samplers || ['DPM++ 2M SDE','Euler','DDIM'])
          .map(s => `<option value="${s}"${s===ds?' selected':''}>${s}</option>`).join('');
        const dsc = forgeStatus.default_scheduler || 'Karras';
        schedSel.innerHTML = (forgeStatus.schedulers || ['Karras','Automatic'])
          .map(s => `<option value="${s}"${s===dsc?' selected':''}>${s}</option>`).join('');
        hrUpSel.innerHTML = (forgeStatus.upscalers || ['ESRGAN_4x','Latent','None'])
          .map(u => `<option value="${u}">${u}</option>`).join('');
      } else {
        forgeDot.className = 'dot not_configured';
        forgeMsg.textContent = 'Forge not running — start with --api flag';
        genBtn.disabled = true;
        if (!_retryTimer) _retryTimer = setInterval(checkForge, 10000);
      }
    } catch (_) {
      forgeDot.className = 'dot error';
      forgeMsg.textContent = 'Cannot reach Forge';
      genBtn.disabled = true;
      if (!_retryTimer) _retryTimer = setInterval(checkForge, 10000);
    }
  }

  // ── Generate ──────────────────────────────────────────────────────────────
  let progressTimer = null;
  genBtn.addEventListener('click', async () => {
    if (!forgeStatus?.alive) { toast('Forge is not running', 'error'); return; }

    const prompt = buildFullPromptWithFC();
    if (!prompt.trim()) { toast('Enter or enhance a prompt above first', 'error'); return; }

    genBtn.disabled = true;
    genBtn.textContent = 'Submitting…';
    progressTimer = setInterval(async () => {
      try {
        const p = await api('/api/prompts/forge/progress');
        const pct = Math.round((p.progress||0)*100);
        genBtn.textContent = pct > 0 ? `Generating… ${pct}%` : 'Generating…';
      } catch (_) {}
    }, 1000);

    try {
      const data = await api('/api/prompts/forge/txt2img', {
        method: 'POST',
        body: JSON.stringify({
          prompt:                  buildFullPrompt(),
          columns:                 fcEnabled ? fcCells.map(ta => ta?.value || '') : [],
          use_forge_couple:        fcEnabled,
          forge_couple_direction:  fcDirection,
          forge_couple_bg_weight:  parseFloat(fcBgSlider.value),
          forge_couple_background: fcBackground,
          steps:                   stepsSlider.value,
          cfg_scale:               cfgSlider.value,
          sampler:                 samplerSel.value,
          scheduler:               schedSel.value,
          width:                   parseInt(wIn.value),
          height:                  parseInt(hIn.value),
          seed:                    parseInt(seedIn.value),
          n_iter:                  parseInt(batchCntIn.value),
          batch_size:              parseInt(batchSizeIn.value),
          enable_hr:               hrEnabled.checked,
          hr_scale:                hrScale.value,
          hr_upscaler:             hrUpSel.value || 'ESRGAN_4x',
          hr_second_pass_steps:    hrSteps.value,
          hr_denoising_strength:   hrDenoise.value,
          adetailer_sweeps:        adEnabled.checked ? getAdSweeps() : null,
          restore_faces:           rfEnabled.checked,
        }),
      });
      if (data.images?.length) {
        const info = `${samplerSel.value} • CFG ${cfgSlider.value} • ${stepsSlider.value} steps`;
        for (let i = 0; i < data.images.length; i++) {
          const b64 = data.images[i];
          const savedPath = data.saved_paths?.[i] || null;
          const entry = { src: `data:image/png;base64,${b64}`, seed: data.seed||-1, prompt, path: savedPath, info };
          generatedImages.push(entry);
          addToGallery(entry);
          if (savedPath) pushToGallery('sd-prompts', savedPath, prompt, data.seed, {
            model: forgeStatus?.current_model || 'forge',
            sampler: samplerSel.value,
            scheduler: schedSel.value,
            steps: Number(stepsSlider.value),
            cfg: Number(cfgSlider.value),
            width: Number(wIn.value),
            height: Number(hIn.value),
          });
        }
        const lastEntry = generatedImages[generatedImages.length - 1];
        showImage(generatedImages.length - 1);
        setPreview(lastEntry.src, prompt, [
          { label: '🎬 Create Video', primary: true, onClick() {
              handoff('fun-videos', { type: 'image', url: lastEntry.src, path: lastEntry.path || '' });
              document.querySelector('[data-tab="fun-videos"]')?.click();
          }},
          { label: '⬇ Save', onClick() {
              const a = document.createElement('a');
              a.href = lastEntry.src;
              a.download = `sd-${lastEntry.seed ?? 'image'}.png`;
              a.click();
          }},
          { label: '🔁 Variation', onClick() {
              seedIn.value = lastEntry.seed + 1;
              genBtn.click();
          }},
        ]);
        toast(`✨ Done! Seed: ${data.seed}`, 'success');
      }
    } catch (e) { toast(e.message, 'error'); }

    clearInterval(progressTimer);
    genBtn.textContent = '⚡ Generate';
    genBtn.disabled = false;
  });


  // ── img2img ───────────────────────────────────────────────────────────────
  i2iBtn.addEventListener('click', async () => {
    const path = i2iPathIn.value.trim();
    if (!path) { toast('Image path required', 'error'); return; }
    i2iBtn.disabled = true;
    i2iBtn.textContent = 'Processing...';
    try {
      const res = await fetch(`/output/${path.split(/[\\/]/).pop()}`);
      const blob = await res.blob();
      const b64 = await new Promise(resolve => {
        const r = new FileReader();
        r.onload = () => resolve(r.result.split(',')[1]);
        r.readAsDataURL(blob);
      });
      const data = await api('/api/prompts/forge/img2img', {
        method: 'POST',
        body: JSON.stringify({
          init_image: b64,
          prompt: i2iPromptIn.value.trim() || buildFullPromptWithFC(),
          denoising_strength: i2iDenoise.value,
          steps: stepsSlider.value,
          cfg_scale: cfgSlider.value,
          sampler: samplerSel.value,
          scheduler: schedSel.value,
          width: parseInt(wIn.value),
          height: parseInt(hIn.value),
          seed: parseInt(seedIn.value),
          adetailer_sweeps: adEnabled.checked ? getAdSweeps() : null,
        })
      });
      if (data.images?.length) {
        const savedPath = data.saved_paths?.[0] || null;
        const entry = {
          src: `data:image/png;base64,${data.images[0]}`,
          seed: data.seed,
          prompt: i2iPromptIn.value,
          path: savedPath,
          info: `img2img • denoise ${i2iDenoise.value}`
        };
        generatedImages.push(entry);
        addToGallery(entry);
        if (savedPath) pushToGallery('sd-prompts', savedPath, i2iPromptIn.value, data.seed, {
          model: forgeStatus?.current_model || 'forge',
          mode: 'img2img',
          denoise: Number(i2iDenoise.value),
          steps: Number(stepsSlider.value),
          cfg: Number(cfgSlider.value),
        });
        showImage(generatedImages.length - 1);
        toast(`img2img done! Seed: ${data.seed}`, 'success');
      }
    } catch (e) { toast(e.message, 'error'); }
    i2iBtn.disabled = false;
    i2iBtn.textContent = 'Run img2img';
  });

  // ── Defaults ──────────────────────────────────────────────────────────────
  async function loadDefaults() {
    try {
      const cfg = await api('/api/config');
      if (cfg.forge_default_width)   wIn.value = cfg.forge_default_width;
      if (cfg.forge_default_height)  hIn.value = cfg.forge_default_height;
      if (cfg.forge_default_steps)   stepsSlider.value = cfg.forge_default_steps;
      if (cfg.forge_default_cfg != null) cfgSlider.value = cfg.forge_default_cfg;
    } catch (_) {}
  }

  // Helper used by the Step 1 panel to apply an enhanced prompt payload.
  // Also populates regional cells when payload includes a `regions` array.
  function applyEnhanced(payload) {
    if (!payload) return;
    if (typeof payload.prompt === 'string') baseTA.value = payload.prompt;
    if (typeof payload.suffix === 'string') suffixIn.value = payload.suffix;
    if (Array.isArray(payload.regions) && payload.regions.length) {
      const n = payload.regions.length;
      // Resize grid to match returned region count (horizontal strip)
      fcRows = 1;
      fcCols = Math.min(Math.max(n, 1), 4);
      fcRowsIn.value = String(fcRows);
      fcColsIn.value = String(fcCols);
      if (!fcEnabled) toggleFC(true);
      renderFCGrid();
      payload.regions.forEach((r, i) => { if (fcCells[i] !== undefined) fcCells[i].value = r; });
      regionalSec.toggle(true);
    }
    promptSec.toggle(true);
  }

  // Settings button opens Advanced Settings section
  settingsBtn.addEventListener('click', () => {
    advancedSec.toggle(true);
  });

  // Startup
  loadDefaults();
  checkForge();
  // Step 1 front door is built after core UI so it can reference baseTA, suffixIn, etc.
  buildStep1Panel({
    root, sectionsContainer,
    baseTA, suffixIn, regionalSec, promptSec,
    toggleFC, renderFCGrid,
    applyEnhanced,
    getFcEnabled: () => fcEnabled,
    getRegionsN: () => Math.max(1, fcRows * fcCols),
    genBtn,
  });
}

// ══════════════════════════════════════════════════════════════════════════════
// Step 1 front door — user decides (a) single vs regional and (b) vague idea
// vs own-prompt paste. Default suffix "(depth blur)". Local Ollama by default;
// toggle to cloud API for R-rated output via the built-in euphemism sanitizer.
// ══════════════════════════════════════════════════════════════════════════════

const STEP1_LS = {
  shape:    'dropcat_step1_shape',
  source:   'dropcat_step1_source',
  provider: 'dropcat_step1_provider',
  rrated:   'dropcat_step1_rrated',
  suffix:   'dropcat_step1_suffix',
};

function _loadStep1(key, fallback) {
  try {
    const v = localStorage.getItem(STEP1_LS[key]);
    return v === null ? fallback : v;
  } catch (_) { return fallback; }
}

function _saveStep1(key, val) {
  try { localStorage.setItem(STEP1_LS[key], String(val)); } catch (_) {}
}

function buildStep1Panel(ctx) {
  const {
    sectionsContainer,
    baseTA, suffixIn,
    toggleFC, renderFCGrid,
    applyEnhanced,
    genBtn,
  } = ctx;

  const DEFAULT_SUFFIX = '(depth blur)';

  // Initial state — read saved prefs (or defaults).
  let shape     = _loadStep1('shape',    'single');    // "single" | "regional"
  let source    = _loadStep1('source',   'vague');     // "vague"  | "paste"
  let provider  = _loadStep1('provider', 'local');     // "local"  | "cloud"
  let rrated    = _loadStep1('rrated',   'false') === 'true';
  let smartWc   = _loadStep1('smart_wildcards', 'true') === 'true';  // default ON
  const savedSuffix = _loadStep1('suffix', DEFAULT_SUFFIX);

  // Sync suffix input with saved value (keeps it consistent on tab re-entry)
  if (savedSuffix) suffixIn.value = savedSuffix;

  // ── Panel container ─────────────────────────────────────────────────────
  const panel = el('div', { class: 'step1-panel', style: 'margin-bottom:14px' });

  const header = el('div', { class: 'step1-header' }, [
    el('span', { class: 'step1-badge', text: 'Step 1' }),
    el('span', { class: 'step1-title', text: 'How do you want to prompt?' }),
  ]);
  panel.appendChild(header);

  // ── Row 1: Prompt shape ────────────────────────────────────────────────
  const shapeRow = el('div', { class: 'step1-row' });
  shapeRow.appendChild(el('span', { class: 'step1-label', text: 'Prompt shape' }));
  const shapeSeg = el('div', { class: 'step1-seg', role: 'radiogroup', 'aria-label': 'Prompt shape' });
  const shapeBtnSingle = el('button', { class: 'step1-seg-btn', type: 'button', text: 'Single prompt', role: 'radio' });
  const shapeBtnRegion = el('button', { class: 'step1-seg-btn', type: 'button', text: 'Regional (Forge Couple)', role: 'radio' });
  shapeSeg.appendChild(shapeBtnSingle);
  shapeSeg.appendChild(shapeBtnRegion);
  shapeRow.appendChild(shapeSeg);
  panel.appendChild(shapeRow);

  // ── Row 1b: Smart wildcards ────────────────────────────────────────────
  const wcRow = el('div', { class: 'step1-row' });
  wcRow.appendChild(el('span', {
    class: 'step1-label',
    text: 'Smart wildcards',
    title: 'When ON, the AI picks __wildcards__ from your library to add variety. Ask it to "add a wildcard for X" to create new ones automatically.',
  }));
  const wcSeg = el('div', { class: 'step1-seg', role: 'radiogroup', 'aria-label': 'Smart wildcards' });
  const wcBtnOn  = el('button', { class: 'step1-seg-btn', type: 'button', text: 'On',  role: 'radio', title: 'AI uses existing wildcards and can create new ones when you ask' });
  const wcBtnOff = el('button', { class: 'step1-seg-btn', type: 'button', text: 'Off', role: 'radio', title: 'Literal prompt, no __token__ placeholders' });
  wcSeg.appendChild(wcBtnOn);
  wcSeg.appendChild(wcBtnOff);
  wcRow.appendChild(wcSeg);
  panel.appendChild(wcRow);

  // ── Row 2: Source ──────────────────────────────────────────────────────
  const sourceRow = el('div', { class: 'step1-row' });
  sourceRow.appendChild(el('span', { class: 'step1-label', text: 'Starting from' }));
  const sourceSeg = el('div', { class: 'step1-seg', role: 'radiogroup', 'aria-label': 'Prompt source' });
  const sourceBtnVague = el('button', { class: 'step1-seg-btn', type: 'button', text: 'Vague idea → AI', role: 'radio' });
  const sourceBtnPaste = el('button', { class: 'step1-seg-btn', type: 'button', text: "I'll paste my own", role: 'radio' });
  sourceSeg.appendChild(sourceBtnVague);
  sourceSeg.appendChild(sourceBtnPaste);
  sourceRow.appendChild(sourceSeg);
  panel.appendChild(sourceRow);

  // ── Idea / paste textarea (shared — label and button text swap by source) ──
  const ideaTA = el('textarea', {
    rows: '3',
    class: 'step1-idea',
    placeholder: 'e.g. "a robot and an alien negotiating in a ruin"',
  });
  panel.appendChild(ideaTA);

  // Wire concept handoff (from Studio Home pipeline tab)
  _applyIdea = (text) => { ideaTA.value = text; ideaTA.focus(); };
  if (_pendingConcept) {
    ideaTA.value = _pendingConcept;
    _pendingConcept = null;
  }

  // ── Provider + R-rated row (only shown for vague source) ───────────────
  const providerRow = el('div', { class: 'step1-row step1-provider-row' });
  providerRow.appendChild(el('span', { class: 'step1-label', text: 'LLM' }));
  const providerSeg = el('div', { class: 'step1-seg', role: 'radiogroup', 'aria-label': 'LLM provider' });
  const provBtnLocal = el('button', { class: 'step1-seg-btn', type: 'button', text: 'Local (Ollama)', role: 'radio', title: 'Uses your configured Ollama model' });
  const provBtnCloud = el('button', { class: 'step1-seg-btn', type: 'button', text: 'Cloud API', role: 'radio', title: 'Uses Anthropic/OpenAI with built-in euphemism sanitizer for R-rated output' });
  providerSeg.appendChild(provBtnLocal);
  providerSeg.appendChild(provBtnCloud);
  providerRow.appendChild(providerSeg);

  const rratedWrap = el('label', { class: 'step1-rrated', title: 'When using Cloud, route explicit terms through the euphemism sanitizer so the API does not refuse' });
  const rratedChk  = el('input', { type: 'checkbox' });
  rratedChk.checked = rrated;
  rratedWrap.appendChild(rratedChk);
  rratedWrap.appendChild(el('span', { text: 'R-rated' }));
  providerRow.appendChild(rratedWrap);
  panel.appendChild(providerRow);

  // ── Suffix row ─────────────────────────────────────────────────────────
  const suffixRow = el('div', { class: 'step1-row step1-suffix-row' });
  suffixRow.appendChild(el('span', { class: 'step1-label', text: 'Suffix' }));
  const suffixMirror = el('input', {
    type: 'text',
    class: 'step1-suffix',
    value: suffixIn.value || DEFAULT_SUFFIX,
    placeholder: DEFAULT_SUFFIX,
    title: 'Appended to the end of the prompt on every Generate. Leave blank for none.',
  });
  suffixRow.appendChild(suffixMirror);
  const suffixReset = el('button', { class: 'btn btn-sm', type: 'button', text: 'Reset', title: `Reset to ${DEFAULT_SUFFIX}` });
  suffixRow.appendChild(suffixReset);
  panel.appendChild(suffixRow);

  // Keep suffix input in the Advanced section mirrored to this one and vice versa.
  suffixMirror.addEventListener('input', () => {
    suffixIn.value = suffixMirror.value;
    _saveStep1('suffix', suffixMirror.value);
  });
  suffixIn.addEventListener('input', () => {
    suffixMirror.value = suffixIn.value;
    _saveStep1('suffix', suffixIn.value);
  });
  suffixReset.addEventListener('click', () => {
    suffixMirror.value = DEFAULT_SUFFIX;
    suffixIn.value = DEFAULT_SUFFIX;
    _saveStep1('suffix', DEFAULT_SUFFIX);
  });

  // ── Action row ─────────────────────────────────────────────────────────
  const actionRow = el('div', { class: 'step1-actions' });
  const enhanceBtn = el('button', { class: 'btn btn-primary', type: 'button', text: '✨ Enhance & Fill Prompt' });
  const pasteBtn   = el('button', { class: 'btn',             type: 'button', text: '✍ Use this prompt' });
  const statusMsg  = el('span', { class: 'step1-status', text: '' });
  actionRow.appendChild(enhanceBtn);
  actionRow.appendChild(pasteBtn);
  actionRow.appendChild(statusMsg);
  panel.appendChild(actionRow);

  // Insert panel at the very top of sectionsContainer (above the generate bar)
  sectionsContainer.insertBefore(panel, sectionsContainer.firstChild);

  // ── Segmented control helpers ──────────────────────────────────────────
  function setSeg(segEl, activeBtn) {
    [...segEl.children].forEach(b => {
      const on = b === activeBtn;
      b.classList.toggle('on', on);
      b.setAttribute('aria-checked', on ? 'true' : 'false');
    });
  }

  function applyShape(newShape, { fromUser = false } = {}) {
    shape = newShape;
    setSeg(shapeSeg, newShape === 'regional' ? shapeBtnRegion : shapeBtnSingle);
    _saveStep1('shape', newShape);
    // Flip Forge Couple toggle to match. When user chooses regional, also make
    // sure the grid is rendered so later applyEnhanced has cells to write into.
    if (fromUser) {
      toggleFC(newShape === 'regional');
      if (newShape === 'regional') renderFCGrid();
    }
  }

  function applySource(newSource) {
    source = newSource;
    setSeg(sourceSeg, newSource === 'paste' ? sourceBtnPaste : sourceBtnVague);
    _saveStep1('source', newSource);
    // Show/hide provider row; vague path needs LLM, paste path doesn't.
    providerRow.style.display = newSource === 'vague' ? '' : 'none';
    // Swap placeholder and primary button to match mode.
    if (newSource === 'paste') {
      ideaTA.placeholder = 'Paste a ready-to-use SD prompt. Tags preferred, but prose works too.';
      enhanceBtn.style.display = 'none';
      pasteBtn.style.display = '';
    } else {
      ideaTA.placeholder = 'e.g. "a robot and an alien negotiating in a ruin"';
      enhanceBtn.style.display = '';
      pasteBtn.style.display = 'none';
    }
  }

  function applyProvider(newProvider) {
    provider = newProvider;
    setSeg(providerSeg, newProvider === 'cloud' ? provBtnCloud : provBtnLocal);
    _saveStep1('provider', newProvider);
    // R-rated only meaningful on cloud path; dim the checkbox when local.
    rratedWrap.style.opacity = newProvider === 'cloud' ? '1' : '.55';
    rratedChk.disabled = newProvider !== 'cloud';
  }

  function applySmartWc(on) {
    smartWc = !!on;
    setSeg(wcSeg, smartWc ? wcBtnOn : wcBtnOff);
    _saveStep1('smart_wildcards', smartWc ? 'true' : 'false');
  }

  // Initial application
  applyShape(shape);
  applySource(source);
  applyProvider(provider);
  applySmartWc(smartWc);

  // ── Wire controls ──────────────────────────────────────────────────────
  shapeBtnSingle.addEventListener('click', () => applyShape('single', { fromUser: true }));
  shapeBtnRegion.addEventListener('click', () => applyShape('regional', { fromUser: true }));
  sourceBtnVague.addEventListener('click', () => applySource('vague'));
  sourceBtnPaste.addEventListener('click', () => applySource('paste'));
  provBtnLocal.addEventListener('click', () => applyProvider('local'));
  provBtnCloud.addEventListener('click', () => applyProvider('cloud'));
  wcBtnOn.addEventListener('click', () => applySmartWc(true));
  wcBtnOff.addEventListener('click', () => applySmartWc(false));
  rratedChk.addEventListener('change', () => {
    rrated = rratedChk.checked;
    _saveStep1('rrated', rrated ? 'true' : 'false');
  });

  function setBusy(isBusy, msg = '') {
    enhanceBtn.disabled = isBusy;
    pasteBtn.disabled   = isBusy;
    statusMsg.textContent = msg || '';
    statusMsg.classList.toggle('busy', isBusy);
  }

  // Enhance → call /api/prompts/enhance, populate baseTA / regions.
  enhanceBtn.addEventListener('click', async () => {
    const idea = ideaTA.value.trim();
    if (!idea) { toast('Describe your idea first', 'info'); ideaTA.focus(); return; }
    setBusy(true, 'Enhancing…');
    try {
      const res = await api('/api/prompts/enhance', {
        method: 'POST',
        body: JSON.stringify({
          idea,
          regional: shape === 'regional',
          regions_n: Math.max(1, Math.min(ctx.getRegionsN(), 4)),
          suffix: suffixMirror.value,
          provider,
          allow_rrated: rrated,
          smart_wildcards: smartWc,
        }),
      });
      applyEnhanced({
        prompt: res.prompt,
        suffix: res.suffix,
        regions: Array.isArray(res.regions) ? res.regions : null,
      });
      const providerLabel = res.provider_used ? ` · via ${res.provider_used}${res.sanitized ? ' (sanitized)' : ''}` : '';
      toast(`${res.one_liner || 'Prompt ready'}${providerLabel}`, 'success', { duration: 7000 });
      if (res.created_wildcards?.length) {
        const names = res.created_wildcards.map(w => w.name).join(', ');
        toast(`Created ${res.created_wildcards.length} new wildcard${res.created_wildcards.length > 1 ? 's' : ''}: ${names}`, 'info', { duration: 8000 });
      }
      setBusy(false, 'Ready — hit Generate.');
      // Briefly pulse the Generate button to direct attention
      if (genBtn && !genBtn.disabled) {
        genBtn.classList.add('step1-pulse');
        setTimeout(() => genBtn.classList.remove('step1-pulse'), 1500);
      }
    } catch (e) {
      setBusy(false, '');
      toast(e.message || 'Enhance failed', 'error');
    }
  });

  // Paste → pipe ideaTA straight into baseTA, honor current suffix + regional.
  pasteBtn.addEventListener('click', () => {
    const text = ideaTA.value.trim();
    if (!text) { toast('Paste a prompt first', 'info'); ideaTA.focus(); return; }
    // If regional + paste: split on blank lines or newlines into regions.
    let payload;
    if (shape === 'regional') {
      const raw = text.split(/\n{2,}|\r?\n/).map(s => s.trim()).filter(Boolean);
      const globalLine = raw[0] || text;
      const regions = raw.slice(1);
      payload = {
        prompt: globalLine,
        suffix: suffixMirror.value,
        regions: regions.length ? regions : null,
      };
      if (!regions.length) {
        toast('No regions detected — treating paste as a single prompt', 'info');
      }
    } else {
      payload = { prompt: text, suffix: suffixMirror.value, regions: null };
    }
    applyEnhanced(payload);
    toast('Prompt loaded — hit Generate', 'success');
    statusMsg.textContent = 'Ready — hit Generate.';
  });

  // ── Palette-driven AI intent ──────────────────────────────────────────
  import('./shell/ai-intent.js?v=20260419e').then(({ registerTabAI }) => {
    registerTabAI('sd-prompts', {
      getContext: () => ({
        prompt: baseTA.value,
        width: parseInt(wIn.value) || 0,
        height: parseInt(hIn.value) || 0,
        steps: Number(stepsSlider.value) || 0,
        cfg: Number(cfgSlider.value) || 0,
        sampler: samplerSel.value,
        scheduler: schedSel.value,
        seed: parseInt(seedIn.value) || -1,
        regional: fcEnabled,
        smart_wildcards: smartWc,
      }),
      applySettings: (s) => {
        if (typeof s.steps === 'number')  stepsSlider.value = Math.max(1, Math.min(60, s.steps));
        if (typeof s.cfg === 'number')    cfgSlider.value   = Math.max(1, Math.min(20, s.cfg));
        if (typeof s.width === 'number')  wIn.value = Math.max(64, Math.min(5120, s.width));
        if (typeof s.height === 'number') hIn.value = Math.max(64, Math.min(5120, s.height));
        if (typeof s.sampler === 'string')   samplerSel.value   = s.sampler;
        if (typeof s.scheduler === 'string') schedSel.value     = s.scheduler;
        if (typeof s.seed === 'number')      seedIn.value       = s.seed;
        if (typeof s.prompt_append === 'string' && s.prompt_append.trim()) {
          const cur = baseTA.value.trim();
          baseTA.value = cur ? `${cur}, ${s.prompt_append.trim()}` : s.prompt_append.trim();
        }
        if (typeof s.smart_wildcards === 'boolean') applySmartWc(s.smart_wildcards);
        if (typeof s.regional === 'boolean' && s.regional !== fcEnabled) {
          applyShape(s.regional ? 'regional' : 'single', { fromUser: true });
        }
      },
    });
  }).catch(() => {});
}
