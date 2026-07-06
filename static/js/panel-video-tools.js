/**
 * Drop Cat Go Studio -- Video Tools
 * Unified editor: pick ONE source video, then stack edit steps
 * (Upscale, Sharpen, Crop, Transform, Smooth) and run them in a single pass.
 * Plus a separate AI Music section (generate + mix a soundtrack).
 */
import { api, apiUpload, pollJob, stopJob } from './api.js?v=20260620a';
import { createProgressCard, createVideoPlayer, createSlider, createCheckbox, createSelect, el, formatDuration, pathToUrl } from './components.js?v=20260620a';
import { toast } from './shell/toast.js?v=20260620a';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260620a';

let _sessionListener = null;
let _pollHandle = null;   // module-level so panel re-mounts don't stack pollers

export function init(panel) {
  panel.innerHTML = '';
  const root = el('div', { style: 'display:flex; flex-direction:column; gap:20px; padding:16px; max-width:900px; margin:0 auto;' });
  panel.appendChild(root);

  const source = _buildSourcePicker(root);
  _buildPipeline(root, source);

  _buildDivider(root, 'Add AI Music');
  _buildAudioSection(root);
}

// -- Shared source picker ------------------------------------------------------

function _buildSourcePicker(root) {
  const _srcs = [];                // [{ path, name, width, height, duration }]
  const listeners = [];

  const card = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(card);

  const header = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-bottom:10px; flex-wrap:wrap;' });
  card.appendChild(header);
  header.appendChild(el('span', { style: 'font-size:.9rem; font-weight:700; flex:1;', text: '1 · Choose video(s)' }));

  const fileInput = el('input', { type: 'file', accept: 'video/*', multiple: 'true', style: 'display:none' });
  card.appendChild(fileInput);
  const openBtn = el('button', { class: 'btn btn-sm', text: 'Open file...' });
  const sessBtn = el('button', { class: 'btn btn-sm', text: '+ From session' });
  const galBtn  = el('button', { class: 'btn btn-sm', text: '+ From gallery' });
  header.appendChild(openBtn);
  header.appendChild(sessBtn);
  header.appendChild(galBtn);
  openBtn.addEventListener('click', () => fileInput.click());

  const picker = el('div', { style: 'display:none; border:1px solid var(--border); border-radius:6px; max-height:180px; overflow-y:auto; margin-bottom:8px;' });
  card.appendChild(picker);

  const hint = el('div', { style: 'display:none; font-size:.72rem; color:var(--text-3); margin-bottom:6px;' });
  card.appendChild(hint);
  const list = el('div', { style: 'display:flex; flex-direction:column; gap:6px;' });
  card.appendChild(list);

  function _notify() { listeners.forEach(cb => { try { cb(_srcs[0] || null); } catch (_) { /* ignore */ } }); }

  function _renderList() {
    list.innerHTML = '';
    hint.style.display = _srcs.length > 1 ? '' : 'none';
    hint.textContent = _srcs.length > 1 ? `${_srcs.length} videos -- edits apply to all; two encode at once on the GPU.` : '';
    _srcs.forEach((s, i) => {
      const dims = (s.width && s.height) ? `${s.width}×${s.height}` : '';
      const dur = s.duration ? formatDuration(s.duration) : '';
      const row = el('div', { style: `display:flex; align-items:center; gap:10px; padding:9px 12px; border-radius:8px; background:var(--bg-raised); border:1px solid ${i === 0 ? 'var(--accent)' : 'var(--border-2)'};` });
      row.appendChild(el('span', { style: `font-size:.64rem; font-weight:800; flex-shrink:0; color:${i === 0 ? 'var(--accent)' : 'var(--text-3)'};`, text: i === 0 ? 'SOURCE' : `#${i + 1}` }));
      row.appendChild(el('span', { style: 'flex:1; font-size:.84rem; font-weight:600; color:var(--text-2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;', text: s.name || s.path.split(/[\\/]/).pop() }));
      row.appendChild(el('span', { style: 'font-size:.74rem; color:var(--text-3); flex-shrink:0; font-variant-numeric:tabular-nums;', text: [dims, dur].filter(Boolean).join('  ·  ') }));
      row.appendChild(el('button', { class: 'btn-icon-xs remove', text: '✕', title: 'Remove',
        onclick() { _srcs.splice(i, 1); _renderList(); _notify(); } }));
      list.appendChild(row);
    });
  }

  async function _set(path) {
    picker.style.display = 'none';
    if (_srcs.some(s => s.path === path)) return;   // dedupe
    try {
      const data = await api('/api/tools/add-paths', { method: 'POST', body: JSON.stringify({ paths: [path] }) });
      const f = (data.files || [])[0];
      if (!f || f.error) { toast(f?.error || 'Could not read this video', 'error'); return; }
      _srcs.push({ path: f.path, name: f.name, width: f.width, height: f.height, duration: f.duration });
      _renderList();
      _notify();
    } catch (e) { toast(e.message, 'error'); }
  }

  fileInput.addEventListener('change', async () => {
    if (!fileInput.files?.length) return;
    try {
      const data = await apiUpload('/api/tools/upload', Array.from(fileInput.files));
      if (data.rejected?.length) toast(`Skipped: ${data.rejected.join(', ')}`, 'error');
      for (const f of (data.files || [])) await _set(f.path);
    } catch (e) { toast(e.message, 'error'); }
    fileInput.value = '';
  });

  async function _togglePicker(btn, loader) {
    const show = picker.style.display === 'none';
    picker.style.display = show ? '' : 'none';
    if (!show) return;
    picker.innerHTML = '<div style="padding:10px; font-size:.8rem; color:var(--text-3);">Loading...</div>';
    try {
      const rows = await loader();
      picker.innerHTML = '';
      if (!rows.length) { picker.appendChild(el('div', { style: 'padding:10px; font-size:.8rem; color:var(--text-3);', text: 'Nothing here yet.' })); return; }
      for (const r of rows) {
        picker.appendChild(el('div', {
          style: 'display:flex; align-items:center; gap:8px; padding:7px 11px; cursor:pointer; border-bottom:1px solid var(--border-2);',
          onclick() { _set(r.path); },
        }, [
          el('span', { style: 'flex:1; font-size:.8rem; color:var(--text-2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;', text: r.name }),
          el('span', { style: 'font-size:.72rem; color:var(--text-3); flex-shrink:0;', text: r.meta || '' }),
        ]));
      }
    } catch (e) { picker.innerHTML = ''; toast(e.message, 'error'); }
  }

  sessBtn.addEventListener('click', () => _togglePicker(sessBtn, async () => {
    const data = await api('/api/session/videos');
    return (data.videos || []).map(v => ({ path: v.path, name: v.filename, meta: formatDuration(v.duration) }));
  }));

  galBtn.addEventListener('click', () => _togglePicker(galBtn, async () => {
    const data = await api('/api/gallery?limit=60');
    return (data.items || data || [])
      .filter(i => /(\.mp4|\.webm|\.mov|\.mkv|\.m4v)$/i.test(i.url || ''))
      .map(i => { const p = i.metadata?.path || i.url; return { path: p, name: p.split(/[\\/]/).pop(), meta: '' }; });
  }));

  return {
    get: () => _srcs[0] || null,
    getAll: () => _srcs.slice(),
    onChange: (cb) => listeners.push(cb),
  };
}

