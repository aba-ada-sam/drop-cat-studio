/**
 * Drop Cat Go Studio -- SD Studio
 * Layout: prompts (left) | settings + chat (right) | output | gallery
 */
import { api, apiUpload } from './api.js';
import { toast, createDropZone, createSlider, el } from './components.js';
import { handoff } from './handoff.js';

// ── State ───────────────────────────────────────────────────────────────────
let sessionId        = 'default';
let sessionActive    = false;   // true after first AI generate
let forgeStatus      = null;
let generatedImages  = [];
let currentIdx       = -1;
let lastFocusedTA    = null;
let refImagePath     = null;    // image to analyse when chatting
let fcRows = 1, fcCols = 3;    // Forge Couple grid
let fcDirection = 'Horizontal';
let fcEnabled = false;
let fcQuickToggle = null;        // quick-access button near Generate

let _pendingHandoff = null;
let _applyHandoff   = null;

export function receiveHandoff(data) {
  if (data.type === 'image' && data.path) {
    if (_applyHandoff) _applyHandoff(data.path);
    else _pendingHandoff = data.path;
  }
}

// ── Init ─────────────────────────────────────────────────────────────────────
export function init(panel) {
  panel.innerHTML = '';
  const outer = el('div', { style: 'display:flex; flex-direction:column; gap:14px;' });
  panel.appendChild(outer);

  // ── Status bar + model ────────────────────────────────────────────────────
  const statusBar = el('div', { class: 'card', style: 'padding:10px 16px; display:flex; align-items:center; gap:12px; flex-wrap:wrap;' });
  outer.appendChild(statusBar);
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
    try { await api('/api/prompts/forge/set-model', { method: 'POST', body: JSON.stringify({ model: modelSel.value }) }); toast(`Loading: ${modelSel.value}`, 'info'); }
    catch (e) { toast(e.message, 'error'); }
  });

  // ── Main two-column grid ───────────────────────────────────────────────────
  const mainGrid = el('div', { class: 'sd-top-grid' });
  outer.appendChild(mainGrid);

  // ════════════════════════════════════════════════════════════════════════════
  // LEFT COLUMN: Prompts + Forge Couple
  // ════════════════════════════════════════════════════════════════════════════
  const leftCol = el('div', { style: 'display:flex; flex-direction:column; gap:14px;' });
  mainGrid.appendChild(leftCol);

  // ── Positive Prompt card ──────────────────────────────────────────────────
  const posCard = el('div', { class: 'card' });
  leftCol.appendChild(posCard);

  posCard.appendChild(el('div', { style: 'display:flex; align-items:center; gap:8px; margin-bottom:8px' }, [
    el('h3', { text: 'Positive Prompt', style: 'flex:1; margin:0' }),
    el('button', { class: 'btn btn-sm', text: '__ Wildcards', style: 'font-size:.72rem; opacity:.8',
      onclick() { wcStrip.style.display = wcStrip.style.display === 'none' ? '' : 'none'; if (wcStrip.style.display !== 'none' && !wcLoaded) loadWildcards(); },
    }),
    el('button', { class: 'btn btn-sm', text: 'Copy', style: 'font-size:.72rem',
      onclick() { navigator.clipboard.writeText(buildFullPrompt()); toast('Copied', 'success'); },
    }),
  ]));

  const baseTA = el('textarea', { rows: '7', style: 'width:100%; resize:vertical',
    placeholder: 'Write your prompt here — or chat with the AI on the right to generate one.\nTips: masterpiece, cinematic, neon city, rain, dramatic lighting...' });
  baseTA.addEventListener('focus', () => lastFocusedTA = baseTA);
  posCard.appendChild(baseTA);

  // Suffix
  const suffixRow = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-top:8px' });
  posCard.appendChild(suffixRow);
  suffixRow.appendChild(el('label', { text: 'Suffix', style: 'font-size:.78rem; color:var(--text-3); flex-shrink:0', title: 'Appended to prompt on every generation' }));
  const suffixIn = el('input', { type: 'text', value: '(depth blur)', style: 'flex:1', title: 'Appended to every generation' });
  suffixRow.appendChild(suffixIn);

  // Wildcard chips
  const wcStrip = el('div', { class: 'wc-strip', style: 'display:none' });
  posCard.appendChild(wcStrip);
  let wcLoaded = false;

  async function loadWildcards() {
    wcLoaded = true;
    wcStrip.innerHTML = '<span style="font-size:.75rem;color:var(--text-3)">Loading...</span>';
    try {
      const data = await api('/api/prompts/wildcards');
      wcStrip.innerHTML = '';
      for (const f of (data.files || [])) {
        const chip = el('button', { class: 'wc-chip', text: f.token.replace(/__/g, ''), title: f.samples?.slice(0, 3).join(', ') || '',
          onclick() {
            const ta = lastFocusedTA || baseTA;
            const pos = ta.selectionStart;
            ta.value = ta.value.slice(0, pos) + f.token + ta.value.slice(ta.selectionEnd);
            ta.selectionStart = ta.selectionEnd = pos + f.token.length;
            ta.focus();
          },
        });
        wcStrip.appendChild(chip);
      }
      if (!data.files?.length) wcStrip.textContent = 'No wildcards — configure wildcards directory in Settings.';
    } catch (e) { wcStrip.textContent = e.message; }
  }

  // ── Negative Prompt card ──────────────────────────────────────────────────
  const negCard = el('div', { class: 'card' });
  leftCol.appendChild(negCard);
  negCard.appendChild(el('div', { style: 'display:flex; align-items:center; gap:8px; margin-bottom:8px' }, [
    el('h3', { text: 'Negative Prompt', style: 'flex:1; margin:0' }),
    el('button', { class: 'btn btn-sm', text: 'Copy', style: 'font-size:.72rem',
      onclick() { navigator.clipboard.writeText(negTA.value); toast('Copied', 'success'); },
    }),
  ]));
  const negTA = el('textarea', { rows: '3', style: 'width:100%; resize:vertical',
    value: 'blurry, low quality, watermark, text, logo, ugly, deformed, bad anatomy' });
  negTA.addEventListener('focus', () => lastFocusedTA = negTA);
  negCard.appendChild(negTA);

  // ── Forge Couple card ──────────────────────────────────────────────────────
  const fcCard = el('div', { class: 'card' });
  leftCol.appendChild(fcCard);

  // Header row
  const fcHeaderRow = el('div', { style: 'display:flex; align-items:center; gap:10px; flex-wrap:wrap' });
  fcCard.appendChild(fcHeaderRow);
  fcHeaderRow.appendChild(el('h3', { text: 'Forge Couple — Regional Prompting', style: 'flex:1; margin:0; white-space:nowrap' }));

  const fcPill = el('button', { class: 'fc-pill off', text: 'OFF', onclick() { toggleFC(!fcEnabled); } });
  fcHeaderRow.appendChild(fcPill);

  function toggleFC(on) {
    fcEnabled = on;
    fcPill.textContent = on ? 'ON' : 'OFF';
    fcPill.className = `fc-pill ${on ? 'on' : 'off'}`;
    if (fcQuickToggle) { fcQuickToggle.textContent = on ? 'FC ●' : 'FC ○'; fcQuickToggle.style.color = on ? 'var(--accent)' : ''; }
    fcConfigRow.style.display = on ? '' : 'none';
    fcGridWrap.style.display  = on ? '' : 'none';
    fcBgRow.style.display     = on ? '' : 'none';
    fcExplain.style.display   = on ? 'none' : '';
    if (on) renderFCGrid();
  }

  const fcExplain = el('p', { style: 'font-size:.8rem; color:var(--text-3); margin-top:8px',
    text: 'Splits the image into independent regions — each with its own prompt. Toggle ON to configure.' });
  fcCard.appendChild(fcExplain);

  // Config row (rows × cols + direction)
  const fcConfigRow = el('div', { style: 'display:none; align-items:center; gap:12px; flex-wrap:wrap; margin-top:10px' });
  fcCard.appendChild(fcConfigRow);

  function numInput(value, min, max, onChange) {
    const inp = el('input', { type: 'number', value: String(value), min: String(min), max: String(max), step: '1',
      style: 'width:52px; text-align:center; padding:4px 6px' });
    inp.addEventListener('change', () => { const v = Math.max(min, Math.min(max, parseInt(inp.value) || min)); inp.value = v; onChange(v); });
    return inp;
  }

  fcConfigRow.appendChild(el('label', { text: 'Rows', style: 'font-size:.8rem; color:var(--text-3)' }));
  const fcRowsIn = numInput(fcRows, 1, 4, v => { fcRows = v; renderFCGrid(); });
  fcConfigRow.appendChild(fcRowsIn);
  fcConfigRow.appendChild(el('span', { text: '×', style: 'font-size:.9rem; color:var(--text-3)' }));
  fcConfigRow.appendChild(el('label', { text: 'Cols', style: 'font-size:.8rem; color:var(--text-3)' }));
  const fcColsIn = numInput(fcCols, 1, 4, v => { fcCols = v; renderFCGrid(); });
  fcConfigRow.appendChild(fcColsIn);

  fcConfigRow.appendChild(el('div', { style: 'width:1px; height:18px; background:var(--border); flex-shrink:0' }));
  fcConfigRow.appendChild(el('label', { text: 'Direction', style: 'font-size:.8rem; color:var(--text-3)' }));

  function dirBtn(label, dir) {
    const b = el('button', { class: `btn btn-sm ${dir === fcDirection ? 'active' : ''}`, text: label,
      style: dir === fcDirection ? 'border-color:var(--accent); color:var(--accent)' : '',
      onclick() {
        fcDirection = dir;
        [hBtn, vBtn].forEach(x => { x.style.borderColor = ''; x.style.color = ''; });
        b.style.borderColor = 'var(--accent)'; b.style.color = 'var(--accent)';
      },
    });
    return b;
  }
  const hBtn = dirBtn('Horizontal', 'Horizontal');
  const vBtn = dirBtn('Vertical', 'Vertical');
  fcConfigRow.appendChild(hBtn);
  fcConfigRow.appendChild(vBtn);

  fcConfigRow.appendChild(el('div', { style: 'width:1px; height:18px; background:var(--border); flex-shrink:0' }));
  fcConfigRow.appendChild(el('button', { class: 'btn btn-sm', text: 'Copy all (FC format)',
    onclick() {
      navigator.clipboard.writeText(buildFullPromptWithFC());
      toast('Copied in Forge Couple newline format', 'success');
    },
  }));

  // FC grid of textareas
  const fcGridWrap = el('div', { class: 'fc-grid-wrap', style: 'display:none' });
  fcCard.appendChild(fcGridWrap);
  let fcCells = []; // textareas

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
        const ta = el('textarea', { rows: '3', style: 'width:100%; resize:vertical; font-size:.8rem',
          placeholder: `${regionLabel} region...` });
        ta.addEventListener('focus', () => lastFocusedTA = ta);
        if (prevValues[idx]) ta.value = prevValues[idx];
        const cell = el('div', { class: 'fc-cell' }, [
          el('label', { text: regionLabel }),
          ta,
        ]);
        fcGridWrap.appendChild(cell);
        fcCells[idx] = ta;
      }
    }
  }

  // Background weight
  const fcBgRow = el('div', { style: 'display:none; align-items:center; gap:10px; margin-top:8px; flex-wrap:wrap' });
  fcCard.appendChild(fcBgRow);
  fcBgRow.appendChild(el('label', { text: 'Background weight', style: 'font-size:.78rem; color:var(--text-3); white-space:nowrap' }));
  const fcBgLabel  = el('span', { text: '0.5', style: 'font-size:.78rem; color:var(--accent); min-width:26px' });
  const fcBgSlider = el('input', { type: 'range', min: '0.1', max: '1', step: '0.05', value: '0.5', style: 'flex:1; min-width:120px' });
  fcBgSlider.addEventListener('input', () => { fcBgLabel.textContent = fcBgSlider.value; });
  fcBgRow.appendChild(fcBgSlider);
  fcBgRow.appendChild(fcBgLabel);

  // Prompt preview
  let previewOpen = false;
  const previewToggle = el('button', { class: 'btn btn-sm', text: '\u25b6 Preview constructed prompt', style: 'margin-top:6px; font-size:.75rem; opacity:.7',
    onclick() {
      previewOpen = !previewOpen;
      previewWrap.style.display = previewOpen ? '' : 'none';
      previewToggle.textContent = (previewOpen ? '\u25bc' : '\u25b6') + ' Preview constructed prompt';
      previewToggle.style.opacity = previewOpen ? '1' : '.7';
      if (previewOpen) updatePreview();
    }
  });
  leftCol.appendChild(previewToggle);

  const previewWrap = el('div', { class: 'card', style: 'display:none' });
  leftCol.appendChild(previewWrap);
  const previewNote = el('span', { style: 'font-size:.72rem; color:var(--text-3)' });
  previewWrap.appendChild(el('div', { style: 'display:flex; justify-content:space-between; align-items:baseline; margin-bottom:8px' }, [
    el('strong', { text: 'What Forge will receive', style: 'font-size:.82rem' }),
    previewNote,
  ]));
  const previewTA = el('textarea', { rows: '7', style: 'width:100%; font-family:monospace; font-size:.76rem; color:var(--text-2); background:var(--surface-2); resize:vertical' });
  previewTA.readOnly = true;
  previewWrap.appendChild(previewTA);

  // ════════════════════════════════════════════════════════════════════════════
  // RIGHT COLUMN: Settings + Generate + Chat
  // ════════════════════════════════════════════════════════════════════════════
  const rightCol = el('div', { style: 'display:flex; flex-direction:column; gap:14px;' });
  mainGrid.appendChild(rightCol);

  // ── Settings card ─────────────────────────────────────────────────────────
  const settingsCard = el('div', { class: 'card' });
  rightCol.appendChild(settingsCard);
  settingsCard.appendChild(el('h3', { text: 'Settings', style: 'margin-bottom:12px' }));

  const samplerSel = el('select', { style: 'width:100%' });
  const schedSel   = el('select', { style: 'width:100%' });
  settingsCard.appendChild(el('div', { class: 'settings-2col' }, [
    el('div', { class: 'form-group' }, [el('label', { text: 'Sampler' }), samplerSel]),
    el('div', { class: 'form-group' }, [el('label', { text: 'Scheduler' }), schedSel]),
  ]));

  const wIn = el('input', { type: 'number', value: '1440', min: '64', max: '5120', step: '8', style: 'width:100%' });
  const hIn = el('input', { type: 'number', value: '810',  min: '64', max: '5120', step: '8', style: 'width:100%' });
  settingsCard.appendChild(el('div', { class: 'settings-2col' }, [
    el('div', { class: 'form-group' }, [el('label', { text: 'Width' }),  wIn]),
    el('div', { class: 'form-group' }, [el('label', { text: 'Height' }), hIn]),
  ]));

  const presetsWrap = el('div', { style: 'display:flex; flex-wrap:wrap; gap:3px; margin-bottom:10px' });
  settingsCard.appendChild(presetsWrap);
  [{ l:'1:1',w:1024,h:1024},{ l:'4:3',w:1440,h:1080},{ l:'3:2',w:1440,h:960},
   { l:'16:9',w:1440,h:810},{ l:'9:16',w:810,h:1440},{ l:'1080p',w:1920,h:1080}].forEach(p => {
    presetsWrap.appendChild(el('button', { class: 'btn btn-sm', text: p.l,
      style: 'font-size:.68rem; padding:2px 7px; opacity:.8',
      onclick() { wIn.value = p.w; hIn.value = p.h; },
    }));
  });
  presetsWrap.appendChild(el('button', { class: 'btn btn-sm', text: 'W\u2194H',
    style: 'font-size:.68rem; padding:2px 7px; opacity:.8',
    onclick() { const t = wIn.value; wIn.value = hIn.value; hIn.value = t; },
  }));

  const stepsSlider = createSlider(settingsCard, { label: 'Steps', min: 1, max: 60, step: 1, value: 28 });
  const cfgSlider   = createSlider(settingsCard, { label: 'CFG Scale', min: 1, max: 20, step: 0.5, value: 7 });

  const seedIn = el('input', { type: 'number', value: '-1', style: 'flex:1' });
  settingsCard.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:flex-end; margin-bottom:8px' }, [
    el('div', { class: 'form-group', style: 'flex:1' }, [el('label', { text: 'Seed (-1 = random)' }), seedIn]),
    el('button', { class: 'btn btn-sm', text: 'Rnd', style: 'margin-bottom:8px', onclick() { seedIn.value = '-1'; } }),
  ]));

  const batchCntIn  = el('input', { type: 'number', value: '1', min: '1', max: '8', step: '1', style: 'width:100%' });
  const batchSizeIn = el('input', { type: 'number', value: '1', min: '1', max: '8', step: '1', style: 'width:100%' });
  settingsCard.appendChild(el('div', { class: 'settings-2col' }, [
    el('div', { class: 'form-group' }, [el('label', { text: 'Batch Count' }), batchCntIn]),
    el('div', { class: 'form-group' }, [el('label', { text: 'Batch Size' }), batchSizeIn]),
  ]));

  // Enhancements accordion (tabbed)
  const enhBody = el('div', { style: 'display:none; padding-top:12px; border-top:1px solid var(--border); margin-top:6px' });
  settingsCard.appendChild(el('button', { class: 'btn btn-sm', style: 'width:100%; text-align:left; margin-top:6px',
    text: '\u25b6 HiRes Fix  \u00b7  ADetailer  \u00b7  img2img',
    onclick(e) {
      const open = enhBody.style.display !== 'none';
      enhBody.style.display = open ? 'none' : '';
      e.target.textContent = (open ? '\u25b6' : '\u25bc') + ' HiRes Fix  \u00b7  ADetailer  \u00b7  img2img';
    },
  }));
  settingsCard.appendChild(enhBody);

  const enhTabs  = el('div', { class: 'enh-tabs' });
  const paneHR   = el('div', { class: 'enh-pane active' });
  const paneAD   = el('div', { class: 'enh-pane' });
  const paneI2I  = el('div', { class: 'enh-pane' });
  enhBody.appendChild(enhTabs);
  [paneHR, paneAD, paneI2I].forEach(p => enhBody.appendChild(p));
  ['HiRes Fix', 'ADetailer', 'img2img'].forEach((name, i) => {
    const tab = el('button', { class: `enh-tab${i===0?' active':''}`, text: name,
      onclick() {
        enhTabs.querySelectorAll('.enh-tab').forEach((t,j) => t.classList.toggle('active', j===i));
        [paneHR, paneAD, paneI2I].forEach((p,j) => p.classList.toggle('active', j===i));
      },
    });
    enhTabs.appendChild(tab);
  });

  const hrEnabled = el('input', { type: 'checkbox', id: 'sd-hr' });
  paneHR.appendChild(el('div', { style: 'display:flex; align-items:center; gap:6px; margin-bottom:8px' }, [hrEnabled, el('label', { for: 'sd-hr', text: 'Enable HiRes Fix', style: 'cursor:pointer; font-weight:600' })]));
  const hrScale   = createSlider(paneHR, { label: 'Scale',   min: 1, max: 4, step: 0.25, value: 2 });
  const hrSteps   = createSlider(paneHR, { label: 'Steps',   min: 0, max: 40, step: 1, value: 10 });
  const hrDenoise = createSlider(paneHR, { label: 'Denoise', min: 0.1, max: 1, step: 0.05, value: 0.35 });
  const hrUpSel   = el('select', { style: 'width:100%' });
  hrUpSel.appendChild(el('option', { value: 'ESRGAN_4x', text: 'ESRGAN_4x' }));
  paneHR.appendChild(el('div', { class: 'form-group' }, [el('label', { text: 'Upscaler' }), hrUpSel]));

  const adEnabled = el('input', { type: 'checkbox', id: 'sd-ad' });
  const adDetect  = el('select', { style: 'width:100%; margin-bottom:6px' });
  ['face_yolov8n.pt', 'hand_yolov8n.pt', 'person_yolov8n-seg.pt'].forEach(v => adDetect.appendChild(el('option', { value: v, text: v })));
  const adDenoise = createSlider(paneAD, { label: 'Fix strength', min: 0.1, max: 0.8, step: 0.05, value: 0.4 });
  const rfEnabled = el('input', { type: 'checkbox', id: 'sd-rf' });
  paneAD.appendChild(el('div', { style: 'display:flex; align-items:center; gap:6px; margin-bottom:8px' }, [adEnabled, el('label', { for: 'sd-ad', text: 'Enable ADetailer', style: 'cursor:pointer; font-weight:600' })]));
  paneAD.appendChild(el('div', { class: 'form-group' }, [el('label', { text: 'Detect' }), adDetect]));
  paneAD.appendChild(el('div', { style: 'display:flex; align-items:center; gap:6px; margin-top:8px' }, [rfEnabled, el('label', { for: 'sd-rf', text: 'Restore Faces (GFPGAN)', style: 'cursor:pointer' })]));

  const i2iPathIn   = el('input', { type: 'text', placeholder: 'Image path (auto-filled after generation)' });
  const i2iPromptIn = el('textarea', { rows: '2', placeholder: 'Prompt (blank = use positive prompt)' });
  const i2iDenoise  = createSlider(paneI2I, { label: 'Denoise strength', min: 0.05, max: 1, step: 0.05, value: 0.5 });
  paneI2I.appendChild(el('div', { class: 'form-group' }, [el('label', { text: 'Input Image Path' }), i2iPathIn]));
  paneI2I.appendChild(el('div', { class: 'form-group' }, [el('label', { text: 'Prompt' }), i2iPromptIn]));
  const i2iBtn = el('button', { class: 'btn btn-primary', text: 'Run img2img', style: 'margin-top:6px; width:100%' });
  paneI2I.appendChild(i2iBtn);

  // ── Generate button ───────────────────────────────────────────────────────
  const genRow = el('div', { style: 'display:flex; gap:8px' });
  rightCol.appendChild(genRow);
  const genBtn = el('button', { class: 'btn btn-primary btn-generate', text: 'Generate Image' });
  const stopBtn = el('button', { class: 'btn', text: '\u25a0 Stop', style: 'padding:14px 16px; font-size:1rem; flex-shrink:0',
    async onclick() { await api('/api/prompts/forge/interrupt', { method: 'POST' }).catch(() => {}); toast('Stop requested', 'info'); },
  });
  fcQuickToggle = el('button', { class: 'btn btn-sm', text: 'FC \u25cb',
    title: 'Toggle Forge Couple regional prompting (shortcut -- full controls below)',
    style: 'padding:14px 10px; font-size:.8rem; flex-shrink:0',
    onclick() { toggleFC(!fcEnabled); },
  });
  genRow.appendChild(genBtn);
  genRow.appendChild(fcQuickToggle);
  genRow.appendChild(stopBtn);
  const progressEl = el('div', { style: 'display:none; padding:8px 14px; background:var(--surface); border:1px solid var(--border); border-radius:var(--r-md); font-size:.85rem; color:var(--accent); text-align:center' });
  rightCol.appendChild(progressEl);

  // ── AI Chat card ──────────────────────────────────────────────────────────
  const chatCard = el('div', { class: 'card', style: 'display:flex; flex-direction:column; gap:0' });
  rightCol.appendChild(chatCard);

  chatCard.appendChild(el('div', { style: 'margin-bottom:8px' }, [
    el('h3', { text: '\u2736 Chat with AI', style: 'margin-bottom:4px' }),
    el('p', { style: 'font-size:.78rem; color:var(--text-3); line-height:1.5',
      text: 'Describe what you want and the AI will write or refine the prompts for you. Drop a reference image below to analyse it.' }),
  ]));

  // Reference image (small, in chat card)
  const refImgEl  = el('img', { style: 'max-height:70px; border-radius:4px; object-fit:cover' });
  const refWrap   = el('div', { style: 'display:none; align-items:center; gap:8px; margin-bottom:8px; padding:6px; background:var(--surface-2); border-radius:var(--r-sm)' }, [
    refImgEl,
    el('div', { style: 'flex:1; min-width:0' }, [
      el('div', { style: 'font-size:.75rem; color:var(--text-2)', id: 'ref-img-name' }),
      el('button', { class: 'btn btn-sm', text: 'Remove', style: 'margin-top:4px', onclick() { refImagePath = null; refWrap.style.display = 'none'; } }),
    ]),
  ]);
  chatCard.appendChild(refWrap);

  _applyHandoff = (path) => {
    refImagePath = path;
    refImgEl.src = `/output/${path.split(/[\\/]/).pop()}`;
    document.getElementById('ref-img-name').textContent = path.split(/[\\/]/).pop();
    refWrap.style.display = 'flex';
    addMsg('assistant', 'Image received. Tell me what kind of prompt you want for it, or just say "generate prompts" and I\'ll analyse it.');
  };
  if (_pendingHandoff) { _applyHandoff(_pendingHandoff); _pendingHandoff = null; }

  createDropZone(chatCard, {
    accept: 'image/*', multiple: false, label: 'Drop image to analyse (optional)',
    async onFiles(files) {
      try {
        const d = await apiUpload('/api/fun/upload', files);
        const f = d.files?.[0];
        if (f) {
          refImagePath = f.path;
          refImgEl.src = f.url || `/uploads/${f.name}`;
          document.getElementById('ref-img-name').textContent = f.name || f.path.split(/[\\/]/).pop();
          refWrap.style.display = 'flex';
          addMsg('assistant', `Got it — I can see ${f.name || 'the image'}. Tell me what you want, or ask me to "generate prompts for this image".`);
        }
      } catch (e) { toast(e.message, 'error'); }
    },
  });

  // AI model select (small, in chat card)
  const aiModelSel = el('select', { style: 'width:100%; margin-top:8px; font-size:.8rem' });
  aiModelSel.appendChild(el('option', { value: 'ollama', text: 'Loading...' }));
  chatCard.appendChild(el('div', { style: 'margin-top:6px' }, [el('label', { style: 'font-size:.72rem; color:var(--text-3)', text: 'AI Model' }), aiModelSel]));
  api('/api/prompts/models').then(d => {
    const ms = d.models?.length ? d.models : ['ollama'];
    aiModelSel.innerHTML = ms.map(m => `<option value="${m}">${m}</option>`).join('');
  }).catch(() => { aiModelSel.innerHTML = '<option value="ollama">ollama</option>'; });

  // Chat messages area
  const chatMessages = el('div', { class: 'chat-messages', style: 'min-height:260px; max-height:420px; margin-top:12px; margin-bottom:4px' });
  chatCard.appendChild(chatMessages);

  // Initial greeting
  addMsg('assistant', 'Hello! I\'m your prompt engineer. You can:\n• Describe what you want to generate\n• Ask me to analyze a reference image\n• Say things like "make it more cinematic" or "add fog"\n\nOr write your own prompt on the left and generate directly.');

  // Chat input
  const chatInput = el('textarea', { rows: '3', placeholder: 'e.g. "a gothic cathedral at dusk with a mysterious robed figure"', style: 'width:100%; resize:none' });
  const chatInputArea = el('div', { class: 'chat-input-area' });
  chatInputArea.appendChild(chatInput);
  const sendBtn = el('button', { class: 'btn btn-primary chat-send-btn', text: 'Send', onclick: sendMessage });
  chatInputArea.appendChild(sendBtn);
  chatCard.appendChild(chatInputArea);

  chatInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  function addMsg(role, text) {
    const div = el('div', { class: `chat-msg ${role}`, text });
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    return div;
  }

  function addThinking() {
    const div = el('div', { class: 'chat-msg assistant thinking', text: 'Thinking' });
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    return div;
  }

  async function sendMessage() {
    const msg = chatInput.value.trim();
    if (!msg) return;
    chatInput.value = '';
    sendBtn.disabled = true;
    addMsg('user', msg);
    const thinkEl = addThinking();

    try {
      let data;
      if (!sessionActive) {
        data = await api('/api/prompts/generate', {
          method: 'POST',
          body: JSON.stringify({
            concept: msg, image_path: refImagePath,
            model: aiModelSel.value, session_id: sessionId,
          }),
        });
        sessionActive = true;
      } else {
        data = await api('/api/prompts/refine', {
          method: 'POST',
          body: JSON.stringify({ feedback: msg, model: aiModelSel.value, session_id: sessionId }),
        });
      }
      thinkEl.remove();
      const reply = data.chat_reply || 'Prompts updated — check the fields on the left.';
      addMsg('assistant', reply);
      applyPrompts(data);
      if (data.create_wildcard?.name) {
        const wc = data.create_wildcard;
        try {
          await api('/api/prompts/wildcards/create', {
            method: 'POST',
            body: JSON.stringify({ name: wc.name, entries: wc.entries || [] }),
          });
          addMsg('assistant', `Wildcard __${wc.name}__ saved (${(wc.entries||[]).length} entries). Click \u201c__ Wildcards\u201d to use it.`);
          wcLoaded = false;
          loadWildcards();
        } catch (wcErr) {
          addMsg('assistant', `Couldn\u2019t save wildcard: ${wcErr.message}. Check that Wildcards directory is set in Settings.`);
        }
      }
    } catch (e) {
      thinkEl.remove();
      addMsg('assistant', `Error: ${e.message}`);
    }
    sendBtn.disabled = false;
    chatInput.focus();
  }

  // ════════════════════════════════════════════════════════════════════════════
  // OUTPUT (full width)
  // ════════════════════════════════════════════════════════════════════════════
  const outputCard = el('div', { class: 'card', style: 'display:none' });
  outer.appendChild(outputCard);

  const resultImg = el('img', { style: 'max-width:100%; border-radius:var(--r-sm); display:block; cursor:pointer', title: 'Click for full size' });
  resultImg.addEventListener('click', () => { if (resultImg.src) window.open(resultImg.src, '_blank'); });
  outputCard.appendChild(resultImg);

  const resultInfo = el('div', { style: 'font-size:.78rem; color:var(--text-2); margin-top:6px; font-family:monospace' });
  outputCard.appendChild(resultInfo);

  const actRow = el('div', { style: 'display:flex; gap:6px; flex-wrap:wrap; margin-top:8px' });
  outputCard.appendChild(actRow);
  actRow.appendChild(el('button', { class: 'btn btn-sm', text: 'Lock Seed', onclick() {
    if (generatedImages[currentIdx]) { seedIn.value = generatedImages[currentIdx].seed; toast('Seed locked', 'success'); }
  }}));
  actRow.appendChild(el('button', { class: 'btn btn-sm', text: 'Variation (seed+1)', onclick() {
    if (generatedImages[currentIdx]) { seedIn.value = generatedImages[currentIdx].seed + 1; genBtn.click(); }
  }}));
  actRow.appendChild(el('button', { class: 'btn btn-sm', text: 'img2img Iterate', onclick() {
    const img = generatedImages[currentIdx]; if (!img) return;
    i2iPathIn.value = img.path || '';
    enhTabs.querySelectorAll('.enh-tab').forEach((t,j) => t.classList.toggle('active', j===2));
    [paneHR, paneAD, paneI2I].forEach((p,j) => p.classList.toggle('active', j===2));
    settingsCard.querySelector('button').click(); // open enhancements
  }}));
  actRow.appendChild(el('button', { class: 'btn btn-sm', text: '\u2192 Make Videos', onclick() {
    const img = generatedImages[currentIdx]; if (!img?.path) return;
    handoff('fun-videos', { type: 'image', path: img.path });
    document.querySelector('[data-tab="fun-videos"]')?.click();
    toast('Image sent to Make Videos', 'info');
  }}));

  const navRow = el('div', { style: 'display:none; justify-content:center; align-items:center; gap:10px; margin-top:8px' });
  const navLabel = el('span', { style: 'font-size:.82rem; color:var(--text-2)' });
  navRow.appendChild(el('button', { class: 'btn btn-sm', text: '\u25c4 Prev', onclick() { showImage(currentIdx - 1); } }));
  navRow.appendChild(navLabel);
  navRow.appendChild(el('button', { class: 'btn btn-sm', text: 'Next \u25ba', onclick() { showImage(currentIdx + 1); } }));
  outputCard.appendChild(navRow);

  // ── Gallery (full width) ──────────────────────────────────────────────────
  const galleryCard = el('div', { class: 'card' });
  outer.appendChild(galleryCard);
  galleryCard.appendChild(el('h3', { style: 'margin-bottom:10px', text: 'Gallery' }));
  const galleryGrid  = el('div', { style: 'display:grid; grid-template-columns:repeat(auto-fill,minmax(90px,1fr)); gap:6px' });
  const galleryEmpty = el('p', { style: 'font-size:.82rem; color:var(--text-3)', text: 'Generated images appear here.' });
  galleryCard.appendChild(galleryEmpty);
  galleryCard.appendChild(galleryGrid);

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
    // Forge Couple: base prompt as background (first line), then each region newline-separated
    return [full, ...cellValues].join('\n');
  }

  function updatePreview() {
    if (!previewOpen) return;
    const prompt = buildFullPromptWithFC();
    previewNote.textContent = fcEnabled
      ? `Forge Couple ON \u2014 ${fcRows}\u00d7${fcCols} grid, ${fcDirection}`
      : 'Forge Couple OFF';
    previewTA.value = prompt;
  }

  function applyPrompts(data) {
    if (data.base_prompt) baseTA.value = data.base_prompt;
    const cols = data.columns || [];
    if (cols.length && fcEnabled && fcCells.length) {
      // Fill FC grid cells, expanding/contracting to fit
      fcCells.forEach((ta, i) => { if (ta) ta.value = cols[i] || ''; });
    }
    updatePreview();
  }

  function showImage(idx) {
    if (idx < 0 || idx >= generatedImages.length) return;
    currentIdx = idx;
    const img = generatedImages[idx];
    resultImg.src = img.src;
    resultInfo.textContent = `Seed: ${img.seed}  \u2502  ${img.info}`;
    outputCard.style.display = '';
    navRow.style.display = generatedImages.length > 1 ? 'flex' : 'none';
    navLabel.textContent = `${idx + 1} / ${generatedImages.length}`;
    galleryGrid.querySelectorAll('.sd-thumb').forEach((t, i) => { t.style.outline = i === idx ? '2px solid var(--accent)' : 'none'; });
  }

  function addToGallery(entry) {
    galleryEmpty.style.display = 'none';
    galleryGrid.appendChild(el('img', {
      class: 'sd-thumb',
      src: entry.src,
      style: 'width:100%; aspect-ratio:1; object-fit:cover; border-radius:var(--r-sm); cursor:pointer',
      title: `Seed: ${entry.seed}`,
      onclick() { showImage(generatedImages.indexOf(entry)); },
    }));
  }

  // ── Forge status ──────────────────────────────────────────────────────────
  let _retryTimer = null;
  async function checkForge() {
    try {
      forgeStatus = await api('/api/prompts/forge/status');
      if (forgeStatus.alive) {
        if (_retryTimer) { clearInterval(_retryTimer); _retryTimer = null; }
        forgeDot.className = 'dot running';
        forgeMsg.textContent = `Forge SD \u2014 ${forgeStatus.current_model || 'ready'}`;
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
        forgeMsg.textContent = 'Forge not running \u2014 start with --api flag';
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
    if (!prompt.trim()) { toast('Enter a prompt', 'error'); return; }

    genBtn.disabled = true; genBtn.textContent = 'Generating...';
    progressEl.style.display = ''; progressEl.textContent = 'Submitting...';
    progressTimer = setInterval(async () => {
      try { const p = await api('/api/prompts/forge/progress'); const pct = Math.round((p.progress||0)*100); progressEl.textContent = pct > 0 ? `Generating... ${pct}%` : 'Generating...'; } catch (_) {}
    }, 1000);

    try {
      const data = await api('/api/prompts/forge/txt2img', {
        method: 'POST',
        body: JSON.stringify({
          prompt:                 buildFullPrompt(),   // base+suffix only; backend joins FC columns
          columns:                fcEnabled ? fcCells.map(ta => ta?.value || '') : [],
          use_forge_couple:       fcEnabled,
          forge_couple_direction: fcDirection,
          forge_couple_bg_weight: parseFloat(fcBgSlider.value),
          forge_couple_background: 'First Line',
          negative_prompt:        negTA.value,
          steps:                  stepsSlider.value,
          cfg_scale:              cfgSlider.value,
          sampler:                samplerSel.value,
          scheduler:              schedSel.value,
          width:                  parseInt(wIn.value),
          height:                 parseInt(hIn.value),
          seed:                   parseInt(seedIn.value),
          n_iter:                 parseInt(batchCntIn.value),
          batch_size:             parseInt(batchSizeIn.value),
          enable_hr:              hrEnabled.checked,
          hr_scale:               hrScale.value,
          hr_upscaler:            hrUpSel.value || 'ESRGAN_4x',
          hr_second_pass_steps:   hrSteps.value,
          hr_denoising_strength:  hrDenoise.value,
          adetailer:              adEnabled.checked,
          adetailer_model:        adDetect.value,
          adetailer_denoise:      adDenoise.value,
          restore_faces:          rfEnabled.checked,
        }),
      });
      if (data.images?.length) {
        const info = `${samplerSel.value} \u2502 CFG ${cfgSlider.value} \u2502 ${stepsSlider.value} steps`;
        for (const b64 of data.images) {
          const entry = { src: `data:image/png;base64,${b64}`, seed: data.seed||−1, prompt, path: data.saved_paths?.[0]||null, info };
          generatedImages.push(entry); addToGallery(entry);
        }
        showImage(generatedImages.length - 1);
        toast(`Done! Seed: ${data.seed}`, 'success');
      }
    } catch (e) { toast(e.message, 'error'); }
    clearInterval(progressTimer);
    progressEl.style.display = 'none'; genBtn.disabled = false; genBtn.textContent = 'Generate Image';
  });

  [baseTA, negTA].forEach(ta => ta.addEventListener('keydown', e => { if (e.ctrlKey && e.key === 'Enter') { e.preventDefault(); genBtn.click(); } }));

  // ── img2img ───────────────────────────────────────────────────────────────
  i2iBtn.addEventListener('click', async () => {
    const path = i2iPathIn.value.trim(); if (!path) { toast('Image path required', 'error'); return; }
    i2iBtn.disabled = true; i2iBtn.textContent = 'Processing...';
    try {
      const res = await fetch(`/output/${path.split(/[\\/]/).pop()}`);
      const blob = await res.blob();
      const b64 = await new Promise(resolve => { const r = new FileReader(); r.onload = () => resolve(r.result.split(',')[1]); r.readAsDataURL(blob); });
      const data = await api('/api/prompts/forge/img2img', { method: 'POST', body: JSON.stringify({
        init_image: b64, prompt: i2iPromptIn.value.trim() || buildFullPromptWithFC(),
        negative_prompt: negTA.value, denoising_strength: i2iDenoise.value,
        steps: stepsSlider.value, cfg_scale: cfgSlider.value,
        sampler: samplerSel.value, scheduler: schedSel.value,
        width: parseInt(wIn.value), height: parseInt(hIn.value), seed: parseInt(seedIn.value),
        adetailer: adEnabled.checked, adetailer_model: adDetect.value,
      })});
      if (data.images?.length) {
        const entry = { src: `data:image/png;base64,${data.images[0]}`, seed: data.seed, prompt: i2iPromptIn.value, path: data.saved_paths?.[0]||null, info: `img2img \u2502 denoise ${i2iDenoise.value}` };
        generatedImages.push(entry); addToGallery(entry); showImage(generatedImages.length - 1);
        toast(`img2img done! Seed: ${data.seed}`, 'success');
      }
    } catch (e) { toast(e.message, 'error'); }
    i2iBtn.disabled = false; i2iBtn.textContent = 'Run img2img';
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

  // Also pass direction+background to the API call
  // (routes.py currently uses use_forge_couple + forge_couple_bg_weight;
  //  we pass direction via forge_couple_direction — need routes.py to forward it)

  loadDefaults();
  checkForge();
}
