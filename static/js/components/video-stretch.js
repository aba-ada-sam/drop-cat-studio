/**
 * Video Stretch & Lock tool.
 *
 * Manual video retiming. The AUDIO is the fixed reference (drawn as a waveform);
 * the user pins video frames ("locks") and drags where each should land against
 * that audio, stretching/condensing the VIDEO to fit. Exports a retimed copy via
 * POST /api/retime. Fully manual -- NO automatic beat detection (that was the
 * removed beat-sync feature). The audio track is never altered.
 *
 * Usage:
 *   const tool = new VideoStretchTool(containerEl, {
 *     videoUrl,        // served URL for <video>/decode (e.g. /output/x.mp4)
 *     videoPath,       // absolute path sent to the API
 *     videoEl,         // optional existing <video> to sync the playhead to
 *     onApplied,       // optional callback(outputPath) after export completes
 *   });
 */
import { el, pathToUrl } from '../components.js?v=20260507a';
import { apiFetch, toast } from '../shell/toast.js?v=20260518a';

const MARK_R = 6;

export class VideoStretchTool {
  constructor(containerEl, opts = {}) {
    this._el = containerEl;
    this._opts = opts;
    this._videoUrl = pathToUrl(opts.videoUrl || opts.videoPath || '');
    this._videoPath = opts.videoPath || '';
    this._video = opts.videoEl || null;
    this._dur = (this._video && this._video.duration) || 0;
    this._wave = null;          // Float32Array of audio samples, or null
    this._anchors = [];         // [{src_t, dst_t}] -- src = video frame, dst = where it lands
    this._dragIdx = -1;
    this._raf = null;
    this._init();
  }

  _init() {
    this._render();
    this._loadAudio();
    if (this._video) {
      this._video.addEventListener('loadedmetadata', () => { this._dur = this._video.duration; this._draw(); });
      this._video.addEventListener('timeupdate', () => this._draw());
      this._video.addEventListener('play',  () => this._startRaf());
      this._video.addEventListener('pause', () => this._stopRaf());
      if (this._video.readyState >= 1) { this._dur = this._video.duration; }
    }
  }

  _render() {
    this._el.innerHTML = '';
    const title = el('div', { style: 'font-size:13px; font-weight:700; color:var(--gold); margin-bottom:4px;', text: 'Stretch & Lock' });
    const hint  = el('div', { style: 'font-size:11px; color:var(--text-3); margin-bottom:6px;',
      text: 'Audio stays fixed (waveform below). Scrub the video to a moment, click "Add Lock", then drag the marker to where that moment should land against the audio. The video stretches/condenses to fit. Double-click a marker to remove it.' });

    this._canvas = el('canvas', { style: 'display:block; width:100%; border-radius:4px; cursor:crosshair; background:#0a0202;' });
    this._canvas.height = 96;
    this._canvas.addEventListener('mousedown', e => this._onDown(e));
    this._canvas.addEventListener('mousemove', e => this._onMove(e));
    this._canvas.addEventListener('dblclick',  e => this._onDblClick(e));
    window.addEventListener('mouseup', () => { this._dragIdx = -1; });

    this._info = el('div', { style: 'font-size:11px; color:var(--text-3); min-height:14px; margin-top:4px;' });

    const addBtn   = el('button', { class: 'btn btn-sm', text: 'Add Lock at Playhead' });
    addBtn.addEventListener('click', () => this._addLock());
    const resetBtn = el('button', { class: 'btn btn-sm', style: 'background:none; border:1px solid var(--border-2); color:var(--text-3);', text: 'Reset' });
    resetBtn.addEventListener('click', () => { this._anchors = []; this._draw(); this._updateInfo(); });
    this._exportBtn = el('button', { class: 'btn btn-sm', style: 'background:var(--circus-red); color:#fff; font-weight:700;', text: 'Export Retimed Video' });
    this._exportBtn.addEventListener('click', () => this._export());

    const btnRow = el('div', { style: 'display:flex; gap:8px; flex-wrap:wrap; margin-top:8px;' }, [addBtn, resetBtn, this._exportBtn]);

    this._status = el('div', { style: 'font-size:11px; color:var(--text-3); min-height:14px; margin-top:4px;' });
    this._bar = el('div', { style: 'height:3px; background:var(--accent); width:0%; border-radius:2px; transition:width .3s;' });
    this._barWrap = el('div', { style: 'display:none; height:3px; background:var(--border-2); border-radius:2px; overflow:hidden; margin-top:4px;' }, [this._bar]);

    this._el.append(title, hint, this._canvas, this._info, btnRow, this._barWrap, this._status);
    this._updateInfo();
    requestAnimationFrame(() => this._draw());
    new ResizeObserver(() => this._draw()).observe(this._canvas);
  }

