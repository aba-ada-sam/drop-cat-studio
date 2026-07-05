/**
 * Drop Cat Go Studio -- Audio & Batch Processing
 * Section 1: Add ACE-Step AI music to session videos
 * Section 2: Upscale & Optimize (Lanczos or AI Real-ESRGAN)
 * Section 3: Batch video transforms (reverse, flip, speed...)
 * Section 4: Frame smoothing (motion interpolation)
 */
import { api, apiUpload, pollJob, stopJob } from './api.js?v=20260620a';
import { createProgressCard, createVideoPlayer, createSlider, createCheckbox, createSelect, el, formatDuration, pathToUrl } from './components.js?v=20260620a';
import { toast } from './shell/toast.js?v=20260620a';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260620a';

let _sessionListener = null;

export function init(panel) {
  panel.innerHTML = '';
  const root = el('div', { style: 'display:flex; flex-direction:column; gap:28px; padding:16px; max-width:900px; margin:0 auto;' });
  panel.appendChild(root);
  _buildAudioSection(root);
  _buildDivider(root, 'Upscale & Optimize');
  _buildUpscaleSection(root);
  _buildDivider(root, 'Crop & Reframe');
  _buildCropSection(root);
  _buildDivider(root, 'Batch Transforms');
  _buildBatchSection(root);
  _buildDivider(root, 'Frame Smoothing');
  _buildInterpolateSection(root);
}

// -- Section 1: AI Music -------------------------------------------------------

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

// -- Section 2: Upscale & Optimize -----------------------------------------------

