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
    if (a.sections && a.sections.length) {
      const sectInfo = el('div', {
        style: 'font-size:11px; color:var(--text-3); margin-top:4px;',
        text: a.sections.length + ' sections detected -- clip boundaries will snap to section changes',
      });
      analysisCard.appendChild(sectInfo);
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

  const { wrap: loopWrap, input: loopCheck }    = _toggle('Loop continuously (repeat folder)', false);
  const { wrap: lipSyncWrap, input: lipSyncCheck } = _toggle('Lip Sync  (audio drives subject mouth/face motion)', true);

  // Clip duration slider
  function _numRow(labelText, min, max, step, def, unit) {
    const lbl   = el('div', { style: 'font-size:12px; color:var(--text-3);', text: labelText });
    const input = el('input', { type: 'range', min: String(min), max: String(max), step: String(step), value: String(def) });
    input.style.cssText = 'flex:1; accent-color:var(--accent);';
    const val   = el('span', { style: 'font-size:11px; color:var(--text-2); min-width:32px; text-align:right;', text: def + unit });
    input.addEventListener('input', () => { val.textContent = input.value + unit; });
    const row   = el('div', { style: 'display:flex; align-items:center; gap:8px;' }, [input, val]);
    const wrap  = el('div', { style: 'display:flex; flex-direction:column; gap:3px; padding:4px 0;' }, [lbl, row]);
    return { wrap, input, val };
  }

  const { wrap: clipDurWrap, input: clipDurSlider } = _numRow('Clip length', 4, 15, 1, 6, 's');

  // Padding: seconds of silent video before song starts / after song ends
  const { wrap: padBeforeWrap, input: padBeforeSlider } = _numRow('Video before song starts', 0, 10, 1, 0, 's');
  const { wrap: padAfterWrap,  input: padAfterSlider  } = _numRow('Video after song ends',    0, 10, 1, 0, 's');
  // Satellite disabled -- unstable, kept for future use
  const satCheck = { checked: false };
  const satWrap  = null;

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
    batchBtn.style.display  = hasAll ? '' : 'none';
    batchBtn.style.cursor   = hasAll ? 'pointer' : 'not-allowed';
    if (hasAll) {
      const loop = loopCheck.checked;
      batchBtn.textContent = loop ? `Start Loop  (${_folderFiles.length} images)` : `Queue All  ${_folderFiles.length} Images`;
    }
  }

  loopCheck.addEventListener('change', _updateButtons);
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
      const clips = (s.clips_done != null && s.clips_total)
        ? `  [clip ${s.clips_done}/${s.clips_total}]` : '';
      batchBtn.textContent = `Stop  (${s.index}/${s.total}${lap}${cur})`;
      batchStatus.textContent = `Running ${s.index}/${s.total}${lap}${cur}${clips}  —  ${s.succeeded} done, ${s.failed} failed`;
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
      const body = {
        audio_path:    _songPath,
        folder:        _folderPath,
        images:        _folderFiles.map(f => ({ path: f.path, name: f.name })),
        repeat:        loopCheck.checked,
        lip_sync:      lipSyncCheck.checked,
        use_satellite: satCheck.checked,
        model:         modelSel.value,
        clip_duration: parseInt(clipDurSlider.value),
        steps:         8,
        guidance:      3.0,
        pad_before:    parseInt(padBeforeSlider.value),
        pad_after:     parseInt(padAfterSlider.value),
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
      const body = {
        audio_path:     _songPath,
        photo_path:     singleImagePath.value || '',
        video_prompt:   ideaInput.value.trim(),
        audio_analysis: _songAnalysis || undefined,
        lip_sync:       lipSyncCheck.checked,
        model:          modelSel.value,
        clip_duration:  parseInt(clipDurSlider.value),
        steps:          8,
        guidance:       3.0,
        pad_before:     parseInt(padBeforeSlider.value),
        pad_after:      parseInt(padAfterSlider.value),
      };
      const resp = await apiFetch('/api/song-video/generate', { method: 'POST', body: JSON.stringify(body) });
      // Clear any previous poll before tracking the new job
      if (_singlePoll) { clearInterval(_singlePoll); _singlePoll = null; }
      _singleJobId = resp.job_id;
      singleStatus.textContent = `Generating ${resp.n_clips} clips... (queued)`;
      singleProgress.value = 10;
      _singlePoll = setInterval(_pollSingle, 2000);
      // Re-enable immediately so the user can queue another video while this one runs
      singleBtn.disabled = false; _updateSingleBtn();
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
          if (_songPath) _showBeatSync(j.output, _songPath);
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

  // ── Inline Beat Sync ────────────────────────────────────────────────────────

  const syncSection = el('div', { style: 'display:none; flex-direction:column; gap:10px;' });
  const syncTitle   = el('div', { style: 'font-size:13px; font-weight:700; color:var(--gold);', text: 'Beat Sync' });
  const syncHint    = el('div', { style: 'font-size:11px; color:var(--text-3);', text: 'Orange = beats  |  Blue = energy peaks  |  White = clip cuts  |  Red diamonds = motion peaks (drag to align)  |  Yellow = manually set' });
  const syncCanvasAudio = el('canvas', { style: 'display:block; width:100%; border-radius:4px; cursor:crosshair; background:#0a0202;' });
  syncCanvasAudio.height = 70;
  const syncCanvasVideo = el('canvas', { style: 'display:block; width:100%; border-radius:4px; cursor:crosshair; background:#050101;' });
  syncCanvasVideo.height = 50;
  const syncInfo = el('div', { style: 'font-size:11px; color:var(--text-3);' });
  const syncAutoBtn   = el('button', { text: 'Auto-Align', style: 'padding:7px 14px; border-radius:var(--r-md); border:1px solid var(--accent-border); background:var(--accent-bg); color:var(--accent); font-size:12px; font-weight:600; cursor:pointer;' });
  const syncResetBtn  = el('button', { text: 'Reset',      style: 'padding:7px 12px; border-radius:var(--r-md); border:1px solid var(--border-2); background:none; color:var(--text-3); font-size:12px; cursor:pointer;' });
  const syncExportBtn = el('button', { text: 'Export Synced Video', style: 'padding:7px 16px; border-radius:var(--r-md); border:none; background:var(--circus-red); color:#fff; font-size:12px; font-weight:700; cursor:pointer;' });
  const syncStatus    = el('div', { style: 'font-size:11px; color:var(--text-3); min-height:14px;' });
  const syncProgress  = el('progress', { style: 'display:none; width:100%; height:3px;' });
  syncProgress.max = 100;

  syncSection.append(
    syncTitle, syncHint,
    syncCanvasAudio, syncCanvasVideo, syncInfo,
    el('div', { style: 'display:flex; gap:8px; flex-wrap:wrap;' }, [syncAutoBtn, syncResetBtn, syncExportBtn]),
    syncStatus, syncProgress,
  );

  let _syncVideoPath = '', _syncAudioPath = '', _syncData = null, _syncRemap = [], _syncDragIdx = -1, _syncAudioBuf = null, _syncAudioCtx = null, _syncPlayPos = 0;
  const MR = 7;

  function _sToX(c, t, dur) { return Math.round(t / dur * c.width); }
  function _xToS(c, x, dur) { return Math.max(0, Math.min(dur, x / c.width * dur)); }

  function _drawSync() {
    if (!_syncData) return;
    const a = _syncData.audio || {}, v = _syncData.video || {};
    const dur = a.duration || v.duration || 1;
    const WA = syncCanvasAudio.clientWidth || 600;
    if (syncCanvasAudio.width !== WA) { syncCanvasAudio.width = WA; syncCanvasVideo.width = WA; }

    // Audio canvas
    const ca = syncCanvasAudio.getContext('2d');
    ca.clearRect(0, 0, WA, 70);
    if (_syncAudioBuf) {
      const d = _syncAudioBuf.getChannelData(0), step = Math.ceil(d.length / WA), cy = 35;
      ca.strokeStyle = '#5a2020'; ca.lineWidth = 1;
      for (let x = 0; x < WA; x++) {
        let mn = 1, mx = -1;
        for (let i = x*step; i < (x+1)*step && i < d.length; i++) { if(d[i]<mn)mn=d[i]; if(d[i]>mx)mx=d[i]; }
        ca.beginPath(); ca.moveTo(x, cy+mn*cy*.85); ca.lineTo(x, cy+mx*cy*.85); ca.stroke();
      }
    }
    ca.strokeStyle = 'rgba(232,124,42,.25)'; ca.lineWidth = 1;
    for (const t of (a.beat_times||[])) { const x=_sToX(syncCanvasAudio,t,dur); ca.beginPath(); ca.moveTo(x,0); ca.lineTo(x,70); ca.stroke(); }
    ca.strokeStyle = '#e87c2a'; ca.lineWidth = 2;
    for (const t of (a.energy_peaks||[])) { const x=_sToX(syncCanvasAudio,t,dur); ca.beginPath(); ca.moveTo(x,0); ca.lineTo(x,70); ca.stroke(); }
    const ph = _sToX(syncCanvasAudio, _syncPlayPos, dur);
    ca.strokeStyle = '#fff'; ca.lineWidth = 1.5; ca.beginPath(); ca.moveTo(ph,0); ca.lineTo(ph,70); ca.stroke();

    // Video canvas
    const cv = syncCanvasVideo.getContext('2d');
    cv.clearRect(0, 0, WA, 50);
    cv.fillStyle = '#050101'; cv.fillRect(0,0,WA,50);
    cv.strokeStyle = 'rgba(255,255,255,.35)'; cv.lineWidth = 1;
    for (const t of (v.clip_boundaries||[])) { if(t<.1)continue; const x=_sToX(syncCanvasVideo,t,dur); cv.beginPath(); cv.moveTo(x,0); cv.lineTo(x,50); cv.stroke(); }
    const peaks = v.motion_peaks||[];
    for (let i=0; i<peaks.length; i++) {
      const rp = _syncRemap.find(r=>r._i===i);
      const t = rp ? rp.target_t : peaks[i];
      const ot = peaks[i], x = _sToX(syncCanvasVideo,t,dur), ox = _sToX(syncCanvasVideo,ot,dur);
      if (rp && Math.abs(x-ox)>2) {
        cv.strokeStyle='rgba(232,74,74,.35)'; cv.lineWidth=1; cv.setLineDash([3,3]);
        cv.beginPath(); cv.moveTo(ox,25); cv.lineTo(x,25); cv.stroke(); cv.setLineDash([]);
      }
      cv.fillStyle = rp ? '#ffaa00' : '#e84a4a'; cv.strokeStyle='#fff'; cv.lineWidth=1.5;
      cv.beginPath(); cv.moveTo(x,25-MR); cv.lineTo(x+MR,25); cv.lineTo(x,25+MR); cv.lineTo(x-MR,25); cv.closePath(); cv.fill(); cv.stroke();
    }
    const pv = _sToX(syncCanvasVideo, _syncPlayPos, dur);
    cv.strokeStyle='#fff'; cv.lineWidth=1.5; cv.beginPath(); cv.moveTo(pv,0); cv.lineTo(pv,50); cv.stroke();
  }

  function _peakAtX(x) {
    if (!_syncData) return -1;
    const dur = (_syncData.audio?.duration || _syncData.video?.duration || 1);
    const peaks = _syncData.video?.motion_peaks || [];
    for (let i=peaks.length-1; i>=0; i--) {
      const rp = _syncRemap.find(r=>r._i===i);
      const t = rp ? rp.target_t : peaks[i];
      if (Math.abs(_sToX(syncCanvasVideo,t,dur)-x) <= MR+3) return i;
    }
    return -1;
  }

  syncCanvasVideo.addEventListener('mousedown', e => {
    const r = syncCanvasVideo.getBoundingClientRect();
    const x = (e.clientX-r.left)*(syncCanvasVideo.width/r.width);
    _syncDragIdx = _peakAtX(x);
    if (_syncDragIdx>=0) e.preventDefault();
  });
  syncCanvasVideo.addEventListener('mousemove', e => {
    const r = syncCanvasVideo.getBoundingClientRect();
    const x = (e.clientX-r.left)*(syncCanvasVideo.width/r.width);
    syncCanvasVideo.style.cursor = _peakAtX(x)>=0 ? 'ew-resize' : 'crosshair';
    if (_syncDragIdx<0) return;
    const dur = (_syncData?.audio?.duration || _syncData?.video?.duration || 1);
    const newT = _xToS(syncCanvasVideo, x, dur);
    const origT = (_syncData?.video?.motion_peaks||[])[_syncDragIdx];
    const ei = _syncRemap.findIndex(r=>r._i===_syncDragIdx);
    if (ei>=0) _syncRemap[ei].target_t = newT;
    else _syncRemap.push({ _i:_syncDragIdx, video_t:origT, target_t:newT });
    _drawSync();
  });
  window.addEventListener('mouseup', () => { _syncDragIdx = -1; });
  syncCanvasAudio.addEventListener('click', e => {
    const r = syncCanvasAudio.getBoundingClientRect();
    _syncPlayPos = _xToS(syncCanvasAudio, (e.clientX-r.left)*(syncCanvasAudio.width/r.width), _syncData?.audio?.duration || _syncData?.video?.duration || 1);
    _drawSync();
  });

  syncAutoBtn.addEventListener('click', () => {
    if (!_syncData) return;
    const a = _syncData.audio||{}, v = _syncData.video||{};
    const beats = a.beat_times||[], energy = a.energy_peaks||[], peaks = v.motion_peaks||[];
    const dur = a.duration||v.duration||1;
    const cands = [...new Set([...energy, ...beats.filter((_,i)=>i%4===0)])].sort((x,y)=>x-y);
    const used = new Set(); _syncRemap = [];
    for (let i=0; i<peaks.length; i++) {
      const ot=peaks[i]; let best=null, bd=Infinity;
      for (const c of cands) { const d=Math.abs(c-ot); if(d<bd&&d<=1.5&&!used.has(c)&&c>0&&c<dur){bd=d;best=c;} }
      if (best!==null) { _syncRemap.push({_i:i,video_t:ot,target_t:best}); used.add(best); }
    }
    syncStatus.textContent = `Auto-aligned ${_syncRemap.length} of ${peaks.length} motion peaks`;
    _drawSync();
  });
  syncResetBtn.addEventListener('click', () => { _syncRemap=[]; _drawSync(); syncStatus.textContent=''; });

  syncExportBtn.addEventListener('click', async () => {
    syncExportBtn.disabled=true; syncStatus.textContent='Submitting...';
    syncProgress.style.display='block'; syncProgress.value=5;
    const remap = _syncRemap.length>0 ? _syncRemap.map(({video_t,target_t})=>({video_t,target_t})) : null;
    try {
      const r = await apiFetch('/api/sync/retime',{method:'POST',body:JSON.stringify({video_path:_syncVideoPath,audio_path:_syncAudioPath,remap_points:remap})});
      if (r.error) throw new Error(r.error);
      const poll = setInterval(async () => {
        const j = await apiFetch(`/api/jobs/${r.job_id}`);
        syncProgress.value = j.progress||5;
        syncStatus.textContent = j.message||'Retiming...';
        if (j.status==='done') {
          clearInterval(poll); syncProgress.style.display='none';
          syncStatus.textContent = 'Synced video saved: ' + (j.output||'');
          syncExportBtn.disabled=false;
          document.dispatchEvent(new Event('session-updated'));
          toast('Beat-synced video ready', 'success');
        } else if (j.status==='error'||j.status==='stopped') {
          clearInterval(poll); syncProgress.style.display='none';
          syncStatus.textContent='Export failed: '+(j.error||j.status);
          syncExportBtn.disabled=false;
        }
      }, 1500);
    } catch(e) { syncProgress.style.display='none'; syncStatus.textContent='Error: '+e.message; syncExportBtn.disabled=false; }
  });

  async function _showBeatSync(videoPath, audioPath) {
    _syncVideoPath = videoPath; _syncAudioPath = audioPath;
    _syncRemap = []; _syncData = null; _syncPlayPos = 0;
    syncSection.style.display = 'flex';
    const ph = panel.querySelector('#beat-sync-placeholder');
    if (ph) ph.style.display = 'none';
    syncStatus.textContent = 'Analyzing...';
    try {
      _syncData = await apiFetch('/api/sync/analyze',{method:'POST',body:JSON.stringify({video_path:videoPath,audio_path:audioPath})});
      const a=_syncData.audio||{}, v=_syncData.video||{};
      syncInfo.textContent = `BPM: ${a.bpm?Math.round(a.bpm):'--'}  |  Beats: ${a.beat_times?.length||0}  |  Energy peaks: ${a.energy_peaks?.length||0}  |  Motion peaks: ${v.motion_peaks?.length||0}`;
      syncStatus.textContent = 'Ready. Hit Auto-Align or drag red markers to sync.';
      // Load waveform
      try {
        const resp = await fetch('/api/sync/serve?path='+encodeURIComponent(audioPath));
        if (resp.ok) {
          if (!_syncAudioCtx) _syncAudioCtx = new (window.AudioContext||window.webkitAudioContext)();
          _syncAudioBuf = await _syncAudioCtx.decodeAudioData(await resp.arrayBuffer());
        }
      } catch(_) {}
      _drawSync();
    } catch(e) { syncStatus.textContent = 'Analysis failed: '+e.message; }
  }

  new ResizeObserver(()=>{ if(_syncData) _drawSync(); }).observe(syncSection);

  // ── Sync Existing MP4 ────────────────────────────────────────────────────────
  // Standalone beat sync for MP4 files that already have audio baked in.
  // Drops audio from the MP4, shows waveform, lets user align, retimes video only.

  function _buildExistingSync() {
    let _exVidPath = '', _exAudioPath = '', _exData = null, _exRemap = [], _exDragIdx = -1, _exAudioBuf = null, _exAudioCtx = null;

    // Drop zone
    const dropHint = el('div', { style: 'font-size:13px; color:var(--text-3);', text: 'Drop MP4 here, or click to browse' });
    const dropSub  = el('div', { style: 'font-size:11px; color:var(--text-4); margin-top:3px;', text: 'mp4 · mov · any video with audio' });
    const dropZone = el('div', {
      style: 'border:1px dashed var(--border-2); border-radius:var(--r-md); padding:18px; text-align:center; cursor:pointer; background:var(--surface-2); transition:border-color .12s, background .12s;',
    }, [dropHint, dropSub]);
    const fileInput = el('input', { type: 'file', accept: 'video/*,.mp4,.mov,.mkv,.webm' });
    fileInput.style.display = 'none';

    dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.style.borderColor = 'var(--accent)'; dropZone.style.background = 'var(--accent-bg)'; });
    dropZone.addEventListener('dragleave', () => { dropZone.style.borderColor = 'var(--border-2)'; dropZone.style.background = 'var(--surface-2)'; });
    dropZone.addEventListener('drop', e => {
      e.preventDefault(); dropZone.style.borderColor = 'var(--border-2)'; dropZone.style.background = 'var(--surface-2)';
      const f = Array.from(e.dataTransfer.files).find(f => f.type.startsWith('video/') || /\.(mp4|mov|mkv|webm)$/i.test(f.name));
      if (f) _exUploadAndAnalyze(f);
    });
    dropZone.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => { if (fileInput.files[0]) _exUploadAndAnalyze(fileInput.files[0]); fileInput.value = ''; });

    const exStatus   = el('div', { style: 'font-size:12px; color:var(--text-3); min-height:14px;' });
    const exProgress = el('progress', { style: 'display:none; width:100%; height:3px;' });
    exProgress.max = 100;

    const exHint = el('div', { style: 'font-size:11px; color:var(--text-3);', text: 'Orange = beats  |  Blue = energy peaks  |  White = clip cuts  |  Red diamonds = motion peaks (drag to align)' });
    const exCanvasA = el('canvas', { style: 'display:none; width:100%; border-radius:4px; cursor:crosshair; background:#0a0202;' });
    exCanvasA.height = 70;
    const exCanvasV = el('canvas', { style: 'display:none; width:100%; border-radius:4px; cursor:crosshair; background:#050101;' });
    exCanvasV.height = 50;
    const exInfo = el('div', { style: 'font-size:11px; color:var(--text-3);' });

    const exAutoBtn   = el('button', { text: 'Auto-Align', style: 'padding:7px 14px; border-radius:var(--r-md); border:1px solid var(--accent-border); background:var(--accent-bg); color:var(--accent); font-size:12px; font-weight:600; cursor:pointer; display:none;' });
    const exResetBtn  = el('button', { text: 'Reset',      style: 'padding:7px 12px; border-radius:var(--r-md); border:1px solid var(--border-2); background:none; color:var(--text-3); font-size:12px; cursor:pointer; display:none;' });
    const exExportBtn = el('button', { text: 'Export Synced MP4', style: 'padding:7px 16px; border-radius:var(--r-md); border:none; background:var(--circus-red); color:#fff; font-size:12px; font-weight:700; cursor:pointer; display:none;' });
    const exExStatus  = el('div', { style: 'font-size:12px; color:var(--text-3); min-height:14px;' });
    const exExProg    = el('progress', { style: 'display:none; width:100%; height:3px;' });
    exExProg.max = 100;

    // Reuse the same draw logic as the post-gen sync but with ex* variables
    function _exSToX(c, t, dur) { return Math.round(t / dur * c.width); }
    function _exXToS(c, x, dur) { return Math.max(0, Math.min(dur, x / c.width * dur)); }

    function _exDraw() {
      if (!_exData) return;
      const a = _exData.audio || {}, v = _exData.video || {};
      const dur = a.duration || v.duration || 1;
      const W = exCanvasA.clientWidth || 700;
      if (exCanvasA.width !== W) { exCanvasA.width = W; exCanvasV.width = W; }

      const ca = exCanvasA.getContext('2d');
      ca.clearRect(0, 0, W, 70);
      if (_exAudioBuf) {
        const d = _exAudioBuf.getChannelData(0), step = Math.ceil(d.length / W), cy = 35;
        ca.strokeStyle = '#5a2020'; ca.lineWidth = 1;
        for (let x = 0; x < W; x++) {
          let mn = 1, mx = -1;
          for (let i = x*step; i < (x+1)*step && i < d.length; i++) { if(d[i]<mn)mn=d[i]; if(d[i]>mx)mx=d[i]; }
          ca.beginPath(); ca.moveTo(x, cy+mn*cy*.85); ca.lineTo(x, cy+mx*cy*.85); ca.stroke();
        }
      }
      ca.strokeStyle = 'rgba(232,124,42,.25)'; ca.lineWidth = 1;
      for (const t of (a.beat_times||[])) { const x=_exSToX(exCanvasA,t,dur); ca.beginPath(); ca.moveTo(x,0); ca.lineTo(x,70); ca.stroke(); }
      ca.strokeStyle = '#e87c2a'; ca.lineWidth = 2;
      for (const t of (a.energy_peaks||[])) { const x=_exSToX(exCanvasA,t,dur); ca.beginPath(); ca.moveTo(x,0); ca.lineTo(x,70); ca.stroke(); }

      const cv = exCanvasV.getContext('2d');
      cv.clearRect(0, 0, W, 50); cv.fillStyle = '#050101'; cv.fillRect(0,0,W,50);
      cv.strokeStyle = 'rgba(255,255,255,.35)'; cv.lineWidth = 1;
      for (const t of (v.clip_boundaries||[])) { if(t<.1)continue; const x=_exSToX(exCanvasV,t,dur); cv.beginPath(); cv.moveTo(x,0); cv.lineTo(x,50); cv.stroke(); }
      const peaks = v.motion_peaks||[];
      for (let i=0; i<peaks.length; i++) {
        const rp = _exRemap.find(r=>r._i===i);
        const t = rp ? rp.target_t : peaks[i], ot = peaks[i];
        const x = _exSToX(exCanvasV,t,dur), ox = _exSToX(exCanvasV,ot,dur);
        if (rp && Math.abs(x-ox)>2) {
          cv.strokeStyle='rgba(232,74,74,.35)'; cv.lineWidth=1; cv.setLineDash([3,3]);
          cv.beginPath(); cv.moveTo(ox,25); cv.lineTo(x,25); cv.stroke(); cv.setLineDash([]);
        }
        cv.fillStyle = rp ? '#ffaa00' : '#e84a4a'; cv.strokeStyle='#fff'; cv.lineWidth=1.5;
        cv.beginPath(); cv.moveTo(x,25-7); cv.lineTo(x+7,25); cv.lineTo(x,25+7); cv.lineTo(x-7,25); cv.closePath(); cv.fill(); cv.stroke();
      }
    }

    function _exPeakAt(x) {
      if (!_exData) return -1;
      const dur = (_exData.audio?.duration || _exData.video?.duration || 1);
      const peaks = _exData.video?.motion_peaks || [];
      for (let i=peaks.length-1; i>=0; i--) {
        const rp = _exRemap.find(r=>r._i===i), t = rp ? rp.target_t : peaks[i];
        if (Math.abs(_exSToX(exCanvasV,t,dur)-x) <= 10) return i;
      }
      return -1;
    }
    exCanvasV.addEventListener('mousedown', e => {
      const r = exCanvasV.getBoundingClientRect(), x = (e.clientX-r.left)*(exCanvasV.width/r.width);
      _exDragIdx = _exPeakAt(x); if (_exDragIdx>=0) e.preventDefault();
    });
    exCanvasV.addEventListener('mousemove', e => {
      const r = exCanvasV.getBoundingClientRect(), x = (e.clientX-r.left)*(exCanvasV.width/r.width);
      exCanvasV.style.cursor = _exPeakAt(x)>=0 ? 'ew-resize' : 'crosshair';
      if (_exDragIdx<0) return;
      const dur = (_exData?.audio?.duration||_exData?.video?.duration||1), newT = _exXToS(exCanvasV,x,dur);
      const origT = (_exData?.video?.motion_peaks||[])[_exDragIdx];
      const ei = _exRemap.findIndex(r=>r._i===_exDragIdx);
      if (ei>=0) _exRemap[ei].target_t=newT; else _exRemap.push({_i:_exDragIdx,video_t:origT,target_t:newT});
      _exDraw();
    });
    window.addEventListener('mouseup', () => { _exDragIdx = -1; });

    exAutoBtn.addEventListener('click', () => {
      if (!_exData) return;
      const a = _exData.audio||{}, v = _exData.video||{};
      const beats=a.beat_times||[], energy=a.energy_peaks||[], peaks=v.motion_peaks||[];
      const dur=a.duration||v.duration||1;
      const cands=[...new Set([...energy,...beats.filter((_,i)=>i%4===0)])].sort((x,y)=>x-y);
      const used=new Set(); _exRemap=[];
      for (let i=0;i<peaks.length;i++) {
        const ot=peaks[i]; let best=null,bd=Infinity;
        for (const c of cands){const d=Math.abs(c-ot);if(d<bd&&d<=1.5&&!used.has(c)&&c>0&&c<dur){bd=d;best=c;}}
        if(best!==null){_exRemap.push({_i:i,video_t:ot,target_t:best});used.add(best);}
      }
      exExStatus.textContent=`Auto-aligned ${_exRemap.length} of ${peaks.length} peaks`; _exDraw();
    });
    exResetBtn.addEventListener('click', () => { _exRemap=[]; _exDraw(); exExStatus.textContent=''; });

    exExportBtn.addEventListener('click', async () => {
      exExportBtn.disabled=true; exExStatus.textContent='Exporting...';
      exExProg.style.display='block'; exExProg.value=5;
      const remap = _exRemap.length>0 ? _exRemap.map(({video_t,target_t})=>({video_t,target_t})) : null;
      try {
        const r = await apiFetch('/api/sync/retime',{method:'POST',body:JSON.stringify({
          video_path:_exVidPath, audio_path:_exAudioPath, remap_points:remap,
        })});
        if (r.error) throw new Error(r.error);
        const poll = setInterval(async () => {
          const j = await apiFetch(`/api/jobs/${r.job_id}`);
          exExProg.value=j.progress||5; exExStatus.textContent=j.message||'Retiming...';
          if (j.status==='done') {
            clearInterval(poll); exExProg.style.display='none';
            exExStatus.textContent='Done: '+j.output; exExportBtn.disabled=false;
            document.dispatchEvent(new Event('session-updated'));
            toast('Synced MP4 saved', 'success');
          } else if (j.status==='error'||j.status==='stopped') {
            clearInterval(poll); exExProg.style.display='none';
            exExStatus.textContent='Failed: '+(j.error||j.status); exExportBtn.disabled=false;
          }
        }, 1500);
      } catch(e) { exExProg.style.display='none'; exExStatus.textContent='Error: '+e.message; exExportBtn.disabled=false; }
    });

    async function _exUploadAndAnalyze(file) {
      _exRemap = []; _exData = null;
      dropHint.textContent = 'Uploading ' + file.name + '...';
      exStatus.textContent = 'Uploading...';
      exProgress.style.display = 'block'; exProgress.value = 5;
      try {
        const up = await apiUpload('/api/fun/upload-video', [file]);
        const f = up?.files?.[0];
        if (!f?.path) throw new Error('Upload failed -- no path returned');
        _exVidPath = f.path;
        dropHint.textContent = file.name;
        dropSub.textContent  = '';
        exStatus.textContent = 'Extracting audio...'; exProgress.value = 20;
        const ex = await apiFetch('/api/sync/extract-audio',{method:'POST',body:JSON.stringify({video_path:_exVidPath})});
        if (ex.error) throw new Error(ex.error);
        _exAudioPath = ex.audio_path;
        exStatus.textContent = 'Analyzing beats and motion peaks...'; exProgress.value = 50;
        _exData = await apiFetch('/api/sync/analyze',{method:'POST',body:JSON.stringify({video_path:_exVidPath,audio_path:_exAudioPath})});
        const a=_exData.audio||{}, v=_exData.video||{};
        exInfo.textContent = `BPM: ${a.bpm?Math.round(a.bpm):'--'}  |  Beats: ${a.beat_times?.length||0}  |  Energy peaks: ${a.energy_peaks?.length||0}  |  Motion peaks: ${v.motion_peaks?.length||0}`;
        exStatus.textContent = 'Ready -- drag red diamonds to align, or hit Auto-Align, then Export.';
        exProgress.style.display = 'none';
        [exCanvasA,exCanvasV,exHint,exAutoBtn,exResetBtn,exExportBtn].forEach(e=>e.style.display='block');
        try {
          const resp = await fetch('/api/sync/serve?path='+encodeURIComponent(_exAudioPath));
          if (resp.ok) {
            if (!_exAudioCtx) _exAudioCtx = new (window.AudioContext||window.webkitAudioContext)();
            _exAudioBuf = await _exAudioCtx.decodeAudioData(await resp.arrayBuffer());
          }
        } catch(_) {}
        _exDraw();
      } catch(e) {
        exProgress.style.display='none'; exStatus.textContent='Error: '+e.message;
        dropHint.textContent='Drop MP4 here, or click to browse';
        dropSub.textContent='mp4 · mov · any video with audio';
      }
    }

    new ResizeObserver(()=>{ if(_exData) _exDraw(); }).observe(exCanvasA);

    return _card([
      LABEL('Sync Existing MP4'),
      el('div', { style: 'font-size:12px; color:var(--text-3); margin-bottom:6px;', text: 'Audio is extracted automatically. Video is retimed to match the beats. Your audio track is never touched.' }),
      fileInput,
      dropZone,
      exProgress,
      exStatus,
      exHint,
      exCanvasA,
      exCanvasV,
      exInfo,
      el('div', { style: 'display:flex; gap:8px; flex-wrap:wrap;' }, [exAutoBtn, exResetBtn, exExportBtn]),
      exExProg,
      exExStatus,
    ]);
  }

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
      lipSyncWrap,
      clipDurWrap,
      padBeforeWrap,
      padAfterWrap,
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

    // Beat sync -- appears after generation completes
    _card([
      LABEL('Beat Sync (after generation)'),
      el('div', {
        id: 'beat-sync-placeholder',
        style: 'font-size:12px; color:var(--text-4); font-style:italic; padding:6px 0;',
        text: 'Appears automatically after a Single Image video finishes generating.',
      }),
      syncSection,
    ]),

    // Standalone sync for existing MP4 files
    _buildExistingSync(),
  );

  // Wire song-upload state to single-image button too
  const _origUpdateButtons = _updateButtons;
  function _updateAll() { _origUpdateButtons(); _updateSingleBtn(); }
  panel.querySelectorAll('input[type=checkbox]').forEach(cb => cb.addEventListener('change', _updateAll));
}

export function receiveHandoff(data) {
  // future: accept handoff from Create Videos or Express
}
