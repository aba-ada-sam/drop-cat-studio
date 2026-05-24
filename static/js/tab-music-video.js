/**
 * Music Video Tab
 * Upload a song + folder of images -> generate beat-synced music videos continuously.
 * Also supports single-image generation for one-off videos.
 *
 * Batch runner is SERVER-SIDE: state persists across DCS restarts.
 * On tab open, the tab auto-resumes any in-progress batch.
 */

import { el }                    from './components.js?v=20260508a';
import { apiFetch }              from './shell/toast.js?v=20260518a';
import { toast }                 from './shell/toast.js?v=20260518a';
import { apiUpload }             from './api.js?v=20260505e';

// ─── helpers ─────────────────────────────────────────────────────────────────

function LABEL(text) {
  return el('div', {
    style: 'font-size:11px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; color:var(--text-3); margin-bottom:4px;',
    text,
  });
}

function _card(children, extraStyle = '') {
  const c = el('div', {
    style: `background:var(--surface-1); border:1px solid var(--border-1); border-radius:var(--r-lg); padding:16px; display:flex; flex-direction:column; gap:10px; ${extraStyle}`,
  });
  (Array.isArray(children) ? children : [children]).forEach(ch => ch && c.appendChild(ch));
  return c;
}

// ─── init ────────────────────────────────────────────────────────────────────