function _buildUpscaleSection(root) {
  let _files = [];

  // -- File queue ----------------------------------------------------------
  const queueCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(queueCard);

  const queueHeader = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-bottom:10px;' });
  queueCard.appendChild(queueHeader);
  queueHeader.appendChild(el('span', { style: 'font-size:.85rem; font-weight:600; flex:1;', text: 'Upscale & Optimize -- File Queue' }));

  const upFileInput = el('input', { type: 'file', accept: 'video/*', multiple: 'true', style: 'display:none' });
  queueCard.appendChild(upFileInput);
  const addFilesBtn = el('button', { class: 'btn btn-sm', text: 'Add files...' });
  queueHeader.appendChild(addFilesBtn);
  addFilesBtn.addEventListener('click', () => upFileInput.click());

  upFileInput.addEventListener('change', async () => {
    if (!upFileInput.files?.length) return;
    try {
      const data = await apiUpload('/api/tools/upload', Array.from(upFileInput.files));
      if (data.rejected?.length) toast(`Skipped non-video file${data.rejected.length > 1 ? 's' : ''}: ${data.rejected.join(', ')}`, 'error');
      for (const f of data.files || []) _files.push(f);
      _renderFiles();
    } catch (e) { toast(e.message, 'error'); }
    upFileInput.value = '';
  });

  // Session picker
  const sessionBtn = el('button', { class: 'btn btn-sm', text: '+ From Session' });
  queueHeader.appendChild(sessionBtn);
  const sessionPicker = el('div', { style: 'display:none; margin-top:8px; border:1px solid var(--border); border-radius:6px; max-height:160px; overflow-y:auto;' });
  queueCard.appendChild(sessionPicker);

  sessionBtn.addEventListener('click', async () => {
    const show = sessionPicker.style.display === 'none';
    sessionPicker.style.display = show ? '' : 'none';
    if (!show) return;
    try {
      const data = await api('/api/session/videos');
      sessionPicker.innerHTML = '';
      const vids = data.videos || [];
      if (!vids.length) { sessionPicker.appendChild(el('div', { style: 'padding:10px; font-size:.8rem; color:var(--text-3);', text: 'No session videos.' })); return; }
      for (const v of vids) {
        const row = el('div', { style: 'display:flex; align-items:center; gap:8px; padding:6px 10px; cursor:pointer; border-bottom:1px solid var(--border-2);',
          onclick() {
            if (!_files.find(f => f.path === v.path)) {
              _files.push({ path: v.path, name: v.filename });
              _renderFiles();
            }
            sessionPicker.style.display = 'none';
          },
        }, [
          el('span', { style: 'flex:1; font-size:.8rem; color:var(--text-2);', text: v.filename }),
          el('span', { style: 'font-size:.72rem; color:var(--text-3);', text: formatDuration(v.duration) }),
        ]);
        sessionPicker.appendChild(row);
      }
    } catch (e) { toast(e.message, 'error'); }
  });

  const fileList = el('div', { style: 'display:flex; flex-direction:column; gap:4px; margin-top:8px;' });
  queueCard.appendChild(fileList);

  function _renderFiles() {
    fileList.innerHTML = '';
    if (!_files.length) {
      fileList.appendChild(el('div', { style: 'text-align:center; padding:16px 0; font-size:.8rem; color:var(--text-3);', text: 'No files added yet.' }));
      return;
    }
    for (let i = 0; i < _files.length; i++) {
      const f = _files[i];
      const row = el('div', { style: 'display:flex; align-items:center; gap:8px; padding:6px 10px; border-radius:6px; background:var(--bg-raised); border:1px solid var(--border-2);' }, [
        el('span', { style: 'flex:1; font-size:.8rem; color:var(--text-2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;', text: f.name || f.path.split(/[\\/]/).pop() }),
        formatDuration(f.duration) ? el('span', { style: 'font-size:.72rem; color:var(--text-3); flex-shrink:0;', text: formatDuration(f.duration) }) : el('span'),
        el('button', { class: 'btn-icon-xs remove', text: 'x', onclick() { _files.splice(i, 1); _renderFiles(); } }),
      ]);
      fileList.appendChild(row);
    }
  }
  _renderFiles();

  // -- Settings --------------------------------------------------------------
  const setCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(setCard);
  setCard.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:12px;', text: 'Settings' }));

  const method = createSelect(setCard, { label: 'Method', options: ['AI (Real-ESRGAN)', 'Fast (Lanczos)'], value: 'AI (Real-ESRGAN)' });
  const scaleSel = createSelect(setCard, { label: 'Scale', options: ['1x (optimize only)', '1.5x', '2x', '3x', '4x'], value: '2x' });
  const crf = createSlider(setCard, { label: 'Quality (CRF -- lower = better)', min: 0, max: 51, step: 1, value: 18 });
  setCard.appendChild(el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-top:8px; line-height:1.5;',
    text: 'AI rebuilds real detail while enlarging -- much sharper, but works frame by frame, so long videos take a while. Fast is instant but only stretches pixels. 1x skips resizing and just re-encodes to shrink oversized files.' }));

  // If the AI engine isn't present on this machine, quietly default to Fast.
  api('/api/tools/upscale').then(d => {
    if (d && d.ai_available === false) {
      method.value = 'Fast (Lanczos)';
      setCard.appendChild(el('div', { style: 'font-size:.72rem; color:var(--warning, #c90); margin-top:6px;',
        text: 'AI engine not installed on this machine -- using Fast.' }));
    }
  }).catch(() => {});

  // -- Process --------------------------------------------------------------
  const upBtn = el('button', {
    class: 'btn btn-primary',
    text: '▶ Upscale / Optimize',
    style: 'width:100%; font-size:1.05rem; padding:13px; font-weight:700;',
  });
  root.appendChild(upBtn);

  const upProgWrap = el('div');
  root.appendChild(upProgWrap);
  const upProg = createProgressCard(upProgWrap);

  upBtn.addEventListener('click', async () => {
    if (!_files.length) { toast('Add video files first', 'error'); return; }
    upBtn.disabled = true;
    upProg.show();

    try {
      const data = await api('/api/tools/upscale', {
        method: 'POST',
        body: JSON.stringify({
          files: _files.map(f => ({ path: f.path })),
          settings: {
            scale:  parseFloat(scaleSel.value),
            method: scaleSel.value.startsWith('1x') ? 'ffmpeg' : (method.value.startsWith('AI') ? 'ai' : 'ffmpeg'),
            crf:    crf.value,
          },
        }),
      });

      upProg.onCancel(async () => { await stopJob(data.job_id).catch(() => {}); upBtn.disabled = false; });
      pollJob(data.job_id,
        j => upProg.update(j.progress || 0, j.message || 'Processing...'),
        j => {
          upProg.hide(); upBtn.disabled = false;
          toast(j.message || `Processed ${j.meta?.processed || _files.length} file(s)`, 'success');
          if (j.output) pushToGallery('video-tools', j.output, 'Upscaled video', null, {});
        },
        err => { upProg.hide(); upBtn.disabled = false; toast(err, 'error'); },
      );
    } catch (e) { upProg.hide(); upBtn.disabled = false; toast(e.message, 'error'); }
  });
}

// -- Section 3: Batch Processing -----------------------------------------------

