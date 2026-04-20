/**
 * Drop Cat Go Studio -- Image Generation tab.
 * Direct interface to Forge SD for txt2img and img2img.
 */
import { api } from './api.js';
import { toast, createSlider, createDropZone, el } from './components.js';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260419h';

let forgeStatus = null;
let _forgeRetryTimer = null;
let generatedImages = []; // { src, seed, prompt, path }
let currentIdx = -1;

export function init(panel) {
  panel.innerHTML = '';
  const layout = el('div', { class: 'wide-layout' });
  panel.appendChild(layout);

  const sidebar = el('div', { class: 'sidebar' });
  const mainArea = el('div', { class: 'main-area' });
  layout.appendChild(sidebar);
  layout.appendChild(mainArea);

  // ── Forge Status ─────────────────────────────────────────────────────
  const forgeBanner = el('div', { class: 'card', style: 'padding:10px; display:flex; align-items:center; gap:8px' });
  const forgeDot = el('span', { class: 'dot' });
  const forgeMsg = el('span', { style: 'font-size:.85rem', text: 'Checking Forge...' });
  forgeBanner.appendChild(forgeDot);
  forgeBanner.appendChild(forgeMsg);
  sidebar.appendChild(forgeBanner);

  // ── Prompt ───────────────────────────────────────────────────────────
  const promptCard = el('div', { class: 'step-card' });
  sidebar.appendChild(promptCard);
  promptCard.appendChild(el('h3', { text: 'Prompt' }));

  const promptArea = el('textarea', {
    rows: '5',
    placeholder: 'Describe the image you want to create...\ne.g. "a cyberpunk city at night, neon signs, rain-slicked streets, cinematic lighting"',
    style: 'width:100%; resize:vertical',
  });
  promptCard.appendChild(promptArea);

  const negArea = el('textarea', {
    rows: '2',
    placeholder: 'Negative prompt (optional)',
    style: 'width:100%; resize:vertical; margin-top:8px; font-size:.85rem',
  });
  negArea.value = 'blurry, low quality, watermark, text, logo, ugly, deformed';
  promptCard.appendChild(negArea);

  // ── Settings ─────────────────────────────────────────────────────────
  const settingsCard = el('div', { class: 'step-card' });
  sidebar.appendChild(settingsCard);
  settingsCard.appendChild(el('h3', { text: 'Settings' }));

  // Model
  const modelGroup = el('div', { class: 'form-group' });
  modelGroup.appendChild(el('label', { text: 'Model' }));
  const modelSelect = el('select', { style: 'width:100%' });
  modelSelect.appendChild(el('option', { text: 'Loading...', value: '' }));
  modelGroup.appendChild(modelSelect);
  settingsCard.appendChild(modelGroup);

  // Sampler + Scheduler on one row
  const ssRow = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:8px' });
  settingsCard.appendChild(ssRow);

  const samplerGroup = el('div', { class: 'form-group' });
  samplerGroup.appendChild(el('label', { text: 'Sampler' }));
  const samplerSelect = el('select', { style: 'width:100%' });
  samplerGroup.appendChild(samplerSelect);
  ssRow.appendChild(samplerGroup);

  const schedulerGroup = el('div', { class: 'form-group' });
  schedulerGroup.appendChild(el('label', { text: 'Scheduler' }));
  const schedulerSelect = el('select', { style: 'width:100%' });
  schedulerGroup.appendChild(schedulerSelect);
  ssRow.appendChild(schedulerGroup);

  // Resolution row
  const resRow = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:8px' });
  settingsCard.appendChild(resRow);

  const widthGroup = el('div', { class: 'form-group' });
  widthGroup.appendChild(el('label', { text: 'Width' }));
  const widthInput = el('input', { type: 'number', value: '1440', min: '256', max: '2048', step: '64', style: 'width:100%' });
  widthGroup.appendChild(widthInput);
  resRow.appendChild(widthGroup);

  const heightGroup = el('div', { class: 'form-group' });
  heightGroup.appendChild(el('label', { text: 'Height' }));
  const heightInput = el('input', { type: 'number', value: '810', min: '256', max: '2048', step: '64', style: 'width:100%' });
  heightGroup.appendChild(heightInput);
  resRow.appendChild(heightGroup);

  // Quick resolution presets
  const presetRow = el('div', { style: 'display:flex; gap:4px; margin-bottom:8px; flex-wrap:wrap' });
  const presets = [
    { label: '1:1', w: 1024, h: 1024 },
    { label: '3:2', w: 1440, h: 960 },
    { label: '2:3', w: 960, h: 1440 },
    { label: '16:9', w: 1440, h: 810 },
    { label: '9:16', w: 810, h: 1440 },
    { label: '4:3', w: 1440, h: 1080 },
  ];
  for (const p of presets) {
    presetRow.appendChild(el('button', {
      class: 'btn btn-sm', text: p.label,
      style: 'font-size:.72rem; padding:2px 8px',
      onclick() { widthInput.value = p.w; heightInput.value = p.h; },
    }));
  }
  settingsCard.appendChild(presetRow);

  // Sliders
  const stepsSlider = createSlider(settingsCard, { label: 'Steps', min: 1, max: 60, step: 1, value: 30 });
  const cfgSlider = createSlider(settingsCard, { label: 'CFG Scale', min: 1, max: 20, step: 0.5, value: 2.5 });

  // Seed
  const seedRow = el('div', { style: 'display:flex; gap:6px; align-items:end' });
  const seedGroup = el('div', { class: 'form-group', style: 'flex:1' });
  seedGroup.appendChild(el('label', { text: 'Seed' }));
  const seedInput = el('input', { type: 'number', value: '-1', style: 'width:100%' });
  seedGroup.appendChild(seedInput);
  seedRow.appendChild(seedGroup);
  seedRow.appendChild(el('button', {
    class: 'btn btn-sm', text: 'Random',
    style: 'margin-bottom:8px',
    onclick() { seedInput.value = '-1'; },
  }));
  settingsCard.appendChild(seedRow);

  // ── HiRes Fix (collapsible) ──────────────────────────────────────────
  const hrToggle = el('details', { style: 'margin-top:6px' });
  const hrSummary = el('summary', { style: 'cursor:pointer; font-size:.85rem; color:var(--text-2)', text: 'HiRes Fix' });
  hrToggle.appendChild(hrSummary);
  const hrBody = el('div', { style: 'margin-top:6px' });
  hrToggle.appendChild(hrBody);
  settingsCard.appendChild(hrToggle);

  const hrEnabled = el('input', { type: 'checkbox', id: 'ig-hr-enable' });
  hrBody.appendChild(el('div', { style: 'display:flex; align-items:center; gap:6px; margin-bottom:6px' }, [
    hrEnabled, el('label', { for: 'ig-hr-enable', text: 'Enable HiRes Fix', style: 'cursor:pointer' }),
  ]));
  const hrScaleSlider = createSlider(hrBody, { label: 'Scale', min: 1.0, max: 2.0, step: 0.1, value: 1.5 });
  const hrStepsSlider = createSlider(hrBody, { label: 'Steps', min: 0, max: 40, step: 1, value: 15 });
  const hrDenoiseSlider = createSlider(hrBody, { label: 'Denoise', min: 0.1, max: 1.0, step: 0.05, value: 0.5 });

  const hrUpscalerGroup = el('div', { class: 'form-group' });
  hrUpscalerGroup.appendChild(el('label', { text: 'Upscaler' }));
  const hrUpscalerSelect = el('select', { style: 'width:100%' });
  hrUpscalerSelect.appendChild(el('option', { value: 'ESRGAN_4x', text: 'ESRGAN_4x' }));
  hrUpscalerGroup.appendChild(hrUpscalerSelect);
  hrBody.appendChild(hrUpscalerGroup);

  // ── Generate Button ──────────────────────────────────────────────────
  const genBtn = el('button', {
    class: 'btn btn-primary',
    text: 'Generate Image',
    style: 'width:100%; font-size:1.1rem; padding:12px 0; margin-top:10px',
  });
  sidebar.appendChild(genBtn);

  const progressMsg = el('div', {
    style: 'display:none; text-align:center; padding:8px; font-size:.85rem; color:var(--accent)',
  });
  sidebar.appendChild(progressMsg);

  // ── Main Area: Image Display ─────────────────────────────────────────
  const resultCard = el('div', { class: 'card', style: 'text-align:center' });
  mainArea.appendChild(resultCard);

  const resultImg = el('img', {
    style: 'max-width:100%; border-radius:var(--r-sm); display:none; cursor:pointer',
    title: 'Click to open full size',
  });
  resultImg.addEventListener('click', () => {
    if (resultImg.src) window.open(resultImg.src, '_blank');
  });
  resultCard.appendChild(resultImg);

  const resultInfo = el('div', { style: 'display:none; margin-top:8px; font-size:.82rem; color:var(--text-2)' });
  resultCard.appendChild(resultInfo);

  const emptyMsg = el('div', {
    style: 'padding:30px 10px; color:var(--text-3); font-size:.9rem',
    text: 'Generated images will appear here.',
  });
  resultCard.appendChild(emptyMsg);

  // Action buttons below image
  const actionRow = el('div', { style: 'display:none; margin-top:10px; display:flex; gap:6px; justify-content:center; flex-wrap:wrap' });
  resultCard.appendChild(actionRow);

  const btnReuse = el('button', { class: 'btn btn-sm', text: 'Reuse seed', onclick() {
    if (generatedImages[currentIdx]) seedInput.value = generatedImages[currentIdx].seed;
  }});
  const btnVariation = el('button', { class: 'btn btn-sm', text: 'Variation (seed+1)', onclick() {
    if (generatedImages[currentIdx]) { seedInput.value = generatedImages[currentIdx].seed + 1; genBtn.click(); }
  }});
  const btnSendFun = el('button', { class: 'btn btn-sm', text: '-> Make Videos', onclick() {
    if (!generatedImages[currentIdx]?.path) return;
    import('./handoff.js').then(h => h.handoff('fun-videos', { type: 'image', path: generatedImages[currentIdx].path }));
    document.querySelector('[data-tab="fun-videos"]')?.click();
    toast('Image sent to Make Videos', 'info');
  }});
  const btnSendSD = el('button', { class: 'btn btn-sm', text: '-> SD Prompts', onclick() {
    if (!generatedImages[currentIdx]?.path) return;
    import('./handoff.js').then(h => h.handoff('sd-prompts', { type: 'image', path: generatedImages[currentIdx].path }));
    document.querySelector('[data-tab="sd-prompts"]')?.click();
    toast('Image sent to SD Prompts for analysis', 'info');
  }});
  actionRow.appendChild(btnReuse);
  actionRow.appendChild(btnVariation);
  actionRow.appendChild(btnSendSD);
  actionRow.appendChild(btnSendFun);

  // ── Gallery (thumbnails of past generations) ─────────────────────────
  const galleryCard = el('div', { class: 'card' });
  mainArea.appendChild(galleryCard);
  galleryCard.appendChild(el('h3', { style: 'margin-bottom:8px', text: 'Gallery' }));
  const galleryGrid = el('div', {
    style: 'display:grid; grid-template-columns:repeat(auto-fill, minmax(100px, 1fr)); gap:6px',
  });
  galleryCard.appendChild(galleryGrid);
  const galleryEmpty = el('div', { style: 'font-size:.82rem; color:var(--text-3); padding:6px 0', text: 'No images generated yet.' });
  galleryCard.appendChild(galleryEmpty);

  // Nav row
  const navRow = el('div', { style: 'display:none; margin-top:8px; display:flex; justify-content:center; align-items:center; gap:12px' });
  const prevBtn = el('button', { class: 'btn btn-sm', text: '< Prev', onclick() { showImage(currentIdx - 1); } });
  const navLabel = el('span', { style: 'font-size:.82rem; color:var(--text-2)' });
  const nextBtn = el('button', { class: 'btn btn-sm', text: 'Next >', onclick() { showImage(currentIdx + 1); } });
  navRow.appendChild(prevBtn);
  navRow.appendChild(navLabel);
  navRow.appendChild(nextBtn);
  resultCard.appendChild(navRow);

  // ── Functions ─────────────────────────────────────────────────────────

  function showImage(idx) {
    if (idx < 0 || idx >= generatedImages.length) return;
    currentIdx = idx;
    const img = generatedImages[idx];
    resultImg.src = img.src;
    resultImg.style.display = '';
    emptyMsg.style.display = 'none';
    resultInfo.style.display = '';
    resultInfo.textContent = `Seed: ${img.seed}  |  ${img.prompt.slice(0, 80)}${img.prompt.length > 80 ? '...' : ''}`;
    actionRow.style.display = 'flex';
    navRow.style.display = generatedImages.length > 1 ? 'flex' : 'none';
    navLabel.textContent = `${idx + 1} / ${generatedImages.length}`;
    prevBtn.disabled = idx <= 0;
    nextBtn.disabled = idx >= generatedImages.length - 1;

    // Highlight gallery thumbnail
    galleryGrid.querySelectorAll('.ig-thumb').forEach((t, i) => {
      t.style.outline = i === idx ? '2px solid var(--accent)' : 'none';
    });
  }

  function addToGallery(entry) {
    galleryEmpty.style.display = 'none';
    const thumb = el('img', {
      class: 'ig-thumb',
      src: entry.src,
      style: 'width:100%; aspect-ratio:1; object-fit:cover; border-radius:var(--r-sm); cursor:pointer',
      title: `Seed: ${entry.seed}`,
      onclick() { showImage(generatedImages.indexOf(entry)); },
    });
    galleryGrid.appendChild(thumb);
  }

  async function checkForge() {
    try {
      forgeStatus = await api('/api/prompts/forge/status');
      if (forgeStatus.alive) {
        if (_forgeRetryTimer) { clearInterval(_forgeRetryTimer); _forgeRetryTimer = null; }
        forgeDot.className = 'dot running';
        forgeMsg.textContent = `Forge running -- ${forgeStatus.current_model || '?'}`;
        genBtn.disabled = false;

        // Populate model dropdown
        modelSelect.innerHTML = '';
        for (const m of forgeStatus.models || []) {
          const opt = el('option', { value: m.title || m.name, text: m.title || m.name });
          if ((m.title || m.name).includes(forgeStatus.current_model || '')) opt.selected = true;
          modelSelect.appendChild(opt);
        }

        // Samplers -- default from config
        samplerSelect.innerHTML = '';
        const defaultSampler = forgeStatus.default_sampler || 'DPM++ 2M SDE';
        for (const s of forgeStatus.samplers || ['DPM++ 2M SDE', 'Euler', 'DDIM']) {
          const opt = el('option', { value: s, text: s });
          if (s === defaultSampler) opt.selected = true;
          samplerSelect.appendChild(opt);
        }

        // Schedulers -- default from config
        schedulerSelect.innerHTML = '';
        const defaultScheduler = forgeStatus.default_scheduler || 'Karras';
        for (const s of forgeStatus.schedulers || ['Karras', 'Automatic']) {
          const opt = el('option', { value: s, text: s });
          if (s === defaultScheduler) opt.selected = true;
          schedulerSelect.appendChild(opt);
        }

        // Upscalers
        hrUpscalerSelect.innerHTML = '';
        for (const u of forgeStatus.upscalers || ['ESRGAN_4x', 'Latent', 'None']) {
          hrUpscalerSelect.appendChild(el('option', { value: u, text: u }));
        }
      } else {
        forgeDot.className = 'dot not_configured';
        forgeMsg.textContent = 'Forge not running -- start it with --api flag';
        genBtn.disabled = true;
        if (!_forgeRetryTimer) _forgeRetryTimer = setInterval(checkForge, 10000);
      }
    } catch (_) {
      forgeDot.className = 'dot not_configured';
      forgeMsg.textContent = 'Forge not detected';
      genBtn.disabled = true;
      if (!_forgeRetryTimer) _forgeRetryTimer = setInterval(checkForge, 10000);
    }
  }

  // Switch model
  modelSelect.addEventListener('change', async () => {
    try {
      await api('/api/prompts/forge/set-model', {
        method: 'POST',
        body: JSON.stringify({ model: modelSelect.value }),
      });
      toast(`Loading model: ${modelSelect.value}`, 'info');
    } catch (e) { toast(e.message, 'error'); }
  });

  // ── Generate ─────────────────────────────────────────────────────────
  let progressTimer = null;

  genBtn.addEventListener('click', async () => {
    const prompt = promptArea.value.trim();
    if (!prompt) { toast('Enter a prompt first', 'error'); return; }
    if (!forgeStatus?.alive) { toast('Forge is not running', 'error'); return; }

    genBtn.disabled = true;
    genBtn.innerHTML = '<span class="spinner"></span> Generating...';
    progressMsg.style.display = '';
    progressMsg.textContent = 'Submitting to Forge...';

    progressTimer = setInterval(async () => {
      try {
        const p = await api('/api/prompts/forge/progress');
        const pct = Math.round((p.progress || 0) * 100);
        progressMsg.textContent = pct > 0 ? `Generating... ${pct}%` : 'Generating...';
      } catch (_) {}
    }, 1000);

    try {
      const data = await api('/api/prompts/forge/txt2img', {
        method: 'POST',
        body: JSON.stringify({
          prompt,
          negative_prompt: negArea.value,
          steps: stepsSlider.value,
          cfg_scale: cfgSlider.value,
          sampler: samplerSelect.value,
          scheduler: schedulerSelect.value,
          width: parseInt(widthInput.value),
          height: parseInt(heightInput.value),
          seed: parseInt(seedInput.value),
          enable_hr: hrEnabled.checked,
          hr_scale: hrScaleSlider.value,
          hr_upscaler: hrUpscalerSelect.value || 'ESRGAN_4x',
          hr_steps: hrStepsSlider.value,
          hr_denoise: hrDenoiseSlider.value,
        }),
      });

      if (data.images?.length) {
        const savedPath = data.saved_paths?.[0] || null;
        const entry = {
          src: `data:image/png;base64,${data.images[0]}`,
          seed: data.seed || -1,
          prompt,
          path: savedPath,
        };
        generatedImages.push(entry);
        addToGallery(entry);
        if (savedPath) pushToGallery('image-gen', savedPath, prompt, data.seed, {
          model: modelSelect.value,
          sampler: samplerSelect.value,
          scheduler: schedulerSelect.value,
          steps: Number(stepsSlider.value),
          cfg: Number(cfgSlider.value),
          width: parseInt(widthInput.value),
          height: parseInt(heightInput.value),
        });
        showImage(generatedImages.length - 1);
        toast(`Image generated! Seed: ${entry.seed}`, 'success');
      }
    } catch (e) { toast(e.message, 'error'); }

    clearInterval(progressTimer);
    progressMsg.style.display = 'none';
    genBtn.disabled = false;
    genBtn.textContent = 'Generate Image';
  });

  // Ctrl+Enter shortcut
  promptArea.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.key === 'Enter') { e.preventDefault(); genBtn.click(); }
  });

  // Load config defaults and apply to UI
  async function loadDefaults() {
    try {
      const cfg = await api('/api/config');
      if (cfg.forge_default_width) widthInput.value = cfg.forge_default_width;
      if (cfg.forge_default_height) heightInput.value = cfg.forge_default_height;
      if (cfg.forge_default_steps) stepsSlider.value = cfg.forge_default_steps;
      if (cfg.forge_default_cfg != null) cfgSlider.value = cfg.forge_default_cfg;
    } catch (_) {}
  }

  // Start
  loadDefaults();
  checkForge();
}