// -- Pipeline builder ----------------------------------------------------------

const _STEP_KINDS = [
  { op: 'upscale',   label: 'Upscale' },
  { op: 'sharpen',   label: 'Sharpen' },
  { op: 'crop',      label: 'Crop' },
  { op: 'transform', label: 'Transform' },
  { op: 'smooth',    label: 'Smooth' },
];

function _buildPipeline(root, source) {
  const steps = [];   // [{ op, el, title, getParams }]

  // Add-step toolbar
  const addCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(addCard);
  addCard.appendChild(el('div', { style: 'font-size:.9rem; font-weight:700; margin-bottom:4px;', text: '2 · Stack your edits' }));
  addCard.appendChild(el('div', { style: 'font-size:.74rem; color:var(--text-3); margin-bottom:10px; line-height:1.5;',
    text: 'Add steps in the order you want them applied. They run top to bottom into one final video.' }));
  const addRow = el('div', { style: 'display:flex; gap:8px; flex-wrap:wrap;' });
  addCard.appendChild(addRow);
  for (const k of _STEP_KINDS) {
    addRow.appendChild(el('button', { class: 'btn btn-sm', text: `+ ${k.label}`, onclick() { _addStep(k.op); } }));
  }

  // Steps list
  const stepsWrap = el('div', { style: 'display:flex; flex-direction:column; gap:10px;' });
  root.appendChild(stepsWrap);

  function _addStep(op) {
    if (op === 'crop' && !source.get()) { toast('Choose a source video first', 'error'); return; }
    steps.push(_makeStep(op, source));
    _render();
  }

  function _render() {
    stepsWrap.innerHTML = '';
    if (!steps.length) {
      stepsWrap.appendChild(el('div', { class: 'card', style: 'padding:18px; text-align:center; font-size:.82rem; color:var(--text-3);',
        text: 'No steps yet -- add one above (e.g. Upscale, then Sharpen).' }));
      return;
    }
    steps.forEach((s, i) => {
      const wrap = el('div', { class: 'card', style: 'padding:0; overflow:hidden;' });
      const head = el('div', { style: 'display:flex; align-items:center; gap:8px; padding:9px 12px; background:var(--bg-raised); border-bottom:1px solid var(--border-2);' });
      head.appendChild(el('span', { style: 'width:20px; height:20px; border-radius:50%; background:var(--accent); color:#000; font-size:.72rem; font-weight:800; display:flex; align-items:center; justify-content:center; flex-shrink:0;', text: String(i + 1) }));
      head.appendChild(el('span', { style: 'flex:1; font-size:.84rem; font-weight:700;', text: s.title }));
      head.appendChild(el('button', { class: 'btn-icon-xs', text: '↑', title: 'Move up',
        onclick() { if (i > 0) { [steps[i - 1], steps[i]] = [steps[i], steps[i - 1]]; _render(); } } }));
      head.appendChild(el('button', { class: 'btn-icon-xs', text: '↓', title: 'Move down',
        onclick() { if (i < steps.length - 1) { [steps[i + 1], steps[i]] = [steps[i], steps[i + 1]]; _render(); } } }));
      head.appendChild(el('button', { class: 'btn-icon-xs remove', text: '✕', title: 'Remove',
        onclick() { steps.splice(i, 1); _render(); } }));
      wrap.appendChild(head);
      const body = el('div', { style: 'padding:14px;' });
      body.appendChild(s.el);
      wrap.appendChild(body);
      stepsWrap.appendChild(wrap);
    });
  }
  _render();

  // Run
  const runBtn = el('button', {
    class: 'btn btn-primary',
    text: '▶ Run edits',
    style: 'width:100%; font-size:1.05rem; padding:13px; font-weight:700;',
  });
  root.appendChild(runBtn);
  source.onChange(() => {
    const n = source.getAll().length;
    runBtn.textContent = n > 1 ? `▶ Run edits on ${n} videos` : '▶ Run edits';
  });

  const progWrap = el('div');
  root.appendChild(progWrap);
  const prog = createProgressCard(progWrap);

  const playerWrap = el('div');
  root.appendChild(playerWrap);
  const player = createVideoPlayer(playerWrap);
  player.onStartOver(() => player.hide());

  // Watch a job to completion -- keeps the progress card live and shows the
  // result. Used both when you start a run and when re-attaching to one that's
  // already running (so the panel never goes blank on a long job).
  function _watch(jobId) {
    if (_pollHandle) { _pollHandle.stop(); _pollHandle = null; }
    runBtn.disabled = true;
    prog.show();
    prog.onCancel(async () => { await stopJob(jobId).catch(() => {}); _pollHandle?.stop(); _pollHandle = null; prog.hide(); runBtn.disabled = false; });
    _pollHandle = pollJob(jobId,
      j => prog.update(j.progress || 0, j.message || 'Working...'),
      j => {
        _pollHandle = null; prog.hide(); runBtn.disabled = false;
        const outs = j.meta?.outputs || (j.output ? [j.output] : []);
        if (outs.length) {
          player.show(pathToUrl(outs[0]), outs[0]);
          outs.forEach(o => pushToGallery('video-tools', o, 'Edited video', null, {}));
          toast(outs.length > 1 ? `Edited ${outs.length} videos` : 'Done!', 'success');
        } else {
          toast('Finished but no output was produced', 'error');
        }
      },
      err => { _pollHandle = null; prog.hide(); runBtn.disabled = false; toast(typeof err === 'string' ? err : (err?.message || 'Failed'), 'error'); },
    );
  }

  // On open, reconnect to an edit job that's already running so its progress is
  // always visible -- surviving page refreshes, tab switches, and hours-long jobs.
  (async () => {
    try {
      const data = await api('/api/jobs');
      const active = [...(data.running || []), ...(data.queued || [])].find(j => j.type === 'video_tool');
      if (active) _watch(active.id);
    } catch (_) { /* ignore -- nothing running */ }
  })();

  runBtn.addEventListener('click', async () => {
    const files = source.getAll();
    if (!files.length) { toast('Choose a source video first', 'error'); return; }
    if (!steps.length) { toast('Add at least one step', 'error'); return; }

    let payload;
    try {
      payload = steps.map(s => s.getParams());
    } catch (e) { toast(e.message || 'A step is not ready', 'error'); return; }

    runBtn.disabled = true;
    prog.show(); prog.update(0, 'Starting...');
    player.hide();
    try {
      const { job_id } = await api('/api/tools/pipeline', {
        method: 'POST',
        body: JSON.stringify({ video_paths: files.map(f => f.path), steps: payload }),
      });
      _watch(job_id);
    } catch (e) { prog.hide(); runBtn.disabled = false; toast(e.message, 'error'); }
  });
}

