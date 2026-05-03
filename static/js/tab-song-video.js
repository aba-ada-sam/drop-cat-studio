/**
 * Drop Cat Go Studio — Music Video tab.
 * Drop a song → AI analyzes BPM/key/energy → generates a full-length music video.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js?v=20260414';
import { el, pathToUrl } from './components.js?v=20260429b';
import { toast, apiFetch } from './shell/toast.js?v=20260429d';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260428a';

export function receiveHandoff(data) {
  // no-op — this tab doesn't currently receive handoffs
}

export function init(panel) {
  panel.innerHTML = '';
  const root = el('div', {
    style: 'max-width:720px; margin:0 auto; padding:24px 16px; display:flex; flex-direction:column; gap:20px;',
  });
  panel.appendChild(root);

  // ── State ─────────────────────────────────────────────────────────────────
  let _audioPath     = null;
  let _audioUrl      = null;
  let _audioDuration = 0;
  let _audioAnalysis = null;
  let _imagePath     = null;
  let _model         = 'LTX-2 Dev19B Distilled';
  let _clipDur       = 8;
  let _numClips      = 0;     // auto-calculated
  let _qualityPx     = 580;
  let _outW          = 1032;
  let _outH          = 580;
  let _steps         = 20;
  let _guidance      = 7.5;
  let _jobId         = null;
  let _analyzeSeq    = 0;   // incremented on each new analysis; stale responses check against this

  const QUALITIES = [
    { label: '480P', px: 480, maxSec: 20 },
    { label: '580P', px: 580, maxSec: 20 },
    { label: '720P', px: 720, maxSec: 16 },
  ];

  function _computeDims(px) {
    // Always 16:9 for music video (most common)
    const h = px, w = Math.round(px * 16 / 9);
    const r32 = n => Math.max(32, Math.round(n / 32) * 32);
    return [r32(w), r32(h)];
  }
  [_outW, _outH] = _computeDims(_qualityPx);

  // ── Heading ───────────────────────────────────────────────────────────────
  root.appendChild(el('div', { style: 'text-align:center; padding-bottom:4px;' }, [
    el('div', { style: 'font-size:1.4rem; font-weight:700; color:var(--text); margin-bottom:6px;', text: 'Music Video' }),
    el('div', { style: 'font-size:.85rem; color:var(--text-3);', text: 'Drop a song. AI reads its energy and generates a full-length video to match.' }),
  ]));

  // ── Song drop zone ────────────────────────────────────────────────────────
  const audioInput    = el('input', { type: 'file', accept: 'audio/*,.mp3,.wav,.flac,.ogg,.m4a,.aac', style: 'display:none' });
  const audioHint     = el('div', { style: 'color:var(--text-3); font-size:.88rem;', text: 'Drop your song here or click to browse (mp3, wav, flac, m4a…)' });
  const audioPreview  = el('audio', { controls: '', style: 'display:none; width:100%; margin-top:8px;' });
  const audioClearBtn = el('button', {
    style: 'display:none; position:absolute; top:6px; right:6px; width:24px; height:24px; border-radius:50%; border:none; background:rgba(0,0,0,.65); color:#fff; font-size:15px; line-height:1; cursor:pointer; z-index:2; padding:0;',
    title: 'Remove song', text: '×',
  });
  const audioDropZone = el('div', { class: 'drop-zone', style: 'position:relative; min-height:80px; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:8px;' },
    [audioHint, audioPreview, audioClearBtn]);
  root.appendChild(audioDropZone);

  // Analysis result card (shown after upload + analysis)
  const analysisCard = el('div', { class: 'card', style: 'display:none; padding:12px 14px; flex-direction:column; gap:8px;' });
  root.appendChild(analysisCard);

  function _renderAnalysis(a) {
    analysisCard.innerHTML = '';
    const chips = [
      a.duration_display && { icon: '♪', text: a.duration_display },
      a.bpm              && { icon: '♩', text: `${a.bpm} BPM` },
      a.key              && { icon: '♯', text: `${a.key} ${a.mode || ''}`.trim() },
      a.mood             && { icon: '✦', text: a.mood.charAt(0).toUpperCase() + a.mood.slice(1) },
      a.energy           && { icon: '⚡', text: `${a.energy} energy` },
    ].filter(Boolean);

    const chipRow = el('div', { style: 'display:flex; gap:8px; flex-wrap:wrap; align-items:center;' });
    chips.forEach(c => {
      chipRow.appendChild(el('span', {
        style: 'display:inline-flex; align-items:center; gap:4px; padding:3px 10px; border-radius:20px; background:var(--bg-raised); border:1px solid var(--border-2); font-size:.78rem; color:var(--text-2);',
        text: `${c.icon} ${c.text}`,
      }));
    });

    if (!a.has_rich_analysis) {
      chipRow.appendChild(el('span', {
        style: 'font-size:.72rem; color:var(--text-3); font-style:italic;',
        text: '(install librosa for BPM/key/energy analysis)',
      }));
    }

    const clipsNote = el('div', {
      style: 'font-size:.78rem; color:var(--text-3);',
      text: `Suggested: ${a.suggested_num_clips} clips × ${a.suggested_clip_dur}s = ~${Math.round(a.suggested_num_clips * a.suggested_clip_dur)}s`,
    });

    analysisCard.appendChild(el('div', { style: 'font-size:.72rem; color:var(--text-3); text-transform:uppercase; letter-spacing:.05em; margin-bottom:2px;', text: 'Song analysis' }));
    analysisCard.appendChild(chipRow);
    analysisCard.appendChild(clipsNote);
    analysisCard.style.display = 'flex';

    // Apply suggestions to settings
    if (a.suggested_clip_dur) {
      _clipDur = a.suggested_clip_dur;
      clipSlider.value = String(_clipDur);
      clipLabel.textContent = `${_clipDur}s`;
    }
    _refreshClipCount();
  }

  async function _analyzeAudio(path) {
    const seq = ++_analyzeSeq;
    analysisCard.style.display = 'none';
    analysisCard.innerHTML = '';
    analysisCard.appendChild(el('div', { style: 'font-size:.8rem; color:var(--text-3);', text: 'Analyzing song…' }));
    analysisCard.style.display = 'flex';

    try {
      const result = await apiFetch('/api/song-video/analyze', {
        method: 'POST',
        body: JSON.stringify({ audio_path: path, clip_duration: _clipDur }),
      });
      if (seq !== _analyzeSeq) return;   // superseded by clear or a newer upload
      _audioAnalysis = result;
      _audioDuration = result.duration || _audioDuration;
      _renderAnalysis(result);
    } catch (e) {
      if (seq !== _analyzeSeq) return;
      analysisCard.innerHTML = '';
      analysisCard.appendChild(el('div', { style: 'font-size:.8rem; color:var(--text-3); font-style:italic;', text: `Analysis unavailable: ${e.message}` }));
      _refreshClipCount();
    }
  }

  function _applyAudio(path, url, duration) {
    _audioPath     = path;
    _audioUrl      = url;
    _audioDuration = duration || 0;
    _audioAnalysis = null;
    audioPreview.src = url;
    audioPreview.style.display = '';
    audioHint.style.display = 'none';
    audioClearBtn.style.display = '';
    audioDropZone.classList.add('drop-zone-loaded');
    _refreshClipCount();
    _analyzeAudio(path);
  }

  audioClearBtn.addEventListener('click', e => {
    e.stopPropagation();
    _analyzeSeq++;   // invalidate any in-flight analysis
    _audioPath = null; _audioUrl = null; _audioDuration = 0; _audioAnalysis = null;
    audioPreview.src = ''; audioPreview.style.display = 'none';
    audioHint.style.display = '';
    audioClearBtn.style.display = 'none';
    audioDropZone.classList.remove('drop-zone-loaded', 'drag-over');
    analysisCard.style.display = 'none';
    _refreshClipCount();
  });

  audioDropZone.addEventListener('click', e => {
    // Don't open file picker when clicking the audio player controls
    if (audioPreview.contains(e.target) || e.target === audioPreview) return;
    audioInput.click();
  });
  audioDropZone.addEventListener('dragover', e => { e.preventDefault(); audioDropZone.classList.add('drag-over'); });
  audioDropZone.addEventListener('dragleave', () => audioDropZone.classList.remove('drag-over'));
  audioDropZone.addEventListener('drop', async e => {
    e.preventDefault();
    audioDropZone.classList.remove('drag-over');
    const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('audio/') || /\.(mp3|wav|flac|ogg|m4a|aac|opus)$/i.test(f.name));
    if (!files.length) return;
    try {
      const data = await apiUpload('/api/song-video/upload-audio', files);
      const f = data.files?.[0];
      if (f) _applyAudio(f.path, f.url, f.duration);
    } catch (err) { toast(err.message, 'error'); }
  });
  audioInput.addEventListener('change', async () => {
    if (!audioInput.files?.length) return;
    try {
      const data = await apiUpload('/api/song-video/upload-audio', Array.from(audioInput.files));
      const f = data.files?.[0];
      if (f) _applyAudio(f.path, f.url, f.duration);
    } catch (err) { toast(err.message, 'error'); }
    audioInput.value = '';
  });
  root.appendChild(audioInput);

  // ── Anchor image (optional) ───────────────────────────────────────────────
  const imgInput    = el('input', { type: 'file', accept: 'image/*', style: 'display:none' });
  const imgPreview  = el('img', { style: 'display:none; max-height:140px; object-fit:contain; border-radius:6px;' });
  const imgHint     = el('div', { style: 'color:var(--text-3); font-size:.82rem; text-align:center;', text: 'Drop anchor image (optional — sets the visual style for all clips)' });
  const imgClearBtn = el('button', {
    style: 'display:none; position:absolute; top:4px; right:4px; width:22px; height:22px; border-radius:50%; border:none; background:rgba(0,0,0,.65); color:#fff; font-size:13px; cursor:pointer; z-index:2; padding:0; line-height:1;',
    text: '×',
  });
  const imgDropZone = el('div', { class: 'drop-zone', style: 'position:relative; min-height:70px; display:flex; align-items:center; justify-content:center;' },
    [imgPreview, imgHint, imgClearBtn]);
  root.appendChild(imgDropZone);
  root.appendChild(imgInput);

  function _applyImage(path, url) {
    _imagePath = path;
    imgPreview.src = url; imgPreview.style.display = '';
    imgHint.style.display = 'none';
    imgClearBtn.style.display = '';
    imgDropZone.classList.add('drop-zone-loaded');
  }
  imgClearBtn.addEventListener('click', e => {
    e.stopPropagation();
    _imagePath = null;
    imgPreview.src = ''; imgPreview.style.display = 'none';
    imgHint.style.display = '';
    imgClearBtn.style.display = 'none';
    imgDropZone.classList.remove('drop-zone-loaded');
  });
  imgDropZone.addEventListener('click', () => imgInput.click());
  imgDropZone.addEventListener('dragover', e => { e.preventDefault(); imgDropZone.classList.add('drag-over'); });
  imgDropZone.addEventListener('dragleave', () => imgDropZone.classList.remove('drag-over'));
  imgDropZone.addEventListener('drop', async e => {
    e.preventDefault(); imgDropZone.classList.remove('drag-over');
    const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('image/'));
    if (!files.length) return;
    try {
      const data = await apiUpload('/api/song-video/upload-image', files);
      const f = data.files?.[0];
      if (f) _applyImage(f.path, f.url);
    } catch (err) { toast(err.message, 'error'); }
  });
  imgInput.addEventListener('change', async () => {
    if (!imgInput.files?.length) return;
    try {
      const data = await apiUpload('/api/song-video/upload-image', Array.from(imgInput.files));
      const f = data.files?.[0];
      if (f) _applyImage(f.path, f.url);
    } catch (err) { toast(err.message, 'error'); }
    imgInput.value = '';
  });

  // ── Story idea ────────────────────────────────────────────────────────────
  const ideaInput = el('textarea', {
    rows: '3',
    style: 'width:100%; resize:vertical; font-size:.9rem;',
    placeholder: 'Describe the vibe, story, or visual style — or leave blank and AI will follow the song\'s energy.',
  });
  root.appendChild(el('div', { class: 'card', style: 'padding:12px 14px;' }, [
    el('div', { style: 'font-size:.75rem; color:var(--text-3); margin-bottom:6px; text-transform:uppercase; letter-spacing:.05em;', text: 'Story idea (optional)' }),
    ideaInput,
  ]));

  // ── Settings ──────────────────────────────────────────────────────────────
  const CHIP_BASE = 'border:1px solid var(--border-2); border-radius:6px; padding:4px 10px; font-size:.78rem; cursor:pointer; background:transparent; color:var(--text-2); transition:background .15s,color .15s;';
  const CHIP_ON   = 'background:var(--accent); border-color:var(--accent); color:#000; font-weight:600;';

  // Clip length
  const clipSlider = el('input', { type: 'range', min: '8', max: '20', value: '8', step: '1', style: 'flex:1; accent-color:var(--accent);' });
  const clipLabel  = el('span',  { style: 'font-size:.82rem; color:var(--accent); font-weight:600; min-width:28px; text-align:right;', text: '8s' });

  // Quality chips
  const qualChips = {};
  const qualRow = el('div', { style: 'display:flex; gap:6px; flex-wrap:wrap;' });
  for (const q of QUALITIES) {
    const btn = el('button', { style: CHIP_BASE + (q.px === _qualityPx ? CHIP_ON : ''), text: q.label });
    btn.addEventListener('click', () => {
      _qualityPx = q.px;
      [_outW, _outH] = _computeDims(_qualityPx);
      dimsLabel.textContent = `${_outW} × ${_outH}`;
      Object.entries(qualChips).forEach(([px, b]) =>
        b.setAttribute('style', CHIP_BASE + (Number(px) === _qualityPx ? CHIP_ON : '')));
    });
    qualChips[q.px] = btn;
    qualRow.appendChild(btn);
  }

  // Steps + Guidance
  const stepsSlider = el('input', { type: 'range', min: '4', max: '50', value: '20', step: '1', style: 'flex:1; accent-color:var(--accent);' });
  const stepsLabel  = el('span', { style: 'font-size:.82rem; color:var(--accent); font-weight:600; min-width:28px; text-align:right;', text: '20' });
  stepsSlider.addEventListener('input', () => { _steps = parseInt(stepsSlider.value); stepsLabel.textContent = String(_steps); });

  const guidSlider = el('input', { type: 'range', min: '1', max: '20', value: '7.5', step: '0.5', style: 'flex:1; accent-color:var(--accent);' });
  const guidLabel  = el('span', { style: 'font-size:.82rem; color:var(--accent); font-weight:600; min-width:28px; text-align:right;', text: '7.5' });
  guidSlider.addEventListener('input', () => { _guidance = parseFloat(guidSlider.value); guidLabel.textContent = String(_guidance); });

  const dimsLabel = el('span', { style: 'font-size:.82rem; color:var(--accent); font-weight:600;', text: `${_outW} × ${_outH}` });

  // Clip count summary + time estimate
  const clipSummary = el('div', { style: 'font-size:.8rem; color:var(--text-3);', text: 'Drop a song to calculate clip count.' });
  const timeWarn    = el('div', {
    style: 'display:none; font-size:.75rem; color:var(--accent-warm, #e8a000); background:rgba(232,160,0,.08); border:1px solid rgba(232,160,0,.25); border-radius:6px; padding:8px 12px; line-height:1.5;',
  });

  function _refreshClipCount() {
    if (!_audioDuration) {
      _numClips = 0;
      clipSummary.textContent = 'Drop a song to calculate clip count.';
      timeWarn.style.display = 'none';
      return;
    }
    _numClips = Math.max(1, Math.ceil(_audioDuration / _clipDur));
    const totalSec  = Math.round(_numClips * _clipDur);
    const dur = _audioDuration;
    const mins = Math.floor(dur / 60), secs = Math.round(dur % 60);
    const songDisplay = `${mins}:${String(secs).padStart(2, '0')}`;
    clipSummary.textContent = `${_numClips} clips × ${_clipDur}s = ~${totalSec}s  (song: ${songDisplay})`;

    const estMin = Math.round(_numClips * 3);   // ~3 min per clip
    if (estMin >= 20) {
      const hrs = Math.floor(estMin / 60), mins2 = estMin % 60;
      const timeStr = hrs > 0 ? `${hrs}h ${mins2}m` : `${estMin}m`;
      timeWarn.textContent = `⏱ Est. ~${timeStr} GPU time for ${_numClips} clips. WanGP must stay running the whole time. You can close this tab — the Queue tab shows progress.`;
      timeWarn.style.display = '';
    } else {
      timeWarn.style.display = 'none';
    }
  }

  clipSlider.addEventListener('input', () => {
    _clipDur = parseInt(clipSlider.value);
    clipLabel.textContent = `${_clipDur}s`;
    _refreshClipCount();
    // Re-analyze with new clip duration if we have a file
    if (_audioPath && _audioAnalysis) {
      // Recalculate clip count from existing analysis using new duration
      if (_audioAnalysis.duration) {
        _numClips = Math.max(1, Math.ceil(_audioAnalysis.duration / _clipDur));
      }
      // Update energy labels if analysis has profile
      if (_audioAnalysis.energy_profile?.length) {
        const n = _audioAnalysis.energy_profile.length;
        const newProfile = _resampleProfile(_audioAnalysis.energy_profile, Math.max(1, Math.ceil((_audioAnalysis.duration || 0) / _clipDur)));
        _audioAnalysis = { ..._audioAnalysis, energy_profile: newProfile, suggested_clip_dur: _clipDur, suggested_num_clips: _numClips };
      }
      _refreshClipCount();
    }
  });

  function _resampleProfile(profile, newLen) {
    if (!profile.length) return [];
    const result = [];
    for (let i = 0; i < newLen; i++) {
      const srcIdx = Math.min(profile.length - 1, Math.round(i * profile.length / newLen));
      result.push(profile[srcIdx]);
    }
    return result;
  }

  root.appendChild(el('div', { class: 'card', style: 'padding:12px 14px; display:flex; flex-direction:column; gap:10px;' }, [
    el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Clip length' }),
      clipSlider, clipLabel,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:10px; flex-wrap:wrap;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Quality' }),
      qualRow,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Steps' }),
      stepsSlider, stepsLabel,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Guidance' }),
      guidSlider, guidLabel,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:6px; padding-top:2px;' }, [
      el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'Output:' }),
      dimsLabel,
    ]),
    clipSummary,
    timeWarn,
  ]));

  // ── Create button ─────────────────────────────────────────────────────────
  const createBtn = el('button', {
    class: 'btn btn-primary btn-generate',
    text: 'Generate Music Video',
    style: 'font-size:1.1rem; padding:16px; font-weight:700; letter-spacing:.04em; width:100%;',
  });
  root.appendChild(createBtn);

  // ── Progress + result ─────────────────────────────────────────────────────
  const progressWrap = el('div', { style: 'display:none; flex-direction:column; gap:8px;' });
  const progressBar  = el('div', { style: 'height:4px; background:var(--border-2); border-radius:2px; overflow:hidden;' });
  const progressFill = el('div', { style: 'height:100%; width:0%; background:var(--accent); border-radius:2px; transition:width .4s;' });
  const progressMsg  = el('div', { style: 'font-size:.8rem; color:var(--text-3); text-align:center;' });
  const stopBtn      = el('button', { class: 'btn btn-sm', text: '■ Stop', style: 'align-self:center;' });
  progressBar.appendChild(progressFill);
  progressWrap.append(progressBar, progressMsg, stopBtn);
  root.appendChild(progressWrap);

  const resultWrap   = el('div', { style: 'display:none; flex-direction:column; gap:10px;' });
  const resultVideo  = el('video', { controls: '', style: 'width:100%; border-radius:8px; background:#000;' });
  const resultBtns   = el('div', { style: 'display:flex; gap:8px; justify-content:center; flex-wrap:wrap;' });
  const newBtn       = el('button', { class: 'btn btn-sm', text: '+ New video' });
  const muteBtn      = el('button', { class: 'btn btn-sm', text: '🔊 Mute' });
  muteBtn.addEventListener('click', () => {
    resultVideo.muted = !resultVideo.muted;
    muteBtn.textContent = resultVideo.muted ? '🔇 Unmute' : '🔊 Mute';
  });
  resultBtns.append(newBtn, muteBtn);
  resultWrap.append(resultVideo, resultBtns);
  root.appendChild(resultWrap);

  function _showProgress(pct, msg) {
    progressWrap.style.display = 'flex';
    resultWrap.style.display   = 'none';
    progressFill.style.width   = `${pct}%`;
    progressMsg.textContent    = msg || '';
  }
  function _showResult(videoPath) {
    progressWrap.style.display = 'none';
    resultWrap.style.display   = 'flex';
    resultVideo.src = pathToUrl(videoPath);
    resultVideo.load();
  }
  function _showError(msg) {
    progressFill.style.width      = '100%';
    progressFill.style.background = 'var(--red, #c41e3a)';
    progressMsg.style.color       = 'var(--red, #c41e3a)';
    progressMsg.textContent       = `Failed: ${msg}`;
    stopBtn.style.display         = 'none';
  }
  function _hideProgress() {
    progressWrap.style.display = 'none';
    progressFill.style.background = 'var(--accent)';
    progressMsg.style.color       = 'var(--text-3)';
  }

  newBtn.addEventListener('click', () => {
    resultWrap.style.display = 'none';
    resultVideo.src = '';
    createBtn.disabled = false;
  });

  stopBtn.addEventListener('click', () => {
    if (_jobId) {
      stopJob(_jobId).catch(() => {});
      toast('Stop requested — current clip will finish then generation halts', 'info');
    }
  });

  // ── Watcher ───────────────────────────────────────────────────────────────
  let _activePoller = null;

  function _watchJob(job_id) {
    if (_activePoller) { _activePoller.stop(); _activePoller = null; }
    _showProgress(2, 'Planning story arc…');
    stopBtn.style.display = '';

    return new Promise(resolve => {
      const poller = pollJob(job_id,
        j => {
          if (j.status === 'queued') {
            const pos   = j.queue_position;
            const label = pos === 0 ? 'up next' : `position ${pos + 1}`;
            _showProgress(2, `In queue — ${label}…`);
          } else {
            _showProgress(j.progress || 5, j.message || `${j.progress || 5}%`);
          }
        },
        j => {
          _activePoller = null;
          stopBtn.style.display = 'none';
          createBtn.disabled = false;
          const out = Array.isArray(j.output) ? j.output[0] : j.output;
          if (out) {
            _showResult(out);
            pushToGallery('song-video', out, ideaInput.value.trim() || 'Music video', -1, {});
          } else {
            _hideProgress();
          }
          resolve(true);
        },
        err => {
          _activePoller = null;
          stopBtn.style.display = 'none';
          createBtn.disabled = false;
          _showError(err);
          resolve(false);
        },
      );
      _activePoller = poller;
    });
  }

  // ── Submit ────────────────────────────────────────────────────────────────
  createBtn.addEventListener('click', async () => {
    if (!_audioPath) {
      toast('Drop a song file first', 'error');
      return;
    }
    if (_numClips <= 0) {
      toast('Song duration could not be determined — try re-uploading the file', 'error');
      return;
    }

    createBtn.disabled = true;

    try {
      const resp = await api('/api/song-video/generate', {
        method: 'POST',
        body: JSON.stringify({
          audio_path:     _audioPath,
          photo_path:     _imagePath || '',
          video_prompt:   ideaInput.value.trim(),
          user_direction: 'cinematic music video, visual energy matches song dynamics',
          audio_analysis: _audioAnalysis || undefined,
          model:          _model,
          clip_duration:  _clipDur,
          num_clips:      _numClips,
          steps:          _steps,
          guidance:       _guidance,
          output_width:   _outW,
          output_height:  _outH,
        }),
      });
      _jobId = resp.job_id;
      document.dispatchEvent(new CustomEvent('job-queued', { detail: { job_id: _jobId } }));
      _watchJob(_jobId);
    } catch (e) {
      createBtn.disabled = false;
      if (e.status === 429 || /queue.*full|full.*queue/i.test(e.message)) {
        toast('Queue is full — wait for the current job to finish', 'error');
      } else {
        toast(e.message || 'Submission failed', 'error');
      }
    }
  });
}
