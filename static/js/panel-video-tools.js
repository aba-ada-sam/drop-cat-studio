/**
 * Drop Cat Go Studio — Audio & Batch Processing
 * Section 1: Add ACE-Step AI music to session videos
 * Section 2: Batch video transforms (reverse, flip, speed, upscale…)
 */
import { api, apiUpload, pollJob, stopJob } from './api.js?v=20260414';
import { createProgressCard, createVideoPlayer, createSlider, createCheckbox, createSelect, el, formatDuration, pathToUrl } from './components.js?v=20260426c';
import { toast } from './shell/toast.js?v=20260421c';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260419o';

export function init(panel) {
  panel.innerHTML = '';
  const root = el('div', { style: 'display:flex; flex-direction:column; gap:28px; padding:16px; max-width:900px; margin:0 auto;' });
  panel.appendChild(root);
  _buildAudioSection(root);
}

export function initBatch(panel) {
  panel.innerHTML = '';
  const root = el('div', { style: 'display:flex; flex-direction:column; gap:28px; padding:16px; max-width:900px; margin:0 auto;' });
  panel.appendChild(root);
  _buildBatchSection(root);
}

// ── Section 1: AI Music ───────────────────────────────────────────────────────

function _buildAudioSection(root) {
  let _selectedVideo = null;
  let _activeJobId   = null;

  // ── Session video picker ────────────────────────────────────────────────
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
  const clearSelBtn  = el('button', { class: 'btn btn-sm', text: '✕ Clear', style: 'flex-shrink:0;',
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
          text: 'No generated videos yet — create some in Create Videos first.',
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
          text: isSelected ? '✓ Selected' : 'Select',
        }));
        row.addEventListener('click', () => _applyVideo(vpath));
        videoList.appendChild(row);
      }
    } catch (e) { toast(e.message, 'error'); }
  }

  refreshBtn.addEventListener('click', _refreshList);
  _refreshList();
  window.addEventListener('session-updated', _refreshList);

  // ── Music options ────────────────────────────────────────────────────────
  const optionsCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(optionsCard);
  optionsCard.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:12px;', text: 'Music Options' }));

  optionsCard.appendChild(el('div', { style: 'font-size:.82rem; color:var(--text-3); margin-bottom:4px;', text: 'Music Prompt' }));
  optionsCard.appendChild(el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-bottom:6px; line-height:1.5;',
    text: 'Leave blank — the AI will analyze your video and write the prompt. Or guide it with genre, mood, and instruments.' }));
  const musicPromptTA = el('textarea', { rows: '2', style: 'width:100%; resize:vertical; font-size:.88rem; margin-bottom:10px;',
    placeholder: 'e.g. "lo-fi hip hop, dusty vinyl warmth, slow jazz brushwork" — or leave blank for auto' });
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

  // ── Generate ─────────────────────────────────────────────────────────────
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
    if (_activeJobId) { await stopJob(_activeJobId).catch(() => {}); toast('Stopping…', 'info'); _activeJobId = null; }
  });

  const vidWrap = el('div');
  root.appendChild(vidWrap);
  const player = createVideoPlayer(vidWrap);
  player.onStartOver(() => player.hide());

  genBtn.addEventListener('click', async () => {
    if (!_selectedVideo) { toast('Select a video first', 'error'); return; }
    genBtn.disabled = true;
    prog.show();
    prog.update(0, 'Starting…');
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
        j => prog.update(j.progress || 0, j.message || 'Working…'),
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

// ── Divider ───────────────────────────────────────────────────────────────────

function _buildDivider(root) {
  const div = el('div', { style: 'display:flex; align-items:center; gap:12px;' });
  div.appendChild(el('div', { style: 'flex:1; height:1px; background:var(--border);' }));
  div.appendChild(el('span', { style: 'font-size:.68rem; font-weight:800; letter-spacing:.12em; color:var(--text-3); opacity:.6; text-transform:uppercase; white-space:nowrap;', text: 'Batch Processing' }));
  div.appendChild(el('div', { style: 'flex:1; height:1px; background:var(--border);' }));
  root.appendChild(div);
}

// ── Section 2: Batch Processing ───────────────────────────────────────────────

function _buildBatchSection(root) {
  let _files = [];

  // ── File queue ──────────────────────────────────────────────────────────
  const queueCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(queueCard);

  const queueHeader = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-bottom:10px;' });
  queueCard.appendChild(queueHeader);
  queueHeader.appendChild(el('span', { style: 'font-size:.85rem; font-weight:600; flex:1;', text: 'Batch Transform — File Queue' }));

  const batchFileInput = el('input', { type: 'file', accept: 'video/*', multiple: 'true', style: 'display:none' });
  queueCard.appendChild(batchFileInput);
  const addFilesBtn = el('button', { class: 'btn btn-sm', text: 'Add files...' });
  queueHeader.appendChild(addFilesBtn);
  addFilesBtn.addEventListener('click', () => batchFileInput.click());

  batchFileInput.addEventListener('change', async () => {
    if (!batchFileInput.files?.length) return;
    try {
      const data = await apiUpload('/api/tools/upload', Array.from(batchFileInput.files));
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
        el('button', { class: 'btn-icon-xs remove', text: '✕', onclick() { _files.splice(i, 1); _renderFiles(); } }),
      ]);
      fileList.appendChild(row);
    }
  }
  _renderFiles();

  // ── Effects ──────────────────────────────────────────────────────────────
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

  // ── Output ───────────────────────────────────────────────────────────────
  const outCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(outCard);
  outCard.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:12px;', text: 'Output' }));

  const outGrid = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:10px;' });
  outCard.appendChild(outGrid);
  const upscale       = createCheckbox(outGrid, { label: 'Upscale' });
  const sharpen       = createCheckbox(outGrid, { label: 'Sharpen' });
  const upscaleMode   = createSelect(outCard, { label: 'Scale',   options: ['1.5x','2x','3x','4x'],                           value: '2x' });
  const outFormat     = createSelect(outCard, { label: 'Format',  options: ['mp4','mkv','mov','webm'],                         value: 'mp4' });
  const crf           = createSlider(outCard, { label: 'Quality (CRF — lower = better)', min: 0, max: 51, step: 1, value: 18 });

  // ── Process ──────────────────────────────────────────────────────────────
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
        j => batchProg.update(j.progress || 0, j.message || 'Processing…'),
        j => { batchProg.hide(); processBtn.disabled = false; toast(`Processed ${j.meta?.processed || _files.length} file(s)`, 'success'); },
        err => { batchProg.hide(); processBtn.disabled = false; toast(err, 'error'); },
      );
    } catch (e) { batchProg.hide(); processBtn.disabled = false; toast(e.message, 'error'); }
  });
}