// -- Individual step editors ---------------------------------------------------

function _makeStep(op, source) {
  const body = el('div');
  if (op === 'upscale')   return _stepUpscale(body);
  if (op === 'sharpen')   return _stepSharpen(body);
  if (op === 'crop')      return _stepCrop(body, source);
  if (op === 'transform') return _stepTransform(body);
  if (op === 'smooth')    return _stepSmooth(body);
  return { op, el: body, title: op, getParams: () => ({ op }) };
}

function _stepUpscale(body) {
  const grid = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:12px;' });
  body.appendChild(grid);
  const scaleSel = createSelect(grid, { label: 'Scale', options: ['1.5x', '2x', '3x', '4x'], value: '2x' });
  const engineSel = createSelect(grid, {
    label: 'Engine',
    options: [{ label: 'AI (rebuilds detail)', value: 'ai' }, { label: 'Fast (Lanczos)', value: 'ffmpeg' }],
    value: 'ai',
  });
  const note = el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-top:6px; line-height:1.5;',
    text: 'AI rebuilds real detail (slower); Fast just stretches pixels (instant).' });
  body.appendChild(note);
  // Adapt silently if the AI engine isn't present on this machine.
  api('/api/tools/upscale').then(d => {
    if (d && d.ai_available === false) {
      const sel = engineSel.el?.querySelector('select');
      sel?.querySelector('option[value="ai"]')?.remove();
      engineSel.value = 'ffmpeg';
      note.textContent = 'Fast (Lanczos) resize -- stretches pixels, no added detail.';
    }
  }).catch(() => {});
  return {
    op: 'upscale', el: body, title: 'Upscale',
    getParams: () => ({ op: 'upscale', scale: parseFloat(scaleSel.value), engine: engineSel.value }),
  };
}

