/**
 * Drop Cat Go Studio — SD Prompts tab.
 * Image → SD prompt generation with Forge integration for the full creative loop.
 * Generate prompts → Send to Forge → Get image → Feed to Fun Videos pipeline.
 */
import { api, apiUpload } from './api.js';
import { toast, createDropZone, createSlider, createSelect, el } from './components.js';

let sessionId = 'default';
let chatHistory = [];
let forgeStatus = null;

export function init(panel) {
  panel.innerHTML = '';
  const layout = el('div', { style: 'max-width:900px; margin:0 auto; display:flex; flex-direction:column; gap:16px' });
  panel.appendChild(layout);

  // ── Forge Status Banner ───────────────────────────────────────────────
  const forgeBanner = el('div', { class: 'card', style: 'padding:12px; display:flex; align-items:center; gap:10px' }, [
    el('span', { class: 'dot', id: 'forge-dot' }),
    el('span', { id: 'forge-msg', text: 'Checking Forge...' }),
  ]);
  layout.appendChild(forgeBanner);
  checkForge();

  // ── Image Input ───────────────────────────────────────────────────────
  const inputCard = el('div', { class: 'card' });
  layout.appendChild(inputCard);
  inputCard.appendChild(el('h3', { text: 'Reference Image + Concept' }));

  let imagePath = null;
  const previewWrap = el('div', { style: 'display:none; margin:8px 0' }, [
    el('img', { class: 'image-preview', id: 'sd-preview' }),
    el('button', { class: 'btn btn-sm', text: 'Remove', style: 'margin-left:10px', onclick() { imagePath = null; previewWrap.style.display = 'none'; } }),
  ]);
  inputCard.appendChild(previewWrap);

  createDropZone(inputCard, {
    accept: 'image/*',
    multiple: false,
    label: 'Drop a reference image (optional)',
    async onFiles(files) {
      try {
        const data = await apiUpload('/api/fun/upload', files);
        const f = data.files?.[0];
        if (f) {
          imagePath = f.path;
          previewWrap.style.display = 'flex';
          previewWrap.querySelector('img').src = f.url || `/uploads/${f.name}`;
        }
      } catch (e) { toast(e.message, 'error'); }
    },
  });

  const conceptInput = el('textarea', { rows: '2', placeholder: 'Describe your concept (e.g. "cyberpunk city at night")' });
  inputCard.appendChild(el('div', { class: 'form-group' }, [el('label', { text: 'Concept' }), conceptInput]));

  const extraInput = el('textarea', { rows: '2', placeholder: 'Extra instructions for the AI prompt engineer...' });
  inputCard.appendChild(el('div', { class: 'form-group' }, [el('label', { text: 'Extra Instructions' }), extraInput]));

  const modelSelect = createSelect(inputCard, {
    label: 'AI Model',
    options: ['Loading...'],
    value: 'Loading...',
  });
  // Populate with live Ollama models
  api('/api/prompts/models').then(data => {
    const sel = modelSelect.el.querySelector('select');
    const models = data.models?.length ? data.models : ['ollama'];
    sel.innerHTML = models.map(m => `<option value="${m}">${m}</option>`).join('');
  }).catch(() => {
    const sel = modelSelect.el.querySelector('select');
    sel.innerHTML = '<option value="ollama">ollama (default)</option>';
  });

  const genBtn = el('button', { class: 'btn btn-primary', text: 'Generate Prompts' });
  inputCard.appendChild(genBtn);

  // ── Prompt Output ─────────────────────────────────────────────────────
  const outputCard = el('div', { class: 'card', style: 'display:none' });
  layout.appendChild(outputCard);
  outputCard.appendChild(el('h3', { text: 'Generated Prompts' }));

  const basePromptEl = el('div', { class: 'prompt-output', id: 'sd-base-prompt' });
  outputCard.appendChild(el('div', { class: 'form-group' }, [
    el('div', { style: 'display:flex; justify-content:space-between; align-items:center' }, [
      el('label', { text: 'Base Prompt' }),
      el('button', { class: 'btn btn-sm', text: 'Copy', onclick() { copyText(basePromptEl.textContent); } }),
    ]),
    basePromptEl,
  ]));

  outputCard.appendChild(el('p', {
    style: 'font-size:.78rem; color:var(--text-2); margin-bottom:8px',
    text: 'These three columns define LEFT / CENTER / RIGHT regions when using Forge Couple. Edit them freely before generating.',
  }));

  const colContainer = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px' });
  // Use textareas so user can edit the AI output before generating
  const col1 = el('textarea', { class: 'prompt-output', rows: '4', style: 'resize:vertical' });
  const col2 = el('textarea', { class: 'prompt-output', rows: '4', style: 'resize:vertical' });
  const col3 = el('textarea', { class: 'prompt-output', rows: '4', style: 'resize:vertical' });
  colContainer.appendChild(el('div', { class: 'form-group' }, [
    el('div', { style: 'display:flex; justify-content:space-between' }, [
      el('label', { html: '◀ Left region' }),
      el('button', { class: 'btn btn-sm', text: 'Copy', onclick() { copyText(col1.value); } }),
    ]),
    col1,
  ]));
  colContainer.appendChild(el('div', { class: 'form-group' }, [
    el('div', { style: 'display:flex; justify-content:space-between' }, [
      el('label', { html: '● Center region' }),
      el('button', { class: 'btn btn-sm', text: 'Copy', onclick() { copyText(col2.value); } }),
    ]),
    col2,
  ]));
  colContainer.appendChild(el('div', { class: 'form-group' }, [
    el('div', { style: 'display:flex; justify-content:space-between' }, [
      el('label', { html: 'Right region ▶' }),
      el('button', { class: 'btn btn-sm', text: 'Copy', onclick() { copyText(col3.value); } }),
    ]),
    col3,
  ]));
  outputCard.appendChild(colContainer);

  const combineBtn = el('button', { class: 'btn', text: 'Copy All (with BREAK — for manual paste into Forge)', onclick() {
    const parts = [basePromptEl.textContent, col1.value, col2.value, col3.value].filter(x => x.trim());
    copyText(parts.join('\nBREAK\n'));
  }});
  outputCard.appendChild(combineBtn);

  // ── Forge Generate Panel ─────────────────────────────────────────────
  const forgeCard = el('div', { class: 'card', style: 'display:none' });
  layout.appendChild(forgeCard);

  forgeCard.appendChild(el('div', { style: 'display:flex; justify-content:space-between; align-items:center; margin-bottom:4px' }, [
    el('h3', { text: 'Generate Image in Forge' }),
    el('button', { class: 'btn btn-sm btn-cancel', text: '■ Stop', async onclick() {
      await api('/api/prompts/forge/interrupt', { method: 'POST' });
      toast('Generation interrupted', 'info');
    }}),
  ]));

  // ── Model (top — most important choice) ───────────────────────────────
  const modelSelectEl = el('select');
  forgeCard.appendChild(el('div', { class: 'form-group' }, [
    el('label', { text: 'Checkpoint Model' }),
    modelSelectEl,
  ]));

  // ── FORGE COUPLE — prominent, not hidden ──────────────────────────────
  // This is the regional prompting feature. We surface it front-and-center
  // because it directly drives the LEFT/CENTER/RIGHT columns above.
  const forgeCoupleSection = el('div', {
    style: 'border:2px solid var(--accent-border); border-radius:var(--r-md); padding:12px; margin-bottom:12px; background:var(--accent-bg)'
  });
  forgeCard.appendChild(forgeCoupleSection);

  const forgeCoupleHeader = el('div', { style: 'display:flex; align-items:center; gap:10px; margin-bottom:6px' });
  const forgeCoupleEnabledCb = el('input', { type: 'checkbox', id: 'forge-couple-enabled', checked: 'true' });
  forgeCoupleHeader.appendChild(forgeCoupleEnabledCb);
  forgeCoupleHeader.appendChild(el('div', {}, [
    el('strong', { text: 'Regional Prompting (Forge Couple)' }),
    el('span', { text: ' — ON by default', style: 'color:var(--accent); font-size:.8rem' }),
  ]));
  forgeCoupleSection.appendChild(forgeCoupleHeader);
  forgeCoupleSection.appendChild(el('p', {
    style: 'font-size:.78rem; color:var(--text-2); margin-bottom:8px; line-height:1.5',
    text: 'Splits your image into Left / Center / Right regions and applies a different prompt to each area. ' +
          'The LEFT, CENTER, RIGHT columns above each control one region. ' +
          'Disable this if you want a single unified prompt instead.',
  }));

  const forgeCoupleDetail = el('div');
  const forgeCouplebgWeight = createSlider(forgeCoupleDetail, {
    label: 'Background weight (how strongly the Base Prompt affects the whole image)',
    min: 0.1, max: 1, step: 0.1, value: 0.5,
  });
  forgeCoupleSection.appendChild(forgeCoupleDetail);
  forgeCoupleEnabledCb.addEventListener('change', () => {
    forgeCoupleDetail.style.opacity = forgeCoupleEnabledCb.checked ? '1' : '0.4';
  });

  // ── Dynamic prompts note ──────────────────────────────────────────────
  forgeCard.appendChild(el('div', {
    style: 'font-size:.78rem; color:var(--text-2); background:var(--surface-2); border-radius:var(--r-sm); padding:8px 12px; margin-bottom:10px; line-height:1.5',
    html: '<strong style="color:var(--text)">Dynamic Prompts are active.</strong> ' +
          'You can type <code style="color:var(--green)">__wildcard__</code> or ' +
          '<code style="color:var(--green)">{option1|option2|option3}</code> anywhere in your prompts — ' +
          'Forge will expand them randomly on each generation. ' +
          'Wildcards from your configured directory work too.',
  }));

  // ── Core generation settings ──────────────────────────────────────────
  const coreGrid = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:10px' });
  forgeCard.appendChild(coreGrid);

  const samplerSelectEl = el('select');
  const schedulerSelectEl = el('select');
  coreGrid.appendChild(el('div', { class: 'form-group' }, [el('label', { text: 'Sampler' }), samplerSelectEl]));
  coreGrid.appendChild(el('div', { class: 'form-group' }, [el('label', { text: 'Scheduler' }), schedulerSelectEl]));

  const cfgSlider = createSlider(coreGrid, { label: 'CFG Scale — how literally Forge follows the prompt (7 is balanced)', min: 1, max: 20, step: 0.5, value: 7 });
  const stepsSlider = createSlider(coreGrid, { label: 'Steps — more = better quality, slower (25 is good)', min: 4, max: 60, step: 1, value: 25 });

  const sizeGrid = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:10px' });
  forgeCard.appendChild(sizeGrid);
  const widthSelect = createSelect(sizeGrid, { label: 'Width', options: ['512', '768', '832', '1024', '1216', '1472'], value: '1024' });
  const heightSelect = createSelect(sizeGrid, { label: 'Height', options: ['512', '768', '832', '1024', '1216', '1472'], value: '1024' });

  const forgeNeg = el('textarea', { rows: '1', placeholder: 'Negative prompt — what to avoid...', value: 'blurry, low quality, deformed, ugly' });
  forgeCard.appendChild(el('div', { class: 'form-group' }, [el('label', { text: 'Negative Prompt' }), forgeNeg]));

  const seedRow = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-bottom:12px' }, [
    el('label', { text: 'Seed', style: 'font-size:.8rem; color:var(--text-2); min-width:40px' }),
    el('input', { type: 'number', id: 'forge-seed', value: '-1', style: 'width:150px; font-family:monospace' }),
    el('span', { text: '(-1 = random)', style: 'font-size:.75rem; color:var(--text-3)' }),
    el('button', { class: 'btn btn-sm', text: '🎲', onclick() { document.getElementById('forge-seed').value = '-1'; } }),
  ]);
  forgeCard.appendChild(seedRow);

  // ── Optional extensions (accordion — OK to hide these) ───────────────
  const extToggle = el('button', { class: 'btn btn-sm', text: '▶ Optional Enhancements', style: 'margin-bottom:8px', onclick() {
    extContent.style.display = extContent.style.display === 'none' ? '' : 'none';
    extToggle.textContent = extContent.style.display === 'none' ? '▶ Optional Enhancements' : '▼ Optional Enhancements';
  }});
  forgeCard.appendChild(extToggle);

  const extContent = el('div', { style: 'display:none; background:var(--surface-2); border-radius:var(--r-md); padding:14px; margin-bottom:12px' });
  forgeCard.appendChild(extContent);

  // ADetailer — faces/hands
  const adetailerEnabledCb = el('input', { type: 'checkbox', id: 'adetailer-enabled' });
  extContent.appendChild(el('div', { class: 'checkbox-group', style: 'margin-bottom:4px' }, [
    adetailerEnabledCb,
    el('label', { for: 'adetailer-enabled', html: '<strong>ADetailer</strong> — automatically fixes faces and hands after generation' }),
  ]));
  extContent.appendChild(el('p', { style: 'font-size:.75rem; color:var(--text-3); padding-left:24px; margin-bottom:8px',
    text: 'Only enable if your image contains faces or hands that look wrong.' }));
  const adetailerDetail = el('div', { style: 'padding-left:24px; display:none' });
  const adetailerDenoise = createSlider(adetailerDetail, { label: 'Fix Strength', min: 0.1, max: 0.8, step: 0.05, value: 0.4 });
  const adetailerModel = createSelect(adetailerDetail, { label: 'Detect', options: ['face_yolov8n.pt', 'hand_yolov8n.pt', 'person_yolov8n-seg.pt'], value: 'face_yolov8n.pt' });
  extContent.appendChild(adetailerDetail);
  adetailerEnabledCb.addEventListener('change', () => {
    adetailerDetail.style.display = adetailerEnabledCb.checked ? '' : 'none';
  });

  // HiRes Fix
  const hrEnabledCb = el('input', { type: 'checkbox', id: 'hires-enabled' });
  extContent.appendChild(el('div', { class: 'checkbox-group', style: 'margin-bottom:4px' }, [
    hrEnabledCb,
    el('label', { for: 'hires-enabled', html: '<strong>HiRes Fix</strong> — generate small then upscale (better detail, 2x slower)' }),
  ]));
  const hrDetail = el('div', { style: 'padding-left:24px; display:none' });
  const hrScale = createSlider(hrDetail, { label: 'Scale (2 = double the size)', min: 1.25, max: 4, step: 0.25, value: 2 });
  const hrDenoise = createSlider(hrDetail, { label: 'Creativity on upscale pass (0.3 = add detail, keep composition)', min: 0.1, max: 0.8, step: 0.05, value: 0.3 });
  const hrSteps = createSlider(hrDetail, { label: 'Extra steps for upscale pass', min: 0, max: 30, step: 1, value: 10 });
  const hrUpscalerSelect = el('select');
  hrDetail.appendChild(el('div', { class: 'form-group' }, [el('label', { text: 'Upscaler' }), hrUpscalerSelect]));
  extContent.appendChild(hrDetail);
  hrEnabledCb.addEventListener('change', () => {
    hrDetail.style.display = hrEnabledCb.checked ? '' : 'none';
  });

  const restoreFacesCb = el('input', { type: 'checkbox', id: 'restore-faces' });
  extContent.appendChild(el('div', { class: 'checkbox-group', style: 'margin-top:8px' }, [
    restoreFacesCb,
    el('label', { for: 'restore-faces', html: '<strong>Restore Faces</strong> — GFPGAN face correction (older method, try ADetailer first)' }),
  ]));

  // ── Generate button + result ──────────────────────────────────────────
  const forgeGenBtn = el('button', { class: 'btn btn-primary', text: 'Generate in Forge', style: 'font-size:1.05rem; padding:10px 28px' });
  forgeCard.appendChild(forgeGenBtn);

  const forgeProgress = el('div', { style: 'display:none; margin-top:10px; font-size:.85rem; color:var(--text-2)' });
  forgeCard.appendChild(forgeProgress);

  const forgeResult = el('div', { style: 'display:none; margin-top:12px' });
  const forgeImg = el('img', { style: 'max-width:100%; border-radius:var(--r-md); background:#000' });
  const seedDisplay = el('div', { style: 'font-size:.75rem; color:var(--text-3); margin-top:6px; font-family:monospace' });
  const forgeActions = el('div', { style: 'display:flex; gap:8px; margin-top:8px; flex-wrap:wrap' }, [
    el('button', { class: 'btn', text: 'Send to Fun Videos →', async onclick() {
      if (lastForgeImagePath) {
        window._dropcat_forge_image = lastForgeImagePath;
        document.querySelector('[data-tab="fun-videos"]')?.click();
        toast('Image queued for Fun Videos', 'success');
      }
    }}),
    el('button', { class: 'btn', text: 'Lock Seed 🔒', onclick() {
      const s = forgeResult.dataset.seed;
      if (s) document.getElementById('forge-seed').value = s;
    }}),
    el('button', { class: 'btn', text: 'img2img Iterate →', onclick() { switchToImg2Img(); } }),
  ]);
  forgeResult.appendChild(forgeImg);
  forgeResult.appendChild(seedDisplay);
  forgeResult.appendChild(forgeActions);
  forgeCard.appendChild(forgeResult);

  let lastForgeImagePath = null;
  let progressTimer = null;

  forgeGenBtn.addEventListener('click', async () => {
    const prompt = basePromptEl.textContent.trim();
    if (!prompt) { toast('Generate prompts first', 'error'); return; }

    forgeGenBtn.disabled = true;
    forgeGenBtn.textContent = 'Generating...';
    forgeProgress.style.display = '';
    forgeProgress.textContent = 'Submitting to Forge...';
    forgeResult.style.display = 'none';

    // Poll Forge's /progress endpoint while generating
    progressTimer = setInterval(async () => {
      try {
        const p = await api('/api/prompts/forge/progress');
        const pct = Math.round((p.progress || 0) * 100);
        forgeProgress.textContent = pct > 0 ? `Generating... ${pct}%` : 'Generating...';
      } catch (_) {}
    }, 1000);

    try {
      const data = await api('/api/prompts/forge/txt2img', {
        method: 'POST',
        body: JSON.stringify({
          // Pass base prompt + columns separately — backend handles joining
          prompt: prompt,
          columns: [col1.value, col2.value, col3.value],
          use_forge_couple: forgeCoupleEnabledCb.checked,
          forge_couple_bg_weight: forgeCouplebgWeight.value,
          // Core
          negative_prompt: forgeNeg.value,
          steps: stepsSlider.value,
          cfg_scale: cfgSlider.value,
          sampler: samplerSelectEl.value,
          scheduler: schedulerSelectEl.value,
          width: parseInt(widthSelect.value),
          height: parseInt(heightSelect.value),
          seed: parseInt(document.getElementById('forge-seed').value),
          // Extensions
          adetailer: adetailerEnabledCb.checked,
          adetailer_model: adetailerModel.value,
          adetailer_denoise: adetailerDenoise.value,
          enable_hr: hrEnabledCb.checked,
          hr_scale: hrScale.value,
          hr_upscaler: hrUpscalerSelect.value || 'ESRGAN_4x',
          hr_steps: hrSteps.value,
          hr_denoise: hrDenoise.value,
          restore_faces: restoreFacesCb.checked,
        }),
      });

      if (data.images?.length) {
        forgeImg.src = `data:image/png;base64,${data.images[0]}`;
        forgeResult.dataset.seed = data.seed;
        seedDisplay.textContent = `Seed: ${data.seed}  |  ${samplerSelectEl.value}  |  CFG ${cfgSlider.value}  |  Steps ${stepsSlider.value}`;
        lastForgeImagePath = data.saved_paths?.[0] || null;
        forgeResult.style.display = '';
        toast(`Image generated! Seed: ${data.seed}`, 'success');
      }
    } catch (e) { toast(e.message, 'error'); }

    clearInterval(progressTimer);
    forgeProgress.style.display = 'none';
    forgeGenBtn.disabled = false;
    forgeGenBtn.textContent = 'Generate in Forge';
  });

  function switchToImg2Img() {
    if (!lastForgeImagePath) return;
    // img2img panel toggle (inline below result)
    img2imgPanel.style.display = img2imgPanel.style.display === 'none' ? '' : 'none';
    if (img2imgPanel.style.display !== 'none') {
      img2imgPathEl.value = lastForgeImagePath;
      img2imgPromptEl.value = basePromptEl.textContent + (col1.value ? '\nBREAK\n' + col1.value : '');
    }
  }

  // ── img2img refinement (shown after generation) ───────────────────────
  const img2imgPanel = el('div', { class: 'card', style: 'display:none; margin-top:10px; background:var(--surface-2)' });
  forgeCard.appendChild(img2imgPanel);
  img2imgPanel.appendChild(el('h3', { text: 'Refine with img2img' }));

  const img2imgPathEl = el('input', { type: 'text', placeholder: 'Image path (auto-filled from last generation)' });
  img2imgPanel.appendChild(el('div', { class: 'form-group' }, [el('label', { text: 'Input Image Path' }), img2imgPathEl]));

  const img2imgPromptEl = el('textarea', { rows: '2', placeholder: 'Prompt for refinement...' });
  img2imgPanel.appendChild(el('div', { class: 'form-group' }, [el('label', { text: 'Prompt' }), img2imgPromptEl]));

  const img2imgDenoise = createSlider(img2imgPanel, { label: 'Denoising Strength', min: 0.05, max: 1, step: 0.05, value: 0.5 });
  const img2imgBtn = el('button', { class: 'btn btn-primary', text: 'Run img2img' });
  img2imgPanel.appendChild(img2imgBtn);

  img2imgBtn.addEventListener('click', async () => {
    const inputPath = img2imgPathEl.value.trim();
    if (!inputPath) { toast('Image path required', 'error'); return; }
    img2imgBtn.disabled = true;
    img2imgBtn.textContent = 'Processing...';
    try {
      // Load image as base64
      const imgRes = await fetch(`/output/${inputPath.split(/[\\/]/).pop()}`);
      const blob = await imgRes.blob();
      const b64 = await new Promise(resolve => {
        const r = new FileReader();
        r.onload = () => resolve(r.result.split(',')[1]);
        r.readAsDataURL(blob);
      });

      const data = await api('/api/prompts/forge/img2img', {
        method: 'POST',
        body: JSON.stringify({
          init_image: b64,
          prompt: img2imgPromptEl.value,
          negative_prompt: forgeNeg.value,
          denoising_strength: img2imgDenoise.value,
          steps: stepsSlider.value,
          cfg_scale: cfgSlider.value,
          sampler: samplerSelectEl.value,
          scheduler: schedulerSelectEl.value,
          width: parseInt(widthSelect.value),
          height: parseInt(heightSelect.value),
          seed: parseInt(document.getElementById('forge-seed').value),
          adetailer: adetailerEnabledCb.checked,
        }),
      });
      if (data.images?.length) {
        forgeImg.src = `data:image/png;base64,${data.images[0]}`;
        lastForgeImagePath = data.saved_paths?.[0] || null;
        forgeResult.style.display = '';
        seedDisplay.textContent = `Seed: ${data.seed}  |  img2img denoise ${img2imgDenoise.value}`;
        toast(`img2img done! Seed: ${data.seed}`, 'success');
      }
    } catch (e) { toast(e.message, 'error'); }
    img2imgBtn.disabled = false;
    img2imgBtn.textContent = 'Run img2img';
  });

  // ── Chat Refinement ───────────────────────────────────────────────────
  const chatCard = el('div', { class: 'card', style: 'display:none' });
  layout.appendChild(chatCard);
  chatCard.appendChild(el('h3', { text: 'Refine Prompts' }));

  const chatMessages = el('div', { class: 'chat-messages' });
  chatCard.appendChild(chatMessages);

  const feedbackInput = el('textarea', { rows: '2', placeholder: 'Tell the AI how to improve the prompts...' });
  chatCard.appendChild(feedbackInput);

  const refineBtn = el('button', { class: 'btn', text: 'Refine', style: 'margin-top:8px' });
  chatCard.appendChild(refineBtn);

  // ── Event handlers ────────────────────────────────────────────────────

  genBtn.addEventListener('click', async () => {
    genBtn.disabled = true;
    genBtn.textContent = 'Generating...';
    try {
      const data = await api('/api/prompts/generate', {
        method: 'POST',
        body: JSON.stringify({
          image_path: imagePath,
          concept: conceptInput.value,
          extra_instructions: extraInput.value,
          model: modelSelect.value,
          session_id: sessionId,
        }),
      });
      displayPrompts(data);
      outputCard.style.display = '';
      chatCard.style.display = '';
      if (forgeStatus?.alive) forgeCard.style.display = '';
      chatHistory = [];
      chatMessages.innerHTML = '';
    } catch (e) { toast(e.message, 'error'); }
    genBtn.disabled = false;
    genBtn.textContent = 'Generate Prompts';
  });

  refineBtn.addEventListener('click', async () => {
    const feedback = feedbackInput.value.trim();
    if (!feedback) return;
    refineBtn.disabled = true;

    chatHistory.push({ role: 'user', text: feedback });
    renderChat();
    feedbackInput.value = '';

    try {
      const data = await api('/api/prompts/refine', {
        method: 'POST',
        body: JSON.stringify({ feedback, session_id: sessionId, model: modelSelect.value }),
      });
      displayPrompts(data);
      chatHistory.push({ role: 'assistant', text: 'Prompts updated based on your feedback.' });
      renderChat();
    } catch (e) { toast(e.message, 'error'); }
    refineBtn.disabled = false;
  });

  function displayPrompts(data) {
    basePromptEl.textContent = data.base_prompt || '';
    const cols = data.columns || ['', '', ''];
    col1.value = cols[0] || '';
    col2.value = cols[1] || '';
    col3.value = cols[2] || '';
  }

  function renderChat() {
    chatMessages.innerHTML = '';
    for (const msg of chatHistory) {
      chatMessages.appendChild(el('div', { class: `chat-msg ${msg.role}`, text: msg.text }));
    }
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  function copyText(text) {
    navigator.clipboard.writeText(text).then(
      () => toast('Copied to clipboard', 'success'),
      () => toast('Copy failed', 'error'),
    );
  }

  async function checkForge() {
    try {
      forgeStatus = await api('/api/prompts/forge/status');
      const dot = document.getElementById('forge-dot');
      const msg = document.getElementById('forge-msg');

      if (forgeStatus.alive) {
        dot.className = 'dot running';
        msg.textContent = `Forge running — ${forgeStatus.models?.length || 0} models  |  current: ${forgeStatus.current_model || '?'}`;

        // Populate model dropdown
        modelSelectEl.innerHTML = '';
        for (const m of forgeStatus.models || []) {
          const opt = el('option', { value: m.title || m.name, text: m.title || m.name });
          if ((m.title || m.name).includes(forgeStatus.current_model || '')) opt.selected = true;
          modelSelectEl.appendChild(opt);
        }

        // Populate sampler dropdown
        samplerSelectEl.innerHTML = '';
        for (const s of forgeStatus.samplers || ['DPM++ 2M SDE', 'Euler', 'DDIM']) {
          const opt = el('option', { value: s, text: s });
          if (s === 'DPM++ 2M SDE') opt.selected = true;
          samplerSelectEl.appendChild(opt);
        }

        // Populate scheduler dropdown
        schedulerSelectEl.innerHTML = '';
        for (const s of forgeStatus.schedulers || ['Karras', 'Automatic']) {
          const opt = el('option', { value: s, text: s });
          if (s === 'Karras') opt.selected = true;
          schedulerSelectEl.appendChild(opt);
        }

        // Populate HiRes upscaler dropdown
        hrUpscalerSelect.innerHTML = '';
        for (const u of forgeStatus.upscalers || ['ESRGAN_4x', 'Latent', 'None']) {
          const opt = el('option', { value: u, text: u });
          if (u === 'ESRGAN_4x') opt.selected = true;
          hrUpscalerSelect.appendChild(opt);
        }

      } else {
        dot.className = 'dot not_configured';
        msg.textContent = 'Forge not running — start it with --api flag for image generation';
      }
    } catch (e) {
      forgeStatus = { alive: false };
    }
  }

  // Switch model when selected
  modelSelectEl.addEventListener('change', async () => {
    try {
      await api('/api/prompts/forge/set-model', {
        method: 'POST',
        body: JSON.stringify({ model: modelSelectEl.value }),
      });
      toast(`Loading model: ${modelSelectEl.value}`, 'info');
    } catch (e) { toast(e.message, 'error'); }
  });
}