export function init(panel) {
  let _songPath      = null;
  let _songDur       = 0;
  let _songAnalysis  = null;
  let _folderFiles   = [];
  let _folderPath    = '';
  let _pollTimer     = null;
  let _analyzeSeq    = 0;

  // ── Song upload ────────────────────────────────────────────────────────────

  const songHintText  = el('div', { style: 'font-size:13px; color:var(--text-3);', text: 'Drop your song here or click to browse' });
  const songHintSub   = el('div', { style: 'font-size:11px; color:var(--text-4); margin-top:4px;', text: 'mp3 · wav · flac · m4a · aac' });
  const songHintArea  = el('div', { style: 'display:flex; flex-direction:column; align-items:center; padding:20px 0; gap:2px;' }, [songHintText, songHintSub]);
  const songPreview   = el('audio', { style: 'display:none; width:100%; margin:8px 0;' });
  songPreview.controls = true;
  const songClearBtn  = el('button', { style: 'display:none; align-self:flex-end; background:none; border:none; color:var(--red); cursor:pointer; font-size:11px; padding:0;', text: 'remove song' });

  const songDrop = el('div', {
    style: 'border:1px dashed var(--border-2); border-radius:var(--r-md); cursor:pointer; display:flex; flex-direction:column; align-items:center; transition:border-color .12s, background .12s; background:var(--surface-2);',
  });
  songDrop.append(songHintArea, songPreview, songClearBtn);

  const songFileInput = el('input', { type: 'file', accept: 'audio/*,.mp3,.wav,.flac,.ogg,.m4a,.aac,.opus,.mpeg' });
  songFileInput.style.display = 'none';
  panel.appendChild(songFileInput);

  const analysisCard = el('div', { style: 'display:none; flex-direction:column; gap:6px; padding-top:8px; border-top:1px solid var(--border-2);' });

  songDrop.addEventListener('dragover', e => { e.preventDefault(); songDrop.style.borderColor = 'var(--accent)'; songDrop.style.background = 'var(--accent-bg)'; });
  songDrop.addEventListener('dragleave', () => { songDrop.style.borderColor = 'var(--border-2)'; songDrop.style.background = 'var(--surface-2)'; });
  songDrop.addEventListener('drop', e => {
    e.preventDefault(); songDrop.style.borderColor = 'var(--border-2)'; songDrop.style.background = 'var(--surface-2)';
    const f = Array.from(e.dataTransfer.files).find(f => f.type.startsWith('audio/') || /\.(mp3|wav|flac|ogg|m4a|aac|opus|mpeg|mpg)$/i.test(f.name));
    if (f) _uploadSong(f);
  });
  songDrop.addEventListener('click', e => {
    if (e.target === songPreview || e.target === songClearBtn || songPreview.contains(e.target)) return;
    songFileInput.click();
  });
  songFileInput.addEventListener('change', () => { if (songFileInput.files[0]) _uploadSong(songFileInput.files[0]); songFileInput.value = ''; });
  songClearBtn.addEventListener('click', e => { e.stopPropagation(); _clearSong(); });

  async function _uploadSong(file) {
    songHintText.textContent = 'Uploading...';
    try {
      const resp = await apiUpload('/api/song-video/upload-audio', [file]);
      const f = resp?.files?.[0];
      if (!f?.path) throw new Error('No path returned');
      songPreview.src = f.url;
      songPreview.style.display = 'block';
      songHintArea.style.display = 'none';
      songClearBtn.style.display = 'block';
      _songPath = f.path;
      _songDur  = f.duration || 0;
      _updateButtons();
      _updateSingleBtn();
      _analyzeAudio(f.path);
    } catch (err) {
      toast('Song upload failed: ' + err.message, 'error');
      songHintText.textContent = 'Drop your song here or click to browse';
    }
  }

  function _clearSong() {
    _analyzeSeq++;
    _songPath = null; _songDur = 0; _songAnalysis = null;
    songPreview.src = ''; songPreview.style.display = 'none';
    songHintArea.style.display = 'flex';
    songClearBtn.style.display = 'none';
    songDrop.style.borderColor = 'var(--border-2)';
    analysisCard.style.display = 'none';
    analysisCard.innerHTML = '';
    _updateButtons();
    _updateSingleBtn();
  }

  async function _analyzeAudio(path) {
    const seq = ++_analyzeSeq;
    analysisCard.innerHTML = '<div style="font-size:12px;color:var(--text-3);">Analyzing...</div>';
    analysisCard.style.display = 'flex';
    try {
      const a = await apiFetch('/api/song-video/analyze', { method: 'POST', body: JSON.stringify({ audio_path: path }) });
      if (seq !== _analyzeSeq) return;
      _songAnalysis = a;
      _songDur = a.duration || _songDur;
      _renderAnalysis(a);
      _updateButtons();
    } catch (e) {
      if (seq !== _analyzeSeq) return;
      analysisCard.innerHTML = '';
      analysisCard.style.display = 'none';
    }
  }

  function _renderAnalysis(a) {
    analysisCard.innerHTML = '';
    analysisCard.style.display = 'flex';
    const chips = [
      a.duration_display && { icon: '♪', text: a.duration_display },
      a.bpm              && { icon: '♩', text: `${a.bpm} BPM` },
      a.key              && { icon: '♭', text: `${a.key} ${a.mode || ''}`.trim() },
      a.mood             && { icon: '◈', text: a.mood },
    ].filter(Boolean);
    if (chips.length) {
      const row = el('div', { style: 'display:flex; flex-wrap:wrap; gap:6px;' });
      chips.forEach(({ icon, text }) => {
        row.appendChild(el('div', {
          style: 'font-size:11px; background:var(--surface-2); border:1px solid var(--border-2); border-radius:20px; padding:3px 10px; color:var(--text-2);',
          text: `${icon} ${text}`,
        }));
      });
      analysisCard.appendChild(row);
    }
  }

  // ── Model selector ─────────────────────────────────────────────────────────

  const modelSel = el('select', {
    style: 'width:100%; background:var(--surface-2); border:1px solid var(--border-2); border-radius:var(--r-md); padding:8px 12px; color:var(--text); font-size:13px; cursor:pointer; outline:none;',
  });
  apiFetch('/api/fun/models').then(data => {
    const models = data.models || {};
    const i2v = Object.entries(models).filter(([, m]) => m.i2v).sort(([a], [b]) => a.localeCompare(b));
    modelSel.innerHTML = '';
    for (const [name] of i2v) {
      const opt = el('option', { value: name, text: name });
      modelSel.appendChild(opt);
    }
    const best = i2v.find(([n]) => n === 'LTX-2 Dev19B Distilled') || i2v[0];
    if (best) modelSel.value = best[0];
  }).catch(() => {
    const opt = el('option', { value: 'LTX-2 Dev19B Distilled', text: 'LTX-2 Dev19B Distilled' });
    modelSel.appendChild(opt);
  });

  // ── Folder picker ──────────────────────────────────────────────────────────

  const folderNameEl  = el('div', { style: 'flex:1; font-size:13px; color:var(--text-2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; padding:4px 0;', text: 'No folder selected' });
  const browseFolderBtn = el('button', {
    text: 'Choose Folder',
    style: 'padding:8px 14px; border-radius:var(--r-md); border:1px solid var(--accent-border); background:var(--accent-bg); color:var(--accent); cursor:pointer; font-size:13px; font-weight:600; white-space:nowrap;',
  });
  const folderStatus = el('div', { style: 'font-size:12px; color:var(--text-3); min-height:16px;' });

  browseFolderBtn.onclick = async () => {
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

  async function _scanFolder(folder) {
    folderStatus.textContent = 'Scanning...';
    _folderFiles = [];
    _updateButtons();
    try {
      const r = await apiFetch('/api/zoom/scan-folder', { method: 'POST', body: JSON.stringify({ folder }) });
      const imgs = (r.files || []).filter(f => !f.is_video);
      _folderFiles = imgs;
      folderStatus.textContent = !imgs.length
        ? 'No images found'
        : `${imgs.length} image${imgs.length !== 1 ? 's' : ''} found`;
      _updateButtons();
    } catch (e) { folderStatus.textContent = 'Error: ' + e.message; }
  }

  // ── Options ────────────────────────────────────────────────────────────────

  function _toggle(label, checked = false) {
    const wrap  = el('label', { style: 'display:flex; align-items:center; gap:8px; cursor:pointer; font-size:13px; color:var(--text-2); user-select:none;' });
    const input = el('input', { type: 'checkbox' });
    input.checked = checked;
    input.style.accentColor = 'var(--accent)';
    wrap.append(input, label);
    return { wrap, input };
  }

  const { wrap: loopWrap, input: loopCheck }   = _toggle('Loop continuously (repeat folder)', false);
  const { wrap: fastWrap, input: fastCheck }   = _toggle('Fast mode  (360P · 8 steps · 50% coverage · ~4x faster, upscaled after)', true);
  const { wrap: satWrap,  input: satCheck }    = _toggle('Use 3060 satellite  (both GPUs generate simultaneously)', false);
  // Check if satellite is reachable and auto-enable the toggle
  fetch('/api/song-video/batch/status').then(() =>
    fetch('/api/satellite/status').then(r => r.json()).then(s => {
      if (s && s.connected && s.services && s.services.wangp && s.services.wangp.state === 'running') {
        satCheck.checked = true;
      }
    }).catch(() => {})
  ).catch(() => {});

  // ── Batch controls ─────────────────────────────────────────────────────────

  const batchStatus = el('div', { style: 'font-size:12px; color:var(--text-3); min-height:16px;' });
  const batchBtn = el('button', {
    text: 'Queue All',
    disabled: true,
    style: 'padding:12px; border-radius:var(--r-lg); border:none; cursor:not-allowed; font-size:15px; font-weight:700; background:var(--circus-red); color:var(--text); opacity:.45; width:100%;',
  });

  function _updateButtons() {
    const hasAll = _folderFiles.length > 0 && !!_songPath;
    batchBtn.disabled = !hasAll;
    batchBtn.style.opacity  = hasAll ? '1' : '.45';
    batchBtn.style.cursor   = hasAll ? 'pointer' : 'not-allowed';
    if (!_songPath) {
      batchBtn.textContent = 'Upload a song first';
    } else if (!_folderFiles.length) {
      batchBtn.textContent = 'Choose a folder of images first';
    } else {
      const loop = loopCheck.checked;
      batchBtn.textContent = loop ? `Start Loop  (${_folderFiles.length} images)` : `Queue All  ${_folderFiles.length} Images`;
    }
  }

  loopCheck.addEventListener('change', _updateButtons);
  fastCheck.addEventListener('change', _updateButtons);
  satCheck.addEventListener('change', _updateButtons);

  let _pollActive = false;

  function _startPoll() {
    if (_pollTimer) return;
    _pollTimer = setInterval(async () => {
      try {
        const r = await fetch('/api/song-video/batch/status');
        if (!r.ok) return;
        const s = await r.json();
        _applySnapshot(s);
        if (!s.active && s.status !== 'running') {
          clearInterval(_pollTimer); _pollTimer = null; _pollActive = false;
        }
      } catch {}
    }, 5000);
    _pollActive = true;
  }

  function _applySnapshot(s) {
    const running = s.active || s.status === 'running';
    if (running) {
      batchBtn.disabled = false;
      batchBtn.style.opacity = '1';
      batchBtn.style.cursor  = 'pointer';
      batchBtn.style.background = 'var(--red)';
      const cur  = s.current_image ? `  ${s.current_image}` : '';
      const lap  = s.lap > 1 ? `  (lap ${s.lap})` : '';
      batchBtn.textContent = `Stop  (${s.index}/${s.total}${lap}${cur})`;
      batchStatus.textContent = `Running ${s.index}/${s.total}${lap}${cur}  —  ${s.succeeded} done, ${s.failed} failed`;
      if (s.folder && !_folderPath) {
        _folderPath = s.folder;
        folderNameEl.textContent = s.folder.split(/[\\/]/).pop() || s.folder;
        folderStatus.textContent = `${s.total} images`;
        _folderFiles = s.images || Array(s.total).fill({});
        _startPoll();
      }
    } else if (s.status === 'done') {
      batchStatus.textContent = `Done  —  ${s.succeeded} videos generated, ${s.failed} failed`;
      batchBtn.style.background = 'var(--circus-red)';
      _updateButtons();
    } else if (s.status === 'stopped') {
      batchStatus.textContent = `Stopped at ${s.index}/${s.total}  —  ${s.succeeded} done`;
      batchBtn.style.background = 'var(--circus-red)';
      _updateButtons();
    } else if (s.status === 'error') {
      const last = s.errors?.length ? s.errors[s.errors.length - 1].msg : '';
      batchStatus.textContent = `Error: ${last}`;
      _updateButtons();
    }
  }

  batchBtn.onclick = async () => {
    // Stop if running
    if (_pollActive || batchBtn.textContent.startsWith('Stop')) {
      try {
        await apiFetch('/api/song-video/batch/stop', { method: 'POST', body: '{}' });
        batchStatus.textContent = 'Stopping after current video...';
        batchBtn.textContent = 'Stopping...';
        batchBtn.disabled = true;
      } catch (e) { toast('Stop failed: ' + e.message, 'error'); }
      return;
    }

    if (!_songPath) { toast('Upload a song first', 'error'); return; }
    if (!_folderFiles.length) { toast('Choose a folder first', 'error'); return; }

    batchBtn.disabled = true;
    batchStatus.textContent = 'Analyzing song and starting batch...';

    try {
      const fast = fastCheck.checked;
      const body = {
        audio_path:      _songPath,
        folder:          _folderPath,
        images:          _folderFiles.map(f => ({ path: f.path, name: f.name })),
        repeat:          loopCheck.checked,
        use_satellite:   satCheck.checked,
        model:           modelSel.value,
        clip_duration:   fast ? 5    : 8,
        num_clips:       fast ? null : undefined,
        steps:           fast ? 8    : 20,
        guidance:        fast ? 2.5  : 3.5,
        coverage_ratio:  fast ? 0.5  : 1.0,
        output_width:    fast ? 640  : undefined,
        output_height:   fast ? 360  : undefined,
      };
      const s = await apiFetch('/api/song-video/batch/start', { method: 'POST', body: JSON.stringify(body) });
      _applySnapshot(s);
      _startPoll();
      toast(`Batch started: ${s.total} images`, 'success');
    } catch (e) {
      batchBtn.disabled = false;
      _updateButtons();
      toast('Batch start failed: ' + e.message, 'error');
    }
  };

  // ── Single-image generation ────────────────────────────────────────────────

  const singleImagePath = { value: null };
  const singleDrop = el('div', {
    style: 'border:1px dashed var(--border-2); border-radius:var(--r-md); padding:16px; text-align:center; cursor:pointer; font-size:13px; color:var(--text-3); background:var(--surface-2); transition:border-color .12s, background .12s;',
    text: 'Drop image here (optional — locks visual style)',
  });
  const singleImgPreview = el('img', { style: 'display:none; max-height:80px; border-radius:4px; margin-top:6px;' });
  const singleImgInput   = el('input', { type: 'file', accept: 'image/*' });
  singleImgInput.style.display = 'none';
  panel.appendChild(singleImgInput);

  singleDrop.addEventListener('dragover', e => { e.preventDefault(); singleDrop.style.borderColor = 'var(--accent)'; singleDrop.style.background = 'var(--accent-bg)'; });
  singleDrop.addEventListener('dragleave', () => { singleDrop.style.borderColor = 'var(--border-2)'; singleDrop.style.background = 'var(--surface-2)'; });
  singleDrop.addEventListener('drop', e => {
    e.preventDefault(); singleDrop.style.borderColor = 'var(--border-2)'; singleDrop.style.background = 'var(--surface-2)';
    const f = Array.from(e.dataTransfer.files).find(f => f.type.startsWith('image/'));
    if (f) _uploadSingleImage(f);
  });
  singleDrop.addEventListener('click', () => singleImgInput.click());
  singleImgInput.addEventListener('change', () => { if (singleImgInput.files[0]) _uploadSingleImage(singleImgInput.files[0]); singleImgInput.value = ''; });

  async function _uploadSingleImage(file) {
    singleDrop.textContent = 'Uploading...';
    try {
      const resp = await apiUpload('/api/song-video/upload-image', [file]);
      const f = resp?.files?.[0];
      if (!f?.path) throw new Error('No path');
      singleImagePath.value = f.path;
      singleImgPreview.src = f.url;
      singleImgPreview.style.display = 'block';
      singleDrop.textContent = f.name || 'Image ready';
    } catch (e) { toast('Image upload failed: ' + e.message, 'error'); singleDrop.textContent = 'Drop image here (optional)'; }
  }

  const ideaInput = el('textarea', {
    placeholder: 'Describe the vibe / visual idea (optional)',
    style: 'width:100%; box-sizing:border-box; background:var(--surface-2); border:1px solid var(--border-2); border-radius:var(--r-md); color:var(--text); padding:10px; font-family:inherit; font-size:13px; resize:vertical; min-height:56px; outline:none;',
  });

  const singleBtn = el('button', {
    text: 'Generate One Video',
    disabled: true,
    style: 'padding:11px; border-radius:var(--r-lg); border:none; cursor:not-allowed; font-size:14px; font-weight:700; background:var(--accent); color:#000; opacity:.45; width:100%;',
  });
  const singleStatus = el('div', { style: 'font-size:12px; color:var(--text-3); min-height:16px;' });
  const singleProgress = el('progress', { style: 'display:none; width:100%; height:4px;' });
  singleProgress.max = 100; singleProgress.value = 0;
  const singleResult = el('video', { style: 'display:none; width:100%; border-radius:var(--r-md); margin-top:6px; max-height:260px;' });
  singleResult.controls = true;

  function _updateSingleBtn() {
    const ready = !!_songPath;
    singleBtn.disabled = !ready;
    singleBtn.style.opacity = ready ? '1' : '.45';
    singleBtn.style.cursor  = ready ? 'pointer' : 'not-allowed';
  }

  let _singleJobId = null;
  let _singlePoll  = null;

  singleBtn.onclick = async () => {
    if (!_songPath) { toast('Upload a song first', 'error'); return; }
    singleBtn.disabled = true;
    singleStatus.textContent = 'Submitting...';
    singleProgress.style.display = 'block'; singleProgress.value = 5;
    singleResult.style.display = 'none';

    try {
      const fast = fastCheck.checked;
      const body = {
        audio_path:    _songPath,
        photo_path:    singleImagePath.value || '',
        video_prompt:  ideaInput.value.trim(),
        audio_analysis: _songAnalysis || undefined,
        model:          modelSel.value,
        clip_duration:  fast ? 5    : 8,
        steps:          fast ? 8    : 20,
        guidance:       fast ? 2.5  : 3.5,
        coverage_ratio: fast ? 0.5  : 1.0,
        output_width:   fast ? 640  : undefined,
        output_height:  fast ? 360  : undefined,
      };
      const resp = await apiFetch('/api/song-video/generate', { method: 'POST', body: JSON.stringify(body) });
      _singleJobId = resp.job_id;
      singleStatus.textContent = `Generating ${resp.n_clips} clips...`;
      singleProgress.value = 10;
      _singlePoll = setInterval(_pollSingle, 2000);
    } catch (e) {
      singleStatus.textContent = 'Error: ' + e.message;
      singleProgress.style.display = 'none';
      singleBtn.disabled = false;
      _updateSingleBtn();
    }
  };

  async function _pollSingle() {
    if (!_singleJobId) return;
    try {
      const j = await apiFetch(`/api/jobs/${_singleJobId}`);
      singleProgress.value = j.progress || 10;
      singleStatus.textContent = j.message || 'Generating...';
      if (j.status === 'done') {
        clearInterval(_singlePoll); _singlePoll = null;
        singleProgress.style.display = 'none';
        singleStatus.textContent = 'Done!';
        if (j.output) {
          const idx = j.output.replace(/\\/g, '/').toLowerCase().indexOf('/output/');
          singleResult.src = idx !== -1 ? j.output.replace(/\\/g, '/').slice(idx) : '';
          singleResult.style.display = 'block';
        }
        singleBtn.disabled = false; _updateSingleBtn();
        document.dispatchEvent(new Event('session-updated'));
      } else if (j.status === 'error' || j.status === 'stopped') {
        clearInterval(_singlePoll); _singlePoll = null;
        singleProgress.style.display = 'none';
        singleStatus.textContent = 'Failed: ' + (j.error || j.status);
        singleBtn.disabled = false; _updateSingleBtn();
      }
    } catch {}
  }

  // ── On tab open: check for already-running batch (do NOT auto-start) ─────────
  // Only connect the poll to a batch that is ALREADY actively running on the
  // server. Never silently start or resume a saved batch without user input.

  (async () => {
    try {
      const r = await fetch('/api/song-video/batch/status');
      if (!r.ok) return;
      const s = await r.json();
      if (s.active && s.status === 'running') {
        _applySnapshot(s);
        _startPoll();
      }
    } catch {}
  })();

  // ── Assemble layout ────────────────────────────────────────────────────────

  panel.style.cssText = 'display:flex; flex-direction:column; gap:14px; padding:16px; overflow-y:auto; height:100%;';

  panel.append(
    // Title
    el('div', { style: 'font-size:18px; font-weight:700; color:var(--gold); letter-spacing:-.01em;', text: 'Music Video' }),

    // Song upload
    _card([
      LABEL('Song'),
      songDrop,
      analysisCard,
    ]),

    // Batch: folder of images
    _card([
      LABEL('Folder Batch'),
      el('div', { style: 'display:flex; gap:8px; align-items:center;' }, [folderNameEl, browseFolderBtn]),
      folderStatus,
      loopWrap,
      fastWrap,
      satWrap,
      batchStatus,
      batchBtn,
    ]),

    // Single image
    _card([
      LABEL('Single Image'),
      el('div', { style: 'display:flex; gap:10px; align-items:flex-start;' }, [
        el('div', { style: 'flex:0 0 auto;' }, [singleDrop, singleImgPreview]),
        el('div', { style: 'flex:1; display:flex; flex-direction:column; gap:8px;' }, [ideaInput]),
      ]),
      singleProgress,
      singleStatus,
      singleResult,
      singleBtn,
    ]),

    // Model
    _card([LABEL('Video Model'), modelSel]),
  );

  // Wire song-upload state to single-image button too
  const _origUpdateButtons = _updateButtons;
  function _updateAll() { _origUpdateButtons(); _updateSingleBtn(); }
  panel.querySelectorAll('input[type=checkbox]').forEach(cb => cb.addEventListener('change', _updateAll));
}

export function receiveHandoff(data) {
  // future: accept handoff from Create Videos or Express
}