function _stepSharpen(body) {
  const s = createSlider(body, { label: 'Strength', min: 0.3, max: 3.0, step: 0.1, value: 1.0 });
  body.appendChild(el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-top:6px;',
    text: 'Higher = crisper edges, but too much looks harsh.' }));
  return {
    op: 'sharpen', el: body, title: 'Sharpen',
    getParams: () => ({ op: 'sharpen', strength: s.value }),
  };
}

function _stepTransform(body) {
  const grid = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:10px;' });
  body.appendChild(grid);
  const revV = createCheckbox(grid, { label: 'Reverse' });
  const mir  = createCheckbox(grid, { label: 'Mirror (H-flip)' });
  const vf   = createCheckbox(grid, { label: 'V-flip' });
  const mute = createCheckbox(grid, { label: 'Mute audio' });
  const speed = createSlider(body, { label: 'Speed', min: 0.25, max: 4, step: 0.25, value: 1, unit: 'x' });
  const vol   = createSlider(body, { label: 'Volume', min: 0, max: 300, step: 5, value: 100, unit: '%' });
  return {
    op: 'transform', el: body, title: 'Transform',
    getParams: () => ({
      op: 'transform',
      reverse_vid: revV.checked, reverse_aud: revV.checked,
      mirror: mir.checked, vflip: vf.checked,
      speed: speed.value, volume: vol.value,
      mute_audio: mute.checked, keep_audio: !mute.checked,
    }),
  };
}

function _stepSmooth(body) {
  const fps = createSlider(body, { label: 'Target FPS (0 = 2x source)', min: 0, max: 120, step: 1, value: 0, unit: 'fps' });
  const modeSel = createSelect(body, { label: 'Mode', options: ['blend', 'mci', 'rife'], value: 'blend' });
  const desc = el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-top:4px; line-height:1.5;' });
  body.appendChild(desc);
  const descriptions = {
    blend: 'Fast. Softens mild jitter by blending neighboring frames.',
    mci: 'Higher quality. Follows real motion between frames -- better for fast action, slower to render.',
    rife: 'Best quality. GPU-accelerated AI that generates true in-between frames.',
  };
  const updateDesc = () => { desc.textContent = descriptions[modeSel.value] || ''; };
  modeSel.el?.querySelector('select')?.addEventListener('change', updateDesc);
  updateDesc();
  api('/api/tools/interpolate').then(d => {
    if (d && d.rife_available === false) {
      const sel = modeSel.el?.querySelector('select');
      sel?.querySelector('option[value="rife"]')?.remove();
      if (modeSel.value === 'rife') { modeSel.value = 'mci'; updateDesc(); }
    }
  }).catch(() => {});
  return {
    op: 'smooth', el: body, title: 'Smooth',
    getParams: () => ({ op: 'smooth', target_fps: fps.value, mode: modeSel.value }),
  };
}

function _stepCrop(body, source) {
  const editor = _buildCropEditor(body, source);
  return {
    op: 'crop', el: body, title: 'Crop',
    getParams: () => {
      const rect = editor.getRect();
      if (!rect) throw new Error('Crop is still loading its preview -- try again in a moment.');
      return { op: 'crop', rect, keep_audio: editor.getKeepAudio() };
    },
  };
}

