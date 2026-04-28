/**
 * Drop Cat Go Studio — Express mode.
 * Drop an image, describe your idea, click Create. Everything else is automatic.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js?v=20260414';
import { createVideoPlayer, el, pathToUrl } from './components.js?v=20260427h';
import { toast, apiFetch } from './shell/toast.js?v=20260421c';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260427a';
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
    { label: '480P',  px: 480,  model: 'Wan2.1-I2V-14B-480P',   maxSec: 16 },
    { label: '580P',  px: 580,  model: 'LTX-2 Dev19B Distilled', maxSec: 20 },
    { label: '720P',  px: 720,  model: 'Wan2.1-I2V-14B-720P',   maxSec: 12 },
    { label: '1080P', px: 1080, model: 'Wan2.1-I2V-14B-720P',   maxSec: 8  },
  ];

  // Which ratios each model natively supports well.
  // LTX-2 was trained on variable aspect ratios; Wan models are 16:9 only.
  const MODEL_RATIOS = {
    'LTX-2 Dev19B Distilled': ['16:9', '9:16', '1:1', '4:3', '3:4'],
    'LTX-2 Dev13B':           ['16:9', '9:16', '1:1', '4:3', '3:4'],
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
    if (w % 2) w++;
    if (h % 2) h++;
    return [w, h];
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
      btn.title = ok ? '' : 'Switch to LTX-2 (580P) for portrait, square & alternative ratios';
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

  api('/api/fun/models').then(data => {
    _allModels = data.models || {};
    const models = Object.entries(_allModels);
    if (models.length) {
      const pref = _preferredModel(_qualityPx);
      _model    = pref || models[0][0];
      _duration = Math.min(8, (_allModels[_model]?.max_sec || 8));
    }
    _updateRatioAvailability();
  }).catch(() => {});

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
  const dropHint = el('div', { style: 'color:var(--text-3); font-size:.88rem;', text: 'Drop an image here or click to browse' });
  const clearImgBtn = el('button', {
    style: 'display:none; position:absolute; top:6px; right:6px; width:24px; height:24px; border-radius:50%; border:none; background:rgba(0,0,0,.65); color:#fff; font-size:15px; line-height:1; cursor:pointer; z-index:2; padding:0;',
    title: 'Clear image', text: '×',
  });
  const dropZone = el('div', { class: 'drop-zone', style: 'position:relative;' }, [preview, dropHint, clearImgBtn]);
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

  function _applyImage(path, url) {
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
  });

  dropZone.addEventListener('click', () => imgInput.click());
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

  // ── Recent images strip ───────────────────────────────────────────────────
  const recentWrap = el('div', { style: 'display:none;' });
  const recentRow  = el('div', { style: 'display:flex; gap:6px; overflow-x:auto; padding:2px 0;' });
  recentWrap.appendChild(el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-bottom:5px;', text: 'Recent images — click to use' }));
  recentWrap.appendChild(recentRow);
  root.appendChild(recentWrap);

  async function _loadRecent() {
    try {
      const data = await apiFetch('/api/gallery?limit=16', { context: 'express.recent' });
      const images = (data.items || []).filter(i => !/\.(mp4|webm|mov)/i.test(i.url));
      if (!images.length) return;
      recentRow.innerHTML = '';
      for (const item of images.slice(0, 12)) {
        const thumb = el('img', {
          src: item.thumbnail || item.url,
          class: 'gallery-thumb',
          title: item.prompt || 'Use this image',
        });
        const removeBtn = el('button', {
          style: 'position:absolute;top:2px;right:2px;width:18px;height:18px;border-radius:50%;border:none;background:rgba(0,0,0,.65);color:#fff;font-size:11px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0;',
          title: 'Remove from list',
          text: '×',
        });
        const thumbWrap = el('div', { style: 'position:relative;flex-shrink:0;' }, [thumb, removeBtn]);
        thumb.addEventListener('click', () => {
          const path = item.metadata?.path || item.url;
          _applyImage(path, item.url);
        });
        removeBtn.addEventListener('click', e => {
          e.stopPropagation();
          thumbWrap.remove();
          if (!recentRow.children.length) recentWrap.style.display = 'none';
        });
        recentRow.appendChild(thumbWrap);
      }
      recentWrap.style.display = '';
    } catch (_) {}
  }
  _loadRecent();

  // ── Idea + Lyric direction ────────────────────────────────────────────────
  const ideaInput = el('textarea', {
    rows: '3',
    style: 'width:100%; resize:vertical; font-size:.95rem;',
    placeholder: 'Describe your idea, mood, or style — or leave blank to let AI decide.',
  });
  const lyricInput = el('input', {
    type: 'text',
    style: 'width:100%; font-size:.82rem; margin-top:8px;',
    placeholder: 'Lyric direction (optional) — e.g. "playful adventure, upbeat, about the dog"',
  });
  root.appendChild(el('div', { class: 'card', style: 'padding:14px;' }, [
    el('div', { style: 'font-size:.8rem; color:var(--text-3); margin-bottom:6px;', text: 'Your idea (optional)' }),
    ideaInput,
    el('div', { style: 'font-size:.78rem; color:var(--text-3); margin-top:10px; margin-bottom:2px;', text: 'Lyric direction (optional)' }),
    lyricInput,
  ]));

  // ── Output settings ───────────────────────────────────────────────────────
  const CHIP_BASE = 'border:1px solid var(--border-2); border-radius:6px; padding:4px 10px; font-size:.78rem; cursor:pointer; background:transparent; color:var(--text-2); transition:background .15s,color .15s;';
  const CHIP_ON   = 'background:var(--accent); border-color:var(--accent); color:#000; font-weight:600;';

  function _makeChipGroup(items, activeVal, onPick) {
    const row = el('div', { style: 'display:flex; gap:6px; flex-wrap:wrap;' });
    const chips = {};
    for (const item of items) {
      const btn = el('button', { style: CHIP_BASE + (item.value === activeVal ? CHIP_ON : ''), text: item.label });
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
    text: '⚠ 1080P is demanding — expect slower generation and higher VRAM use. Make sure your GPU has at least 16 GB VRAM.',
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

  const { row: qualRow } = _makeChipGroup(
    QUALITIES.map(q => ({ label: q.label, value: String(q.px) })),
    String(_qualityPx),
    item => {
      _qualityPx = Number(item.value);
      _model     = _preferredModel(_qualityPx);
      const tierMax = QUALITIES.find(q => q.px === _qualityPx)?.maxSec || 20;
      _duration  = Math.min(8, tierMax);
      durSlider.max = String(tierMax);
      durSlider.value = String(_duration);
      durLabel.textContent = `${_duration}s`;
      _updateRatioAvailability();
    },
  );

  const ratioHintEl = el('div', {
    style: 'display:none; font-size:.72rem; color:var(--text-3); padding-top:2px;',
    text: 'Portrait, square & 4:3 ratios only available with LTX-2 (580P)',
  });
  _ratioHint = ratioHintEl;

  root.appendChild(el('div', { class: 'card', style: 'padding:12px 14px; display:flex; flex-direction:column; gap:10px;' }, [
    el('div', { style: 'display:flex; align-items:center; gap:10px; flex-wrap:wrap;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Aspect ratio' }),
      ratioRow,
    ]),
    ratioHintEl,
    el('div', { style: 'display:flex; align-items:center; gap:10px; flex-wrap:wrap;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Quality' }),
      qualRow,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Duration' }),
      durSlider,
      durLabel,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:6px; padding-top:2px;' }, [
      el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'Output:' }),
      dimsLabel,
    ]),
    warnEl,
  ]));

  // ── Create button ─────────────────────────────────────────────────────────
  const createBtn = el('button', {
    class: 'btn btn-primary btn-generate',
    text: 'Create',
    style: 'width:100%; font-size:1.15rem; padding:16px; font-weight:700; letter-spacing:.04em;',
  });
  root.appendChild(createBtn);

  // Subtle escape hatch to the full Create Videos tab
  const advLink = el('div', { style: 'text-align:center; margin-top:-10px;' });
  const advBtn  = el('button', {
    style: 'background:none; border:none; cursor:pointer; font-size:.75rem; color:var(--text-3); padding:4px 8px;',
    text: 'Want more control?  Open Create Videos →',
  });
  advLink.appendChild(advBtn);
  root.appendChild(advLink);
  advBtn.addEventListener('click', () => {
    if (_imagePath) handoff('fun-videos', { type: 'image', path: _imagePath, url: pathToUrl(_imagePath) });
    document.querySelector('.rail-tab[data-tab="fun-videos"]')?.click();
  });

  // ── Progress ──────────────────────────────────────────────────────────────
  const progressArea = el('div', { class: 'card', style: 'display:none; padding:16px;' });
  root.appendChild(progressArea);

  const progressLabel = el('div', { style: 'font-size:.85rem; color:var(--accent); margin-bottom:10px;' });
  const progressBar   = el('div', { style: 'height:4px; background:var(--border-2); border-radius:2px; overflow:hidden;' }, [
    el('div', { class: 'express-bar', style: 'height:100%; background:var(--accent); width:0%; transition:width .4s;' }),
  ]);
  const cancelBtn = el('button', { class: 'btn btn-sm', text: 'Cancel', style: 'margin-top:10px;' });
  progressArea.appendChild(progressLabel);
  progressArea.appendChild(progressBar);
  progressArea.appendChild(cancelBtn);

  let _jobId = null;
  cancelBtn.addEventListener('click', async () => {
    if (_jobId) { await stopJob(_jobId).catch(() => {}); }
    _reset();
  });

  // ── Result ────────────────────────────────────────────────────────────────
  const resultArea = el('div', { style: 'display:none;' });
  root.appendChild(resultArea);

  const resultTabBar = el('div', { class: 'result-tabs', style: 'display:none;' });
  const tabMix = el('button', { class: 'result-tab active', text: 'With music' });
  const tabRaw = el('button', { class: 'result-tab', text: 'Raw video' });
  resultTabBar.appendChild(tabMix);
  resultTabBar.appendChild(tabRaw);
  resultArea.appendChild(resultTabBar);

  const playerWrap = el('div');
  resultArea.appendChild(playerWrap);
  const player = createVideoPlayer(playerWrap);

  let _rawPath = null, _mixPath = null;

  function _showTab(which) {
    tabMix.classList.toggle('active', which === 'mix');
    tabRaw.classList.toggle('active', which === 'raw');
    const p = which === 'mix' ? _mixPath : _rawPath;
    if (p) player.show(pathToUrl(p), p);
  }
  tabMix.addEventListener('click', () => _showTab('mix'));
  tabRaw.addEventListener('click', () => _showTab('raw'));

  // Post-result action row
  const resultActions = el('div', { style: 'display:flex; gap:8px; margin-top:8px; flex-wrap:wrap;' });
  resultArea.appendChild(resultActions);

  const startOverBtn = el('button', { class: 'btn btn-sm', text: 'Start over' });
  startOverBtn.addEventListener('click', _reset);
  resultActions.appendChild(startOverBtn);

  const clearResultBtn = el('button', { class: 'btn btn-sm', text: 'Clear' });
  clearResultBtn.addEventListener('click', () => {
    player.hide();
    resultArea.style.display = 'none';
    resultTabBar.style.display = 'none';
    _rawPath = null; _mixPath = null;
  });
  resultActions.appendChild(clearResultBtn);

  const deleteFileBtn = el('button', { class: 'btn btn-sm', style: 'color:var(--red,#c41e3a);', text: 'Delete file' });
  deleteFileBtn.addEventListener('click', async () => {
    if (!confirm('Permanently delete this output file?')) return;
    const paths = [_mixPath, _rawPath].filter(Boolean);
    await Promise.all(paths.map(p =>
      apiFetch('/api/output/delete', { method: 'POST', body: JSON.stringify({ path: p }), context: 'express.delete' }).catch(() => {})
    ));
    player.hide();
    resultArea.style.display = 'none';
    resultTabBar.style.display = 'none';
    _rawPath = null; _mixPath = null;
    toast('File deleted', 'success');
  });
  resultActions.appendChild(deleteFileBtn);

  const tweakBtn = el('button', { class: 'btn btn-sm', text: 'Tweak in Create Videos →' });
  tweakBtn.addEventListener('click', () => {
    if (_imagePath) handoff('fun-videos', { type: 'image', path: _imagePath, url: pathToUrl(_imagePath) });
    document.querySelector('.rail-tab[data-tab="fun-videos"]')?.click();
  });
  resultActions.appendChild(tweakBtn);

  function _reset() {
    _jobId = null; _imagePath = null; _rawPath = null; _mixPath = null;
    preview.style.display = 'none'; preview.src = '';
    dropHint.style.display = '';
    clearImgBtn.style.display = 'none';
    dropZone.classList.remove('drop-zone-loaded', 'drag-over');
    ideaInput.value = '';
    lyricInput.value = '';
    createBtn.disabled = false;
    progressArea.style.display = 'none';
    resultArea.style.display = 'none';
    resultTabBar.style.display = 'none';
    player.hide();
    _loadRecent();
  }

  // ── Create ────────────────────────────────────────────────────────────────
  createBtn.addEventListener('click', async () => {
    if (!_imagePath) {
      dropZone.style.borderColor = 'var(--red)';
      setTimeout(() => { if (!_imagePath) dropZone.style.borderColor = 'var(--border-2)'; }, 2000);
      toast('Drop an image first', 'error');
      return;
    }

    createBtn.disabled = true;
    progressArea.style.display = '';
    progressLabel.textContent = 'Submitting…';
    progressBar.querySelector('.express-bar').style.width = '5%';

    // Auto-generate a motion prompt from the image + idea if not provided
    let motionPrompt = ideaInput.value.trim();
    if (!motionPrompt) {
      try {
        progressLabel.textContent = 'Reading image…';
        const data = await api('/api/fun/generate-prompts', {
          method: 'POST',
          body: JSON.stringify({
            image_path: _imagePath,
            num_prompts: 1,
            creativity: 9,
            max_tokens: 400,
            user_direction: 'explosive physical action — subject must be actively moving and doing something dramatic',
          }),
        });
        const p = data.prompts?.[0];
        motionPrompt = (typeof p === 'string' ? p : p?.prompt) || 'Subject erupts into motion, hair and clothes whipping in sudden wind, arms fly wide, explosive energy bursts through the frame';
      } catch (_) {
        motionPrompt = 'Subject erupts into motion, hair and clothes whipping in sudden wind, arms fly wide, explosive energy bursts through the frame';
      }
      // Show what was generated so the user can see it
      ideaInput.value = motionPrompt;
    }

    progressBar.querySelector('.express-bar').style.width = '15%';
    progressLabel.textContent = 'Generating video…';

    try {
      const { job_id } = await api('/api/fun/make-it', {
        method: 'POST',
        body: JSON.stringify({
          photo_path:    _imagePath,
          video_prompt:  motionPrompt,
          music_prompt:  '',
          lyric_direction: lyricInput.value.trim(),
          model:         _model,
          duration:      _duration,
          steps:         40,
          guidance:      8.5,
          seed:          -1,
          skip_audio:    false,
          instrumental:  false,
          output_width:  _outW,
          output_height: _outH,
        }),
      });
      _jobId = job_id;

      pollJob(job_id,
        j => {
          const pct = Math.max(15, Math.min(95, j.progress || 0));
          progressBar.querySelector('.express-bar').style.width = `${pct}%`;
          progressLabel.textContent = j.message || (j.status === 'queued' ? 'Queued — waiting for GPU…' : 'Working…');
        },
        j => {
          progressArea.style.display = 'none';
          createBtn.disabled = false;
          if (j.output) {
            const outputs = Array.isArray(j.output) ? j.output : [j.output];
            _rawPath = outputs[0];
            _mixPath = outputs.length > 1 ? outputs[1] : null;
            const best = _mixPath || _rawPath;
            resultArea.style.display = '';
            if (_mixPath) {
              resultTabBar.style.display = 'flex';
              _showTab('mix');
            } else {
              player.show(pathToUrl(_rawPath), _rawPath);
            }
            pushToGallery('express', best, motionPrompt, null, {});
            toast('Done!', 'success');
          }
        },
        err => {
          progressArea.style.display = 'none';
          createBtn.disabled = false;
          toast(err, 'error');
        },
      );
    } catch (e) {
      progressArea.style.display = 'none';
      createBtn.disabled = false;
      toast(e.message, 'error');
    }
  });
}
