/**
 * Drop Cat Go Studio — Fun Videos tab.
 * Photo → AI video + audio pipeline with step-by-step cards.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js';
import { toast, createDropZone, createProgressCard, createVideoPlayer, createSlider, createSelect, el, escHtml } from './components.js';

function outputPathToUrl(p) {
  if (!p) return '';
  if (p.startsWith('/') || p.startsWith('http')) return p;
  // Convert absolute Windows path e.g. C:\...\output\2026-04-12\...\video.mp4
  // to /output/2026-04-12/.../video.mp4
  const norm = p.replace(/\\/g, '/');
  const idx = norm.toLowerCase().indexOf('/output/');
  if (idx !== -1) return norm.substring(idx);
  return `/output/${norm.split('/').pop()}`;
}

let state = {
  photoPath: null,
  photoUrl: null,
  endPhotoPath: null,
  analysis: null,
  prompts: [],
  selectedPrompt: null,
  jobPoller: null,
};

export function init(panel) {
  panel.innerHTML = '';
  const layout = el('div', { class: 'wide-layout' });
  panel.appendChild(layout);

  const sidebar = el('div', { class: 'sidebar' });
  const mainArea = el('div', { class: 'main-area' });
  const infoPanel = el('div', { class: 'info-panel' });
  layout.appendChild(sidebar);
  layout.appendChild(mainArea);
  layout.appendChild(infoPanel);

  // Info panel content — visible on 49" ultrawide, hidden otherwise
  infoPanel.appendChild(el('div', { class: 'card' }, [
    el('h3', { text: 'Session Files' }),
    el('p', { class: 'text-muted', style: 'font-size:.85rem', text: 'Files generated this session appear here and are available in other tabs.' }),
    el('div', { id: 'fun-session-list', class: 'file-list', style: 'max-height:300px' }),
  ]));
  infoPanel.appendChild(el('div', { class: 'card' }, [
    el('h3', { text: 'Services' }),
    el('div', { id: 'fun-services-info', style: 'font-size:.88rem; color:var(--text-2)' }),
  ]));
  infoPanel.appendChild(el('div', { class: 'card' }, [
    el('h3', { text: 'WanGP Models' }),
    el('div', { style: 'font-size:.85rem; color:var(--text-2); line-height:1.7' }, [
      el('div', { html: '<strong style="color:var(--text)">LTX-2 Dev19B</strong> — Fastest. 580p, 25fps' }),
      el('div', { html: '<strong style="color:var(--text)">Wan2.1 480P</strong> — Balanced. 480p, 16fps' }),
      el('div', { html: '<strong style="color:var(--text)">Wan2.1 720P</strong> — Best quality. 720p, slower' }),
    ]),
  ]));

  // ── Top "Make It" quick-submit button ────────────────────────────────
  // Visible at the top of the sidebar at all times so the user never needs
  // to scroll down to card4 when queueing up overnight runs.
  const makeItTopBtn = el('button', {
    class: 'btn btn-primary',
    text: '★  Make It!',
    style: 'width:100%; font-size:1.05rem; padding:10px 0; margin-bottom:10px',
  });
  sidebar.appendChild(makeItTopBtn);

  // Alias so all existing card.appendChild calls still work
  const cards = sidebar;

  // ── Card 1: Input (Photo or Text) ─────────────────────────────────────
  const card1 = el('div', { class: 'step-card' }, [
    el('h3', {}, [el('span', { class: 'step-num', text: '1' }), document.createTextNode('Input')]),
  ]);
  cards.appendChild(card1);

  // Mode toggle: Photo vs Text
  let inputMode = 'photo';
  const modeToggle = el('div', { class: 'input-mode-toggle' });
  const btnPhoto = el('button', { class: 'mode-toggle-btn on', text: '📷  Photo' });
  const btnText  = el('button', { class: 'mode-toggle-btn',     text: '📝  Text' });
  modeToggle.appendChild(btnPhoto);
  modeToggle.appendChild(btnText);
  card1.appendChild(modeToggle);

  const photoSection = el('div');
  const textSection  = el('div', { style: 'display:none' });
  card1.appendChild(photoSection);
  card1.appendChild(textSection);

  function setInputMode(mode) {
    inputMode = mode;
    const isText = mode === 'text';
    btnPhoto.classList.toggle('on', !isText);
    btnText.classList.toggle('on',  isText);
    photoSection.style.display = isText ? 'none' : '';
    textSection.style.display  = isText ? ''     : 'none';
    // Card 2 (prompts workflow) is photo-only — hide it entirely in text mode
    card2.style.display = isText ? 'none' : '';
  }
  btnPhoto.addEventListener('click', () => setInputMode('photo'));
  btnText.addEventListener('click',  () => setInputMode('text'));

  // Text input section
  const textPromptArea = el('textarea', { rows: '4', placeholder: 'Describe the scene you want to generate…\ne.g. "A lone astronaut walks across a crimson desert at sunset, dust swirling"' });
  textSection.appendChild(el('div', { class: 'form-group', style: 'margin-top:8px' }, [textPromptArea]));
  textSection.appendChild(el('p', { class: 'text-muted', style: 'font-size:.78rem', text: 'Uses Text-to-Video model (no image needed). Set model to Wan2.1-T2V in Step 4.' }));

  const previewWrap = el('div', { class: 'photo-preview-wrap', style: 'display:none' }, [
    el('img', { class: 'image-preview', id: 'fun-photo-preview' }),
    el('button', { class: 'btn btn-sm', text: 'Remove', onclick() { resetPhoto(); } }),
  ]);
  photoSection.appendChild(previewWrap);

  createDropZone(photoSection, {
    accept: 'image/*',
    label: 'Drop a photo here or click to browse',
    multiple: false,
    async onFiles(files) {
      if (!files.length) return;
      try {
        const data = await apiUpload('/api/fun/upload', files);
        const file = data.files?.[0];
        if (file) {
          state.photoPath = file.path;
          state.photoUrl = file.url || `/uploads/${file.name || ''}`;
          previewWrap.style.display = '';
          previewWrap.querySelector('img').src = state.photoUrl;
          toast('Photo uploaded', 'success');
        }
      } catch (e) { toast(e.message, 'error'); }
    },
  });

  function resetPhoto() {
    state.photoPath = null;
    state.photoUrl = null;
    state.analysis = null;
    state.prompts = [];
    state.selectedPrompt = null;
    previewWrap.style.display = 'none';
    promptsGrid.innerHTML = '';
    promptsGrid.style.display = 'none';
    selectedPromptEdit.style.display = 'none';
    analyzeBtn.disabled = false;
  }

  // ── Card 2: Analyze & Prompts ─────────────────────────────────────────
  const card2 = el('div', { class: 'step-card' }, [
    el('h3', {}, [el('span', { class: 'step-num', text: '2' }), document.createTextNode('Creative Direction')]),
  ]);
  cards.appendChild(card2);

  const directionInput = el('textarea', { placeholder: 'Optional: describe your creative vision...', rows: '2' });
  card2.appendChild(el('div', { class: 'form-group' }, [
    el('label', { text: 'Creative Direction' }),
    directionInput,
  ]));

  const creativitySlider = createSlider(card2, { label: 'Creativity', min: 1, max: 10, step: 1, value: 8 });

  const analyzeBtn = el('button', { class: 'btn btn-primary', text: 'Generate Prompts' });
  card2.appendChild(analyzeBtn);

  const promptsGrid = el('div', { class: 'prompt-grid', style: 'display:none' });
  card2.appendChild(promptsGrid);

  const selectedPromptEdit = el('textarea', { rows: '3', style: 'display:none', placeholder: 'Edit the selected prompt...' });
  card2.appendChild(selectedPromptEdit);

  analyzeBtn.addEventListener('click', async () => {
    if (!state.photoPath) { toast('Upload a photo first', 'error'); return; }
    analyzeBtn.disabled = true;
    analyzeBtn.textContent = 'Generating...';
    try {
      const data = await api('/api/fun/generate-prompts', {
        method: 'POST',
        body: JSON.stringify({
          image_path: state.photoPath,
          user_direction: directionInput.value,
          creativity: creativitySlider.value,
        }),
      });
      if (data.error) throw new Error(data.error);
      if (!data.prompts?.length) throw new Error('AI returned no prompts — try again');
      state.prompts = data.prompts;
      renderPrompts();
    } catch (e) { toast(e.message, 'error'); }
    analyzeBtn.disabled = false;
    analyzeBtn.textContent = 'Generate Prompts';
  });

  function renderPrompts() {
    promptsGrid.innerHTML = '';
    promptsGrid.style.display = 'grid';
    for (let i = 0; i < state.prompts.length; i++) {
      const p = state.prompts[i];
      const card = el('div', { class: 'prompt-card', onclick() { selectPrompt(i); } }, [
        el('div', { class: 'label', text: p.label || `Prompt ${i + 1}` }),
        el('div', { class: 'text', text: p.prompt }),
        el('div', { class: 'tags' }, [
          el('span', { class: 'tag', text: p.mood || '' }),
          el('span', { class: 'tag', text: p.style || '' }),
        ]),
      ]);
      promptsGrid.appendChild(card);
    }
  }

  function selectPrompt(idx) {
    state.selectedPrompt = idx;
    promptsGrid.querySelectorAll('.prompt-card').forEach((c, i) => {
      c.classList.toggle('selected', i === idx);
    });
    selectedPromptEdit.style.display = '';
    selectedPromptEdit.value = state.prompts[idx].prompt;
  }

  // ── Card 3: Soundtrack ───────────────────────────────────────────────
  const card3 = el('div', { class: 'step-card' });
  card3.appendChild(el('h3', {}, [el('span', { class: 'step-num', text: '3' }), document.createTextNode('Soundtrack')]));
  cards.appendChild(card3);

  // Soundtrack state
  const snd = { on: true, genres: new Set(), energy: 5, vocals: 'ai', tone: 'sardonic' };

  // ON/OFF row
  const sndToggleRow = el('div', { class: 'snd-toggle-row' });
  const sndChk = el('input', { type: 'checkbox', id: 'snd-on-chk', class: 'big-toggle-input' });
  sndChk.checked = true;
  const sndLbl = el('label', { class: 'big-toggle-label snd-size-sm', style: 'cursor:pointer' });
  sndLbl.setAttribute('for', 'snd-on-chk');
  sndLbl.appendChild(el('span', { class: 'big-toggle-thumb' }));
  sndToggleRow.appendChild(el('span', { class: 'snd-on-label', text: '🎵  Soundtrack' }));
  sndToggleRow.appendChild(el('div', { style: 'display:flex;align-items:center;gap:8px' }, [sndChk, sndLbl]));
  card3.appendChild(sndToggleRow);

  const sndBody = el('div', { class: 'snd-body' });
  card3.appendChild(sndBody);

  sndChk.addEventListener('change', () => {
    snd.on = sndChk.checked;
    sndBody.style.display = snd.on ? '' : 'none';
  });

  // ── Genre tiles ──────────────────────────────────────────────────────
  const GENRES = [
    { id: 'cinematic',   icon: '🎬', label: 'Cinematic',   prompt: 'epic cinematic score, orchestral swells' },
    { id: 'electronic',  icon: '⚡', label: 'Electronic',  prompt: 'electronic, synthesizers, driving beat' },
    { id: 'rock',        icon: '🎸', label: 'Rock',        prompt: 'rock, electric guitar, powerful drums' },
    { id: 'jazz',        icon: '🎷', label: 'Jazz',        prompt: 'jazz, saxophone, upright bass, brushed drums' },
    { id: 'ambient',     icon: '🌊', label: 'Ambient',     prompt: 'ambient, atmospheric pads, gentle textures' },
    { id: 'hiphop',      icon: '🎤', label: 'Hip-Hop',     prompt: 'hip-hop, boom bap, punchy kicks, bass groove' },
    { id: 'orchestral',  icon: '🎻', label: 'Orchestral',  prompt: 'full orchestra, strings, brass, choir' },
    { id: 'folk',        icon: '🪕', label: 'Folk',        prompt: 'folk, acoustic guitar, warm, earthy' },
  ];

  sndBody.appendChild(el('div', { class: 'snd-label', text: 'VIBE  —  pick any combo' }));
  const genreGrid = el('div', { class: 'genre-grid' });
  for (const g of GENRES) {
    const tile = el('div', { class: 'genre-tile' }, [
      el('span', { class: 'genre-icon', text: g.icon }),
      el('span', { class: 'genre-name', text: g.label }),
    ]);
    tile.addEventListener('click', () => {
      if (snd.genres.has(g.id)) { snd.genres.delete(g.id); tile.classList.remove('on'); }
      else                       { snd.genres.add(g.id);    tile.classList.add('on'); }
    });
    genreGrid.appendChild(tile);
  }
  sndBody.appendChild(genreGrid);

  // ── Energy slider ────────────────────────────────────────────────────
  sndBody.appendChild(el('div', { class: 'snd-label', text: 'ENERGY' }));
  const energyWrap = el('div', { class: 'energy-wrap' });
  energyWrap.appendChild(el('span', { class: 'energy-cap', text: '😴' }));
  const energySlider = el('input', { type: 'range', min: '1', max: '10', value: '5', class: 'energy-slider' });
  energySlider.addEventListener('input', () => { snd.energy = +energySlider.value; });
  energyWrap.appendChild(energySlider);
  energyWrap.appendChild(el('span', { class: 'energy-cap', text: '🔥' }));
  sndBody.appendChild(energyWrap);

  // ── Vocals 3-way ─────────────────────────────────────────────────────
  sndBody.appendChild(el('div', { class: 'snd-label', text: 'VOCALS' }));
  const VOCALS = [
    { id: 'none',   icon: '🔇', label: 'None' },
    { id: 'ai',     icon: '🤖', label: 'AI Writes' },
    { id: 'custom', icon: '✍️', label: 'Custom' },
  ];
  const vocalsRow = el('div', { class: 'vocals-row' });
  const vocalBtns = {};
  const toneSection    = el('div', { class: 'tone-section' });
  const customLyricsWrap = el('div', { class: 'custom-lyrics-wrap', style: 'display:none' });

  for (const v of VOCALS) {
    const btn = el('button', { class: `vocals-btn${v.id === 'ai' ? ' on' : ''}` }, [
      el('span', { class: 'vocals-icon', text: v.icon }),
      el('span', { class: 'vocals-name', text: v.label }),
    ]);
    btn.addEventListener('click', () => {
      snd.vocals = v.id;
      Object.values(vocalBtns).forEach(b => b.classList.remove('on'));
      btn.classList.add('on');
      toneSection.style.display    = v.id === 'ai'     ? '' : 'none';
      customLyricsWrap.style.display = v.id === 'custom' ? '' : 'none';
    });
    vocalBtns[v.id] = btn;
    vocalsRow.appendChild(btn);
  }
  sndBody.appendChild(vocalsRow);

  // ── Tone chips (shown when AI Writes) ────────────────────────────────
  const TONES = [
    { id: 'sardonic',  icon: '😏', label: 'Sardonic',  hint: 'sardonic, dry wit, ironic, gently mocking' },
    { id: 'uplifting', icon: '✨', label: 'Uplifting', hint: 'uplifting, positive, celebratory, heartfelt' },
    { id: 'epic',      icon: '😱', label: 'Epic',      hint: 'epic, dramatic, grandiose, mythic' },
    { id: 'absurd',    icon: '🤪', label: 'Absurd',    hint: 'absurd, surreal, dadaist, nonsensical humor' },
  ];
  const toneBtns = {};
  for (const t of TONES) {
    const btn = el('button', { class: `tone-chip${t.id === 'sardonic' ? ' on' : ''}` }, [
      el('span', { text: t.icon + '  ' + t.label }),
    ]);
    btn.addEventListener('click', () => {
      snd.tone = t.id;
      Object.values(toneBtns).forEach(b => b.classList.remove('on'));
      btn.classList.add('on');
    });
    toneBtns[t.id] = btn;
    toneSection.appendChild(btn);
  }
  sndBody.appendChild(toneSection);

  // ── Custom lyrics textarea ───────────────────────────────────────────
  const lyricsInput = el('textarea', { rows: '5', placeholder: '[verse]\nWrite your own lyrics here...\n\n[chorus]\n...' });
  customLyricsWrap.appendChild(lyricsInput);
  sndBody.appendChild(customLyricsWrap);

  // Build music prompt from genre + energy selections
  function buildMusicPrompt() {
    const parts = [];
    for (const id of snd.genres) {
      const g = GENRES.find(x => x.id === id);
      if (g) parts.push(g.prompt);
    }
    const e = snd.energy;
    if      (e <= 2) parts.push('slow, delicate, minimal');
    else if (e <= 4) parts.push('gentle, moderate pace');
    else if (e <= 6) parts.push('steady, engaging');
    else if (e <= 8) parts.push('energetic, driving');
    else             parts.push('intense, fierce, relentless');
    return parts.join(', ');
  }

  // Build tone hint for lyrics direction
  function buildToneHint() {
    return TONES.find(t => t.id === snd.tone)?.hint || '';
  }

  // ── Card 4: Generate ──────────────────────────────────────────────────
  const card4 = el('div', { class: 'step-card' }, [
    el('h3', {}, [el('span', { class: 'step-num', text: '4' }), document.createTextNode('Generate')]),
  ]);
  cards.appendChild(card4);

  const settingsRow = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:12px' });
  card4.appendChild(settingsRow);

  const modelSelect = createSelect(settingsRow, {
    label: 'Model',
    options: ['LTX-2 Dev19B Distilled', 'Wan2.1-I2V-14B-480P', 'Wan2.1-I2V-14B-720P', 'Wan2.1-T2V-14B', 'Wan2.1-T2V-1.3B'],
    value: 'LTX-2 Dev19B Distilled',
  });
  const resSelect = createSelect(settingsRow, {
    label: 'Resolution',
    options: ['480p', '580p', '720p'],
    value: '580p',
  });
  const durationSlider = createSlider(card4, { label: 'Duration', min: 4, max: 20, step: 0.5, value: 14, unit: 's' });
  const stepsSlider = createSlider(card4, { label: 'Steps', min: 4, max: 50, step: 1, value: 30 });
  const guidanceSlider = createSlider(card4, { label: 'Guidance', min: 1, max: 20, step: 0.5, value: 7.5 });

  const makeItBtn = el('button', { class: 'btn btn-primary', text: '★  Make It!', style: 'margin-top:14px; font-size:1.2rem; padding:14px 36px; width:100%' });
  card4.appendChild(makeItBtn);

  // Progress and output live in the right-hand main area — big and visible
  const progress = createProgressCard(mainArea);
  const player = createVideoPlayer(mainArea);

  // Placeholder shown in main area before anything is generated
  const placeholder = el('div', { class: 'card', style: 'text-align:center; padding:60px 20px; color:var(--text-3)' }, [
    el('div', { style: 'font-size:4rem; margin-bottom:16px', text: '🎬' }),
    el('p', { style: 'font-size:1.1rem', text: 'Your video will appear here' }),
    el('p', { style: 'font-size:.9rem; margin-top:8px', text: 'Upload a photo, pick a prompt, then hit Make It!' }),
  ]);
  mainArea.appendChild(placeholder);

  // ── Job Queue panel ───────────────────────────────────────────────────
  // Lives in the sidebar below the step-cards so it's always visible.
  // Shows all queued, running, and completed jobs as compact cards.
  const queuePanel = el('div', { class: 'card', style: 'display:none' });
  cards.appendChild(queuePanel);

  let jobQueue = []; // {id, label, photoUrl, status, progress, message, output, poller}

  function renderJobQueue() {
    if (!jobQueue.length) { queuePanel.style.display = 'none'; return; }
    queuePanel.style.display = '';
    queuePanel.innerHTML = '';

    const pending = jobQueue.filter(j => j.status === 'queued' || j.status === 'running').length;
    queuePanel.appendChild(el('h3', {
      style: 'margin-bottom:10px',
      text: `Queue  —  ${pending} pending`,
    }));

    // Newest first
    for (const j of [...jobQueue].reverse()) {
      const dotClass = { running: 'starting', done: 'running', queued: 'not_configured', error: 'error' }[j.status] || 'unknown';
      const card = el('div', {
        style: 'display:flex; gap:10px; align-items:center; padding:6px 0; border-top:1px solid var(--border)',
      });

      card.appendChild(el('span', { class: `dot ${dotClass}`, style: 'flex-shrink:0' }));

      if (j.photoUrl) {
        card.appendChild(el('img', {
          src: j.photoUrl,
          style: 'width:42px; height:42px; object-fit:cover; border-radius:var(--r-sm); flex-shrink:0',
        }));
      }

      const info = el('div', { style: 'flex:1; min-width:0' });
      info.appendChild(el('div', {
        style: 'font-size:.83rem; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis',
        text: j.label,
      }));
      const subText = j.status === 'running'
        ? `${j.progress}%  —  ${j.message}`
        : (j.status === 'done' ? 'Complete' : (j.status === 'error' ? `Error: ${j.message}` : 'Waiting in queue…'));
      info.appendChild(el('div', { style: 'font-size:.73rem; color:var(--text-2)', text: subText }));

      if (j.status === 'running') {
        const track = el('div', { style: 'margin-top:5px; height:3px; background:var(--surface-3); border-radius:2px; overflow:hidden' });
        track.appendChild(el('div', { style: `width:${j.progress}%; height:100%; background:var(--accent); transition:width .5s` }));
        info.appendChild(track);
      }
      card.appendChild(info);

      if (j.status === 'done' && j.output) {
        card.appendChild(el('button', {
          class: 'btn btn-sm',
          text: '▶ Play',
          style: 'flex-shrink:0',
          onclick() {
            progress.hide();
            player.show(outputPathToUrl(j.output), j.output);
            placeholder.style.display = 'none';
          },
        }));
      } else if (j.status === 'queued' || j.status === 'running') {
        card.appendChild(el('button', {
          class: 'btn btn-sm btn-cancel',
          text: '■',
          title: 'Cancel',
          style: 'flex-shrink:0',
          async onclick() {
            j.poller?.stop();
            await stopJob(j.id).catch(() => {});
            j.status = 'error';
            j.message = 'Cancelled';
            renderJobQueue();
          },
        }));
      }

      queuePanel.appendChild(card);
    }
  }

  // ── Shared submit logic ───────────────────────────────────────────────
  async function submitJob(btn) {
    const isText = inputMode === 'text';
    if (!isText && !state.photoPath) { toast('Upload a photo first', 'error'); return; }
    if (isText && !textPromptArea.value.trim()) { toast('Enter a scene description', 'error'); return; }
    const prompt = isText
      ? textPromptArea.value.trim()
      : (selectedPromptEdit.value || state.prompts[state.selectedPrompt]?.prompt || '');
    if (!isText && !prompt) { toast('Generate and select a prompt first', 'error'); return; }

    btn.disabled = true;

    try {
      const body = {
        photo_path: isText ? null : state.photoPath,
        video_prompt: prompt,
        use_wildcards: false,
        duration: durationSlider.value,
        model: modelSelect.value,
        resolution: resSelect.value,
        steps: stepsSlider.value,
        guidance: guidanceSlider.value,
        seed: -1,
        skip_audio: !snd.on,
        instrumental: snd.vocals === 'none',
        music_prompt: buildMusicPrompt(),
        lyrics: snd.vocals === 'custom' ? lyricsInput.value : '',
        user_direction: [directionInput.value, snd.vocals === 'ai' ? buildToneHint() : ''].filter(Boolean).join(' — '),
        bpm: null,
      };

      const data = await api('/api/fun/make-it', { method: 'POST', body: JSON.stringify(body) });

      const photoName = isText ? null : (state.photoPath || '').split(/[\\/]/).pop().replace(/\.[^.]+$/, '').slice(0, 24);
      const entry = {
        id: data.job_id,
        label: isText ? `T2V: ${prompt.slice(0, 32)}` : `Fun Video: ${photoName}`,
        photoUrl: isText ? null : state.photoUrl,
        status: 'queued',
        progress: 0,
        message: 'Waiting in queue…',
        output: null,
        poller: null,
      };
      jobQueue.push(entry);
      placeholder.style.display = 'none';
      renderJobQueue();

      // Re-enable immediately — next job can be queued right away
      btn.disabled = false;
      [makeItBtn, makeItTopBtn].forEach(b => { b.disabled = false; });

      // Show active job progress in the main progress card
      progress.show();
      progress.update(0, 'Queued…');
      progress.onCancel(async () => {
        entry.poller?.stop();
        await stopJob(entry.id).catch(() => {});
        entry.status = 'error';
        entry.message = 'Cancelled';
        progress.hide();
        renderJobQueue();
      });

      entry.poller = pollJob(
        data.job_id,
        job => {
          entry.status = 'running';
          entry.progress = job.progress;
          entry.message = job.message;
          // Only update the main progress card for the most recently running job
          const latestActive = [...jobQueue].reverse().find(j => j.status === 'running' || j.status === 'queued');
          if (latestActive === entry) progress.update(job.progress, job.message);
          renderJobQueue();
        },
        job => {
          entry.status = 'done';
          entry.output = job.output;
          entry.message = 'Complete!';
          entry.progress = 100;
          // Check if any other job is still running; if not, hide progress and show result
          const stillRunning = jobQueue.some(j => j.status === 'running' || j.status === 'queued');
          if (!stillRunning) progress.hide();
          if (job.output) {
            player.show(outputPathToUrl(job.output), job.output);
            toast(`Done: ${entry.label}`, 'success');
          }
          renderJobQueue();
        },
        err => {
          entry.status = 'error';
          entry.message = err;
          const stillRunning = jobQueue.some(j => j.status === 'running' || j.status === 'queued');
          if (!stillRunning) progress.hide();
          toast(`Failed: ${err}`, 'error');
          renderJobQueue();
        },
      );

    } catch (e) {
      btn.disabled = false;
      toast(e.message, 'error');
    }
  }

  makeItBtn.addEventListener('click', () => submitJob(makeItBtn));
  makeItTopBtn.addEventListener('click', () => submitJob(makeItTopBtn));

  player.onStartOver(() => {
    player.hide();
    resetPhoto();
  });
}
