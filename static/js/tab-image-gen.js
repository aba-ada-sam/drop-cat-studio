/**
 * Drop Cat Go Studio -- Image Generation tab.
 * Direct interface to Forge SD or OpenAI DALL-E 3 for txt2img.
 * Source toggle auto-falls back to DALL-E when Forge is offline.
 */
import { api } from './api.js';
import { toast, createSlider, createDropZone, el } from './components.js';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260419h';

let forgeStatus  = null;
let openaiAvail  = false;
let _forgeRetryTimer = null;
let generatedImages = []; // { src, seed, prompt, path }
let currentIdx = -1;
let source = 'forge'; // 'forge' | 'dalle'

export function init(panel) {
  panel.innerHTML = '';
  const layout = el('div', { class: 'wide-layout' });
  panel.appendChild(layout);

  const sidebar  = el('div', { class: 'sidebar' });
  const mainArea = el('div', { class: 'main-area' });
  layout.appendChild(sidebar);
  layout.appendChild(mainArea);

  // ── Source toggle ─────────────────────────────────────────────────────
  const sourceCard = el('div', { class: 'card', style: 'padding:10px' });
  sidebar.appendChild(sourceCard);

  const sourceRow = el('div', { style: 'display:flex; gap:6px; align-items:center' });
  sourceCard.appendChild(sourceRow);
  sourceCard.appendChild(el('div', { style: 'font-size:.78rem; color:var(--text-3); margin-top:6px',
    text: 'Forge: local SD models  |  DALL-E 3: OpenAI API (no local GPU needed)' }));

  const btnForge = el('button', {
    class: 'btn btn-sm btn-primary',
    text: 'Forge SD',
    style: 'flex:1',
    onclick() { setSource('forge'); },
  });
  const btnDalle = el('button', {
    class: 'btn btn-sm',
    text: 'DALL-E 3',
    style: 'flex:1',
    onclick() { setSource('dalle'); },
  });
  sourceRow.appendChild(btnForge);
  sourceRow.appendChild(btnDalle);

  // ── Status banner ────────────────────────────────────────────────────
  const statusBanner = el('div', { class: 'card', style: 'padding:10px; display:flex; align-items:center; gap:8px' });
  const statusDot = el('span', { class: 'dot' });
  const statusMsg = el('span', { style: 'font-size:.85rem', text: 'Checking...' });
  statusBanner.appendChild(statusDot);
  statusBanner.appendChild(statusMsg);
  sidebar.appendChild(statusBanner);

  // ── Prompt ───────────────────────────────────────────────────────────
  const promptCard = el('div', { class: 'step-card' });
  sidebar.appendChild(promptCard);
  promptCard.appendChild(el('h3', { text: 'Prompt' }));

  const promptArea = el('textarea', {
    rows: '5',
    placeholder: 'Describe the image...',
    style: 'width:100%; resize:vertical',
  });
  promptCard.appendChild(promptArea);

  const negArea = el('textarea', {
    rows: '2',
    placeholder: 'Negative prompt (Forge only)',
    style: 'width:100%; resize:vertical; margin-top:8px; font-size:.85rem',
  });
  negArea.value = 'blurry, low quality, watermark, text, logo, ugly, deformed';
  promptCard.appendChild(negArea);

  // ── Forge settings ────────────────────────────────────────────────────
  const forgeSettings = el('div');
  sidebar.appendChild(forgeSettings);

  const settingsCard = el('div', { class: 'step-card' });
  forgeSettings.appendChild(settingsCard);
  settingsCard.appendChild(el('h3', { text: 'Forge Settings' }));

  const modelGroup = el('div', { class: 'form-group' });
  modelGroup.appendChild(el('label', { text: 'Model' }));
  const modelSelect = el('select', { style: 'width:100%' });
  modelSelect.appendChild(el('option', { text: 'Loading...', value: '' }));
  modelGroup.appendChild(modelSelect);
  settingsCard.appendChild(modelGroup);

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

  const presetRow = el('div', { style: 'display:flex; gap:4px; margin-bottom:8px; flex-wrap:wrap' });
  for (const p of [{label:'1:1',w:1024,h:1024},{label:'16:9',w:1440,h:810},{label:'9:16',w:810,h:1440},{label:'3:2',w:1440,h:960},{label:'4:3',w:1440,h:1080}]) {
    presetRow.appendChild(el('button', { class:'btn btn-sm', text:p.label,
      style:'font-size:.72rem;padding:2px 8px',
      onclick() { widthInput.value=p.w; heightInput.value=p.h; } }));
  }
  settingsCard.appendChild(presetRow);

  const stepsSlider  = createSlider(settingsCard, { label: 'Steps',     min: 1,   max: 60,  step: 1,    value: 30  });
  const cfgSlider    = createSlider(settingsCard, { label: 'CFG Scale', min: 1,   max: 20,  step: 0.5,  value: 2.5 });

  const seedRow = el('div', { style: 'display:flex; gap:6px; align-items:end' });
  const seedGroup = el('div', { class: 'form-group', style: 'flex:1' });
  seedGroup.appendChild(el('label', { text: 'Seed' }));
  const seedInput = el('input', { type: 'number', value: '-1', style: 'width:100%' });
  seedGroup.appendChild(seedInput);
  seedRow.appendChild(seedGroup);
  seedRow.appendChild(el('button', { class:'btn btn-sm', text:'Random', style:'margin-bottom:8px',
    onclick() { seedInput.value = '-1'; } }));
  settingsCard.appendChild(seedRow);

  const hrToggle = el('details', { style: 'margin-top:6px' });
  hrToggle.appendChild(el('summary', { style:'cursor:pointer;font-size:.85rem;color:var(--text-2)', text:'HiRes Fix' }));
  const hrBody = el('div', { style: 'margin-top:6px' });
  hrToggle.appendChild(hrBody);
  settingsCard.appendChild(hrToggle);
  const hrEnabled = el('input', { type: 'checkbox', id: 'ig-hr-enable' });
  hrBody.appendChild(el('div', { style:'display:flex;align-items:center;gap:6px;margin-bottom:6px' }, [
    hrEnabled, el('label', { for:'ig-hr-enable', text:'Enable HiRes Fix', style:'cursor:pointer' }),
  ]));
  const hrScaleSlider  = createSlider(hrBody, { label:'Scale',   min:1.0, max:2.0, step:0.1,  value:1.5 });
  const hrStepsSlider  = createSlider(hrBody, { label:'Steps',   min:0,   max:40,  step:1,    value:15  });
  const hrDenoiseSlider= createSlider(hrBody, { label:'Denoise', min:0.1, max:1.0, step:0.05, value:0.5 });
  const hrUpscalerGroup= el('div', { class: 'form-group' });
  hrUpscalerGroup.appendChild(el('label', { text:'Upscaler' }));
  const hrUpscalerSelect = el('select', { style:'width:100%' });
  hrUpscalerSelect.appendChild(el('option', { value:'ESRGAN_4x', text:'ESRGAN_4x' }));
  hrUpscalerGroup.appendChild(hrUpscalerSelect);
  hrBody.appendChild(hrUpscalerGroup);

  // ── DALL-E settings ───────────────────────────────────────────────────
  const dalleSettings = el('div', { style: 'display:none' });
  sidebar.appendChild(dalleSettings);

  const dalleCard = el('div', { class: 'step-card' });
  dalleSettings.appendChild(dalleCard);
  dalleCard.appendChild(el('h3', { text: 'DALL-E 3 Settings' }));

  const sizeGroup = el('div', { class: 'form-group' });
  sizeGroup.appendChild(el('label', { text: 'Size' }));
  const sizeSelect = el('select', { style: 'width:100%' });
  for (const [val, lbl] of [['1792x1024','1792x1024 (landscape)'],['1024x1024','1024x1024 (square)'],['1024x1792','1024x1792 (portrait)']]) {
    sizeSelect.appendChild(el('option', { value:val, text:lbl }));
  }
  sizeGroup.appendChild(sizeSelect);
  dalleCard.appendChild(sizeGroup);

  const qualGroup = el('div', { class: 'form-group' });
  qualGroup.appendChild(el('label', { text: 'Quality' }));
  const qualSelect = el('select', { style: 'width:100%' });
  qualSelect.appendChild(el('option', { value:'standard', text:'Standard' }));
  qualSelect.appendChild(el('option', { value:'hd', text:'HD (2x cost)' }));
  qualGroup.appendChild(qualSelect);
  dalleCard.appendChild(qualGroup);

  const styleGroup = el('div', { class: 'form-group' });
  styleGroup.appendChild(el('label', { text: 'Style' }));
  const styleSelect = el('select', { style: 'width:100%' });
  styleSelect.appendChild(el('option', { value:'vivid',   text:'Vivid (dramatic, hyper-real)' }));
  styleSelect.appendChild(el('option', { value:'natural', text:'Natural (closer to reality)' }));
  styleGroup.appendChild(styleSelect);
  dalleCard.appendChild(styleGroup);

  dalleCard.appendChild(el('div', {
    style: 'font-size:.78rem; color:var(--text-3); margin-top:8px',
    text: 'DALL-E 3 rewrites your prompt internally for best results. The revised prompt is shown after generation.',
  }));

  // ── Generate button ───────────────────────────────────────────────────
  const genBtn = el('button', {
    class: 'btn btn-primary',
    text: 'Generate Image',
    style: 'width:100%; font-size:1.1rem; padding:12px 0; margin-top:10px',
  });
  sidebar.appendChild(genBtn);

  const progressMsg = el('div', { style: 'display:none; text-align:center; padding:8px; font-size:.85rem; color:var(--accent)' });
  sidebar.appendChild(progressMsg);

  // ── Main area ─────────────────────────────────────────────────────────
  const resultCard = el('div', { class: 'card', style: 'text-align:center' });
  mainArea.appendChild(resultCard);

  const revisedPromptEl = el('div', {
    style: 'display:none; margin-bottom:8px; font-size:.78rem; color:var(--text-3); font-style:italic; text-align:left; padding:6px 8px; background:var(--bg-2); border-radius:var(--r-sm)',
  });
  resultCard.appendChild(revisedPromptEl);

  const resultImg = el('img', {
    style: 'max-width:100%; border-radius:var(--r-sm); display:none; cursor:pointer',
    title: 'Click to open full size',
  });
  resultImg.addEventListener('click', () => { if (resultImg.src) window.open(resultImg.src, '_blank'); });
  resultCard.appendChild(resultImg);

  const resultInfo = el('div', { style: 'display:none; margin-top:8px; font-size:.82rem; color:var(--text-2)' });
  resultCard.appendChild(resultInfo);

  const emptyMsg = el('div', {
    style: 'padding:30px 10px; color:var(--text-3); font-size:.9rem',
    text: 'Generated images will appear here.',
  });
  resultCard.appendChild(emptyMsg);

  const actionRow = el('div', { style: 'display:none; margin-top:10px; display:flex; gap:6px; justify-content:center; flex-wrap:wrap' });
  resultCard.appendChild(actionRow);

  const btnReuse = el('button', { class:'btn btn-sm', text:'Reuse seed', onclick() {
    if (generatedImages[currentIdx]) seedInput.value = generatedImages[currentIdx].seed;
  }});
  const btnSendFun = el('button', { class:'btn btn-sm', text:'-> Make Videos', onclick() {
    if (!generatedImages[currentIdx]?.path) return;
    import('./handoff.js').then(h => h.handoff('fun-videos', { type:'image', path:generatedImages[currentIdx].path }));
    document.querySelector('[data-tab="fun-videos"]')?.click();
    toast('Image sent to Make Videos', 'info');
  }});
  const btnSendSD = el('button', { class:'btn btn-sm', text:'-> SD Prompts', onclick() {
    if (!generatedImages[currentIdx]?.path) return;
    import('./handoff.js').then(h => h.handoff('sd-prompts', { type:'image', path:generatedImages[currentIdx].path }));
    document.querySelector('[data-tab="sd-prompts"]')?.click();
    toast('Image sent to SD Prompts', 'info');
  }});
  actionRow.appendChild(btnReuse);
  actionRow.appendChild(btnSendSD);
  actionRow.appendChild(btnSendFun);

  const navRow = el('div', { style: 'display:none; margin-top:8px; display:flex; justify-content:center; align-items:center; gap:12px' });
  const prevBtn = el('button', { class:'btn btn-sm', text:'< Prev', onclick() { showImage(currentIdx-1); } });
  const navLabel = el('span', { style: 'font-size:.82rem; color:var(--text-2)' });
  const nextBtn = el('button', { class:'btn btn-sm', text:'Next >', onclick() { showImage(currentIdx+1); } });
  navRow.appendChild(prevBtn); navRow.appendChild(navLabel); navRow.appendChild(nextBtn);
  resultCard.appendChild(navRow);

  const galleryCard = el('div', { class: 'card' });
  mainArea.appendChild(galleryCard);
  galleryCard.appendChild(el('h3', { style:'margin-bottom:8px', text:'Gallery' }));
  const galleryGrid = el('div', { style:'display:grid;grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:6px' });
  galleryCard.appendChild(galleryGrid);
  const galleryEmpty = el('div', { style:'font-size:.82rem;color:var(--text-3);padding:6px 0', text:'No images generated yet.' });
  galleryCard.appendChild(galleryEmpty);

  // ── Functions ─────────────────────────────────────────────────────────

  function setSource(s) {
    source = s;
    if (s === 'forge') {
      btnForge.className = 'btn btn-sm btn-primary'; btnForge.style.flex = '1';
      btnDalle.className = 'btn btn-sm';             btnDalle.style.flex = '1';
      forgeSettings.style.display = '';
      dalleSettings.style.display = 'none';
      negArea.style.display = '';
    } else {
      btnDalle.className = 'btn btn-sm btn-primary'; btnDalle.style.flex = '1';
      btnForge.className = 'btn btn-sm';             btnForge.style.flex = '1';
      forgeSettings.style.display = 'none';
      dalleSettings.style.display = '';
      negArea.style.display = 'none';
    }
    updateBanner();
  }

  function updateBanner() {
    if (source === 'forge') {
      if (forgeStatus?.alive) {
        statusDot.className = 'dot running';
        statusMsg.textContent = `Forge running — ${forgeStatus.current_model || '?'}`;
        genBtn.disabled = false;
      } else {
        statusDot.className = 'dot not_configured';
        statusMsg.textContent = 'Forge not running — start it with --api flag';
        genBtn.disabled = true;
      }
    } else {
      if (openaiAvail) {
        statusDot.className = 'dot running';
        statusMsg.textContent = 'DALL-E 3 ready (OpenAI API)';
        genBtn.disabled = false;
      } else {
        statusDot.className = 'dot not_configured';
        statusMsg.textContent = 'OpenAI key not set — add it in Settings';
        genBtn.disabled = true;
      }
    }
  }

  function showImage(idx) {
    if (idx < 0 || idx >= generatedImages.length) return;
    currentIdx = idx;
    layout.classList.add('has-result');
    const img = generatedImages[idx];
    resultImg.src = img.src;
    resultImg.style.display = '';
    emptyMsg.style.display = 'none';
    resultInfo.style.display = '';
    resultInfo.textContent = img.seed > 0 ? `Seed: ${img.seed}  |  ${img.prompt.slice(0,80)}` : img.prompt.slice(0,100);
    if (img.revisedPrompt && img.revisedPrompt !== img.prompt) {
      revisedPromptEl.style.display = '';
      revisedPromptEl.textContent = `DALL-E revised: ${img.revisedPrompt}`;
    } else {
      revisedPromptEl.style.display = 'none';
    }
    actionRow.style.display = 'flex';
    navRow.style.display = generatedImages.length > 1 ? 'flex' : 'none';
    navLabel.textContent = `${idx+1} / ${generatedImages.length}`;
    prevBtn.disabled = idx <= 0;
    nextBtn.disabled = idx >= generatedImages.length - 1;
    galleryGrid.querySelectorAll('.ig-thumb').forEach((t, i) => {
      t.style.outline = i === idx ? '2px solid var(--accent)' : 'none';
    });
  }

  function addToGallery(entry) {
    galleryEmpty.style.display = 'none';
    galleryGrid.appendChild(el('img', {
      class: 'ig-thumb',
      src: entry.src,
      style: 'width:100%;aspect-ratio:1;object-fit:cover;border-radius:var(--r-sm);cursor:pointer',
      title: entry.seed > 0 ? `Seed: ${entry.seed}` : 'DALL-E 3',
      onclick() { showImage(generatedImages.indexOf(entry)); },
    }));
  }

  async function checkForge() {
    try {
      forgeStatus = await api('/api/prompts/forge/status');
      if (forgeStatus.alive) {
        if (_forgeRetryTimer) { clearInterval(_forgeRetryTimer); _forgeRetryTimer = null; }
        modelSelect.innerHTML = '';
        for (const m of forgeStatus.models || []) {
          const opt = el('option', { value: m.title||m.name, text: m.title||m.name });
          if ((m.title||m.name).includes(forgeStatus.current_model||'')) opt.selected = true;
          modelSelect.appendChild(opt);
        }
        samplerSelect.innerHTML = '';
        const defSampler = forgeStatus.default_sampler || 'DPM++ 2M SDE';
        for (const s of forgeStatus.samplers || ['DPM++ 2M SDE','Euler','DDIM']) {
          const opt = el('option', { value:s, text:s });
          if (s === defSampler) opt.selected = true;
          samplerSelect.appendChild(opt);
        }
        schedulerSelect.innerHTML = '';
        const defScheduler = forgeStatus.default_scheduler || 'Karras';
        for (const s of forgeStatus.schedulers || ['Karras','Automatic']) {
          const opt = el('option', { value:s, text:s });
          if (s === defScheduler) opt.selected = true;
          schedulerSelect.appendChild(opt);
        }
        hrUpscalerSelect.innerHTML = '';
        for (const u of forgeStatus.upscalers || ['ESRGAN_4x','Latent','None']) {
          hrUpscalerSelect.appendChild(el('option', { value:u, text:u }));
        }
      } else {
        if (!_forgeRetryTimer) _forgeRetryTimer = setInterval(checkForge, 10000);
        // Auto-switch to DALL-E if Forge is offline and key is available
        if (openaiAvail && source === 'forge') {
          setSource('dalle');
          toast('Forge offline — switched to DALL-E 3', 'info');
        }
      }
      updateBanner();
    } catch (_) {
      if (!_forgeRetryTimer) _forgeRetryTimer = setInterval(checkForge, 10000);
      updateBanner();
    }
  }

  async function checkOpenAI() {
    try {
      const r = await api('/api/prompts/openai/status');
      openaiAvail = r.available;
    } catch (_) { openaiAvail = false; }
    updateBanner();
  }

  modelSelect.addEventListener('change', async () => {
    try {
      await api('/api/prompts/forge/set-model', { method:'POST', body:JSON.stringify({ model: modelSelect.value }) });
      toast(`Loading model: ${modelSelect.value}`, 'info');
    } catch (e) { toast(e.message, 'error'); }
  });

  let progressTimer = null;

  genBtn.addEventListener('click', async () => {
    const prompt = promptArea.value.trim();
    if (!prompt) { toast('Enter a prompt first', 'error'); return; }

    genBtn.disabled = true;
    genBtn.innerHTML = '<span class="spinner"></span> Generating...';
    progressMsg.style.display = '';

    if (source === 'dalle') {
      progressMsg.textContent = 'Sending to DALL-E 3...';
      try {
        const data = await api('/api/prompts/openai/txt2img', {
          method: 'POST',
          body: JSON.stringify({
            prompt,
            size:    sizeSelect.value,
            quality: qualSelect.value,
            style:   styleSelect.value,
          }),
        });
        if (data.images?.length) {
          const entry = {
            src: `data:image/png;base64,${data.images[0]}`,
            seed: -1,
            prompt,
            revisedPrompt: data.revised_prompt || '',
            path: data.saved_paths?.[0] || null,
          };
          generatedImages.push(entry);
          addToGallery(entry);
          if (entry.path) pushToGallery('image-gen', entry.path, prompt, -1, { source:'dall-e-3', size:sizeSelect.value, quality:qualSelect.value });
          showImage(generatedImages.length - 1);
          toast('DALL-E 3 image generated!', 'success');
        }
      } catch (e) { toast(e.message, 'error'); }
    } else {
      progressMsg.textContent = 'Submitting to Forge...';
      progressTimer = setInterval(async () => {
        try {
          const p = await api('/api/prompts/forge/progress');
          const pct = Math.round((p.progress||0)*100);
          progressMsg.textContent = pct > 0 ? `Generating... ${pct}%` : 'Generating...';
        } catch (_) {}
      }, 1000);
      try {
        const data = await api('/api/prompts/forge/txt2img', {
          method: 'POST',
          body: JSON.stringify({
            prompt,
            negative_prompt: negArea.value,
            steps:      stepsSlider.value,
            cfg_scale:  cfgSlider.value,
            sampler:    samplerSelect.value,
            scheduler:  schedulerSelect.value,
            width:      parseInt(widthInput.value),
            height:     parseInt(heightInput.value),
            seed:       parseInt(seedInput.value),
            enable_hr:  hrEnabled.checked,
            hr_scale:   hrScaleSlider.value,
            hr_upscaler:hrUpscalerSelect.value || 'ESRGAN_4x',
            hr_steps:   hrStepsSlider.value,
            hr_denoise: hrDenoiseSlider.value,
          }),
        });
        if (data.images?.length) {
          const entry = { src:`data:image/png;base64,${data.images[0]}`, seed:data.seed||-1, prompt, path:data.saved_paths?.[0]||null };
          generatedImages.push(entry);
          addToGallery(entry);
          if (entry.path) pushToGallery('image-gen', entry.path, prompt, data.seed, {
            model: modelSelect.value, sampler: samplerSelect.value, steps: Number(stepsSlider.value), cfg: Number(cfgSlider.value),
            width: parseInt(widthInput.value), height: parseInt(heightInput.value),
          });
          showImage(generatedImages.length - 1);
          toast(`Image generated! Seed: ${entry.seed}`, 'success');
        }
      } catch (e) { toast(e.message, 'error'); }
      clearInterval(progressTimer);
    }

    progressMsg.style.display = 'none';
    genBtn.disabled = false;
    genBtn.textContent = 'Generate Image';
  });

  promptArea.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.key === 'Enter') { e.preventDefault(); genBtn.click(); }
  });

  async function loadDefaults() {
    try {
      const cfg = await api('/api/config');
      if (cfg.forge_default_width)  widthInput.value   = cfg.forge_default_width;
      if (cfg.forge_default_height) heightInput.value  = cfg.forge_default_height;
      if (cfg.forge_default_steps)  stepsSlider.value  = cfg.forge_default_steps;
      if (cfg.forge_default_cfg != null) cfgSlider.value = cfg.forge_default_cfg;
    } catch (_) {}
  }

  loadDefaults();
  checkOpenAI().then(() => checkForge());
}
