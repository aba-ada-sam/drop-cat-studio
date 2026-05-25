/**
 * Drop Cat Go Studio -- Express mode.
 * Drop an image, describe your idea, click Create. Everything else is automatic.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js?v=20260505e';
import { el, pathToUrl } from './components.js?v=20260507a';
import { toast, apiFetch } from './shell/toast.js?v=20260518a';
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

  // -- Output resolution state -----------------------------------------------
  const RATIOS = [
    { label: '16:9', value: '16:9', rw: 16, rh: 9  },
    { label: '9:16', value: '9:16', rw: 9,  rh: 16 },
    { label: '1:1',  value: '1:1',  rw: 1,  rh: 1  },
    { label: '4:3',  value: '4:3',  rw: 4,  rh: 3  },
    { label: '3:4',  value: '3:4',  rw: 3,  rh: 4  },
  ];
  // Renamed from Fast/Quality/HD because those labels implied a monotonic
  // spectrum (higher = better) and novices picked Quality expecting strict
  // improvement. In reality each model has a different motion character:
  //   Photo Mood -- LTX Distilled, best for calm/still subjects, ~1-2 min
  //   Action     -- Wan I2V 480P, best for kinetic motion, ~5-15 min
  //   Action HD  -- Wan I2V 720P, same as Action but 720p, ~10-20 min
  const QUALITIES = [
    { id: 'fast',    label: 'Photo Mood', px: 480, model: 'LTX-2 Dev19B Distilled', maxSec: 20, hint: 'LTX-2 -- calm/still subjects, breathing-photograph style, ~1-2 min per clip, any aspect ratio' },
    { id: 'quality', label: 'Action',     px: 480, model: 'Wan2.1-I2V-14B-480P',    maxSec: 16, hint: 'Wan2.1 480P -- kinetic motion, action shots, dramatic verbs, ~5-15 min per clip' },
    { id: 'hd',      label: 'Action HD',  px: 720, model: 'Wan2.1-I2V-14B-720P',    maxSec: 12, hint: 'Wan2.1 720P -- same as Action but 720p delivery quality, ~10-20 min per clip' },
  ];

  // Which ratios each model natively supports well.
  // LTX-2 was trained on variable aspect ratios; Wan models are 16:9 only.
  const MODEL_RATIOS = {
    'LTX-2 Dev19B Distilled': ['16:9', '9:16', '1:1', '4:3', '3:4'],
    'LTX-2 Dev13B':           ['16:9', '9:16', '1:1', '4:3', '3:4'],
  };

  // Per-model optimal defaults. These are a fallback only -- the live values
  // come from /api/fun/models (server-side MODELS in video_generator.py) and
  // override these the moment the API responds. Keep these in sync as a safety
  // net for the brief moment between UI build and the API call returning.
  // LTX-2 Distilled: distilled two-stage schedule, 4-8 steps optimal (>8 regresses).
  // LTX-2 Dev13B: full schedule, 40 steps optimal (25 undersamples).
  // Wan I2V: 80-100 frames at 16fps = 5-6s sweet spot, 25 steps standard.
  const MODEL_DEFAULTS = {
    'LTX-2 Dev19B Distilled': { steps: 6,  guidance: 3.0, duration: 8 },
    'LTX-2 Dev13B':           { steps: 40, guidance: 3.5, duration: 6 },
    'Wan2.1-I2V-14B-480P':    { steps: 25, guidance: 4.5, duration: 6 },
    'Wan2.1-I2V-14B-720P':    { steps: 25, guidance: 4.5, duration: 6 },
    'Wan2.1-T2V-14B':         { steps: 25, guidance: 5.5, duration: 6 },
    'Wan2.1-T2V-1.3B':        { steps: 20, guidance: 5.0, duration: 6 },
  };
  let _model      = 'LTX-2 Dev19B Distilled';
  let _duration   = 8;
  let _allModels  = {};
  let _ratio      = '16:9';
  let _qualityId  = 'fast';
  let _qualityPx  = 480;
  let _outW       = 864;
  let _outH       = 480;
  // Auto-pick: when on (default for Express), the server classifies the idea
  // and picks the best model behind the scenes. Manual chip selection is
  // disabled visually so users see what's happening. Toggle off to manual.
  let _autoPick   = true;

  function _computeDims(ratioStr, qualityPx) {
    const [rw, rh] = ratioStr.split(':').map(Number);
    let w, h;
    if (rw === rh)      { w = h = qualityPx; }
    else if (rh > rw)   { w = qualityPx; h = Math.round(qualityPx * rh / rw); }
    else                 { h = qualityPx; w = Math.round(qualityPx * rw / rh); }
    // Round to nearest multiple of 32 -- LTX-Video VAE requires 32-pixel alignment.
    // Wan models also benefit; ensures no partial-stride artifacts.
    const r32 = n => Math.max(32, Math.round(n / 32) * 32);
    return [r32(w), r32(h)];
  }

  function _preferredModel() {
    const pref = QUALITIES.find(q => q.id === _qualityId)?.model;
    return (pref && _allModels[pref]) ? pref : _model;
  }

  // Placeholders filled in when the UI section is built
  let _dimsLabel       = null;
  let _warnEl          = null;
  let _ratioHint       = null;
  let _ratioChips      = {};
  let _qualVramWarnEl  = null;

  function _refreshOutput() {
    [_outW, _outH] = _computeDims(_ratio, _qualityPx);
    if (_dimsLabel) _dimsLabel.textContent = `${_outW} x ${_outH}`;
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

  let _gpuVramGb = 0;
  api('/api/fun/models').then(data => {
    _allModels = data.models || {};
    _gpuVramGb = data.gpu_vram_gb || 0;
    const models = Object.entries(_allModels);
    if (models.length) {
      const pref = _preferredModel();
      _model = pref || models[0][0];
      _applyModelDefaults(_model);
    }
    _updateRatioAvailability();
    _announceModel();
    _updateQualityVramHints();
  }).catch(() => toast('Could not load video models -- using defaults', 'error'));

  function _updateQualityVramHints() {
    if (!_gpuVramGb || !_qualVramWarnEl) return;
    const q = QUALITIES.find(q2 => q2.id === _qualityId);
    if (!q) return;
    const modelInfo = _allModels[q.model];
    const needs = modelInfo?.vram_min_gb || 0;
    if (needs && _gpuVramGb < needs) {
      _qualVramWarnEl.textContent = `${q.label} needs ~${needs} GB -- your GPU has ${_gpuVramGb} GB. Generation may be slow or fail.`;
      _qualVramWarnEl.style.display = '';
    } else {
      _qualVramWarnEl.style.display = 'none';
    }
  }

  // -- Heading ---------------------------------------------------------------
  root.appendChild(el('div', { style: 'text-align:center; padding-bottom:4px;' }, [
    el('div', { style: 'font-size:1.4rem; font-weight:700; color:var(--text); margin-bottom:6px;', text: 'Create a video' }),
    el('div', { style: 'font-size:.85rem; color:var(--text-3);', text: 'Drop an image, describe what you want, click Create.' }),
  ]));

  // -- Image drop zone -------------------------------------------------------
  const imgInput = el('input', { type: 'file', accept: 'image/*', style: 'display:none' });
  root.appendChild(imgInput);

  let _imagePath = null;
  const preview = el('img', { style: 'display:none; width:100%; max-height:260px; object-fit:contain; border-radius:8px; background:var(--bg-raised);' });
  // pickFolderLink is an inline action inside the drop zone so users see
  // 'or pick a folder' alongside the single-photo affordance. Clicking it
  // routes through the same prompt+iteration logic as the Loop Folder
  // button at the bottom of the form. pointer-events on dropHint stay
  // 'none' so drags pass through to dropZone; the link gets its own
  // pointer events so it's clickable.
  const pickFolderLink = el('a', {
    href: '#',
    style: 'font-size:.78rem; color:var(--accent); text-decoration:underline; cursor:pointer; pointer-events:auto;',
    text: 'pick a folder of photos',
  });
  const dropHint = el('div', { style: 'display:flex; flex-direction:column; align-items:center; gap:8px; pointer-events:none;' }, [
    el('div', { style: 'font-size:1rem; font-weight:600; color:var(--text-2);', text: 'Drop a photo here' }),
    el('div', { style: 'font-size:.8rem; color:var(--text-3);', text: 'or paste from clipboard (Ctrl+V) or click to browse a file' }),
    el('div', { style: 'font-size:.78rem; color:var(--text-3); margin-top:2px;' }, [
      el('span', { text: 'Working on a batch? ' }),
      pickFolderLink,
      el('span', { text: ' to run one generation per image.' }),
    ]),
  ]);
  const clearImgBtn = el('button', {
    style: 'display:none; position:absolute; top:6px; right:6px; width:24px; height:24px; border-radius:50%; border:none; background:rgba(0,0,0,.65); color:#fff; font-size:15px; line-height:1; cursor:pointer; z-index:2; padding:0;',
    title: 'Clear image', text: 'x',
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
    // Only adopt real server paths into _imagePath. blob: URLs are for the
    // visual preview only -- the server needs a path it can stat(), and any
    // brainstorm/AI call made before the upload completes must wait for the
    // real path (see _uploadInFlight below) instead of silently falling back
    // to text-only mode and hallucinating subjects not in the photo.
    if (path && !path.startsWith('blob:')) _imagePath = path;
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
    if (pickFolderLink.contains(e.target) || e.target === pickFolderLink) return;
    imgInput.click();
  });
  dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
  dropZone.addEventListener('drop', () => dropZone.classList.remove('drag-over'));
  // Instant preview: show local blob URL immediately, upload in background.
  // The file is already on disk; waiting for a server round-trip + a second
  // HTTP fetch of /uploads/... before showing anything looks like dial-up.
  // Tracks the in-flight upload so AI calls (Spark, lyric-gen, etc.) can
  // await the real server path instead of racing with the blob: preview.
  let _uploadInFlight = null;

  async function _handleFile(file) {
    const blobUrl = URL.createObjectURL(file);
    _applyImage(blobUrl, blobUrl);  // visual preview only; _imagePath stays null
    _uploadInFlight = (async () => {
      try {
        const data = await apiUpload('/api/fun/upload', [file]);
        const f = data.files?.[0];
        if (f) _imagePath = f.path;
      } catch (err) {
        toast(err.message, 'error');
      } finally {
        _uploadInFlight = null;
      }
    })();
    return _uploadInFlight;
  }

  dropZone.addEventListener('drop', async e => {
    e.preventDefault();
    const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('image/'));
    if (!files.length) return;
    await _handleFile(files[0]);
  });
  imgInput.addEventListener('change', async () => {
    if (!imgInput.files?.length) return;
    await _handleFile(imgInput.files[0]);
    imgInput.value = '';
  });

  async function _pasteImage(e) {
    if (!panel.offsetParent) return;  // Express tab not visible -- ignore paste
    const active = document.activeElement;
    if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable)) return;
    const items = Array.from(e.clipboardData?.items || []);
    const imgItem = items.find(it => it.type.startsWith('image/'));
    if (!imgItem) return;
    e.preventDefault();
    const file = imgItem.getAsFile();
    if (!file) return;
    await _handleFile(file);
  }
  document.addEventListener('paste', _pasteImage);

  // -- Creative brief inputs + brainstorm -----------------------------------
  const ideaInput = el('textarea', {
    rows: '3',
    style: 'width:100%; resize:vertical; font-size:.95rem;',
    placeholder: 'Describe your video in one or two sentences -- or leave blank and click Spark to let AI write it from your photo.',
  });
  const lyricInput = el('input', {
    type: 'text',
    style: 'width:100%; font-size:.82rem;',
    placeholder: 'e.g. "gypsy folk, raw vocals" | "dark cabaret wit" | "dreamy indie, wistful"',
  });

  // Shared brainstorm call -- updates fields, returns {idea, lyric_direction, reply}
  let _chatHistory = [];
  async function _brainstorm(message, { ideaOnly = false, lyricOnly = false } = {}) {
    // If the user just dropped an image, wait for the upload to finish so
    // the LLM gets the real server path instead of seeing image_path="" and
    // silently falling back to text-only hallucination mode.
    if (_uploadInFlight) await _uploadInFlight;
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
      title, text: '* Generate',
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

  // -- Creative brief --------------------------------------------------------
  const talkReplyEl = el('div', { style: 'display:none; font-size:.78rem; color:var(--text-3); line-height:1.5; font-style:italic;' });

  const sparkBtn = el('button', {
    style: 'flex-shrink:0; font-size:.78rem; padding:4px 11px; border:1px solid var(--accent); border-radius:6px; background:transparent; color:var(--accent); cursor:pointer; white-space:nowrap; font-weight:600;',
    title: 'Auto-fill idea and music vibe from your photo using AI',
    text: '* Spark from photo',
  });

  sparkBtn.addEventListener('click', async () => {
    const existingIdea = ideaInput.value.trim();
    const stop = _btnThinking(sparkBtn);
    talkReplyEl.style.display = 'none';
    [ideaInput, lyricInput].forEach(f => f.classList.add('ai-generating'));
    try {
      const msg = existingIdea
        ? existingIdea
        : 'Look at the photo. Describe ONE specific physical action the visible subject performs, rooted in what is actually shown -- their pose, setting, expression, props. ' +
          'Do NOT invent characters, animals, or props that are not in the image. ' +
          'Do NOT use the words transforms, becomes, reveals, establishes, unfolds, or "the camera". ' +
          'Concrete physical motion only -- something a real camera could capture in 5 seconds. ' +
          'Then write a lyric direction for a song that fits the image -- gypsy punk energy, dark cabaret wit, dreamy folk, raw punk, world music, or any distinctive voice. ' +
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

  // Music vibe is auto-generated from the creative brief if left blank, so
  // it lives behind a "Customize music" expander -- novices never see it.
  const musicVibeBlock = el('details', { style: 'margin-top:2px;' }, [
    el('summary', {
      style: 'cursor:pointer; font-size:.72rem; color:var(--text-3); user-select:none; padding:4px 0; outline:none; list-style:none;',
      text: '+  Customize music vibe (auto-generated if left blank)',
    }),
    el('div', { style: 'display:flex; flex-direction:column; gap:4px; margin-top:6px;' }, [
      el('div', { style: 'display:flex; align-items:center; justify-content:space-between;' }, [
        el('div', { style: 'font-size:.75rem; color:var(--text-3);', text: 'Music vibe' }),
        lyricGenBtn,
      ]),
      lyricInput,
    ]),
  ]);

  // Per-model prompt hints for the Express idea field.
  // Auto-pick resolves a model after submit; hints show before submit based on
  // the quality chip selection (which maps to a likely model).
  const EXPRESS_PROMPT_HINTS = {
    'LTX-2 Dev19B Distilled': 'LTX (fast): describe atmosphere and subtle movement -- mist drifts, light shifts, hair stirs in a gentle breeze. Subject is mostly still. 1-2 sentences.',
    'LTX-2 Dev13B':           'LTX 13B: one clear physical action with visual detail -- "spins sharply, jacket flares, neon reflections blur". 1-2 sentences.',
    'Wan2.1-I2V-14B-480P':    'Wan I2V: strong action verb first -- "Sprints down the alley..." then environment reaction. The more specific the better. 2-3 sentences.',
    'Wan2.1-I2V-14B-720P':    'Wan I2V 720P: strong action verb, fine detail (fabric texture, specific lighting). 2-3 sentences.',
    'Wan2.1-T2V-14B':         'Wan T2V: no image -- describe everything: setting, time of day, subject appearance, action, camera move. 3-4 sentences.',
    'Wan2.1-T2V-1.3B':        'Wan T2V Lite: keep it simple -- one subject, one action, one setting. 1-2 sentences.',
  };

  const ideaHint = el('div', {
    style: 'display:none; font-size:.72rem; color:var(--text-3); line-height:1.5;',
  });

  function _updateExpressHint(modelName) {
    const hint = EXPRESS_PROMPT_HINTS[modelName];
    if (hint) {
      ideaHint.textContent = hint;
      ideaHint.style.display = '';
    } else {
      ideaHint.style.display = 'none';
    }
  }

  root.appendChild(el('div', { class: 'card', style: 'padding:14px; display:flex; flex-direction:column; gap:10px;' }, [
    el('div', { style: 'display:flex; align-items:center; justify-content:space-between;' }, [
      el('div', { style: 'font-size:.75rem; color:var(--text-3); text-transform:uppercase; letter-spacing:.06em;', text: 'Creative brief' }),
      sparkBtn,
    ]),
    ideaInput,
    ideaHint,
    musicVibeBlock,
    talkReplyEl,
  ]));

  // -- Output settings -------------------------------------------------------
  const CHIP_BASE     = 'border:1px solid var(--border-2); border-radius:6px; padding:4px 10px; font-size:.78rem; cursor:pointer; background:transparent; color:var(--text-2); transition:background .15s,color .15s;';
  const CHIP_ON       = 'background:var(--accent); border-color:var(--accent); color:#000; font-weight:600;';
  const CHIP_DISABLED = 'opacity:.3; cursor:not-allowed; pointer-events:none;';

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

  const dimsLabel = el('span', { style: 'font-size:.82rem; color:var(--accent); font-weight:600;', text: '1032 x 580' });
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

  // Duration slider -- declared here so quality chip handler can update it
  const tierMax0  = QUALITIES.find(q => q.id === _qualityId)?.maxSec || 20;
  const durSlider = el('input', { type: 'range', min: '1', max: String(tierMax0), value: String(_duration), step: '1', style: 'flex:1; accent-color:var(--accent);' });
  const durLabel  = el('span', { style: 'font-size:.82rem; color:var(--accent); font-weight:600; min-width:30px; text-align:right;', text: `${_duration}s` });
  durSlider.addEventListener('input', () => {
    _duration = parseInt(durSlider.value);
    durLabel.textContent = `${_duration}s`;
  });

  // Creativity (guidance_scale) slider -- initial value set by _applyModelDefaults below
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
    // Prefer live server values (server-side MODELS dict) over hardcoded
    // fallbacks. The server uses keys: steps, guidance, default_dur.
    const live = _allModels[modelName] || {};
    const fallback = MODEL_DEFAULTS[modelName] || {};
    const steps    = live.steps    ?? fallback.steps;
    const guidance = live.guidance ?? fallback.guidance;
    const duration = live.default_dur ?? fallback.duration;
    if (steps == null && guidance == null && duration == null) return;
    const tierMax = QUALITIES.find(q => q.id === _qualityId)?.maxSec || 20;
    if (guidance != null) {
      _guidance = guidance;
      guidSlider.value = String(_guidance);
      guidLabel.textContent = String(_guidance);
    }
    if (steps != null) {
      _steps = steps;
      stepsSlider.value = String(_steps);
      stepsLabel.textContent = String(_steps);
    }
    if (duration != null) {
      _duration = Math.min(duration, tierMax);
      durSlider.value = String(_duration);
      durLabel.textContent = `${_duration}s`;
    }
    _updateExpressHint(modelName);
  }

  const { row: qualRow } = _makeChipGroup(
    QUALITIES.map(q => ({ label: q.label, value: q.id, title: q.hint || '' })),
    _qualityId,
    item => {
      const q  = QUALITIES.find(q2 => q2.id === item.value);
      _qualityId = q.id;
      _qualityPx = q.px;
      _model     = _preferredModel();
      const tierMax = q.maxSec || 20;
      durSlider.max = String(tierMax);
      _applyModelDefaults(_model);
      _refreshClipInfo();
      _updateRatioAvailability();
      _announceModel();
      _updateQualityVramHints();
    },
  );

  // VRAM warning shown when the selected quality requires more than the detected GPU
  const qualVramWarn = el('div', {
    style: 'display:none; font-size:.72rem; color:#e88; padding-top:2px;',
  });
  _qualVramWarnEl = qualVramWarn;

  const ratioHintEl = el('div', {
    style: 'display:none; font-size:.72rem; color:var(--text-3); padding-top:2px;',
    text: 'Portrait, square & 4:3 ratios only available with LTX Fast quality',
  });
  _ratioHint = ratioHintEl;

  // Auto-pick toggle. When on, the server classifies the user's idea + photo
  // and picks the model+motion silently. Greys out the Quality chips so it's
  // visually clear they're not driving anything.
  const autoPickChk = el('input', {
    type: 'checkbox', id: 'express-auto-pick',
    style: 'cursor:pointer; width:15px; height:15px; flex-shrink:0;',
  });
  autoPickChk.checked = _autoPick;
  const autoPickRow = el('label', {
    for: 'express-auto-pick',
    style: 'display:flex; align-items:center; gap:8px; cursor:pointer; font-size:.78rem; color:var(--text-2);',
    title: 'Let AI pick the best model based on your idea. Turn off to choose manually below.',
  }, [
    autoPickChk,
    el('span', { text: 'Auto-pick best model for my idea' }),
  ]);

  function _applyAutoPickState() {
    qualRow.style.opacity = _autoPick ? '0.4' : '1';
    qualRow.style.pointerEvents = _autoPick ? 'none' : 'auto';
    qualRow.title = _autoPick ? 'Turn off Auto-pick to choose manually' : '';
  }
  autoPickChk.addEventListener('change', () => {
    _autoPick = autoPickChk.checked;
    _applyAutoPickState();
  });
  // Set initial greyed state (Auto-pick defaults ON for Express)
  _applyAutoPickState();

  const advancedInner = el('div', { style: 'display:flex; flex-direction:column; gap:10px; padding-top:10px; border-top:1px solid var(--border-2); margin-top:4px;' }, [
    autoPickRow,
    el('div', { style: 'display:flex; align-items:center; gap:10px; flex-wrap:wrap;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Quality' }),
      qualRow,
    ]),
    qualVramWarn,
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
  // Duration + per-clip + render advanced are all under one collapsed
  // expander -- novices don't need to set duration, the default works.
  root.appendChild(el('div', { class: 'card', style: 'padding:12px 14px;' }, [
    el('details', {}, [
      el('summary', { style: 'cursor:pointer; font-size:.75rem; color:var(--text-3); user-select:none; padding:4px 0; outline:none;', text: '+  Advanced settings (duration, model, quality, aspect ratio)' }),
      el('div', { style: 'display:flex; flex-direction:column; gap:10px; margin-top:8px;' }, [
        el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
          el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Per-clip duration' }),
          durSlider,
          durLabel,
        ]),
        advancedInner,
      ]),
    ]),
  ]));

  // -- Multi-video (scene-hold extension) ------------------------------------
  // Default ON: server now uses SAME prompt for every clip and lets the
  // chain anchor (last-frame -> next-clip-start) carry continuity. This is
  // 'extend one shot into a longer atmospheric video', not 'narrative arc'.
  // The narrative arc path drifts on 16GB and is reserved for Wan I2V (24GB+).
  let _multiVideo      = true;
  let _targetSecs      = 10;  // 10s default -> 2 clips -> ~2-3 min vs 5-7 min at 24s
  // Quick Video is built around music + lyrics. Default ON; user can untick
  // to ship a silent clip when iterating on the visuals alone.
  let _addMusic        = true;
  // motion_style driven by auto-pick: Distilled->calm, Dev13B->dynamic. No toggle needed.
  let _numClips        = Math.max(2, Math.round(_targetSecs / _duration));
  let _upscaleOn       = false;
  let _upscaleMethod   = 'ffmpeg';
  let _upscaleScale    = 2.0;
  let _directorPasses  = 0;

  const multiChk = el('input', { type: 'checkbox', id: 'express-multi-video', checked: 'checked', style: 'cursor:pointer; width:15px; height:15px; flex-shrink:0;' });

  // Story length: free slider, 10-120s in 5s steps
  const lenSlider = el('input', {
    type: 'range', min: '10', max: '120', step: '5',
    value: String(_targetSecs),
    style: 'flex:1; max-width:240px; cursor:pointer;',
  });
  const lenLabel = el('span', {
    style: 'font-size:.82rem; color:var(--accent); font-weight:600; min-width:40px; text-align:right;',
    text: `${_targetSecs}s`,
  });
  lenSlider.addEventListener('input', () => {
    _targetSecs = Number(lenSlider.value);
    lenLabel.textContent = `${_targetSecs}s`;
    _refreshClipInfo();
  });

  const clipInfoLabel = el('span', {
    style: 'font-size:.75rem; color:var(--text-3);',
    text: `~${_numClips} clips at ${_duration}s each - AI adjusts pacing`,
  });

  function _refreshClipInfo() {
    _numClips = Math.max(2, Math.round(_targetSecs / _duration));
    clipInfoLabel.textContent = `~${_numClips} clips at ${_duration}s each - AI adjusts pacing`;
  }
  durSlider.addEventListener('input', _refreshClipInfo);

  const upscaleChk = el('input', { type: 'checkbox', id: 'express-upscale', style: 'cursor:pointer; width:13px; height:13px; flex-shrink:0;' });

  const { row: methodRow } = _makeChipGroup(
    [{ label: 'Fast', value: 'ffmpeg' }, { label: 'AI', value: 'ai' }],
    _upscaleMethod,
    item => { _upscaleMethod = item.value; },
  );
  const { row: scaleRow } = _makeChipGroup(
    [{ label: '1.5x', value: '1.5' }, { label: '2x', value: '2' }, { label: '4x', value: '4' }],
    String(_upscaleScale === 2.0 ? 2 : _upscaleScale),
    item => { _upscaleScale = Number(item.value); },
  );

  const upscaleControls = el('div', { style: 'display:none; align-items:center; gap:8px; flex-wrap:wrap;' }, [
    methodRow, scaleRow,
  ]);
  upscaleChk.addEventListener('change', () => {
    _upscaleOn = upscaleChk.checked;
    upscaleControls.style.display = _upscaleOn ? 'flex' : 'none';
  });

  const multiSettings = el('div', { style: 'display:flex; flex-direction:column; gap:10px; margin-top:10px; padding-top:10px; border-top:1px solid var(--border-2);' });
  multiSettings.appendChild(el('div', { style: 'display:flex; align-items:center; gap:10px; flex-wrap:wrap;' }, [
    el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Story length' }),
    lenSlider,
    lenLabel,
  ]));
  multiSettings.appendChild(el('div', { style: 'padding-left:90px;' }, [clipInfoLabel]));
  multiSettings.appendChild(el('div', {
    style: 'font-size:.73rem; color:var(--text-3); line-height:1.5; padding:4px 0;',
    text: 'AI writes a story arc, each clip starts from the last frame of the previous one. Audio spans the whole piece.',
  }));
  // Music + lyrics toggle. ON by default -- this is the headline feature.
  // Untick to ship a silent clip when iterating on visuals only.
  const musicChk = el('input', {
    type: 'checkbox', id: 'express-music',
    style: 'cursor:pointer; width:13px; height:13px; flex-shrink:0;',
  });
  musicChk.checked = _addMusic;
  musicChk.addEventListener('change', () => { _addMusic = musicChk.checked; });
  multiSettings.appendChild(el('div', { style: 'display:flex; align-items:center; gap:8px; flex-wrap:wrap;' }, [
    musicChk,
    el('label', {
      for: 'express-music',
      style: 'font-size:.78rem; color:var(--text-3); cursor:pointer;',
      text: 'Music with lyrics (untick for silent clip)',
    }),
  ]));

  let _lipSync = true;
  const lipSyncChk = el('input', {
    type: 'checkbox', id: 'express-lip-sync',
    style: 'cursor:pointer; width:13px; height:13px; flex-shrink:0;',
  });
  lipSyncChk.checked = _lipSync;
  lipSyncChk.addEventListener('change', () => { _lipSync = lipSyncChk.checked; });
  multiSettings.appendChild(el('div', { style: 'display:flex; align-items:center; gap:8px; flex-wrap:wrap;' }, [
    lipSyncChk,
    el('label', {
      for: 'express-lip-sync',
      style: 'font-size:.78rem; color:var(--text-3); cursor:pointer;',
      text: 'Lip Sync (audio drives mouth/face motion)',
    }),
  ]));
  multiSettings.appendChild(el('div', { style: 'display:flex; align-items:center; gap:8px; flex-wrap:wrap;' }, [
    upscaleChk,
    el('label', { for: 'express-upscale', style: 'font-size:.78rem; color:var(--text-3); cursor:pointer;', text: 'Upscale output' }),
    upscaleControls,
  ]));

  // Director passes: AI reviews and re-shoots weak clips between passes
  const DIRECTOR_OPTIONS = [
    { label: 'Off',      value: '0', tip: 'No director review -- clips ship as generated' },
    { label: 'Reviewed', value: '1', tip: 'AI reviews + re-shoots weak clips once. Samples 1 frame/sec per clip (max 12), downscaled locally before sending to the AI for speed.' },
    { label: 'Refined',  value: '2', tip: 'Two rounds of AI review and re-direction. Same per-clip frame sampling on each pass.' },
  ];
  const { row: directorRow } = _makeChipGroup(
    DIRECTOR_OPTIONS.map(d => ({ label: d.label, value: d.value, title: d.tip })),
    String(_directorPasses),
    item => { _directorPasses = Number(item.value); },
  );
  multiSettings.appendChild(el('div', { style: 'display:flex; align-items:center; gap:10px; flex-wrap:wrap;' }, [
    el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Director' }),
    directorRow,
    el('span', { style: 'font-size:.72rem; color:var(--text-3);', text: '- AI reviews and re-shoots weak clips' }),
  ]));


  // Novice-friendly: hide the per-clip / director / upscale tweaks behind a
  // "Customize" expander. The defaults already produce a good multi-clip
  // story with music + lyrics, so the casual flow is: drop image, brief,
  // click Create Story.
  const multiSettingsDetails = el('details', { style: 'margin-top:8px;' }, [
    el('summary', {
      style: 'cursor:pointer; font-size:.72rem; color:var(--text-3); user-select:none; padding:4px 0; outline:none; list-style:none;',
      text: '+  Customize story (length, music, director)',
    }),
    multiSettings,
  ]);

  const multiCard = el('div', { class: 'card', style: 'padding:12px 14px;' }, [
    el('div', { style: 'display:flex; align-items:center; gap:8px;' }, [
      multiChk,
      el('label', {
        for: 'express-multi-video',
        style: 'font-size:.88rem; font-weight:600; cursor:pointer; user-select:none; color:var(--text);',
        text: 'Extend scene',
      }),
      el('span', { style: 'font-size:.74rem; color:var(--text-3);', text: '- chain multiple short clips of the same scene into a longer atmospheric video' }),
    ]),
    multiSettingsDetails,
  ]);
  root.appendChild(multiCard);

  multiChk.addEventListener('change', () => {
    _multiVideo = multiChk.checked;
    // Hide the whole Customize expander when multi-video is off -- the
    // controls inside are meaningless for single-clip mode.
    multiSettingsDetails.style.display = _multiVideo ? '' : 'none';
    createBtn.textContent = _multiVideo ? 'Create Extended Video' : (_pendingCount > 0 ? '+ Add to Queue' : 'Create');
    _refreshClipInfo();
  });

  // -- Queue-depth tracking for Create button --------------------------------
  let _pendingCount = 0;
  function _refreshCreateBtn() {
    createBtn.disabled = false;
    if (_multiVideo) {
      createBtn.textContent = 'Create Extended Video';
    } else {
      createBtn.textContent = _pendingCount > 0 ? '+ Add to Queue' : 'Create';
    }
  }
  function _trackDone(job_id) {
    const done = () => { _pendingCount = Math.max(0, _pendingCount - 1); _refreshCreateBtn(); };
    pollJob(job_id, null, done, done);
  }

  // -- Create + Loop button row ----------------------------------------------
  const createBtn = el('button', {
    class: 'btn btn-primary btn-generate',
    text: 'Create Extended Video',
    style: 'flex:1; font-size:1.1rem; padding:14px; font-weight:700; letter-spacing:.04em;',
  });
  const loopBtn = el('button', {
    class: 'btn',
    text: '∞  Loop',
    title: 'Generate continuously until stopped',
    style: 'display:none; font-size:.95rem; padding:14px 18px; white-space:nowrap;',
  });
  const loopFolderBtn = el('button', {
    class: 'btn',
    text: 'Loop Folder',
    title: 'Iterate through every image in a folder, running the current settings once per image.',
    style: 'font-size:.95rem; padding:14px 18px; white-space:nowrap;',
  });
  // Quick Preview: 1 clip, no audio, results in ~30-45s instead of 2-5 min.
  // Lets the user validate the visual direction before committing to a full run.
  const quickBtn = el('button', {
    class: 'btn',
    text: 'Quick',
    title: 'Fast 30-45s preview -- validates composition and atmosphere, not motion',
    style: 'font-size:.9rem; padding:14px 16px; white-space:nowrap;',
  });
  root.appendChild(el('div', { style: 'display:flex; gap:8px;' }, [createBtn, quickBtn, loopBtn, loopFolderBtn]));

  // Folder path input -- replaces window.prompt() so the user has a proper
  // labelled text box to paste into. Shown/hidden by _startLoopFolder().
  const folderPathInput = el('input', {
    type: 'text',
    placeholder: 'Paste folder path, e.g. C:\\Users\\andrew\\Desktop\\photos',
    style: [
      'flex:1; min-width:0; padding:6px 8px;',
      'border-radius:6px; border:1px solid var(--border-2);',
      'background:var(--bg-raised); color:var(--text-1);',
      'font-size:.82rem; font-family:monospace;',
    ].join(' '),
  });
  folderPathInput.value = localStorage.getItem('dcs-loop-folder') || '';
  const folderStartBtn   = el('button', { class: 'btn btn-sm', text: 'Start',  title: 'Start folder loop' });
  const folderCancelLink = el('a', {
    href: '#',
    style: 'font-size:.78rem; color:var(--text-3); text-decoration:underline; cursor:pointer; white-space:nowrap;',
    text: 'cancel',
  });
  const folderInputRow = el('div', {
    style: 'display:none; align-items:center; gap:6px; padding:4px 2px 0;',
  }, [folderPathInput, folderStartBtn, folderCancelLink]);
  root.appendChild(folderInputRow);

  // -- Progress + result area ------------------------------------------------
  const progressWrap = el('div', { style: 'display:none; flex-direction:column; gap:8px;' });
  const progressBar  = el('div', { style: 'height:4px; background:var(--border-2); border-radius:2px; overflow:hidden;' });
  const progressFill = el('div', { style: 'height:100%; width:0%; background:var(--accent); border-radius:2px; transition:width .4s;' });
  progressBar.appendChild(progressFill);
  const progressMsg  = el('div', { style: 'font-size:.8rem; color:var(--text-3); text-align:center;' });
  const progressCancelBtn = el('button', {
    style: 'align-self:center; padding:4px 12px; border-radius:var(--r-sm); border:1px solid var(--border-2); background:transparent; color:var(--text-3); cursor:pointer; font-size:11px;',
    text: 'Cancel',
  });
  progressCancelBtn.addEventListener('click', () => {
    if (_activePoller) { _activePoller.stop(); _activePoller = null; }
    if (_jobId) { stopJob(_jobId); _jobId = null; }
    progressWrap.style.display = 'none';
  });
  progressWrap.appendChild(progressBar);
  progressWrap.appendChild(progressMsg);
  progressWrap.appendChild(progressCancelBtn);
  root.appendChild(progressWrap);

  const resultWrap = el('div', { style: 'display:none; flex-direction:column; gap:8px;' });
  const resultVideo = el('video', { controls: '', style: 'width:100%; border-radius:8px; background:#000;' });
  const resultActions = el('div', { style: 'display:flex; gap:8px; justify-content:center; align-items:center; flex-wrap:wrap;' });
  const newBtn  = el('button', { class: 'btn btn-sm', text: '+ New video' });
  const sendBtn = el('button', { class: 'btn btn-sm', text: 'Open in Create Videos ->' });
  const muteBtn = el('button', { class: 'btn btn-sm', text: 'Mute', title: 'Toggle mute' });
  muteBtn.addEventListener('click', () => {
    resultVideo.muted = !resultVideo.muted;
    muteBtn.textContent = resultVideo.muted ? 'Unmute' : 'Mute';
  });
  resultActions.appendChild(newBtn);
  resultActions.appendChild(sendBtn);
  resultActions.appendChild(muteBtn);
  // Lyric sheet -- shown after generation when lyrics were produced.
  // Pre-populated by _showResult when job.meta.lyrics is present.
  const lyricsWrap = el('details', {
    style: 'display:none; margin-top:4px; background:var(--bg-raised); border:1px solid var(--border-2); border-radius:8px; padding:0;',
  }, [
    el('summary', {
      style: 'cursor:pointer; font-size:.75rem; color:var(--text-3); padding:8px 12px; outline:none; user-select:none; list-style:none;',
      text: '+ Show lyrics',
    }),
  ]);
  const lyricsBody = el('pre', {
    style: [
      'margin:0; padding:8px 14px 12px;',
      'font-size:.8rem; line-height:1.65; color:var(--text-2);',
      'font-family:inherit; white-space:pre-wrap; word-break:break-word;',
    ].join(' '),
  });
  lyricsWrap.appendChild(lyricsBody);
  resultWrap.appendChild(resultVideo);
  resultWrap.appendChild(resultActions);
  resultWrap.appendChild(lyricsWrap);
  root.appendChild(resultWrap);

  // High-water mark: progress should only ever climb, never recede. The
  // backend can legitimately re-emit a lower number when status flips back
  // to "queued" between prep and GPU phases, but the user reads that as the
  // bar going backwards. Clamp the BAR width here; the message still updates
  // freely so users see queue-position text without losing visual progress.
  let _progressMax = 0;
  function _showProgress(pct, msg) {
    progressWrap.style.display    = 'flex';
    resultWrap.style.display      = 'none';
    if (pct > _progressMax) _progressMax = pct;
    progressFill.style.width      = `${_progressMax}%`;
    progressFill.style.background = 'var(--accent)';
    progressMsg.style.color       = 'var(--text-3)';
    progressMsg.textContent       = msg || '';
  }
  function _resetProgress() {
    _progressMax = 0;
    progressFill.style.width = '0%';
  }

  function _showResult(videoPath, lyrics) {
    progressWrap.style.display = 'none';
    resultWrap.style.display   = 'flex';
    resultVideo.src = pathToUrl(videoPath);
    resultVideo.load();
    if (lyrics && typeof lyrics === 'string' && lyrics.trim()) {
      lyricsBody.textContent = lyrics.trim();
      lyricsWrap.style.display = '';
    } else {
      lyricsWrap.style.display = 'none';
    }
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
    text: 'Want more control?  Open Create Videos ->',
  });
  advLink.appendChild(advBtn);
  root.appendChild(advLink);
  advBtn.addEventListener('click', () => {
    if (_imagePath) handoff('create-videos', { type: 'image', path: _imagePath, url: pathToUrl(_imagePath) });
    document.querySelector('.rail-tab[data-tab="create-videos"]')?.click();
  });

  let _jobId = null;
  let _activePoller = null;  // stop() handle -- ensures only one watcher runs at a time

  // -- Core generation -------------------------------------------------------
  // Returns Promise<boolean> -- true = queued/success, false = failure
  async function _generateOne() {
    let motionPrompt = ideaInput.value.trim();
    const needIdea   = !motionPrompt;
    const needLyric  = !lyricInput.value.trim();

    if (needIdea && needLyric && _imagePath) {
      // Both blank + image -- one brainstorm call fills both with energy
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
              user_direction: 'explosive physical action -- subject must be actively moving and doing something dramatic',
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
      // Always generate a lyric direction when missing -- bare ideas produce flat music
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
          steps: _steps, guidance: _guidance, seed: -1, skip_audio: !_addMusic, instrumental: false, lip_sync: _lipSync,
          output_width: _outW, output_height: _outH,
          auto_pick_model: _autoPick,
          // LTX Distilled (Photo Mood) defaults calm; Wan chips are dynamic.
          // auto_pick_model overrides this server-side when ON.
          // For LTX: 'calm' (subject still) or 'gentle' (subject moves subtly).
          // For Wan I2V: always 'dynamic' (kinetic) -- it's the only mode Wan
          // does well.
          motion_style: _model.toLowerCase().includes('dev13') ? 'dynamic' : 'calm',
        }),
      });
      _jobId = job_id;
      document.dispatchEvent(new CustomEvent('job-queued', { detail: { job_id } }));

      // Start watching in the background -- does NOT block the caller.
      // The button re-enables immediately; user can queue more jobs while this runs.
      _watchJob(job_id);
      return true;
    } catch (e) {
      if (e.status === 429 || /queue.*full|full.*queue/i.test(e.message)) {
        toast('Queue is full -- wait for the current video to finish', 'error');
      } else {
        toast(e.message, 'error');
      }
      return false;
    }
  }

  // Watch a job and update the progress/result area.
  // Returns a Promise so loop mode can await completion; single-run mode ignores the return value.
  // Cancels any previously running watcher so only one job's progress shows at a time.
  function _watchJob(job_id) {
    if (_activePoller) { _activePoller.stop(); _activePoller = null; }
    _resetProgress();
    _showProgress(2, 'Added to queue...');
    return new Promise(resolve => {
      let _firstClipShown = false;
      const poller = pollJob(job_id,
        j => {
          // Use server progress whenever it's reported (>= 1). For status=queued
          // before any prep has run, fall back to the 2% bootstrap so the bar
          // shows something. Don't fall back to 5 -- that artificially leaps
          // ahead and makes the next real tick (e.g. progress=3) snap backwards.
          const reported = Number(j.progress) || 0;
          if (j.status === 'queued') {
            const pos = j.queue_position;
            const label = pos === 0 ? 'up next' : `position ${pos + 1}`;
            _showProgress(reported >= 1 ? reported : 2, `In queue -- ${label}...`);
          } else {
            _showProgress(reported >= 1 ? reported : 2, j.message || `${reported}%`);
          }
          // Streaming preview: show clip 1 the moment it's ready -- users see
          // a result in ~30s instead of waiting for the full multi-clip job.
          if (!_firstClipShown && j.meta?.first_clip) {
            _firstClipShown = true;
            resultVideo.src = pathToUrl(j.meta.first_clip);
            resultVideo.load();
            resultWrap.style.display = 'flex';
          }
        },
        j => {
          _activePoller = null;
          const outputs = Array.isArray(j.output) ? j.output : [j.output];
          const best = outputs.length > 1 ? outputs[1] : outputs[0];
          if (best) _showResult(best, j.meta?.lyrics);
          else _hideProgress();
          resolve(true);
        },
        err => { _activePoller = null; _showError(err); resolve(false); },
      );
      _activePoller = poller;
    });
  }

  // -- Loop Folder: server-side runner tied to a browser heartbeat ---------
  //
  // The actual loop runs in the server (features/fun_videos/folder_loop.py),
  // submitting one job at a time through the existing job manager. This
  // tab just (a) starts the loop, (b) polls /status to update the UI,
  // (c) sends a Stop request when the user clicks.
  //
  // The poll IS the heartbeat. If the server stops getting /status calls
  // for ~60s (e.g., browser tab closed), it stops the loop automatically.
  // We deliberately do NOT persist server state across DCS restarts --
  // background processes that survive a closed browser are a footgun.
  //
  // What this gets us over the old client-side loop:
  //   * Survives transient browser-tab throttling
  //   * Continues past per-image failures (logs them; doesn't abort)
  //   * Resumes UI on page refresh if a loop is still alive server-side
  //
  // What it intentionally does NOT do:
  //   * Survive a closed browser tab (heartbeat dies, loop stops)
  //   * Survive a DCS restart (state is in-process only)

  let _folderPath      = localStorage.getItem('dcs-loop-folder') || '';
  let _folderActive    = false;
  let _folderPollTimer = null;

  function _renderFolderStatus(snap) {
    _folderActive = !!snap.active;
    if (_folderActive) {
      const lapTag = (snap.repeat && snap.lap > 1) ? ` lap ${snap.lap}` : '';
      loopFolderBtn.textContent = `Stop (${snap.index}/${snap.total}${lapTag})`;
      loopFolderBtn.classList.add('btn-primary');
      folderInputRow.style.display = 'none';   // hide path box while running
    } else {
      loopFolderBtn.textContent = 'Loop Folder';
      loopFolderBtn.classList.remove('btn-primary');
    }
  }

  async function _pollFolderStatus() {
    try {
      const snap = await api('/api/fun/folder-loop/status');
      _renderFolderStatus(snap);
      if (!snap.active) {
        // Loop ended -- stop polling and report
        if (_folderPollTimer) { clearInterval(_folderPollTimer); _folderPollTimer = null; }
        const msg = `Loop Folder ${snap.status}: ${snap.succeeded} done, ${snap.failed} failed`;
        if (snap.status === 'done')        toast(msg, 'success');
        else if (snap.status === 'stopped') toast(msg, 'info');
        else if (snap.status === 'error')   toast(msg, 'error');
      }
    } catch (e) {
      // Transient network blips shouldn't toast-spam; just log.
      // Note: if the polling fails persistently the SERVER will hit the
      // heartbeat timeout and stop the loop on its own.
      console.warn('[folder-loop] status poll failed:', e && e.message);
    }
  }

  // Build the same body shape _generateMulti / _generateOne would send,
  // minus photo_path (server replaces it per image) and we leave
  // video_prompt empty when the user hasn't typed an idea so the pipeline
  // brainstorms per image. If the user HAS typed something, every image
  // gets the same direction.
  function _collectFolderLoopSettings() {
    const isLtx = _model.toLowerCase().includes('ltx');
    return {
      video_prompt:        ideaInput.value.trim(),
      music_prompt:        '',
      lyric_direction:     lyricInput.value.trim(),
      user_direction:      'cinematic narrative, story continuity',
      model:               _model,
      duration:            _duration,       // single-clip path
      clip_duration:       _duration,       // multi-clip path
      num_clips:           _numClips,
      target_story_length: _targetSecs,
      upscale:             _upscaleOn,
      upscale_scale:       _upscaleScale,
      upscale_method:      _upscaleMethod,
      director_passes:     _directorPasses,
      steps:               _steps,
      guidance:            _guidance,
      seed:                -1,
      skip_audio:          !_addMusic,
      instrumental:        false,
      lip_sync:            _lipSync,
      output_width:        _outW,
      output_height:       _outH,
      auto_pick_model:     _autoPick,
      motion_style:        isLtx ? 'calm' : 'dynamic',
    };
  }

  // Open a native OS folder-picker dialog then immediately start the loop.
  // Called by both the Loop Folder button and the drop-zone pick-folder link.
  async function _startLoopFolder() {
    if (_folderActive) {
      // Loop is running -- request stop
      api('/api/fun/folder-loop/stop', { method: 'POST' }).catch(() => {});
      toast('Loop Folder: stop requested', 'info');
      return;
    }
    let picked;
    try {
      const r = await api('/api/browse-folder', { method: 'POST' });
      picked = r.path;
    } catch (_) {
      picked = null;
    }
    if (!picked) return;  // dialog cancelled or errored
    folderPathInput.value = picked;
    localStorage.setItem('dcs-loop-folder', picked);
    _submitFolderPath();
  }

  // Actually start the loop with the path currently in folderPathInput.
  async function _submitFolderPath() {
    const folder = folderPathInput.value.trim();
    if (!folder) { toast('Paste a folder path first', 'error'); return; }
    folderInputRow.style.display = 'none';
    try {
      const snap = await api('/api/fun/folder-loop/start', {
        method: 'POST',
        body: JSON.stringify({
          folder,
          settings:    _collectFolderLoopSettings(),
          multi_video: _multiVideo,
          repeat:      true,
        }),
      });
      _folderPath = snap.folder;
      localStorage.setItem('dcs-loop-folder', _folderPath);
      folderPathInput.value = _folderPath;   // normalise to resolved path
      toast(`Loop Folder: ${snap.total} images queued from ${snap.folder}`, 'info');
      _renderFolderStatus(snap);

      // Begin polling. THIS POLL IS THE HEARTBEAT. If we stop polling for
      // ~60s the server-side loop ends itself; that's by design.
      if (_folderPollTimer) clearInterval(_folderPollTimer);
      _folderPollTimer = setInterval(_pollFolderStatus, 5000);
    } catch (e) {
      // Re-show the input so the user can fix the path and try again
      folderInputRow.style.display = 'flex';
      folderPathInput.focus();
      toast((e && e.message) || 'Could not start folder loop', 'error');
    }
  }

  folderStartBtn.addEventListener('click', _submitFolderPath);
  folderCancelLink.addEventListener('click', (ev) => {
    ev.preventDefault();
    folderInputRow.style.display = 'none';
  });
  folderPathInput.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter')  { ev.preventDefault(); _submitFolderPath(); }
    if (ev.key === 'Escape') { folderInputRow.style.display = 'none'; }
  });

  loopFolderBtn.addEventListener('click', _startLoopFolder);
  pickFolderLink.addEventListener('click', (e) => {
    // Drop zone has its own click handler that opens the image file picker;
    // stop propagation so a click on the link doesn't ALSO open that picker.
    e.preventDefault();
    e.stopPropagation();
    _startLoopFolder();
  });

  // On tab init: check if a folder loop is already running on the server
  // (happens if the user refreshed within ~60s of activity). Restore the
  // UI and resume polling so the heartbeat doesn't lapse.
  (async () => {
    try {
      const snap = await api('/api/fun/folder-loop/status');
      if (snap.active) {
        _renderFolderStatus(snap);
        if (_folderPollTimer) clearInterval(_folderPollTimer);
        _folderPollTimer = setInterval(_pollFolderStatus, 5000);
        toast(`Resumed folder loop view: ${snap.index}/${snap.total}`, 'info');
      }
    } catch (_) {}
  })();

  // -- Multi-video generation ------------------------------------------------
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
          // No "dramatic" here -- Express defaults to LTX which defaults to CALM
          // motion. "Dramatic" in the user direction fights the CALM system prompt
          // and forces the story-arc LLM to reconcile contradictory signals. Let
          // the system prompt (per motion_style) drive the energy.
          user_direction:  'cinematic narrative, story continuity',
          model:           _model,
          clip_duration:       _duration,
          num_clips:           _numClips,
          target_story_length: _targetSecs,
          upscale:             _upscaleOn,
          upscale_scale:       _upscaleScale,
          upscale_method:      _upscaleMethod,
          director_passes:     _directorPasses,
          steps:           _steps,
          guidance:        _guidance,
          seed:            -1,
          skip_audio:      !_addMusic,
          instrumental:    false,
          lip_sync:        _lipSync,
          output_width:    _outW,
          output_height:   _outH,
          auto_pick_model: _autoPick,
          // LTX uses the Motion chip (calm | gentle); Wan is always dynamic.
          // auto_pick_model overrides this server-side when ON.
          motion_style: _model.toLowerCase().includes('dev13') ? 'dynamic' : 'calm',
        }),
      });
      _jobId = job_id;
      document.dispatchEvent(new CustomEvent('job-queued', { detail: { job_id } }));
      _watchJob(job_id);
      return true;
    } catch (e) {
      if (e.status === 429 || /queue.*full|full.*queue/i.test(e.message)) {
        toast('Queue is full -- wait for the current video to finish', 'error');
      } else {
        toast(e.message, 'error');
      }
      return false;
    }
  }

  // -- Create (single run or multi-video) -----------------------------------
  createBtn.addEventListener('click', async () => {
    if (!_imagePath && !ideaInput.value.trim()) {
      dropZone.style.borderColor = 'var(--red)';
      setTimeout(() => { if (!_imagePath) dropZone.style.borderColor = 'var(--border-2)'; }, 2000);
      toast('Drop an image or type a video idea first', 'error');
      return;
    }
    _showProgress(1, 'Getting started...');
    const submitted = _multiVideo ? await _generateMulti() : await _generateOne();
    if (submitted && _jobId) {
      _pendingCount++;
      _refreshCreateBtn();
      _trackDone(_jobId);
    }
  });

  // -- Quick Preview: 1 clip, no audio, ~30-45s turnaround ------------------
  quickBtn.addEventListener('click', async () => {
    if (!_imagePath && !ideaInput.value.trim()) {
      dropZone.style.borderColor = 'var(--red)';
      setTimeout(() => { if (!_imagePath) dropZone.style.borderColor = 'var(--border-2)'; }, 2000);
      toast('Drop an image or type an idea first', 'error');
      return;
    }
    _showProgress(1, 'Quick Preview -- generating single clip...');
    try {
      const idea = ideaInput.value.trim();
      const { job_id } = await api('/api/fun/make-it', {
        method: 'POST',
        body: JSON.stringify({
          photo_path:   _imagePath || null,
          video_prompt: idea,
          skip_audio:   true,
          duration:     5,
          model:        'LTX-2 Dev19B Distilled',
        }),
      });
      _jobId = job_id;
      _pendingCount++;
      _refreshCreateBtn();
      document.dispatchEvent(new CustomEvent('job-queued', { detail: { job_id } }));
      _watchJob(job_id);
      _trackDone(job_id);
    } catch (e) {
      _showError(e.message);
    }
  });
}
