/**
 * Drop Cat Go Studio — Create Videos
 * Pick a generated image, write a motion prompt, get a video.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js?v=20260414';
import { createProgressCard, createVideoPlayer, createSlider, el } from './components.js?v=20260414';
import { toast, apiFetch } from './shell/toast.js?v=20260421c';
import { handoff } from './handoff.js?v=20260422a';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260419o';

let _startImagePath = null;
let _endImagePath   = null;
let _activeJobId    = null;
let _models         = {};
let _applyStart     = null;

export function receiveHandoff(data) {
  if (data.type === 'image' && data.path) {
    if (_applyStart) _applyStart(data.path, data.url || '');
    else toast('Open Create Videos tab first', 'info');
  }
}

function pathToUrl(p) {
  if (!p || p.startsWith('/') || p.startsWith('http')) return p || '';
  const norm = p.replace(/\\/g, '/');
  const idx  = norm.toLowerCase().indexOf('/output/');
  return idx !== -1 ? norm.substring(idx) : `/output/${norm.split('/').pop()}`;
}

export function init(panel) {
  panel.innerHTML = '';
  _startImagePath = null;
  _endImagePath   = null;

  const root = el('div', { style: 'display:flex; flex-direction:column; gap:14px; padding:16px; max-width:860px; margin:0 auto;' });
  panel.appendChild(root);

  // ── Image picker ──────────────────────────────────────────────────────────
  const pickerCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(pickerCard);

  const pickerHeader = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-bottom:10px;' });
  pickerCard.appendChild(pickerHeader);
  pickerHeader.appendChild(el('span', { style: 'font-size:.85rem; font-weight:600; flex:1;', text: 'Your Generated Images' }));

  const refreshBtn = el('button', { class: 'btn btn-sm', text: 'Refresh' });
  pickerHeader.appendChild(refreshBtn);

  const fileInput = el('input', { type: 'file', accept: 'image/*', style: 'display:none' });
  pickerCard.appendChild(fileInput);
  const openFileBtn = el('button', { class: 'btn btn-sm', text: 'Open file...' });
  pickerHeader.appendChild(openFileBtn);
  openFileBtn.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', async () => {
    if (!fileInput.files?.length) return;
    try {
      const data = await apiUpload('/api/fun/upload', Array.from(fileInput.files));
      const f = data.files?.[0];
      if (f) _applyStart(f.path, f.url || pathToUrl(f.path));
    } catch (e) { toast(e.message, 'error'); }
    fileInput.value = '';
  });

  const imageGrid = el('div', { style: 'display:grid; grid-template-columns:repeat(auto-fill,minmax(90px,1fr)); gap:6px; max-height:280px; overflow-y:auto;' });
  pickerCard.appendChild(imageGrid);

  let _selectedThumb = null;

  async function loadSessionImages() {
    try {
      // Use gallery (persists across restarts) filtered to images only
      const data = await api('/api/gallery?limit=60&tab=sd-prompts');
      const items = (data.items || data || []).filter(i =>
        !/(\.mp4|\.webm|\.mov)$/i.test(i.url || '')
      ).slice(0, 36);
      imageGrid.innerHTML = '';
      _selectedThumb = null;
      if (!items.length) {
        imageGrid.appendChild(el('div', {
          style: 'grid-column:1/-1; text-align:center; padding:32px 0; color:var(--text-3); font-size:.82rem; line-height:1.6;',
          text: 'No generated images yet — go to Generate Images first.',
        }));
        return;
      }
      for (const item of items) {
        const url  = item.url;
        const path = item.metadata?.path || url;
        const thumb = el('img', {
          src: url, title: item.prompt || url.split('/').pop(),
          style: 'width:100%; aspect-ratio:1; object-fit:cover; border-radius:6px; cursor:pointer; border:2px solid transparent; transition:border-color .15s;',
        });
        thumb.addEventListener('click', () => {
          if (_selectedThumb) _selectedThumb.style.borderColor = 'transparent';
          thumb.style.borderColor = 'var(--accent)';
          _selectedThumb = thumb;
          _applyStart(path, url);
        });
        imageGrid.appendChild(thumb);
      }
    } catch (e) { toast(e.message, 'error'); }
  }

  refreshBtn.addEventListener('click', loadSessionImages);
  loadSessionImages();

  // ── Selected image strip ──────────────────────────────────────────────────
  const selectedCard = el('div', { class: 'card', style: 'display:none; padding:10px 14px; align-items:center; gap:12px;' });
  root.appendChild(selectedCard);

  const selectedPreview = el('img', { style: 'width:72px; height:72px; object-fit:cover; border-radius:6px; flex-shrink:0;' });
  const selectedName    = el('div', { style: 'font-size:.74rem; color:var(--text-3); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-top:2px;' });
  const clearBtn = el('button', { class: 'btn btn-sm', text: '✕ Clear', style: 'margin-left:auto; flex-shrink:0;',
    onclick() {
      _startImagePath = null;
      selectedCard.style.display = 'none';
      if (_selectedThumb) { _selectedThumb.style.borderColor = 'transparent'; _selectedThumb = null; }
    },
  });

  selectedCard.appendChild(selectedPreview);
  selectedCard.appendChild(el('div', { style: 'flex:1; min-width:0;' }, [
    el('div', { style: 'font-size:.82rem; font-weight:600; color:var(--text-2);', text: 'Selected for video' }),
    selectedName,
  ]));
  selectedCard.appendChild(clearBtn);

  _applyStart = (path, url) => {
    _startImagePath = path;
    selectedPreview.src = url || pathToUrl(path);
    selectedName.textContent = path.split(/[\\/]/).pop();
    selectedCard.style.display = 'flex';
  };

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
    text: 'Describe what moves and where the camera goes — present tense, action verbs.' }));
  const PROMPT_PLACEHOLDER = 'e.g. "Camera slowly pushes in, subject blinks and smiles, hair lifts in the breeze, warm light pulses across the frame"';
  const PROMPT_DEFAULT     = 'Camera slowly pushes in, gentle natural movement, warm light across the frame';
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

  let _autoPromptAbort = null;

  // Auto-generate motion prompt from the selected image via LLM vision.
  // Abortable so clicking Generate immediately cancels it.
  async function _autoGeneratePrompt(imagePath) {
    if (promptTA.value.trim()) return;
    if (_autoPromptAbort) _autoPromptAbort.abort();
    _autoPromptAbort = new AbortController();
    const { signal } = _autoPromptAbort;

    promptStatus.style.display = 'flex';

    // Safety timeout — give up after 30s and let the user proceed
    const timeout = setTimeout(() => {
      _autoPromptAbort?.abort();
      promptStatus.style.display = 'none';
    }, 30000);

    try {
      const data = await api('/api/fun/generate-prompts', {
        method: 'POST',
        body: JSON.stringify({ image_path: imagePath, num_prompts: 1, creativity: 7, max_tokens: 400 }),
        signal,
      });
      const prompts = data.prompts || [];
      const text = typeof prompts[0] === 'string' ? prompts[0] : prompts[0]?.prompt;
      if (text && !promptTA.value.trim()) promptTA.value = text;
    } catch (e) {
      if (e?.name !== 'AbortError') {
        console.warn('[auto-prompt] failed:', e?.message);
        apiFetch('/api/logs/client', { method: 'POST', body: JSON.stringify({ message: `auto-prompt failed: ${e?.message} | path: ${imagePath}`, source: 'tab-fun-videos', lineno: 0 }) }).catch(() => {});
      }
    } finally {
      clearTimeout(timeout);
      promptStatus.style.display = 'none';
      _autoPromptAbort = null;
    }
  }

  // Extend _applyStart (defined above) to trigger auto-prompt
  const _applyStartBase = _applyStart;
  _applyStart = (path, url) => {
    _applyStartBase(path, url);
    _autoGeneratePrompt(path);
  };

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

  const durSlider = createSlider(settingsCard, { label: 'Duration (seconds)', min: 2, max: 20, step: 1, value: 14 });

  const slidersGrid = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:10px;' });
  settingsCard.appendChild(slidersGrid);
  const stepsSlider    = createSlider(slidersGrid, { label: 'Steps',    min: 4,  max: 50,  step: 1,   value: 30  });
  const guidanceSlider = createSlider(slidersGrid, { label: 'Guidance', min: 1,  max: 20,  step: 0.5, value: 7.5 });

  modelSel.addEventListener('change', () => {
    const m = _models[modelSel.value];
    if (!m) return;
    durSlider.value = Math.min(parseFloat(durSlider.value) || 14, m.max_sec);
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
  }).catch(() => {});

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
  const musicIn = el('input', { type: 'text', placeholder: 'Music style (optional — AI picks from your video if blank)', style: 'width:100%; margin-bottom:8px;' });
  audioBody.appendChild(el('div', {}, [
    el('label', { text: 'Music Prompt', style: 'display:block; font-size:.82rem; color:var(--text-3); margin-bottom:4px;' }),
    musicIn,
  ]));
  const instrChk = el('input', { type: 'checkbox', id: 'fv-instr' });
  audioBody.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:center;' }, [
    instrChk,
    el('label', { for: 'fv-instr', text: 'Instrumental only (no AI lyrics)', style: 'cursor:pointer; font-size:.85rem;' }),
  ]));
  audioChk.addEventListener('change', () => { audioBody.style.display = audioChk.checked ? '' : 'none'; });

  // ── Generate ──────────────────────────────────────────────────────────────
  const genBtn = el('button', {
    class: 'btn btn-primary btn-generate',
    text: 'Generate Video',
    style: 'width:100%; font-size:1.1rem; padding:14px; font-weight:700;',
  });
  root.appendChild(genBtn);

  const progWrap = el('div');
  root.appendChild(progWrap);
  const prog = createProgressCard(progWrap);
  prog.onCancel(async () => {
    if (_activeJobId) { await stopJob(_activeJobId).catch(() => {}); toast('Stopping…', 'info'); }
  });

  const vidWrap = el('div', { style: 'display:flex; flex-direction:column; gap:16px;' });
  root.appendChild(vidWrap);

  const playerRawWrap = el('div');
  const playerMixWrap = el('div');
  vidWrap.appendChild(playerMixWrap);   // ACE-Step mixed — shown first (the good one)
  vidWrap.appendChild(playerRawWrap);   // raw video — shown second

  const playerMix = createVideoPlayer(playerMixWrap);
  const playerRaw = createVideoPlayer(playerRawWrap);
  playerMix.onStartOver(() => { playerMix.hide(); playerRaw.hide(); });
  playerRaw.onStartOver(() => { playerMix.hide(); playerRaw.hide(); });

  genBtn.addEventListener('click', async () => {
    // Cancel any in-flight auto-prompt — we don't need it anymore
    if (_autoPromptAbort) { _autoPromptAbort.abort(); _autoPromptAbort = null; }
    promptStatus.style.display = 'none';

    const prompt = promptTA.value.trim() || PROMPT_DEFAULT;
    if (!promptTA.value.trim()) promptTA.value = prompt;
    if (!_startImagePath) {
      pickerCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      pickerCard.style.outline = '2px solid var(--red)';
      setTimeout(() => { pickerCard.style.outline = ''; }, 2000);
      toast('Select an image above first', 'error');
      return;
    }

    const m        = _models[modelSel.value] || {};
    const duration = Math.min(parseFloat(durSlider.value) || 14, m.max_sec || 20);

    genBtn.disabled = true;
    prog.show();
    prog.update(0, 'Submitting...');
    playerMix.hide();
    playerRaw.hide();

    try {
      const { job_id } = await api('/api/fun/make-it', {
        method: 'POST',
        body: JSON.stringify({
          photo_path:   _startImagePath,
          video_prompt: prompt,
          music_prompt: musicIn.value.trim(),
          model:        modelSel.value,
          duration,
          steps:        parseInt(stepsSlider.value)     || 30,
          guidance:     parseFloat(guidanceSlider.value) || 7.5,
          seed:         parseInt(seedIn.value)           || -1,
          skip_audio:     !audioChk.checked,
          instrumental:   instrChk.checked,
          end_photo_path: _endImagePath || null,
        }),
      });
      _activeJobId = job_id;

      pollJob(
        job_id,
        (j) => {
          const msg = j.message || (j.status === 'queued' ? 'Queued — waiting for GPU...' : 'Working...');
          prog.update(j.progress || 0, msg);
        },
        (j) => {
          prog.hide();
          genBtn.disabled = false;
          _activeJobId = null;
          if (j.output) {
            const outputs  = Array.isArray(j.output) ? j.output : [j.output];
            const rawPath  = outputs[0];                       // raw WanGP video
            const mixPath  = outputs.length > 1 ? outputs[1] : null;  // ACE-Step mixed

            // Best output for gallery / handoff is the mixed version if available
            const bestPath = mixPath || rawPath;

            if (mixPath) {
              playerMix.showLabelled(pathToUrl(mixPath), mixPath, 'With ACE-Step music');
              playerRaw.showLabelled(pathToUrl(rawPath), rawPath, 'Raw video (no music)');
            } else {
              playerMix.show(pathToUrl(rawPath), rawPath);
            }

            pushToGallery('fun-videos', bestPath, prompt, null, {
              steps: Number(stepsSlider.value),
              guidance: Number(guidanceSlider.value),
              duration_sec: Number(durSlider.value),
            });

            const existing = vidWrap.querySelector('.to-bridges-btn');
            const bridgesBtn = existing || (() => {
              const b = el('button', { class: 'btn btn-sm to-bridges-btn', text: '→ Add to Transitions', style: 'margin-top:8px;' });
              vidWrap.appendChild(b);
              return b;
            })();
            bridgesBtn.onclick = () => {
              handoff('bridges', { type: 'video', path: bestPath });
              document.querySelector('[data-tab="bridges"]')?.click();
            };
          } else {
            toast('Generation finished but no output file found — check server logs', 'error');
          }
        },
        (err) => {
          prog.hide();
          genBtn.disabled = false;
          _activeJobId = null;
          toast(typeof err === 'string' ? err : (err?.message || 'Generation failed'), 'error');
        },
      );
    } catch (e) {
      prog.hide();
      genBtn.disabled = false;
      toast(e.message, 'error');
    }
  });

  // ── Palette AI intent ─────────────────────────────────────────────────────
  import('./shell/ai-intent.js?v=20260419e').then(({ registerTabAI }) => {
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
        if (typeof s.prompt_append === 'string' && s.prompt_append.trim()) {
          const cur = promptTA.value.trim();
          promptTA.value = cur ? `${cur}, ${s.prompt_append.trim()}` : s.prompt_append.trim();
        }
      },
    });
  }).catch(() => {});
}
