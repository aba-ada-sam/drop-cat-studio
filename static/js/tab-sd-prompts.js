/**
 * Drop Cat Go Studio — Generate Images
 *
 * Unified tab: direct Forge interface + AI prompt composition + wildcard workshop.
 * Wildcard tokens (__token__) are expanded server-side before sending to Forge.
 */
import { api } from './api.js';
import { createSlider, el } from './components.js';
import { toast, apiFetch } from './shell/toast.js?v=20260421c';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260420k';
import { handoff } from './handoff.js?v=20260422a';
import { RegionEditor } from './components/region-editor.js';

// ── Module state ─────────────────────────────────────────────────────────────
let forgeStatus   = null;
let _retryTimer   = null;
let generatedImages = [];
let currentIdx    = -1;
let _progressTimer = null;
let _lastHandoffPath = null;

export function receiveHandoff(data) {
  if (data?.type === 'image' && data.path) _lastHandoffPath = data.path;
}

// ── Init ─────────────────────────────────────────────────────────────────────
export function init(panel) {
  panel.innerHTML = '';
  const root = el('div', { style: 'display:flex; flex-direction:column; height:100%; overflow-y:auto;' });
  panel.appendChild(root);

  // ── Top area: sidebar + result ───────────────────────────────────────────
  const topArea = el('div', { class: 'wide-layout', style: 'flex:1; min-height:0;' });
  root.appendChild(topArea);

  const sidebar  = el('div', { class: 'sidebar', style: 'display:flex; flex-direction:column; gap:10px;' });
  const mainArea = el('div', { class: 'main-area' });
  topArea.appendChild(sidebar);
  topArea.appendChild(mainArea);

  // ── Backend selector ─────────────────────────────────────────────────────
  let _backend = 'forge';  // 'forge' | 'openai'
  const backendBar = el('div', { style: 'display:flex; gap:2px; padding:3px; background:var(--bg-raised); border:1px solid var(--border-2); border-radius:6px; flex-shrink:0;' });
  const btnForge  = el('button', { class: 'provider-pill active', text: 'Forge SD (local)' });
  const btnOpenAI = el('button', { class: 'provider-pill', text: 'OpenAI  —  SFW only' });
  backendBar.appendChild(btnForge);
  backendBar.appendChild(btnOpenAI);
  sidebar.appendChild(backendBar);

  function _setBackend(b) {
    _backend = b;
    btnForge.classList.toggle('active', b === 'forge');
    btnOpenAI.classList.toggle('active', b === 'openai');
    forgeSettings.style.display  = b === 'forge'  ? '' : 'none';
    openaiSettings.style.display = b === 'openai' ? '' : 'none';
    if (b === 'openai') genBtn.disabled = false;
    else if (!forgeStatus?.alive) genBtn.disabled = true;
  }
  btnForge.addEventListener('click',  () => _setBackend('forge'));
  btnOpenAI.addEventListener('click', () => _setBackend('openai'));

  // ── Talk to me ────────────────────────────────────────────────────────────
  let _sdChatHistory = [];
  const sdTalkInput   = el('textarea', {
    rows: '2',
    style: 'width:100%; resize:vertical; font-size:.83rem;',
    placeholder: 'Describe what you\'re imagining — any words, feelings, references. AI builds the prompt.',
  });
  const sdTalkSendBtn = el('button', { class: 'btn btn-sm btn-primary', text: '→ Send' });
  const sdTalkReply   = el('div', { style: 'display:none; font-size:.75rem; color:var(--text-3); margin-top:5px; font-style:italic; line-height:1.5;' });

  async function _sdSend() {
    const msg = sdTalkInput.value.trim();
    if (!msg) return;
    sdTalkSendBtn.disabled = true; sdTalkSendBtn.textContent = '…';
    sdTalkReply.style.display = 'none';
    try {
      const body = {
        message:        msg,
        mode:           'sd_prompt',
        history:        _sdChatHistory.slice(-8),
        current_prompt: promptArea.value.trim(),
      };
      const data = await apiFetch('/api/fun/brainstorm', {
        method: 'POST', body: JSON.stringify(body), context: 'sd.brainstorm',
      });
      if (data.prompt) promptArea.value = data.prompt;
      if (data.reply) { sdTalkReply.textContent = `AI: ${data.reply}`; sdTalkReply.style.display = ''; }
      _sdChatHistory.push({ role: 'user', content: msg });
      _sdChatHistory.push({ role: 'assistant', content: data.reply || '' });
      sdTalkInput.value = '';
    } catch (e) { toast(e.message, 'error'); }
    finally { sdTalkSendBtn.disabled = false; sdTalkSendBtn.textContent = '→ Send'; }
  }
  sdTalkSendBtn.addEventListener('click', _sdSend);
  sdTalkInput.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _sdSend(); } });

  sidebar.appendChild(el('div', { class: 'card', style: 'padding:10px 12px; flex-shrink:0;' }, [
    el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-bottom:5px; text-transform:uppercase; letter-spacing:.05em;', text: 'Talk to me' }),
    sdTalkInput,
    el('div', { style: 'display:flex; justify-content:flex-end; margin-top:5px;' }, [sdTalkSendBtn]),
    sdTalkReply,
  ]));

  // ── Forge status ─────────────────────────────────────────────────────────
  const statusBar = el('div', { class: 'card', style: 'display:flex; align-items:center; gap:8px; padding:8px 12px; flex-shrink:0' });
  const forgeDot  = el('span', { class: 'dot' });
  const forgeMsg  = el('span', { style: 'font-size:.82rem; flex:1', text: 'Checking Forge…' });
  const modelSel  = el('select', { style: 'font-size:.8rem; max-width:180px; display:none' });
  statusBar.append(forgeDot, forgeMsg, modelSel);
  sidebar.appendChild(statusBar);  // moved into forgeSettings below after it's created

  // ── Prompt card ──────────────────────────────────────────────────────────
  const promptCard = el('div', { class: 'card', style: 'flex-shrink:0' });
  sidebar.appendChild(promptCard);

  const promptArea = el('textarea', {
    rows: '5',
    placeholder: 'Describe the image…\nUse __wildcard_name__ tokens for variety.\nOr type an idea and hit Compose.',
    style: 'width:100%; resize:vertical; font-size:.9rem',
  });
  promptCard.appendChild(promptArea);

  const negArea = el('textarea', {
    rows: '2',
    placeholder: 'Negative prompt (optional)',
    style: 'width:100%; resize:vertical; margin-top:6px; font-size:.82rem; color:var(--text-2)',
  });
  promptCard.appendChild(negArea);

  // Compose + suffix row
  const composeRow = el('div', { style: 'display:flex; gap:6px; margin-top:8px; align-items:center' });
  promptCard.appendChild(composeRow);

  const composeBtn = el('button', {
    class: 'btn btn-sm',
    style: 'font-size:.82rem',
    text: 'Compose with AI',
    title: 'AI reads your wildcard library, may create new wildcards, and fills the prompt with __tokens__',
  });
  const suffixInput = el('input', {
    type: 'text', placeholder: 'Style suffix…',
    style: 'flex:1; font-size:.8rem',
    value: '(depth blur)',
  });
  const wildcardToggle = el('input', { type: 'checkbox', id: 'sd-smart-wc', checked: true, style: 'cursor:pointer' });
  const wcLabel = el('label', { for: 'sd-smart-wc', text: 'Smart wildcards', title: 'AI creates new wildcard tokens as needed', style: 'cursor:pointer; font-size:.78rem; color:var(--text-3)' });
  composeRow.append(composeBtn, suffixInput, wildcardToggle, wcLabel);

  // ── Forge settings wrapper (status + all forge-specific controls) ─────────
  const forgeSettings = el('div', { style: 'display:flex; flex-direction:column; gap:10px;' });
  forgeSettings.appendChild(statusBar);  // move statusBar into forgeSettings
  sidebar.appendChild(forgeSettings);

  // ── OpenAI settings ───────────────────────────────────────────────────────
  const openaiSettings = el('div', { class: 'card', style: 'display:none; flex-shrink:0; padding:14px; display:none;' });
  sidebar.appendChild(openaiSettings);
  openaiSettings.appendChild(el('div', { style: 'font-size:.75rem; color:var(--text-3); margin-bottom:10px;', text: 'DALL-E 3 generates one image at a time. All content is SFW (OpenAI policy).' }));
  const oaiAspectSel = el('select', { style: 'width:100%; margin-bottom:8px; font-size:.85rem;' });
  [['1:1','Square (1024×1024)'],['16:9','Landscape (1792×1024)'],['9:16','Portrait (1024×1792)']].forEach(([v,t]) => {
    oaiAspectSel.appendChild(el('option', { value: v, text: t }));
  });
  openaiSettings.appendChild(el('div', {}, [
    el('label', { text: 'Aspect ratio', style: 'display:block; font-size:.75rem; color:var(--text-3); margin-bottom:4px;' }),
    oaiAspectSel,
  ]));
  const oaiQualSel = el('select', { style: 'width:100%; font-size:.85rem;' });
  [['standard','Standard'],['hd','HD (slower, sharper)']].forEach(([v,t]) => {
    oaiQualSel.appendChild(el('option', { value: v, text: t }));
  });
  openaiSettings.appendChild(el('div', {}, [
    el('label', { text: 'Quality', style: 'display:block; font-size:.75rem; color:var(--text-3); margin-bottom:4px;' }),
    oaiQualSel,
  ]));

  // ── Settings ─────────────────────────────────────────────────────────────
  const settingsBody = el('div', { class: 'card', style: 'flex-shrink:0' });
  forgeSettings.appendChild(settingsBody);

  // Sampler + Scheduler
  const ssRow = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:8px' });
  settingsBody.appendChild(ssRow);
  const samplerSel = el('select', { style: 'width:100%; font-size:.82rem' });
  const schedulerSel = el('select', { style: 'width:100%; font-size:.82rem' });
  const samplerGrp = el('div', {}, [el('label', { text: 'Sampler', style: 'font-size:.75rem; color:var(--text-3)' }), samplerSel]);
  const schedulerGrp = el('div', {}, [el('label', { text: 'Scheduler', style: 'font-size:.75rem; color:var(--text-3)' }), schedulerSel]);
  ssRow.append(samplerGrp, schedulerGrp);

  // Resolution
  const resRow = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:6px' });
  settingsBody.appendChild(resRow);
  const widthInput  = el('input', { type: 'number', value: '1440', min: '256', max: '2048', step: '64', style: 'width:100%; font-size:.85rem' });
  const heightInput = el('input', { type: 'number', value:  '810', min: '256', max: '2048', step: '64', style: 'width:100%; font-size:.85rem' });
  resRow.append(
    el('div', {}, [el('label', { text: 'Width',  style: 'font-size:.75rem; color:var(--text-3)' }), widthInput]),
    el('div', {}, [el('label', { text: 'Height', style: 'font-size:.75rem; color:var(--text-3)' }), heightInput]),
  );

  // Quick res presets
  const presetRow = el('div', { style: 'display:flex; gap:4px; margin-bottom:8px; flex-wrap:wrap' });
  settingsBody.appendChild(presetRow);
  for (const p of [
    { label: '1:1', w: 1024, h: 1024 }, { label: '16:9', w: 1440, h: 810 },
    { label: '9:16', w: 810, h: 1440 }, { label: '4:3',  w: 1440, h: 1080 },
    { label: '3:2',  w: 1440, h: 960 }, { label: '↔',    w: null,  h: null },
  ]) {
    presetRow.appendChild(el('button', {
      class: 'btn btn-sm', text: p.label, style: 'font-size:.72rem; padding:2px 6px',
      onclick() {
        if (p.label === '↔') { const tmp = widthInput.value; widthInput.value = heightInput.value; heightInput.value = tmp; }
        else { widthInput.value = p.w; heightInput.value = p.h; }
      },
    }));
  }

  // Steps / CFG / Seed
  const stepsSlider = createSlider(settingsBody, { label: 'Steps',     min: 1,  max: 60,  step: 1,   value: 28 });
  const cfgSlider   = createSlider(settingsBody, { label: 'CFG Scale', min: 1,  max: 20,  step: 0.5, value: 7  });
  const seedRow     = el('div', { style: 'display:flex; gap:6px; align-items:flex-end; margin-top:4px' });
  const seedInput   = el('input', { type: 'number', value: '-1', style: 'flex:1; font-size:.85rem' });
  seedRow.append(
    el('div', { style: 'flex:1' }, [
      el('label', { text: 'Seed', style: 'font-size:.75rem; color:var(--text-3); display:block' }),
      seedInput,
    ]),
    el('button', { class: 'btn btn-sm', text: 'Random', style: 'margin-bottom:2px', onclick() { seedInput.value = '-1'; } }),
  );
  settingsBody.appendChild(seedRow);

  // HiRes Fix
  const hrDet  = el('details', { style: 'margin-top:8px' });
  const hrSumm = el('summary', { style: 'cursor:pointer; font-size:.8rem; color:var(--text-3)', text: 'HiRes Fix' });
  hrDet.appendChild(hrSumm);
  const hrBody = el('div', { style: 'margin-top:6px' });
  hrDet.appendChild(hrBody);
  settingsBody.appendChild(hrDet);

  const hrEnabled  = el('input', { type: 'checkbox', id: 'sd-hr-enable' });
  hrBody.appendChild(el('div', { style: 'display:flex; align-items:center; gap:6px; margin-bottom:4px' }, [
    hrEnabled, el('label', { for: 'sd-hr-enable', text: 'Enable HiRes Fix', style: 'cursor:pointer; font-size:.85rem' }),
  ]));
  const hrScaleSlider   = createSlider(hrBody, { label: 'Scale',   min: 1.0, max: 2.0, step: 0.1,  value: 1.5 });
  const hrStepsSlider   = createSlider(hrBody, { label: 'Steps',   min: 0,   max: 40,  step: 1,    value: 15  });
  const hrDenoiseSlider = createSlider(hrBody, { label: 'Denoise', min: 0.1, max: 1.0, step: 0.05, value: 0.5 });
  const hrUpscalerSel   = el('select', { style: 'width:100%; font-size:.82rem; margin-top:4px' });
  hrUpscalerSel.appendChild(el('option', { value: 'ESRGAN_4x', text: 'ESRGAN_4x' }));
  hrBody.appendChild(hrUpscalerSel);

  // ── ADetailer ────────────────────────────────────────────────────────────
  const adDet  = el('details', { style: 'margin-top:8px' });
  const adSumm = el('summary', { style: 'cursor:pointer; font-size:.8rem; color:var(--text-3)', text: 'ADetailer (face / hand fix)' });
  adDet.appendChild(adSumm);
  const adBody = el('div', { style: 'margin-top:6px; display:flex; flex-direction:column; gap:6px' });
  adDet.appendChild(adBody);
  settingsBody.appendChild(adDet);

  const adEnabledRow = el('div', { style: 'display:flex; align-items:center; gap:6px' });
  const adEnabled = el('input', { type: 'checkbox', id: 'sd-ad-enable' });
  adEnabledRow.append(adEnabled, el('label', { for: 'sd-ad-enable', text: 'Enable ADetailer', style: 'cursor:pointer; font-size:.85rem' }));
  adBody.appendChild(adEnabledRow);

  const adModelGrp = el('div', {});
  adModelGrp.appendChild(el('label', { text: 'Model', style: 'font-size:.75rem; color:var(--text-3); display:block' }));
  const adModelSel = el('select', { style: 'width:100%; font-size:.82rem' });
  for (const m of ['face_yolov8n.pt', 'face_yolov8s.pt', 'hand_yolov8n.pt', 'person_yolov8n-seg.pt', 'mediapipe_face_full', 'mediapipe_face_short', 'mediapipe_face_mesh'])
    adModelSel.appendChild(el('option', { value: m, text: m }));
  adModelGrp.appendChild(adModelSel);
  adBody.appendChild(adModelGrp);

  const adDenoiseSlider = createSlider(adBody, { label: 'Denoise', min: 0.1, max: 1.0, step: 0.05, value: 0.4 });
  const adConfidenceSlider = createSlider(adBody, { label: 'Confidence', min: 0.1, max: 1.0, step: 0.05, value: 0.3 });

  // ── Forge Couple (Regional Prompting) ────────────────────────────────────
  const fcDet  = el('details', { style: 'margin-top:8px' });
  const fcSumm = el('summary', { style: 'cursor:pointer; font-size:.8rem; color:var(--text-3)', text: 'Regional Prompting (Forge Couple)' });
  fcDet.appendChild(fcSumm);
  const fcBody = el('div', { style: 'margin-top:8px; display:flex; flex-direction:column; gap:8px' });
  fcDet.appendChild(fcBody);
  settingsBody.appendChild(fcDet);

  const fcEnabledRow = el('div', { style: 'display:flex; align-items:center; gap:6px' });
  const fcEnabled = el('input', { type: 'checkbox', id: 'sd-fc-enable' });
  fcEnabledRow.append(fcEnabled, el('label', { for: 'sd-fc-enable', text: 'Enable Forge Couple', style: 'cursor:pointer; font-size:.85rem' }));
  fcBody.appendChild(fcEnabledRow);

  // Direction + region count
  const fcCtrlRow = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:8px' });
  fcBody.appendChild(fcCtrlRow);

  const fcDirSel = el('select', { style: 'width:100%; font-size:.82rem' });
  for (const d of ['Horizontal', 'Vertical'])
    fcDirSel.appendChild(el('option', { value: d, text: d }));
  fcCtrlRow.append(
    el('div', {}, [el('label', { text: 'Direction', style: 'font-size:.75rem; color:var(--text-3); display:block' }), fcDirSel]),
  );

  const fcCountSel = el('select', { style: 'width:100%; font-size:.82rem' });
  for (const n of [2, 3, 4])
    fcCountSel.appendChild(el('option', { value: String(n), text: `${n} regions` }));
  fcCountSel.value = '3';
  fcCtrlRow.append(
    el('div', {}, [el('label', { text: 'Regions', style: 'font-size:.75rem; color:var(--text-3); display:block' }), fcCountSel]),
  );

  const fcBgWeight = createSlider(fcBody, { label: 'Background weight', min: 0.1, max: 1.0, step: 0.05, value: 0.5 });

  // Visual region editor
  const fcEditorWrap = el('div', { style: 'border:1px solid var(--border-2); border-radius:var(--r-sm); padding:8px; background:var(--bg)' });
  fcBody.appendChild(fcEditorWrap);
  const fcEditor = new RegionEditor(fcEditorWrap, { rows: 1, cols: 3, direction: 'Horizontal' });

  // Rebuild editor when direction/count changes
  function _rebuildFcEditor() {
    const n = parseInt(fcCountSel.value);
    const dir = fcDirSel.value;
    fcEditor.setFromGrid(dir === 'Vertical' ? n : 1, dir === 'Horizontal' ? n : 1, dir);
  }
  fcDirSel.addEventListener('change', _rebuildFcEditor);
  fcCountSel.addEventListener('change', _rebuildFcEditor);

  // ── Generate button row ──────────────────────────────────────────────────
  // ── Loop state ────────────────────────────────────────────────────────────
  let _sdLooping = false;
  let _sdLoopCount = 0;

  const genRow  = el('div', { style: 'display:flex; gap:8px; flex-shrink:0' });
  sidebar.appendChild(genRow);

  const genBtn  = el('button', {
    class: 'btn btn-primary',
    text: 'Generate',
    style: 'flex:1; font-size:1rem; padding:10px 0',
    disabled: true,
  });
  const sdLoopBtn = el('button', {
    class: 'btn',
    text: '∞',
    title: 'Generate forever — click again to stop',
    style: 'font-size:1rem; padding:10px 14px;',
  });
  const stopBtn = el('button', {
    class: 'btn btn-sm', text: 'Stop',
    style: 'font-size:.85rem; display:none',
    async onclick() {
      try { await api('/api/prompts/forge/interrupt', { method: 'POST' }); }
      catch (_) {}
    },
  });
  genRow.append(genBtn, sdLoopBtn, stopBtn);

  sdLoopBtn.addEventListener('click', () => {
    if (_sdLooping) {
      _sdLooping = false;
      _sdLoopCount = 0;
      sdLoopBtn.textContent = '∞';
      sdLoopBtn.classList.remove('btn-primary');
      toast('Loop stopped', 'info');
    } else {
      if (genBtn.disabled) { toast('Wait for the current generation to finish', 'info'); return; }
      _sdLooping = true;
      _sdLoopCount = 0;
      sdLoopBtn.textContent = '■';
      sdLoopBtn.classList.add('btn-primary');
      toast('Looping — click ■ to stop', 'info');
      genBtn.click();
    }
  });

  const progressMsg = el('div', { style: 'display:none; font-size:.8rem; color:var(--accent); text-align:center; padding:4px 0; flex-shrink:0' });
  sidebar.appendChild(progressMsg);

  // ── Result area ──────────────────────────────────────────────────────────
  const resultCard = el('div', { class: 'card', style: 'text-align:center; display:flex; flex-direction:column; align-items:center; gap:8px;' });
  mainArea.appendChild(resultCard);

  const resultImg = el('img', {
    style: 'max-width:100%; max-height:70vh; border-radius:var(--r-sm); display:none; cursor:pointer',
    title: 'Click to open full size',
  });
  resultImg.addEventListener('click', () => { if (resultImg.src) window.open(resultImg.src, '_blank'); });
  resultCard.appendChild(resultImg);

  const emptyMsg = el('div', { style: 'padding:40px 20px; color:var(--text-3); font-size:.9rem', text: 'Generated images appear here.' });
  resultCard.appendChild(emptyMsg);

  const resultInfo = el('div', { style: 'display:none; font-size:.78rem; color:var(--text-2)' });
  resultCard.appendChild(resultInfo);

  const actionRow = el('div', { style: 'display:none; flex-wrap:wrap; gap:6px; justify-content:center' });
  resultCard.appendChild(actionRow);

  const btnReuse = el('button', { class: 'btn btn-sm', text: 'Reuse seed', onclick() {
    const img = generatedImages[currentIdx];
    if (img) seedInput.value = img.seed;
  }});
  const btnVariation = el('button', { class: 'btn btn-sm', text: 'Variation (+1)', onclick() {
    const img = generatedImages[currentIdx];
    if (img) { seedInput.value = img.seed + 1; genBtn.click(); }
  }});
  const btnSendVideos = el('button', { class: 'btn btn-sm', text: '→ Make Videos', onclick() {
    const img = generatedImages[currentIdx];
    if (!img?.path) { toast('Generate an image first', 'error'); return; }
    handoff('fun-videos', { type: 'image', path: img.path });
    document.querySelector('[data-tab="fun-videos"]')?.click();
    toast('Image sent to Create Videos', 'info');
  }});
  actionRow.append(btnReuse, btnVariation, btnSendVideos);

  // Nav
  const navRow = el('div', { style: 'display:none; gap:12px; align-items:center' });
  const prevBtn = el('button', { class: 'btn btn-sm', text: '< Prev', onclick() { showImage(currentIdx - 1); } });
  const navLabel = el('span', { style: 'font-size:.8rem; color:var(--text-2)' });
  const nextBtn = el('button', { class: 'btn btn-sm', text: 'Next >', onclick() { showImage(currentIdx + 1); } });
  navRow.append(prevBtn, navLabel, nextBtn);
  resultCard.appendChild(navRow);

  // Session gallery (thumbnails)
  const thumbGrid = el('div', { style: 'display:grid; grid-template-columns:repeat(auto-fill,minmax(80px,1fr)); gap:6px; margin-top:8px' });
  mainArea.appendChild(thumbGrid);

  // ── Wildcard Workshop ────────────────────────────────────────────────────
  const wcSection = el('div', { style: 'flex-shrink:0; margin-top:8px' });
  root.appendChild(wcSection);

  const wcDet  = el('details', { style: 'border-top:1px solid var(--border); padding-top:8px' });
  const wcSumm = el('summary', {
    style: 'cursor:pointer; font-weight:600; font-size:.9rem; padding:8px 16px; list-style:none; display:flex; align-items:center; gap:8px; user-select:none',
    text: 'Wildcard Workshop',
  });
  wcDet.appendChild(wcSumm);
  wcSection.appendChild(wcDet);

  const wcBody = el('div', { style: 'padding:16px; display:grid; grid-template-columns:240px 1fr; gap:16px;' });
  wcDet.appendChild(wcBody);

  // File list
  const fileListCol = el('div');
  wcBody.appendChild(fileListCol);
  fileListCol.appendChild(el('div', { style: 'font-size:.78rem; color:var(--text-3); margin-bottom:6px', text: 'Wildcard files' }));
  const fileList = el('div', { style: 'max-height:240px; overflow-y:auto; display:flex; flex-direction:column; gap:2px' });
  fileListCol.appendChild(fileList);
  const refreshFilesBtn = el('button', { class: 'btn btn-sm', text: 'Refresh', style: 'margin-top:6px; width:100%; font-size:.78rem', onclick: loadWildcardFiles });
  fileListCol.appendChild(refreshFilesBtn);

  let wcFiles = [];
  let selectedWcFile = null;

  function renderWildcardFiles() {
    fileList.innerHTML = '';
    if (!wcFiles.length) {
      fileList.appendChild(el('div', { style: 'font-size:.8rem; color:var(--text-3); padding:8px 0', text: 'No wildcard files. Configure the directory in Settings.' }));
      return;
    }
    for (const f of wcFiles) {
      const item = el('button', {
        class: 'btn btn-sm',
        style: `width:100%; text-align:left; font-size:.78rem; justify-content:space-between; display:flex; gap:4px; ${selectedWcFile?.token === f.token ? 'border-color:var(--accent); color:var(--accent)' : ''}`,
        onclick() { selectedWcFile = f; renderWildcardFiles(); updateWcOpsPanel(); },
      }, [
        el('span', { text: f.token.replace(/__/g, ''), style: 'flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap' }),
        el('span', { text: String(f.count), style: 'color:var(--text-3); flex-shrink:0' }),
      ]);
      fileList.appendChild(item);
    }
  }

  async function loadWildcardFiles() {
    try {
      const data = await api('/api/prompts/wildcards');
      wcFiles = (data.files || []).filter(f => f.source === 'filesystem');
      renderWildcardFiles();
    } catch (e) { toast(e.message, 'error'); }
  }

  // Ops panel (right column)
  const opsCol = el('div');
  wcBody.appendChild(opsCol);

  // Tabs: Grow / Expand / Prune / Audit
  const opTabBar = el('div', { style: 'display:flex; gap:4px; margin-bottom:10px; border-bottom:1px solid var(--border); padding-bottom:8px' });
  opsCol.appendChild(opTabBar);

  const opContents = el('div');
  opsCol.appendChild(opContents);

  const OPS = ['Grow', 'Expand', 'Prune', 'Audit'];
  const opPanels = {};
  let activeOp = 'Grow';

  function switchOp(name) {
    activeOp = name;
    opTabBar.querySelectorAll('.wc-op-tab').forEach(b => b.classList.toggle('active', b.dataset.op === name));
    Object.entries(opPanels).forEach(([k, p]) => p.style.display = k === name ? '' : 'none');
  }

  for (const op of OPS) {
    const tb = el('button', { class: 'btn btn-sm wc-op-tab', text: op, style: 'font-size:.8rem', 'data-op': op });
    tb.addEventListener('click', () => switchOp(op));
    opTabBar.appendChild(tb);
    const panel = el('div', { style: `display:${op === 'Grow' ? '' : 'none'}` });
    opPanels[op] = panel;
    opContents.appendChild(panel);
  }

  // ── Grow panel ──
  const growPanel = opPanels['Grow'];

  const conceptInput = el('textarea', {
    rows: '3', placeholder: 'Describe the wildcard you want to create…\ne.g. "baroque architectural details" or "underwater lighting moods"',
    style: 'width:100%; font-size:.85rem; resize:vertical',
  });
  growPanel.appendChild(conceptInput);

  const growRow = el('div', { style: 'display:flex; gap:8px; margin-top:8px; align-items:center' });
  growPanel.appendChild(growRow);

  const nameInput = el('input', { type: 'text', placeholder: 'File name (optional — auto-derived)', style: 'flex:1; font-size:.82rem' });
  const countSlider = createSlider(growPanel, { label: 'Entries', min: 10, max: 80, step: 5, value: 30 });

  growRow.appendChild(nameInput);

  const growBtn = el('button', { class: 'btn btn-primary', text: 'Grow Wildcards', style: 'font-size:.85rem; width:100%; margin-top:8px' });
  growPanel.appendChild(growBtn);
  const growResult = el('div', { style: 'display:none; margin-top:8px; font-size:.8rem' });
  growPanel.appendChild(growResult);

  growBtn.addEventListener('click', async () => {
    const concept = conceptInput.value.trim();
    if (!concept) { toast('Describe a concept first', 'error'); return; }
    growBtn.disabled = true;
    growBtn.textContent = 'Growing…';
    growResult.style.display = 'none';
    try {
      const data = await api('/api/prompts/wildcards/grow', {
        method: 'POST',
        body: JSON.stringify({ concept, name: nameInput.value.trim() || undefined, count: Number(countSlider.value) }),
      });
      growResult.style.display = '';
      growResult.innerHTML = `<span style="color:var(--green)">Created <strong>${data.token}</strong> with ${data.count} entries (+${data.added} new).</span>
        <div style="margin-top:4px; color:var(--text-3); max-height:120px; overflow-y:auto; white-space:pre-wrap">${(data.entries || []).slice(0,10).join('\n')}${data.count > 10 ? `\n… and ${data.count - 10} more` : ''}</div>`;
      nameInput.value = '';
      conceptInput.value = '';
      await loadWildcardFiles();
    } catch (e) { toast(e.message, 'error'); }
    growBtn.disabled = false;
    growBtn.textContent = 'Grow Wildcards';
  });

  // ── Expand panel ──
  const expandPanel = opPanels['Expand'];
  expandPanel.appendChild(el('div', { style: 'font-size:.82rem; color:var(--text-2); margin-bottom:6px', text: 'Select a file on the left, then expand it.' }));
  const expandCount = createSlider(expandPanel, { label: 'New entries', min: 5, max: 50, step: 5, value: 20 });
  const expandBtn = el('button', { class: 'btn btn-primary', text: 'Expand', style: 'width:100%; margin-top:6px' });
  expandPanel.appendChild(expandBtn);
  const expandResult = el('div', { style: 'display:none; margin-top:8px; font-size:.8rem' });
  expandPanel.appendChild(expandResult);

  expandBtn.addEventListener('click', async () => {
    if (!selectedWcFile?.path) { toast('Select a wildcard file first', 'error'); return; }
    expandBtn.disabled = true;
    expandBtn.textContent = 'Expanding…';
    try {
      const data = await api('/api/prompts/expand', {
        method: 'POST',
        body: JSON.stringify({ path: selectedWcFile.path, count: Number(expandCount.value), apply: true }),
      });
      expandResult.style.display = '';
      expandResult.innerHTML = `<span style="color:var(--green)">${data.count} new entries added.</span>
        <div style="margin-top:4px; color:var(--text-3); max-height:120px; overflow-y:auto; white-space:pre-wrap">${(data.new_entries || []).join('\n')}</div>`;
      await loadWildcardFiles();
    } catch (e) { toast(e.message, 'error'); }
    expandBtn.disabled = false;
    expandBtn.textContent = 'Expand';
  });

  // ── Prune panel ──
  const prunePanel = opPanels['Prune'];
  prunePanel.appendChild(el('div', { style: 'font-size:.82rem; color:var(--text-2); margin-bottom:6px', text: 'Select a file on the left, then prune it.' }));
  const pruneLevel = createSlider(prunePanel, { label: 'Aggressiveness', min: 1, max: 5, step: 1, value: 3 });
  const pruneBtn = el('button', { class: 'btn btn-primary', text: 'Prune (preview)', style: 'width:100%; margin-top:6px' });
  const pruneApplyBtn = el('button', { class: 'btn btn-sm', text: 'Apply changes', style: 'width:100%; margin-top:4px; display:none' });
  prunePanel.append(pruneBtn, pruneApplyBtn);
  const pruneResult = el('div', { style: 'display:none; margin-top:8px; font-size:.8rem' });
  prunePanel.appendChild(pruneResult);
  let _lastPruneData = null;

  pruneBtn.addEventListener('click', async () => {
    if (!selectedWcFile?.path) { toast('Select a wildcard file first', 'error'); return; }
    pruneBtn.disabled = true;
    pruneBtn.textContent = 'Pruning…';
    pruneApplyBtn.style.display = 'none';
    try {
      _lastPruneData = await api('/api/prompts/prune', {
        method: 'POST',
        body: JSON.stringify({ path: selectedWcFile.path, level: Number(pruneLevel.value), apply: false }),
      });
      pruneResult.style.display = '';
      pruneResult.innerHTML = `<span style="color:var(--green)">Keep: ${_lastPruneData.kept?.length || 0}</span> &nbsp; <span style="color:var(--red)">Remove: ${_lastPruneData.removed?.length || 0}</span>
        <pre style="font-size:.75rem; color:var(--text-3); max-height:100px; overflow:auto; white-space:pre-wrap; margin-top:4px">${_lastPruneData.notes || ''}</pre>`;
      pruneApplyBtn.style.display = '';
    } catch (e) { toast(e.message, 'error'); }
    pruneBtn.disabled = false;
    pruneBtn.textContent = 'Prune (preview)';
  });

  pruneApplyBtn.addEventListener('click', async () => {
    if (!selectedWcFile?.path || !_lastPruneData?.kept) return;
    try {
      await api('/api/prompts/prune', {
        method: 'POST',
        body: JSON.stringify({ path: selectedWcFile.path, level: Number(pruneLevel.value), apply: true }),
      });
      toast('Prune applied', 'success');
      pruneApplyBtn.style.display = 'none';
      await loadWildcardFiles();
    } catch (e) { toast(e.message, 'error'); }
  });

  // ── Audit panel ──
  const auditPanel = opPanels['Audit'];
  auditPanel.appendChild(el('div', { style: 'font-size:.82rem; color:var(--text-2); margin-bottom:6px', text: 'AI analysis of your entire wildcard library.' }));
  const auditBtn = el('button', { class: 'btn btn-primary', text: 'Audit Library', style: 'width:100%' });
  auditPanel.appendChild(auditBtn);
  const auditResult = el('div', { style: 'display:none; margin-top:8px; font-size:.8rem' });
  auditPanel.appendChild(auditResult);

  auditBtn.addEventListener('click', async () => {
    auditBtn.disabled = true;
    auditBtn.textContent = 'Auditing…';
    try {
      const data = await api('/api/prompts/audit', { method: 'POST', body: '{}' });
      auditResult.style.display = '';
      auditResult.innerHTML = `<pre style="font-size:.78rem; color:var(--text-2); white-space:pre-wrap; max-height:300px; overflow:auto">${data.report || '(no report)'}</pre>`;
    } catch (e) { toast(e.message, 'error'); }
    auditBtn.disabled = false;
    auditBtn.textContent = 'Audit Library';
  });

  switchOp('Grow');

  function updateWcOpsPanel() {
    // nothing to update per-file beyond re-rendering the file list
  }

  // ── Core functions ───────────────────────────────────────────────────────

  function showImage(idx) {
    if (idx < 0 || idx >= generatedImages.length) return;
    currentIdx = idx;
    const img = generatedImages[idx];
    resultImg.src = img.src;
    resultImg.style.display = '';
    emptyMsg.style.display  = 'none';
    topArea.classList.add('has-result');
    resultInfo.style.display = '';
    resultInfo.textContent   = `Seed: ${img.seed}`;
    actionRow.style.display  = 'flex';
    navRow.style.display     = generatedImages.length > 1 ? 'flex' : 'none';
    navLabel.textContent     = `${idx + 1} / ${generatedImages.length}`;
    prevBtn.disabled = idx <= 0;
    nextBtn.disabled = idx >= generatedImages.length - 1;
    thumbGrid.querySelectorAll('.sd-thumb').forEach((t, i) => {
      t.style.outline = i === idx ? '2px solid var(--accent)' : 'none';
    });
  }

  function addThumb(entry) {
    const thumb = el('img', {
      class: 'sd-thumb',
      src: entry.src,
      style: 'width:100%; aspect-ratio:1; object-fit:cover; border-radius:var(--r-sm); cursor:pointer',
      title: `Seed: ${entry.seed}`,
      onclick() { showImage(generatedImages.indexOf(entry)); },
    });
    thumbGrid.appendChild(thumb);
  }

  async function checkForge() {
    try {
      forgeStatus = await api('/api/prompts/forge/status');
      if (forgeStatus.alive) {
        if (_retryTimer) { clearInterval(_retryTimer); _retryTimer = null; }
        forgeDot.className   = 'dot running';
        forgeMsg.textContent = forgeStatus.current_model || 'Forge running';
        genBtn.disabled      = false;
        modelSel.style.display = '';

        // Populate selects
        modelSel.innerHTML = '';
        for (const m of forgeStatus.models || []) {
          const v = m.title || m.name;
          const opt = el('option', { value: v, text: v.length > 30 ? v.slice(0, 28) + '…' : v });
          if (v.includes(forgeStatus.current_model || '')) opt.selected = true;
          modelSel.appendChild(opt);
        }

        const fill = (sel, arr, def) => {
          sel.innerHTML = '';
          for (const s of arr || []) {
            const opt = el('option', { value: s, text: s });
            if (s === def) opt.selected = true;
            sel.appendChild(opt);
          }
        };
        fill(samplerSel, forgeStatus.samplers, forgeStatus.default_sampler || 'DPM++ 2M SDE');
        fill(schedulerSel, forgeStatus.schedulers, forgeStatus.default_scheduler || 'Karras');
        hrUpscalerSel.innerHTML = '';
        for (const u of forgeStatus.upscalers || ['ESRGAN_4x', 'Latent', 'None'])
          hrUpscalerSel.appendChild(el('option', { value: u, text: u }));

      } else {
        forgeDot.className   = 'dot not_configured';
        forgeMsg.textContent = 'Forge not running — start it with --api flag';
        genBtn.disabled      = true;
        modelSel.style.display = 'none';
        if (!_retryTimer) _retryTimer = setInterval(checkForge, 10000);
      }
    } catch (_) {
      forgeDot.className   = 'dot not_configured';
      forgeMsg.textContent = 'Forge not detected';
      genBtn.disabled      = true;
      if (!_retryTimer) _retryTimer = setInterval(checkForge, 10000);
    }
  }

  modelSel.addEventListener('change', async () => {
    try {
      await api('/api/prompts/forge/set-model', { method: 'POST', body: JSON.stringify({ model: modelSel.value }) });
      toast(`Loading: ${modelSel.value}`, 'info');
    } catch (e) { toast(e.message, 'error'); }
  });

  // ── AI Compose ───────────────────────────────────────────────────────────
  composeBtn.addEventListener('click', async () => {
    const idea = promptArea.value.trim();
    if (!idea) { toast('Type an idea in the prompt box first', 'error'); return; }
    composeBtn.disabled = true;
    composeBtn.textContent = 'Composing…';
    try {
      const fcOn = fcEnabled.checked;
      const data = await api('/api/prompts/enhance', {
        method: 'POST',
        body: JSON.stringify({
          idea,
          suffix: suffixInput.value.trim(),
          provider: 'local',
          smart_wildcards: wildcardToggle.checked,
          regional: fcOn,
          regions_n: fcOn ? parseInt(fcCountSel.value) : undefined,
        }),
      });
      if (data.prompt) promptArea.value = data.prompt;
      if (fcOn && Array.isArray(data.regions) && data.regions.length) {
        fcEditor.setRegionPrompts(data.regions);
        fcDet.open = true;
        fcEnabled.checked = true;
      }
      if (data.created_wildcards?.length) {
        toast(`Created ${data.created_wildcards.length} new wildcard file(s)`, 'success');
        await loadWildcardFiles();
      }
    } catch (e) { toast(e.message, 'error'); }
    composeBtn.disabled = false;
    composeBtn.textContent = 'Compose with AI';
  });

  // ── Generate ─────────────────────────────────────────────────────────────
  genBtn.addEventListener('click', async () => {
    const prompt = promptArea.value.trim();
    if (!prompt) { toast('Enter a prompt', 'error'); return; }

    // ── OpenAI path ───────────────────────────────────────────────────────
    if (_backend === 'openai') {
      genBtn.disabled = true;
      genBtn.innerHTML = '<span class="spinner"></span> Generating…';
      try {
        const data = await api('/api/prompts/openai/generate', {
          method: 'POST',
          body: JSON.stringify({ prompt, aspect: oaiAspectSel.value, quality: oaiQualSel.value }),
        });
        if (data.images?.length) {
          const img = data.images[0];
          const entry = { src: img.url, seed: 0, prompt, path: img.path };
          generatedImages.push(entry);
          addThumb(entry);
          if (img.path) pushToGallery('sd-prompts', img.path, prompt, 0, { model: 'dall-e-3' });
          topArea.classList.add('has-result');
          toast('Image generated', 'success');
        }
      } catch (e) { toast(e.message, 'error'); _sdLooping = false; sdLoopBtn.textContent = '∞'; sdLoopBtn.classList.remove('btn-primary'); }
      finally { genBtn.disabled = false; genBtn.textContent = 'Generate'; }
      if (_sdLooping) { _sdLoopCount++; setTimeout(() => genBtn.click(), 1500); }
      return;
    }

    if (!forgeStatus?.alive) { toast('Forge is not running', 'error'); return; }

    genBtn.disabled          = true;
    genBtn.innerHTML         = '<span class="spinner"></span> Generating…';
    stopBtn.style.display    = '';
    progressMsg.style.display = '';
    progressMsg.textContent  = 'Submitting…';

    _progressTimer = setInterval(async () => {
      try {
        const p = await api('/api/prompts/forge/progress');
        const pct = Math.round((p.progress || 0) * 100);
        progressMsg.textContent = pct > 0 ? `Generating… ${pct}%` : 'Generating…';
      } catch (_) {}
    }, 1000);

    try {
      const useFc = fcEnabled.checked;
      const fcRegions = useFc ? fcEditor.getRegions().map(r => r.prompt || '') : [];

      const data = await api('/api/prompts/forge/txt2img', {
        method: 'POST',
        body: JSON.stringify({
          prompt,
          negative_prompt: negArea.value,
          steps: Number(stepsSlider.value),
          cfg_scale: Number(cfgSlider.value),
          sampler: samplerSel.value,
          scheduler: schedulerSel.value,
          width: parseInt(widthInput.value),
          height: parseInt(heightInput.value),
          seed: parseInt(seedInput.value),
          enable_hr: hrEnabled.checked,
          hr_scale: Number(hrScaleSlider.value),
          hr_upscaler: hrUpscalerSel.value || 'ESRGAN_4x',
          hr_steps: Number(hrStepsSlider.value),
          hr_denoise: Number(hrDenoiseSlider.value),
          adetailer: adEnabled.checked,
          adetailer_model: adModelSel.value,
          adetailer_denoise: Number(adDenoiseSlider.value),
          adetailer_confidence: Number(adConfidenceSlider.value),
          use_forge_couple: useFc,
          columns: fcRegions,
          forge_couple_direction: fcDirSel.value,
          forge_couple_background: 'First Line',
          forge_couple_bg_weight: Number(fcBgWeight.value),
        }),
      });

      if (data.images?.length) {
        const savedPath = data.saved_paths?.[0] || null;
        const entry = { src: `data:image/png;base64,${data.images[0]}`, seed: data.seed || -1, prompt, path: savedPath };
        generatedImages.push(entry);
        addThumb(entry);
        if (savedPath) pushToGallery('sd-prompts', savedPath, prompt, data.seed, {
          model: modelSel.value, sampler: samplerSel.value, scheduler: schedulerSel.value,
          steps: Number(stepsSlider.value), cfg: Number(cfgSlider.value),
          width: parseInt(widthInput.value), height: parseInt(heightInput.value),
        });
        showImage(generatedImages.length - 1);
      }
    } catch (e) { toast(e.message, 'error'); _sdLooping = false; sdLoopBtn.textContent = '∞'; sdLoopBtn.classList.remove('btn-primary'); }

    clearInterval(_progressTimer);
    progressMsg.style.display = 'none';
    stopBtn.style.display     = 'none';
    genBtn.disabled           = false;
    genBtn.textContent        = 'Generate';
    if (_sdLooping) { _sdLoopCount++; setTimeout(() => genBtn.click(), 1500); }
  });

  promptArea.addEventListener('keydown', e => { if (e.ctrlKey && e.key === 'Enter') { e.preventDefault(); genBtn.click(); } });

  // ── Config defaults ──────────────────────────────────────────────────────
  async function loadDefaults() {
    try {
      const cfg = await api('/api/config');
      if (cfg.forge_default_width)  widthInput.value  = cfg.forge_default_width;
      if (cfg.forge_default_height) heightInput.value = cfg.forge_default_height;
      if (cfg.forge_default_steps)  stepsSlider.value = cfg.forge_default_steps;
      if (cfg.forge_default_cfg != null) cfgSlider.value = cfg.forge_default_cfg;
    } catch (_) {}
  }

  // Lazy-load wildcard files when workshop is first opened
  wcDet.addEventListener('toggle', () => { if (wcDet.open && !wcFiles.length) loadWildcardFiles(); }, { once: true });

  loadDefaults();
  checkForge();
}
