/**
 * Post Processing tab — Netflix VOID video object removal.
 *
 * Features:
 *  - Video upload
 *  - Frame scrubber + canvas mask painter (paint what to remove)
 *  - Brush / eraser with adjustable size
 *  - Submit for VOID inpainting
 *  - Job progress + result playback
 */

const API = '/api/post';

// ── State ─────────────────────────────────────────────────────────────────────

const st = {
  videoFile: null,       // filename (relative to uploads/)
  videoPath: null,       // absolute server path
  videoUrl:  null,       // for local preview
  frameB64:  null,       // current reference frame (JPEG base64)
  frameImg:  null,       // HTMLImageElement for canvas draw
  framePos:  0.0,        // 0.0–1.0 position in video
  canvas:    null,
  ctx:       null,
  painting:  false,
  tool:      'paint',    // 'paint' | 'erase'
  brushSize: 30,
  jobId:     null,
  pollTimer: null,
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function escHtml(s) {
  const d = document.createElement('div'); d.textContent = s; return d.innerHTML;
}

async function apiPost(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

async function apiGet(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}

function setStatus(el, msg, cls = '') {
  if (!el) return;
  el.textContent = msg;
  el.className   = `pp-status ${cls}`;
}

// ── Canvas mask painter ───────────────────────────────────────────────────────

function initCanvas(canvas, frameImg) {
  st.canvas = canvas;
  st.ctx    = canvas.getContext('2d');

  canvas.width  = frameImg.naturalWidth  || 640;
  canvas.height = frameImg.naturalHeight || 360;

  // Draw frame as background reference (visual only — actual mask is transparent png)
  redrawCanvas();

  canvas.addEventListener('mousedown', e => { st.painting = true; doPaint(e); });
  canvas.addEventListener('mousemove', e => { if (st.painting) doPaint(e); });
  canvas.addEventListener('mouseup',   () => { st.painting = false; });
  canvas.addEventListener('mouseleave',() => { st.painting = false; });

  // Touch support
  canvas.addEventListener('touchstart', e => { e.preventDefault(); st.painting = true; doPaint(e.touches[0]); });
  canvas.addEventListener('touchmove',  e => { e.preventDefault(); if (st.painting) doPaint(e.touches[0]); });
  canvas.addEventListener('touchend',   () => { st.painting = false; });
}

function doPaint(e) {
  const rect = st.canvas.getBoundingClientRect();
  const scaleX = st.canvas.width  / rect.width;
  const scaleY = st.canvas.height / rect.height;
  const x = (e.clientX - rect.left) * scaleX;
  const y = (e.clientY - rect.top)  * scaleY;

  const ctx = st.ctx;
  ctx.beginPath();
  ctx.arc(x, y, st.brushSize, 0, Math.PI * 2);

  if (st.tool === 'paint') {
    // Red semi-transparent — marks areas to REMOVE
    ctx.fillStyle = 'rgba(220, 30, 30, 0.75)';
    ctx.globalCompositeOperation = 'source-over';
    ctx.fill();
  } else {
    // Erase paint (restore transparency)
    ctx.globalCompositeOperation = 'destination-out';
    ctx.fill();
  }
  ctx.globalCompositeOperation = 'source-over';
}

function redrawCanvas() {
  if (!st.ctx || !st.canvas) return;
  // The canvas holds ONLY the painted mask (transparent background).
  // The video frame is shown via a CSS background-image behind the canvas.
}

function getMaskPNG() {
  // Export the mask canvas as PNG:
  //   painted pixels → white (255)  = remove
  //   transparent    → black (0)    = keep
  const { width, height } = st.canvas;
  const offscreen = document.createElement('canvas');
  offscreen.width  = width;
  offscreen.height = height;
  const ctx2 = offscreen.getContext('2d');

  // Fill black (keep)
  ctx2.fillStyle = 'black';
  ctx2.fillRect(0, 0, width, height);

  // Draw painted regions (red becomes white)
  ctx2.drawImage(st.canvas, 0, 0);

  // Convert red pixels to white
  const imgData = ctx2.getImageData(0, 0, width, height);
  const data = imgData.data;
  for (let i = 0; i < data.length; i += 4) {
    // If pixel has any red paint (alpha > 0 from our red paint)
    const a = data[i + 3];
    if (a > 30 && data[i] > 100) {  // red channel dominant
      data[i] = 255; data[i+1] = 255; data[i+2] = 255; data[i+3] = 255;
    } else {
      data[i] = 0; data[i+1] = 0; data[i+2] = 0; data[i+3] = 255;
    }
  }
  ctx2.putImageData(imgData, 0, 0);

  // Return as base64 PNG (strip data-URL prefix)
  return offscreen.toDataURL('image/png').split(',')[1];
}

function clearMask() {
  if (!st.ctx || !st.canvas) return;
  st.ctx.clearRect(0, 0, st.canvas.width, st.canvas.height);
}

// ── Frame loading ─────────────────────────────────────────────────────────────

async function loadFrame(panel, pos) {
  st.framePos = pos;
  const statusEl = panel.querySelector('#pp-frame-status');
  setStatus(statusEl, 'Extracting frame...', 'loading');

  try {
    const data = await apiPost(`${API}/extract-frame`, {
      path:     st.videoPath || st.videoFile,
      position: pos,
    });
    st.frameB64 = data.frame_b64;

    const framePreview = panel.querySelector('#pp-frame-preview');
    if (framePreview) {
      framePreview.style.backgroundImage = `url("data:image/jpeg;base64,${data.frame_b64}")`;
    }

    // Re-init canvas dimensions from actual image
    const img = new Image();
    img.onload = () => {
      st.frameImg = img;
      const canvas = panel.querySelector('#pp-mask-canvas');
      if (canvas) {
        canvas.width  = img.naturalWidth;
        canvas.height = img.naturalHeight;
        st.canvas = canvas;
        st.ctx    = canvas.getContext('2d');
      }
    };
    img.src = `data:image/jpeg;base64,${data.frame_b64}`;

    setStatus(statusEl, `Frame at ${Math.round(pos * 100)}%`, 'ok');
  } catch (e) {
    setStatus(statusEl, `Frame error: ${e.message}`, 'err');
  }
}

// ── Job polling ───────────────────────────────────────────────────────────────

function startPolling(panel) {
  if (st.pollTimer) clearInterval(st.pollTimer);
  st.pollTimer = setInterval(() => pollJob(panel), 1500);
}

async function pollJob(panel) {
  if (!st.jobId) return;
  try {
    const job = await apiGet(`/api/jobs/${st.jobId}`);
    const progressEl = panel.querySelector('#pp-progress');
    const resultEl   = panel.querySelector('#pp-result');

    if (progressEl) progressEl.textContent = job.progress || job.status || '';

    if (job.status === 'completed' && job.result) {
      clearInterval(st.pollTimer);
      st.pollTimer = null;
      const url = job.result.url || '';
      if (resultEl && url) {
        resultEl.innerHTML = `
          <p class="pp-done">Inpainting complete!</p>
          <video src="${url}" controls style="max-width:100%;border-radius:8px;margin-top:10px"></video>
          <div style="margin-top:8px">
            <a href="${url}" download class="btn btn-sm">Download</a>
            <button class="btn btn-sm" onclick="navigator.clipboard.writeText(location.origin+'${url}')">Copy URL</button>
          </div>`;
        panel.querySelector('#pp-run-btn')?.removeAttribute('disabled');
      }
    } else if (job.status === 'error') {
      clearInterval(st.pollTimer);
      st.pollTimer = null;
      if (progressEl) setStatus(progressEl, `Error: ${job.error || 'unknown'}`, 'err');
      panel.querySelector('#pp-run-btn')?.removeAttribute('disabled');
    }
  } catch (_) {}
}

// ── Upload ────────────────────────────────────────────────────────────────────

async function uploadVideo(panel, file) {
  const statusEl = panel.querySelector('#pp-upload-status');
  setStatus(statusEl, 'Uploading...', 'loading');

  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch(`${API}/upload`, { method: 'POST', body: fd });
  if (!r.ok) throw new Error(`Upload failed: ${r.status}`);
  const data = await r.json();

  st.videoFile = data.filename;
  st.videoPath = data.path;
  st.videoUrl  = data.url;

  setStatus(statusEl, `Uploaded: ${file.name}`, 'ok');
  return data;
}

// ── Submit ────────────────────────────────────────────────────────────────────

async function runInpaint(panel) {
  if (!st.videoFile) {
    alert('Please upload a video first.');
    return;
  }
  if (!st.canvas) {
    alert('Please load a frame and paint the area to remove.');
    return;
  }

  const maskB64 = getMaskPNG();
  const prompt  = panel.querySelector('#pp-prompt')?.value || '';
  const steps   = parseInt(panel.querySelector('#pp-steps')?.value || '30', 10);

  const runBtn = panel.querySelector('#pp-run-btn');
  if (runBtn) runBtn.setAttribute('disabled', '');

  const resultEl   = panel.querySelector('#pp-result');
  const progressEl = panel.querySelector('#pp-progress');
  if (resultEl)   resultEl.innerHTML = '';
  if (progressEl) setStatus(progressEl, 'Submitting...', 'loading');

  try {
    const data = await apiPost(`${API}/inpaint`, {
      video_path: st.videoPath || st.videoFile,
      mask_b64:   maskB64,
      prompt,
      steps,
    });
    st.jobId = data.job_id;
    if (progressEl) setStatus(progressEl, 'Job queued...', 'loading');
    startPolling(panel);
  } catch (e) {
    if (progressEl) setStatus(progressEl, `Submit failed: ${e.message}`, 'err');
    if (runBtn) runBtn.removeAttribute('disabled');
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────

export async function init(panel) {
  panel.innerHTML = `
<div class="pp-layout">
  <div class="pp-sidebar">

    <div class="card">
      <h3>1. Upload Video</h3>
      <div class="upload-zone" id="pp-upload-zone">
        <p>Drop a video here or click to browse</p>
        <input type="file" id="pp-file-input" accept="video/*" style="display:none">
      </div>
      <div id="pp-upload-status" class="pp-status"></div>
    </div>

    <div class="card">
      <h3>2. Choose Frame to Mask</h3>
      <p style="font-size:.85rem;color:var(--text-3);margin-bottom:8px">
        Scrub to the frame that best shows the object to remove.
      </p>
      <div style="display:flex;align-items:center;gap:8px">
        <input type="range" id="pp-scrubber" min="0" max="100" value="0"
               style="flex:1" title="Frame position">
        <span id="pp-scrub-pct" style="font-size:.8rem;color:var(--text-3);min-width:32px">0%</span>
      </div>
      <button class="btn btn-sm" id="pp-load-frame-btn" style="margin-top:8px;width:100%">
        Load Frame
      </button>
      <div id="pp-frame-status" class="pp-status"></div>
    </div>

    <div class="card">
      <h3>3. Paint Mask</h3>
      <p style="font-size:.85rem;color:var(--text-3);margin-bottom:8px">
        Paint <span style="color:#e04040">red</span> over the object to remove.
        VOID will fill the region using physics-aware inpainting.
      </p>
      <div style="display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap">
        <button class="btn btn-sm" id="pp-tool-paint">Paint</button>
        <button class="btn btn-sm" id="pp-tool-erase">Erase</button>
        <button class="btn btn-sm btn-danger" id="pp-clear-mask">Clear</button>
      </div>
      <label style="font-size:.82rem;color:var(--text-3)">
        Brush size: <span id="pp-brush-label">30</span>px
        <input type="range" id="pp-brush-size" min="5" max="120" value="30" style="width:100%">
      </label>
    </div>

    <div class="card">
      <h3>4. Run VOID</h3>
      <label style="font-size:.82rem;color:var(--text-3)">
        Hint (optional)
        <input type="text" id="pp-prompt" class="input" placeholder="e.g. empty park bench"
               style="width:100%;margin-top:4px">
      </label>
      <label style="font-size:.82rem;color:var(--text-3);display:block;margin-top:8px">
        Steps: <span id="pp-steps-label">30</span>
        <input type="range" id="pp-steps" min="10" max="50" value="30" style="width:100%">
      </label>
      <button class="btn btn-primary" id="pp-run-btn" style="width:100%;margin-top:12px">
        Run VOID Inpainting
      </button>
      <div id="pp-progress" class="pp-status" style="margin-top:8px"></div>
    </div>

    <div class="card" id="pp-void-status-card">
      <h3>VOID Service</h3>
      <div id="pp-void-service-msg" class="pp-status">Checking...</div>
      <button class="btn btn-sm" id="pp-start-void" style="margin-top:8px;width:100%">
        Start VOID Worker
      </button>
    </div>

  </div>

  <div class="pp-main">
    <div class="pp-canvas-wrap" id="pp-canvas-wrap">
      <div id="pp-frame-preview" class="pp-frame-bg">
        <span class="pp-canvas-hint">Upload a video and load a frame to start painting</span>
      </div>
      <canvas id="pp-mask-canvas" class="pp-mask-canvas"></canvas>
    </div>

    <div class="card" id="pp-result" style="margin-top:12px"></div>
  </div>
</div>

<style>
.pp-layout { display:flex; gap:16px; height:100%; }
.pp-sidebar { width:280px; flex-shrink:0; display:flex; flex-direction:column; gap:10px; overflow-y:auto; }
.pp-main    { flex:1; display:flex; flex-direction:column; gap:10px; min-width:0; }
.pp-canvas-wrap { position:relative; flex:1; min-height:360px; background:var(--surface); border-radius:var(--r-md); overflow:hidden; }
.pp-frame-bg { position:absolute; inset:0; background-size:contain; background-repeat:no-repeat; background-position:center; background-color:#111; display:flex; align-items:center; justify-content:center; }
.pp-canvas-hint { color:var(--text-3); font-size:.9rem; }
.pp-mask-canvas { position:absolute; inset:0; width:100%; height:100%; cursor:crosshair; }
.pp-status { font-size:.82rem; margin-top:4px; }
.pp-status.ok   { color:#4caf50; }
.pp-status.err  { color:#e04040; }
.pp-status.loading { color:var(--accent); }
.pp-done { color:#4caf50; font-weight:600; }
.btn-danger { background:var(--circus-red); color:#fff; }
#pp-tool-paint.active, #pp-tool-erase.active { border-color:var(--accent); color:var(--accent); }
</style>
`;

  // ── Wire up events ─────────────────────────────────────────────────────────

  // Upload zone
  const uploadZone = panel.querySelector('#pp-upload-zone');
  const fileInput  = panel.querySelector('#pp-file-input');
  uploadZone.addEventListener('click', () => fileInput.click());
  uploadZone.addEventListener('dragover', e => { e.preventDefault(); uploadZone.classList.add('dragover'); });
  uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('dragover'));
  uploadZone.addEventListener('drop', async e => {
    e.preventDefault();
    uploadZone.classList.remove('dragover');
    const file = e.dataTransfer.files[0];
    if (file) await uploadVideo(panel, file).catch(err => alert(err.message));
  });
  fileInput.addEventListener('change', async () => {
    if (fileInput.files[0]) await uploadVideo(panel, fileInput.files[0]).catch(err => alert(err.message));
  });

  // Scrubber
  const scrubber  = panel.querySelector('#pp-scrubber');
  const scrubPct  = panel.querySelector('#pp-scrub-pct');
  scrubber.addEventListener('input', () => { scrubPct.textContent = `${scrubber.value}%`; });

  // Load frame button
  panel.querySelector('#pp-load-frame-btn').addEventListener('click', async () => {
    if (!st.videoFile) { alert('Upload a video first.'); return; }
    const pos = parseInt(scrubber.value, 10) / 100;
    await loadFrame(panel, pos);
    // Wire canvas after frame load
    const canvas = panel.querySelector('#pp-mask-canvas');
    if (canvas && !canvas.dataset.wired) {
      canvas.dataset.wired = '1';
      initCanvas(canvas, st.frameImg || new Image());
    }
  });

  // Tool buttons
  const paintBtn = panel.querySelector('#pp-tool-paint');
  const eraseBtn = panel.querySelector('#pp-tool-erase');
  paintBtn.classList.add('active');
  paintBtn.addEventListener('click', () => { st.tool = 'paint'; paintBtn.classList.add('active'); eraseBtn.classList.remove('active'); });
  eraseBtn.addEventListener('click', () => { st.tool = 'erase'; eraseBtn.classList.add('active'); paintBtn.classList.remove('active'); });

  // Brush size
  const brushInput = panel.querySelector('#pp-brush-size');
  const brushLabel = panel.querySelector('#pp-brush-label');
  brushInput.addEventListener('input', () => {
    st.brushSize = parseInt(brushInput.value, 10);
    brushLabel.textContent = st.brushSize;
  });

  // Clear
  panel.querySelector('#pp-clear-mask').addEventListener('click', clearMask);

  // Steps label
  const stepsInput = panel.querySelector('#pp-steps');
  const stepsLabel = panel.querySelector('#pp-steps-label');
  stepsInput.addEventListener('input', () => { stepsLabel.textContent = stepsInput.value; });

  // Run button
  panel.querySelector('#pp-run-btn').addEventListener('click', () => runInpaint(panel));

  // Start VOID worker button
  panel.querySelector('#pp-start-void').addEventListener('click', async () => {
    const msgEl = panel.querySelector('#pp-void-service-msg');
    setStatus(msgEl, 'Starting VOID worker...', 'loading');
    try {
      const r = await fetch('/api/services/start/void', { method: 'POST' });
      const d = await r.json();
      if (d.ok) {
        setStatus(msgEl, 'VOID worker starting (model loading...)');
      } else {
        setStatus(msgEl, d.error || 'Failed to start', 'err');
      }
    } catch (e) {
      setStatus(msgEl, e.message, 'err');
    }
  });

  // ── Initial VOID status check ─────────────────────────────────────────────
  try {
    const s = await apiGet(`${API}/void-ready`);
    const msgEl = panel.querySelector('#pp-void-service-msg');
    if (s.alive) {
      setStatus(msgEl, 'VOID worker running', 'ok');
    } else if (s.configured) {
      setStatus(msgEl, 'VOID configured — not running yet');
    } else {
      setStatus(msgEl, 'VOID not configured — set model path in Settings or use HF auto-download');
    }
  } catch (_) {}
}