// Reusable crop marquee -- loads preview frames for the current source, lets the
// user drag a box, and exposes getRect() (normalized 0..1) + getKeepAudio().
function _buildCropEditor(container, source) {
  let _srcW = 0, _srcH = 0;
  let _frames = [];
  let _active = 0;
  let _loaded = false;
  let _aspect = '1:1';
  let rect = { x: 0.2, y: 0.2, w: 0.6, h: 0.6 };
  const MIN = 0.05;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function aspectN() {
    if (_aspect === 'free') return null;
    const [a, b] = _aspect.split(':').map(Number);
    if (!a || !b || !_srcW || !_srcH) return null;
    return (a / b) * (_srcH / _srcW);
  }

  const stage = el('div');
  container.appendChild(stage);

  const ctrlRow = el('div', { style: 'display:flex; align-items:flex-end; gap:14px; flex-wrap:wrap; margin-bottom:12px;' });
  stage.appendChild(ctrlRow);
  const aspectSel = createSelect(ctrlRow, {
    label: 'Output shape', options: _CROP_ASPECTS, value: '1:1',
    onChange(v) { _aspect = v; _fitAspect(); _renderBox(); _updateReadout(); },
  });
  aspectSel.el.style.marginBottom = '0';
  const keepAudioChk = el('input', { type: 'checkbox', id: `crop-keep-${Math.random().toString(36).slice(2, 7)}`, checked: 'true' });
  ctrlRow.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:center; padding-bottom:6px;' }, [
    keepAudioChk,
    el('label', { for: keepAudioChk.id, style: 'cursor:pointer; font-size:.82rem; color:var(--text-2);', text: 'Keep audio' }),
  ]));
  const sizeReadout = el('div', { style: 'margin-left:auto; padding-bottom:6px; font-size:.78rem; color:var(--text-3); font-variant-numeric:tabular-nums;' });
  ctrlRow.appendChild(sizeReadout);

  const tabRow = el('div', { style: 'display:flex; gap:8px; margin-bottom:10px;' });
  stage.appendChild(tabRow);

  const editorWrap = el('div', {
    style: 'position:relative; width:100%; max-width:560px; margin:0 auto; overflow:hidden; border-radius:8px; background:#000; user-select:none; touch-action:none;',
  });
  stage.appendChild(editorWrap);
  const editorImg = el('img', { style: 'display:block; width:100%; -webkit-user-drag:none; pointer-events:none;' });
  editorWrap.appendChild(editorImg);
  const overlay = el('div', { style: 'position:absolute; inset:0;' });
  editorWrap.appendChild(overlay);

  const box = el('div', {
    style: 'position:absolute; box-sizing:border-box; border:2px solid #fff; '
      + 'box-shadow:0 0 0 9999px rgba(0,0,0,.55); cursor:move; background:rgba(255,255,255,.02);',
  });
  overlay.appendChild(box);
  box.appendChild(el('div', { style: 'position:absolute; inset:0; pointer-events:none; background:'
    + 'linear-gradient(to right, transparent 33.33%, rgba(255,255,255,.35) 33.33%, rgba(255,255,255,.35) calc(33.33% + 1px), transparent calc(33.33% + 1px)),'
    + 'linear-gradient(to right, transparent 66.66%, rgba(255,255,255,.35) 66.66%, rgba(255,255,255,.35) calc(66.66% + 1px), transparent calc(66.66% + 1px)),'
    + 'linear-gradient(to bottom, transparent 33.33%, rgba(255,255,255,.35) 33.33%, rgba(255,255,255,.35) calc(33.33% + 1px), transparent calc(33.33% + 1px)),'
    + 'linear-gradient(to bottom, transparent 66.66%, rgba(255,255,255,.35) 66.66%, rgba(255,255,255,.35) calc(66.66% + 1px), transparent calc(66.66% + 1px));' }));

  const HANDLES = [
    ['nw', -1, -1, 'nwse-resize'], ['ne', 1, -1, 'nesw-resize'],
    ['sw', -1, 1, 'nesw-resize'], ['se', 1, 1, 'nwse-resize'],
    ['n', 0, -1, 'ns-resize'], ['s', 0, 1, 'ns-resize'],
    ['w', -1, 0, 'ew-resize'], ['e', 1, 0, 'ew-resize'],
  ];
  const handleEls = {};
  for (const [id, hx, hy, cursor] of HANDLES) {
    const hEl = el('div', {
      'data-edge': id,
      style: 'position:absolute; width:14px; height:14px; background:#fff; border:1px solid rgba(0,0,0,.4); border-radius:2px; '
        + `cursor:${cursor}; transform:translate(-50%,-50%); `
        + `left:${hx === 0 ? 50 : (hx < 0 ? 0 : 100)}%; top:${hy === 0 ? 50 : (hy < 0 ? 0 : 100)}%;`,
    });
    hEl.addEventListener('pointerdown', (e) => _startResize(e, hx, hy));
    box.appendChild(hEl);
    handleEls[id] = hEl;
  }
  box.addEventListener('pointerdown', _startMove);

  function _renderBox() {
    box.style.left = (rect.x * 100) + '%';
    box.style.top = (rect.y * 100) + '%';
    box.style.width = (rect.w * 100) + '%';
    box.style.height = (rect.h * 100) + '%';
    const showEdges = _aspect === 'free';
    for (const id of ['n', 's', 'w', 'e']) handleEls[id].style.display = showEdges ? '' : 'none';
  }

  function _updateReadout() {
    const wpx = Math.max(2, Math.round(rect.w * _srcW));
    const hpx = Math.max(2, Math.round(rect.h * _srcH));
    sizeReadout.textContent = `Output: ${wpx} × ${hpx}px  ·  from ${_srcW} × ${_srcH}`;
  }

  function _fitAspect() {
    const arN = aspectN();
    if (arN == null) return;
    let h = 0.78, w = h * arN;
    if (w > 0.94) { w = 0.94; h = w / arN; }
    if (h > 0.94) { h = 0.94; w = h * arN; }
    rect = { x: (1 - w) / 2, y: (1 - h) / 2, w, h };
  }

  function _startMove(e) {
    if (e.target !== box) return;
    e.preventDefault();
    const ob = overlay.getBoundingClientRect();
    const start = { ...rect };
    const px = e.clientX, py = e.clientY;
    box.setPointerCapture?.(e.pointerId);
    function move(ev) {
      const dx = (ev.clientX - px) / ob.width;
      const dy = (ev.clientY - py) / ob.height;
      rect.x = clamp(start.x + dx, 0, 1 - rect.w);
      rect.y = clamp(start.y + dy, 0, 1 - rect.h);
      _renderBox(); _updateReadout();
    }
    function up() {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    }
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  function _startResize(e, hx, hy) {
    e.preventDefault();
    e.stopPropagation();
    const ob = overlay.getBoundingClientRect();
    const start = { ...rect };
    const px = e.clientX, py = e.clientY;
    const arN = aspectN();
    function move(ev) {
      const dxN = (ev.clientX - px) / ob.width;
      const dyN = (ev.clientY - py) / ob.height;
      if (arN == null) {
        let x1 = start.x, y1 = start.y, x2 = start.x + start.w, y2 = start.y + start.h;
        if (hx === 1) x2 = clamp(start.x + start.w + dxN, x1 + MIN, 1);
        if (hx === -1) x1 = clamp(start.x + dxN, 0, x2 - MIN);
        if (hy === 1) y2 = clamp(start.y + start.h + dyN, y1 + MIN, 1);
        if (hy === -1) y1 = clamp(start.y + dyN, 0, y2 - MIN);
        rect = { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
      } else {
        const anchorX = hx === 1 ? start.x : start.x + start.w;
        const anchorY = hy === 1 ? start.y : start.y + start.h;
        const curX = clamp((hx === 1 ? start.x + start.w : start.x) + dxN, 0, 1);
        const curY = clamp((hy === 1 ? start.y + start.h : start.y) + dyN, 0, 1);
        let w = Math.abs(curX - anchorX);
        let h = Math.abs(curY - anchorY);
        if (w / arN >= h) h = w / arN; else w = h * arN;
        const availW = hx === 1 ? (1 - anchorX) : anchorX;
        const availH = hy === 1 ? (1 - anchorY) : anchorY;
        if (w > availW) { w = availW; h = w / arN; }
        if (h > availH) { h = availH; w = h * arN; }
        if (w < MIN) { w = MIN; h = w / arN; }
        rect = { x: hx === 1 ? anchorX : anchorX - w, y: hy === 1 ? anchorY : anchorY - h, w, h };
      }
      _renderBox(); _updateReadout();
    }
    function up() {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    }
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  function _renderTabs() {
    tabRow.innerHTML = '';
    _frames.forEach((f, i) => {
      tabRow.appendChild(el('div', {
        style: `flex:1; cursor:pointer; border-radius:6px; overflow:hidden; position:relative; border:2px solid ${i === _active ? 'var(--accent)' : 'var(--border-2)'};`,
        onclick() { _active = i; editorImg.src = f.dataUrl; _renderTabs(); },
      }, [
        el('img', { src: f.dataUrl, style: 'display:block; width:100%; pointer-events:none;' }),
        el('span', { style: 'position:absolute; bottom:3px; right:4px; font-size:.66rem; font-weight:700; color:#fff; background:rgba(0,0,0,.6); padding:1px 5px; border-radius:8px;', text: `${f.t.toFixed(1)}s` }),
      ]));
    });
  }

  const loading = el('div', { style: 'font-size:.8rem; color:var(--text-3);', text: 'Loading preview frames...' });
  stage.appendChild(loading);
  stage.style.opacity = '.5';

  async function _load(path) {
    try {
      const data = await api('/api/tools/crop-frames', { method: 'POST', body: JSON.stringify({ video_path: path }) });
      _srcW = data.width; _srcH = data.height;
      _frames = (data.frames || []).map(f => ({ t: f.t, dataUrl: `data:image/jpeg;base64,${f.b64}` }));
      if (!_frames.length) { loading.textContent = 'Could not read this video.'; return; }
      _active = 0;
      editorImg.src = _frames[0].dataUrl;
      _fitAspect(); _renderTabs(); _renderBox(); _updateReadout();
      _loaded = true;
      loading.style.display = 'none';
      stage.style.opacity = '';
    } catch (e) {
      loading.textContent = e.message || 'Failed to load preview.';
    }
  }

  const cur = source.get();
  if (cur) _load(cur.path);
  // If the source changes while this step exists, reload against the new video.
  source.onChange(s => { if (s) { _loaded = false; loading.style.display = ''; loading.textContent = 'Loading preview frames...'; stage.style.opacity = '.5'; _load(s.path); } });

  return {
    getRect: () => (_loaded ? { x: rect.x, y: rect.y, w: rect.w, h: rect.h } : null),
    getKeepAudio: () => keepAudioChk.checked,
  };
}

// -- AI Music (generate + mix a soundtrack) ------------------------------------

function _buildAudioSection(root) {
  let _selectedVideo = null;
  let _activeJobId   = null;

  // -- Session video picker ------------------------------------------------
  const pickerCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(pickerCard);

  const pickerHeader = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-bottom:10px;' });
  pickerCard.appendChild(pickerHeader);
  pickerHeader.appendChild(el('span', { style: 'font-size:.85rem; font-weight:600; flex:1;', text: 'Add AI Music to a Video' }));

  const refreshBtn = el('button', { class: 'btn btn-sm', text: '↻ Refresh' });
  pickerHeader.appendChild(refreshBtn);

  const fileInput = el('input', { type: 'file', accept: 'video/*', style: 'display:none' });
  pickerCard.appendChild(fileInput);
  const openFileBtn = el('button', { class: 'btn btn-sm', text: 'Open file...' });
  pickerHeader.appendChild(openFileBtn);
  openFileBtn.addEventListener('click', () => fileInput.click());

  const videoList = el('div', { style: 'display:flex; flex-direction:column; gap:4px; max-height:220px; overflow-y:auto;' });
  pickerCard.appendChild(videoList);

  // Selected strip
  const selectedCard = el('div', { class: 'card', style: 'display:none; padding:10px 14px; align-items:center; gap:12px;' });
  root.appendChild(selectedCard);
  const selectedName = el('div', { style: 'font-size:.82rem; font-weight:600; color:var(--text-2); flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;' });
  const clearSelBtn  = el('button', { class: 'btn btn-sm', text: 'x Clear', style: 'flex-shrink:0;',
    onclick() { _selectedVideo = null; selectedCard.style.display = 'none'; _refreshList(); },
  });
  selectedCard.appendChild(el('span', { style: 'font-size:.7rem; font-weight:700; color:var(--text-3); flex-shrink:0;', text: 'VID' }));
  selectedCard.appendChild(selectedName);
  selectedCard.appendChild(clearSelBtn);

  function _applyVideo(path) {
    _selectedVideo = path;
    selectedName.textContent = path.split(/[\\/]/).pop();
    selectedCard.style.display = 'flex';
    _refreshList();
  }

  fileInput.addEventListener('change', async () => {
    if (!fileInput.files?.length) return;
    try {
      const data = await apiUpload('/api/tools/upload', Array.from(fileInput.files));
      if (data.rejected?.length) toast(`Skipped non-video file${data.rejected.length > 1 ? 's' : ''}: ${data.rejected.join(', ')}`, 'error');
      const f = data.files?.[0];
      if (f) _applyVideo(f.path);
    } catch (e) { toast(e.message, 'error'); }
    fileInput.value = '';
  });

  async function _refreshList() {
    try {
      const data = await api('/api/gallery?limit=40');
      const vids = (data.items || data || [])
        .filter(i => /(\.mp4|\.webm|\.mov)$/i.test(i.url || ''))
        .slice(0, 30);
      videoList.innerHTML = '';
      if (!vids.length) {
        videoList.appendChild(el('div', {
          style: 'text-align:center; padding:24px 0; color:var(--text-3); font-size:.82rem;',
          text: 'No generated videos yet -- create some in Create Videos first.',
        }));
        return;
      }
      for (const v of vids) {
        const vpath = v.metadata?.path || v.url;
        const vname = vpath.split(/[\\/]/).pop();
        const isSelected = _selectedVideo === vpath;
        const row = el('div', {
          style: `display:flex; align-items:center; gap:10px; padding:7px 10px; border-radius:6px; cursor:pointer; background:var(--bg-raised); border:1px solid ${isSelected ? 'var(--accent)' : 'var(--border-2)'};`,
        });
        row.appendChild(el('span', { style: 'font-size:.7rem; font-weight:700; color:var(--text-3); flex-shrink:0; width:28px;', text: 'VID' }));
        row.appendChild(el('span', { style: 'flex:1; font-size:.8rem; color:var(--text-2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;', text: vname }));
        row.appendChild(el('span', {
          style: `font-size:.7rem; flex-shrink:0; padding:2px 7px; border-radius:10px; font-weight:600; ${isSelected ? 'color:var(--accent); background:color-mix(in srgb,var(--accent) 15%,transparent);' : 'color:var(--text-3); background:var(--bg);'}`,
          text: isSelected ? 'v Selected' : 'Select',
        }));
        row.addEventListener('click', () => _applyVideo(vpath));
        videoList.appendChild(row);
      }
    } catch (e) { toast(e.message, 'error'); }
  }

  refreshBtn.addEventListener('click', _refreshList);
  _refreshList();
  if (_sessionListener) window.removeEventListener('session-updated', _sessionListener);
  _sessionListener = _refreshList;
  window.addEventListener('session-updated', _sessionListener);

  // -- Music options --------------------------------------------------------
  const optionsCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(optionsCard);
  optionsCard.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:12px;', text: 'Music Options' }));

  optionsCard.appendChild(el('div', { style: 'font-size:.82rem; color:var(--text-3); margin-bottom:4px;', text: 'Music Prompt' }));
  optionsCard.appendChild(el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-bottom:6px; line-height:1.5;',
    text: 'Leave blank -- the AI will analyze your video and write the prompt. Or guide it with genre, mood, and instruments.' }));
  const musicPromptTA = el('textarea', { rows: '2', style: 'width:100%; resize:vertical; font-size:.88rem; margin-bottom:10px;',
    placeholder: 'e.g. "lo-fi hip hop, dusty vinyl warmth, slow jazz brushwork" -- or leave blank for auto' });
  optionsCard.appendChild(musicPromptTA);

  optionsCard.appendChild(el('div', { style: 'font-size:.82rem; color:var(--text-3); margin-bottom:4px;', text: 'Direction Guidelines (optional)' }));
  optionsCard.appendChild(el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-bottom:6px; line-height:1.5;',
    text: 'Hints for the AI music director: tempo, energy, era, mood, what to avoid.' }));
  const directionTA = el('textarea', { rows: '2', style: 'width:100%; resize:vertical; font-size:.88rem; margin-bottom:10px;',
    placeholder: 'e.g. "Keep it under 90 BPM. Avoid anything too upbeat. Think late-night, cinematic, melancholic."' });
  optionsCard.appendChild(directionTA);

  const instrChk = el('input', { type: 'checkbox', id: 'audio-instrumental', checked: 'true' });
  optionsCard.appendChild(el('div', { style: 'display:flex; gap:8px; align-items:center;' }, [
    instrChk,
    el('label', { for: 'audio-instrumental', style: 'cursor:pointer; font-size:.85rem;', text: 'Instrumental only (no AI-generated lyrics)' }),
  ]));

  // -- Generate -------------------------------------------------------------
  const genBtn = el('button', {
    class: 'btn btn-primary btn-generate',
    text: 'Generate & Mix Music',
    style: 'width:100%; font-size:1.05rem; padding:13px; font-weight:700;',
  });
  root.appendChild(genBtn);

  const progWrap = el('div');
  root.appendChild(progWrap);
  const prog = createProgressCard(progWrap);
  prog.onCancel(async () => {
    if (_activeJobId) { await stopJob(_activeJobId).catch(() => {}); toast('Stopping...', 'info'); _activeJobId = null; }
  });

  const vidWrap = el('div');
  root.appendChild(vidWrap);
  const player = createVideoPlayer(vidWrap);
  player.onStartOver(() => player.hide());

  genBtn.addEventListener('click', async () => {
    if (!_selectedVideo) { toast('Select a video first', 'error'); return; }
    genBtn.disabled = true;
    prog.show();
    prog.update(0, 'Starting...');
    player.hide();

    try {
      const { job_id } = await api('/api/fun/add-music', {
        method: 'POST',
        body: JSON.stringify({
          video_path:     _selectedVideo,
          music_prompt:   musicPromptTA.value.trim(),
          user_direction: directionTA.value.trim(),
          instrumental:   instrChk.checked,
        }),
      });
      _activeJobId = job_id;

      pollJob(job_id,
        j => prog.update(j.progress || 0, j.message || 'Working...'),
        j => {
          prog.hide();
          genBtn.disabled = false;
          _activeJobId = null;
          if (j.output) {
            player.show(pathToUrl(j.output), j.output);
            pushToGallery('video-tools', j.output, musicPromptTA.value.trim() || 'AI music', null, {});
            toast('Music added!', 'success');
          }
        },
        err => {
          prog.hide();
          genBtn.disabled = false;
          _activeJobId = null;
          toast(typeof err === 'string' ? err : (err?.message || 'Failed'), 'error');
        },
      );
    } catch (e) {
      prog.hide();
      genBtn.disabled = false;
      toast(e.message, 'error');
    }
  });
}

// -- Divider -------------------------------------------------------------------

function _buildDivider(root, label) {
  const div = el('div', { style: 'display:flex; align-items:center; gap:12px;' });
  div.appendChild(el('div', { style: 'flex:1; height:1px; background:var(--border);' }));
  div.appendChild(el('span', { style: 'font-size:.68rem; font-weight:800; letter-spacing:.12em; color:var(--text-3); opacity:.6; text-transform:uppercase; white-space:nowrap;', text: label || 'Batch Processing' }));
  div.appendChild(el('div', { style: 'flex:1; height:1px; background:var(--border);' }));
  root.appendChild(div);
}

// -- Crop aspect presets -------------------------------------------------------

const _CROP_ASPECTS = [
  { label: 'Freeform',        value: 'free' },
  { label: 'Square 1:1',      value: '1:1'  },
  { label: 'Portrait 9:16',   value: '9:16' },
  { label: 'Portrait 4:5',    value: '4:5'  },
  { label: 'Landscape 16:9',  value: '16:9' },
  { label: 'Landscape 4:3',   value: '4:3'  },
];
