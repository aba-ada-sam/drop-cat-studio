/**
 * Drop Cat Go Studio — Create Videos
 * Direct WanGP interface: I2V, T2V, start+end image morphs.
 * No AI Director. Write your prompt, hit generate.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js?v=20260414';
import { toast, createDropZone, createProgressCard, createVideoPlayer, createSlider, el } from './components.js?v=20260414';
import { handoff } from './handoff.js?v=20260415';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260419h';

let _startImagePath = null;
let _endImagePath   = null;
let _activeJobId    = null;
let _models         = {};
let _applyStart     = null;   // wired after init for handoff

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

  // ── Mode ──────────────────────────────────────────────────────────────────
  let mode = 'i2v';
  const modeRow = el('div', { style: 'display:flex; gap:8px; align-items:center; flex-wrap:wrap;' });
  root.appendChild(modeRow);

  modeRow.appendChild(el('span', { text: 'Mode:', style: 'font-size:.85rem; color:var(--text-3); font-weight:600;' }));
  const i2vBtn = el('button', { class: 'btn btn-sm btn-primary', text: '📷 Image → Video' });
  const t2vBtn = el('button', { class: 'btn btn-sm',             text: '📝 Text → Video'  });
  modeRow.appendChild(i2vBtn);
  modeRow.appendChild(t2vBtn);
  const modeHint = el('span', { style: 'font-size:.76rem; color:var(--text-3); margin-left:4px;' });
  modeRow.appendChild(modeHint);

  // ── Image inputs ──────────────────────────────────────────────────────────
  const imageCard = el('div', { class: 'card', style: 'padding:14px; display:grid; grid-template-columns:1fr 1fr; gap:14px;' });
  root.appendChild(imageCard);

  // Start image
  const startCol = el('div');
  imageCard.appendChild(startCol);
  startCol.appendChild(el('div', { style: 'font-size:.82rem; font-weight:600; margin-bottom:6px; color:var(--text-2);', text: 'Start Image' }));

  const startPreview = el('img', { style: 'display:none; width:100%; max-height:180px; object-fit:cover; border-radius:var(--r-sm); margin-bottom:6px;' });
  startCol.appendChild(startPreview);
  const startClearBtn = el('button', { class: 'btn btn-sm', text: '✕ Clear', style: 'display:none; font-size:.72rem; margin-bottom:6px;',
    onclick() { _startImagePath = null; startPreview.style.display = 'none'; startPreview.src = ''; startClearBtn.style.display = 'none'; }
  });
  startCol.appendChild(startClearBtn);

  createDropZone(startCol, {
    accept: 'image/*', multiple: false, label: 'Drop start image or click to browse',
    async onFiles(files) {
      try {
        const data = await apiUpload('/api/fun/upload', files);
        const f = data.files?.[0];
        if (f) {
          _startImagePath = f.path;
          startPreview.src = f.url || `/uploads/${f.name}`;
          startPreview.style.display = '';
          startClearBtn.style.display = '';
          toast('Start image ready', 'success');
        }
      } catch (e) { toast(e.message, 'error'); }
    },
  });

  _applyStart = (path, url) => {
    _startImagePath = path;
    startPreview.src = url || pathToUrl(path);
    startPreview.style.display = '';
    startClearBtn.style.display = '';
    toast('Image loaded', 'success');
  };

  // End image (optional)
  const endCol = el('div');
  imageCard.appendChild(endCol);
  endCol.appendChild(el('div', { style: 'font-size:.82rem; font-weight:600; margin-bottom:2px; color:var(--text-2);', text: 'End Image' }));
  endCol.appendChild(el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-bottom:6px;', text: 'Optional — video morphs from start to end' }));

  const endPreview = el('img', { style: 'display:none; width:100%; max-height:180px; object-fit:cover; border-radius:var(--r-sm); margin-bottom:6px;' });
  endCol.appendChild(endPreview);
  const endClearBtn = el('button', { class: 'btn btn-sm', text: '✕ Clear', style: 'display:none; font-size:.72rem; margin-bottom:6px;',
    onclick() { _endImagePath = null; endPreview.style.display = 'none'; endPreview.src = ''; endClearBtn.style.display = 'none'; }
  });
  endCol.appendChild(endClearBtn);

  createDropZone(endCol, {
    accept: 'image/*', multiple: false, label: 'Drop end image (optional)',
    async onFiles(files) {
      try {
        const data = await apiUpload('/api/fun/upload', files);
        const f = data.files?.[0];
        if (f) {
          _endImagePath = f.path;
          endPreview.src = f.url || `/uploads/${f.name}`;
          endPreview.style.display = '';
          endClearBtn.style.display = '';
          toast('End image ready', 'success');
        }
      } catch (e) { toast(e.message, 'error'); }
    },
  });

  // ── Prompt ────────────────────────────────────────────────────────────────
  const promptCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(promptCard);

  promptCard.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:4px;', text: '🎬 Video Prompt' }));
  promptCard.appendChild(el('div', { style: 'font-size:.74rem; color:var(--text-3); margin-bottom:8px;',
    text: 'Describe motion and action — present tense, action verbs. What moves, where the camera goes, what transforms. The model already sees your image.' }));

  const promptTA = el('textarea', { rows: '4', style: 'width:100%; resize:vertical; font-size:.9rem;',
    placeholder: 'e.g. "Camera slowly pushes in, subject blinks and smiles, hair lifts in the breeze, warm light pulses across the frame"' });
  promptCard.appendChild(promptTA);

  // ── Model & Settings ──────────────────────────────────────────────────────
  const settingsCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(settingsCard);
  settingsCard.appendChild(el('div', { style: 'font-size:.85rem; font-weight:600; margin-bottom:12px;', text: '⚙️ Settings' }));

  const topGrid = el('div', { style: 'display:grid; grid-template-columns:2fr 1fr; gap:10px; margin-bottom:10px;' });
  settingsCard.appendChild(topGrid);

  const modelSel = el('select', { style: 'width:100%;' });
  const modelInfo = el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-top:3px;', text: '' });
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

  function onModelChange() {
    const m = _models[modelSel.value];
    if (!m) return;
    const dur = Math.min(parseFloat(durSlider.value) || 14, m.max_sec);
    durSlider.value = dur;
    modelInfo.textContent = `Max ${m.max_sec}s  •  ${m.res ? m.res[0]+'×'+m.res[1] : ''}  •  ${m.fps}fps  •  ${m.i2v ? 'I2V+T2V' : 'T2V only'}`;
    if (mode === 'i2v' && !m.i2v) setMode('t2v');
  }
  modelSel.addEventListener('change', onModelChange);

  api('/api/fun/models').then(data => {
    _models = data.models || {};
    modelSel.innerHTML = '';
    for (const [name] of Object.entries(_models)) {
      modelSel.appendChild(el('option', { value: name, text: name }));
    }
    const ltx = Object.keys(_models).find(k => k.includes('LTX'));
    if (ltx) modelSel.value = ltx;
    onModelChange();
  }).catch(() => {});

  // ── Audio ─────────────────────────────────────────────────────────────────
  const audioCard = el('div', { class: 'card', style: 'padding:14px;' });
  root.appendChild(audioCard);

  const audioChk = el('input', { type: 'checkbox', id: 'av-audio', checked: 'true' });
  audioCard.appendChild(el('div', { style: 'display:flex; gap:8px; align-items:center; margin-bottom:10px;' }, [
    audioChk,
    el('label', { for: 'av-audio', text: '🎵 Generate music', style: 'cursor:pointer; font-weight:600;' }),
  ]));

  const audioBody = el('div');
  audioCard.appendChild(audioBody);
  const musicIn = el('input', { type: 'text', placeholder: 'Music style (optional — AI picks from your video if blank)', style: 'width:100%; margin-bottom:8px;' });
  audioBody.appendChild(el('div', {}, [
    el('label', { text: 'Music Prompt', style: 'display:block; font-size:.82rem; color:var(--text-3); margin-bottom:4px;' }),
    musicIn,
  ]));
  const instrChk = el('input', { type: 'checkbox', id: 'av-instr' });
  audioBody.appendChild(el('div', { style: 'display:flex; gap:6px; align-items:center;' }, [
    instrChk,
    el('label', { for: 'av-instr', text: 'Instrumental only (no AI lyrics)', style: 'cursor:pointer; font-size:.85rem;' }),
  ]));
  audioChk.addEventListener('change', () => { audioBody.style.display = audioChk.checked ? '' : 'none'; });

  // ── Generate ──────────────────────────────────────────────────────────────
  const genBtn = el('button', {
    class: 'btn btn-primary btn-generate',
    text: '⚡ Generate Video',
    style: 'width:100%; font-size:1.1rem; padding:14px; font-weight:700;',
  });
  root.appendChild(genBtn);

  // Progress
  const progWrap = el('div');
  root.appendChild(progWrap);
  const prog = createProgressCard(progWrap);
  prog.onCancel(async () => {
    if (_activeJobId) { await stopJob(_activeJobId).catch(() => {}); toast('Stopping...', 'info'); }
  });

  // Video result
  const vidWrap = el('div');
  root.appendChild(vidWrap);
  const player = createVideoPlayer(vidWrap);
  player.onStartOver(() => { player.hide(); });

  genBtn.addEventListener('click', async () => {
    const prompt = promptTA.value.trim();
    if (!prompt)                       { toast('Write a video prompt first', 'error'); return; }
    if (mode === 'i2v' && !_startImagePath) { toast('Drop a start image first', 'error');    return; }

    const m       = _models[modelSel.value] || {};
    const maxSec  = m.max_sec || 20;
    const duration = Math.min(parseFloat(durSlider.value) || 14, maxSec);

    genBtn.disabled = true;
    prog.show();
    prog.update(0, 'Submitting...');
    player.hide();

    try {
      const { job_id } = await api('/api/fun/make-it', {
        method: 'POST',
        body: JSON.stringify({
          photo_path:    mode === 'i2v' ? _startImagePath : null,
          video_prompt:  prompt,
          music_prompt:  musicIn.value.trim(),
          model:         modelSel.value,
          duration,
          steps:         parseInt(stepsSlider.value)    || 30,
          guidance:      parseFloat(guidanceSlider.value) || 7.5,
          seed:          parseInt(seedIn.value) || -1,
          skip_audio:    !audioChk.checked,
          instrumental:  instrChk.checked,
          end_photo_path: _endImagePath || null,
        }),
      });
      _activeJobId = job_id;

      pollJob(
        job_id,
        (j) => prog.update(j.progress || 0, j.message || 'Working...'),
        (j) => {
          prog.hide();
          genBtn.disabled = false;
          _activeJobId = null;
          if (j.output) {
            const url = pathToUrl(j.output);
            player.show(url, j.output);
            pushToGallery('fun-videos', j.output, promptTA.value, null, {
              mode,
              steps: Number(stepsSlider.value),
              guidance: Number(guidanceSlider.value),
              duration_sec: Number(durSlider.value),
            });
            // Handoff to bridges
            const bridgesBtn = vidWrap.querySelector('.to-bridges-btn') || (() => {
              const b = el('button', { class: 'btn btn-sm to-bridges-btn', text: '→ Add to Video Bridges', style: 'margin-top:8px;' });
              vidWrap.appendChild(b);
              return b;
            })();
            bridgesBtn.onclick = () => {
              handoff('bridges', { type: 'video', path: j.output });
              document.querySelector('[data-tab="bridges"]')?.click();
            };
          } else {
            toast('Done — check output folder', 'success');
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

  // ── Mode switching ────────────────────────────────────────────────────────
  function setMode(m) {
    mode = m;
    i2vBtn.classList.toggle('btn-primary', m === 'i2v');
    t2vBtn.classList.toggle('btn-primary', m === 't2v');
    imageCard.style.display = m === 'i2v' ? '' : 'none';
    if (m === 'i2v') {
      modeHint.textContent = 'Animates your image. Prompt describes what HAPPENS — motion, camera, action.';
      promptTA.placeholder = 'e.g. "Camera slowly pushes in, subject blinks and smiles, hair lifts in the breeze, warm light pulses across the frame"';
    } else {
      modeHint.textContent = 'Generates from text only. No image needed. Use a T2V model.';
      promptTA.placeholder = 'e.g. "A lone astronaut walks across a crimson desert at sunset, dust swirling in the wind, cinematic tracking shot"';
    }
  }

  i2vBtn.addEventListener('click', () => setMode('i2v'));
  t2vBtn.addEventListener('click', () => setMode('t2v'));
  setMode('i2v');

  // ── Palette-driven AI intent ──────────────────────────────────────────
  import('./shell/ai-intent.js?v=20260419e').then(({ registerTabAI }) => {
    registerTabAI('fun-videos', {
      getContext: () => ({
        prompt: promptTA.value,
        steps: Number(stepsSlider.value) || 0,
        guidance: Number(guidanceSlider.value) || 0,
        duration_sec: Number(durSlider.value) || 0,
      }),
      applySettings: (s) => {
        if (typeof s.steps === 'number')        stepsSlider.value    = Math.max(4, Math.min(50, s.steps));
        if (typeof s.guidance === 'number')     guidanceSlider.value = Math.max(1, Math.min(20, s.guidance));
        if (typeof s.duration_sec === 'number') durSlider.value      = Math.max(2, Math.min(20, s.duration_sec));
        if (typeof s.prompt_append === 'string' && s.prompt_append.trim()) {
          const cur = promptTA.value.trim();
          promptTA.value = cur ? `${cur}, ${s.prompt_append.trim()}` : s.prompt_append.trim();
        }
      },
    });
  }).catch(() => {});
}