function _buildBatchSection(root) {
  let _files = [];

  // -- File queue ----------------------------------------------------------
  const queueCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(queueCard);

  const queueHeader = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-bottom:10px;' });
  queueCard.appendChild(queueHeader);
  queueHeader.appendChild(el('span', { style: 'font-size:.85rem; font-weight:600; flex:1;', text: 'Batch Transform -- File Queue' }));

  const batchFileInput = el('input', { type: 'file', accept: 'video/*', multiple: 'true', style: 'display:none' });
  queueCard.appendChild(batchFileInput);
  const addFilesBtn = el('button', { class: 'btn btn-sm', text: 'Add files...' });
  queueHeader.appendChild(addFilesBtn);
  addFilesBtn.addEventListener('click', () => batchFileInput.click());

  batchFileInput.addEventListener('change', async () => {
    if (!batchFileInput.files?.length) return;
    try {
      const data = await apiUpload('/api/tools/upload', Array.from(batchFileInput.files));
      if (data.rejected?.length) toast(`Skipped non-video file${data.rejected.length > 1 ? 's' : ''}: ${data.rejected.join(', ')}`, 'error');
      for (const f of data.files || []) _files.push(f);
      _renderFiles();
    } catch (e) { toast(e.message, 'error'); }
    batchFileInput.value = '';
  });

  // Session picker
  const sessionBtn = el('button', { class: 'btn btn-sm', text: '+ From Session' });
  queueHeader.appendChild(sessionBtn);
  const sessionPicker = el('div', { style: 'display:none; margin-top:8px; border:1px solid var(--border); border-radius:6px; max-height:160px; overflow-y:auto;' });
  queueCard.appendChild(sessionPicker);

  sessionBtn.addEventListener('click', async () => {
    const show = sessionPicker.style.display === 'none';
    sessionPicker.style.display = show ? '' : 'none';
    if (!show) return;
    try {
      const data = await api('/api/session/videos');
      sessionPicker.innerHTML = '';
      const vids = data.videos || [];
      if (!vids.length) { sessionPicker.appendChild(el('div', { style: 'padding:10px; font-size:.8rem; color:var(--text-3);', text: 'No session videos.' })); return; }
      for (const v of vids) {
        const row = el('div', { style: 'display:flex; align-items:center; gap:8px; padding:6px 10px; cursor:pointer; border-bottom:1px solid var(--border-2);',
          onclick() {
            if (!_files.find(f => f.path === v.path)) {
              _files.push({ path: v.path, name: v.filename });
              _renderFiles();
            }
            sessionPicker.style.display = 'none';
          },
        }, [
          el('span', { style: 'flex:1; font-size:.8rem; color:var(--text-2);', text: v.filename }),
          el('span', { style: 'font-size:.72rem; color:var(--text-3);', text: formatDuration(v.duration) }),
        ]);
        sessionPicker.appendChild(row);
      }
    } catch (e) { toast(e.message, 'error'); }
  });

  const fileList = el('div', { style: 'display:flex; flex-direction:column; gap:4px; margin-top:8px;' });
  queueCard.appendChild(fileList);

  function _renderFiles() {
    fileList.innerHTML = '';
    if (!_files.length) {
      fileList.appendChild(el('div', { style: 'text-align:center; padding:16px 0; font-size:.8rem; color:var(--text-3);', text: 'No files added yet.' }));
      return;
    }
    for (let i = 0; i < _files.length; i++) {
      const f = _files[i];
      const row = el('div', { style: 'display:flex; align-items:center; gap:8px; padding:6px 10px; border-radius:6px; background:var(--bg-raised); border:1px solid var(--border-2);' }, [
        el('span', { style: 'flex:1; font-size:.8rem; color:var(--text-2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;', text: f.name || f.path.split(/[\\/]/).pop() }),
        formatDuration(f.duration) ? el('span', { style: 'font-size:.72rem; color:var(--text-3); flex-shrink:0;', text: formatDuration(f.duration) }) : el('span'),
        el('button', { class: 'btn-icon-xs remove', text: 'x', onclick() { _files.splice(i, 1); _renderFiles(); } }),
      ]);
      fileList.appendChild(row);
    }
  }
  _renderFiles();

  // -- Effects --------------------------------------------------------------
  const fxCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(fxCard);
  fxCard.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:12px;', text: 'Effects' }));

  const fxGrid = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:10px;' });
  fxCard.appendChild(fxGrid);
  const reverseVid = createCheckbox(fxGrid, { label: 'Reverse Video',  checked: true });
  const mirror     = createCheckbox(fxGrid, { label: 'Mirror (H-Flip)' });
  const vflip      = createCheckbox(fxGrid, { label: 'V-Flip' });
  const muteAudio  = createCheckbox(fxGrid, { label: 'Mute Audio' });
  const speed = createSlider(fxCard, { label: 'Playback Speed', min: 0.25, max: 4, step: 0.25, value: 1, unit: 'x' });
  const volume = createSlider(fxCard, { label: 'Volume', min: 0, max: 300, step: 5, value: 100, unit: '%' });

  // -- Output ---------------------------------------------------------------
  const outCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(outCard);
  outCard.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:12px;', text: 'Output' }));

  const outGrid = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:10px;' });
  outCard.appendChild(outGrid);
  const upscale       = createCheckbox(outGrid, { label: 'Upscale' });
  const sharpen       = createCheckbox(outGrid, { label: 'Sharpen' });
  const upscaleMode   = createSelect(outCard, { label: 'Scale',   options: ['1.5x','2x','3x','4x'],                           value: '2x' });
  const outFormat     = createSelect(outCard, { label: 'Format',  options: ['mp4','mkv','mov','webm'],                         value: 'mp4' });
  const crf           = createSlider(outCard, { label: 'Quality (CRF -- lower = better)', min: 0, max: 51, step: 1, value: 18 });

  // -- Process --------------------------------------------------------------
  const processBtn = el('button', {
    class: 'btn btn-primary',
    text: '▶ Start Batch',
    style: 'width:100%; font-size:1.05rem; padding:13px; font-weight:700;',
  });
  root.appendChild(processBtn);

  const batchProgWrap = el('div');
  root.appendChild(batchProgWrap);
  const batchProg = createProgressCard(batchProgWrap);

  processBtn.addEventListener('click', async () => {
    if (!_files.length) { toast('Add video files first', 'error'); return; }
    processBtn.disabled = true;
    batchProg.show();

    try {
      const data = await api('/api/tools/process', {
        method: 'POST',
        body: JSON.stringify({
          files: _files.map(f => ({ path: f.path })),
          settings: {
            reverse_vid:    reverseVid.checked,
            mirror:         mirror.checked,
            vflip:          vflip.checked,
            speed:          speed.value,
            keep_audio:     !muteAudio.checked,
            reverse_aud:    reverseVid.checked,
            mute_audio:     muteAudio.checked,
            volume:         volume.value,
            upscale:        upscale.checked,
            upscale_mode:   upscaleMode.value,
            upscale_method: 'lanczos',
            sharpen:        sharpen.checked,
            sharpen_str:    1.0,
            out_format:     outFormat.value,
            crf:            crf.value,
          },
        }),
      });

      batchProg.onCancel(async () => { await stopJob(data.job_id).catch(() => {}); processBtn.disabled = false; });
      pollJob(data.job_id,
        j => batchProg.update(j.progress || 0, j.message || 'Processing...'),
        j => { batchProg.hide(); processBtn.disabled = false; toast(`Processed ${j.meta?.processed || _files.length} file(s)`, 'success'); },
        err => { batchProg.hide(); processBtn.disabled = false; toast(err, 'error'); },
      );
    } catch (e) { batchProg.hide(); processBtn.disabled = false; toast(e.message, 'error'); }
  });
}

