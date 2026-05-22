/**
 * Infinite Zoom tab -- zoom in or out from a photo or video.
 * Source -> direction -> steps -> Generate
 */
import { pollJob, stopJob, apiUpload } from './api.js?v=20260505e';
import { el, pathToUrl } from './components.js?v=20260507a';
import { toast, apiFetch } from './shell/toast.js?v=20260518a';

export function init(panel) {
  panel.innerHTML = '';
  const root = el('div', {
    style: 'max-width:680px; margin:0 auto; padding:24px 16px; display:flex; flex-direction:column; gap:18px;',
  });
  panel.appendChild(root);

  let _sourcePath = null;
  let _isVideo    = false;
  let _direction  = 'out';
  let _jobId      = null;
  let _modelData  = {};   // cached from /api/fun/models
  let _gpuVram    = 0;

  // ── helpers ────────────────────────────────────────────────────────────────
  const LABEL = s => el('div', {
    style: 'font-size:11px; font-weight:600; letter-spacing:.08em; text-transform:uppercase; color:var(--text-3);',
    text: s,
  });

  function _chip(label, value, row, onSelect) {
    const b = el('button', {
      style: [
        'flex:1; padding:7px 4px; border-radius:6px; border:1px solid var(--border-2);',
        'background:var(--surface); cursor:pointer; font-size:13px; font-weight:600;',
        'color:var(--text-2); transition:all .12s; white-space:nowrap;',
      ].join(''),
    });
    b.textContent = label;
    b.dataset.value = value;
    b.onclick = () => {
      row.querySelectorAll('button').forEach(x => {
        x.style.borderColor = 'var(--border-2)';
        x.style.background  = 'var(--surface)';
        x.style.color       = 'var(--text-2)';
        delete x.dataset.active;
      });
      b.style.borderColor = 'var(--accent-border)';
      b.style.background  = 'var(--accent-bg)';
      b.style.color       = 'var(--accent)';
      b.dataset.active    = '1';
      onSelect(value);
    };
    return b;
  }

  function _activeValue(row) {
    return row.querySelector('button[data-active]')?.dataset.value;
  }

  function _card(content) {
    const c = el('div', {
      style: 'background:var(--surface); border:1px solid var(--border-2); border-radius:var(--r-lg); padding:16px;',
    });
    c.append(...[content].flat());
    return c;
  }

  // ── source drop zone ───────────────────────────────────────────────────────
  const fileInput = el('input', { type: 'file', accept: 'image/*,video/*', style: 'display:none' });
  panel.appendChild(fileInput);

  const previewImg = el('img', {
    style: 'max-height:180px; max-width:100%; border-radius:6px; display:none; object-fit:contain; margin:0 auto;',
  });
  const dropHint = el('div', {
    style: 'display:flex; flex-direction:column; align-items:center; gap:8px; padding:20px 0;',
  });
  const dropIcon = el('div', { style: 'font-size:36px; color:var(--text-3);', text: '+' });
  const dropTextEl = el('div', {
    style: 'font-size:13px; color:var(--text-2);',
    text: 'Drop a photo or video, or click to browse',
  });
  dropHint.append(dropIcon, dropTextEl);

  const sourceInfo = el('div', {
    style: 'display:none; align-items:center; justify-content:space-between; font-size:12px; color:var(--text-2); padding-top:8px;',
  });
  const sourceNameEl = el('span');
  const clearBtn = el('button', {
    style: 'background:none; border:none; color:var(--red); cursor:pointer; font-size:11px; padding:0;',
    text: 'clear',
  });
  clearBtn.onclick = e => { e.stopPropagation(); _clearSource(); };
  sourceInfo.append(sourceNameEl, clearBtn);

  const dropArea = el('div', {
    style: [
      'border:2px dashed var(--border-2); border-radius:var(--r-lg); cursor:pointer;',
      'transition:border-color .15s, background .15s; background:var(--surface);',
    ].join(''),
  });
  dropArea.append(dropHint, previewImg, sourceInfo);

  dropArea.addEventListener('dragover', e => {
    e.preventDefault();
    dropArea.style.borderColor = 'var(--accent)';
    dropArea.style.background  = 'var(--accent-bg)';
  });
  dropArea.addEventListener('dragleave', () => {
    dropArea.style.borderColor = 'var(--border-2)';
    dropArea.style.background  = 'var(--surface)';
  });
  dropArea.addEventListener('drop', e => {
    e.preventDefault();
    dropArea.style.borderColor = 'var(--border-2)';
    dropArea.style.background  = 'var(--surface)';
    if (e.dataTransfer.files[0]) _uploadFile(e.dataTransfer.files[0]);
  });
  dropArea.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    if (fileInput.files[0]) _uploadFile(fileInput.files[0]);
    fileInput.value = '';
  });

  function _clearSource() {
    _sourcePath = null; _isVideo = false;
    previewImg.style.display = 'none'; previewImg.src = '';
    dropHint.style.display = 'flex';
    sourceInfo.style.display = 'none';
    _updateBtn();
  }

  async function _uploadFile(file) {
    const isVid = file.type.startsWith('video/');
    dropTextEl.textContent = 'Uploading...';
    try {
      const resp = await apiUpload(isVid ? '/api/fun/upload-video' : '/api/fun/upload', [file]);
      const data = resp?.files?.[0];
      if (!data?.path) throw new Error('No path returned');
      _sourcePath = data.path; _isVideo = isVid;

      if (isVid) {
        const fr = await apiFetch('/api/zoom/extract-frame', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ video_path: _sourcePath, time_sec: _direction === 'out' ? -1 : 0.1 }),
        }).catch(() => null);
        if (fr?.frame_url) { previewImg.src = fr.frame_url; previewImg.style.display = 'block'; }
      } else {
        previewImg.src = pathToUrl(data.path);
        previewImg.style.display = 'block';
      }

      dropHint.style.display = 'none';
      sourceNameEl.textContent = isVid ? `${file.name} (video)` : file.name;
      sourceInfo.style.display = 'flex';
      _updateBtn();
    } catch (err) {
      toast('Upload failed: ' + err.message, 'error');
      dropTextEl.textContent = 'Drop a photo or video, or click to browse';
    }
  }

  // ── direction ──────────────────────────────────────────────────────────────
  const dirRow = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:10px;' });

  function _dirBtn(label, sub, value) {
    const b = el('button', {
      style: [
        'display:flex; flex-direction:column; align-items:center; gap:4px; padding:14px;',
        'border-radius:var(--r-md); border:2px solid var(--border-2);',
        'background:var(--surface); cursor:pointer; transition:all .15s;',
      ].join(''),
    });
    b.append(
      el('span', { style: 'font-size:15px; font-weight:700; color:var(--text);', text: label }),
      el('span', { style: 'font-size:11px; color:var(--text-3);', text: sub }),
    );
    b.dataset.value = value;
    b.onclick = () => {
      [btnOut, btnIn].forEach(x => {
        x.style.borderColor = 'var(--border-2)';
        x.style.background  = 'var(--surface)';
      });
      b.style.borderColor = 'var(--accent-border)';
      b.style.background  = 'var(--accent-bg)';
      _direction = value;
    };
    return b;
  }

  const btnOut = _dirBtn('Zoom Out', 'Reveals surroundings', 'out');
  const btnIn  = _dirBtn('Zoom In',  'Approaches detail',    'in');
  dirRow.append(btnOut, btnIn);
  btnOut.style.borderColor = 'var(--accent-border)';
  btnOut.style.background  = 'var(--accent-bg)';

  // ── clips + duration ───────────────────────────────────────────────────────
  // Each "clip" = one WanGP generation. They chain losslessly (last frame ->
  // next clip's first frame) to produce a continuous zoom movement.
  const totalEstEl = el('div', {
    style: 'font-size:11px; color:var(--text-3); text-align:right; align-self:flex-end; padding-bottom:2px;',
  });

  const stepsRow = el('div', { style: 'display:flex; gap:6px; flex-wrap:wrap;' });
  const durRow   = el('div', { style: 'display:flex; gap:6px; flex-wrap:wrap;' });

  function _updateEst() {
    const clips = Number(_activeValue(stepsRow)) || 5;
    const secs  = Number(_activeValue(durRow))   || 4;
    const total = clips * secs;
    const m = Math.floor(total / 60);
    const s = total % 60;
    totalEstEl.textContent = `~${m > 0 ? m + 'm ' : ''}${s > 0 ? s + 's' : ''} video`;
  }

  [3, 4, 5, 6, 8, 10, 12].forEach((n, i) => {
    const b = _chip(n, n, stepsRow, _updateEst);
    if (i === 2) b.click();   // default: 5 clips
    stepsRow.appendChild(b);
  });

  [4, 5, 6, 8, 10, 12, 15].forEach((n, i) => {
    const b = _chip(`${n}s`, n, durRow, _updateEst);
    if (i === 0) b.click();   // default: 4s per clip
    durRow.appendChild(b);
  });

  _updateEst();

  const controlsGrid = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:16px;' });
  const stepsGroup = el('div', { style: 'display:flex; flex-direction:column; gap:8px;' });
  stepsGroup.append(LABEL('Clips in chain'), stepsRow);
  const durGroup = el('div', { style: 'display:flex; flex-direction:column; gap:8px;' });
  durGroup.append(
    el('div', { style: 'display:flex; justify-content:space-between; align-items:baseline;' },
      [LABEL('Seconds per clip'), totalEstEl]),
    durRow,
  );
  controlsGrid.append(stepsGroup, durGroup);

  // ── idea ───────────────────────────────────────────────────────────────────
  const ideaInput = el('textarea', {
    placeholder: '"reveal a foggy mountain valley"  or  "zoom into the ring on her finger"',
    rows: 2,
    style: [
      'width:100%; box-sizing:border-box; background:var(--surface-2); border:1px solid var(--border-2);',
      'border-radius:var(--r-md); padding:10px 12px; color:var(--text); font-size:13px;',
      'resize:none; font-family:inherit; outline:none;',
    ].join(''),
  });

  // ── model ──────────────────────────────────────────────────────────────────
  const vramNote = el('span', { style: 'font-size:11px; color:var(--text-3);' });
  const modelLabelRow = el('div', {
    style: 'display:flex; align-items:center; justify-content:space-between;',
  });
  modelLabelRow.append(LABEL('Model'), vramNote);

  const modelSel = el('select', {
    style: [
      'width:100%; background:var(--surface-2); border:1px solid var(--border-2);',
      'border-radius:var(--r-md); padding:9px 12px; color:var(--text); font-size:13px;',
      'cursor:pointer; outline:none; margin-top:6px;',
    ].join(''),
  });

  const modelWarn = el('div', {
    style: 'font-size:11px; color:var(--red); display:none; padding-top:4px;',
  });

  const modelGroup = el('div');
  modelGroup.append(modelLabelRow, modelSel, modelWarn, audioFirstToggle);

  apiFetch('/api/fun/models').then(data => {
    _modelData = data.models || {};
    _gpuVram   = data.gpu_vram_gb || 0;
    if (_gpuVram) vramNote.textContent = `${_gpuVram} GB GPU`;

    const i2v = Object.entries(_modelData).filter(([, m]) => m.i2v)
                       .sort(([a], [b]) => a.localeCompare(b));
    modelSel.innerHTML = '';
    for (const [name, info] of i2v) {
      const fits = !_gpuVram || _gpuVram >= (info.vram_min_gb || 0);
      const opt  = el('option', { value: name });
      opt.textContent = fits ? name : `${name}  (needs ${info.vram_min_gb} GB)`;
      if (!fits) opt.style.color = 'var(--red)';
      modelSel.appendChild(opt);
    }
    const fits = i2v.filter(([, m]) => !_gpuVram || _gpuVram >= (m.vram_min_gb || 0));
    const best = fits.find(([name]) => name === 'LTX-2 Dev19B Distilled') || fits[0];
    if (best) modelSel.value = best[0];
    _checkModelVram();
  }).catch(() => {
    modelSel.appendChild(el('option', { value: 'LTX-2 Dev19B Distilled', text: 'LTX-2 Dev19B Distilled' }));
  });

  function _checkModelVram() {
    const info = _modelData[modelSel.value];
    const needs = info?.vram_min_gb || 0;
    if (_gpuVram && needs > _gpuVram) {
      modelWarn.textContent = `This model needs ${needs} GB -- you have ${_gpuVram} GB. May fail.`;
      modelWarn.style.display = 'block';
    } else {
      modelWarn.style.display = 'none';
    }
  }
  modelSel.addEventListener('change', _checkModelVram);

  // ── audio-first toggle ─────────────────────────────────────────────────────
  const audioFirstToggle = el('label', {
    style: 'display:flex; align-items:center; gap:8px; cursor:pointer; font-size:13px; color:var(--text-2); user-select:none;',
  });
  const audioFirstCheck = el('input', { type: 'checkbox' });
  audioFirstToggle.append(
    audioFirstCheck,
    el('span', { textContent: 'Sync video to music (generates audio first, takes longer)' }),
  );

  // ── generate button + queue badge ──────────────────────────────────────────
  let _activeCount = 0;

  const generateBtn = el('button', {
    disabled: true,
    style: [
      'padding:14px; border-radius:var(--r-lg); border:none; cursor:not-allowed;',
      'font-size:15px; font-weight:700; letter-spacing:.04em;',
      'background:var(--circus-red); color:var(--text); opacity:.45; transition:opacity .15s;',
    ].join(''),
    text: 'Generate Zoom',
  });

  const queueBadge = el('div', {
    style: 'display:none; font-size:12px; color:var(--text-2); text-align:center; padding-top:4px;',
  });

  function _updateBtn() {
    const ok = !!_sourcePath;
    generateBtn.disabled = !ok;
    generateBtn.style.opacity = ok ? '1' : '.45';
    generateBtn.style.cursor  = ok ? 'pointer' : 'not-allowed';
    generateBtn.textContent = _activeCount > 0 ? `+ Add to Queue (${_activeCount} running)` : 'Generate Zoom';
  }

  function _incActive() { _activeCount++; _updateBtn(); queueBadge.style.display = 'block'; queueBadge.textContent = `${_activeCount} zoom job${_activeCount > 1 ? 's' : ''} in queue -- see Queue tab for progress`; }
  function _decActive() { _activeCount = Math.max(0, _activeCount - 1); _updateBtn(); if (_activeCount === 0) queueBadge.style.display = 'none'; }

  // ── progress ───────────────────────────────────────────────────────────────
  const progressLabel = el('div', { style: 'font-size:13px; color:var(--text-2);' });
  const progressTrack = el('div', {
    style: 'height:4px; background:var(--border-2); border-radius:2px; overflow:hidden;',
  });
  const progressFill = el('div', {
    style: 'height:100%; width:0%; background:var(--accent); border-radius:2px; transition:width .4s;',
  });
  progressTrack.appendChild(progressFill);

  const cancelBtn = el('button', {
    style: [
      'align-self:flex-start; padding:5px 12px; border-radius:var(--r-sm);',
      'border:1px solid var(--border-2); background:transparent;',
      'color:var(--text-3); cursor:pointer; font-size:11px;',
    ].join(''),
    text: 'Cancel',
  });
  cancelBtn.onclick = () => { if (_jobId) stopJob(_jobId); };

  const progressArea = el('div', {
    style: 'display:none; flex-direction:column; gap:8px;',
  });
  progressArea.append(progressLabel, progressTrack, cancelBtn);

  // ── output ─────────────────────────────────────────────────────────────────
  const videoEl = el('video', {
    controls: true, loop: true, playsInline: true, src: '',
    style: 'width:100%; display:block; border-radius:var(--r-lg); background:#000;',
  });
  const outputActions = el('div', { style: 'display:flex; gap:8px; flex-wrap:wrap;' });

  function _actionBtn(label, fn) {
    const b = el('button', {
      style: [
        'padding:7px 14px; border-radius:var(--r-sm);',
        'border:1px solid var(--border-2); background:var(--surface);',
        'color:var(--text-2); cursor:pointer; font-size:12px; transition:background .12s;',
      ].join(''),
      text: label,
    });
    b.onclick = fn;
    return b;
  }

  const outputArea = el('div', { style: 'display:none; flex-direction:column; gap:12px;' });
  outputArea.append(videoEl, outputActions);

  // ── folder batch ───────────────────────────────────────────────────────────
  let _folderFiles  = [];
  let _folderPath   = '';
  let _loopActive   = false;
  let _loopPollTimer = null;

  const batchDivider = el('div', {
    style: 'display:flex; align-items:center; gap:10px; color:var(--text-3); font-size:11px; padding-top:4px;',
  });
  batchDivider.innerHTML = '<hr style="flex:1;border:none;border-top:1px solid var(--border-2)"> or process a whole folder <hr style="flex:1;border:none;border-top:1px solid var(--border-2)">';

  // folder picker row
  const folderNameEl = el('div', {
    style: 'flex:1; font-size:13px; color:var(--text-2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; padding:9px 0;',
    text: 'No folder selected',
  });
  const browseFolderBtn = el('button', {
    text: 'Choose Folder',
    style: [
      'padding:9px 16px; border-radius:var(--r-md); border:1px solid var(--accent-border);',
      'background:var(--accent-bg); color:var(--accent); cursor:pointer; font-size:13px; font-weight:600; white-space:nowrap;',
    ].join(''),
  });

  // loop toggle
  const loopToggle = el('label', {
    style: 'display:flex; align-items:center; gap:8px; cursor:pointer; font-size:13px; color:var(--text-2); user-select:none;',
  });
  const loopCheck = el('input', { type: 'checkbox' });
  loopCheck.style.accentColor = 'var(--accent)';
  loopToggle.append(loopCheck, 'Loop continuously');

  const folderStatus = el('div', {
    style: 'font-size:12px; color:var(--text-3); min-height:16px;',
  });

  const batchBtn = el('button', {
    text: 'Queue All',
    disabled: true,
    style: [
      'padding:11px; border-radius:var(--r-lg); border:none; cursor:not-allowed;',
      'font-size:14px; font-weight:700; background:var(--circus-red); color:var(--text); opacity:.45;',
    ].join(''),
  });

  function _setBatchReady() {
    const n = _folderFiles.length;
    if (!n) {
      batchBtn.disabled = true;
      batchBtn.style.opacity = '.45';
      batchBtn.style.cursor = 'not-allowed';
      batchBtn.textContent = 'Queue All';
      return;
    }
    batchBtn.disabled = false;
    batchBtn.style.opacity = '1';
    batchBtn.style.cursor = 'pointer';
    batchBtn.textContent = loopCheck.checked ? `Start Loop (${n} files)` : `Queue All ${n} Files`;
  }

  loopCheck.addEventListener('change', _setBatchReady);

  async function _scanFolder(folder) {
    folderStatus.textContent = 'Scanning...';
    _folderFiles = [];
    _setBatchReady();
    try {
      const r = await apiFetch('/api/zoom/scan-folder', {
        method: 'POST', body: JSON.stringify({ folder }),
      });
      _folderFiles = r.files || [];
      const imgs   = _folderFiles.filter(f => !f.is_video).length;
      const vids   = _folderFiles.filter(f =>  f.is_video).length;
      if (!_folderFiles.length) {
        folderStatus.textContent = 'No supported files found (jpg, png, mp4, mov, ...)';
      } else {
        const parts = [];
        if (imgs) parts.push(`${imgs} image${imgs !== 1 ? 's' : ''}`);
        if (vids) parts.push(`${vids} video${vids !== 1 ? 's' : ''}`);
        folderStatus.textContent = parts.join(', ') + ' found';
      }
      _setBatchReady();
    } catch (e) {
      folderStatus.textContent = 'Error: ' + e.message;
    }
  }

  browseFolderBtn.onclick = async () => {
    if (_loopActive) return;
    try {
      const r = await apiFetch('/api/browse-folder', { method: 'POST' });
      const picked = r.folder || r.path;
      if (picked) {
        _folderPath = picked;
        folderNameEl.textContent = picked.split(/[\\/]/).pop() || picked;
        folderNameEl.title = picked;
        await _scanFolder(picked);
      }
    } catch {}
  };

  // -- loop poll --
  function _stopLoopPoll() {
    if (_loopPollTimer) { clearInterval(_loopPollTimer); _loopPollTimer = null; }
  }

  async function _pollLoop() {
    try {
      const snap = await apiFetch('/api/zoom/folder-loop/status');
      const total = snap.files?.length || _folderFiles.length;
      if (snap.active) {
        const lap  = snap.lap > 0 ? ` lap ${snap.lap + 1}` : '';
        batchBtn.textContent = `Stop Loop (${snap.index}/${total}${lap})`;
        batchBtn.disabled = false;
        batchBtn.style.opacity = '1';
        folderStatus.textContent = snap.current_file
          ? `Processing: ${snap.current_file} -- ${snap.succeeded} done, ${snap.failed} failed`
          : `Running...`;
      } else {
        // Loop finished
        _loopActive = false;
        _stopLoopPoll();
        batchBtn.textContent = `Start Loop (${total} files)`;
        browseFolderBtn.disabled = false;
        const msg = snap.status === 'done'
          ? `Loop done -- ${snap.succeeded} succeeded, ${snap.failed} failed`
          : `Loop stopped -- ${snap.succeeded} done, ${snap.failed} failed`;
        folderStatus.textContent = msg;
        toast(msg, snap.failed ? 'error' : 'success');
      }
    } catch {}
  }

  batchBtn.onclick = async () => {
    if (!_folderFiles.length && !_loopActive) return;

    // Stop loop if running
    if (_loopActive) {
      await apiFetch('/api/zoom/folder-loop/stop', { method: 'POST' }).catch(() => {});
      _loopActive = false;
      _stopLoopPoll();
      batchBtn.textContent = `Start Loop (${_folderFiles.length} files)`;
      folderStatus.textContent = 'Loop stopped.';
      browseFolderBtn.disabled = false;
      return;
    }

    const nClips  = Number(_activeValue(stepsRow)) || 4;
    const clipDur = Number(_activeValue(durRow))   || 5;
    const idea    = ideaInput.value.trim();
    const body    = {
      folder:         _folderPath,
      zoom_direction: _direction,
      n_clips:        nClips,
      clip_duration:  clipDur,
      idea,
      model_name:     modelSel.value,
      skip_audio:     false,
      audio_first:    audioFirstCheck.checked,
      repeat:         loopCheck.checked,
    };

    if (loopCheck.checked) {
      // Server-side loop
      batchBtn.disabled = true;
      browseFolderBtn.disabled = true;
      try {
        await apiFetch('/api/zoom/folder-loop/start', { method: 'POST', body: JSON.stringify(body) });
        _loopActive = true;
        batchBtn.disabled = false;
        batchBtn.textContent = `Stop Loop (0/${_folderFiles.length})`;
        folderStatus.textContent = 'Loop started -- processing one file at a time...';
        _loopPollTimer = setInterval(_pollLoop, 5000);
      } catch (e) {
        batchBtn.disabled = false;
        browseFolderBtn.disabled = false;
        toast('Failed to start loop: ' + e.message, 'error');
        _setBatchReady();
      }
    } else {
      // Queue all at once
      batchBtn.disabled = true;
      let queued = 0;
      for (const f of _folderFiles) {
        batchBtn.textContent = `Queuing ${queued + 1}/${_folderFiles.length}...`;
        try {
          const res = await apiFetch('/api/zoom/make', {
            method: 'POST',
            body: JSON.stringify({ source_path: f.path, ...body }),
          });
          _incActive();
          pollJob(res.job_id, () => {}, j => {
            _decActive();
            const out = Array.isArray(j.output) ? j.output[0] : j.output;
            if (out) {
              videoEl.src = pathToUrl(out); videoEl.style.opacity = '1'; videoEl.load();
              outputArea.style.display = 'flex';
              outputActions.innerHTML = '';
              outputActions.append(
                _actionBtn('Open in folder', () =>
                  apiFetch('/api/reveal', { method: 'POST', body: JSON.stringify({ path: out, action: 'explorer' }) }).catch(() => {})),
                _actionBtn('Continue zoom...', () => _showExtendPanel(out, _direction)),
              );
            }
            toast(`Zoom done: ${f.name}`, 'success');
            document.dispatchEvent(new CustomEvent('session-updated'));
          }, msg => { _decActive(); toast(`Zoom failed (${f.name}): ${msg}`, 'error'); });
          queued++;
        } catch (e) {
          if (e.status === 429 || /queue.*full/i.test(e.message)) {
            toast(`Queue full at file ${queued + 1} -- ${_folderFiles.length - queued} files not queued`, 'error');
            break;
          }
          toast(`Failed to queue ${f.name}: ${e.message}`, 'error');
        }
      }
      batchBtn.disabled = false;
      batchBtn.textContent = `Queued ${queued} -- check Queue tab`;
      setTimeout(_setBatchReady, 4000);
    }
  };

  const batchSection = el('div', { style: 'display:flex; flex-direction:column; gap:10px;' });
  batchSection.append(
    batchDivider,
    el('div', { style: 'display:flex; gap:8px; align-items:center;' }, [folderNameEl, browseFolderBtn]),
    loopToggle,
    folderStatus,
    batchBtn,
  );

  // ── assemble ───────────────────────────────────────────────────────────────
  root.append(
    _card(dropArea),
    el('div', { style: 'display:flex; flex-direction:column; gap:8px;' },
      [LABEL('Direction'), dirRow]),
    controlsGrid,
    el('div', { style: 'display:flex; flex-direction:column; gap:6px;' },
      [LABEL('What the zoom reveals (optional)'), ideaInput]),
    _card(modelGroup),
    generateBtn,
    progressArea,
    queueBadge,
    batchSection,
    outputArea,
  );

  // ── extend/continue ────────────────────────────────────────────────────────
  let _extendPanel = null;

  function _showExtendPanel(existingVideoPath, capturedDirection) {
    if (_extendPanel) { _extendPanel.remove(); _extendPanel = null; }

    const panel = el('div', {
      style: 'background:var(--bg-2); border:1px solid var(--border); border-radius:8px; padding:14px; display:flex; flex-direction:column; gap:10px;',
    });
    _extendPanel = panel;

    panel.append(el('div', {
      style: 'font-size:12px; font-weight:700; color:var(--text-2); text-transform:uppercase; letter-spacing:.08em;',
      textContent: 'Continue this zoom',
    }));

    // clips count
    const clipsRow = el('div', { style: 'display:flex; align-items:center; gap:8px; font-size:13px; color:var(--text-2);' });
    clipsRow.append(el('span', { textContent: 'Additional clips:' }));
    const clipsInput = el('input', { type: 'number', min: '2', max: '10', value: '3',
      style: 'width:56px; padding:4px 6px; background:var(--bg-1); color:var(--text-1); border:1px solid var(--border); border-radius:4px;' });
    clipsRow.append(clipsInput);

    // duration
    const durRow2 = el('div', { style: 'display:flex; align-items:center; gap:8px; font-size:13px; color:var(--text-2);' });
    durRow2.append(el('span', { textContent: 'Seconds per clip:' }));
    const durInput = el('input', { type: 'number', min: '3', max: '12', value: '4',
      style: 'width:56px; padding:4px 6px; background:var(--bg-1); color:var(--text-1); border:1px solid var(--border); border-radius:4px;' });
    durRow2.append(durInput);

    // idea
    const ideaInput2 = el('textarea', { rows: '2', placeholder: 'Optional: describe what the zoom continues to reveal...',
      style: 'width:100%; resize:vertical; font-size:13px; background:var(--bg-1); color:var(--text-1); border:1px solid var(--border); border-radius:4px; padding:6px;' });

    const btnRow = el('div', { style: 'display:flex; gap:8px;' });

    const submitBtn = el('button', { class: 'btn btn-primary', textContent: 'Continue zoom' });
    const cancelBtn = el('button', { class: 'btn', textContent: 'Cancel',
      style: 'background:var(--bg-3);' });

    cancelBtn.onclick = () => { panel.remove(); _extendPanel = null; };

    submitBtn.onclick = async () => {
      submitBtn.disabled = true;
      submitBtn.textContent = 'Queuing...';
      try {
        const res = await apiFetch('/api/zoom/extend', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            existing_video_path: existingVideoPath,
            zoom_direction:      capturedDirection,
            n_clips:             Number(clipsInput.value) || 3,
            clip_duration:       Number(durInput.value) || 4,
            model_name:          modelSel.value,
            idea:                ideaInput2.value.trim(),
            audio_first:         audioFirstCheck.checked,
          }),
        });
        _incActive();
        panel.remove(); _extendPanel = null;

        // Show progress inline for the extend job
        progressLabel.textContent = 'Extending zoom...';
        progressFill.style.width = '0%';
        progressArea.style.display = 'flex';

        pollJob(res.job_id,
          j => {
            progressFill.style.width = `${j.progress || 0}%`;
            progressLabel.textContent = j.message || `${j.progress || 0}%`;
          },
          j => {
            _decActive();
            progressArea.style.display = 'none';
            const out = Array.isArray(j.output) ? j.output[0] : j.output;
            if (out) {
              videoEl.src = pathToUrl(out); videoEl.style.opacity = '1'; videoEl.load();
              outputArea.style.display = 'flex';
              outputActions.innerHTML = '';
              outputActions.append(
                _actionBtn('Open in folder', () =>
                  apiFetch('/api/reveal', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path: out, action: 'explorer' }) }).catch(() => {})),
                _actionBtn('Send to Bridges', () => {
                  document.dispatchEvent(new CustomEvent('dcs:handoff', { detail: { from: 'zoom', to: 'bridges', path: out } }));
                  toast('Sent to Bridges', 'success');
                }),
                _actionBtn('Continue zoom...', () => _showExtendPanel(out, capturedDirection)),
              );
            }
            toast('Zoom extended!', 'success');
            document.dispatchEvent(new CustomEvent('session-updated'));
          },
          msg => {
            _decActive();
            progressArea.style.display = 'none';
            progressFill.style.width = '0%';
            toast(`Extend failed: ${msg}`, 'error');
          },
        );
      } catch (err) {
        if (err.status === 429 || /queue.*full/i.test(err.message)) {
          toast('Queue full -- wait for a job to finish first', 'error');
        } else {
          toast('Failed: ' + err.message, 'error');
        }
      } finally {
        // Re-enable only if panel is still in the DOM (not removed by success path)
        if (panel.isConnected) {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Continue zoom';
        }
      }
    };

    btnRow.append(submitBtn, cancelBtn);
    panel.append(clipsRow, durRow2, ideaInput2, btnRow);

    // Insert the panel below outputArea
    outputArea.after(panel);
  }

  // ── generate ───────────────────────────────────────────────────────────────
  generateBtn.onclick = async () => {
    if (!_sourcePath) return;
    const nClips  = Number(_activeValue(stepsRow)) || 4;
    const clipDur = Number(_activeValue(durRow))   || 5;

    let queueDepth = 0;
    try {
      const qs = await apiFetch('/api/jobs');
      queueDepth = (qs.running?.length || 0) + (qs.queued?.length || 0);
    } catch {}

    generateBtn.disabled = true;
    generateBtn.textContent = 'Queuing...';

    try {
      const res = await apiFetch('/api/zoom/make', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_path:    _sourcePath,
          zoom_direction: _direction,
          n_clips:        nClips,
          clip_duration:  clipDur,
          idea:           ideaInput.value.trim(),
          model_name:     modelSel.value,
          skip_audio:     false,
          audio_first:    audioFirstCheck.checked,
        }),
      });

      _jobId = res.job_id;
      _incActive();

      // Show inline progress bar
      progressLabel.textContent = queueDepth > 0
        ? `Queued (#${queueDepth + 1}) -- waiting for GPU...`
        : 'Starting...';
      progressFill.style.width = '0%';
      progressArea.style.display = 'flex';

      pollJob(res.job_id,
        j => {
          // Progress tick
          const pct = j.progress || 0;
          progressFill.style.width = `${pct}%`;
          progressLabel.textContent = j.message || `${pct}%`;
        },
        j => {
          // Done
          _jobId = null;
          _decActive();
          progressArea.style.display = 'none';
          if (_extendPanel) { _extendPanel.remove(); _extendPanel = null; }
          const out = Array.isArray(j.output) ? j.output[0] : j.output;
          if (out) {
            videoEl.src = pathToUrl(out);
            videoEl.style.opacity = '1';
            videoEl.load();
            outputArea.style.display = 'flex';
            outputActions.innerHTML = '';
            outputActions.append(
              _actionBtn('Open in folder', () =>
                apiFetch('/api/reveal', {
                  method: 'POST', headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ path: out, action: 'explorer' }),
                }).catch(() => {})),
              _actionBtn('Send to Bridges', () => {
                document.dispatchEvent(new CustomEvent('dcs:handoff',
                  { detail: { from: 'zoom', to: 'bridges', path: out } }));
                toast('Sent to Bridges', 'success');
              }),
              _actionBtn('Continue zoom...', () => _showExtendPanel(out, _direction)),
            );
          }
          toast(`Zoom ${_direction} complete!`, 'success');
          document.dispatchEvent(new CustomEvent('session-updated'));
        },
        msg => {
          // Error
          _jobId = null;
          _decActive();
          progressArea.style.display = 'none';
          progressFill.style.width = '0%';
          toast(`Zoom failed: ${msg}`, 'error');
        },
      );

    } catch (err) {
      if (err.status === 429 || /queue.*full|full.*queue/i.test(err.message)) {
        toast('Queue is full -- open the Queue tab and wait for a job to finish', 'error');
      } else {
        toast('Failed: ' + err.message, 'error');
      }
    } finally {
      _updateBtn();
    }
  };
}
