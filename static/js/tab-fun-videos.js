/**
 * Drop Cat Go Studio — Create Videos
 * Pick a generated image, write a motion prompt, get a video.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js?v=20260414';
import { createProgressCard, createVideoPlayer, createSlider, el, pathToUrl } from './components.js?v=20260426c';
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
      const data = await api('/api/gallery?limit=60');
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

  // ── Create Story row ──────────────────────────────────────────────────────
  const storyRow = el('div', { style: 'display:flex; gap:6px; align-items:center; margin-top:8px;' });
  const storyBtn = el('button', { class: 'btn btn-sm btn-primary', text: '✦ Create Story', title: 'Generate a motion prompt from your image using AI' });
  const storyProviderSel = el('select', { style: 'font-size:.78rem; padding:2px 6px;', title: 'LLM used to write the story' });
  for (const [val, label] of [['auto','Auto'], ['anthropic','Anthropic'], ['openai','OpenAI'], ['ollama','Ollama']]) {
    storyProviderSel.appendChild(el('option', { value: val, text: label }));
  }
  storyRow.appendChild(storyBtn);
  storyRow.appendChild(el('span', { style: 'font-size:.74rem; color:var(--text-3);', text: 'via' }));
  storyRow.appendChild(storyProviderSel);
  promptCard.appendChild(storyRow);

  let _autoPromptAbort = null;

  // Auto-generate motion prompt from the selected image via LLM vision.
  // force=true: regenerates even if prompt textarea already has content.
  async function _autoGeneratePrompt(imagePath, force = false) {
    if (!force && promptTA.value.trim()) return;
    if (_autoPromptAbort) _autoPromptAbort.abort();
    _autoPromptAbort = new AbortController();
    const { signal } = _autoPromptAbort;

    promptStatus.style.display = 'flex';
    storyBtn.disabled = true;

    // Safety timeout — give up after 30s and let the user proceed
    const timeout = setTimeout(() => {
      _autoPromptAbort?.abort();
      promptStatus.style.display = 'none';
    }, 30000);

    try {
      const provider = storyProviderSel.value || 'auto';
      const data = await api('/api/fun/generate-prompts', {
        method: 'POST',
        body: JSON.stringify({ image_path: imagePath, num_prompts: 1, creativity: 7, max_tokens: 400, provider }),
        signal,
      });
      const prompts = data.prompts || [];
      const text = typeof prompts[0] === 'string' ? prompts[0] : prompts[0]?.prompt;
      if (text) promptTA.value = text;
    } catch (e) {
      if (e?.name !== 'AbortError') {
        console.warn('[auto-prompt] failed:', e?.message);
        apiFetch('/api/logs/client', { method: 'POST', body: JSON.stringify({ message: `auto-prompt failed: ${e?.message} | path: ${imagePath}`, source: 'tab-fun-videos', lineno: 0 }) }).catch(() => {});
      }
    } finally {
      clearTimeout(timeout);
      promptStatus.style.display = 'none';
      storyBtn.disabled = false;
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
    if (!_startImagePath) { toast('Load an image first', 'error'); return; }
    _autoGeneratePrompt(_startImagePath, true);
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
  audioBody.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:center; margin-bottom:8px;' }, [
    instrChk,
    el('label', { for: 'fv-instr', text: 'Instrumental only (no AI lyrics)', style: 'cursor:pointer; font-size:.85rem;' }),
  ]));

  const lyricGuideWrap = el('div');
  const lyricGuideTA = el('textarea', {
    rows: '2',
    placeholder: 'Lyric guideline (optional) — theme, mood, subject, tone, e.g. "playful adventure, celebrate the dog"',
    style: 'width:100%; resize:vertical; font-size:.82rem;',
  });
  lyricGuideWrap.appendChild(el('div', {}, [
    el('label', { text: 'Lyric Guideline', style: 'display:block; font-size:.82rem; color:var(--text-3); margin-bottom:4px;' }),
    lyricGuideTA,
  ]));
  audioBody.appendChild(lyricGuideWrap);

  function _syncLyricGuide() {
    lyricGuideWrap.style.display = instrChk.checked ? 'none' : '';
  }
  instrChk.addEventListener('change', _syncLyricGuide);
  _syncLyricGuide();

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

  const vidWrap = el('div');
  root.appendChild(vidWrap);

  const resultTabBar = el('div', { class: 'result-tabs', style: 'display:none;' });
  const tabMix = el('button', { class: 'result-tab active', text: 'With ACE-Step music' });
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
    player.hide();
    resultTabBar.style.display = 'none';

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
          lyrics:         instrChk.checked ? '' : lyricGuideTA.value.trim(),
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
            const outputs = Array.isArray(j.output) ? j.output : [j.output];
            _rawPath  = outputs[0];
            _mixPath  = outputs.length > 1 ? outputs[1] : null;
            const bestPath = _mixPath || _rawPath;

            if (_mixPath) {
              resultTabBar.style.display = 'flex';
              _showResultTab('mix');
            } else {
              resultTabBar.style.display = 'none';
              player.show(pathToUrl(_rawPath), _rawPath);
            }

            pushToGallery('fun-videos', bestPath, prompt, null, {
              steps: Number(stepsSlider.value),
              guidance: Number(guidanceSlider.value),
              duration_sec: Number(durSlider.value),
            });

            const existing = vidWrap.querySelector('.to-seq-btn');
            const seqBtn = existing || (() => {
              const b = el('button', { class: 'btn btn-sm to-seq-btn', text: '+ Add to sequence', style: 'margin-top:8px;' });
              vidWrap.appendChild(b);
              return b;
            })();
            seqBtn.onclick = () => {
              const name = bestPath.split(/[\\/]/).pop();
              _seqAddItem({ path: bestPath, name });
              seqToggle.checked = true;
              seqSection.style.display = '';
              seqSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
              toast(`Added "${name}" to sequence`, 'success');
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

  // ── Sequence builder (multi-clip with transitions) ────────────────────────
  const seqToggleRow = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-top:4px;' });
  root.appendChild(seqToggleRow);
  const seqToggle = el('input', { type: 'checkbox', id: 'fv-seq-toggle' });
  seqToggleRow.appendChild(seqToggle);
  seqToggleRow.appendChild(el('label', {
    for: 'fv-seq-toggle',
    style: 'font-size:.82rem; color:var(--text-3); cursor:pointer; user-select:none;',
    text: 'Build a sequence — arrange multiple clips with AI transitions',
  }));

  const seqSection = el('div', { class: 'card', style: 'display:none; padding:14px;' });
  root.appendChild(seqSection);
  seqToggle.addEventListener('change', () => {
    seqSection.style.display = seqToggle.checked ? '' : 'none';
  });

  // Sequence state
  let _seqItems = [];  // [{path, name, gap}]  gap = transition duration after this clip
  let _dragSrcIdx = null;

  const seqHeader = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-bottom:10px;' });
  seqSection.appendChild(seqHeader);
  const seqTitle = el('span', { style: 'font-size:.85rem; font-weight:600; flex:1;' });
  seqHeader.appendChild(seqTitle);

  const seqFileInput = el('input', { type: 'file', accept: 'video/*', multiple: 'true', style: 'display:none' });
  seqSection.appendChild(seqFileInput);
  const seqAddBtn = el('button', { class: 'btn btn-sm', text: 'Add files...' });
  seqHeader.appendChild(seqAddBtn);
  seqAddBtn.addEventListener('click', () => seqFileInput.click());

  const seqList = el('div', { style: 'display:flex; flex-direction:column; gap:0;' });
  seqSection.appendChild(seqList);

  // Drop zone for dragging files in
  const seqDrop = el('div', {
    style: 'border:2px dashed var(--border-2); border-radius:6px; padding:14px; text-align:center; font-size:.8rem; color:var(--text-3); margin-top:8px; cursor:pointer; transition:border-color .15s;',
    text: 'Drop video files here or use Add files...',
  });
  seqSection.appendChild(seqDrop);
  seqDrop.addEventListener('click', () => seqFileInput.click());
  seqDrop.addEventListener('dragover', e => { e.preventDefault(); seqDrop.style.borderColor = 'var(--accent)'; });
  seqDrop.addEventListener('dragleave', () => { seqDrop.style.borderColor = 'var(--border-2)'; });
  seqDrop.addEventListener('drop', async e => {
    e.preventDefault();
    seqDrop.style.borderColor = 'var(--border-2)';
    const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('video/'));
    if (files.length) await _seqUpload(files);
  });

  seqFileInput.addEventListener('change', async () => {
    if (!seqFileInput.files?.length) return;
    await _seqUpload(Array.from(seqFileInput.files));
    seqFileInput.value = '';
  });

  async function _seqUpload(files) {
    try {
      const data = await apiUpload('/api/bridges/upload', files);
      for (const f of data.files || []) _seqAddItem({ path: f.path, name: f.name || f.path.split(/[\\/]/).pop() });
    } catch (e) { toast(e.message, 'error'); }
  }

  function _seqAddItem(item) {
    if (_seqItems.find(i => i.path === item.path)) { toast('Already in sequence', 'info'); return; }
    _seqItems.push({ ...item, gap: 8 });
    _renderSeq();
  }

  function _renderSeq() {
    seqList.innerHTML = '';
    const n = _seqItems.length;
    seqTitle.textContent = n
      ? `Sequence — ${n} clip${n !== 1 ? 's' : ''}${n > 1 ? `, ${n - 1} transition${n - 1 !== 1 ? 's' : ''}` : ''}`
      : 'Sequence';

    if (!n) return;

    _seqItems.forEach((item, i) => {
      // ── Clip row ──────────────────────────────────────────────────────────
      const row = el('div', {
        draggable: 'true',
        style: 'display:flex; align-items:center; gap:8px; padding:8px 10px; background:var(--bg-raised); border:1px solid var(--border-2); border-radius:6px; cursor:grab; user-select:none;',
      });

      // Drag handle
      row.appendChild(el('span', { style: 'color:var(--text-3); font-size:.8rem; flex-shrink:0;', text: '⠿' }));
      row.appendChild(el('span', {
        style: 'flex:1; font-size:.8rem; color:var(--text-2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;',
        text: `${i + 1}. ${item.name}`,
      }));
      const removeBtn = el('button', { class: 'btn-icon-xs remove', text: '✕', title: 'Remove',
        onclick(e) { e.stopPropagation(); _seqItems.splice(i, 1); _renderSeq(); },
      });
      row.appendChild(removeBtn);

      // HTML5 drag-to-reorder
      row.addEventListener('dragstart', e => {
        _dragSrcIdx = i;
        setTimeout(() => { row.style.opacity = '0.35'; }, 0);
        e.dataTransfer.effectAllowed = 'move';
      });
      row.addEventListener('dragend', () => { row.style.opacity = ''; _dragSrcIdx = null; });
      row.addEventListener('dragover', e => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        row.style.borderColor = 'var(--accent)';
      });
      row.addEventListener('dragleave', () => { row.style.borderColor = 'var(--border-2)'; });
      row.addEventListener('drop', e => {
        e.preventDefault();
        row.style.borderColor = 'var(--border-2)';
        if (_dragSrcIdx === null || _dragSrcIdx === i) return;
        const [moved] = _seqItems.splice(_dragSrcIdx, 1);
        _seqItems.splice(i, 0, moved);
        _renderSeq();
      });

      seqList.appendChild(row);

      // ── Gap control between clips ─────────────────────────────────────────
      if (i < _seqItems.length - 1) {
        const gapRow = el('div', { style: 'display:flex; align-items:center; gap:8px; padding:4px 12px; color:var(--text-3); font-size:.72rem;' });
        const gapLabel = el('span', { text: `Transition: ${item.gap}s` });
        const gapSlider = el('input', {
          type: 'range', min: '3', max: '20', step: '1', value: String(item.gap),
          style: 'flex:1; cursor:pointer;',
        });
        gapSlider.addEventListener('input', () => {
          item.gap = Number(gapSlider.value);
          gapLabel.textContent = `Transition: ${item.gap}s`;
        });
        gapRow.appendChild(el('div', { style: 'width:1px; height:20px; background:var(--border-2); flex-shrink:0;' }));
        gapRow.appendChild(gapLabel);
        gapRow.appendChild(gapSlider);
        gapRow.appendChild(el('div', { style: 'width:1px; height:20px; background:var(--border-2); flex-shrink:0;' }));
        seqList.appendChild(gapRow);
      }
    });
  }
  _renderSeq();

  // ── Sequence generate ──────────────────────────────────────────────────────
  const seqGenBtn = el('button', {
    class: 'btn btn-primary btn-generate',
    text: 'Generate Transitions & Stitch',
    style: 'width:100%; font-size:1rem; padding:12px; font-weight:700; margin-top:10px;',
  });
  seqSection.appendChild(seqGenBtn);

  const seqProgWrap = el('div');
  seqSection.appendChild(seqProgWrap);
  const seqProg = createProgressCard(seqProgWrap);

  const seqVidWrap = el('div');
  seqSection.appendChild(seqVidWrap);
  const seqPlayer = createVideoPlayer(seqVidWrap);
  seqPlayer.onStartOver(() => seqPlayer.hide());

  seqGenBtn.addEventListener('click', async () => {
    if (_seqItems.length < 2) {
      seqDrop.style.outline = '2px solid var(--red)';
      setTimeout(() => { seqDrop.style.outline = ''; }, 2000);
      toast('Add at least 2 clips to the sequence', 'error');
      return;
    }
    seqGenBtn.disabled = true;
    seqProg.show();
    seqProg.update(0, 'Submitting...');
    seqPlayer.hide();

    try {
      const { job_id } = await api('/api/bridges/generate', {
        method: 'POST',
        body: JSON.stringify({
          items: _seqItems.map(it => ({ path: it.path, kind: 'video', prompt: '', analysis: null })),
          settings: {
            model:           'LTX-2 Dev19B Distilled',
            resolution:      '480p',
            transition_mode: 'cinematic',
            prompt_mode:     'ai_informed',
            duration:        Math.round(_seqItems.slice(0, -1).reduce((s, i) => s + i.gap, 0) / Math.max(1, _seqItems.length - 1)),
            steps:           20,
            guidance:        10,
            creativity:      8,
          },
        }),
      });

      seqProg.onCancel(async () => { await stopJob(job_id).catch(() => {}); seqGenBtn.disabled = false; });
      pollJob(job_id,
        j => seqProg.update(j.progress || 0, j.status === 'queued' ? 'Queued — waiting for GPU...' : (j.message || 'Working...')),
        j => {
          seqProg.hide();
          seqGenBtn.disabled = false;
          if (j.output) {
            const out = Array.isArray(j.output) ? j.output[0] : j.output;
            seqPlayer.show(pathToUrl(out), out);
            pushToGallery('fun-videos', out, `Sequence (${_seqItems.length} clips)`, null, {});
            toast('Sequence complete!', 'success');
          }
        },
        err => { seqProg.hide(); seqGenBtn.disabled = false; toast(err, 'error'); },
      );
    } catch (e) { seqProg.hide(); seqGenBtn.disabled = false; toast(e.message, 'error'); }
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
