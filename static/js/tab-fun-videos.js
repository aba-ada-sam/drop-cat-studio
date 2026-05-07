/**
 * Drop Cat Go Studio — Create Videos
 * Pick a generated image, write a motion prompt, get a video.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js?v=20260505e';
import { createProgressCard, createVideoPlayer, createSlider, el, pathToUrl } from './components.js?v=20260429b';
import { toast, apiFetch } from './shell/toast.js?v=20260503a';
import { handoff } from './handoff.js?v=20260422a';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260503g';

// Concurrency limiter for thumbnail extraction — caps parallel <video> preloads.
const _thumbQueue = { running: 0, max: 4, pending: [] };
function _thumbSlot(fn) {
  return new Promise(resolve => {
    const run = () => {
      _thumbQueue.running++;
      fn().then(v => {
        resolve(v);
        _thumbQueue.running--;
        if (_thumbQueue.pending.length) _thumbQueue.pending.shift()();
      });
    };
    if (_thumbQueue.running < _thumbQueue.max) run();
    else _thumbQueue.pending.push(run);
  });
}

// Extract a video frame client-side via hidden <video> + canvas.
// Returns a data-URL string, or null on failure.
function _videoThumb(videoUrl) {
  return _thumbSlot(() => _videoThumbInner(videoUrl));
}
function _videoThumbInner(videoUrl) {
  return new Promise(resolve => {
    let settled = false;
    const done = (val) => {
      if (settled) return;
      settled = true;
      video.src = '';     // release the network request
      resolve(val);
    };

    const video = document.createElement('video');
    video.muted    = true;
    video.preload  = 'auto';  // must actually buffer data, not just metadata
    video.playsInline = true;

    // loadedmetadata fires once dimensions + duration are known.
    // Seek to 5% of duration (or 0.5s, whichever is smaller) so we don't
    // wait for the whole file to buffer.
    video.addEventListener('loadedmetadata', () => {
      video.currentTime = Math.min(0.5, (video.duration || 10) * 0.05);
    }, { once: true });

    // seeked fires when the browser has decoded the frame at currentTime.
    video.addEventListener('seeked', () => {
      try {
        const w = video.videoWidth  || 320;
        const h = video.videoHeight || 180;
        const c = document.createElement('canvas');
        c.width = w; c.height = h;
        c.getContext('2d').drawImage(video, 0, 0, w, h);
        done(c.toDataURL('image/jpeg', 0.75));
      } catch { done(null); }
    }, { once: true });

    video.addEventListener('error', () => done(null), { once: true });
    setTimeout(() => done(null), 10000);

    video.src = videoUrl;
  });
}

let _startImagePath  = null;
let _endImagePath    = null;
let _videoFramePath  = null; // first frame of start-video, used for LLM vision calls
let _activeJobId     = null;
let _activePoller    = null;
let _models          = {};
let _applyStart      = null;
let _applyVideoFn    = null;
let _pendingHandoff  = null; // queued before tab first visit; applied on init

export function receiveHandoff(data) {
  if (!data.path) return;
  if (data.type === 'video' && _applyVideoFn) {
    _applyVideoFn(data.path, data.url || '');
  } else if (data.type === 'image' && _applyStart) {
    _applyStart(data.path, data.url || '');
  } else {
    // Tab not yet initialized -- queue it; init() will pick it up
    _pendingHandoff = data;
  }
}


export function init(panel) {
  panel.innerHTML = '';
  _startImagePath = null;
  _endImagePath   = null;

  const root = el('div', { style: 'display:flex; flex-direction:column; gap:14px; padding:16px; max-width:860px; margin:0 auto;' });
  panel.appendChild(root);

  // ── Recent Media picker ───────────────────────────────────────────────────
  const pickerCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(pickerCard);

  const pickerHeader = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-bottom:10px;' });
  pickerCard.appendChild(pickerHeader);
  pickerHeader.appendChild(el('span', { style: 'font-size:.85rem; font-weight:600; flex:1;', text: 'Recent Media' }));

  const refreshBtn = el('button', { class: 'btn btn-sm', text: 'Refresh' });
  pickerHeader.appendChild(refreshBtn);

  const fileInput = el('input', { type: 'file', accept: 'image/*,video/*', style: 'display:none' });
  pickerCard.appendChild(fileInput);
  const openFileBtn = el('button', { class: 'btn btn-sm', text: 'Open file…' });
  pickerHeader.appendChild(openFileBtn);
  openFileBtn.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', async () => {
    if (!fileInput.files?.length) return;
    const f0 = fileInput.files[0];
    const isVid = f0.type.startsWith('video/');
    try {
      const data = await apiUpload(isVid ? '/api/fun/upload-video' : '/api/fun/upload', Array.from(fileInput.files));
      const f = data.files?.[0];
      if (f) {
        if (isVid) _applyVideo(f.path, f.url || pathToUrl(f.path));
        else        _applyStart(f.path, f.url || pathToUrl(f.path));
      }
    } catch (e) { toast(e.message, 'error'); }
    fileInput.value = '';
  });

  const mediaGrid = el('div', { style: 'display:grid; grid-template-columns:repeat(auto-fill,minmax(90px,1fr)); gap:6px; max-height:280px; overflow-y:auto;' });
  pickerCard.appendChild(mediaGrid);

  let _selectedThumb = null;

  function _isVideo(url) { return /(\.mp4|\.webm|\.mov)$/i.test(url || ''); }

  function _mediaFallback(isVid) {
    return el('div', {
      style: 'width:100%; aspect-ratio:1; display:flex; align-items:center; justify-content:center; font-size:1.6rem; color:var(--text-3); background:var(--surface-2); border-radius:6px;',
      text: isVid ? '🎬' : '🖼',
    });
  }

  const _FV_DISMISSED_KEY = 'fv_dismissed_media';
  function _getFvDismissed() {
    try { return new Set(JSON.parse(localStorage.getItem(_FV_DISMISSED_KEY) || '[]')); }
    catch (_) { return new Set(); }
  }
  function _saveFvDismissed(s) {
    try { localStorage.setItem(_FV_DISMISSED_KEY, JSON.stringify([...s].slice(-300))); }
    catch (_) {}
  }

  async function loadRecentMedia() {
    try {
      const [galleryRes, sessionRes] = await Promise.allSettled([
        api('/api/gallery'),
        api('/api/session/images'),
      ]);

      const dismissed = _getFvDismissed();
      const seen = new Set();
      const items = [];

      for (const item of (galleryRes.status === 'fulfilled' ? (galleryRes.value.items || galleryRes.value || []) : [])) {
        const rawUrl = item.url || '';
        const url  = pathToUrl(rawUrl) || rawUrl;
        const path = item.metadata?.path || rawUrl;
        if (!url || seen.has(url) || dismissed.has(url)) continue;
        seen.add(url);
        const vid = _isVideo(rawUrl);
        items.push({ url, path, vid, thumbSrc: vid ? null : (pathToUrl(item.thumbnail) || url) });
      }

      for (const img of (sessionRes.status === 'fulfilled' ? (sessionRes.value.images || []) : [])) {
        const rawUrl = img.url || img.path || '';
        const url  = pathToUrl(rawUrl) || rawUrl;
        const path = img.path || rawUrl;
        if (!url || seen.has(url) || dismissed.has(url)) continue;
        seen.add(url);
        items.push({ url, path, vid: false, thumbSrc: url });
      }

      mediaGrid.innerHTML = '';
      _selectedThumb = null;

      if (!items.length) {
        mediaGrid.appendChild(el('div', {
          style: 'grid-column:1/-1; text-align:center; padding:32px 0; color:var(--text-3); font-size:.82rem;',
          text: 'Nothing yet -- generate or upload something first.',
        }));
        return;
      }

      for (const item of items.slice(0, 60)) {
        const wrap = el('div', {
          style: 'position:relative; cursor:pointer; background:var(--surface-2); border-radius:6px; overflow:hidden;',
        });

        const thumb = el('img', {
          style: 'width:100%; aspect-ratio:1; object-fit:cover; border-radius:6px; border:2px solid transparent; transition:border-color .15s; display:block;',
        });
        wrap.appendChild(thumb);

        const dismissBtn = el('button', {
          style: 'position:absolute;top:3px;right:3px;width:20px;height:20px;border-radius:50%;border:none;background:rgba(0,0,0,.7);color:#fff;font-size:12px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0;z-index:2;',
          title: 'Remove from list',
          text: 'x',
        });
        dismissBtn.addEventListener('click', e => {
          e.stopPropagation();
          wrap.remove();
          const d = _getFvDismissed();
          d.add(item.url);
          _saveFvDismissed(d);
          if (!mediaGrid.children.length) loadRecentMedia();
        });
        wrap.appendChild(dismissBtn);

        wrap.addEventListener('click', e => {
          if (e.target === dismissBtn) return;
          mediaGrid.querySelectorAll('img').forEach(i => { i.style.borderColor = 'transparent'; });
          if (_selectedThumb) _selectedThumb.style.borderColor = 'transparent';
          thumb.style.borderColor = 'var(--accent)';
          _selectedThumb = thumb;
          if (item.vid) _applyVideo(item.path, item.url);
          else          _applyStart(item.path, item.url);
        });

        if (item.thumbSrc) {
          thumb.src = item.thumbSrc;
          thumb.onerror = () => { thumb.replaceWith(_mediaFallback(false)); };
        } else if (item.vid) {
          const fb = _mediaFallback(true);
          wrap.insertBefore(fb, thumb);
          thumb.style.display = 'none';
          _videoThumb(item.url).then(dataUrl => {
            if (dataUrl) { fb.remove(); thumb.src = dataUrl; thumb.style.display = ''; }
          });
        }

        if (item.vid) {
          wrap.appendChild(el('div', {
            style: 'position:absolute; inset:0; display:flex; align-items:center; justify-content:center; pointer-events:none;',
          }, [el('div', { style: 'background:rgba(0,0,0,.55); border-radius:50%; width:28px; height:28px; display:flex; align-items:center; justify-content:center; font-size:13px; color:#fff;', text: '>' })]));
        }

        mediaGrid.appendChild(wrap);
      }
    } catch (e) { toast(e.message, 'error'); }
  }

  refreshBtn.addEventListener('click', loadRecentMedia);
  loadRecentMedia();

  // ── Selected media preview ─────────────────────────────────────────────────
  const previewCard = el('div', { class: 'drop-zone', style: 'display:none; position:relative; overflow:hidden; padding:0;' });
  root.appendChild(previewCard);

  const previewImg   = el('img',   { style: 'display:none; width:100%; max-height:260px; object-fit:contain; border-radius:8px; background:var(--bg-raised);' });
  const previewVid   = el('video', { controls: '', style: 'display:none; width:100%; max-height:260px; border-radius:8px; background:#000;' });
  const previewClear = el('button', {
    style: 'position:absolute; top:6px; right:6px; width:24px; height:24px; border-radius:50%; border:none; background:rgba(0,0,0,.65); color:#fff; font-size:15px; line-height:1; cursor:pointer; z-index:2; padding:0;',
    title: 'Clear', text: '×',
  });
  previewCard.appendChild(previewImg);
  previewCard.appendChild(previewVid);
  previewCard.appendChild(previewClear);

  // Wipe the motion prompt when the source image changes so the auto-gen
  // refills it from the NEW image instead of recycling text written for the
  // previous one. Defined here as a closure so the listeners below can call
  // it; promptTA is declared further down but exists by the time these fire.
  function _wipeMotionPrompt() {
    if (typeof promptTA !== 'undefined' && promptTA) promptTA.value = '';
  }

  previewClear.addEventListener('click', () => {
    _startImagePath = null;
    _startVideoPath = null;
    _videoFramePath = null;
    previewCard.style.display = 'none';
    previewImg.style.display = 'none'; previewImg.src = '';
    previewVid.style.display = 'none'; previewVid.src = '';
    if (_selectedThumb) { _selectedThumb.style.borderColor = 'transparent'; _selectedThumb = null; }
    // Un-check video toggle
    videoChk.checked = false;
    videoCard.style.display = 'none';
    _wipeMotionPrompt();
  });

  _applyStart = (path, url) => {
    if (path !== _startImagePath) _wipeMotionPrompt();
    _startImagePath = path;
    _startVideoPath = null;
    previewImg.src = url || pathToUrl(path);
    previewImg.style.display = 'block';
    previewVid.style.display = 'none'; previewVid.src = '';
    previewCard.style.display = '';
    // Un-check video toggle if switching from video to image
    videoChk.checked = false;
    videoCard.style.display = 'none';
  };

  function _applyVideo(path, url) {
    _startVideoPath = path;
    _startImagePath = null;
    _videoFramePath = null; // reset; extraction runs async below
    const vUrl = url || pathToUrl(path);
    previewVid.src = vUrl;
    previewVid.style.display = 'block';
    previewImg.style.display = 'none'; previewImg.src = '';
    previewCard.style.display = '';
    // Auto-enable the video-to-video toggle
    videoChk.checked = true;
    videoCard.style.display = '';
    videoName.textContent = path.split(/[\\/]/).pop();
    videoClearBtn.style.display = '';
    // Extract first frame client-side and upload it so Create Story can use LLM vision.
    _videoThumb(vUrl).then(async (dataUrl) => {
      if (!dataUrl || _startVideoPath !== path) return; // aborted or new video loaded
      try {
        const blob = await fetch(dataUrl).then(r => r.blob());
        const file = new File([blob], 'frame.jpg', { type: 'image/jpeg' });
        const data = await apiUpload('/api/fun/upload', [file]);
        const f = data.files?.[0];
        if (f && _startVideoPath === path) _videoFramePath = f.path;
      } catch (_) {}
    });
  }
  _applyVideoFn = _applyVideo;

  // Drain any handoff that arrived before this tab was initialized
  if (_pendingHandoff) {
    const ph = _pendingHandoff; _pendingHandoff = null;
    receiveHandoff(ph);
  }

  // ── Start video (video-to-video) ──────────────────────────────────────────
  let _startVideoPath = null;
  const videoToggleRow = el('div', { style: 'display:flex; align-items:center; gap:8px;' });
  root.appendChild(videoToggleRow);
  const videoChk = el('input', { type: 'checkbox', id: 'fv-video-toggle' });
  videoToggleRow.appendChild(videoChk);
  videoToggleRow.appendChild(el('label', { for: 'fv-video-toggle', style: 'font-size:.82rem; color:var(--text-3); cursor:pointer; user-select:none;', text: '+ Start video — use a video as the source instead of an image (video-to-video)' }));

  const videoCard = el('div', { class: 'card', style: 'display:none; padding:12px;' });
  root.appendChild(videoCard);

  const videoFileInput = el('input', { type: 'file', accept: 'video/*', style: 'display:none' });
  videoCard.appendChild(videoFileInput);
  const videoOpenBtn = el('button', { class: 'btn btn-sm', text: 'Choose video...' });
  const videoClearBtn = el('button', { class: 'btn btn-sm', text: '✕ Clear', style: 'display:none;' });
  const videoName = el('div', { style: 'font-size:.78rem; color:var(--text-2); margin-top:6px;' });
  videoCard.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:center;' }, [videoOpenBtn, videoClearBtn]));
  videoCard.appendChild(videoName);

  videoOpenBtn.addEventListener('click', () => videoFileInput.click());
  videoFileInput.addEventListener('change', async () => {
    if (!videoFileInput.files?.length) return;
    try {
      const data = await apiUpload('/api/fun/upload-video', Array.from(videoFileInput.files));
      const f = data.files?.[0];
      if (f) { _startVideoPath = f.path; videoName.textContent = f.name; videoClearBtn.style.display = ''; }
    } catch (e) { toast(e.message, 'error'); }
    videoFileInput.value = '';
  });
  videoClearBtn.addEventListener('click', () => { _startVideoPath = null; videoName.textContent = ''; videoClearBtn.style.display = 'none'; });
  videoChk.addEventListener('change', () => {
    videoCard.style.display = videoChk.checked ? '' : 'none';
    if (!videoChk.checked) { _startVideoPath = null; videoName.textContent = ''; videoClearBtn.style.display = 'none'; }
  });

  // ── End image (optional morph) ────────────────────────────────────────────
  const endToggleRow = el('div', { style: 'display:flex; align-items:center; gap:8px;' });
  root.appendChild(endToggleRow);

  const endChk = el('input', { type: 'checkbox', id: 'fv-end-toggle' });
  const endToggleLabel = el('label', { for: 'fv-end-toggle', style: 'font-size:.82rem; color:var(--text-3); cursor:pointer; user-select:none;',
    text: '+ End image — morph from start to end' });
  endToggleRow.appendChild(endChk);
  endToggleRow.appendChild(endToggleLabel);

  const endCard = el('div', { class: 'card', style: 'display:none; padding:14px;' });
  root.appendChild(endCard);

  endCard.appendChild(el('div', { style: 'font-size:.82rem; font-weight:600; margin-bottom:2px; color:var(--text-2);', text: 'End Image' }));
  endCard.appendChild(el('div', { style: 'font-size:.74rem; color:var(--text-3); margin-bottom:8px;', text: 'Video morphs from your selected image into this one.' }));

  const endPreview = el('img', { style: 'display:none; width:120px; height:80px; object-fit:cover; border-radius:6px; margin-bottom:6px;' });
  endCard.appendChild(endPreview);
  const endClearBtn = el('button', { class: 'btn btn-sm', text: '✕ Clear end image', style: 'display:none; font-size:.72rem; margin-bottom:8px;',
    onclick() { _endImagePath = null; endPreview.style.display = 'none'; endPreview.src = ''; endClearBtn.style.display = 'none'; },
  });
  endCard.appendChild(endClearBtn);

  const endFileInput = el('input', { type: 'file', accept: 'image/*', style: 'display:none' });
  endCard.appendChild(endFileInput);
  const endOpenBtn = el('button', { class: 'btn btn-sm', text: 'Choose end image...' });
  endCard.appendChild(endOpenBtn);
  endOpenBtn.addEventListener('click', () => endFileInput.click());
  endFileInput.addEventListener('change', async () => {
    if (!endFileInput.files?.length) return;
    try {
      const data = await apiUpload('/api/fun/upload', Array.from(endFileInput.files));
      const f = data.files?.[0];
      if (f) {
        _endImagePath = f.path;
        endPreview.src = f.url || pathToUrl(f.path);
        endPreview.style.display = '';
        endClearBtn.style.display = '';
      }
    } catch (e) { toast(e.message, 'error'); }
    endFileInput.value = '';
  });

  endChk.addEventListener('change', () => {
    endCard.style.display = endChk.checked ? '' : 'none';
    if (!endChk.checked) {
      _endImagePath = null;
      endPreview.style.display = 'none';
      endPreview.src = '';
      endClearBtn.style.display = 'none';
    }
  });

  // ── Motion prompt ─────────────────────────────────────────────────────────
  const promptCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(promptCard);
  promptCard.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:4px;', text: 'Motion Prompt' }));
  promptCard.appendChild(el('div', { style: 'font-size:.74rem; color:var(--text-3); margin-bottom:8px;',
    text: 'Describe the action — what does the subject DO? Camera moves are secondary.' }));
  const PROMPT_PLACEHOLDER = 'e.g. "Throws head back laughing, hair whipping sideways, hands clap wildly, energy radiates outward, camera pulls back to reveal full burst of motion"';
  const PROMPT_DEFAULT     = 'Subject erupts into motion, hair and clothes responding to sudden energy, arms move expressively, dynamic action fills every corner of the frame';
  const promptTA = el('textarea', { rows: '3', style: 'width:100%; resize:vertical; font-size:.9rem;',
    placeholder: PROMPT_PLACEHOLDER });
  promptCard.appendChild(promptTA);

  const promptStatusMsg = el('span', { text: 'Generating motion prompt from image...' });
  const promptStatus = el('div', {
    style: 'display:none; font-size:.75rem; color:var(--accent); margin-top:5px; align-items:center; gap:6px;',
  }, [
    el('span', { style: 'display:inline-block; width:10px; height:10px; border:2px solid var(--accent); border-top-color:transparent; border-radius:50%; animation:spin .7s linear infinite; flex-shrink:0;' }),
    promptStatusMsg,
    el('span', { style: 'color:var(--text-3);', text: '— or just click Generate to skip' }),
  ]);
  promptCard.appendChild(promptStatus);

  // ── Create Story row ──────────────────────────────────────────────────────
  const storyRow = el('div', { style: 'display:flex; gap:6px; align-items:center; margin-top:8px;' });
  const storyBtn = el('button', { class: 'btn btn-sm btn-primary', text: '✦ Create Story', title: 'Generate a motion prompt from your image using AI' });
  storyRow.appendChild(storyBtn);
  promptCard.appendChild(storyRow);

  let _autoPromptAbort = null;

  // Auto-generate motion prompt from the selected image via LLM vision.
  // force=true: regenerates even if prompt textarea already has content.
  async function _autoGeneratePrompt(imagePath, force = false) {
    if (!force && promptTA.value.trim()) return;
    // Don't fire Ollama while a video job is running — it competes for VRAM.
    // The user can still click "Create Story" manually (force=true bypasses this).
    if (!force && _activeJobId) return;
    if (_autoPromptAbort) _autoPromptAbort.abort();
    _autoPromptAbort = new AbortController();
    const { signal } = _autoPromptAbort;

    promptStatus.style.display = 'flex';
    storyBtn.disabled = true;
    storyBtn.textContent = '…';

    // Safety timeout — give up after 90s and let the user proceed
    // (Ollama vision cold-start can take 60s+)
    const timeout = setTimeout(() => {
      _autoPromptAbort?.abort();
      promptStatus.style.display = 'none';
      toast('Motion prompt timed out — type one manually or click Create Story to retry', 'warn');
    }, 90000);

    try {
      const data = await apiFetch('/api/fun/generate-prompts', {
        method: 'POST',
        body: JSON.stringify({ image_path: imagePath, num_prompts: 1, creativity: 7, max_tokens: 400 }),
        signal,
      });
      const prompts = data.prompts || [];
      const text = typeof prompts[0] === 'string' ? prompts[0] : prompts[0]?.prompt;
      if (text) promptTA.value = text;
    } catch (e) {
      if (e?.name !== 'AbortError') toast(e.message || 'Story generation failed', 'error');
    } finally {
      clearTimeout(timeout);
      promptStatus.style.display = 'none';
      storyBtn.disabled = false;
      storyBtn.textContent = '✦ Create Story';
      _autoPromptAbort = null;
    }
  }

  // Extend _applyStart (defined above) to trigger auto-prompt
  const _applyStartBase = _applyStart;
  _applyStart = (path, url) => {
    _applyStartBase(path, url);
    _autoGeneratePrompt(path);
    refineRow.style.display = 'flex';
  };

  storyBtn.addEventListener('click', () => {
    const visionPath = _startImagePath || _videoFramePath;
    if (!visionPath && !_startVideoPath) { toast('Select an image or video first', 'error'); return; }
    if (!visionPath) {
      toast('Frame still extracting from video -- try again in a moment', 'info');
      return;
    }
    _autoGeneratePrompt(visionPath, true);
  });

  // ── Prompt refinement row ──────────────────────────────────────────────────
  const refineRow = el('div', { style: 'display:none; gap:6px; align-items:center; margin-top:6px;' });
  promptCard.appendChild(refineRow);

  const refineInput = el('input', {
    type: 'text',
    placeholder: 'Refine: "make it more dramatic", "add fog", "slow camera"...',
    style: 'flex:1; font-size:.82rem;',
  });
  const refineBtn = el('button', { class: 'btn btn-sm', text: 'Refine' });
  refineRow.appendChild(refineInput);
  refineRow.appendChild(refineBtn);

  const refineSuggestion = el('div', { style: 'display:none; margin-top:6px; padding:8px 10px; background:var(--bg-raised); border-radius:6px; font-size:.82rem; color:var(--text-2); border:1px solid var(--border-2);' });
  promptCard.appendChild(refineSuggestion);

  const refineActions = el('div', { style: 'display:none; gap:6px; margin-top:4px;' });
  const refineApply  = el('button', { class: 'btn btn-sm btn-primary', text: 'Use this' });
  const refineTryAgain = el('button', { class: 'btn btn-sm', text: 'Try again' });
  refineActions.appendChild(refineApply);
  refineActions.appendChild(refineTryAgain);
  promptCard.appendChild(refineActions);

  let _lastSuggestion = '';

  async function _refinePrompt() {
    const feedback = refineInput.value.trim();
    if (!feedback || !promptTA.value.trim()) return;
    refineBtn.disabled = true;
    refineBtn.textContent = '...';
    try {
      const data = await api('/api/fun/refine-prompt', {
        method: 'POST',
        body: JSON.stringify({
          current_prompt: promptTA.value.trim(),
          feedback,
          image_path: _startImagePath || '',
        }),
      });
      _lastSuggestion = data.prompt || '';
      if (_lastSuggestion) {
        refineSuggestion.textContent = _lastSuggestion;
        refineSuggestion.style.display = '';
        refineActions.style.display = 'flex';
      }
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      refineBtn.disabled = false;
      refineBtn.textContent = 'Refine';
    }
  }

  refineBtn.addEventListener('click', _refinePrompt);
  refineInput.addEventListener('keydown', e => { if (e.key === 'Enter') _refinePrompt(); });

  refineApply.addEventListener('click', () => {
    if (_lastSuggestion) {
      promptTA.value = _lastSuggestion;
      refineSuggestion.style.display = 'none';
      refineActions.style.display = 'none';
      refineInput.value = '';
    }
  });

  refineTryAgain.addEventListener('click', () => {
    refineSuggestion.style.display = 'none';
    refineActions.style.display = 'none';
    _refinePrompt();
  });

  // ── Settings ──────────────────────────────────────────────────────────────
  const settingsCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(settingsCard);
  settingsCard.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:12px;', text: 'Settings' }));

  const topGrid = el('div', { style: 'display:grid; grid-template-columns:2fr 1fr; gap:10px; margin-bottom:10px;' });
  settingsCard.appendChild(topGrid);

  const modelSel  = el('select', { style: 'width:100%;' });
  const modelInfo = el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-top:3px;' });
  topGrid.appendChild(el('div', {}, [
    el('label', { text: 'Model', style: 'display:block; font-size:.82rem; color:var(--text-3); margin-bottom:4px;' }),
    modelSel, modelInfo,
  ]));

  const seedIn = el('input', { type: 'number', value: '-1', style: 'width:100%;' });
  topGrid.appendChild(el('div', {}, [
    el('label', { text: 'Seed (-1 = random)', style: 'display:block; font-size:.82rem; color:var(--text-3); margin-bottom:4px;' }),
    seedIn,
  ]));

  const durSlider = createSlider(settingsCard, { label: 'Duration (seconds)', min: 2, max: 20, step: 1, value: 8 });

  const slidersGrid = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:10px;' });
  settingsCard.appendChild(slidersGrid);
  const stepsSlider    = createSlider(slidersGrid, { label: 'Steps',    min: 4,  max: 50,  step: 1,   value: 8   });
  const guidanceSlider = createSlider(slidersGrid, { label: 'Guidance', min: 1,  max: 20,  step: 0.5, value: 8.5 });

  modelSel.addEventListener('change', () => {
    const m = _models[modelSel.value];
    if (!m) return;
    durSlider.value = Math.min(parseFloat(durSlider.value) || 8, m.max_sec);
    modelInfo.textContent = `Max ${m.max_sec}s  •  ${m.res ? m.res[0]+'×'+m.res[1] : ''}  •  ${m.fps}fps`;
  });

  api('/api/fun/models').then(data => {
    _models = data.models || {};
    modelSel.innerHTML = '';
    for (const [name] of Object.entries(_models))
      modelSel.appendChild(el('option', { value: name, text: name }));
    const ltx = Object.keys(_models).find(k => k.includes('LTX'));
    if (ltx) modelSel.value = ltx;
    modelSel.dispatchEvent(new Event('change'));
  }).catch(() => toast('Could not load video models — using defaults', 'error'));

  // ── Audio ─────────────────────────────────────────────────────────────────
  const audioCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(audioCard);

  const audioChk = el('input', { type: 'checkbox', id: 'fv-audio', checked: 'true' });
  audioCard.appendChild(el('div', { style: 'display:flex; gap:8px; align-items:center; margin-bottom:10px;' }, [
    audioChk,
    el('label', { for: 'fv-audio', text: 'Generate music', style: 'cursor:pointer; font-weight:600;' }),
  ]));

  const audioBody = el('div');
  audioCard.appendChild(audioBody);

  // Music prompt + AI suggest
  const musicIn = el('input', { type: 'text', style: 'flex:1;',
    placeholder: 'Music style — genre, mood, tempo, instruments (blank = AI picks from video)',
  });
  const musicSuggestBtn = el('button', { class: 'btn btn-sm', text: '✦ Suggest', title: 'AI suggests a music style from your motion prompt', style: 'flex-shrink:0;' });
  audioBody.appendChild(el('div', { style: 'margin-bottom:8px;' }, [
    el('label', { text: 'Music Prompt', style: 'display:block; font-size:.82rem; color:var(--text-3); margin-bottom:4px;' }),
    el('div', { style: 'display:flex; gap:6px;' }, [musicIn, musicSuggestBtn]),
  ]));

  // Song/instrumental — default is SONG (unchecked)
  const instrChk = el('input', { type: 'checkbox', id: 'fv-instr' });
  audioBody.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:center; margin-bottom:8px;' }, [
    instrChk,
    el('label', { for: 'fv-instr', text: 'Instrumental (no vocals)', style: 'cursor:pointer; font-size:.85rem;' }),
  ]));

  // Lyric direction (visible when not instrumental)
  const lyricGuideWrap = el('div');
  const lyricGuideTA = el('textarea', {
    rows: '2',
    placeholder: 'Lyric direction — theme, mood, subject, e.g. "uplifting, overcoming challenges, anthemic chorus"',
    style: 'width:100%; resize:vertical; font-size:.82rem;',
  });
  lyricGuideWrap.appendChild(el('div', {}, [
    el('label', { text: 'Lyric Direction', style: 'display:block; font-size:.82rem; color:var(--text-3); margin-bottom:4px;' }),
    lyricGuideTA,
  ]));
  audioBody.appendChild(lyricGuideWrap);

  function _syncLyricGuide() { lyricGuideWrap.style.display = instrChk.checked ? 'none' : ''; }
  instrChk.addEventListener('change', _syncLyricGuide);
  _syncLyricGuide();
  audioChk.addEventListener('change', () => { audioBody.style.display = audioChk.checked ? '' : 'none'; });

  // Suggest music from motion prompt text (no video yet at this point)
  musicSuggestBtn.addEventListener('click', async () => {
    musicSuggestBtn.disabled = true; musicSuggestBtn.textContent = '…';
    try {
      const hint = [promptTA.value.trim(), lyricGuideTA.value.trim()].filter(Boolean).join(' | ');
      const data = await api('/api/fun/brainstorm', {
        method: 'POST',
        body: JSON.stringify({
          message: `Suggest a music style for a video with this motion: "${hint || 'cinematic scene'}". Return a short music prompt (genre, mood, tempo, key instruments) and a lyric direction hint. Reply with JSON: {"music_prompt":"...","lyric_direction":"..."}`,
          image_path: _startImagePath || '',
          current_idea: promptTA.value.trim(),
          current_lyric: '',
        }),
      });
      // brainstorm returns reply field; parse JSON from it
      let parsed = null;
      try { parsed = JSON.parse((data.reply || '').replace(/```json|```/g, '').trim()); } catch (_) {}
      if (parsed?.music_prompt) musicIn.value = parsed.music_prompt;
      if (parsed?.lyric_direction && !instrChk.checked) lyricGuideTA.value = parsed.lyric_direction;
      else if (!parsed && data.lyric_direction) lyricGuideTA.value = data.lyric_direction;
    } catch (e) { toast(e.message || 'Suggestion failed', 'error'); }
    finally { musicSuggestBtn.disabled = false; musicSuggestBtn.textContent = '✦ Suggest'; }
  });

  // ── Real-time sync tool ────────────────────────────────────────────────────
  // Plays a muted <video> for visual + separate <audio> element offset by the
  // slider value. Adjusts in real-time while playing; "Lock In + Export" bakes
  // the offset to disk via /api/fun/sync-audio.
  function _buildSyncTool(getVideoPath, onSynced) {
    const wrap = el('div', { class: 'card', style: 'padding:12px; margin-top:8px; background:var(--surface-1);' });
    wrap.appendChild(el('div', { style: 'font-size:.8rem; font-weight:600; margin-bottom:3px; color:var(--text-2);', text: 'Audio Sync' }));
    wrap.appendChild(el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-bottom:10px;',
      text: 'Hit play, drag the slider to shift audio earlier (−) or later (+). Adjust until it locks, then export.' }));

    // Dual-element player: video is muted visually, audio plays from a shadow audio el
    const videoEl = document.createElement('video');
    videoEl.controls = true;
    videoEl.muted = true;
    videoEl.style.cssText = 'display:none; width:100%; max-height:200px; border-radius:6px; margin-bottom:8px; background:#000;';
    const audioEl = document.createElement('audio');
    audioEl.preload = 'auto';
    wrap.appendChild(videoEl);
    wrap.appendChild(audioEl);

    let _offsetMs = 0;

    function _resync() {
      const targetAudioTime = videoEl.currentTime - _offsetMs / 1000;
      if (targetAudioTime < 0) {
        audioEl.pause();
        return;
      }
      if (Math.abs(audioEl.currentTime - targetAudioTime) > 0.05) {
        audioEl.currentTime = targetAudioTime;
      }
      if (audioEl.paused && !videoEl.paused) audioEl.play().catch(() => {});
    }

    videoEl.addEventListener('play',    () => { _resync(); audioEl.play().catch(() => {}); });
    videoEl.addEventListener('pause',   () => audioEl.pause());
    videoEl.addEventListener('seeked',  () => _resync());
    videoEl.addEventListener('ended',   () => { audioEl.pause(); audioEl.currentTime = 0; });
    videoEl.addEventListener('timeupdate', _resync);

    // Slider
    const offsetLabel = el('span', { style: 'font-size:.82rem; color:var(--accent); min-width:72px; text-align:right;', text: '0 ms' });
    const offsetSlider = el('input', { type: 'range', min: '-2000', max: '2000', step: '10', value: '0', style: 'flex:1; cursor:pointer;' });
    offsetSlider.addEventListener('input', () => {
      _offsetMs = parseInt(offsetSlider.value);
      offsetLabel.textContent = `${_offsetMs > 0 ? '+' : ''}${_offsetMs} ms`;
      if (!videoEl.paused) _resync();
    });
    wrap.appendChild(el('div', { style: 'display:flex; gap:8px; align-items:center; margin-bottom:10px;' }, [offsetSlider, offsetLabel]));

    // Buttons
    const exportBtn  = el('button', { class: 'btn btn-sm btn-primary', text: 'Lock In + Export' });
    const resetBtn   = el('button', { class: 'btn btn-sm', text: 'Reset' });
    const syncStatus = el('span', { style: 'font-size:.75rem; color:var(--text-3);' });
    wrap.appendChild(el('div', { style: 'display:flex; gap:8px; align-items:center; flex-wrap:wrap;' }, [exportBtn, resetBtn, syncStatus]));

    resetBtn.addEventListener('click', () => {
      _offsetMs = 0; offsetSlider.value = '0'; offsetLabel.textContent = '0 ms';
      if (!videoEl.paused) _resync();
    });

    exportBtn.addEventListener('click', async () => {
      const videoPath = getVideoPath();
      if (!videoPath) return;
      if (_offsetMs === 0) { syncStatus.textContent = 'No offset — nothing to export'; return; }
      exportBtn.disabled = true; syncStatus.textContent = 'Baking…';
      try {
        const data = await api('/api/fun/sync-audio', {
          method: 'POST',
          body: JSON.stringify({ video_path: videoPath, offset_ms: _offsetMs }),
        });
        const url = pathToUrl(data.output);
        _offsetMs = 0; offsetSlider.value = '0'; offsetLabel.textContent = '0 ms';
        syncStatus.textContent = `Done (+${data.output.split(/[\\/]/).pop()})`;
        // Update both elements so the preview reflects the baked file
        videoEl.src = url; audioEl.src = url;
        onSynced(data.output);
      } catch (e) {
        syncStatus.textContent = e.message || 'Export failed';
        toast(e.message || 'Sync export failed', 'error');
      } finally { exportBtn.disabled = false; }
    });

    // Call this when a video is ready to preview
    wrap.load = (videoPath) => {
      const url = pathToUrl(videoPath);
      videoEl.src = url; audioEl.src = url;
      videoEl.style.display = '';
      _offsetMs = 0; offsetSlider.value = '0'; offsetLabel.textContent = '0 ms';
      syncStatus.textContent = '';
    };

    return wrap;
  }

  // ── Multi-video story ──────────────────────────────────────────────────────
  let _multiVideo  = false;
  let _multiClips  = 4;

  const multiCard = el('div', { class: 'card', style: 'padding:12px 14px;' });
  root.appendChild(multiCard);

  const multiChk = el('input', { type: 'checkbox', id: 'fv-multi-video', style: 'cursor:pointer; width:15px; height:15px; flex-shrink:0;' });
  const multiToggleRow = el('div', { style: 'display:flex; align-items:center; gap:8px; cursor:pointer;' }, [
    multiChk,
    el('label', { for: 'fv-multi-video', style: 'font-size:.85rem; font-weight:600; cursor:pointer;', text: 'Multi-video story' }),
  ]);
  multiCard.appendChild(multiToggleRow);
  multiCard.appendChild(el('div', {
    style: 'font-size:.74rem; color:var(--text-3); margin-top:4px; padding-left:23px;',
    text: 'AI writes a story arc, each clip starts from the last frame of the previous one. Music spans the whole piece.',
  }));

  const multiSettings = el('div', { style: 'display:none; flex-direction:column; gap:10px; margin-top:10px; padding-top:10px; border-top:1px solid var(--border-2);' });
  multiCard.appendChild(multiSettings);

  const clipsSlider = el('input', { type: 'range', min: '2', max: '8', step: '1', value: '4', style: 'flex:1; cursor:pointer;' });
  const clipsLabel  = el('span', { style: 'min-width:1.8rem; text-align:right; font-size:.85rem; color:var(--accent);', text: '4' });
  const totalLabel  = el('span', { style: 'font-size:.78rem; color:var(--text-3);', text: '' });

  function _refreshMultiTotal() {
    _multiClips = parseInt(clipsSlider.value);
    clipsLabel.textContent = String(_multiClips);
    const dur = parseFloat(durSlider.value) || 8;
    totalLabel.textContent = `~${_multiClips * dur}s total`;
  }
  clipsSlider.addEventListener('input', _refreshMultiTotal);
  durSlider.addEventListener('input', _refreshMultiTotal);

  multiSettings.appendChild(el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
    el('label', { style: 'font-size:.82rem; color:var(--text-3); white-space:nowrap;', text: 'Clips:' }),
    clipsSlider, clipsLabel, totalLabel,
  ]));

  multiChk.addEventListener('change', () => {
    _multiVideo = multiChk.checked;
    multiSettings.style.display = _multiVideo ? 'flex' : 'none';
    genBtn.textContent = _multiVideo ? 'Create Story' : 'Generate Video';
    _refreshMultiTotal();
  });

  // ── Generate ──────────────────────────────────────────────────────────────
  const genBtn = el('button', {
    class: 'btn btn-primary btn-generate',
    text: 'Generate Video',
    style: 'width:100%; font-size:1.1rem; padding:14px; font-weight:700;',
  });
  const fvQueueBtn = el('button', {
    class: 'btn',
    text: '+ Add to Queue',
    style: 'display:none; width:100%; margin-top:6px; font-size:.9rem;',
    title: 'Queue another generation with current settings -- runs after the active job',
  });
  root.appendChild(genBtn);
  root.appendChild(fvQueueBtn);

  const progWrap = el('div');
  root.appendChild(progWrap);
  const prog = createProgressCard(progWrap);
  prog.onCancel(async () => {
    if (_activePoller) { _activePoller.stop(); _activePoller = null; }
    if (_activeJobId) { await stopJob(_activeJobId).catch(() => {}); toast('Stopping…', 'info'); }
  });

  const vidWrap = el('div');
  root.appendChild(vidWrap);

  const resultTabBar = el('div', { class: 'result-tabs', style: 'display:none;' });
  const tabMix = el('button', { class: 'result-tab active', text: 'With Music' });
  const tabRaw = el('button', { class: 'result-tab', text: 'Raw video' });
  resultTabBar.appendChild(tabMix);
  resultTabBar.appendChild(tabRaw);
  vidWrap.appendChild(resultTabBar);

  const playerWrap = el('div');
  vidWrap.appendChild(playerWrap);
  const player = createVideoPlayer(playerWrap);

  let _rawPath = null, _mixPath = null;

  function _showResultTab(which) {
    tabMix.classList.toggle('active', which === 'mix');
    tabRaw.classList.toggle('active', which === 'raw');
    const p = which === 'mix' ? _mixPath : _rawPath;
    if (p) player.show(pathToUrl(p), p);
  }

  tabMix.addEventListener('click', () => _showResultTab('mix'));
  tabRaw.addEventListener('click', () => _showResultTab('raw'));
  player.onStartOver(() => {
    player.hide();
    resultTabBar.style.display = 'none';
    _rawPath = null; _mixPath = null;
  });

  function _buildFvPayload() {
    const m        = _models[modelSel.value] || {};
    const duration = Math.min(parseFloat(durSlider.value) || 14, m.max_sec || 20);
    return {
      photo_path:       _startImagePath,
      video_prompt:     promptTA.value.trim() || PROMPT_DEFAULT,
      music_prompt:     musicIn.value.trim(),
      model:            modelSel.value,
      duration,
      steps:            parseInt(stepsSlider.value)     || 40,
      guidance:         parseFloat(guidanceSlider.value) || 8.5,
      seed:             parseInt(seedIn.value)           || -1,
      skip_audio:       !audioChk.checked,
      instrumental:     instrChk.checked,
      lyric_direction:  instrChk.checked ? '' : lyricGuideTA.value.trim(),
      end_photo_path:   _endImagePath || null,
      start_video_path: _startVideoPath || null,
    };
  }

  fvQueueBtn.addEventListener('click', async () => {
    if (!_startImagePath && !_startVideoPath) { toast('Select an image first', 'error'); return; }
    try {
      const resp = await api('/api/fun/make-it', {
        method: 'POST',
        body: JSON.stringify(_buildFvPayload()),
      });
      document.dispatchEvent(new CustomEvent('job-queued', { detail: { job_id: resp.job_id } }));
      toast('Added to queue!', 'success');
    } catch (e) {
      toast(e.message || 'Failed to queue job', 'error');
    }
  });

  genBtn.addEventListener('click', async () => {
    if (_autoPromptAbort) { _autoPromptAbort.abort(); _autoPromptAbort = null; }
    promptStatus.style.display = 'none';

    const prompt = promptTA.value.trim() || PROMPT_DEFAULT;
    if (!promptTA.value.trim()) promptTA.value = prompt;
    if (!_startImagePath && !_startVideoPath) {
      pickerCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      pickerCard.style.outline = '2px solid var(--red)';
      setTimeout(() => { pickerCard.style.outline = ''; }, 2000);
      toast('Select an image or start video above first', 'error');
      return;
    }

    genBtn.disabled = true;
    fvQueueBtn.style.display = '';
    prog.show();
    prog.update(0, 'Submitting...');
    player.hide();
    resultTabBar.style.display = 'none';

    try {
      const base = _buildFvPayload();
      const endpoint = _multiVideo ? '/api/fun/make-it-multi' : '/api/fun/make-it';
      const payload  = _multiVideo
        ? { ...base, clip_duration: base.duration, num_clips: _multiClips, user_direction: 'cinematic narrative, story continuity, dramatic' }
        : base;
      const { job_id } = await api(endpoint, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (_activePoller) { _activePoller.stop(); _activePoller = null; }
      _activeJobId = job_id;

      _activePoller = pollJob(
        job_id,
        (j) => {
          const msg = j.message || (j.status === 'queued' ? 'Queued — waiting for GPU...' : 'Working...');
          prog.update(j.progress || 0, msg);
        },
        (j) => {
          _activePoller = null;
          prog.hide();
          genBtn.disabled = false;
          fvQueueBtn.style.display = 'none';
          _activeJobId = null;
          if (j.output) {
            const outputs = Array.isArray(j.output) ? j.output : [j.output];
            // outputs[0] is always the final video (merged with audio, or raw if no audio).
            // meta.video_path holds the silent raw WanGP video for downstream tools.
            _rawPath  = (j.meta && j.meta.video_path) || outputs[0];
            _mixPath  = outputs.length > 1 ? outputs[1] : null;
            const bestPath = _mixPath || outputs[0];

            resultTabBar.style.display = 'none';
            player.show(pathToUrl(bestPath), bestPath);

            // Redo Audio + Sync — build once, update paths on each generation
            let redoCard = vidWrap.querySelector('.redo-audio-card');
            // Load sync preview whenever we have a new mix (even on repeat generations)
            if (redoCard?._syncTool && _mixPath) redoCard._syncTool.load(_mixPath);
            if (!redoCard) {
              redoCard = el('div', { class: 'card redo-audio-card', style: 'padding:12px; margin-top:10px;' });
              redoCard.appendChild(el('div', { style: 'font-size:.8rem; font-weight:600; margin-bottom:8px; color:var(--text-2);', text: 'Redo Audio' }));

              const redoMusicIn  = el('input', { type: 'text', style: 'flex:1; font-size:.82rem;',
                placeholder: 'Music style (blank = AI picks from video)',
              });
              const redoSuggestBtn = el('button', { class: 'btn btn-sm', text: '✦ Suggest', style: 'flex-shrink:0;' });
              redoCard.appendChild(el('div', { style: 'margin-bottom:6px;' }, [
                el('label', { text: 'Music Prompt', style: 'display:block; font-size:.75rem; color:var(--text-3); margin-bottom:3px;' }),
                el('div', { style: 'display:flex; gap:6px;' }, [redoMusicIn, redoSuggestBtn]),
              ]));

              const redoInstrChk = el('input', { type: 'checkbox', id: 'fv-redo-instr' });
              const redoLyricWrap = el('div', { style: 'margin-bottom:8px;' });
              const redoLyricIn = el('textarea', { rows: '2', style: 'width:100%; font-size:.82rem; resize:vertical;',
                placeholder: 'Lyric direction (optional)',
              });
              redoLyricWrap.appendChild(el('label', { text: 'Lyric Direction', style: 'display:block; font-size:.75rem; color:var(--text-3); margin-bottom:3px;' }));
              redoLyricWrap.appendChild(redoLyricIn);

              redoCard.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:center; margin-bottom:6px;' }, [
                redoInstrChk,
                el('label', { for: 'fv-redo-instr', text: 'Instrumental', style: 'cursor:pointer; font-size:.82rem;' }),
              ]));
              redoCard.appendChild(redoLyricWrap);

              function _syncRedoLyric() { redoLyricWrap.style.display = redoInstrChk.checked ? 'none' : ''; }
              redoInstrChk.addEventListener('change', _syncRedoLyric);
              _syncRedoLyric();

              const redoBtn  = el('button', { class: 'btn btn-sm btn-primary', text: 'Regenerate Audio' });
              const redoProg = el('div', { style: 'display:none; font-size:.75rem; color:var(--accent);' });
              redoCard.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:center;' }, [redoBtn, redoProg]));

              // Sync tool for the result (uses current _mixPath / _rawPath)
              const syncTool = _buildSyncTool(
                () => _mixPath || _rawPath,
                (newPath) => {
                  _mixPath = newPath;
                  resultTabBar.style.display = 'flex';
                  _showResultTab('mix');
                  toast('Sync applied — playing adjusted video', 'success');
                },
              );
              redoCard.appendChild(syncTool);
              redoCard._syncTool = syncTool;  // store ref for load() calls below

              vidWrap.appendChild(redoCard);

              // AI suggest for redo (has real video path)
              redoSuggestBtn.addEventListener('click', async () => {
                if (!_rawPath) { toast('Generate a video first', 'info'); return; }
                redoSuggestBtn.disabled = true; redoSuggestBtn.textContent = '…';
                try {
                  const data = await api('/api/fun/suggest-music', {
                    method: 'POST',
                    body: JSON.stringify({
                      video_path: _rawPath,
                      user_direction: redoLyricIn.value.trim(),
                      instrumental: redoInstrChk.checked,
                    }),
                  });
                  if (data.music_prompt) redoMusicIn.value = data.music_prompt;
                  if (data.lyric_direction && !redoInstrChk.checked) redoLyricIn.value = data.lyric_direction;
                } catch (e) { toast(e.message || 'Suggestion failed', 'error'); }
                finally { redoSuggestBtn.disabled = false; redoSuggestBtn.textContent = '✦ Suggest'; }
              });

              redoBtn.addEventListener('click', async () => {
                if (!_rawPath) return;
                redoBtn.disabled = true; redoProg.style.display = ''; redoProg.textContent = 'Submitting…';
                try {
                  const { job_id: rid } = await api('/api/fun/add-music', {
                    method: 'POST',
                    body: JSON.stringify({
                      video_path:      _rawPath,
                      music_prompt:    redoMusicIn.value.trim(),
                      lyric_direction: redoInstrChk.checked ? '' : redoLyricIn.value.trim(),
                      instrumental:    redoInstrChk.checked,
                    }),
                  });
                  pollJob(rid,
                    (j) => { redoProg.textContent = j.message || 'Working…'; },
                    (j) => {
                      redoBtn.disabled = false; redoProg.style.display = 'none';
                      if (j.output) {
                        _mixPath = j.output;
                        resultTabBar.style.display = 'flex';
                        _showResultTab('mix');
                        syncTool.load(_mixPath);
                        toast('Audio regenerated!', 'success');
                      }
                    },
                    (err) => { redoBtn.disabled = false; redoProg.style.display = 'none'; toast(err, 'error'); },
                  );
                } catch (e) { redoBtn.disabled = false; redoProg.style.display = 'none'; toast(e.message, 'error'); }
              });
            }

            // Send to Bridges tab for sequence building
            const existing = vidWrap.querySelector('.to-seq-btn');
            const seqBtn = existing || (() => {
              const b = el('button', { class: 'btn btn-sm to-seq-btn', text: '+ Add to Transitions', style: 'margin-top:8px;' });
              vidWrap.appendChild(b);
              return b;
            })();
            seqBtn.onclick = () => {
              handoff('bridges', { type: 'video', path: bestPath, url: pathToUrl(bestPath) });
              document.querySelector('.rail-tab[data-tab="bridges"]')?.click();
              toast('Clip sent to Add Transitions tab', 'success');
            };
          } else {
            toast('Generation finished but no output file found — check server logs', 'error');
          }
        },
        (err) => {
          _activePoller = null;
          prog.hide();
          genBtn.disabled = false;
          fvQueueBtn.style.display = 'none';
          _activeJobId = null;
          toast(typeof err === 'string' ? err : (err?.message || 'Generation failed'), 'error');
        },
      );
    } catch (e) {
      prog.hide();
      genBtn.disabled = false;
      fvQueueBtn.style.display = 'none';
      toast(e.message, 'error');
    }
  });

  // ── Add Audio to Any Video ─────────────────────────────────────────────────
  // Lets the user add AI-generated music to any video — newly generated, uploaded,
  // or anything from the gallery — without running a new video generation.
  const extAudioToggleRow = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-top:4px;' });
  root.appendChild(extAudioToggleRow);
  const extAudioToggle = el('input', { type: 'checkbox', id: 'fv-ext-audio-toggle' });
  extAudioToggleRow.appendChild(extAudioToggle);
  extAudioToggleRow.appendChild(el('label', {
    for: 'fv-ext-audio-toggle',
    style: 'font-size:.82rem; color:var(--text-3); cursor:pointer; user-select:none;',
    text: 'Select video for audio addition',
  }));

  const extAudioSection = el('div', { class: 'card', style: 'display:none; padding:14px;' });
  root.appendChild(extAudioSection);
  extAudioToggle.addEventListener('change', () => {
    extAudioSection.style.display = extAudioToggle.checked ? '' : 'none';
  });

  extAudioSection.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:10px;', text: 'Add Audio to a Video' }));

  // Video picker
  let _extVideoPath = null;
  const extVideoPreview = el('video', {
    controls: 'true',
    style: 'display:none; width:100%; max-height:220px; border-radius:6px; margin-bottom:8px; background:#000;',
  });
  extAudioSection.appendChild(extVideoPreview);

  const extPickRow = el('div', { style: 'display:flex; gap:8px; margin-bottom:10px;' });
  const extFileInput = el('input', { type: 'file', accept: 'video/*', style: 'display:none;' });
  extAudioSection.appendChild(extFileInput);
  const extOpenBtn = el('button', { class: 'btn btn-sm', text: 'Browse video file…' });
  extPickRow.appendChild(extOpenBtn);
  const extFromGalleryBtn = el('button', { class: 'btn btn-sm', text: 'Pick from Recent Media' });
  extPickRow.appendChild(extFromGalleryBtn);
  extAudioSection.appendChild(extPickRow);

  const extGalleryList = el('div', { style: 'display:none; max-height:160px; overflow-y:auto; margin-bottom:10px; border:1px solid var(--border-2); border-radius:6px;' });
  extAudioSection.appendChild(extGalleryList);

  function _setExtVideo(path, url) {
    _extVideoPath = path;
    extVideoPreview.src = url || pathToUrl(path);
    extVideoPreview.style.display = '';
    extGalleryList.style.display = 'none';
  }

  extOpenBtn.addEventListener('click', () => extFileInput.click());
  extFileInput.addEventListener('change', async () => {
    if (!extFileInput.files?.length) return;
    try {
      const data = await apiUpload('/api/fun/upload-video', Array.from(extFileInput.files));
      const f = data.files?.[0];
      if (f) _setExtVideo(f.path, f.url || pathToUrl(f.path));
    } catch (e) { toast(e.message, 'error'); }
  });

  extFromGalleryBtn.addEventListener('click', async () => {
    const isVisible = extGalleryList.style.display !== 'none';
    if (isVisible) { extGalleryList.style.display = 'none'; return; }
    extGalleryList.innerHTML = '<div style="padding:8px;font-size:.8rem;color:var(--text-3);">Loading…</div>';
    extGalleryList.style.display = '';
    try {
      const data = await apiFetch('/api/gallery?limit=48');
      const videos = (data.items || []).filter(i => /\.(mp4|webm|mov)/i.test(i.url));
      extGalleryList.innerHTML = '';
      if (!videos.length) {
        extGalleryList.innerHTML = '<div style="padding:8px;font-size:.8rem;color:var(--text-3);">No videos in gallery yet</div>';
        return;
      }
      for (const v of videos) {
        const row = el('div', {
          style: 'padding:6px 10px; cursor:pointer; font-size:.8rem; border-bottom:1px solid var(--border-1);',
          text: v.url.split('/').pop().replace(/\?.*/, ''),
        });
        row.addEventListener('click', () => {
          const path = v.metadata?.path || v.url;
          _setExtVideo(path, v.url);
        });
        row.addEventListener('mouseenter', () => { row.style.background = 'var(--surface-2)'; });
        row.addEventListener('mouseleave', () => { row.style.background = ''; });
        extGalleryList.appendChild(row);
      }
    } catch (e) { extGalleryList.innerHTML = '<div style="padding:8px;font-size:.8rem;color:var(--red);">Failed to load gallery</div>'; }
  });

  // Music prompt + AI suggest
  const extMusicIn = el('input', { type: 'text', style: 'flex:1;',
    placeholder: 'Music style (blank = AI picks from video)',
  });
  const extSuggestBtn = el('button', { class: 'btn btn-sm', text: '✦ Suggest', style: 'flex-shrink:0;' });
  extAudioSection.appendChild(el('div', { style: 'margin-bottom:8px;' }, [
    el('label', { text: 'Music Prompt', style: 'display:block; font-size:.82rem; color:var(--text-3); margin-bottom:4px;' }),
    el('div', { style: 'display:flex; gap:6px;' }, [extMusicIn, extSuggestBtn]),
  ]));

  const extInstrChk = el('input', { type: 'checkbox', id: 'fv-ext-instr' });
  const extLyricWrap = el('div', { style: 'margin-bottom:10px;' });
  const extLyricTA = el('textarea', { rows: '2', style: 'width:100%; font-size:.82rem; resize:vertical;',
    placeholder: 'Lyric direction — theme, mood, subject',
  });
  extLyricWrap.appendChild(el('label', { text: 'Lyric Direction', style: 'display:block; font-size:.82rem; color:var(--text-3); margin-bottom:4px;' }));
  extLyricWrap.appendChild(extLyricTA);

  extAudioSection.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:center; margin-bottom:8px;' }, [
    extInstrChk,
    el('label', { for: 'fv-ext-instr', text: 'Instrumental (no vocals)', style: 'cursor:pointer; font-size:.85rem;' }),
  ]));
  extAudioSection.appendChild(extLyricWrap);

  function _syncExtLyric() { extLyricWrap.style.display = extInstrChk.checked ? 'none' : ''; }
  extInstrChk.addEventListener('change', _syncExtLyric);
  _syncExtLyric();

  extSuggestBtn.addEventListener('click', async () => {
    if (!_extVideoPath) { toast('Pick a video first', 'info'); return; }
    extSuggestBtn.disabled = true; extSuggestBtn.textContent = '…';
    try {
      const data = await api('/api/fun/suggest-music', {
        method: 'POST',
        body: JSON.stringify({ video_path: _extVideoPath, user_direction: extLyricTA.value.trim(), instrumental: extInstrChk.checked }),
      });
      if (data.music_prompt) extMusicIn.value = data.music_prompt;
      if (data.lyric_direction && !extInstrChk.checked) extLyricTA.value = data.lyric_direction;
    } catch (e) { toast(e.message || 'Suggestion failed', 'error'); }
    finally { extSuggestBtn.disabled = false; extSuggestBtn.textContent = '✦ Suggest'; }
  });

  const extGenBtn  = el('button', { class: 'btn btn-primary', text: 'Generate Audio', style: 'width:100%;' });
  const extProgRow = el('div', { style: 'display:none; font-size:.8rem; color:var(--accent); margin-top:6px;' });
  extAudioSection.appendChild(extGenBtn);
  extAudioSection.appendChild(extProgRow);

  // Result area for external video
  const extResultWrap = el('div', { style: 'margin-top:10px;' });
  extAudioSection.appendChild(extResultWrap);

  let _extResultPath = null;
  const extPlayer = createVideoPlayer(extResultWrap);

  // Sync tool for the external video result
  const extSyncTool = _buildSyncTool(
    () => _extResultPath,
    (newPath) => {
      _extResultPath = newPath;
      extPlayer.show(pathToUrl(newPath), newPath);
      toast('Sync applied', 'success');
    },
  );
  extSyncTool.style.display = 'none';
  extResultWrap.appendChild(extSyncTool);

  extGenBtn.addEventListener('click', async () => {
    if (!_extVideoPath) {
      toast('Pick a video to add audio to first', 'error');
      extAudioSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      return;
    }
    extGenBtn.disabled = true;
    extProgRow.style.display = '';
    extProgRow.textContent = 'Submitting…';
    try {
      const { job_id } = await api('/api/fun/add-music', {
        method: 'POST',
        body: JSON.stringify({
          video_path:      _extVideoPath,
          music_prompt:    extMusicIn.value.trim(),
          lyric_direction: extInstrChk.checked ? '' : extLyricTA.value.trim(),
          instrumental:    extInstrChk.checked,
        }),
      });
      pollJob(job_id,
        (j) => { extProgRow.textContent = j.message || 'Working…'; },
        (j) => {
          extGenBtn.disabled = false; extProgRow.style.display = 'none';
          if (j.output) {
            _extResultPath = j.output;
            extPlayer.show(pathToUrl(j.output), j.output);
            extSyncTool.style.display = '';
            extSyncTool.load(j.output);
            toast('Audio added!', 'success');
          }
        },
        (err) => { extGenBtn.disabled = false; extProgRow.style.display = 'none'; toast(err, 'error'); },
      );
    } catch (e) { extGenBtn.disabled = false; extProgRow.style.display = 'none'; toast(e.message, 'error'); }
  });

  // ── Palette AI intent ─────────────────────────────────────────────────────
  import('./shell/ai-intent.js?v=20260503h').then(({ registerTabAI }) => {
    registerTabAI('fun-videos', {
      getContext: () => ({
        prompt:       promptTA.value,
        steps:        Number(stepsSlider.value)    || 0,
        guidance:     Number(guidanceSlider.value) || 0,
        duration_sec: Number(durSlider.value)      || 0,
      }),
      applySettings: (s) => {
        if (typeof s.steps        === 'number') stepsSlider.value    = Math.max(4, Math.min(50, s.steps));
        if (typeof s.guidance     === 'number') guidanceSlider.value = Math.max(1, Math.min(20, s.guidance));
        if (typeof s.duration_sec === 'number') durSlider.value      = Math.max(2, Math.min(20, s.duration_sec));
        // Restore the original prompt (before quality suffixes were appended)
        if (typeof s.prompt === 'string' && s.prompt.trim()) {
          promptTA.value = s.prompt.trim();
        }
        if (typeof s.prompt_append === 'string' && s.prompt_append.trim()) {
          const cur = promptTA.value.trim();
          promptTA.value = cur ? `${cur}, ${s.prompt_append.trim()}` : s.prompt_append.trim();
        }
        // Restore the source image into the start drop zone
        if (typeof s.source_image === 'string' && s.source_image.trim() && _applyStart) {
          _applyStart(s.source_image, pathToUrl(s.source_image) || s.source_image);
        }
        // photo_path is the extracted last frame passed by _doContinuation
        if (typeof s.photo_path === 'string' && s.photo_path.trim() && _applyStart) {
          _applyStart(s.photo_path, pathToUrl(s.photo_path) || s.photo_path);
        }
      },
    });
  }).catch(() => {});
}
