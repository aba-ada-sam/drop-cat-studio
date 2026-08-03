/**
 * Drop Cat Go Studio -- Image Studio.
 * Manual, dedicated image-creation surface: prompt/negative/size/steps/cfg
 * always visible, a preset selector (Default / NSFW) that can swap Forge's
 * loaded checkpoint, and an Animate button into the same video pipeline
 * Chat Studio uses.
 */
import { pollJob } from './api.js?v=20260620a';
import { el, pathToUrl } from './components.js?v=20260801b';
import { toast, apiFetch } from './shell/toast.js?v=20260620a';

const GALLERY_KEY = 'dropcat_image_studio_gallery';
const GALLERY_CAP = 200;
const SETTINGS_KEY = 'dropcat_image_studio_settings';

export function init(panel) {
  panel.innerHTML = '';

  let gallery = _loadGallery();
  let _currentImagePath = null;
  const _activePollers = [];
  // animateBtn only stays disabled for the initial POST, not for however long
  // the render actually takes, and jobArea is one shared div with no per-job
  // scoping -- generating a new image while a video is still rendering used
  // to silently wipe its visible progress with no indication it was still
  // working (the job itself keeps running server-side regardless).
  let _animateJobActive = false;
  // Tags which animate job is "current" -- see _watchVideoJob(). Without
  // this, generating a new image (or re-clicking Animate) while a previous
  // video was still rendering started a SECOND poller against the same
  // shared jobArea/_animateJobActive with no way to tell them apart:
  // whichever job's poller callback fired last silently won, sometimes
  // overwriting an already-displayed finished video with a stale/abandoned
  // job's result, and could reset _animateJobActive to false while the
  // real current job was still running.
  let _latestVideoJobId = null;

  const root = el('div', {
    style: 'max-width:900px; margin:0 auto; padding:24px 16px; display:flex; flex-direction:column; gap:14px;',
  });
  panel.appendChild(root);

  root.appendChild(el('div', {}, [
    el('div', { style: 'font-size:1.3rem; font-weight:700; color:var(--text);', text: 'Image Studio' }),
    el('div', { style: 'font-size:.82rem; color:var(--text-3);', text: 'Direct prompt controls, NSFW-capable presets, straight to Forge -- then animate the result.' }),
  ]));

  // -- Form card ---------------------------------------------------------------
  const promptTa = el('textarea', { rows: '3', style: 'width:100%; font-size:.9rem; resize:vertical;', placeholder: 'Describe the image...' });
  const negTa = el('input', { type: 'text', style: 'width:100%; font-size:.82rem; margin-top:6px;', placeholder: 'Negative prompt (optional)' });

  const presetSel = el('select', { style: 'font-size:.85rem;' });
  const presetDesc = el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-top:4px;' });

  const subjectSel = el('select', { style: 'font-size:.85rem; display:none;' }, [
    el('option', { value: 'auto', text: 'Auto (detect from prompt)' }),
    el('option', { value: 'male', text: 'Male' }),
    el('option', { value: 'female', text: 'Female' }),
    el('option', { value: 'multi', text: 'Multi: man + woman (regional)' }),
    el('option', { value: 'multi_male', text: 'Multi: two+ men (regional)' }),
    el('option', { value: 'multi_female', text: 'Multi: two+ women (regional)' }),
  ]);
  const creatureCb = el('input', { type: 'checkbox', id: 'is-creature-cb' });
  const subjectRow = el('div', { style: 'display:none; align-items:center; gap:14px; margin-top:6px; flex-wrap:wrap;' }, [
    el('span', { style: 'font-size:.72rem; color:var(--text-3); text-transform:uppercase; letter-spacing:.05em;', text: 'Subject' }),
    subjectSel,
    el('label', { for: 'is-creature-cb', style: 'display:flex; align-items:center; gap:5px; font-size:.8rem; color:var(--text); cursor:pointer;' }, [
      creatureCb,
      el('span', { text: 'Anthropomorphic creature' }),
    ]),
  ]);

  const widthIn  = el('input', { type: 'number', min: '256', max: '2048', step: '64', value: '1024', style: 'width:80px; font-size:.8rem;' });
  const heightIn = el('input', { type: 'number', min: '256', max: '2048', step: '64', value: '1024', style: 'width:80px; font-size:.8rem;' });
  const stepsIn  = el('input', { type: 'number', min: '1', max: '80', value: '25', style: 'width:70px; font-size:.8rem;' });
  const cfgIn    = el('input', { type: 'number', min: '1', max: '20', step: '0.5', value: '3', style: 'width:70px; font-size:.8rem;' });
  const seedIn   = el('input', { type: 'number', step: '1', value: '-1', style: 'width:100px; font-size:.8rem;' });

  const controlsRow = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-top:8px; flex-wrap:wrap;' }, [
    el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'W' }), widthIn,
    el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'H' }), heightIn,
    el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'Steps' }), stepsIn,
    el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'CFG' }), cfgIn,
    el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'Seed' }), seedIn,
  ]);

  const genBtn = el('button', { class: 'btn btn-primary', text: 'Generate' });
  const errBox = el('div', { style: 'display:none; font-size:.8rem; color:#e88; margin-top:8px;' });

  const formCard = el('div', { class: 'card', style: 'padding:14px;' }, [
    el('div', { style: 'font-size:.72rem; color:var(--text-3); text-transform:uppercase; letter-spacing:.05em; margin-bottom:4px;', text: 'Prompt' }),
    promptTa,
    negTa,
    el('div', { style: 'display:flex; align-items:center; gap:8px; margin-top:10px;' }, [
      el('span', { style: 'font-size:.72rem; color:var(--text-3); text-transform:uppercase; letter-spacing:.05em;', text: 'Preset' }),
      presetSel,
    ]),
    presetDesc,
    subjectRow,
    controlsRow,
    el('div', { style: 'margin-top:10px;' }, [genBtn]),
    errBox,
  ]);
  root.appendChild(formCard);

  // -- Result card ---------------------------------------------------------------
  const resultImg = el('img', { style: 'max-width:100%; border-radius:8px; cursor:pointer; display:none;', title: 'Click to open full size' });
  resultImg.addEventListener('click', () => { if (resultImg.src) window.open(resultImg.src, '_blank'); });
  const resultMeta = el('div', { style: 'font-size:.7rem; color:var(--text-3); margin-top:4px;' });

  const videoInput = el('input', { type: 'text', style: 'flex:1; font-size:.82rem;', placeholder: 'Describe the motion...' });
  const animateBtn = el('button', { class: 'btn btn-sm btn-primary', text: 'Animate this', style: 'flex-shrink:0;' });
  const animateRow = el('div', { style: 'display:none; gap:6px; margin-top:10px;' }, [videoInput, animateBtn]);

  const jobArea = el('div', { style: 'margin-top:10px;' });

  const resultCard = el('div', { class: 'card', style: 'padding:14px; display:none;' }, [
    el('div', { style: 'font-size:.72rem; color:var(--text-3); text-transform:uppercase; letter-spacing:.05em; margin-bottom:4px;', text: 'Result' }),
    resultImg, resultMeta, animateRow, jobArea,
  ]);
  root.appendChild(resultCard);

  // -- Gallery ---------------------------------------------------------------
  const galleryGrid = el('div', { style: 'display:flex; flex-wrap:wrap; gap:8px;' });
  const galleryCard = el('div', { class: 'card', style: 'padding:14px;' }, [
    el('div', { style: 'font-size:.72rem; color:var(--text-3); text-transform:uppercase; letter-spacing:.05em; margin-bottom:8px;', text: 'Session renders' }),
    galleryGrid,
  ]);
  root.appendChild(galleryCard);

  // -- Helpers ---------------------------------------------------------------
  function _loadGallery() {
    try {
      const raw = JSON.parse(localStorage.getItem(GALLERY_KEY) || '[]');
      return Array.isArray(raw) ? raw : [];
    } catch (e) { return []; }
  }

  function _saveGallery() {
    try { localStorage.setItem(GALLERY_KEY, JSON.stringify(gallery.slice(-GALLERY_CAP))); } catch (e) { /* non-fatal */ }
  }

  // Preset/subject/creature choice previously reset to "Default" (vanilla
  // zavychromaxl, no NSFW/taste steering) every time this tab was reopened
  // or the app restarted, since the <select> was rebuilt fresh from the
  // backend's preset list with no memory of the last choice -- silently, no
  // indication anything changed. Andrew hit this 2026-08-01: generated
  // through "Default" without noticing, got generic cloned-face stock-photo
  // output. Persist the last-used choice so it survives reopens/restarts.
  function _loadSettings() {
    try {
      const raw = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
      return (raw && typeof raw === 'object') ? raw : {};
    } catch (e) { return {}; }
  }

  function _saveSettings() {
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify({
        preset: presetSel.value, subject: subjectSel.value, creature: creatureCb.checked,
      }));
    } catch (e) { /* non-fatal */ }
  }

  function _renderGallery() {
    galleryGrid.innerHTML = '';
    if (!gallery.length) {
      galleryGrid.appendChild(el('div', { style: 'font-size:.8rem; color:var(--text-3);', text: 'Nothing generated yet this session.' }));
      return;
    }
    for (const item of gallery.slice().reverse()) {
      const thumb = el('img', {
        src: item.url, style: 'width:96px; height:96px; object-fit:cover; border-radius:6px; cursor:pointer;',
        title: item.prompt || '',
      });
      thumb.addEventListener('click', () => _loadFromGallery(item));
      galleryGrid.appendChild(thumb);
    }
  }

  function _loadFromGallery(item) {
    promptTa.value = item.prompt || '';
    negTa.value = item.negative || '';
    widthIn.value = item.width || 1024;
    heightIn.value = item.height || 1024;
    // This tab's whole point is seed-comparison work (per the backend's own
    // doc comments) -- restoring the prompt/preset but leaving whatever
    // seed/steps/cfg happen to still be sitting in the inputs meant clicking
    // an old thumbnail to reproduce/tweak it silently used the WRONG
    // generation settings, with nothing indicating the restore was partial.
    if (item.seed  != null) seedIn.value  = item.seed;
    if (item.steps != null) stepsIn.value = item.steps;
    if (item.cfg   != null) cfgIn.value   = item.cfg;
    if (presetSel.querySelector(`option[value="${item.preset}"]`)) presetSel.value = item.preset;
    if (item.subject && subjectSel.querySelector(`option[value="${item.subject}"]`)) subjectSel.value = item.subject;
    creatureCb.checked = !!item.creature;
    _updatePresetDesc();
    _showResult(item);
  }

  function _showResult(data) {
    if (_animateJobActive) {
      // The job is still running server-side (and will still show up in the
      // Queue tab) -- clearing jobArea here would just make it look like it
      // vanished instead of saying so.
      toast('The previous video is still rendering in the background -- check the Queue tab for progress', 'info');
      _animateJobActive = false;
    }
    _currentImagePath = data.image_path;
    videoInput.value = ''; // motion description was for the previous image -- don't silently reuse it
    resultImg.src = data.url || data.image_url;
    resultImg.style.display = '';
    const bits = [`seed ${data.seed}`];
    if (data.checkpoint_used) bits.push(data.checkpoint_used);
    if (data.subject_used && data.subject_used !== 'n/a') bits.push(`subject: ${data.subject_used}`);
    if (data.creature && data.creature_applied) bits.push('creature');
    if (data.creature && !data.creature_applied) bits.push('creature NOT applied (multi-subject not yet supported)');
    if (data.adetailer_ran) bits.push('ADetailer ran');
    resultMeta.textContent = bits.join('  --  ');
    resultCard.style.display = '';
    animateRow.style.display = 'flex';
    jobArea.innerHTML = '';
  }

  // -- Presets ---------------------------------------------------------------
  let _presetsCache = [];

  async function _loadPresets() {
    try {
      const data = await apiFetch('/api/image-studio/presets', { context: 'image-studio.presets', silent: true });
      _presetsCache = data.presets || [];
      presetSel.innerHTML = '';
      for (const p of _presetsCache) {
        const opt = el('option', { value: p.id, text: p.label });
        presetSel.appendChild(opt);
      }
      const saved = _loadSettings();
      if (saved.preset && presetSel.querySelector(`option[value="${saved.preset}"]`)) {
        presetSel.value = saved.preset;
      }
      if (saved.subject && subjectSel.querySelector(`option[value="${saved.subject}"]`)) {
        subjectSel.value = saved.subject;
      }
      creatureCb.checked = !!saved.creature;
      _updatePresetDesc();
    } catch (e) {
      _presetsCache = [];
      presetSel.innerHTML = '';
      presetSel.appendChild(el('option', { value: 'default', text: 'Default' }));
    }
  }

  function _updatePresetDesc() {
    const p = _presetsCache.find(p => p.id === presetSel.value);
    presetDesc.textContent = p ? p.description : '';
    const isNsfw = !!(p && p.nsfw);
    subjectRow.style.display = isNsfw ? 'flex' : 'none';
  }
  presetSel.addEventListener('change', () => { _updatePresetDesc(); _saveSettings(); });
  subjectSel.addEventListener('change', _saveSettings);
  creatureCb.addEventListener('change', _saveSettings);

  // -- Generate ---------------------------------------------------------------
  genBtn.addEventListener('click', async () => {
    const prompt = promptTa.value.trim();
    if (!prompt) { toast('Prompt is empty', 'error'); return; }
    errBox.style.display = 'none';

    const body = {
      prompt,
      negative_prompt: negTa.value.trim(),
      width:  parseInt(widthIn.value, 10)  || 1024,
      height: parseInt(heightIn.value, 10) || 1024,
      steps:  parseInt(stepsIn.value, 10)  || 25,
      cfg:    parseFloat(cfgIn.value)      || 5.0,
      // NOT `|| -1` -- 0 is a legitimate deliberately-chosen seed and is
      // falsy in JS, so that pattern silently turned "seed 0" into "randomize"
      // with no error and no hint in the response (data.seed just comes back
      // as whatever random seed was actually rolled, indistinguishable from
      // "the seed I asked for, resolved").
      seed:   (() => { const s = parseInt(seedIn.value, 10); return Number.isNaN(s) ? -1 : s; })(),
      preset: presetSel.value || 'default',
      subject: subjectSel.value || 'auto',
      creature: creatureCb.checked === true,
    };

    genBtn.disabled = true;
    const origText = genBtn.textContent;
    genBtn.textContent = 'Generating...';
    try {
      const data = await apiFetch('/api/image-studio/generate', {
        method: 'POST', body: JSON.stringify(body), context: 'image-studio.generate', silent: true,
      });
      const item = {
        url: data.image_url, image_path: data.image_path, seed: data.seed,
        checkpoint_used: data.checkpoint_used, adetailer_ran: data.adetailer_ran,
        subject_used: data.subject_used, creature: data.creature, creature_applied: data.creature_applied,
        prompt, negative: body.negative_prompt, width: body.width, height: body.height,
        steps: body.steps, cfg: body.cfg,
        preset: body.preset, subject: body.subject,
      };
      gallery.push(item);
      _saveGallery();
      _renderGallery();
      _showResult(item);
    } catch (e) {
      errBox.textContent = e.message || 'Generation failed';
      errBox.style.display = '';
    } finally {
      genBtn.disabled = false;
      genBtn.textContent = origText;
    }
  });

  // -- Animate ---------------------------------------------------------------
  animateBtn.addEventListener('click', async () => {
    const vp = videoInput.value.trim();
    if (!vp) { toast('Describe the motion first', 'error'); return; }
    if (!_currentImagePath) { toast('No image to animate', 'error'); return; }
    animateBtn.disabled = true;
    try {
      const data = await apiFetch('/api/image-studio/animate', {
        method: 'POST',
        body: JSON.stringify({ image_path: _currentImagePath, video_prompt: vp }),
        context: 'image-studio.animate',
      });
      _watchVideoJob(data.job_id);
    } catch (e) {
      // apiFetch already toasted the server error string
    } finally {
      animateBtn.disabled = false;
    }
  });

  function _watchVideoJob(jobId) {
    _latestVideoJobId = jobId;
    jobArea.innerHTML = '';
    _animateJobActive = true;
    const statusEl = el('div', { style: 'font-size:.82rem; color:var(--text-3);', text: 'Rendering video...' });
    const track = el('div', { style: 'height:4px; width:220px; background:var(--border-2); border-radius:2px; overflow:hidden; margin-top:6px;' });
    const fill  = el('div', { style: 'height:100%; width:0%; background:var(--accent); transition:width .4s;' });
    track.appendChild(fill);
    jobArea.appendChild(statusEl);
    jobArea.appendChild(track);

    const poller = pollJob(
      jobId,
      (j) => {
        if (jobId !== _latestVideoJobId) return; // a newer animate job has superseded this one
        fill.style.width = `${j.progress || 0}%`;
        statusEl.textContent = j.message || (j.status === 'queued' ? 'Queued -- waiting for GPU...' : 'Rendering video...');
      },
      (j) => {
        if (jobId !== _latestVideoJobId) return; // stale/abandoned job -- don't clobber the current one's display
        _animateJobActive = false;
        const outputs = Array.isArray(j.output) ? j.output : [j.output];
        const videoPath = outputs[0];
        jobArea.innerHTML = '';
        jobArea.appendChild(el('div', { style: 'font-size:.82rem; color:var(--text-3); margin-bottom:4px;', text: 'Video ready' }));
        jobArea.appendChild(el('video', {
          controls: '', style: 'max-width:100%; border-radius:8px; background:#000;', src: pathToUrl(videoPath),
        }));
      },
      (err) => {
        if (jobId !== _latestVideoJobId) return; // stale/abandoned job
        _animateJobActive = false;
        jobArea.innerHTML = '';
        jobArea.appendChild(el('div', { style: 'font-size:.82rem; color:#e88;', text: `Video failed: ${err}` }));
      },
      3000,
    );
    _activePollers.push(poller);
  }

  // -- Init ---------------------------------------------------------------
  _loadPresets();
  _renderGallery();

  // Cleanup pollers if the tab is torn down (init() is only called once per
  // panel lifetime in this app, but stop() is cheap and matches Chat's pattern).
  panel.addEventListener('dcs:teardown', () => {
    for (const p of _activePollers) { try { p.stop(); } catch (e) { /* already finished */ } }
  });
}
