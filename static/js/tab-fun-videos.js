/**
 * Drop Cat Go Studio -- Create Videos
 * Pick a generated image, write a motion prompt, get a video.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js?v=20260505e';
import { createProgressCard, createVideoPlayer, createSlider, el, pathToUrl } from './components.js?v=20260507a';
import { toast, apiFetch } from './shell/toast.js?v=20260518a';
import { handoff } from './handoff.js?v=20260422a';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260509a';

// Concurrency limiter for thumbnail extraction -- caps parallel <video> preloads.
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

  // -- Start image / video upload --------------------------------------------
  const uploadCard = el('div', { class: 'card', style: 'padding:14px; display:flex; align-items:center; gap:10px;' });
  root.appendChild(uploadCard);
  uploadCard.appendChild(el('span', { style: 'font-size:.85rem; font-weight:600; flex:1;', text: 'Start Image or Video' }));

  const fileInput = el('input', { type: 'file', accept: 'image/*,video/*', style: 'display:none' });
  uploadCard.appendChild(fileInput);
  const openFileBtn = el('button', { class: 'btn btn-sm', text: 'Open file...' });
  uploadCard.appendChild(openFileBtn);
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

  // -- Selected media preview -------------------------------------------------
  const previewCard = el('div', { class: 'drop-zone', style: 'display:none; position:relative; overflow:hidden; padding:0;' });
  root.appendChild(previewCard);

  const previewImg   = el('img',   { style: 'display:none; width:100%; max-height:260px; object-fit:contain; border-radius:8px; background:var(--bg-raised);' });
  const previewVid   = el('video', { controls: '', style: 'display:none; width:100%; max-height:260px; border-radius:8px; background:#000;' });
  const previewClear = el('button', {
    style: 'position:absolute; top:6px; right:6px; width:24px; height:24px; border-radius:50%; border:none; background:rgba(0,0,0,.65); color:#fff; font-size:15px; line-height:1; cursor:pointer; z-index:2; padding:0;',
    title: 'Clear', text: 'x',
  });
  previewCard.appendChild(previewImg);
  previewCard.appendChild(previewVid);
  previewCard.appendChild(previewClear);

  previewImg.addEventListener('error', () => {
    previewImg.style.display = 'none';
    previewImg.src = '';
    _startImagePath = null;
    previewCard.style.display = 'none';
  });

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
    // Un-check video toggle
    videoChk.checked = false;
    videoCard.style.display = 'none';
    _showVideoMode(false);
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
    _showVideoMode(false);
    // Auto-select the ratio that best matches the uploaded image dimensions
    previewImg.onload = () => {
      const w = previewImg.naturalWidth, h = previewImg.naturalHeight;
      if (!w || !h) return;
      const supported = FV_MODEL_RATIOS[modelSel.value] || ['16:9'];
      let best = '16:9', bestDiff = Infinity;
      for (const r of FV_RATIOS) {
        if (!supported.includes(r.value)) continue;
        const diff = Math.abs(w / h - r.rw / r.rh);
        if (diff < bestDiff) { bestDiff = diff; best = r.value; }
      }
      if (best !== _fvRatio) {
        _fvRatio = best;
        _updateFvRatioAvailability();
        _computeFvDims();
      }
    };
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
    _showVideoMode(true);
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

  // NOTE: _pendingHandoff drain is below, AFTER the extended _applyStart is
  // installed (line ~432). If drained here, only the base _applyStart runs --
  // auto-prompt generation would be skipped on the first cross-tab navigation.

  // -- Start video (video-to-video) ------------------------------------------
  let _startVideoPath = null;
  let _startVideoSeekSeconds = null;
  let _videoMode = 'continuation'; // 'continuation' | 'inspired'
  const videoToggleRow = el('div', { style: 'display:flex; align-items:center; gap:8px;' });
  root.appendChild(videoToggleRow);
  const videoChk = el('input', { type: 'checkbox', id: 'fv-video-toggle' });
  videoToggleRow.appendChild(videoChk);
  videoToggleRow.appendChild(el('label', { for: 'fv-video-toggle', style: 'font-size:.82rem; color:var(--text-3); cursor:pointer; user-select:none;', text: '+ Start video -- use a video as the source instead of an image (video-to-video)' }));

  const videoCard = el('div', { class: 'card', style: 'display:none; padding:12px;' });
  root.appendChild(videoCard);

  const videoFileInput = el('input', { type: 'file', accept: 'video/*', style: 'display:none' });
  videoCard.appendChild(videoFileInput);
  const videoOpenBtn = el('button', { class: 'btn btn-sm', text: 'Choose video...' });
  const videoClearBtn = el('button', { class: 'btn btn-sm', text: 'x Clear', style: 'display:none;' });
  const videoName = el('div', { style: 'font-size:.78rem; color:var(--text-2); margin-top:6px;' });
  videoCard.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:center;' }, [videoOpenBtn, videoClearBtn]));
  videoCard.appendChild(videoName);

  // -- Frame picker (shown after video upload, continuation mode only) -------
  const videoPlayer = el('video', {
    style: 'display:none; width:100%; max-height:160px; border-radius:6px; margin-top:8px; background:#000;',
    controls: true,
    preload: 'metadata',
  });
  videoCard.appendChild(videoPlayer);

  const framePickerRow = el('div', { style: 'display:none; align-items:center; gap:8px; margin-top:6px;' });
  const useFrameBtn = el('button', { class: 'btn btn-sm', text: 'Use this frame', style: 'font-size:.75rem;' });
  const frameLabel = el('span', { style: 'font-size:.74rem; color:var(--text-3);' });
  framePickerRow.appendChild(useFrameBtn);
  framePickerRow.appendChild(frameLabel);
  videoCard.appendChild(framePickerRow);

  const framePreviewRow = el('div', { style: 'display:none; align-items:center; gap:8px; margin-top:6px;' });
  const framePreviewImg = el('img', { style: 'width:80px; height:52px; object-fit:cover; border-radius:4px; border:1px solid var(--border-2);' });
  const framePreviewLabel = el('span', { style: 'font-size:.74rem; color:var(--text-2);' });
  framePreviewRow.appendChild(framePreviewImg);
  framePreviewRow.appendChild(framePreviewLabel);
  videoCard.appendChild(framePreviewRow);

  const LONG_VIDEO_SECS = 45; // above this, prompt user to pick a moment

  function _fmt(s) {
    const m = Math.floor(s / 60);
    const sec = (s % 60).toFixed(1).padStart(4, '0');
    return `${m}:${sec}`;
  }

  // Fetch + display a frame preview. Pass t=null for the backend default (85%).
  async function _setPreviewFrame(t) {
    if (!_startVideoPath) return;
    const capturedPath = _startVideoPath;
    const url = t != null
      ? `/api/fun/video-frame?path=${encodeURIComponent(_startVideoPath)}&t=${t}`
      : `/api/fun/video-frame?path=${encodeURIComponent(_startVideoPath)}`;
    framePreviewLabel.textContent = t != null ? `Loading frame at ${_fmt(t)}...` : 'Selecting start frame...';
    framePreviewRow.style.display = 'flex';
    try {
      const resp = await fetch(url);
      if (!resp.ok || _startVideoPath !== capturedPath) return;
      const blob = await resp.blob();
      if (_startVideoPath !== capturedPath) return;
      framePreviewImg.src = URL.createObjectURL(blob);
      if (t != null) {
        framePreviewLabel.textContent = `Continuation starts at ${_fmt(t)}`;
      } else {
        const dur = videoPlayer.duration || 0;
        if (dur > LONG_VIDEO_SECS) {
          framePreviewLabel.textContent = `Default shown -- scrub the video to the scene you want and click "Use this frame"`;
        } else {
          framePreviewLabel.textContent = `Auto-selected from your clip -- scrub to change if needed`;
        }
      }
    } catch (_e) {
      if (_startVideoPath === capturedPath) framePreviewLabel.textContent = 'Preview unavailable';
    }
  }

  // Auto-preview fires as soon as the video metadata is known -- zero clicks for the novice.
  videoPlayer.addEventListener('loadedmetadata', () => {
    if (!_startVideoPath) return;
    // Seek the player to the default frame so player + thumbnail are in sync.
    videoPlayer.currentTime = videoPlayer.duration * 0.85;
    const dur = videoPlayer.duration || 0;
    if (dur > LONG_VIDEO_SECS) {
      // Long video: surface the picker hint prominently before fetching preview.
      frameLabel.textContent = `Long video (${_fmt(dur)}) -- scrub to the scene you want, then click "Use this frame".`;
    } else {
      frameLabel.textContent = 'Scrub to change the start point if needed.';
    }
    _setPreviewFrame(null); // null = backend default (85%)
  });

  useFrameBtn.addEventListener('click', async () => {
    if (!_startVideoPath || !videoPlayer.src) return;
    const t = videoPlayer.currentTime;
    _startVideoSeekSeconds = t;
    await _setPreviewFrame(t);
  });

  function _showFramePicker(visible) {
    videoPlayer.style.display = visible ? '' : 'none';
    framePickerRow.style.display = visible ? 'flex' : 'none';
    if (!visible) {
      framePreviewRow.style.display = 'none';
      if (framePreviewImg.src) { URL.revokeObjectURL(framePreviewImg.src); framePreviewImg.src = ''; }
      _startVideoSeekSeconds = null;
    }
  }

  // Video mode toggle -- shown once a video is loaded
  const _vmBtnBase = 'border:1px solid var(--border-2); border-radius:5px; padding:3px 10px; font-size:.75rem; cursor:pointer; background:transparent; color:var(--text-2); transition:background .15s,color .15s;';
  const _vmBtnOn   = 'background:var(--accent); border-color:var(--accent); color:#000; font-weight:600;';
  const vmBtnCont  = el('button', { style: _vmBtnBase + _vmBtnOn, text: 'Continue', title: 'Continue the video -- AI picks up from the last frame and the original video is prepended to the output' });
  const vmBtnInsp  = el('button', { style: _vmBtnBase, text: 'Inspired by', title: 'Use the video as creative inspiration -- AI generates new clips in the same world, no stitching' });
  function _setVideoMode(mode) {
    _videoMode = mode;
    vmBtnCont.setAttribute('style', _vmBtnBase + (mode === 'continuation' ? _vmBtnOn : ''));
    vmBtnInsp.setAttribute('style', _vmBtnBase + (mode === 'inspired'     ? _vmBtnOn : ''));
    _showFramePicker(mode === 'continuation' && !!_startVideoPath);
  }
  vmBtnCont.addEventListener('click', () => _setVideoMode('continuation'));
  vmBtnInsp.addEventListener('click', () => _setVideoMode('inspired'));
  const videoModeRow = el('div', {
    style: 'display:none; align-items:center; gap:6px; margin-top:8px; padding-top:8px; border-top:1px solid var(--border-2);',
  }, [
    el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'Mode:' }),
    vmBtnCont, vmBtnInsp,
  ]);
  videoCard.appendChild(videoModeRow);

  function _showVideoMode(visible) {
    videoModeRow.style.display = visible ? 'flex' : 'none';
    if (!visible) _setVideoMode('continuation');
  }

  videoOpenBtn.addEventListener('click', () => videoFileInput.click());
  videoFileInput.addEventListener('change', async () => {
    if (!videoFileInput.files?.length) return;
    try {
      const data = await apiUpload('/api/fun/upload-video', Array.from(videoFileInput.files));
      const f = data.files?.[0];
      if (f) {
        _startVideoPath = f.path;
        _startVideoSeekSeconds = null;
        _videoFramePath = null;
        videoName.textContent = f.name;
        videoClearBtn.style.display = '';
        _showVideoMode(true);
        const vUrl = f.url || pathToUrl(f.path);
        videoPlayer.src = vUrl;
        _showFramePicker(true);
        _videoThumb(vUrl).then(async (dataUrl) => {
          if (!dataUrl || _startVideoPath !== f.path) return;
          try {
            const blob = await fetch(dataUrl).then(r => r.blob());
            const frameFile = new File([blob], 'frame.jpg', { type: 'image/jpeg' });
            const up = await apiUpload('/api/fun/upload', [frameFile]);
            const fr = up.files?.[0];
            if (fr && _startVideoPath === f.path) _videoFramePath = fr.path;
          } catch (_) {}
        });
      }
    } catch (e) { toast(e.message, 'error'); }
    videoFileInput.value = '';
  });
  videoClearBtn.addEventListener('click', () => {
    _startVideoPath = null; _startVideoSeekSeconds = null;
    videoName.textContent = ''; videoClearBtn.style.display = 'none';
    videoPlayer.src = ''; _showFramePicker(false);
    _showVideoMode(false);
  });
  videoChk.addEventListener('change', () => {
    videoCard.style.display = videoChk.checked ? '' : 'none';
    if (!videoChk.checked) {
      _startVideoPath = null; _startVideoSeekSeconds = null;
      videoName.textContent = ''; videoClearBtn.style.display = 'none';
      videoPlayer.src = ''; _showFramePicker(false);
      _showVideoMode(false);
    }
  });

  // -- End image (optional morph) --------------------------------------------
  const endToggleRow = el('div', { style: 'display:flex; align-items:center; gap:8px;' });
  root.appendChild(endToggleRow);

  const endChk = el('input', { type: 'checkbox', id: 'fv-end-toggle' });
  const endToggleLabel = el('label', { for: 'fv-end-toggle', style: 'font-size:.82rem; color:var(--text-3); cursor:pointer; user-select:none;',
    text: '+ End image -- morph from start to end' });
  endToggleRow.appendChild(endChk);
  endToggleRow.appendChild(endToggleLabel);

  const endCard = el('div', { class: 'card', style: 'display:none; padding:14px;' });
  root.appendChild(endCard);

  endCard.appendChild(el('div', { style: 'font-size:.82rem; font-weight:600; margin-bottom:2px; color:var(--text-2);', text: 'End Image' }));
  endCard.appendChild(el('div', { style: 'font-size:.74rem; color:var(--text-3); margin-bottom:8px;', text: 'Video morphs from your selected image into this one.' }));

  const endPreview = el('img', { style: 'display:none; width:120px; height:80px; object-fit:cover; border-radius:6px; margin-bottom:6px;' });
  endCard.appendChild(endPreview);
  const endClearBtn = el('button', { class: 'btn btn-sm', text: 'x Clear end image', style: 'display:none; font-size:.72rem; margin-bottom:8px;',
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

  // -- Motion prompt ---------------------------------------------------------
  const promptCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(promptCard);
  promptCard.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:4px;', text: 'Video Prompt' }));
  promptCard.appendChild(el('div', { style: 'font-size:.74rem; color:var(--text-3); margin-bottom:8px;',
    text: 'Describe the action -- what does the subject DO? Camera moves are secondary.' }));
  const PROMPT_PLACEHOLDER = 'e.g. "Throws head back laughing, hair whipping sideways, hands clap wildly, energy radiates outward, camera pulls back to reveal full burst of motion"';
  const PROMPT_DEFAULT     = 'Subject erupts into motion, hair and clothes responding to sudden energy, arms move expressively, dynamic action fills every corner of the frame';
  const promptTA = el('textarea', { rows: '3', style: 'width:100%; resize:vertical; font-size:.9rem;',
    placeholder: PROMPT_PLACEHOLDER });
  promptCard.appendChild(promptTA);

  const promptSpinner = el('span', { style: 'display:inline-block; width:10px; height:10px; border:2px solid var(--accent); border-top-color:transparent; border-radius:50%; animation:spin .7s linear infinite; flex-shrink:0;' });
  const promptStatusMsg = el('span', { text: 'Generating video prompt from image...' });
  const promptStatus = el('div', {
    style: 'display:none; font-size:.75rem; color:var(--accent); margin-top:5px; align-items:center; gap:6px;',
  }, [
    promptSpinner,
    promptStatusMsg,
    el('span', { style: 'color:var(--text-3);', text: '-- or just click Generate to skip' }),
  ]);
  promptCard.appendChild(promptStatus);

  // -- Create Story row ------------------------------------------------------
  // Per-model prompt hints shown below the textarea.
  // Keys: "<model>|<motion>" -- fallback "<model>|dynamic" then "default".
  const PROMPT_GUIDES = {
    'LTX-2 Dev19B Distilled|calm': {
      placeholder: 'e.g. "A woman stands at a foggy harbor at dawn, breathes slowly. Mist drifts across still water. Fishing boats rest in the background. Static shot, fixed camera."',
      hint:        'LTX Calm -- subject stays still, environment moves (mist, light, fabric, steam). 40-60 words. No action verbs on the subject.',
    },
    'LTX-2 Dev19B Distilled|gentle': {
      placeholder: 'e.g. "A man in a grey coat tilts his head toward the window, eyes closing slowly. Curtains shift in a soft breeze. Warm afternoon light falls across his face. Fixed camera."',
      hint:        'LTX Gentle -- ONE subtle subject gesture (head turn, exhale, hand lift) + ONE environmental motion. 45-65 words. No rapid actions.',
    },
    'LTX-2 Dev19B Distilled|narrative': {
      placeholder: 'e.g. "A detective slides a photograph across the desk toward her partner, eyes locked on his reaction. Rain taps the grimy window behind them. Camera pushes in slowly."',
      hint:        'LTX Narrative -- purposeful action with story weight (picking up object, turning to face someone). 50-70 words. Slow push-in or static camera.',
    },
    'LTX-2 Dev13B|dynamic': {
      placeholder: 'e.g. "Red-haired woman in black jacket, backlit by streetlights. Pivots sharply -- jacket flares wide, hair whips across her face. Wet cobblestones reflect orange neon. Slow dolly-in."',
      hint:        'LTX 13B Dynamic -- start with exact visual markers (hair, clothing, light). ONE kinetic action. ONE scene anchor. ONE camera move. 45-65 words.',
    },
    'Wan2.1-I2V-14B-480P|dynamic': {
      placeholder: 'e.g. "Sprints full speed down a rain-soaked alley, dark hoodie plastered to skin, sneakers slapping wet asphalt. Camera tracks close behind at shoulder height, barely keeping pace. Puddles explode underfoot. Photorealistic, smooth motion, high quality."',
      hint:        'Wan I2V 480P -- start with a STRONG ACTION VERB. Re-state key visual markers. ONE camera move that reacts to the action. 80-100 words.',
    },
    'Wan2.1-I2V-14B-720P|dynamic': {
      placeholder: 'e.g. "Leaps from rooftop to rooftop at dusk, tailored coat catching wind, polished boots striking concrete. Camera matches height, panning right as figure lands. City lights smear into streaks below. Fine fabric detail, cinematic 720P, smooth motion."',
      hint:        'Wan I2V 720P -- same as 480P but add fine textural detail (fabric weave, skin, specific light quality). 80-100 words. No dolly-out.',
    },
    'Wan2.1-T2V-14B|dynamic': {
      placeholder: 'e.g. "Late afternoon, rain-slicked city street, neon signs bleeding orange into puddles. A young woman in a yellow raincoat steps off the curb and opens an umbrella. Camera dollies in slowly as she walks away. Photorealistic, smooth motion, cinematic color grade."',
      hint:        'Wan T2V 14B -- no image, describe EVERYTHING: time of day + setting + subject appearance + action + camera move. 80-100 words.',
    },
    'Wan2.1-T2V-1.3B|dynamic': {
      placeholder: 'e.g. "A cat sits on a sunlit windowsill and stretches lazily. Dust floats in the warm light. Static shot. Smooth motion, high quality."',
      hint:        'Wan T2V 1.3B (fast) -- keep it simple: ONE subject, ONE action, ONE setting. 50-70 words max.',
    },
  };

  // Dynamic hint text shown below prompt textarea.
  const promptHint = el('div', {
    style: 'font-size:.72rem; color:var(--text-3); margin-top:4px; line-height:1.5;',
  });
  promptCard.appendChild(promptHint);

  // "Enhance" button -- calls /api/fun/enhance-prompt to rewrite current text.
  const enhanceBtn = el('button', {
    class: 'btn btn-sm',
    text: 'Enhance',
    title: 'AI rewrites your rough idea as a model-appropriate prompt',
    style: 'flex-shrink:0;',
  });

  const storyRow = el('div', { style: 'display:flex; gap:6px; align-items:center; margin-top:8px;' });
  const storyBtn = el('button', { class: 'btn btn-sm btn-primary', text: '* Create Story', title: 'Generate a motion prompt from your image using AI' });
  storyRow.appendChild(storyBtn);
  storyRow.appendChild(enhanceBtn);
  promptCard.appendChild(storyRow);

  function _updatePromptGuide(modelName, motionStyle) {
    const key = modelName + '|' + (motionStyle || 'dynamic');
    const dyn = modelName + '|dynamic';
    const guide = PROMPT_GUIDES[key] || PROMPT_GUIDES[dyn] || null;
    if (guide) {
      promptTA.placeholder = guide.placeholder;
      promptHint.textContent = guide.hint;
      promptHint.style.display = '';
    } else {
      promptTA.placeholder = PROMPT_PLACEHOLDER;
      promptHint.textContent = '';
      promptHint.style.display = 'none';
    }
  }

  enhanceBtn.addEventListener('click', async () => {
    const raw = promptTA.value.trim();
    if (!raw) { toast('Type a rough idea first, then click Enhance', 'warning'); return; }
    enhanceBtn.disabled = true;
    enhanceBtn.textContent = '...';
    try {
      const r = await apiFetch('/api/fun/enhance-prompt', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: raw, model: modelSel.value, motion: _motionStyle }),
      });
      const data = await r.json();
      if (data.prompt) {
        promptTA.value = data.prompt;
        promptTA.dispatchEvent(new Event('input'));
      }
    } catch (e) {
      toast('Enhance failed: ' + (e.message || e), 'error');
    } finally {
      enhanceBtn.disabled = false;
      enhanceBtn.textContent = 'Enhance';
    }
  });

  let _autoPromptAbort = null;

  // Auto-generate motion prompt from the selected image via LLM vision.
  // force=true: regenerates even if prompt textarea already has content.
  async function _autoGeneratePrompt(imagePath, force = false) {
    if (!force && promptTA.value.trim()) return;
    // Don't fire Ollama while a video job is running -- it competes for VRAM.
    // The user can still click "Create Story" manually (force=true bypasses this).
    if (!force && _activeJobId) return;
    if (_autoPromptAbort) _autoPromptAbort.abort();
    _autoPromptAbort = new AbortController();
    const myAbort = _autoPromptAbort;
    const { signal } = myAbort;

    promptSpinner.style.display = '';
    promptStatusMsg.textContent = 'Generating motion prompt from image...';
    promptStatus.style.cssText = 'display:flex; font-size:.75rem; color:var(--accent); margin-top:5px; align-items:center; gap:6px;';
    storyBtn.disabled = true;
    storyBtn.textContent = '...';

    // Safety timeout -- Ollama vision cold-start can take 30s+; after 45s give up,
    // populate the textarea with the default prompt so the user has something
    // usable and Generate doesn't fail silently with an empty prompt.
    let _timedOut = false;
    const _fallback = () => {
      if (!promptTA.value.trim()) promptTA.value = PROMPT_DEFAULT;
    };
    const timeout = setTimeout(() => {
      _timedOut = true;
      _autoPromptAbort?.abort();
      _fallback();
      promptSpinner.style.display = 'none';
      promptStatusMsg.textContent = 'AI video prompt timed out -- using default. Edit it or click Create Story to retry.';
      promptStatus.style.cssText = 'display:flex; font-size:.75rem; color:var(--text-3); margin-top:5px; align-items:center; gap:6px;';
      storyBtn.disabled = false;
      storyBtn.textContent = '* Create Story';
    }, 45000);

    try {
      const data = await apiFetch('/api/fun/generate-prompts', {
        method: 'POST',
        body: JSON.stringify({ image_path: imagePath, num_prompts: 1, creativity: 7, max_tokens: 400 }),
        signal,
      });
      const prompts = data.prompts || [];
      const text = typeof prompts[0] === 'string' ? prompts[0] : prompts[0]?.prompt;
      if (text) {
        promptTA.value = text;
      } else {
        // API returned but no usable prompt (e.g. qwen3-vl thinking-block junk). Fall back.
        _fallback();
        if (!_timedOut) {
          promptStatusMsg.textContent = 'AI returned no prompt -- using default. Edit it or click Create Story to retry.';
          promptStatus.style.cssText = 'display:flex; font-size:.75rem; color:var(--text-3); margin-top:5px; align-items:center; gap:6px;';
          _timedOut = true;  // re-use the post-fallback message path
        }
      }
    } catch (e) {
      if (e?.name !== 'AbortError') {
        _fallback();
        toast(e.message || 'Story generation failed -- using default prompt', 'warn');
      }
    } finally {
      clearTimeout(timeout);
      if (_autoPromptAbort === myAbort) {
        if (!_timedOut) {
          promptStatus.style.display = 'none';
          promptSpinner.style.display = '';
          promptStatusMsg.textContent = 'Generating motion prompt from image...';
        }
        storyBtn.disabled = false;
        storyBtn.textContent = '* Create Story';
        _autoPromptAbort = null;
      }
    }
  }

  // Extend _applyStart (defined above) to trigger auto-prompt
  const _applyStartBase = _applyStart;
  _applyStart = (path, url) => {
    _applyStartBase(path, url);
    _autoGeneratePrompt(path);
    refineRow.style.display = 'flex';
  };

  // Drain any handoff that arrived before this tab was initialized.
  // MUST be here -- AFTER the extended _applyStart above is installed -- so
  // cross-tab image navigation correctly triggers auto-prompt generation.
  if (_pendingHandoff) {
    const ph = _pendingHandoff; _pendingHandoff = null;
    receiveHandoff(ph);
  }

  storyBtn.addEventListener('click', () => {
    const visionPath = _startImagePath || _videoFramePath;
    if (!visionPath && !_startVideoPath) { toast('Select an image or video first', 'error'); return; }
    if (!visionPath) {
      toast('Frame still extracting from video -- try again in a moment', 'info');
      return;
    }
    _autoGeneratePrompt(visionPath, true);
  });

  // -- Prompt refinement row --------------------------------------------------
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

  // -- Settings --------------------------------------------------------------
  const settingsCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(settingsCard);
  settingsCard.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:12px;', text: 'Settings' }));

  const topGrid = el('div', { style: 'display:grid; grid-template-columns:2fr 1fr; gap:10px; margin-bottom:10px;' });
  settingsCard.appendChild(topGrid);

  const modelSel  = el('select', { style: 'width:100%;' });
  const modelInfo = el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-top:3px;' });
  // Auto-pick toggle. Default OFF here -- advanced users have a model dropdown
  // because they want to choose. When toggled on, server classifies the idea
  // and overrides the model; the dropdown greys out so the source of truth is
  // visually clear.
  let _autoPick = false;
  const autoPickChk = el('input', {
    type: 'checkbox', id: 'fv-auto-pick',
    style: 'cursor:pointer; width:14px; height:14px; flex-shrink:0;',
  });
  const autoPickRow = el('label', {
    for: 'fv-auto-pick',
    style: 'display:flex; align-items:center; gap:6px; cursor:pointer; font-size:.72rem; color:var(--text-2); margin-top:4px;',
    title: 'Let AI pick the best model for your idea. Off by default so your selection wins.',
  }, [
    autoPickChk,
    el('span', { text: 'Auto-pick model from idea' }),
  ]);
  function _applyFvAutoPickState() {
    modelSel.disabled = _autoPick;
    modelSel.style.opacity = _autoPick ? '0.4' : '1';
    modelSel.title = _autoPick ? 'Turn off Auto-pick to choose manually' : '';
  }
  autoPickChk.addEventListener('change', () => {
    _autoPick = autoPickChk.checked;
    _applyFvAutoPickState();
  });
  topGrid.appendChild(el('div', {}, [
    el('label', { text: 'Model', style: 'display:block; font-size:.82rem; color:var(--text-3); margin-bottom:4px;' }),
    modelSel, modelInfo, autoPickRow,
  ]));

  const seedIn = el('input', { type: 'number', value: '-1', style: 'width:100%;' });
  topGrid.appendChild(el('div', {}, [
    el('label', { text: 'Seed (-1 = random)', style: 'display:block; font-size:.82rem; color:var(--text-3); margin-bottom:4px;' }),
    seedIn,
  ]));

  const durSlider = createSlider(settingsCard, { label: 'Duration (seconds)', min: 2, max: 20, step: 1, value: 5 });

  // -- Aspect ratio + resolution ---------------------------------------------
  const FV_RATIOS = [
    { label: '16:9', value: '16:9', rw: 16, rh: 9  },
    { label: '9:16', value: '9:16', rw: 9,  rh: 16 },
    { label: '1:1',  value: '1:1',  rw: 1,  rh: 1  },
    { label: '4:3',  value: '4:3',  rw: 4,  rh: 3  },
    { label: '3:4',  value: '3:4',  rw: 3,  rh: 4  },
  ];
  // Models not listed here are assumed 16:9 only (Wan-based)
  const FV_MODEL_RATIOS = {
    'LTX-2 Dev19B Distilled': ['16:9', '9:16', '1:1', '4:3', '3:4'],
    'LTX-2 Dev13B':           ['16:9', '9:16', '1:1', '4:3', '3:4'],
  };
  const FV_CHIP_BASE     = 'border:1px solid var(--border-2); border-radius:6px; padding:4px 10px; font-size:.78rem; cursor:pointer; background:transparent; color:var(--text-2); transition:background .15s,color .15s;';
  const FV_CHIP_ON       = 'background:var(--accent); border-color:var(--accent); color:#000; font-weight:600;';
  const FV_CHIP_DISABLED = 'opacity:.3; cursor:not-allowed; pointer-events:none;';

  let _fvRatio  = '16:9';
  let _fvQualPx = 480;
  let _fvOutW   = 864;
  let _fvOutH   = 480;
  let _fvDimsLabel = null;
  const _fvRatioChips = {};

  function _computeFvDims() {
    const [rw, rh] = _fvRatio.split(':').map(Number);
    let w, h;
    const px = _fvQualPx;
    if (rw === rh)     { w = h = px; }
    else if (rh > rw)  { w = px; h = Math.round(px * rh / rw); }
    else               { h = px; w = Math.round(px * rw / rh); }
    const r32 = n => Math.max(32, Math.round(n / 32) * 32);
    _fvOutW = r32(w); _fvOutH = r32(h);
    if (_fvDimsLabel) _fvDimsLabel.textContent = `${_fvOutW} x ${_fvOutH}`;
  }

  function _updateFvRatioAvailability() {
    const supported = FV_MODEL_RATIOS[modelSel.value] || ['16:9'];
    for (const [val, btn] of Object.entries(_fvRatioChips)) {
      const ok       = supported.includes(val);
      const isActive = val === _fvRatio;
      btn.setAttribute('style', FV_CHIP_BASE + (isActive && ok ? FV_CHIP_ON : '') + (!ok ? FV_CHIP_DISABLED : ''));
      if (!ok && isActive) {
        _fvRatio = '16:9';
        _fvRatioChips['16:9']?.setAttribute('style', FV_CHIP_BASE + FV_CHIP_ON);
        _computeFvDims();
      }
    }
  }

  const fvRatioRow = el('div', { style: 'display:flex; gap:6px; flex-wrap:wrap;' });
  for (const r of FV_RATIOS) {
    const btn = el('button', { style: FV_CHIP_BASE + (r.value === _fvRatio ? FV_CHIP_ON : ''), text: r.label });
    btn.addEventListener('click', () => {
      _fvRatio = r.value;
      _updateFvRatioAvailability();
      _computeFvDims();
    });
    _fvRatioChips[r.value] = btn;
    fvRatioRow.appendChild(btn);
  }

  _fvDimsLabel = el('span', { style: 'font-size:.82rem; color:var(--accent); font-weight:600;', text: '864 x 480' });

  settingsCard.appendChild(el('div', { style: 'display:flex; align-items:center; gap:10px; margin-top:8px; flex-wrap:wrap;' }, [
    el('div', { style: 'font-size:.78rem; color:var(--text-3); min-width:80px; flex-shrink:0;', text: 'Aspect ratio' }),
    fvRatioRow,
  ]));
  settingsCard.appendChild(el('div', { style: 'display:flex; align-items:center; gap:6px; padding-top:4px;' }, [
    el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'Output:' }),
    _fvDimsLabel,
  ]));

  const slidersGrid = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:10px;' });
  settingsCard.appendChild(slidersGrid);
  const stepsSlider    = createSlider(slidersGrid, { label: 'Steps',    min: 4,  max: 50,  step: 1,   value: 25  });
  const guidanceSlider = createSlider(slidersGrid, { label: 'Guidance', min: 1,  max: 20,  step: 0.5, value: 4.5 });

  function _applyFvModelDefaults(m) {
    if (!m) return;
    if (m.res && m.res[1]) _fvQualPx = m.res[1];
    // Clip count and duration -- per-model sweet spot
    if (m.default_clips != null) { clipsSlider.value = m.default_clips; _refreshMultiTotal(); }
    const maxSec = m.max_sec || 20;
    const durInput = durSlider.el.querySelector('input');
    if (durInput) { durInput.max = String(maxSec); }
    // Cap to model max first, then apply per-model default (order matters)
    durSlider.value = Math.min(durSlider.value || 8, maxSec);
    if (m.default_dur != null) { durSlider.value = Math.min(m.default_dur, maxSec); _refreshMultiTotal(); }
    if (m.steps    != null) stepsSlider.value    = m.steps;
    if (m.guidance != null) guidanceSlider.value = m.guidance;
    modelInfo.textContent = `Max ${maxSec}s  --  ${m.res ? m.res[0]+'x'+m.res[1] : ''}  --  ${m.fps || '?'}fps`;
    _updateFvRatioAvailability();
    _computeFvDims();
    // Motion style from model definition -- no string guessing
    if (typeof _setMotionStyle === 'function') {
      _setMotionStyle(m.motion || 'dynamic');
    }
    _updatePromptGuide(modelSel.value, _motionStyle);
  }

  modelSel.addEventListener('change', () => {
    _applyFvModelDefaults(_models[modelSel.value]);
  });

  api('/api/fun/models').then(data => {
    _models = data.models || {};
    const gpuVram = data.gpu_vram_gb || 0;
    modelSel.innerHTML = '';
    for (const [name, info] of Object.entries(_models)) {
      const opt = el('option', { value: name });
      const needs = info.vram_min_gb || 0;
      const fits = !gpuVram || gpuVram >= needs;
      // Append a VRAM badge so users know at a glance if their GPU can run it
      opt.textContent = fits
        ? name
        : `${name}  [needs ${needs} GB]`;
      if (!fits) opt.style.color = '#e88';
      modelSel.appendChild(opt);
    }
    const preferred = data.default || Object.keys(_models).find(k => k.includes('Wan2.1-I2V-14B-480P')) || Object.keys(_models)[0];
    if (preferred && _models[preferred]) modelSel.value = preferred;
    modelSel.dispatchEvent(new Event('change'));
  }).catch(() => toast('Could not load video models -- using defaults', 'error'));

  // -- Audio -----------------------------------------------------------------
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
    placeholder: 'Music style -- genre, mood, tempo, instruments (blank = AI picks from video)',
  });
  const musicSuggestBtn = el('button', { class: 'btn btn-sm', text: '* Suggest', title: 'AI suggests a music style from your motion prompt', style: 'flex-shrink:0;' });
  audioBody.appendChild(el('div', { style: 'margin-bottom:8px;' }, [
    el('label', { text: 'Music Prompt', style: 'display:block; font-size:.82rem; color:var(--text-3); margin-bottom:4px;' }),
    el('div', { style: 'display:flex; gap:6px;' }, [musicIn, musicSuggestBtn]),
  ]));

  // Song/instrumental -- default is SONG (unchecked)
  const instrChk = el('input', { type: 'checkbox', id: 'fv-instr' });
  audioBody.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:center; margin-bottom:8px;' }, [
    instrChk,
    el('label', { for: 'fv-instr', text: 'Instrumental (no vocals)', style: 'cursor:pointer; font-size:.85rem;' }),
  ]));

  // Lip sync -- on by default; drives subject mouth/face motion from audio waveform
  const lipSyncChk = el('input', { type: 'checkbox', id: 'fv-lip-sync', checked: 'true' });
  audioBody.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:center; margin-bottom:8px;' }, [
    lipSyncChk,
    el('label', { for: 'fv-lip-sync', text: 'Lip Sync (audio drives subject mouth/face motion)', style: 'cursor:pointer; font-size:.85rem;' }),
  ]));

  // Lyric direction (visible when not instrumental)
  const lyricGuideWrap = el('div');
  const lyricGuideTA = el('textarea', {
    rows: '2',
    placeholder: 'Lyric direction -- theme, mood, subject, e.g. "uplifting, overcoming challenges, anthemic chorus"',
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
    musicSuggestBtn.disabled = true; musicSuggestBtn.textContent = '...';
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
    finally { musicSuggestBtn.disabled = false; musicSuggestBtn.textContent = '* Suggest'; }
  });

  // -- Real-time sync tool ----------------------------------------------------
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
      if (_offsetMs === 0) { syncStatus.textContent = 'No offset -- nothing to export'; return; }
      exportBtn.disabled = true; syncStatus.textContent = 'Baking...';
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

  // -- Multi-video story ------------------------------------------------------
  let _multiVideo  = false;
  let _multiClips  = 4;
  let _motionStyle = 'calm';

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
  // durSlider is a createSlider() wrapper -- listen on the inner <input>
  durSlider.el.querySelector('input').addEventListener('input', _refreshMultiTotal);

  multiSettings.appendChild(el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
    el('label', { style: 'font-size:.82rem; color:var(--text-3); white-space:nowrap;', text: 'Clips:' }),
    clipsSlider, clipsLabel, totalLabel,
  ]));

  // Motion style toggle
  const _msBtnBase = 'border:1px solid var(--border-2); border-radius:6px; padding:4px 12px; font-size:.78rem; cursor:pointer; background:transparent; color:var(--text-2); transition:background .15s,color .15s;';
  const _msBtnOn   = 'background:var(--accent); border-color:var(--accent); color:#000; font-weight:600;';
  const msBtnCalm  = el('button', { style: _msBtnBase + _msBtnOn, text: 'Calm', title: 'Environment-only motion -- subject stays still (best for LTX)' });
  const msBtnDyn   = el('button', { style: _msBtnBase, text: 'Dynamic', title: 'Subject action -- kinetic motion (Wan I2V)' });
  const msBtnNar   = el('button', { style: _msBtnBase, text: 'Narrative', title: 'Story beats -- subject does purposeful things with narrative meaning' });
  function _setMotionStyle(style) {
    _motionStyle = style;
    msBtnCalm.setAttribute('style', _msBtnBase + (style === 'calm'      ? _msBtnOn : ''));
    msBtnDyn .setAttribute('style', _msBtnBase + (style === 'dynamic'   ? _msBtnOn : ''));
    msBtnNar .setAttribute('style', _msBtnBase + (style === 'narrative' ? _msBtnOn : ''));
    _updatePromptGuide(modelSel.value, style);
  }
  msBtnCalm.addEventListener('click', () => _setMotionStyle('calm'));
  msBtnDyn .addEventListener('click', () => _setMotionStyle('dynamic'));
  msBtnNar .addEventListener('click', () => _setMotionStyle('narrative'));
  multiSettings.appendChild(el('div', { style: 'display:flex; align-items:center; gap:8px; flex-wrap:wrap;' }, [
    el('label', { style: 'font-size:.82rem; color:var(--text-3); white-space:nowrap;', text: 'Motion:' }),
    msBtnCalm, msBtnDyn, msBtnNar,
  ]));

  multiChk.addEventListener('change', () => {
    _multiVideo = multiChk.checked;
    multiSettings.style.display = _multiVideo ? 'flex' : 'none';
    genBtn.textContent = _multiVideo ? 'Create Story' : 'Generate Video';
    _refreshMultiTotal();
  });

  // -- Generate --------------------------------------------------------------
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

  // -- Loop Folder -----------------------------------------------------------
  const loopFolderBtn = el('button', {
    class: 'btn',
    text: 'Loop Folder...',
    style: 'width:100%; margin-top:6px; font-size:.9rem;',
    title: 'Pick a folder of photos and generate videos one-by-one, forever',
  });
  root.appendChild(loopFolderBtn);

  const loopFolderStatus = el('div', {
    style: 'display:none; margin-top:6px; padding:8px 10px; background:var(--surface-2); border-radius:6px; font-size:.8rem; color:var(--accent);',
  });
  root.appendChild(loopFolderStatus);

  let _fvFolderActive = false;
  let _fvFolderPoll = null;

  function _fvUpdateLoopStatus(state) {
    if (!state || !state.active) {
      _fvFolderActive = false;
      loopFolderBtn.textContent = 'Loop Folder...';
      loopFolderBtn.style.borderColor = '';
      loopFolderStatus.style.display = 'none';
      if (_fvFolderPoll) { clearInterval(_fvFolderPoll); _fvFolderPoll = null; }
      return;
    }
    _fvFolderActive = true;
    loopFolderBtn.textContent = 'Stop Loop Folder';
    loopFolderBtn.style.borderColor = 'var(--red)';
    const msg = state.current_file
      ? `Looping: ${state.current_file.split(/[/\\]/).pop()}`
      : 'Loop Folder running...';
    loopFolderStatus.textContent = msg;
    loopFolderStatus.style.display = '';
  }

  async function _fvStartLoopFolder() {
    if (_fvFolderActive) {
      api('/api/fun/folder-loop/stop', { method: 'POST' }).catch(() => {});
      toast('Loop Folder: stop requested', 'info');
      return;
    }
    let picked;
    try {
      const r = await api('/api/browse-folder', { method: 'POST' });
      picked = r.path;
    } catch (_) { picked = null; }
    if (!picked) return;

    const base = _buildFvPayload();
    const settings = {
      video_prompt:     base.video_prompt,
      music_prompt:     base.music_prompt,
      model:            base.model,
      duration:         base.duration,
      steps:            base.steps,
      guidance:         base.guidance,
      seed:             base.seed,
      skip_audio:       base.skip_audio,
      instrumental:     base.instrumental,
      lyric_direction:  base.lyric_direction,
      output_width:     base.output_width,
      output_height:    base.output_height,
      auto_pick_model:  base.auto_pick_model,
      motion_style:     base.motion_style,
    };

    try {
      await api('/api/fun/folder-loop/start', {
        method: 'POST',
        body: JSON.stringify({ folder: picked, repeat: true, settings }),
      });
      toast('Loop Folder started', 'success');
      _fvFolderActive = true;
      _fvUpdateLoopStatus({ active: true, current_file: null });
      if (_fvFolderPoll) clearInterval(_fvFolderPoll);
      _fvFolderPoll = setInterval(async () => {
        try {
          const s = await api('/api/fun/folder-loop/status');
          _fvUpdateLoopStatus(s);
        } catch (_) {}
      }, 3000);
    } catch (e) {
      toast(e.message || 'Failed to start loop', 'error');
    }
  }

  loopFolderBtn.addEventListener('click', _fvStartLoopFolder);

  // Check if a folder loop is already running when the tab first loads
  api('/api/fun/folder-loop/status').then(s => _fvUpdateLoopStatus(s)).catch(() => {});

  const progWrap = el('div');
  root.appendChild(progWrap);
  const prog = createProgressCard(progWrap);
  prog.onCancel(async () => {
    if (_activePoller) { _activePoller.stop(); _activePoller = null; }
    if (_activeJobId) { await stopJob(_activeJobId).catch(() => {}); toast('Stopping...', 'info'); }
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
      lip_sync:         lipSyncChk.checked,
      instrumental:     instrChk.checked,
      lyric_direction:  instrChk.checked ? '' : lyricGuideTA.value.trim(),
      end_photo_path:   _endImagePath || null,
      start_video_path:         _startVideoPath || null,
      start_video_seek_seconds: _startVideoSeekSeconds,
      video_mode:               _videoMode,
      output_width:     _fvOutW,
      output_height:    _fvOutH,
      auto_pick_model:  _autoPick,
      // Send motion_style for single-clip so the pipeline applies the right prompt
      // suffix (calm -> "subject completely still"; dynamic -> kinetic verbs).
      // auto_pick_model overrides this server-side when ON.
      motion_style:     _motionStyle,
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
    if (_autoPromptAbort) {
      _autoPromptAbort.abort();
      _autoPromptAbort = null;
      storyBtn.disabled = false;
      storyBtn.textContent = '* Create Story';
    }
    promptStatus.style.display = 'none';

    const prompt = promptTA.value.trim() || PROMPT_DEFAULT;
    if (!promptTA.value.trim()) promptTA.value = prompt;
    if (!_startImagePath && !_startVideoPath) {
      uploadCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      uploadCard.style.outline = '2px solid var(--red)';
      setTimeout(() => { uploadCard.style.outline = ''; }, 2000);
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
        ? { ...base, clip_duration: base.duration, num_clips: _multiClips, user_direction: 'cinematic narrative, story continuity', motion_style: _motionStyle, start_video_path: _startVideoPath || null, video_mode: _videoMode }
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
          const msg = j.message || (j.status === 'queued' ? 'Queued -- waiting for GPU...' : 'Working...');
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
            // outputs[0] is the final video (merged with audio + upscaled if enabled).
            // _rawPath used to point at meta.video_path (the silent intermediate WanGP
            // output) but the post-process pipeline deletes that file after the mux/
            // upscale steps, leaving downstream 'Add Audio'/'Add Music' buttons hitting
            // a 404. The final video carries audio anyway, and downstream tools demux
            // when they need a silent copy -- safer to always reach for outputs[0].
            _rawPath  = outputs[0];
            _mixPath  = outputs.length > 1 ? outputs[1] : null;
            const bestPath = _mixPath || outputs[0];

            resultTabBar.style.display = 'none';
            player.show(pathToUrl(bestPath), bestPath);

            // Redo Audio + Sync -- build once, update paths on each generation
            let redoCard = vidWrap.querySelector('.redo-audio-card');
            // Load sync preview whenever we have a new mix (even on repeat generations)
            if (redoCard?._syncTool && _mixPath) redoCard._syncTool.load(_mixPath);
            if (!redoCard) {
              redoCard = el('div', { class: 'card redo-audio-card', style: 'padding:12px; margin-top:10px;' });
              redoCard.appendChild(el('div', { style: 'font-size:.8rem; font-weight:600; margin-bottom:8px; color:var(--text-2);', text: 'Redo Audio' }));

              const redoMusicIn  = el('input', { type: 'text', style: 'flex:1; font-size:.82rem;',
                placeholder: 'Music style (blank = AI picks from video)',
              });
              const redoSuggestBtn = el('button', { class: 'btn btn-sm', text: '* Suggest', style: 'flex-shrink:0;' });
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
                  toast('Sync applied -- playing adjusted video', 'success');
                },
              );
              redoCard.appendChild(syncTool);
              redoCard._syncTool = syncTool;  // store ref for load() calls below

              vidWrap.appendChild(redoCard);

              // AI suggest for redo (has real video path)
              redoSuggestBtn.addEventListener('click', async () => {
                if (!_rawPath) { toast('Generate a video first', 'info'); return; }
                redoSuggestBtn.disabled = true; redoSuggestBtn.textContent = '...';
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
                finally { redoSuggestBtn.disabled = false; redoSuggestBtn.textContent = '* Suggest'; }
              });

              redoBtn.addEventListener('click', async () => {
                if (!_rawPath) return;
                redoBtn.disabled = true; redoProg.style.display = ''; redoProg.textContent = 'Submitting...';
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
                    (j) => { redoProg.textContent = j.message || 'Working...'; },
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

            // If job metadata carries a separate audio path, offer beat-sync retime
            const outAudioPath = j.meta && j.meta.audio_path;
            if (outAudioPath && outputs[0]) {
              const existingSync = vidWrap.querySelector('.beat-sync-btn');
              if (!existingSync) {
                const syncBtn = el('button', {
                  class: 'btn btn-sm beat-sync-btn',
                  style: 'margin-top:8px;',
                  text: 'Sync Video to Beat',
                });
                syncBtn.addEventListener('click', async () => {
                  syncBtn.disabled = true;
                  syncBtn.textContent = 'Syncing...';
                  try {
                    const r = await apiFetch('/api/sync/retime', {
                      method: 'POST',
                      body: JSON.stringify({ video_path: outputs[0], audio_path: outAudioPath }),
                    });
                    if (r.job_id) {
                      toast('Beat sync job started -- check Queue tab', 'info');
                    } else if (r.error) {
                      toast('Sync error: ' + r.error, 'error');
                    }
                  } catch (e) {
                    toast('Sync failed: ' + e.message, 'error');
                  } finally {
                    syncBtn.disabled = false;
                    syncBtn.textContent = 'Sync Video to Beat';
                  }
                });
                vidWrap.appendChild(syncBtn);
              }
            }
          } else {
            toast('Generation finished but no output file found -- check server logs', 'error');
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

  // -- Add Audio to Any Video -------------------------------------------------
  // Lets the user add AI-generated music to any video -- newly generated, uploaded,
  // or anything from the gallery -- without running a new video generation.
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
  const extOpenBtn = el('button', { class: 'btn btn-sm', text: 'Browse video file...' });
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
    extGalleryList.innerHTML = '<div style="padding:8px;font-size:.8rem;color:var(--text-3);">Loading...</div>';
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
  const extSuggestBtn = el('button', { class: 'btn btn-sm', text: '* Suggest', style: 'flex-shrink:0;' });
  extAudioSection.appendChild(el('div', { style: 'margin-bottom:8px;' }, [
    el('label', { text: 'Music Prompt', style: 'display:block; font-size:.82rem; color:var(--text-3); margin-bottom:4px;' }),
    el('div', { style: 'display:flex; gap:6px;' }, [extMusicIn, extSuggestBtn]),
  ]));

  const extInstrChk = el('input', { type: 'checkbox', id: 'fv-ext-instr' });
  const extLyricWrap = el('div', { style: 'margin-bottom:10px;' });
  const extLyricTA = el('textarea', { rows: '2', style: 'width:100%; font-size:.82rem; resize:vertical;',
    placeholder: 'Lyric direction -- theme, mood, subject',
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
    extSuggestBtn.disabled = true; extSuggestBtn.textContent = '...';
    try {
      const data = await api('/api/fun/suggest-music', {
        method: 'POST',
        body: JSON.stringify({ video_path: _extVideoPath, user_direction: extLyricTA.value.trim(), instrumental: extInstrChk.checked }),
      });
      if (data.music_prompt) extMusicIn.value = data.music_prompt;
      if (data.lyric_direction && !extInstrChk.checked) extLyricTA.value = data.lyric_direction;
    } catch (e) { toast(e.message || 'Suggestion failed', 'error'); }
    finally { extSuggestBtn.disabled = false; extSuggestBtn.textContent = '* Suggest'; }
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
    extProgRow.textContent = 'Submitting...';
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
        (j) => { extProgRow.textContent = j.message || 'Working...'; },
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

  // -- Palette AI intent -----------------------------------------------------
  import('./shell/ai-intent.js?v=20260503h').then(({ registerTabAI }) => {
    registerTabAI('create-videos', {
      getContext: () => ({
        prompt:       promptTA.value,
        steps:        Number(stepsSlider.value)    || 0,
        guidance:     Number(guidanceSlider.value) || 0,
        duration_sec: Number(durSlider.value)      || 0,
      }),
      applySettings: (s) => {
        // Restore model first -- the rest of the panel reacts to model changes
        // (max duration, supported ratios). Without this, Create continuation
        // inherited a tab-stale T2V model and WanGP rejected the start image
        // ("This model doesn't accept a Start Image -> WanGP did not create any tasks").
        if (typeof s.model === 'string' && s.model.trim()) {
          const want = s.model.trim();
          const _setModel = () => {
            if (_models[want]) {
              modelSel.value = want;
              modelSel.dispatchEvent(new Event('change'));
              return true;
            }
            return false;
          };
          if (!_setModel()) {
            // Models load async from /api/fun/models; if not in yet, retry briefly.
            let tries = 0;
            const retry = () => {
              if (_setModel() || ++tries > 25) return;
              setTimeout(retry, 200);
            };
            setTimeout(retry, 200);
          }
        }
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