// -- Section 4: Frame Smoothing ------------------------------------------------

function _buildInterpolateSection(root) {
  let _srcPath = null;
  let _activeJobId = null;

  // -- File picker -----------------------------------------------------------
  const pickCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(pickCard);

  const pickHeader = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-bottom:10px;' });
  pickCard.appendChild(pickHeader);
  pickHeader.appendChild(el('span', { style: 'font-size:.85rem; font-weight:600; flex:1;', text: 'Frame Smoothing -- Fill Jerky Segments' }));
  pickHeader.appendChild(el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'Generates in-between frames via motion interpolation' }));

  const fileInput = el('input', { type: 'file', accept: 'video/*', style: 'display:none' });
  pickCard.appendChild(fileInput);

  const btnRow = el('div', { style: 'display:flex; gap:8px; margin-bottom:10px;' });
  pickCard.appendChild(btnRow);
  const openBtn = el('button', { class: 'btn btn-sm', text: 'Open file...' });
  btnRow.appendChild(openBtn);
  openBtn.addEventListener('click', () => fileInput.click());

  const sessionBtn2 = el('button', { class: 'btn btn-sm', text: '+ From Session' });
  btnRow.appendChild(sessionBtn2);

  const selectedStrip = el('div', { style: 'display:none; padding:7px 10px; border-radius:6px; background:var(--bg-raised); border:1px solid var(--border-2); font-size:.8rem; color:var(--text-2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;' });
  pickCard.appendChild(selectedStrip);

  const sessionPicker2 = el('div', { style: 'display:none; border:1px solid var(--border); border-radius:6px; max-height:140px; overflow-y:auto; margin-top:6px;' });
  pickCard.appendChild(sessionPicker2);

  function _setSource(path) {
    _srcPath = path;
    selectedStrip.textContent = path.split(/[\\/]/).pop();
    selectedStrip.style.display = '';
    sessionPicker2.style.display = 'none';
  }

  fileInput.addEventListener('change', async () => {
    if (!fileInput.files?.length) return;
    try {
      const data = await apiUpload('/api/tools/upload', Array.from(fileInput.files));
      if (data.files?.[0]) _setSource(data.files[0].path);
    } catch (e) { toast(e.message, 'error'); }
    fileInput.value = '';
  });

  sessionBtn2.addEventListener('click', async () => {
    const show = sessionPicker2.style.display === 'none';
    sessionPicker2.style.display = show ? '' : 'none';
    if (!show) return;
    try {
      const data = await api('/api/session/videos');
      sessionPicker2.innerHTML = '';
      const vids = data.videos || [];
      if (!vids.length) { sessionPicker2.appendChild(el('div', { style: 'padding:10px; font-size:.8rem; color:var(--text-3);', text: 'No session videos.' })); return; }
      for (const v of vids) {
        const row = el('div', { style: 'display:flex; align-items:center; gap:8px; padding:6px 10px; cursor:pointer; border-bottom:1px solid var(--border-2);',
          onclick() { _setSource(v.path); },
        }, [
          el('span', { style: 'flex:1; font-size:.8rem; color:var(--text-2);', text: v.filename }),
          el('span', { style: 'font-size:.72rem; color:var(--text-3);', text: formatDuration(v.duration) }),
        ]);
        sessionPicker2.appendChild(row);
      }
    } catch (e) { toast(e.message, 'error'); }
  });

  // -- Options ---------------------------------------------------------------
  const optCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(optCard);
  optCard.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:12px;', text: 'Smoothing Options' }));

  const targetFps = createSlider(optCard, { label: 'Target FPS (0 = 2x source)', min: 0, max: 120, step: 1, value: 0, unit: 'fps' });

  const modeSelect = createSelect(optCard, {
    label: 'Mode',
    options: ['blend', 'mci', 'rife'],
    value: 'blend',
  });

  const modeDesc = el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-top:4px; line-height:1.5;' });
  optCard.appendChild(modeDesc);

  const modeDescriptions = {
    blend: 'Blend -- fast, uses simple frame averaging. Good for mild jitter.',
    mci: 'Motion-Compensated -- slower but tracks actual motion between frames. Better for fast action.',
    rife: 'RIFE -- GPU-accelerated, best quality. Needs rife-ncnn-vulkan.exe at C:\\rife-ncnn-vulkan\\. Falls back to mci if not found.',
  };

  function _updateModeDesc() { modeDesc.textContent = modeDescriptions[modeSelect.value] || ''; }
  modeSelect.el?.querySelector('select')?.addEventListener('change', _updateModeDesc);
  _updateModeDesc();

  // -- Run -------------------------------------------------------------------
  const smoothBtn = el('button', {
    class: 'btn btn-primary',
    text: 'Smooth Frames',
    style: 'width:100%; font-size:1.05rem; padding:13px; font-weight:700;',
  });
  root.appendChild(smoothBtn);

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

  smoothBtn.addEventListener('click', async () => {
    if (!_srcPath) { toast('Select a video first', 'error'); return; }
    smoothBtn.disabled = true;
    prog.show();
    prog.update(0, 'Starting...');
    player.hide();

    try {
      const { job_id } = await api('/api/tools/interpolate', {
        method: 'POST',
        body: JSON.stringify({
          video_path: _srcPath,
          target_fps: targetFps.value || 0,
          mode: modeSelect.value,
        }),
      });
      _activeJobId = job_id;

      pollJob(job_id,
        j => prog.update(j.progress || 0, j.message || 'Interpolating...'),
        j => {
          prog.hide();
          smoothBtn.disabled = false;
          _activeJobId = null;
          if (j.output) {
            player.show(pathToUrl(j.output), j.output);
            pushToGallery('video-tools', j.output, `Frame smooth (${modeSelect.value})`, null, {});
            toast('Smoothing done!', 'success');
          }
        },
        err => {
          prog.hide();
          smoothBtn.disabled = false;
          _activeJobId = null;
          toast(typeof err === 'string' ? err : (err?.message || 'Failed'), 'error');
        },
      );
    } catch (e) {
      prog.hide();
      smoothBtn.disabled = false;
      toast(e.message, 'error');
    }
  });
}

