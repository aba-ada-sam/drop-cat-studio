/**
 * Beat Sync tab -- align video motion peaks to audio beats.
 *
 * Workflow:
 *   1. Drop or paste paths for a generated video + audio file
 *   2. Hit Analyze -- server returns beat positions + video motion peaks
 *   3. See the waveform (audio) and motion markers (video) on a shared timeline
 *   4. Drag red markers to align video moments to beats, OR hit Auto-Align
 *   5. Hit Export -- server retimes the video and muxes the audio
 */

import { el }       from './components.js?v=20260508a';
import { apiFetch } from './shell/toast.js?v=20260518a';
import { toast }    from './shell/toast.js?v=20260518a';
import { pollJob }  from './api.js?v=20260505e';

// pixels per second on the timeline canvas
const PX_PER_SEC = 60;
const CANVAS_H_AUDIO = 80;
const CANVAS_H_VIDEO = 60;
const MARKER_R = 7;   // draggable marker radius

export function init(panel) {
  let _videoPath  = '';
  let _audioPath  = '';
  let _analysis   = null;   // {audio:{beat_times,energy_peaks,bpm,duration}, video:{motion_peaks,clip_boundaries,duration}}
  let _remapPts   = [];     // [{video_t, target_t}] -- user's manual alignments
  let _dragIdx    = -1;     // which motion peak is being dragged
  let _dragOffX   = 0;
  let _audioCtx   = null;
  let _audioBuf   = null;   // decoded AudioBuffer for waveform
  let _playPos    = 0;      // current playhead position in seconds
  let _rafId      = null;
  let _videoEl    = null;

  // ── helpers ─────────────────────────────────────────────────────────────────

  function _sToX(t, totalDur) {
    return Math.round(t / totalDur * canvasAudio.width);
  }
  function _xToS(x, totalDur) {
    return Math.max(0, Math.min(totalDur, x / canvasAudio.width * totalDur));
  }
  function _fmtTime(s) {
    const m = Math.floor(s / 60), sec = Math.floor(s % 60);
    return `${m}:${sec.toString().padStart(2, '0')}`;
  }

  // ── layout ───────────────────────────────────────────────────────────────────

  panel.style.cssText = 'display:flex; flex-direction:column; gap:12px; padding:16px; overflow-y:auto; height:100%; box-sizing:border-box;';

  panel.appendChild(el('div', {
    style: 'font-size:18px; font-weight:700; color:var(--gold); letter-spacing:-.01em;',
    text: 'Beat Sync',
  }));

  // ── file pickers ─────────────────────────────────────────────────────────────

  function _filePicker(label, placeholder, onSet) {
    const inp = el('input', {
      type: 'text',
      placeholder,
      style: 'flex:1; background:var(--surface-2); border:1px solid var(--border-2); border-radius:var(--r-sm); padding:7px 10px; color:var(--text); font-size:13px; outline:none;',
    });
    inp.addEventListener('change', () => onSet(inp.value.trim()));
    const fileInput = el('input', { type: 'file' });
    fileInput.style.display = 'none';
    fileInput.addEventListener('change', () => {
      if (fileInput.files[0]) {
        // For local files, the path isn't accessible via JS -- the user must
        // use the session picker or paste the path from the output folder.
        toast('Paste the file path directly, or use a file from the session', 'info');
      }
    });
    const row = el('div', { style: 'display:flex; gap:6px; align-items:center;' }, [
      el('span', { style: 'font-size:12px; color:var(--text-3); min-width:44px;', text: label }),
      inp,
    ]);
    return { row, inp, set: v => { inp.value = v; onSet(v); } };
  }

  const videoPicker = _filePicker('Video', 'Paste full path to generated video (.mp4)', v => { _videoPath = v; _checkReady(); });
  const audioPicker = _filePicker('Audio', 'Paste full path to audio file (.mp3 / .wav)', v => { _audioPath = v; _checkReady(); });

  const analyzeBtn = el('button', {
    text: 'Analyze',
    disabled: true,
    style: 'padding:9px 20px; border-radius:var(--r-md); border:none; font-size:13px; font-weight:700; background:var(--accent); color:#000; cursor:not-allowed; opacity:.45; align-self:flex-start;',
  });

  function _checkReady() {
    const ok = _videoPath.length > 0 && _audioPath.length > 0;
    analyzeBtn.disabled = !ok;
    analyzeBtn.style.opacity  = ok ? '1' : '.45';
    analyzeBtn.style.cursor   = ok ? 'pointer' : 'not-allowed';
  }

  panel.appendChild(el('div', {
    style: 'background:var(--surface-1); border:1px solid var(--border-1); border-radius:var(--r-lg); padding:14px; display:flex; flex-direction:column; gap:8px;',
  }, [
    videoPicker.row,
    audioPicker.row,
    analyzeBtn,
  ]));

  // ── timeline container ───────────────────────────────────────────────────────

  const timelineWrap = el('div', {
    style: 'display:none; flex-direction:column; gap:0; border:1px solid var(--border-1); border-radius:var(--r-lg); overflow:hidden; background:#0a0404; position:relative;',
  });

  const canvasAudio = el('canvas', { style: 'display:block; width:100%; cursor:crosshair;' });
  canvasAudio.height = CANVAS_H_AUDIO;

  const canvasVideo = el('canvas', { style: 'display:block; width:100%; cursor:crosshair;' });
  canvasVideo.height = CANVAS_H_VIDEO;

  // Ruler / time labels
  const ruler = el('canvas', { style: 'display:block; width:100%;' });
  ruler.height = 18;

  // Labels strip between canvases
  const labelStrip = el('div', {
    style: 'background:#111; border-top:1px solid #222; border-bottom:1px solid #222; padding:2px 8px; display:flex; gap:16px; align-items:center;',
  });
  const lblBpm  = el('span', { style: 'font-size:10px; color:#888;', text: 'BPM: --' });
  const lblDur  = el('span', { style: 'font-size:10px; color:#888;', text: 'Duration: --' });
  const lblBeats= el('span', { style: 'font-size:10px; color:#e87c2a;', text: 'Beats: --' });
  const lblPeaks= el('span', { style: 'font-size:10px; color:#4a9eff;', text: 'Energy peaks: --' });
  const lblMov  = el('span', { style: 'font-size:10px; color:#e84a4a;', text: 'Motion peaks: --' });
  labelStrip.append(lblBpm, lblDur, lblBeats, lblPeaks, lblMov);

  timelineWrap.append(canvasAudio, labelStrip, canvasVideo, ruler);
  panel.appendChild(timelineWrap);

  // Playback / preview row
  const previewWrap = el('div', { style: 'display:none; gap:10px; align-items:flex-start;' });
  _videoEl = el('video', {
    style: 'width:260px; height:146px; border-radius:6px; background:#000; flex:0 0 auto;',
  });
  _videoEl.controls = true;
  _videoEl.muted = true;
  const playheadInfo = el('div', { style: 'font-size:12px; color:var(--text-3); padding-top:4px;', text: '0:00 / 0:00' });
  previewWrap.append(_videoEl, playheadInfo);
  panel.appendChild(previewWrap);

  // ── action bar ───────────────────────────────────────────────────────────────

  const autoAlignBtn = el('button', {
    text: 'Auto-Align Peaks to Beats',
    disabled: true,
    style: 'padding:9px 18px; border-radius:var(--r-md); border:1px solid var(--accent-border); background:var(--accent-bg); color:var(--accent); font-size:13px; font-weight:600; cursor:not-allowed; opacity:.45;',
  });
  const resetBtn = el('button', {
    text: 'Reset Alignment',
    style: 'padding:9px 14px; border-radius:var(--r-md); border:1px solid var(--border-2); background:none; color:var(--text-3); font-size:13px; cursor:pointer;',
  });
  const exportBtn = el('button', {
    text: 'Export Synced Video',
    disabled: true,
    style: 'padding:9px 18px; border-radius:var(--r-md); border:none; background:var(--circus-red); color:#fff; font-size:13px; font-weight:700; cursor:not-allowed; opacity:.45;',
  });
  const exportStatus = el('div', { style: 'font-size:12px; color:var(--text-3); min-height:16px;' });
  const exportProgress = el('progress', { style: 'display:none; width:100%; height:4px;' });
  exportProgress.max = 100;

  panel.appendChild(el('div', {
    style: 'display:flex; gap:8px; flex-wrap:wrap; align-items:center;',
  }, [autoAlignBtn, resetBtn, exportBtn]));
  panel.appendChild(exportStatus);
  panel.appendChild(exportProgress);

  function _setActionState(hasAnalysis) {
    autoAlignBtn.disabled = !hasAnalysis;
    autoAlignBtn.style.opacity = hasAnalysis ? '1' : '.45';
    autoAlignBtn.style.cursor  = hasAnalysis ? 'pointer' : 'not-allowed';
    exportBtn.disabled = !hasAnalysis;
    exportBtn.style.opacity = hasAnalysis ? '1' : '.45';
    exportBtn.style.cursor  = hasAnalysis ? 'pointer' : 'not-allowed';
  }

  // ── analyze ──────────────────────────────────────────────────────────────────

  analyzeBtn.addEventListener('click', async () => {
    analyzeBtn.disabled = true;
    analyzeBtn.textContent = 'Analyzing...';
    try {
      const r = await apiFetch('/api/sync/analyze', {
        method: 'POST',
        body: JSON.stringify({ video_path: _videoPath, audio_path: _audioPath }),
      });
      _analysis = r;
      _remapPts = [];
      _playPos  = 0;
      _renderTimeline();
      await _loadWaveform();
      _setActionState(true);
      timelineWrap.style.display = 'flex';
      previewWrap.style.display  = 'flex';
      if (_videoEl) _videoEl.src = 'file:///' + _videoPath.replace(/\\/g, '/');
      const a = _analysis.audio || {};
      const v = _analysis.video || {};
      lblBpm.textContent   = 'BPM: ' + (a.bpm ? Math.round(a.bpm) : '--');
      lblDur.textContent   = 'Duration: ' + _fmtTime((a.duration || v.duration || 0));
      lblBeats.textContent  = 'Beats: ' + (a.beat_times?.length || 0);
      lblPeaks.textContent  = 'Energy peaks: ' + (a.energy_peaks?.length || 0);
      lblMov.textContent    = 'Motion peaks: ' + (v.motion_peaks?.length || 0);
    } catch (e) {
      toast('Analyze failed: ' + e.message, 'error');
    } finally {
      analyzeBtn.disabled = false;
      analyzeBtn.textContent = 'Re-analyze';
    }
  });

  // ── waveform loading ─────────────────────────────────────────────────────────

  async function _loadWaveform() {
    try {
      const resp = await fetch('/api/sync/serve?path=' + encodeURIComponent(_audioPath));
      if (!resp.ok) return;
      const buf = await resp.arrayBuffer();
      if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      _audioBuf = await _audioCtx.decodeAudioData(buf);
      _drawAudioCanvas();
    } catch (e) {
      log.warn('[beat-sync] Waveform load failed:', e);
    }
  }

  // ── canvas sizing ────────────────────────────────────────────────────────────

  function _resizeCanvases() {
    const w = timelineWrap.clientWidth || 800;
    [canvasAudio, canvasVideo, ruler].forEach(c => {
      if (c.width !== w) c.width = w;
    });
  }

  // ── draw audio canvas ────────────────────────────────────────────────────────

  function _drawAudioCanvas() {
    _resizeCanvases();
    const ctx = canvasAudio.getContext('2d');
    const W = canvasAudio.width, H = CANVAS_H_AUDIO;
    const dur = (_analysis?.audio?.duration || _analysis?.video?.duration || 1);
    ctx.clearRect(0, 0, W, H);

    // Waveform background
    if (_audioBuf) {
      const data  = _audioBuf.getChannelData(0);
      const step  = Math.ceil(data.length / W);
      const cy    = H / 2;
      ctx.strokeStyle = '#3a1a1a';
      ctx.lineWidth   = 1;
      for (let x = 0; x < W; x++) {
        let mn = 1, mx = -1;
        for (let i = x * step; i < (x + 1) * step && i < data.length; i++) {
          if (data[i] < mn) mn = data[i];
          if (data[i] > mx) mx = data[i];
        }
        ctx.beginPath();
        ctx.moveTo(x, cy + mn * cy * 0.9);
        ctx.lineTo(x, cy + mx * cy * 0.9);
        ctx.stroke();
      }
      // Brighter waveform on top
      ctx.strokeStyle = '#7a3030';
      for (let x = 0; x < W; x++) {
        let mn = 1, mx = -1;
        for (let i = x * step; i < (x + 1) * step && i < data.length; i++) {
          if (data[i] < mn) mn = data[i];
          if (data[i] > mx) mx = data[i];
        }
        ctx.beginPath();
        ctx.moveTo(x, cy + mn * cy * 0.7);
        ctx.lineTo(x, cy + mx * cy * 0.7);
        ctx.stroke();
      }
    }

    const a = _analysis?.audio || {};

    // Beat lines (orange, subtle)
    ctx.strokeStyle = 'rgba(232,124,42,0.3)';
    ctx.lineWidth = 1;
    for (const t of (a.beat_times || [])) {
      const x = _sToX(t, dur);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    }

    // Energy peaks (bright orange)
    ctx.strokeStyle = '#e87c2a';
    ctx.lineWidth = 2;
    for (const t of (a.energy_peaks || [])) {
      const x = _sToX(t, dur);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    }

    // Playhead
    const px = _sToX(_playPos, dur);
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke();
  }

  // ── draw video canvas ────────────────────────────────────────────────────────

  function _drawVideoCanvas() {
    _resizeCanvases();
    const ctx = canvasVideo.getContext('2d');
    const W = canvasVideo.width, H = CANVAS_H_VIDEO;
    const dur = (_analysis?.audio?.duration || _analysis?.video?.duration || 1);
    ctx.clearRect(0, 0, W, H);

    // Dark background
    ctx.fillStyle = '#050202';
    ctx.fillRect(0, 0, W, H);

    const v = _analysis?.video || {};

    // Clip boundaries (white lines)
    ctx.strokeStyle = 'rgba(255,255,255,0.4)';
    ctx.lineWidth = 1;
    for (const t of (v.clip_boundaries || [])) {
      if (t < 0.1) continue;
      const x = _sToX(t, dur);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    }

    // Motion peaks -- use remap_pts positions if they exist, else original
    const peaks = v.motion_peaks || [];
    for (let i = 0; i < peaks.length; i++) {
      const remapped = _remapPts.find(r => r._idx === i);
      const t = remapped ? remapped.target_t : peaks[i];
      const origT = peaks[i];
      const x = _sToX(t, dur);
      const ox = _sToX(origT, dur);

      // Line from original to remapped position
      if (remapped && Math.abs(x - ox) > 2) {
        ctx.strokeStyle = 'rgba(232,74,74,0.4)';
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(ox, H / 2); ctx.lineTo(x, H / 2); ctx.stroke();
        ctx.setLineDash([]);
      }

      // Marker diamond
      const cy = H / 2;
      ctx.fillStyle = remapped ? '#ffaa00' : '#e84a4a';
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(x,            cy - MARKER_R);
      ctx.lineTo(x + MARKER_R, cy);
      ctx.lineTo(x,            cy + MARKER_R);
      ctx.lineTo(x - MARKER_R, cy);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
    }

    // Playhead
    const px = _sToX(_playPos, dur);
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke();
  }

  // ── draw ruler ───────────────────────────────────────────────────────────────

  function _drawRuler() {
    _resizeCanvases();
    const ctx = ruler.getContext('2d');
    const W = ruler.width, H = 18;
    const dur = (_analysis?.audio?.duration || _analysis?.video?.duration || 1);
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#0d0606';
    ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = '#555';
    ctx.font = '9px monospace';
    ctx.textBaseline = 'top';
    const step = dur > 120 ? 30 : dur > 60 ? 15 : dur > 30 ? 10 : 5;
    for (let t = 0; t <= dur; t += step) {
      const x = _sToX(t, dur);
      ctx.fillStyle = '#444';
      ctx.fillRect(x, 0, 1, 6);
      ctx.fillStyle = '#666';
      ctx.fillText(_fmtTime(t), x + 2, 6);
    }
  }

  function _renderTimeline() {
    _drawAudioCanvas();
    _drawVideoCanvas();
    _drawRuler();
  }

  // ── drag interaction on video canvas ─────────────────────────────────────────

  function _peakAtX(x) {
    const dur = (_analysis?.audio?.duration || _analysis?.video?.duration || 1);
    const peaks = _analysis?.video?.motion_peaks || [];
    for (let i = peaks.length - 1; i >= 0; i--) {
      const remapped = _remapPts.find(r => r._idx === i);
      const t = remapped ? remapped.target_t : peaks[i];
      const px = _sToX(t, dur);
      if (Math.abs(x - px) <= MARKER_R + 3) return i;
    }
    return -1;
  }

  canvasVideo.addEventListener('mousedown', e => {
    const rect = canvasVideo.getBoundingClientRect();
    const x = (e.clientX - rect.left) * (canvasVideo.width / rect.width);
    _dragIdx = _peakAtX(x);
    if (_dragIdx >= 0) {
      _dragOffX = x;
      e.preventDefault();
    }
  });

  canvasVideo.addEventListener('mousemove', e => {
    const rect = canvasVideo.getBoundingClientRect();
    const x = (e.clientX - rect.left) * (canvasVideo.width / rect.width);
    canvasVideo.style.cursor = _peakAtX(x) >= 0 ? 'ew-resize' : 'crosshair';
    if (_dragIdx < 0) return;
    const dur = (_analysis?.audio?.duration || _analysis?.video?.duration || 1);
    const newT = _xToS(x, dur);
    const existing = _remapPts.findIndex(r => r._idx === _dragIdx);
    const origT = (_analysis?.video?.motion_peaks || [])[_dragIdx];
    if (existing >= 0) {
      _remapPts[existing].target_t = newT;
    } else {
      _remapPts.push({ _idx: _dragIdx, video_t: origT, target_t: newT });
    }
    _drawVideoCanvas();
  });

  window.addEventListener('mouseup', () => { _dragIdx = -1; });

  // Seek on click (not drag) for both canvases
  function _seekAt(canvas, e) {
    if (_dragIdx >= 0) return;
    const rect = canvas.getBoundingClientRect();
    const x = (e.clientX - rect.left) * (canvas.width / rect.width);
    const dur = (_analysis?.audio?.duration || _analysis?.video?.duration || 1);
    _playPos = _xToS(x, dur);
    if (_videoEl) _videoEl.currentTime = _playPos;
    _renderTimeline();
    playheadInfo.textContent = _fmtTime(_playPos) + ' / ' + _fmtTime(dur);
  }
  canvasAudio.addEventListener('click', e => _seekAt(canvasAudio, e));
  canvasVideo.addEventListener('click', e => _seekAt(canvasVideo, e));

  // Sync playhead to video element time
  if (_videoEl) {
    _videoEl.addEventListener('timeupdate', () => {
      _playPos = _videoEl.currentTime;
      _renderTimeline();
      const dur = (_analysis?.audio?.duration || _analysis?.video?.duration || 1);
      playheadInfo.textContent = _fmtTime(_playPos) + ' / ' + _fmtTime(dur);
    });
  }

  // ── auto-align ───────────────────────────────────────────────────────────────

  autoAlignBtn.addEventListener('click', () => {
    if (!_analysis) return;
    const a = _analysis.audio || {};
    const v = _analysis.video || {};
    const peaks  = v.motion_peaks || [];
    const beats  = a.beat_times   || [];
    const energy = a.energy_peaks || [];
    // Candidates = energy peaks + every 4th beat (downbeats)
    const candidates = [...new Set([...energy, ...beats.filter((_, i) => i % 4 === 0)])].sort((a, b) => a - b);
    const dur = a.duration || v.duration || 1;
    const WINDOW = 1.5; // snap window in seconds
    const used = new Set();
    _remapPts = [];
    for (let i = 0; i < peaks.length; i++) {
      const origT = peaks[i];
      let bestC = null, bestDist = Infinity;
      for (const c of candidates) {
        const d = Math.abs(c - origT);
        if (d < bestDist && d <= WINDOW && !used.has(c) && c > 0 && c < dur) {
          bestDist = d; bestC = c;
        }
      }
      if (bestC !== null) {
        _remapPts.push({ _idx: i, video_t: origT, target_t: bestC });
        used.add(bestC);
      }
    }
    toast(`Auto-aligned ${_remapPts.length} of ${peaks.length} motion peaks to beats`, 'success');
    _drawVideoCanvas();
  });

  resetBtn.addEventListener('click', () => {
    _remapPts = [];
    if (_analysis) _drawVideoCanvas();
  });

  // ── export ───────────────────────────────────────────────────────────────────

  exportBtn.addEventListener('click', async () => {
    if (!_analysis) return;
    exportBtn.disabled = true;
    exportStatus.textContent = 'Submitting retime job...';
    exportProgress.style.display = 'block';
    exportProgress.value = 5;

    // Strip internal _idx from remap_pts before sending
    const remap = _remapPts.length > 0
      ? _remapPts.map(({ video_t, target_t }) => ({ video_t, target_t }))
      : null;   // null = auto-align server-side

    try {
      const r = await apiFetch('/api/sync/retime', {
        method: 'POST',
        body: JSON.stringify({
          video_path:   _videoPath,
          audio_path:   _audioPath,
          remap_points: remap,
        }),
      });
      if (r.error) throw new Error(r.error);

      pollJob(r.job_id, {
        onProgress: (pct, msg) => {
          exportProgress.value = pct;
          exportStatus.textContent = msg || 'Retiming...';
        },
        onDone: (output) => {
          exportProgress.style.display = 'none';
          exportStatus.textContent = 'Done: ' + (output || '');
          exportBtn.disabled = false;
          toast('Synced video saved', 'success');
          document.dispatchEvent(new Event('session-updated'));
        },
        onError: (err) => {
          exportProgress.style.display = 'none';
          exportStatus.textContent = 'Export failed: ' + err;
          exportBtn.disabled = false;
        },
      });
    } catch (e) {
      exportProgress.style.display = 'none';
      exportStatus.textContent = 'Error: ' + e.message;
      exportBtn.disabled = false;
    }
  });

  // ── resize handler ───────────────────────────────────────────────────────────

  const ro = new ResizeObserver(() => { if (_analysis) _renderTimeline(); });
  ro.observe(timelineWrap);

  // Legend
  panel.appendChild(el('div', {
    style: 'font-size:11px; color:var(--text-3); line-height:1.8;',
    text: 'Orange lines = audio beats.  Blue lines = energy peaks.  White lines = video clip cuts.  Red diamonds = video motion peaks (drag to align).  Yellow = manually aligned.',
  }));

  // ── session file population (populate from most recent session files) ────────

  try {
    apiFetch('/api/session/files').then(files => {
      if (!files || !files.length) return;
      const videos = files.filter(f => f.type === 'video' && f.path);
      const audios  = files.filter(f => f.type === 'audio' && f.path);
      if (videos.length && !_videoPath) videoPicker.set(videos[videos.length - 1].path);
      if (audios.length  && !_audioPath) audioPicker.set(audios[audios.length - 1].path);
    }).catch(() => {});
  } catch (_) {}
}

export function receiveHandoff(data) {
  // Allow other tabs to hand off video + audio paths directly
}
