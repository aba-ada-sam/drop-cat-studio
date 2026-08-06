/**
 * Music Video Tab
 *
 * PRIMARY surface: the RATIFIED ENGINE (chain.py, wrapped as a subprocess by
 * features/song_video/chain_runner.py -- see REVIEW_FINDINGS_2026-08-05.md).
 * Song (upload or AI-generate) + 1+ anchor images (each with an optional
 * per-scene prompt) -> one continuous-performance video via chain.py's
 * ratified recipe v3 defaults (241f/clip, 0.15 crossfade, smart-seams,
 * judge-select). Progress polls GET /api/song-video/chain/status.
 *
 * Everything below the "Legacy pipeline" collapsible is the OLDER
 * features/song_video/pipeline.py implementation (batch folder + single
 * image) -- kept working exactly as before, just demoted out of the primary
 * spot. No MuseTalk/Lip Sync control anywhere in this file (removed per
 * Andrew, 2026-08-05 night -- native audio-conditioned sync in chain.py is
 * the proven mechanism; MuseTalk is gone from V2).
 *
 * Batch runner (legacy) is SERVER-SIDE: state persists across DCS restarts.
 * On tab open, the tab auto-resumes any in-progress LEGACY batch. The
 * ratified engine is NOT wired into that batch runner or into
 * core/job_manager.py's queue (chain.py talks to the WanGP worker directly
 * over HTTP, bypassing the queue) -- its progress only shows in this panel.
 */

import { el }                    from './components.js';
import { apiFetch }              from './shell/toast.js';
import { toast }                 from './shell/toast.js';
import { apiUpload }             from './api.js';

// --- shared helpers ----------------------------------------------------------

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

function _toggle(label, checked = false) {
  const wrap  = el('label', { style: 'display:flex; align-items:center; gap:8px; cursor:pointer; font-size:13px; color:var(--text-2); user-select:none;' });
  const input = el('input', { type: 'checkbox' });
  input.checked = checked;
  input.style.accentColor = 'var(--accent)';
  wrap.append(input, label);
  return { wrap, input };
}

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

// Converts an absolute filesystem output path to a servable URL, mirroring
// the pattern used everywhere else in this app (e.g. tab-fun-videos.js).
function _outputPathToUrl(p) {
  if (!p) return '';
  const norm = p.replace(/\\/g, '/').toLowerCase();
  const idx = norm.indexOf('/output/');
  return idx !== -1 ? p.replace(/\\/g, '/').slice(idx) : '';
}

// --- init ---------------------------------------------------------------------