// -- Section: Crop & Reframe ---------------------------------------------------
// Visually crop a single video by dragging a marquee (like a Photoshop
// rectangular selection) over three frames sampled from the first 30 seconds.
// The same crop box is checked against each timepoint so the subject stays in
// frame across the clip. Coordinates are kept normalized (0..1) so the crop is
// resolution-independent; ffmpeg does the actual crop server-side.

const _CROP_ASPECTS = [
  { label: 'Freeform',        value: 'free' },
  { label: 'Square 1:1',      value: '1:1'  },
  { label: 'Portrait 9:16',   value: '9:16' },
  { label: 'Portrait 4:5',    value: '4:5'  },
  { label: 'Landscape 16:9',  value: '16:9' },
  { label: 'Landscape 4:3',   value: '4:3'  },
];

function _buildCropSection(root) {
  let _video = null;        // source path
  let _srcW = 0, _srcH = 0; // native pixels
  let _frames = [];         // [{ t, dataUrl }]
  let _active = 0;          // active frame index
  let _jobId = null;
  let _aspect = '1:1';
  let rect = { x: 0.2, y: 0.2, w: 0.6, h: 0.6 };  // normalized
  const MIN = 0.05;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  // Normalized width/height ratio for the current aspect (accounts for the
  // fact that normalized space is stretched vs. the pixel frame).
  function aspectN() {
    if (_aspect === 'free') return null;
    const [a, b] = _aspect.split(':').map(Number);
    if (!a || !b || !_srcW || !_srcH) return null;
    return (a / b) * (_srcH / _srcW);
  }

  // -- Picker --------------------------------------------------------------
  const pickCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(pickCard);
  const pickHeader = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-bottom:10px;' });
  pickCard.appendChild(pickHeader);
  pickHeader.appendChild(el('span', { style: 'font-size:.85rem; font-weight:600; flex:1;', text: 'Crop a Video -- drag the box to reframe' }));

  const cropFileInput = el('input', { type: 'file', accept: 'video/*', style: 'display:none' });
  pickCard.appendChild(cropFileInput);
  const cropOpenBtn = el('button', { class: 'btn btn-sm', text: 'Open file...' });
  pickHeader.appendChild(cropOpenBtn);
  cropOpenBtn.addEventListener('click', () => cropFileInput.click());
  const cropSessBtn = el('button', { class: 'btn btn-sm', text: '+ From Session' });
  pickHeader.appendChild(cropSessBtn);

  const cropSelStrip = el('div', { style: 'display:none; padding:7px 10px; border-radius:6px; background:var(--bg-raised); border:1px solid var(--border-2); font-size:.8rem; color:var(--text-2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;' });
  pickCard.appendChild(cropSelStrip);
  const cropSessPicker = el('div', { style: 'display:none; border:1px solid var(--border); border-radius:6px; max-height:140px; overflow-y:auto; margin-top:6px;' });
  pickCard.appendChild(cropSessPicker);

  cropFileInput.addEventListener('change', async () => {
    if (!cropFileInput.files?.length) return;
    try {
      const data = await apiUpload('/api/tools/upload', Array.from(cropFileInput.files));
      if (data.rejected?.length) toast(`Skipped: ${data.rejected.join(', ')}`, 'error');
      if (data.files?.[0]) await _loadVideo(data.files[0].path);
    } catch (e) { toast(e.message, 'error'); }
    cropFileInput.value = '';
  });

  cropSessBtn.addEventListener('click', async () => {
    const show = cropSessPicker.style.display === 'none';
    cropSessPicker.style.display = show ? '' : 'none';
    if (!show) return;
    try {
      const data = await api('/api/session/videos');
      cropSessPicker.innerHTML = '';
      const vids = data.videos || [];
      if (!vids.length) { cropSessPicker.appendChild(el('div', { style: 'padding:10px; font-size:.8rem; color:var(--text-3);', text: 'No session videos.' })); return; }
      for (const v of vids) {
        cropSessPicker.appendChild(el('div', {
          style: 'display:flex; align-items:center; gap:8px; padding:6px 10px; cursor:pointer; border-bottom:1px solid var(--border-2);',
          onclick() { cropSessPicker.style.display = 'none'; _loadVideo(v.path); },
        }, [
          el('span', { style: 'flex:1; font-size:.8rem; color:var(--text-2);', text: v.filename }),
          el('span', { style: 'font-size:.72rem; color:var(--text-3);', text: formatDuration(v.duration) }),
        ]));
      }
    } catch (e) { toast(e.message, 'error'); }
  });

  // -- Editor stage (hidden until a video is chosen) -----------------------
  const stage = el('div', { class: 'card', style: 'display:none; padding:14px;' });
  root.appendChild(stage);

  // Aspect ratio + audio row
  const ctrlRow = el('div', { style: 'display:flex; align-items:flex-end; gap:14px; flex-wrap:wrap; margin-bottom:12px;' });
  stage.appendChild(ctrlRow);
  const aspectSel = createSelect(ctrlRow, {
    label: 'Output shape', options: _CROP_ASPECTS, value: '1:1',
    onChange(v) { _aspect = v; _fitAspect(); _renderBox(); _updateReadout(); },
  });
  aspectSel.el.style.marginBottom = '0';
  const keepAudioChk = el('input', { type: 'checkbox', id: 'crop-keep-audio', checked: 'true' });
  ctrlRow.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:center; padding-bottom:6px;' }, [
    keepAudioChk,
    el('label', { for: 'crop-keep-audio', style: 'cursor:pointer; font-size:.82rem; color:var(--text-2);', text: 'Keep audio' }),
  ]));
  const sizeReadout = el('div', { style: 'margin-left:auto; padding-bottom:6px; font-size:.78rem; color:var(--text-3); font-variant-numeric:tabular-nums;' });
  ctrlRow.appendChild(sizeReadout);

  // Frame tabs (the 3 grabs)
  const tabRow = el('div', { style: 'display:flex; gap:8px; margin-bottom:10px;' });
  stage.appendChild(tabRow);

  // Editor (active frame + marquee overlay)
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
      + 'box-shadow:0 0 0 9999px rgba(0,0,0,.55); cursor:move; '
      + 'background:rgba(255,255,255,.02);',
  });
  overlay.appendChild(box);
  // Rule-of-thirds guides
  box.appendChild(el('div', { style: 'position:absolute; inset:0; pointer-events:none; background:'
    + 'linear-gradient(to right, transparent 33.33%, rgba(255,255,255,.35) 33.33%, rgba(255,255,255,.35) calc(33.33% + 1px), transparent calc(33.33% + 1px)),'
    + 'linear-gradient(to right, transparent 66.66%, rgba(255,255,255,.35) 66.66%, rgba(255,255,255,.35) calc(66.66% + 1px), transparent calc(66.66% + 1px)),'
    + 'linear-gradient(to bottom, transparent 33.33%, rgba(255,255,255,.35) 33.33%, rgba(255,255,255,.35) calc(33.33% + 1px), transparent calc(33.33% + 1px)),'
    + 'linear-gradient(to bottom, transparent 66.66%, rgba(255,255,255,.35) 66.66%, rgba(255,255,255,.35) calc(66.66% + 1px), transparent calc(66.66% + 1px));' }));

  // Handles: [id, hx, hy, cursor]
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
    // Edge handles only make sense in freeform; hide them when aspect-locked.
    const showEdges = _aspect === 'free';
    for (const id of ['n', 's', 'w', 'e']) handleEls[id].style.display = showEdges ? '' : 'none';
  }

  function _updateReadout() {
    const wpx = Math.max(2, Math.round(rect.w * _srcW));
    const hpx = Math.max(2, Math.round(rect.h * _srcH));
    sizeReadout.textContent = `Output: ${wpx} × ${hpx}px  ·  from ${_srcW} × ${_srcH}`;
  }

  // Fit a centered box of the current aspect at ~78% of the frame.
  function _fitAspect() {
    const arN = aspectN();
    if (arN == null) return;  // freeform -- leave the box as-is
    let h = 0.78, w = h * arN;
    if (w > 0.94) { w = 0.94; h = w / arN; }
    if (h > 0.94) { h = 0.94; w = h * arN; }
    rect = { x: (1 - w) / 2, y: (1 - h) / 2, w, h };
  }

  function _startMove(e) {
    if (e.target !== box) return;  // handles manage their own drags
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
        // Freeform -- each active edge moves independently.
        let x1 = start.x, y1 = start.y, x2 = start.x + start.w, y2 = start.y + start.h;
        if (hx === 1) x2 = clamp(start.x + start.w + dxN, x1 + MIN, 1);
        if (hx === -1) x1 = clamp(start.x + dxN, 0, x2 - MIN);
        if (hy === 1) y2 = clamp(start.y + start.h + dyN, y1 + MIN, 1);
        if (hy === -1) y1 = clamp(start.y + dyN, 0, y2 - MIN);
        rect = { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
      } else {
        // Aspect-locked -- corner drag, anchored at the opposite corner.
        const anchorX = hx === 1 ? start.x : start.x + start.w;
        const anchorY = hy === 1 ? start.y : start.y + start.h;
        const curX = clamp((hx === 1 ? start.x + start.w : start.x) + dxN, 0, 1);
        const curY = clamp((hy === 1 ? start.y + start.h : start.y) + dyN, 0, 1);
        let w = Math.abs(curX - anchorX);
        let h = Math.abs(curY - anchorY);
        // Reconcile to the locked ratio, driven by whichever axis moved more.
        if (w / arN >= h) h = w / arN; else w = h * arN;
        // Clamp within the frame from the anchor, preserving ratio.
        const availW = hx === 1 ? (1 - anchorX) : anchorX;
        const availH = hy === 1 ? (1 - anchorY) : anchorY;
        if (w > availW) { w = availW; h = w / arN; }
        if (h > availH) { h = availH; w = h * arN; }
        if (w < MIN) { w = MIN; h = w / arN; }
        rect = {
          x: hx === 1 ? anchorX : anchorX - w,
          y: hy === 1 ? anchorY : anchorY - h,
          w, h,
        };
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
      const t = el('div', {
        style: `flex:1; cursor:pointer; border-radius:6px; overflow:hidden; position:relative; `
          + `border:2px solid ${i === _active ? 'var(--accent)' : 'var(--border-2)'};`,
        onclick() { _active = i; editorImg.src = f.dataUrl; _renderTabs(); },
      }, [
        el('img', { src: f.dataUrl, style: 'display:block; width:100%; pointer-events:none;' }),
        el('span', {
          style: 'position:absolute; bottom:3px; right:4px; font-size:.66rem; font-weight:700; color:#fff; '
            + 'background:rgba(0,0,0,.6); padding:1px 5px; border-radius:8px;',
          text: `${f.t.toFixed(1)}s`,
        }),
      ]);
      tabRow.appendChild(t);
    });
  }

  async function _loadVideo(path) {
    cropSelStrip.textContent = 'Loading frames from ' + path.split(/[\\/]/).pop() + ' ...';
    cropSelStrip.style.display = '';
    cropResult.hide?.();
    try {
      const data = await api('/api/tools/crop-frames', {
        method: 'POST', body: JSON.stringify({ video_path: path }),
      });
      _video = path;
      _srcW = data.width; _srcH = data.height;
      _frames = (data.frames || []).map(f => ({ t: f.t, dataUrl: `data:image/jpeg;base64,${f.b64}` }));
      if (!_frames.length) { toast('Could not read this video', 'error'); return; }
      _active = 0;
      editorImg.src = _frames[0].dataUrl;
      cropSelStrip.textContent = path.split(/[\\/]/).pop() + `  (${_srcW}×${_srcH})`;
      _fitAspect();
      _renderTabs(); _renderBox(); _updateReadout();
      stage.style.display = '';
    } catch (e) {
      cropSelStrip.style.display = 'none';
      toast(e.message || 'Failed to load video', 'error');
    }
  }

  // -- Apply ---------------------------------------------------------------
  const cropBtn = el('button', {
    class: 'btn btn-primary',
    text: '✓ Apply Crop',
    style: 'width:100%; font-size:1.05rem; padding:13px; font-weight:700; margin-top:14px;',
  });
  stage.appendChild(cropBtn);

  const cropProgWrap = el('div');
  stage.appendChild(cropProgWrap);
  const cropProg = createProgressCard(cropProgWrap);
  cropProg.onCancel(async () => {
    if (_jobId) { await stopJob(_jobId).catch(() => {}); toast('Stopping...', 'info'); _jobId = null; }
  });

  const cropVidWrap = el('div');
  stage.appendChild(cropVidWrap);
  const cropResult = createVideoPlayer(cropVidWrap);
  cropResult.onStartOver(() => cropResult.hide());

  cropBtn.addEventListener('click', async () => {
    if (!_video) { toast('Choose a video first', 'error'); return; }
    cropBtn.disabled = true;
    cropProg.show(); cropProg.update(0, 'Starting...');
    cropResult.hide();
    try {
      const { job_id } = await api('/api/tools/crop', {
        method: 'POST',
        body: JSON.stringify({
          video_path: _video,
          rect: { x: rect.x, y: rect.y, w: rect.w, h: rect.h },
          keep_audio: keepAudioChk.checked,
        }),
      });
      _jobId = job_id;
      pollJob(job_id,
        j => cropProg.update(j.progress || 0, j.message || 'Cropping...'),
        j => {
          cropProg.hide(); cropBtn.disabled = false; _jobId = null;
          if (j.output) {
            cropResult.show(pathToUrl(j.output), j.output);
            pushToGallery('video-tools', j.output, 'Cropped video', null, {});
            toast('Crop complete!', 'success');
          } else {
            toast('Crop finished but no output found', 'error');
          }
        },
        err => {
          cropProg.hide(); cropBtn.disabled = false; _jobId = null;
          toast(typeof err === 'string' ? err : (err?.message || 'Crop failed'), 'error');
        },
      );
    } catch (e) {
      cropProg.hide(); cropBtn.disabled = false;
      toast(e.message, 'error');
    }
  });
}