  async _loadAudio() {
    if (!this._videoUrl) return;
    try {
      const resp = await fetch(this._videoUrl);
      if (!resp.ok) return;
      const buf = await resp.arrayBuffer();
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const audio = await ctx.decodeAudioData(buf);
      if (!this._dur) this._dur = audio.duration;
      this._wave = audio.getChannelData(0);
      ctx.close();
      this._draw();
    } catch (_) {
      // Decode can fail for some codecs/containers -- fall back to a plain ruler.
      this._wave = null;
      this._draw();
    }
  }

  _startRaf() {
    const tick = () => { this._draw(); this._raf = requestAnimationFrame(tick); };
    if (!this._raf) this._raf = requestAnimationFrame(tick);
  }
  _stopRaf() { if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; } }

  _sToX(t, W) { const d = this._dur || 1; return Math.round(t / d * W); }
  _xToS(x, W) { const d = this._dur || 1; return Math.max(0, Math.min(d, x / W * d)); }

  _draw() {
    const c = this._canvas;
    const W = c.clientWidth || 600;
    if (c.width !== W) c.width = W;
    const H = c.height;
    const ctx = c.getContext('2d');
    ctx.clearRect(0, 0, W, H);

    // Audio waveform (top ~64px) or flat baseline
    const wfH = 64, cy = wfH / 2;
    ctx.strokeStyle = '#5a2020'; ctx.lineWidth = 1;
    if (this._wave && this._wave.length) {
      const step = Math.ceil(this._wave.length / W);
      for (let x = 0; x < W; x++) {
        let mn = 1, mx = -1;
        for (let i = x * step; i < (x + 1) * step && i < this._wave.length; i++) {
          const v = this._wave[i]; if (v < mn) mn = v; if (v > mx) mx = v;
        }
        ctx.beginPath(); ctx.moveTo(x, cy + mn * cy * 0.9); ctx.lineTo(x, cy + mx * cy * 0.9); ctx.stroke();
      }
    } else {
      ctx.beginPath(); ctx.moveTo(0, cy); ctx.lineTo(W, cy); ctx.stroke();
    }

    // Time ruler
    ctx.fillStyle = '#7a5a3a'; ctx.font = '9px sans-serif';
    const d = this._dur || 0;
    const tickEvery = d > 60 ? 15 : (d > 20 ? 5 : 2);
    for (let t = 0; t <= d; t += tickEvery) {
      const x = this._sToX(t, W);
      ctx.strokeStyle = 'rgba(255,255,255,.08)'; ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, wfH); ctx.stroke();
      ctx.fillText(`${t}s`, Math.min(W - 18, x + 2), wfH + 11);
    }

    // Playhead
    if (this._video) {
      const px = this._sToX(this._video.currentTime || 0, W);
      ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, wfH); ctx.stroke();
    }

    // Lock markers (diamonds on the marker row), with src->dst connector
    const my = wfH + 22;
    for (let i = 0; i < this._anchors.length; i++) {
      const a = this._anchors[i];
      const xs = this._sToX(a.src_t, W), xd = this._sToX(a.dst_t, W);
      if (Math.abs(xs - xd) > 2) {
        ctx.strokeStyle = 'rgba(232,124,42,.5)'; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(xs, my); ctx.lineTo(xd, my); ctx.stroke(); ctx.setLineDash([]);
      }
      ctx.fillStyle = '#ffaa00'; ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(xd, my - MARK_R); ctx.lineTo(xd + MARK_R, my);
      ctx.lineTo(xd, my + MARK_R); ctx.lineTo(xd - MARK_R, my); ctx.closePath();
      ctx.fill(); ctx.stroke();
    }
  }

  _markerAtX(x, W) {
    for (let i = this._anchors.length - 1; i >= 0; i--) {
      if (Math.abs(this._sToX(this._anchors[i].dst_t, W) - x) <= MARK_R + 3) return i;
    }
    return -1;
  }

  _onDown(e) {
    const r = this._canvas.getBoundingClientRect();
    const W = this._canvas.width;
    const x = (e.clientX - r.left) * (W / r.width);
    const y = (e.clientY - r.top) * (this._canvas.height / r.height);
    if (y > 70) { this._dragIdx = this._markerAtX(x, W); }      // marker row
    else if (this._video) { this._video.currentTime = this._xToS(x, W); this._draw(); }  // scrub
  }

  _onMove(e) {
    const r = this._canvas.getBoundingClientRect();
    const W = this._canvas.width;
    const x = (e.clientX - r.left) * (W / r.width);
    this._canvas.style.cursor = this._markerAtX(x, W) >= 0 ? 'ew-resize' : 'crosshair';
    if (this._dragIdx < 0) return;
    const lo = this._dragIdx > 0 ? this._anchors[this._dragIdx - 1].dst_t + 0.1 : 0;
    const hi = this._dragIdx < this._anchors.length - 1 ? this._anchors[this._dragIdx + 1].dst_t - 0.1 : (this._dur || 1);
    this._anchors[this._dragIdx].dst_t = Math.max(lo, Math.min(hi, this._xToS(x, W)));
    this._draw(); this._updateInfo();
  }

  _onDblClick(e) {
    const r = this._canvas.getBoundingClientRect();
    const W = this._canvas.width;
    const x = (e.clientX - r.left) * (W / r.width);
    const i = this._markerAtX(x, W);
    if (i >= 0) { this._anchors.splice(i, 1); this._draw(); this._updateInfo(); }
  }

  _addLock() {
    const t = this._video ? (this._video.currentTime || 0) : 0;
    this._anchors.push({ src_t: t, dst_t: t });
    this._anchors.sort((a, b) => a.src_t - b.src_t);
    this._draw(); this._updateInfo();
  }

  _updateInfo() {
    if (!this._anchors.length) { this._info.textContent = 'No locks yet -- the video plays at its natural timing.'; return; }
    const pts = [[0, 0], ...this._anchors.map(a => [a.src_t, a.dst_t]), [this._dur, this._dur]];
    const parts = [];
    for (let k = 0; k < pts.length - 1; k++) {
      const ss = pts[k + 1][0] - pts[k][0], ds = pts[k + 1][1] - pts[k][1];
      if (ss > 0.05 && ds > 0.05) {
        const f = ds / ss;
        parts.push(`${pts[k][1].toFixed(1)}-${pts[k + 1][1].toFixed(1)}s: video ${f < 1 ? 'faster' : (f > 1 ? 'slower' : 'normal')} ${f.toFixed(2)}x`);
      }
    }
    this._info.textContent = `${this._anchors.length} lock(s) | ${parts.join('  |  ')}`;
  }

  async _export() {
    if (!this._videoPath) { toast('No source video path', 'error'); return; }
    this._exportBtn.disabled = true;
    this._status.textContent = 'Submitting...';
    this._barWrap.style.display = 'block'; this._bar.style.width = '5%';
    try {
      const resp = await apiFetch('/api/retime/run', {
        method: 'POST',
        body: JSON.stringify({ video_path: this._videoPath, anchors: this._anchors }),
      });
      if (!resp.job_id) throw new Error(resp.error || 'No job started');
      const poll = setInterval(async () => {
        try {
          const j = await apiFetch(`/api/jobs/${resp.job_id}`);
          this._bar.style.width = (j.progress || 5) + '%';
          this._status.textContent = j.message || 'Retiming...';
          if (j.status === 'done') {
            clearInterval(poll);
            this._barWrap.style.display = 'none';
            this._status.textContent = 'Saved: ' + (j.output || '');
            this._exportBtn.disabled = false;
            document.dispatchEvent(new Event('session-updated'));
            toast('Retimed video saved', 'success');
            if (this._opts.onApplied) this._opts.onApplied(j.output);
          } else if (j.status === 'error' || j.status === 'stopped') {
            clearInterval(poll);
            this._barWrap.style.display = 'none';
            this._status.textContent = 'Failed: ' + (j.error || j.status);
            this._exportBtn.disabled = false;
          }
        } catch (_) {}
      }, 1500);
    } catch (e) {
      this._barWrap.style.display = 'none';
      this._status.textContent = 'Error: ' + e.message;
      this._exportBtn.disabled = false;
    }
  }

  destroy() { this._stopRaf(); }
}