export function init(panel) {
  // ==========================================================================
  // RATIFIED ENGINE (primary surface)
  // ==========================================================================

  let _rSongPath   = null;
  let _rSongDur    = 0;
  let _rImages     = [];   // [{ path, url, name, promptInput }]
  let _rRunning    = false;
  let _rPollTimer  = null;

  // -- Song (upload or generate) ----------------------------------------------

  const rSongHint    = el('div', { style: 'font-size:13px; color:var(--text-3);', text: 'Drop your song here or click to browse' });
  const rSongHintSub = el('div', { style: 'font-size:11px; color:var(--text-4); margin-top:4px;', text: 'mp3 / wav / flac / m4a / aac -- optional: skip this and generate one with AI below' });
  const rSongHintArea = el('div', { style: 'display:flex; flex-direction:column; align-items:center; padding:20px 0; gap:2px;' }, [rSongHint, rSongHintSub]);
  const rSongPreview = el('audio', { style: 'display:none; width:100%; margin:8px 0;' });
  rSongPreview.controls = true;
  const rSongClearBtn = el('button', { style: 'display:none; align-self:flex-end; background:none; border:none; color:var(--red); cursor:pointer; font-size:11px; padding:0;', text: 'remove song' });

  const rSongDrop = el('div', {
    style: 'border:1px dashed var(--border-2); border-radius:var(--r-md); cursor:pointer; display:flex; flex-direction:column; align-items:center; transition:border-color .12s, background .12s; background:var(--surface-2);',
  });
  rSongDrop.append(rSongHintArea, rSongPreview, rSongClearBtn);

  const rSongFileInput = el('input', { type: 'file', accept: 'audio/*,.mp3,.wav,.flac,.ogg,.m4a,.aac,.opus,.mpeg' });
  rSongFileInput.style.display = 'none';
  panel.appendChild(rSongFileInput);

  rSongDrop.addEventListener('dragover', e => { e.preventDefault(); rSongDrop.style.borderColor = 'var(--accent)'; rSongDrop.style.background = 'var(--accent-bg)'; });
  rSongDrop.addEventListener('dragleave', () => { rSongDrop.style.borderColor = 'var(--border-2)'; rSongDrop.style.background = 'var(--surface-2)'; });
  rSongDrop.addEventListener('drop', e => {
    e.preventDefault(); rSongDrop.style.borderColor = 'var(--border-2)'; rSongDrop.style.background = 'var(--surface-2)';
    const f = Array.from(e.dataTransfer.files).find(f => f.type.startsWith('audio/') || /\.(mp3|wav|flac|ogg|m4a|aac|opus|mpeg|mpg)$/i.test(f.name));
    if (f) _rUploadSong(f);
  });
  rSongDrop.addEventListener('click', e => {
    if (e.target === rSongPreview || e.target === rSongClearBtn || rSongPreview.contains(e.target)) return;
    rSongFileInput.click();
  });
  rSongFileInput.addEventListener('change', () => { if (rSongFileInput.files[0]) _rUploadSong(rSongFileInput.files[0]); rSongFileInput.value = ''; });
  rSongClearBtn.addEventListener('click', e => { e.stopPropagation(); _rClearSong(); });

  async function _rUploadSong(file) {
    rSongHint.textContent = 'Uploading...';
    try {
      const resp = await apiUpload('/api/song-video/upload-audio', [file]);
      const f = resp?.files?.[0];
      if (!f?.path) throw new Error('No path returned');
      rSongPreview.src = f.url;
      rSongPreview.style.display = 'block';
      rSongHintArea.style.display = 'none';
      rSongClearBtn.style.display = 'block';
      _rSongPath = f.path;
      _rSongDur  = f.duration || 0;
      _rUpdateGenerateVisibility();
      _rUpdateStartBtn();
    } catch (err) {
      toast('Song upload failed: ' + err.message, 'error');
      rSongHint.textContent = 'Drop your song here or click to browse';
    }
  }

  function _rClearSong() {
    _rSongPath = null; _rSongDur = 0;
    rSongPreview.src = ''; rSongPreview.style.display = 'none';
    rSongHintArea.style.display = 'flex';
    rSongClearBtn.style.display = 'none';
    rSongDrop.style.borderColor = 'var(--border-2)';
    _rUpdateGenerateVisibility();
    _rUpdateStartBtn();
  }

  // Generate-with-AI fields -- only relevant/shown when no song is uploaded.
  const { wrap: rLengthWrap, input: rLengthSlider } = _numRow('Video length (AI-written song)', 5, 120, 1, 30, 's');
  const rLyricsInput = el('textarea', {
    placeholder: 'Lyrics (optional -- blank = instrumental)',
    style: 'width:100%; box-sizing:border-box; background:var(--surface-2); border:1px solid var(--border-2); border-radius:var(--r-md); color:var(--text); padding:8px 10px; font-family:inherit; font-size:12px; resize:vertical; min-height:44px; outline:none;',
  });
  const rMusicPromptInput = el('input', {
    type: 'text', placeholder: 'Music vibe (e.g. "warm acoustic ballad")',
    style: 'width:100%; box-sizing:border-box; background:var(--surface-2); border:1px solid var(--border-2); border-radius:var(--r-md); color:var(--text); padding:8px 10px; font-family:inherit; font-size:12px; outline:none;',
  });
  const rGenerateWrap = el('div', { style: 'display:flex; flex-direction:column; gap:6px; padding-top:6px; border-top:1px solid var(--border-2);' },
    [LABEL('No song -- generate one (ACE-Step)'), rLengthWrap, rLyricsInput, rMusicPromptInput]);

  function _rUpdateGenerateVisibility() {
    rGenerateWrap.style.display = _rSongPath ? 'none' : 'flex';
  }
  _rUpdateGenerateVisibility();

  // -- Anchor images (1+, each with an optional scene prompt) -----------------

  const rImgDrop = el('div', {
    style: 'border:1px dashed var(--border-2); border-radius:var(--r-md); padding:16px; text-align:center; cursor:pointer; font-size:13px; color:var(--text-3); background:var(--surface-2); transition:border-color .12s, background .12s;',
    text: 'Drop anchor image(s) here or click to browse -- 1 required, more = A/B/... scene cycling',
  });
  const rImgInput = el('input', { type: 'file', accept: 'image/*' });
  rImgInput.multiple = true;
  rImgInput.style.display = 'none';
  panel.appendChild(rImgInput);

  rImgDrop.addEventListener('dragover', e => { e.preventDefault(); rImgDrop.style.borderColor = 'var(--accent)'; rImgDrop.style.background = 'var(--accent-bg)'; });
  rImgDrop.addEventListener('dragleave', () => { rImgDrop.style.borderColor = 'var(--border-2)'; rImgDrop.style.background = 'var(--surface-2)'; });
  rImgDrop.addEventListener('drop', e => {
    e.preventDefault(); rImgDrop.style.borderColor = 'var(--border-2)'; rImgDrop.style.background = 'var(--surface-2)';
    const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('image/'));
    if (files.length) _rUploadImages(files);
  });
  rImgDrop.addEventListener('click', () => rImgInput.click());
  rImgInput.addEventListener('change', () => {
    const files = Array.from(rImgInput.files);
    if (files.length) _rUploadImages(files);
    rImgInput.value = '';
  });

  const rImgList = el('div', { style: 'display:flex; flex-direction:column; gap:8px;' });
  const rScenePromptHint = el('div', {
    style: 'display:none; font-size:11px; color:#e8b820;',
    text: 'Fill a scene prompt for every anchor image, or leave them ALL blank to use the default prompt -- a partial mix would misalign scenes to the wrong prompt.',
  });

  async function _rUploadImages(files) {
    try {
      const resp = await apiUpload('/api/song-video/upload-image', files);
      const added = (resp?.files || []).filter(f => f?.path);
      if (!added.length) throw new Error('No usable images in that selection');
      for (const f of added) {
        const promptInput = el('textarea', {
          placeholder: 'Scene prompt for this image (optional)',
          style: 'flex:1; box-sizing:border-box; background:var(--surface-2); border:1px solid var(--border-2); border-radius:var(--r-md); color:var(--text); padding:6px 8px; font-family:inherit; font-size:12px; resize:vertical; min-height:36px; outline:none;',
        });
        promptInput.addEventListener('input', _rValidateScenePrompts);
        _rImages.push({ path: f.path, url: f.url, name: f.name, promptInput });
      }
      _rRenderImages();
    } catch (e) {
      toast('Image upload failed: ' + e.message, 'error');
    }
  }

  function _rRenderImages() {
    rImgList.textContent = '';
    _rImages.forEach((im, i) => {
      const thumb = el('img', {
        title: im.name,
        style: 'height:52px; width:52px; object-fit:cover; border-radius:4px; flex:0 0 auto;',
      });
      thumb.src = im.url;
      const tag = el('div', { style: 'font-size:11px; color:var(--text-3); min-width:16px;', text: String.fromCharCode(65 + i) }); // A, B, C...
      const removeBtn = el('button', {
        text: 'x', title: 'Remove',
        style: 'background:none; border:none; color:var(--red); cursor:pointer; font-size:14px; font-weight:700; line-height:1; padding:4px 6px; flex:0 0 auto;',
      });
      removeBtn.addEventListener('click', () => { _rImages.splice(i, 1); _rRenderImages(); _rUpdateStartBtn(); });
      const row = el('div', { style: 'display:flex; align-items:center; gap:8px;' }, [tag, thumb, im.promptInput, removeBtn]);
      rImgList.appendChild(row);
    });
    _rValidateScenePrompts();
    _rUpdateStartBtn();
  }

  function _rValidateScenePrompts() {
    const filled = _rImages.filter(im => im.promptInput.value.trim()).length;
    rScenePromptHint.style.display = (filled > 0 && filled < _rImages.length) ? 'block' : 'none';
  }

  // -- Options: seeds-per-clip, judge-select, DOF finish (stub, disabled) -----

  const { wrap: rSeedsWrap, input: rSeedsSlider } = _numRow('Seeds per clip (best-of-N)', 1, 10, 1, 4, '');
  const { wrap: rJudgeWrap, input: rJudgeToggle } = _toggle('Judge-select (vision-judge take selection)', true);
  const { wrap: rDofWrap, input: rDofToggle } = _toggle(
    el('span', {}, [
      'Apply DOF finish (post-step) ',
      el('span', { style: 'color:var(--text-4); font-size:11px;', text: '-- not implemented yet, see features/song_video/dof_finish.py' }),
    ]),
    false,
  );
  rDofToggle.disabled = true;
  rDofToggle.title = 'The ratified per-scene DOF pass has no recovered script -- see dof_finish.py. Disabled until one exists.';

  // -- Start / progress / cancel -----------------------------------------------

  const rStartBtn = el('button', {
    text: 'Start Ratified Render',
    disabled: true,
    style: 'padding:12px; border-radius:var(--r-lg); border:none; cursor:not-allowed; font-size:15px; font-weight:700; background:var(--gold); color:#000; opacity:.45; width:100%;',
  });
  const rCancelBtn = el('button', {
    text: 'Cancel',
    style: 'display:none; padding:8px 14px; border-radius:var(--r-md); border:1px solid var(--red); background:none; color:var(--red); cursor:pointer; font-size:13px; font-weight:600;',
  });

  const rPhaseLabel = el('div', { style: 'font-size:13px; color:var(--text-2); min-height:16px;' });
  const rClipLabel  = el('div', { style: 'font-size:12px; color:var(--text-3); min-height:14px;' });
  const rProgress   = el('progress', { style: 'display:none; width:100%; height:6px;' });
  rProgress.max = 100; rProgress.value = 0;
  const rLogTail = el('pre', {
    style: 'display:none; max-height:160px; overflow-y:auto; background:var(--surface-2); border:1px solid var(--border-2); border-radius:var(--r-md); padding:8px; font-size:10.5px; line-height:1.4; color:var(--text-3); white-space:pre-wrap; word-break:break-word; margin:0;',
  });
  const rResult = el('video', { style: 'display:none; width:100%; border-radius:var(--r-md); margin-top:6px; max-height:280px;' });
  rResult.controls = true;

  function _rUpdateStartBtn() {
    const ready = _rImages.length > 0 && !_rRunning;
    rStartBtn.disabled = !ready;
    rStartBtn.style.opacity = ready ? '1' : '.45';
    rStartBtn.style.cursor  = ready ? 'pointer' : 'not-allowed';
  }

  async function _rStart() {
    if (!_rImages.length) { toast('Add at least one anchor image first', 'error'); return; }
    const filled = _rImages.filter(im => im.promptInput.value.trim()).length;
    if (filled > 0 && filled < _rImages.length) {
      toast('Fill a scene prompt for every image, or clear them all', 'error');
      return;
    }

    _rRunning = true;
    _rUpdateStartBtn();
    rCancelBtn.style.display = 'inline-block';
    rProgress.style.display = 'block'; rProgress.value = 2;
    rLogTail.style.display = 'block'; rLogTail.textContent = '';
    rResult.style.display = 'none';
    rPhaseLabel.textContent = 'Starting...';
    rClipLabel.textContent = '';

    const body = {
      song_path:      _rSongPath || '',
      target_length:  parseInt(rLengthSlider.value, 10),
      lyrics_text:    rLyricsInput.value.trim(),
      music_prompt:   rMusicPromptInput.value.trim(),
      images:         _rImages.map(im => im.path),
      scene_prompts:  filled ? _rImages.map(im => im.promptInput.value.trim()) : [],
      seeds_per_clip: parseInt(rSeedsSlider.value, 10),
      judge_select:   rJudgeToggle.checked,
      dof_finish:     false,  // checkbox is disabled -- always off tonight
    };

    try {
      await apiFetch('/api/song-video/chain/start', { method: 'POST', body: JSON.stringify(body) });
      _rStartPoll();
    } catch (e) {
      _rRunning = false;
      _rUpdateStartBtn();
      rCancelBtn.style.display = 'none';
      rPhaseLabel.textContent = 'Failed to start: ' + e.message;
      toast('Start failed: ' + e.message, 'error');
    }
  }

  function _rStartPoll() {
    if (_rPollTimer) return;
    _rPollTimer = setInterval(_rPollStatus, 1200);
    _rPollStatus();
  }

  function _rStopPoll() {
    if (_rPollTimer) { clearInterval(_rPollTimer); _rPollTimer = null; }
  }

  async function _rPollStatus() {
    let s;
    try {
      s = await apiFetch('/api/song-video/chain/status');
    } catch {
      return;
    }
    rProgress.value = s.pct || 0;
    rPhaseLabel.textContent = s.message || s.phase || s.status;
    rClipLabel.textContent = s.clip_i && s.clip_n
      ? `Clip ${s.clip_i}/${s.clip_n} [${s.clip_kind || ''}]` + (s.take_n ? `  --  take ${Math.min(s.take_i || 0, s.take_n)}/${s.take_n}` : '')
      : '';
    if (s.log_tail && s.log_tail.length) {
      rLogTail.textContent = s.log_tail.slice(-60).join('\n');
      rLogTail.scrollTop = rLogTail.scrollHeight;
    }

    if (s.status === 'done') {
      _rStopPoll();
      _rRunning = false;
      _rUpdateStartBtn();
      rCancelBtn.style.display = 'none';
      rProgress.style.display = 'none';
      rPhaseLabel.textContent = 'Done';
      const url = _outputPathToUrl(s.output_path);
      if (url) { rResult.src = url; rResult.style.display = 'block'; }
      toast('Ratified render complete', 'success');
      document.dispatchEvent(new Event('session-updated'));
    } else if (s.status === 'error') {
      _rStopPoll();
      _rRunning = false;
      _rUpdateStartBtn();
      rCancelBtn.style.display = 'none';
      rProgress.style.display = 'none';
      rPhaseLabel.textContent = 'Failed: ' + (s.error || 'unknown error');
      toast('Ratified render failed: ' + (s.error || 'unknown error'), 'error');
    } else if (s.status === 'cancelled') {
      _rStopPoll();
      _rRunning = false;
      _rUpdateStartBtn();
      rCancelBtn.style.display = 'none';
      rProgress.style.display = 'none';
      rPhaseLabel.textContent = 'Cancelled';
    }
  }

  rStartBtn.addEventListener('click', _rStart);
  rCancelBtn.addEventListener('click', async () => {
    rCancelBtn.disabled = true;
    rPhaseLabel.textContent = 'Cancelling...';
    try {
      await apiFetch('/api/song-video/chain/cancel', { method: 'POST', body: '{}' });
    } catch (e) {
      toast('Cancel failed: ' + e.message, 'error');
    }
    rCancelBtn.disabled = false;
  });

  // On tab open: if a chain job is already running (e.g. left mid-render on
  // a previous visit), reconnect the progress panel -- never auto-START one.
  (async () => {
    try {
      const s = await apiFetch('/api/song-video/chain/status');
      if (s.status === 'starting' || s.status === 'running') {
        _rRunning = true;
        _rUpdateStartBtn();
        rCancelBtn.style.display = 'inline-block';
        rProgress.style.display = 'block';
        rLogTail.style.display = 'block';
        _rStartPoll();
      }
    } catch {}
  })();

  const ratifiedSection = _card([
    LABEL('Song'),
    rSongDrop,
    rGenerateWrap,
  ]);
  const ratifiedImages = _card([
    LABEL('Anchor Image(s) + Scene Prompts'),
    rImgDrop,
    rImgList,
    rScenePromptHint,
  ]);
  const ratifiedOptions = _card([
    LABEL('Ratified Recipe Options'),
    el('div', { style: 'font-size:11px; color:var(--text-4);', text: 'Fixed by the ratified recipe: 241 frames/clip, 0.15s crossfade, smart-seams, min-clip-frames 169 (RECIPE.json + review/render_v16_detached.ps1).' }),
    rSeedsWrap,
    rJudgeWrap,
    rDofWrap,
  ]);
  const ratifiedRun = _card([
    el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [rStartBtn, rCancelBtn]),
    rPhaseLabel,
    rClipLabel,
    rProgress,
    rLogTail,
    rResult,
  ]);

  // ==========================================================================
  // LEGACY PIPELINE (features/song_video/pipeline.py -- older engine)
  // ==========================================================================

  let _songPath      = null;
  let _songDur       = 0;
  let _songAnalysis  = null;
  let _folderFiles   = [];
  let _folderPath    = '';
  let _pollTimer     = null;
  let _analyzeSeq    = 0;

  // -- Song upload --------------------------------------------------------------

  const songHintText  = el('div', { style: 'font-size:13px; color:var(--text-3);', text: 'Drop your song here or click to browse' });
  const songHintSub   = el('div', { style: 'font-size:11px; color:var(--text-4); margin-top:4px;', text: 'mp3 / wav / flac / m4a / aac -- optional: skip this and AI writes one to your Video length below' });
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
      _syncLengthControl();
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
    _syncLengthControl();
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
      _syncLengthControl();
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
      a.duration_display || null,
      a.bpm ? `${a.bpm} BPM` : null,
      a.key ? `${a.key} ${a.mode || ''}`.trim() : null,
      a.mood || null,
    ].filter(Boolean);
    if (chips.length) {
      const row = el('div', { style: 'display:flex; flex-wrap:wrap; gap:6px;' });
      chips.forEach(text => {
        row.appendChild(el('div', {
          style: 'font-size:11px; background:var(--surface-2); border:1px solid var(--border-2); border-radius:20px; padding:3px 10px; color:var(--text-2);',
          text,
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

  // -- Model selector -------------------------------------------------------------

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

  // -- Folder picker ----------------------------------------------------------------

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
      // Image-only folder scan (was /api/zoom/scan-folder before Forge/zoom removal).
      const r = await apiFetch('/api/i2v/scan_folder', { method: 'POST', body: JSON.stringify({ folder }) });
      const imgs = r.images || [];
      _folderFiles = imgs;
      folderStatus.textContent = !imgs.length
        ? 'No images found'
        : `${imgs.length} image${imgs.length !== 1 ? 's' : ''} found`;
      _updateButtons();
    } catch (e) { folderStatus.textContent = 'Error: ' + e.message; }
  }

  // -- Options ------------------------------------------------------------------

  const { wrap: loopWrap, input: loopCheck } = _toggle('Loop continuously (repeat folder)', false);

  // Clip duration slider
  const { wrap: clipDurWrap, input: clipDurSlider } = _numRow('Clip length', 4, 15, 1, 6, 's');
  // Shared by both Single Image and Folder Batch below -- but the server
  // floors Folder Batch clips at 8s regardless of this value (routes.py
  // batch_start: max(8, min(15, ...))), while Single Image respects it
  // exactly (routes.py generate: max(4, min(15, ...))). A batch run left at
  // the 6s default silently became 8s with no indication anywhere, so this
  // only shows up when it's actually relevant instead of raising the slider's
  // floor and taking away a range Single Image genuinely supports.
  const clipDurBatchHint = el('div', {
    style: 'display:none; font-size:11px; color:#e8b820;',
    text: 'Folder Batch floors this at 8s regardless -- Single Image uses your exact value.',
  });
  clipDurWrap.appendChild(clipDurBatchHint);
  function _updateClipDurHint() {
    clipDurBatchHint.style.display = parseInt(clipDurSlider.value, 10) < 8 ? '' : 'none';
  }
  clipDurSlider.addEventListener('input', _updateClipDurHint);
  _updateClipDurHint();

  // Padding: seconds of silent video before song starts / after song ends
  const { wrap: padBeforeWrap, input: padBeforeSlider } = _numRow('Video before song starts', 0, 10, 1, 0, 's');
  const { wrap: padAfterWrap,  input: padAfterSlider  } = _numRow('Video after song ends',    0, 10, 1, 0, 's');

  // Video length: with a song loaded the real length is fixed by
  // audio_dur + pad_before + pad_after (routes.py /generate), so this
  // control mirrors that total and edits push the difference into
  // pad_after. With NO song this is the only length signal DCS has, and
  // it sizes the ACE-Step track routes.py writes to fill the gap --
  // Andrew's spec: no song -> follow the length setting, generate one.
  const ACE_MAX_LEN = 120;  // mirrors audio_generator.MAX_DURATION
  const { wrap: lengthWrap, input: lengthSlider, val: lengthVal } = _numRow('Video length', 5, ACE_MAX_LEN, 1, 30, 's');
  const lengthHint = el('div', { style: 'font-size:11px; color:var(--text-3);' });
  lengthWrap.appendChild(lengthHint);

  function _syncLengthControl() {
    if (_songPath && _songDur > 0) {
      const songSecs = Math.round(_songDur);
      const padB = parseInt(padBeforeSlider.value, 10) || 0;
      const padA = parseInt(padAfterSlider.value, 10) || 0;
      lengthSlider.min      = String(songSecs + padB);
      lengthSlider.max      = String(songSecs + padB + 10);  // pad_after tops out at 10s
      lengthSlider.value    = String(songSecs + padB + padA);
      lengthVal.textContent = `${songSecs + padB + padA}s`;
      lengthHint.textContent = `From song: ${songSecs}s -- drag to add trailing padding (up to 10s).`;
    } else {
      lengthSlider.min = '5';
      lengthSlider.max = String(ACE_MAX_LEN);
      lengthHint.textContent = `No song yet -- AI writes one to fit this length (ACE-Step, max ${ACE_MAX_LEN}s).`;
    }
  }
  lengthSlider.addEventListener('input', () => {
    lengthVal.textContent = `${lengthSlider.value}s`;
    if (_songPath && _songDur > 0) {
      const songSecs = Math.round(_songDur);
      const padB = parseInt(padBeforeSlider.value, 10) || 0;
      const padA = Math.max(0, Math.min(10, Number(lengthSlider.value) - songSecs - padB));
      padAfterSlider.value = String(padA);
      padAfterSlider.dispatchEvent(new Event('input'));
      _syncLengthControl();
    }
  });
  padBeforeSlider.addEventListener('input', _syncLengthControl);
  padAfterSlider.addEventListener('input', _syncLengthControl);
  _syncLengthControl();

  // -- Batch controls -------------------------------------------------------------

  const batchStatus = el('div', { style: 'font-size:12px; color:var(--text-3); min-height:16px;' });
  const batchBtn = el('button', {
    text: 'Queue All',
    disabled: true,
    style: 'padding:12px; border-radius:var(--r-lg); border:none; cursor:not-allowed; font-size:15px; font-weight:700; background:var(--circus-red); color:var(--text); opacity:.45; width:100%;',
  });

  function _updateButtons() {
    // Song is optional -- routes.py writes one via ACE-Step when absent.
    const hasAll = _folderFiles.length > 0;
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
      batchStatus.textContent = `Running ${s.index}/${s.total}${lap}${cur}${clips}  --  ${s.succeeded} done, ${s.failed} failed`;
      if (s.folder && !_folderPath) {
        _folderPath = s.folder;
        folderNameEl.textContent = s.folder.split(/[\\/]/).pop() || s.folder;
        folderStatus.textContent = `${s.total} images`;
        _folderFiles = s.images || Array(s.total).fill({});
        _startPoll();
      }
    } else if (s.status === 'done') {
      batchStatus.textContent = `Done  --  ${s.succeeded} videos generated, ${s.failed} failed`;
      batchBtn.style.background = 'var(--circus-red)';
      _updateButtons();
    } else if (s.status === 'stopped') {
      batchStatus.textContent = `Stopped at ${s.index}/${s.total}  --  ${s.succeeded} done`;
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

    if (!_folderFiles.length) { toast('Choose a folder first', 'error'); return; }

    batchBtn.disabled = true;
    batchStatus.textContent = _songPath
      ? 'Analyzing song and starting batch...'
      : 'Writing a song with AI (ACE-Step), then starting batch -- can take a minute or two...';

    try {
      const body = {
        audio_path:    _songPath || '',
        target_length: parseInt(lengthSlider.value, 10),
        folder:        _folderPath,
        images:        _folderFiles.map(f => ({ path: f.path, name: f.name })),
        repeat:        loopCheck.checked,
        use_satellite: false,  // satellite hardware retired -- see project memory
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

  // -- Single-image generation --------------------------------------------------

  const DROP_HINT = 'Drop images here (optional -- one video queued per image)';

  const singleImages = { list: [] };   // [{ path, url, name }] -- one queued job each
  const singleDrop = el('div', {
    style: 'border:1px dashed var(--border-2); border-radius:var(--r-md); padding:16px; text-align:center; cursor:pointer; font-size:13px; color:var(--text-3); background:var(--surface-2); transition:border-color .12s, background .12s;',
    text: DROP_HINT,
  });
  const singleImgPreview = el('div', { style: 'display:none; flex-wrap:wrap; gap:4px; margin-top:6px; max-width:220px;' });
  const singleImgInput   = el('input', { type: 'file', accept: 'image/*' });
  singleImgInput.multiple = true;
  singleImgInput.style.display = 'none';
  panel.appendChild(singleImgInput);

  singleDrop.addEventListener('dragover', e => { e.preventDefault(); singleDrop.style.borderColor = 'var(--accent)'; singleDrop.style.background = 'var(--accent-bg)'; });
  singleDrop.addEventListener('dragleave', () => { singleDrop.style.borderColor = 'var(--border-2)'; singleDrop.style.background = 'var(--surface-2)'; });
  singleDrop.addEventListener('drop', e => {
    e.preventDefault(); singleDrop.style.borderColor = 'var(--border-2)'; singleDrop.style.background = 'var(--surface-2)';
    const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('image/'));
    if (files.length) _uploadSingleImages(files);
  });
  singleDrop.addEventListener('click', () => singleImgInput.click());
  singleImgInput.addEventListener('change', () => {
    const files = Array.from(singleImgInput.files);
    if (files.length) _uploadSingleImages(files);
    singleImgInput.value = '';
  });

  function _renderSingleImages() {
    singleImgPreview.textContent = '';
    const imgs = singleImages.list;
    if (!imgs.length) {
      singleImgPreview.style.display = 'none';
      singleDrop.textContent = DROP_HINT;
      _updateSingleBtn();
      return;
    }
    imgs.forEach((f, i) => {
      const thumb = el('img', {
        title: `${f.name} -- click to remove`,
        style: 'height:44px; width:44px; object-fit:cover; border-radius:4px; cursor:pointer;',
      });
      thumb.src = f.url;
      thumb.addEventListener('click', () => { singleImages.list.splice(i, 1); _renderSingleImages(); });
      singleImgPreview.appendChild(thumb);
    });
    singleImgPreview.style.display = 'flex';
    singleDrop.textContent = imgs.length === 1
      ? (imgs[0].name || '1 image ready')
      : `${imgs.length} images ready`;
    _updateSingleBtn();
  }

  async function _uploadSingleImages(files) {
    singleDrop.textContent = `Uploading ${files.length} image${files.length > 1 ? 's' : ''}...`;
    try {
      const resp = await apiUpload('/api/song-video/upload-image', files);
      const added = (resp?.files || []).filter(f => f?.path);
      if (!added.length) throw new Error('No usable images in that selection');
      singleImages.list.push(...added);
      _renderSingleImages();
    } catch (e) {
      toast('Image upload failed: ' + e.message, 'error');
      _renderSingleImages();
    }
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

  let _submitting = false;

  function _updateSingleBtn() {
    const n = singleImages.list.length;
    // Song is optional -- routes.py writes one via ACE-Step when absent.
    const ready = !_submitting;
    singleBtn.disabled = !ready;
    singleBtn.style.opacity = ready ? '1' : '.45';
    singleBtn.style.cursor  = ready ? 'pointer' : 'not-allowed';
    if (!_submitting) {
      singleBtn.textContent = n > 1 ? `Queue ${n} Videos` : 'Generate One Video';
    }
  }

  let _singleJobId = null;
  let _singlePoll  = null;

  singleBtn.onclick = async () => {
    // One job per starter image, same song. No image at all -> a single
    // job with no anchor, which is what the server does with photo_path ''.
    const shots = singleImages.list.length ? singleImages.list : [{ path: '' }];

    _submitting = true;
    _updateSingleBtn();
    singleProgress.style.display = 'block'; singleProgress.value = 5;
    singleResult.style.display = 'none';

    let queued = 0;
    let failure = null;

    for (const [i, shot] of shots.entries()) {
      const noSongMsg = 'Writing a song with AI (ACE-Step) -- can take a minute or two...';
      singleBtn.textContent = shots.length > 1 ? `Queueing ${i + 1}/${shots.length}...` : 'Submitting...';
      singleStatus.textContent = shots.length > 1
        ? `Queueing ${i + 1} of ${shots.length}: ${shot.name || 'no anchor image'}`
        : (_songPath ? 'Submitting...' : noSongMsg);

      const body = {
        audio_path:     _songPath || '',
        target_length:  parseInt(lengthSlider.value, 10),
        photo_path:     shot.path || '',
        video_prompt:   ideaInput.value.trim(),
        audio_analysis: _songAnalysis || undefined,
        model:          modelSel.value,
        clip_duration:  parseInt(clipDurSlider.value),
        steps:          8,
        guidance:       3.0,
        pad_before:     parseInt(padBeforeSlider.value),
        pad_after:      parseInt(padAfterSlider.value),
      };

      let resp;
      try {
        resp = await apiFetch('/api/song-video/generate', { method: 'POST', body: JSON.stringify(body) });
      } catch (e) {
        failure = e;
        break;   // queue full or server error -- stop, keep what we already queued
      }
      queued++;

      // Track the first job in-panel; the rest are visible in the rail job feed.
      if (!_singlePoll) {
        _singleJobId = resp.job_id;
        singleProgress.value = 10;
        _singlePoll = setInterval(_pollSingle, 2000);
      }
    }

    _submitting = false;
    _updateSingleBtn();

    if (failure) {
      const partial = queued ? ` (${queued} of ${shots.length} made it into the queue)` : '';
      singleStatus.textContent = `Error: ${failure.message}${partial}`;
      if (!queued) singleProgress.style.display = 'none';
      toast(`Queue failed after ${queued}: ${failure.message}`, 'error');
      return;
    }

    if (queued > 1) {
      singleStatus.textContent = `${queued} videos queued -- watch the job feed in the rail.`;
      toast(`${queued} videos queued from one song.`, 'success');
    } else {
      singleStatus.textContent = 'Generating...';
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
          singleResult.src = _outputPathToUrl(j.output);
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

  // -- On tab open: check for already-running batch (do NOT auto-start) ---------
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

  const legacySection = el('details', { style: 'margin-top:4px;' }, [
    el('summary', {
      style: 'cursor:pointer; font-size:12px; color:var(--text-3); user-select:none; padding:6px 0; outline:none;',
      text: 'Legacy pipeline -- older engine, not the ratified recipe',
    }),
    el('div', { style: 'display:flex; flex-direction:column; gap:14px; padding-top:10px;' }, [
      _card([
        LABEL('Song'),
        songDrop,
        analysisCard,
        lengthWrap,
      ]),
      _card([
        LABEL('Folder Batch'),
        el('div', { style: 'display:flex; gap:8px; align-items:center;' }, [folderNameEl, browseFolderBtn]),
        folderStatus,
        loopWrap,
        clipDurWrap,
        padBeforeWrap,
        padAfterWrap,
        batchStatus,
        batchBtn,
      ]),
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
      _card([LABEL('Video Model'), modelSel]),
    ]),
  ]);

  // ==========================================================================
  // Assemble layout
  // ==========================================================================

  // A style set directly on the shared `panel` element (rather than a child
  // wrapper, the pattern every other tab uses) is an inline style that
  // persists forever once set -- switchTab() in app.js only ever toggles the
  // .active CLASS to hide inactive panels (.tab-panel { display:none }), and
  // an inline style always wins over a class rule. Visiting Music Video once
  // left it permanently showing display:flex underneath/around whatever tab
  // was opened next, for the rest of the session.
  const root = el('div', { style: 'display:flex; flex-direction:column; gap:14px; padding:16px; overflow-y:auto; height:100%;' });
  panel.appendChild(root);

  root.append(
    el('div', { style: 'font-size:18px; font-weight:700; color:var(--gold); letter-spacing:-.01em;', text: 'Music Video' }),
    el('div', { style: 'font-size:12px; color:var(--text-3); margin-top:-8px;', text: 'Ratified Engine -- chain.py, recipe v3 (2026-08-05)' }),
    ratifiedSection,
    ratifiedImages,
    ratifiedOptions,
    ratifiedRun,
    legacySection,
  );

  _rUpdateStartBtn();
}

export function receiveHandoff(data) {
  // future: accept handoff from Create Videos or Express
}
