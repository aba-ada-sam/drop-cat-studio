/**
 * Drop Cat Go Studio — Express mode.
 * Drop an image, describe your idea, click Create. Everything else is automatic.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js?v=20260505e';
import { el, pathToUrl } from './components.js?v=20260507a';
import { toast, apiFetch } from './shell/toast.js?v=20260503a';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260503g';
import { handoff } from './handoff.js?v=20260422a';

// Module-level so receiveHandoff can call _applyImageFn even after init
let _applyImageFn = null;

export function receiveHandoff(data) {
  if (!_applyImageFn) return;
  if (data?.type === 'image' && data.path) {
    _applyImageFn(data.path, data.url || pathToUrl(data.path));
  }
}

export function init(panel) {
  panel.innerHTML = '';
  const root = el('div', { style: 'max-width:680px; margin:0 auto; padding:24px 16px; display:flex; flex-direction:column; gap:20px;' });
  panel.appendChild(root);

  // ── Output resolution state ───────────────────────────────────────────────
  const RATIOS = [
    { label: '16:9', value: '16:9', rw: 16, rh: 9  },
    { label: '9:16', value: '9:16', rw: 9,  rh: 16 },
    { label: '1:1',  value: '1:1',  rw: 1,  rh: 1  },
    { label: '4:3',  value: '4:3',  rw: 4,  rh: 3  },
    { label: '3:4',  value: '3:4',  rw: 3,  rh: 4  },
  ];
  const QUALITIES = [
    { label: 'Fast',    px: 580, model: 'LTX-2 Dev19B Distilled', maxSec: 20, hint: 'LTX-2 -- ~30-60s per clip, any aspect ratio' },
    { label: 'Quality', px: 480, model: 'Wan2.1-I2V-14B-480P',    maxSec: 16, hint: 'Wan2.1 480P -- richer motion, ~5-15 min per clip' },
    { label: 'HD',      px: 720, model: 'Wan2.1-I2V-14B-720P',    maxSec: 12, hint: 'Wan2.1 720P -- highest quality, ~10-20 min per clip' },
  ];

  // Which ratios each model natively supports well.
  // LTX-2 was trained on variable aspect ratios; Wan models are 16:9 only.
  const MODEL_RATIOS = {
    'LTX-2 Dev19B Distilled': ['16:9', '9:16', '1:1', '4:3', '3:4'],
    'LTX-2 Dev13B':           ['16:9', '9:16', '1:1', '4:3', '3:4'],
  };

  // Per-model optimal defaults.
  // LTX-2 Distilled is a distilled model (like SDXL-Lightning) — its denoising
  // schedule is compressed into 4-8 steps. Running 20+ steps overshoots the
  // schedule and degrades quality. CFG 3 matches its low-guidance training regime.
  // Wan2.1 models are standard diffusion — they need 25 steps and higher CFG.
  const MODEL_DEFAULTS = {
    'LTX-2 Dev19B Distilled': { steps: 8,  guidance: 3.0, duration: 8 },
    'LTX-2 Dev13B':           { steps: 25, guidance: 3.5, duration: 8 },
    'Wan2.1-I2V-14B-480P':    { steps: 25, guidance: 5.5, duration: 6 },
    'Wan2.1-I2V-14B-720P':    { steps: 25, guidance: 5.5, duration: 5 },
    'Wan2.1-T2V-14B':         { steps: 25, guidance: 5.5, duration: 6 },
    'Wan2.1-T2V-1.3B':        { steps: 20, guidance: 5.0, duration: 5 },
  };
  const CHIP_DISABLED = 'opacity:.3; cursor:not-allowed; pointer-events:none;';

  let _model      = 'LTX-2 Dev19B Distilled';
  let _duration   = 8;
  let _allModels  = {};
  let _ratio      = '16:9';
  let _qualityPx  = 580;
  let _outW       = 1032;
  let _outH       = 580;

  function _computeDims(ratioStr, qualityPx) {
    const [rw, rh] = ratioStr.split(':').map(Number);
    let w, h;
    if (rw === rh)      { w = h = qualityPx; }
    else if (rh > rw)   { w = qualityPx; h = Math.round(qualityPx * rh / rw); }
    else                 { h = qualityPx; w = Math.round(qualityPx * rw / rh); }
    // Round to nearest multiple of 32 — LTX-Video VAE requires 32-pixel alignment.
    // Wan models also benefit; ensures no partial-stride artifacts.
    const r32 = n => Math.max(32, Math.round(n / 32) * 32);
    return [r32(w), r32(h)];
  }

  function _preferredModel(qualityPx) {
    const pref = QUALITIES.find(q => q.px === qualityPx)?.model;
    return (pref && _allModels[pref]) ? pref : _model;
  }

  // Placeholders filled in when the UI section is built
  let _dimsLabel  = null;
  let _warnEl     = null;
  let _ratioHint  = null;
  let _ratioChips = {};

  function _refreshOutput() {
    [_outW, _outH] = _computeDims(_ratio, _qualityPx);
    if (_dimsLabel) _dimsLabel.textContent = `${_outW} × ${_outH}`;
    if (_warnEl) _warnEl.style.display = (_outW >= 1080 || _outH >= 1080) ? '' : 'none';
  }

  function _updateRatioAvailability() {
    const supported = MODEL_RATIOS[_model] || ['16:9'];
    const allSupported = RATIOS.every(r => supported.includes(r.value));
    for (const [val, btn] of Object.entries(_ratioChips)) {
      const ok = supported.includes(val);
      const isActive = val === _ratio;
      btn.setAttribute('style', CHIP_BASE + (isActive && ok ? CHIP_ON : '') + (ok ? '' : CHIP_DISABLED));
      btn.title = ok ? '' : 'Switch to LTX Fast quality for portrait, square & alternative ratios';
      if (!ok && isActive) {
        _ratio = '16:9';
        _ratioChips['16:9']?.setAttribute('style', CHIP_BASE + CHIP_ON);
      }
    }
    if (_ratioHint) {
      _ratioHint.style.display = allSupported ? 'none' : '';
    }
    _refreshOutput();
  }

  function _announceModel() {
    document.dispatchEvent(new CustomEvent('dcs:video-model', { detail: { model: _model } }));
  }

  // Re-announce whenever this tab becomes active again
  document.addEventListener('dcs:tab-activated', e => { if (e.detail?.tab === 'express') _announceModel(); });

  api('/api/fun/models').then(data => {
    _allModels = data.models || {};
    const models = Object.entries(_allModels);
    if (models.length) {
      const pref = _preferredModel(_qualityPx);
      _model = pref || models[0][0];
      _applyModelDefaults(_model);
    }
    _updateRatioAvailability();
    _announceModel();
  }).catch(() => toast('Could not load video models -- using defaults', 'error'));

  // ── Heading ───────────────────────────────────────────────────────────────
  root.appendChild(el('div', { style: 'text-align:center; padding-bottom:4px;' }, [
    el('div', { style: 'font-size:1.4rem; font-weight:700; color:var(--text); margin-bottom:6px;', text: 'Create a video' }),
    el('div', { style: 'font-size:.85rem; color:var(--text-3);', text: 'Drop an image, describe what you want, click Create.' }),
  ]));

  // ── Image drop zone ───────────────────────────────────────────────────────
  const imgInput = el('input', { type: 'file', accept: 'image/*', style: 'display:none' });
  root.appendChild(imgInput);

  let _imagePath = null;
  const preview = el('img', { style: 'display:none; width:100%; max-height:260px; object-fit:contain; border-radius:8px; background:var(--bg-raised);' });
  const dropHint = el('div', { style: 'display:flex; flex-direction:column; align-items:center; gap:8px; pointer-events:none;' }, [
    el('div', { style: 'font-size:1rem; font-weight:600; color:var(--text-2);', text: 'Drop a photo here' }),
    el('div', { style: 'font-size:.8rem; color:var(--text-3);', text: 'or paste from clipboard (Ctrl+V) or click to browse' }),
  ]);
  const clearImgBtn = el('button', {
    style: 'display:none; position:absolute; top:6px; right:6px; width:24px; height:24px; border-radius:50%; border:none; background:rgba(0,0,0,.65); color:#fff; font-size:15px; line-height:1; cursor:pointer; z-index:2; padding:0;',
    title: 'Clear image', text: '×',
  });
  const dropZone = el('div', { class: 'drop-zone', style: 'position:relative; min-height:160px; display:flex; align-items:center; justify-content:center;' }, [preview, dropHint, clearImgBtn]);
  root.appendChild(dropZone);

  function _autoSelectRatio(imgW, imgH) {
    if (!imgW || !imgH) return;
    const imgR = imgW / imgH;
    const supported = MODEL_RATIOS[_model] || ['16:9'];
    let best = '16:9', bestDiff = Infinity;
    for (const r of RATIOS) {
      if (!supported.includes(r.value)) continue;
      const diff = Math.abs(imgR - r.rw / r.rh);
      if (diff < bestDiff) { bestDiff = diff; best = r.value; }
    }
    if (best === _ratio) return;
    _ratio = best;
    _updateRatioAvailability(); // re-applies all chip styles with new active
  }

  function _resetPromptsForNewImage() {
    if (typeof ideaInput !== 'undefined' && ideaInput) ideaInput.value = '';
    if (typeof lyricInput !== 'undefined' && lyricInput) lyricInput.value = '';
    if (typeof talkReplyEl !== 'undefined' && talkReplyEl) {
      talkReplyEl.textContent = '';
      talkReplyEl.style.display = 'none';
    }
    _chatHistory = [];
  }

  function _applyImage(path, url) {
    if (path !== _imagePath) _resetPromptsForNewImage();
    _imagePath = path;
    preview.src = url;
    preview.style.display = '';
    dropHint.style.display = 'none';
    dropZone.classList.add('drop-zone-loaded');
    clearImgBtn.style.display = '';
    preview.onload = () => _autoSelectRatio(preview.naturalWidth, preview.naturalHeight);
  }
  _applyImageFn = _applyImage;

  clearImgBtn.addEventListener('click', e => {
    e.stopPropagation();
    _imagePath = null;
    preview.src = ''; preview.style.display = 'none';
    dropHint.style.display = '';
    dropZone.classList.remove('drop-zone-loaded', 'drag-over');
    clearImgBtn.style.display = 'none';
    _resetPromptsForNewImage();
  });

  dropZone.addEventListener('click', e => {
    if (preview.contains(e.target) || e.target === preview) return;
    imgInput.click();
  });
  dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
  dropZone.addEventListener('drop', () => dropZone.classList.remove('drag-over'));
  dropZone.addEventListener('drop', async e => {
    e.preventDefault();
    const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('image/'));
    if (!files.length) return;
    try {
      const data = await apiUpload('/api/fun/upload', files);
      const f = data.files?.[0];
      if (f) _applyImage(f.path, f.url || pathToUrl(f.path));
    } catch (err) { toast(err.message, 'error'); }
  });
  imgInput.addEventListener('change', async () => {
    if (!imgInput.files?.length) return;
    try {
      const data = await apiUpload('/api/fun/upload', Array.from(imgInput.files));
      const f = data.files?.[0];
      if (f) _applyImage(f.path, f.url || pathToUrl(f.path));
    } catch (err) { toast(err.message, 'error'); }
    imgInput.value = '';
  });

  async function _pasteImage(e) {
    const active = document.activeElement;
    if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable)) return;
    const items = Array.from(e.clipboardData?.items || []);
    const imgItem = items.find(it => it.type.startsWith('image/'));
    if (!imgItem) return;
    e.preventDefault();
    const file = imgItem.getAsFile();
    if (!file) return;
    try {
      const data = await apiUpload('/api/fun/upload', [file]);
      const f = data.files?.[0];
      if (f) _applyImage(f.path, f.url || pathToUrl(f.path));
    } catch (err) { toast(err.message, 'error'); }
  }
  document.addEventListener('paste', _pasteImage);

  // ── Idea + Lyric direction ────────────────────────────────────────────────
  const ideaInput = el('textarea', {
    rows: '3',
    style: 'width:100%; resize:vertical; font-size:.95rem;',
    placeholder: 'What should happen? Mood, action, style -- or click Spark to generate from your photo.',
  });
  const lyricInput = el('input', {
    type: 'text',
    style: 'width:100%; font-size:.82rem;',
    placeholder: 'e.g. "gypsy folk, raw vocals" | "dark cabaret wit" | "dreamy indie, wistful"',
  });

  // Shared brainstorm call — updates fields, returns {idea, lyric_direction, reply}
  let _chatHistory = [];
  async function _brainstorm(message, { ideaOnly = false, lyricOnly = false } = {}) {
    const body = {
      image_path:    _imagePath || '',
      message,
      history:       _chatHistory.slice(-8),
      current_idea:  ideaInput.value.trim(),
      current_lyric: lyricInput.value.trim(),
    };
    const data = await apiFetch('/api/fun/brainstorm', {
      method: 'POST', body: JSON.stringify(body), context: 'express.brainstorm',
    });
    const updated = (data.idea && !lyricOnly) || (data.lyric_direction && !ideaOnly);
    if (data.idea            && !lyricOnly) ideaInput.value  = data.idea;
    if (data.lyric_direction && !ideaOnly)  lyricInput.value = data.lyric_direction;
    if (!updated && data.reply) toast(data.reply, 'info');  // LLM responded but changed nothing
    _chatHistory.push({ role: 'user', content: message });
    _chatHistory.push({ role: 'assistant', content: data.reply || '' });
    return data;
  }

  function _genBtn(title) {
    return el('button', {
      style: 'flex-shrink:0; font-size:.72rem; padding:2px 7px; border:1px solid var(--border-2); border-radius:5px; background:transparent; color:var(--accent); cursor:pointer; white-space:nowrap;',
      title, text: '✦ Generate',
    });
  }

  const lyricGenBtn = _genBtn('Regenerate music vibe from image using AI');

  // Returns a stop() fn -- call it when the async work is done.
  function _btnThinking(btn) {
    const orig = btn.textContent;
    btn.disabled = true;
    const frames = ['Thinking', 'Thinking.', 'Thinking..', 'Thinking...'];
    let i = 0;
    btn.textContent = frames[0];
    const t = setInterval(() => { btn.textContent = frames[i = (i + 1) % frames.length]; }, 380);
    return () => { clearInterval(t); btn.disabled = false; btn.textContent = orig; };
  }

  async function _runGen(btn, action, targetEl) {
    const stop = _btnThinking(btn);
    if (targetEl) targetEl.classList.add('ai-generating');
    try { await _brainstorm(action, { ideaOnly: action.includes('idea'), lyricOnly: action.includes('lyric') }); }
    catch (e) { toast(e.message, 'error'); }
    finally { stop(); if (targetEl) targetEl.classList.remove('ai-generating'); }
  }
  lyricGenBtn.addEventListener('click', () => _runGen(lyricGenBtn, 'Generate a brief lyric direction for music that matches this image', lyricInput));

  // ── Creative brief ────────────────────────────────────────────────────────
  const talkReplyEl = el('div', { style: 'display:none; font-size:.78rem; color:var(--text-3); line-height:1.5; font-style:italic;' });

  const sparkBtn = el('button', {
    style: 'flex-shrink:0; font-size:.78rem; padding:4px 11px; border:1px solid var(--accent); border-radius:6px; background:transparent; color:var(--accent); cursor:pointer; white-space:nowrap; font-weight:600;',
    title: 'Auto-fill idea and music vibe from your photo using AI',
    text: '✶ Spark from photo',
  });

  sparkBtn.addEventListener('click', async () => {
    const existingIdea = ideaInput.value.trim();
    const stop = _btnThinking(sparkBtn);
    talkReplyEl.style.display = 'none';
    [ideaInput, lyricInput].forEach(f => f.classList.add('ai-generating'));
    try {
      const msg = existingIdea
        ? existingIdea
        : 'Create a fun, high-energy video: describe dramatic physical movement or a wild transformation happening to the subject. ' +
          'Also write a lyric direction for a song with real character and personality -- pick a style that actually fits the image: ' +
          'could be gypsy punk energy, dark cabaret wit, dreamy folk, raw punk, world music, or anything with a distinctive voice. ' +
          'Avoid generic upbeat pop. Real sung lyrics with something to say, never instrumental.';
      const data = await _brainstorm(msg);
      if (data.reply) {
        talkReplyEl.textContent = `AI: ${data.reply}`;
        talkReplyEl.style.display = '';
      }
    } catch (e) { toast(e.message, 'error'); }
    finally {
      stop();
      [ideaInput, lyricInput].forEach(f => f.classList.remove('ai-generating'));
    }
  });

  root.appendChild(el('div', { class: 'card', style: 'padding:14px; display:flex; flex-direction:column; gap:10px;' }, [
    el('div', { style: 'display:flex; align-items:center; justify-content:space-between;' }, [
      el('div', { style: 'font-size:.75rem; color:var(--text-3); text-transform:uppercase; letter-spacing:.06em;', text: 'Creative brief' }),
      sparkBtn,
    ]),
    ideaInput,
    el('div', { style: 'display:flex; flex-direction:column; gap:4px;' }, [
      el('div', { style: 'display:flex; align-items:center; justify-content:space-between;' }, [
        el('div', { style: 'font-size:.75rem; color:var(--text-3);', text: 'Music vibe' }),
        lyricGenBtn,
      ]),
      lyricInput,
    ]),
    talkReplyEl,
  ]));

  // ── Output settings ───────────────────────────────────────────────────────
  const CHIP_BASE = 'border:1px solid var(--border-2); border-radius:6px; padding:4px 10px; font-size:.78rem; cursor:pointer; background:transparent; color:var(--text-2); transition:background .15s,color .15s;';
  const CHIP_ON   = 'background:var(--accent); border-color:var(--accent); color:#000; font-weight:600;';

  function _makeChipGroup(items, activeVal, onPick) {
    const row = el('div', { style: 'display:flex; gap:6px; flex-wrap:wrap;' });
    const chips = {};
    for (const item of items) {
      const btn = el('button', { style: CHIP_BASE + (item.value === activeVal ? CHIP_ON : ''), text: item.label, title: item.title || '' });
      btn.addEventListener('click', () => {
        Object.entries(chips).forEach(([v, b]) => b.setAttribute('style', CHIP_BASE + (v === item.value ? CHIP_ON : '')));
        onPick(item);
      });
      chips[item.value] = btn;
      row.appendChild(btn);
    }
    return { row, chips };
  }

  const dimsLabel = el('span', { style: 'font-size:.82rem; color:var(--accent); font-weight:600;', text: '1032 × 580' });
  _dimsLabel = dimsLabel;
  const warnEl = el('div', {
    style: 'display:none; font-size:.75rem; color:var(--accent-warm, #e8a000); background:rgba(232,160,0,.1); border:1px solid rgba(232,160,0,.3); border-radius:6px; padding:6px 10px;',
    text: 'HD quality requires a lot of VRAM and takes 10-20 min per clip. Consider Fast or Quality for a quicker result.',
  });
  _warnEl = warnEl;

  const { row: ratioRow, chips: _rChips } = _makeChipGroup(
    RATIOS.map(r => ({ label: r.label, value: r.value })),
    _ratio,
    item => { _ratio = item.value; _refreshOutput(); },
  );
  _ratioChips = _rChips;

  // Duration slider — declared here so quality chip handler can update it
  const tierMax0  = QUALITIES.find(q => q.px === _qualityPx)?.maxSec || 20;
  const durSlider = el('input', { type: 'range', min: '1', max: String(tierMax0), value: String(_duration), step: '1', style: 'flex:1; accent-color:var(--accent);' });
  const durLabel  = el('span', { style: 'font-size:.82rem; color:var(--accent); font-weight:600; min-width:30px; text-align:right;', text: `${_duration}s` });
  durSlider.addEventListener('input', () => {
    _duration = parseInt(durSlider.value);
    durLabel.textContent = `${_duration}s`;
  });

  // Creativity (guidance_scale) slider — initial value set by _applyModelDefaults below
  let _guidance = MODEL_DEFAULTS[_model]?.guidance ?? 3.0;
  const guidSlider = el('input', { type: 'range', min: '1', max: '20', value: String(_guidance), step: '0.5', style: 'flex:1; accent-color:var(--accent);' });
  const guidLabel  = el('span', { style: 'font-size:.82rem; color:var(--accent); font-weight:600; min-width:30px; text-align:right;', text: String(_guidance) });
  guidSlider.addEventListener('input', () => {
    _guidance = parseFloat(guidSlider.value);
    guidLabel.textContent = String(_guidance);
  });

  // Steps slider
  let _steps = MODEL_DEFAULTS[_model]?.steps ?? 8;
  const stepsSlider = el('input', { type: 'range', min: '4', max: '50', value: String(_steps), step: '1', style: 'flex:1; accent-color:var(--accent);' });
  const stepsLabel  = el('span', { style: 'font-size:.82rem; color:var(--accent); font-weight:600; min-width:30px; text-align:right;', text: String(_steps) });
  stepsSlider.addEventListener('input', () => {
    _steps = parseInt(stepsSlider.value);
    stepsLabel.textContent = String(_steps);
  });

  function _applyModelDefaults(modelName) {
    const d = MODEL_DEFAULTS[modelName];
    if (!d) return;
    const tierMax = QUALITIES.find(q => q.px === _qualityPx)?.maxSec || 20;
    _guidance = d.guidance;
    guidSlider.value = String(_guidance);
    guidLabel.textContent = String(_guidance);
    _steps = d.steps;
    stepsSlider.value = String(_steps);
    stepsLabel.textContent = String(_steps);
    _duration = Math.min(d.duration, tierMax);
    durSlider.value = String(_duration);
    durLabel.textContent = `${_duration}s`;
  }

  const { row: qualRow } = _makeChipGroup(
    QUALITIES.map(q => ({ label: q.label, value: String(q.px), title: q.hint || '' })),
    String(_qualityPx),
    item => {
      _qualityPx = Number(item.value);
      _model     = _preferredModel(_qualityPx);
      const tierMax = QUALITIES.find(q => q.px === _qualityPx)?.maxSec || 20;
      durSlider.max = String(tierMax);
      _applyModelDefaults(_model);
      _updateRatioAvailability();
      _announceModel();
    },
  );

  const ratioHintEl = el('div', {
    style: 'display:none; font-size:.72rem; color:var(--text-3); padding-top:2px;',
    text: 'Portrait, square & 4:3 ratios only available with LTX Fast quality',
  });
  _ratioHint = ratioHintEl;

  const advancedInner = el('div', { style: 'display:flex; flex-direction:column; gap:10px; padding-top:10px; border-top:1px solid var(--border-2); margin-top:4px;' }, [
    el('div', { style: 'display:flex; align-items:center; gap:10px; flex-wrap:wrap;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Quality' }),
      qualRow,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:10px; flex-wrap:wrap;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Aspect ratio' }),
      ratioRow,
    ]),
    ratioHintEl,
    el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Guidance' }),
      guidSlider,
      guidLabel,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Steps' }),
      stepsSlider,
      stepsLabel,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:6px; padding-top:2px;' }, [
      el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'Output:' }),
      dimsLabel,
    ]),
    warnEl,
  ]);
  root.appendChild(el('div', { class: 'card', style: 'padding:12px 14px; display:flex; flex-direction:column; gap:10px;' }, [
    el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Duration' }),
      durSlider,
      durLabel,
    ]),
    el('details', {}, [
      el('summary', { style: 'cursor:pointer; font-size:.75rem; color:var(--text-3); user-select:none; padding:4px 0; outline:none;', text: 'Advanced settings' }),
      advancedInner,
    ]),
  ]));

  // ── Multi-video story ─────────────────────────────────────────────────────
  let _multiVideo = false;
  let _numClips   = 4;

  const multiChk = el('input', { type: 'checkbox', id: 'express-multi-video', style: 'cursor:pointer; width:15px; height:15px; flex-shrink:0;' });

  const clipsSlider = el('input', { type: 'range', min: '2', max: '8', value: '4', step: '1', style: 'flex:1; accent-color:var(--accent);' });
  const clipsLabel  = el('span', { style: 'font-size:.82rem; color:var(--accent); font-weight:600; min-width:20px; text-align:right;', text: '4' });
  const totalLabel  = el('span', { style: 'font-size:.82rem; color:var(--accent); font-weight:600;', text: '~32s' });

  function _refreshMultiTotal() {
    totalLabel.textContent = `~${_numClips * _duration}s`;
  }
  clipsSlider.addEventListener('input', () => {
    _numClips = parseInt(clipsSlider.value);
    clipsLabel.textContent = String(_numClips);
    _refreshMultiTotal();
  });
  // Keep total in sync when clip-length slider changes
  durSlider.addEventListener('input', _refreshMultiTotal);

  const multiSettings = el('div', { style: 'display:none; flex-direction:column; gap:10px; margin-top:10px; padding-top:10px; border-top:1px solid var(--border-2);' });
  multiSettings.appendChild(el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
    el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Clips' }),
    clipsSlider,
    clipsLabel,
  ]));
  multiSettings.appendChild(el('div', { style: 'display:flex; align-items:center; gap:6px; flex-wrap:wrap;' }, [
    el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'Total story length:' }),
    totalLabel,
    el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: '(clip length × clips)' }),
  ]));
  multiSettings.appendChild(el('div', {
    style: 'font-size:.73rem; color:var(--text-3); line-height:1.5; padding:4px 0;',
    text: 'AI writes a story arc, each clip starts from the last frame of the previous one. Audio is generated once over the whole piece.',
  }));

  const multiCard = el('div', { class: 'card', style: 'padding:12px 14px;' }, [
    el('div', { style: 'display:flex; align-items:center; gap:8px;' }, [
      multiChk,
      el('label', {
        for: 'express-multi-video',
        style: 'font-size:.88rem; font-weight:600; cursor:pointer; user-select:none; color:var(--text);',
        text: 'Multi-video story',
      }),
      el('span', { style: 'font-size:.74rem; color:var(--text-3);', text: '— chain clips into a narrative' }),
    ]),
    multiSettings,
  ]);
  root.appendChild(multiCard);

  multiChk.addEventListener('change', () => {
    _multiVideo = multiChk.checked;
    multiSettings.style.display = _multiVideo ? 'flex' : 'none';
    loopBtn.style.display = _multiVideo ? 'none' : '';
    if (!_looping) {
      createBtn.textContent = _multiVideo ? 'Create Story' : (_pendingCount > 0 ? '＋ Add to Queue' : 'Create');
    }
    _refreshMultiTotal();
  });

  // ── Loop state ────────────────────────────────────────────────────────────
  let _looping    = false;
  let _varyPrompt = false;
  let _loopCount  = 0;

  // ── Queue-depth tracking for Create button ────────────────────────────────
  let _pendingCount = 0;
  function _refreshCreateBtn() {
    createBtn.disabled = false;
    if (_multiVideo) {
      createBtn.textContent = 'Create Story';
    } else {
      createBtn.textContent = _pendingCount > 0 ? '＋ Add to Queue' : 'Create';
    }
  }
  function _trackDone(job_id) {
    const done = () => { _pendingCount = Math.max(0, _pendingCount - 1); _refreshCreateBtn(); };
    pollJob(job_id, null, done, done);
  }

  // ── Create + Loop button row ──────────────────────────────────────────────
  const createBtn = el('button', {
    class: 'btn btn-primary btn-generate',
    text: 'Create',
    style: 'flex:1; font-size:1.1rem; padding:14px; font-weight:700; letter-spacing:.04em;',
  });
  const loopBtn = el('button', {
    class: 'btn',
    text: '∞  Loop',
    title: 'Generate continuously until stopped',
    style: 'font-size:.95rem; padding:14px 18px; white-space:nowrap;',
  });
  root.appendChild(el('div', { style: 'display:flex; gap:8px;' }, [createBtn, loopBtn]));

  // Vary-prompt toggle (only visible when loop is active)
  const varyChk   = el('input', { type: 'checkbox', id: 'express-vary-prompt', style: 'cursor:pointer;' });
  const varyRow   = el('div', {
    style: 'display:none; align-items:center; gap:6px; padding:0 2px;',
  }, [
    varyChk,
    el('label', { for: 'express-vary-prompt', style: 'font-size:.76rem; color:var(--text-3); cursor:pointer; user-select:none;', text: 'Vary prompt each time (AI generates slight variation)' }),
  ]);
  root.appendChild(varyRow);
  varyChk.addEventListener('change', () => { _varyPrompt = varyChk.checked; });

  // ── Progress + result area ────────────────────────────────────────────────
  const progressWrap = el('div', { style: 'display:none; flex-direction:column; gap:8px;' });
  const progressBar  = el('div', { style: 'height:4px; background:var(--border-2); border-radius:2px; overflow:hidden;' });
  const progressFill = el('div', { style: 'height:100%; width:0%; background:var(--accent); border-radius:2px; transition:width .4s;' });
  progressBar.appendChild(progressFill);
  const progressMsg  = el('div', { style: 'font-size:.8rem; color:var(--text-3); text-align:center;' });
  progressWrap.appendChild(progressBar);
  progressWrap.appendChild(progressMsg);
  root.appendChild(progressWrap);

  const resultWrap = el('div', { style: 'display:none; flex-direction:column; gap:8px;' });
  const resultVideo = el('video', { controls: '', style: 'width:100%; border-radius:8px; background:#000;' });
  const resultActions = el('div', { style: 'display:flex; gap:8px; justify-content:center; align-items:center; flex-wrap:wrap;' });
  const newBtn  = el('button', { class: 'btn btn-sm', text: '+ New video' });
  const sendBtn = el('button', { class: 'btn btn-sm', text: 'Open in Create Videos →' });
  const muteBtn = el('button', { class: 'btn btn-sm', text: '🔊 Mute', title: 'Toggle mute' });
  muteBtn.addEventListener('click', () => {
    resultVideo.muted = !resultVideo.muted;
    muteBtn.textContent = resultVideo.muted ? '🔇 Unmute' : '🔊 Mute';
  });
  resultActions.appendChild(newBtn);
  resultActions.appendChild(sendBtn);
  resultActions.appendChild(muteBtn);
  resultWrap.appendChild(resultVideo);
  resultWrap.appendChild(resultActions);
  root.appendChild(resultWrap);

  function _showProgress(pct, msg) {
    progressWrap.style.display    = 'flex';
    resultWrap.style.display      = 'none';
    progressFill.style.width      = `${pct}%`;
    progressFill.style.background = 'var(--accent)';
    progressMsg.style.color       = 'var(--text-3)';
    progressMsg.textContent       = msg || '';
  }

  function _showResult(videoPath) {
    progressWrap.style.display = 'none';
    resultWrap.style.display   = 'flex';
    resultVideo.src = pathToUrl(videoPath);
    resultVideo.load();
  }

  function _showError(msg) {
    progressWrap.style.display = 'flex';
    progressFill.style.width   = '100%';
    progressFill.style.background = 'var(--red, #c41e3a)';
    progressMsg.style.color    = 'var(--red, #c41e3a)';
    progressMsg.textContent    = `Failed: ${msg}`;
  }

  function _hideProgress() {
    progressWrap.style.display = 'none';
    progressFill.style.background = 'var(--accent)';
    progressMsg.style.color    = 'var(--text-3)';
  }

  newBtn.addEventListener('click', () => {
    resultWrap.style.display = 'none';
    resultVideo.src = '';
  });
  sendBtn.addEventListener('click', () => {
    const src = resultVideo.src ? resultVideo.src.replace(location.origin, '') : null;
    if (src) handoff('create-videos', { type: 'video', path: src, url: src });
    document.querySelector('.rail-tab[data-tab="create-videos"]')?.click();
  });

  // Subtle escape hatch to the full Create Videos tab
  const advLink = el('div', { style: 'text-align:center; margin-top:-10px;' });
  const advBtn  = el('button', {
    style: 'background:none; border:none; cursor:pointer; font-size:.75rem; color:var(--text-3); padding:4px 8px;',
    text: 'Want more control?  Open Create Videos →',
  });
  advLink.appendChild(advBtn);
  root.appendChild(advLink);
  advBtn.addEventListener('click', () => {
    if (_imagePath) handoff('create-videos', { type: 'image', path: _imagePath, url: pathToUrl(_imagePath) });
    document.querySelector('.rail-tab[data-tab="create-videos"]')?.click();
  });

  let _jobId = null;
  let _activePoller = null;  // stop() handle — ensures only one watcher runs at a time

  function _reset() {
    _stopLoop();
    _jobId = null; _imagePath = null;
    _loopCount = 0; _chatHistory = [];
    preview.style.display = 'none'; preview.src = '';
    dropHint.style.display = '';
    clearImgBtn.style.display = 'none';
    dropZone.classList.remove('drop-zone-loaded', 'drag-over');
    ideaInput.value = '';
    lyricInput.value = '';
    talkReplyEl.style.display = 'none';
    _hideProgress();
    resultWrap.style.display = 'none';
    resultVideo.src = '';
    createBtn.disabled = false;
  }

  // ── Core generation ───────────────────────────────────────────────────────
  // Returns Promise<boolean> — true = queued/success, false = failure
  async function _generateOne(fromLoop = false) {
    // Optionally vary the prompt on loop iterations
    if (fromLoop && _varyPrompt && _loopCount > 1 && _imagePath) {
      try {
        await _brainstorm('Create a slightly different variation — same subject and energy but change the action, timing, or camera movement');
      } catch (_) {}
    }

    let motionPrompt = ideaInput.value.trim();
    const needIdea   = !motionPrompt;
    const needLyric  = !lyricInput.value.trim();

    if (needIdea && needLyric && _imagePath) {
      // Both blank + image — one brainstorm call fills both with energy
      _showProgress(3, 'AI is reading your photo...');
      try {
        await _brainstorm(
          'Create a fun, high-energy video: describe dramatic physical movement or a wild transformation happening to the subject. ' +
          'Also write a lyric direction for a song with real character and personality -- pick a style that actually fits the image: ' +
          'could be gypsy punk energy, dark cabaret wit, dreamy folk, raw punk, world music, or anything with a distinctive voice. ' +
          'Avoid generic upbeat pop. Real sung lyrics with something to say, never instrumental.'
        );
        motionPrompt = ideaInput.value.trim();
      } catch (_) {}
    } else {
      if (needIdea) {
        _showProgress(3, 'AI is writing your video idea...');
        try {
          const data = await api('/api/fun/generate-prompts', {
            method: 'POST',
            body: JSON.stringify({
              image_path: _imagePath, num_prompts: 1, creativity: 9, max_tokens: 400,
              user_direction: 'explosive physical action — subject must be actively moving and doing something dramatic',
            }),
          });
          const p = data.prompts?.[0];
          motionPrompt = (typeof p === 'string' ? p : p?.prompt) || '';
        } catch (_) {}
        if (!motionPrompt) motionPrompt = 'Subject erupts into motion, energy bursts through the frame';
        ideaInput.value = motionPrompt;
      } else {
        motionPrompt = ideaInput.value.trim();
      }
      // Always generate a lyric direction when missing — bare ideas produce flat music
      if (needLyric) {
        _showProgress(8, 'AI is picking your music vibe...');
        try {
          await _brainstorm(
            'Write a lyric direction for a song with real character and personality that matches this video: ' + motionPrompt + '. ' +
            'Pick a style that actually fits -- gypsy punk, dark cabaret, dreamy indie, raw punk, folk, world music, ' +
            'or whatever has a distinctive voice. Avoid generic pop. Real sung lyrics with something to say, never instrumental.',
            { lyricOnly: true }
          );
        } catch (_) {}
      }
    }

    try {
      const { job_id } = await api('/api/fun/make-it', {
        method: 'POST',
        body: JSON.stringify({
          photo_path: _imagePath, video_prompt: motionPrompt, music_prompt: '',
          lyric_direction: lyricInput.value.trim(), user_direction: 'character-driven, specific energy, not generic',
          model: _model, duration: _duration,
          steps: _steps, guidance: _guidance, seed: -1, skip_audio: false, instrumental: false,
          output_width: _outW, output_height: _outH,
        }),
      });
      _jobId = job_id;
      document.dispatchEvent(new CustomEvent('job-queued', { detail: { job_id } }));

      // Start watching in the background — does NOT block the caller.
      // The button re-enables immediately; user can queue more jobs while this runs.
      _watchJob(job_id);
      return true;
    } catch (e) {
      if (e.status === 429 || /queue.*full|full.*queue/i.test(e.message)) {
        toast('Queue is full — wait for the current video to finish', 'error');
      } else {
        toast(e.message, 'error');
      }
      return false;
    }
  }

  // Watch a job and update the progress/result area.
  // Returns a Promise so loop mode can await completion; single-run ignores the return value.
  // Cancels any previously running watcher so only one job's progress shows at a time.
  function _watchJob(job_id) {
    if (_activePoller) { _activePoller.stop(); _activePoller = null; }
    _showProgress(2, 'Added to queue…');
    return new Promise(resolve => {
      const poller = pollJob(job_id,
        j => {
          if (j.status === 'queued') {
            const pos = j.queue_position;
            const label = pos === 0 ? 'up next' : `position ${pos + 1}`;
            _showProgress(2, `In queue — ${label}…`);
          } else {
            const pct = j.progress || 5;
            _showProgress(pct, j.message || `${pct}%`);
          }
        },
        j => {
          _activePoller = null;
          const outputs = Array.isArray(j.output) ? j.output : [j.output];
          const best = outputs.length > 1 ? outputs[1] : outputs[0];
          if (best) _showResult(best);
          else _hideProgress();
          resolve(true);
        },
        err => { _activePoller = null; _showError(err); resolve(false); },
      );
      _activePoller = poller;
    });
  }

  // ── Loop runner ───────────────────────────────────────────────────────────
  async function _runLoop() {
    while (_looping) {
      _loopCount++;
      // _generateOne returns as soon as the job is submitted; await the watch promise for completion
      const submitted = await _generateOne(true);
      if (!submitted) { _stopLoop(); toast('Loop stopped — failed to submit job', 'error'); break; }
      const ok = await _watchJob(_jobId);
      if (!ok) { _stopLoop(); toast('Loop stopped — generation failed', 'error'); break; }
      if (!_looping) break;
      // Brief pause so the user can see the result before next run kicks in
      await new Promise(r => setTimeout(r, 2500));
    }
    if (!_looping) {
      loopBtn.textContent = '∞  Loop';
      loopBtn.classList.remove('btn-primary');
      varyRow.style.display = 'none';
    }
  }

  function _stopLoop() {
    _looping = false;
    loopBtn.textContent = '∞  Loop';
    loopBtn.classList.remove('btn-primary');
    varyRow.style.display = 'none';
  }

  loopBtn.addEventListener('click', async () => {
    if (_looping) {
      _stopLoop();
      if (_jobId) stopJob(_jobId).catch(() => {});
      toast('Loop stopped', 'info');
    } else {
      if (!_imagePath && !ideaInput.value.trim()) { toast('Drop an image or type a video idea first', 'error'); return; }
      _looping = true;
      _loopCount = 0;
      loopBtn.textContent = '■  Stop';
      loopBtn.classList.add('btn-primary');
      varyRow.style.display = 'flex';
      _runLoop();
    }
  });

  // ── Multi-video generation ────────────────────────────────────────────────
  async function _generateMulti() {
    let motionPrompt = ideaInput.value.trim();

    // Auto-generate a story concept if the idea field is blank
    if (!motionPrompt && _imagePath) {
      try {
        await _brainstorm(
          'Create a cinematic story concept for a multi-clip video: ' +
          'describe the overall narrative arc and what action happens across several scenes. Keep it dramatic and visual.'
        );
        motionPrompt = ideaInput.value.trim();
      } catch (_) {}
    }
    if (!motionPrompt) {
      toast('Type a story idea or drop an image first', 'error');
      return false;
    }

    // Auto-generate lyric direction if blank
    if (!lyricInput.value.trim()) {
      try {
        await _brainstorm(
          'Write a lyric direction for a song that spans a multi-clip cinematic story: ' + motionPrompt,
          { lyricOnly: true }
        );
      } catch (_) {}
    }

    try {
      const { job_id } = await api('/api/fun/make-it-multi', {
        method: 'POST',
        body: JSON.stringify({
          photo_path:      _imagePath,
          video_prompt:    motionPrompt,
          music_prompt:    '',
          lyric_direction: lyricInput.value.trim(),
          user_direction:  'cinematic narrative, story continuity, dramatic',
          model:           _model,
          clip_duration:   _duration,
          num_clips:       _numClips,
          steps:           _steps,
          guidance:        _guidance,
          seed:            -1,
          skip_audio:      false,
          instrumental:    false,
          output_width:    _outW,
          output_height:   _outH,
        }),
      });
      _jobId = job_id;
      document.dispatchEvent(new CustomEvent('job-queued', { detail: { job_id } }));
      _watchJob(job_id);
      return true;
    } catch (e) {
      if (e.status === 429 || /queue.*full|full.*queue/i.test(e.message)) {
        toast('Queue is full — wait for the current video to finish', 'error');
      } else {
        toast(e.message, 'error');
      }
      return false;
    }
  }

  // ── Create (single run or multi-video) ───────────────────────────────────
  createBtn.addEventListener('click', async () => {
    if (!_imagePath && !ideaInput.value.trim()) {
      dropZone.style.borderColor = 'var(--red)';
      setTimeout(() => { if (!_imagePath) dropZone.style.borderColor = 'var(--border-2)'; }, 2000);
      toast('Drop an image or type a video idea first', 'error');
      return;
    }
    _loopCount = 0;
    _showProgress(1, 'Getting started...');
    const submitted = _multiVideo ? await _generateMulti() : await _generateOne(false);
    if (submitted && _jobId) {
      _pendingCount++;
      _refreshCreateBtn();
      _trackDone(_jobId);
    }
  });
}
