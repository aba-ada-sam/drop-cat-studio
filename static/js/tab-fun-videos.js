/**
 * Drop Cat Go Studio -- Fun Videos tab.
 * Photo -> AI video + audio pipeline with step-by-step cards.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js';
import { toast, createDropZone, createProgressCard, createVideoPlayer, createSlider, createSelect, el, escHtml } from './components.js';
import { handoff } from './handoff.js';

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
  selectedPrompts: new Set(),
  jobPoller: null,
};

// UI references shared with receiveHandoff (populated during init)
let _ui = {};

export function init(panel) {
  panel.innerHTML = '';
  _ui.panel = panel;
  const layout = el('div', { class: 'wide-layout' });
  panel.appendChild(layout);

  const sidebar = el('div', { class: 'sidebar' });
  const mainArea = el('div', { class: 'main-area' });
  const infoPanel = el('div', { class: 'info-panel' });
  layout.appendChild(sidebar);
  layout.appendChild(mainArea);
  layout.appendChild(infoPanel);

  // Info panel content -- visible on 49" ultrawide, hidden otherwise
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
      el('div', { html: '<strong style="color:var(--text)">LTX-2 Dev19B</strong> -- Fastest. 580p, 25fps' }),
      el('div', { html: '<strong style="color:var(--text)">Wan2.1 480P</strong> -- Balanced. 480p, 16fps' }),
      el('div', { html: '<strong style="color:var(--text)">Wan2.1 720P</strong> -- Best quality. 720p, slower' }),
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
    // Card 2 (prompts workflow) is photo-only -- hide it entirely in text mode
    card2.style.display = isText ? 'none' : '';
    if (isText) promptsCard.style.display = 'none';
  }
  _ui.setInputMode = setInputMode;
  btnPhoto.addEventListener('click', () => setInputMode('photo'));
  btnText.addEventListener('click',  () => setInputMode('text'));

  // Text input section
  const textPromptArea = el('textarea', { rows: '4', placeholder: 'Describe the scene you want to generate...\ne.g. "A lone astronaut walks across a crimson desert at sunset, dust swirling"' });
  _ui.textPromptArea = textPromptArea;
  textSection.appendChild(el('div', { class: 'form-group', style: 'margin-top:8px' }, [textPromptArea]));
  textSection.appendChild(el('p', { class: 'text-muted', style: 'font-size:.78rem', text: 'Uses Text-to-Video model (no image needed). Set model to Wan2.1-T2V in Step 4.' }));

  const previewImg = el('img', { class: 'image-preview', id: 'fun-photo-preview' });
  const previewWrap = el('div', { class: 'photo-preview-wrap', style: 'display:none' }, [
    previewImg,
    el('button', { class: 'btn btn-sm', text: 'Remove', onclick() { resetPhoto(); } }),
  ]);
  _ui.previewWrap = previewWrap;
  _ui.previewImg  = previewImg;
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
          previewImg.src = state.photoUrl;
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
    state.selectedPrompts = new Set();
    previewWrap.style.display = 'none';
    promptsCard.style.display = 'none';
    promptsGrid.innerHTML = '';
    selectedPromptEdit.style.display = 'none';
    analyzeBtn.disabled = false;
  }

  // ── Card 2: Analyze & Prompts ─────────────────────────────────────────
  const card2 = el('div', { class: 'step-card' }, [
    el('h3', {}, [el('span', { class: 'step-num', text: '2' }), document.createTextNode('Creative Direction')]),
  ]);
  cards.appendChild(card2);

  const directionInput = el('textarea', { placeholder: 'Optional: describe your creative vision...', rows: '2' });
  _ui.directionInput = directionInput;
  card2.appendChild(el('div', { class: 'form-group' }, [
    el('label', { text: 'Creative Direction' }),
    directionInput,
  ]));

  const creativitySlider = createSlider(card2, { label: 'Creativity', min: 1, max: 10, step: 1, value: 8 });

  const analyzeBtn = el('button', { class: 'btn btn-primary', text: 'Generate Prompts' });
  card2.appendChild(analyzeBtn);

  // Prompts output lives in the main area so they're wide enough to read
  const promptsCard = el('div', { class: 'card', style: 'display:none' });
  const promptsHeaderRow = el('div', { class: 'prompts-header' });
  promptsHeaderRow.appendChild(el('h3', { text: 'Pick Prompts' }));
  const selAllBtn  = el('button', { class: 'btn btn-sm', text: 'All', onclick() {
    for (let i = 0; i < state.prompts.length; i++) state.selectedPrompts.add(i);
    renderPrompts();
    selectedPromptEdit.style.display = 'none';
  }});
  const selNoneBtn = el('button', { class: 'btn btn-sm', text: 'None', onclick() {
    state.selectedPrompts.clear();
    renderPrompts();
    selectedPromptEdit.style.display = 'none';
  }});
  promptsHeaderRow.appendChild(selAllBtn);
  promptsHeaderRow.appendChild(selNoneBtn);
  promptsCard.appendChild(promptsHeaderRow);
  mainArea.appendChild(promptsCard);

  const promptsGrid = el('div', { class: 'prompt-grid', style: 'margin-top:10px' });
  promptsCard.appendChild(promptsGrid);

  const selectedPromptEdit = el('textarea', { rows: '3', style: 'display:none; margin-top:10px', placeholder: 'Edit the selected prompt...' });
  promptsCard.appendChild(selectedPromptEdit);

  analyzeBtn.addEventListener('click', async () => {
    if (!state.photoPath) { toast('Upload a photo first', 'error'); return; }
    analyzeBtn.disabled = true;
    analyzeBtn.innerHTML = '<span class="spinner"></span> Analyzing image...';
    const t0 = Date.now();
    const ticker = setInterval(() => {
      const secs = Math.round((Date.now() - t0) / 1000);
      analyzeBtn.innerHTML = `<span class="spinner"></span> Generating prompts... ${secs}s`;
    }, 1000);
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
      if (!data.prompts?.length) throw new Error('AI returned no prompts -- try again');
      state.prompts = data.prompts;
      renderPrompts();
    } catch (e) { toast(e.message, 'error'); }
    clearInterval(ticker);
    analyzeBtn.disabled = false;
    analyzeBtn.textContent = 'Generate Prompts';
  });

  function updateMakeItBtnLabel() {
    const n = state.selectedPrompts.size;
    const text = n > 1 ? `★  Queue ${n} Videos` : '★  Make It!';
    makeItBtn.textContent = text;
    makeItTopBtn.textContent = text;
  }

  function renderPrompts() {
    promptsGrid.innerHTML = '';
    promptsCard.style.display = '';
    for (let i = 0; i < state.prompts.length; i++) {
      const p = state.prompts[i];
      const isSelected = state.selectedPrompts.has(i);
      const card = el('div', { class: 'prompt-card' + (isSelected ? ' selected' : ''), onclick() { togglePrompt(i); } }, [
        el('div', { class: 'prompt-card-header' }, [
          el('span', { class: 'label', text: p.label || `Prompt ${i + 1}` }),
          el('span', { class: 'prompt-check' }),
        ]),
        el('div', { class: 'text', text: p.prompt }),
        el('div', { class: 'tags' }, [
          el('span', { class: 'tag', text: p.mood || '' }),
          el('span', { class: 'tag', text: p.style || '' }),
        ]),
      ]);
      promptsGrid.appendChild(card);
    }
    updateMakeItBtnLabel();
  }

  function togglePrompt(idx) {
    if (state.selectedPrompts.has(idx)) {
      state.selectedPrompts.delete(idx);
    } else {
      state.selectedPrompts.add(idx);
    }
    promptsGrid.querySelectorAll('.prompt-card').forEach((c, i) => {
      c.classList.toggle('selected', state.selectedPrompts.has(i));
    });
    updateMakeItBtnLabel();
    // Show edit textarea only when exactly one prompt is selected
    if (state.selectedPrompts.size === 1) {
      const [only] = state.selectedPrompts;
      selectedPromptEdit.style.display = '';
      selectedPromptEdit.value = state.prompts[only].prompt;
    } else {
      selectedPromptEdit.style.display = 'none';
    }
  }

  // ── Card 3: Soundtrack ───────────────────────────────────────────────
  const card3 = el('div', { class: 'step-card' });
  card3.appendChild(el('h3', {}, [el('span', { class: 'step-num', text: '3' }), document.createTextNode('Soundtrack')]));
  cards.appendChild(card3);

  const snd = { on: true, genres: new Set(), energy: 5, vocals: 'ai', tone: 'sardonic' };

  // ON/OFF toggle row (same as before)
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

  // ═══ SECTION A: MUSIC STYLE ══════════════════════════════════════════
  // These settings shape what the MUSIC sounds like (sent to ACE-Step)
  sndBody.appendChild(el('div', { class: 'snd-section-header' }, [
    el('span', { class: 'snd-section-title', text: 'MUSIC STYLE' }),
    el('span', { class: 'snd-section-hint', text: 'what it sounds like' }),
  ]));

  const GENRES = [
    { id: 'cinematic',  icon: '🎬', label: 'Cinematic',  prompt: 'epic cinematic score, orchestral swells' },
    { id: 'electronic', icon: '⚡', label: 'Electronic', prompt: 'electronic, synthesizers, driving beat' },
    { id: 'rock',       icon: '🎸', label: 'Rock',       prompt: 'rock, electric guitar, powerful drums' },
    { id: 'jazz',       icon: '🎷', label: 'Jazz',       prompt: 'jazz, saxophone, upright bass, brushed drums' },
    { id: 'ambient',    icon: '🌊', label: 'Ambient',    prompt: 'ambient, atmospheric pads, gentle textures' },
    { id: 'hiphop',     icon: '🎤', label: 'Hip-Hop',    prompt: 'hip-hop, boom bap, punchy kicks, bass groove' },
    { id: 'orchestral', icon: '🎻', label: 'Orchestral', prompt: 'full orchestra, strings, brass, choir' },
    { id: 'folk',       icon: '🪕', label: 'Folk',       prompt: 'folk, acoustic guitar, warm, earthy' },
  ];

  const genreGrid = el('div', { class: 'genre-grid' });
  for (const g of GENRES) {
    const tile = el('div', { class: 'genre-tile' }, [
      el('span', { class: 'genre-icon', text: g.icon }),
      el('span', { class: 'genre-name', text: g.label }),
    ]);
    tile.addEventListener('click', () => {
      if (snd.genres.has(g.id)) { snd.genres.delete(g.id); tile.classList.remove('on'); }
      else { snd.genres.add(g.id); tile.classList.add('on'); }
      updateComboPreview();
    });
    genreGrid.appendChild(tile);
  }
  sndBody.appendChild(genreGrid);

  sndBody.appendChild(el('div', { class: 'snd-label', text: 'ENERGY' }));
  const energyWrap = el('div', { class: 'energy-wrap' });
  energyWrap.appendChild(el('span', { class: 'energy-cap', text: '😴' }));
  const energySlider = el('input', { type: 'range', min: '1', max: '10', value: '5', class: 'energy-slider' });
  energySlider.addEventListener('input', () => { snd.energy = +energySlider.value; });
  energyWrap.appendChild(energySlider);
  energyWrap.appendChild(el('span', { class: 'energy-cap', text: '🔥' }));
  sndBody.appendChild(energyWrap);

  // ═══ SECTION B: LYRICS ═══════════════════════════════════════════════
  // These settings control whether there are vocals and what the words say
  sndBody.appendChild(el('div', { class: 'snd-divider' }));
  sndBody.appendChild(el('div', { class: 'snd-section-header' }, [
    el('span', { class: 'snd-section-title', text: 'LYRICS' }),
    el('span', { class: 'snd-section-hint', text: 'what the words say (if any)' }),
  ]));

  const VOCALS = [
    { id: 'none',   icon: '🎹', label: 'Instrumental', sub: 'music only, no vocals' },
    { id: 'ai',     icon: '🤖', label: 'AI writes',    sub: 'AI generates lyrics' },
    { id: 'custom', icon: '✍️', label: 'I write',      sub: 'paste your own lyrics' },
  ];
  const vocalsRow = el('div', { class: 'vocals-row' });
  const vocalBtns = {};
  const lyricMoodSection = el('div');
  const customLyricsWrap = el('div', { class: 'custom-lyrics-wrap', style: 'display:none' });

  for (const v of VOCALS) {
    const btn = el('button', { class: `vocals-btn${v.id === 'ai' ? ' on' : ''}` }, [
      el('span', { class: 'vocals-icon', text: v.icon }),
      el('div', { class: 'vocals-text' }, [
        el('span', { class: 'vocals-name', text: v.label }),
        el('span', { class: 'vocals-sub', text: v.sub }),
      ]),
    ]);
    btn.addEventListener('click', () => {
      snd.vocals = v.id;
      Object.values(vocalBtns).forEach(b => b.classList.remove('on'));
      btn.classList.add('on');
      lyricMoodSection.style.display   = v.id === 'ai'     ? '' : 'none';
      customLyricsWrap.style.display   = v.id === 'custom' ? '' : 'none';
    });
    vocalBtns[v.id] = btn;
    vocalsRow.appendChild(btn);
  }
  sndBody.appendChild(vocalsRow);
  sndBody.appendChild(lyricMoodSection);
  sndBody.appendChild(customLyricsWrap);

  // ── Lyric mood (shown when AI writes) ────────────────────────────────
  // These 4 moods tell the AI what emotional angle to write from.
  // Combined with your music style, they shape the final lyric feel.
  const LYRIC_COMBOS = {
    sardonic:  { cinematic: 'sweeping drama with sardonic undercurrents', rock: 'biting sarcastic rock anthem', ambient: 'ironic dreamscape', jazz: 'dry witty jazz poetry', electronic: 'sardonic club banger', orchestral: 'pompous self-aware grandeur', hiphop: 'deadpan bars', folk: 'wry knowing folk tale' },
    uplifting: { cinematic: 'soaring triumphant theme', rock: 'anthemic crowd-pleaser', ambient: 'gentle hopeful atmosphere', jazz: 'warm jubilant jazz', electronic: 'euphoric build-up', orchestral: 'glorious celebration', hiphop: 'motivational banger', folk: 'heartfelt positive folk' },
    epic:      { cinematic: 'grand cinematic power ballad', rock: 'arena-filling rock saga', ambient: 'vast cosmic soundscape', jazz: 'explosive passionate jazz', electronic: 'massive festival drop', orchestral: 'full orchestral epic', hiphop: 'mythic rap narrative', folk: 'legendary folk ballad' },
    absurd:    { cinematic: 'surreal movie nightmare', rock: 'dadaist noise anthem', ambient: 'drifting fever dream', jazz: 'chaotic avant-garde jazz', electronic: 'glitchy fever rave', orchestral: 'theatrical chaos', hiphop: 'nonsense bars over beats', folk: 'whimsical nonsense fairy tale' },
  };

  const TONES = [
    { id: 'sardonic',  icon: '😏', label: 'Sardonic',  desc: 'dry wit & irony',       hint: 'sardonic, dry wit, ironic, gently mocking' },
    { id: 'uplifting', icon: '✨', label: 'Uplifting', desc: 'hopeful & celebratory',  hint: 'uplifting, positive, celebratory, heartfelt' },
    { id: 'epic',      icon: '😱', label: 'Epic',      desc: 'dramatic & grandiose',   hint: 'epic, dramatic, grandiose, mythic' },
    { id: 'absurd',    icon: '🤪', label: 'Absurd',    desc: 'surreal & weird',        hint: 'absurd, surreal, dadaist, nonsensical humor' },
  ];

  lyricMoodSection.appendChild(el('div', { class: 'snd-label', text: 'LYRIC MOOD' }));
  const comboPreview = el('div', { class: 'snd-combo-preview', text: '<- pick a music genre above to see the combo' });
  lyricMoodSection.appendChild(comboPreview);

  const toneGrid = el('div', { class: 'tone-chip-grid' });
  const toneBtns = {};
  for (const t of TONES) {
    const chip = el('div', { class: `tone-chip-card${t.id === 'sardonic' ? ' on' : ''}` }, [
      el('span', { class: 'tone-chip-icon', text: t.icon }),
      el('span', { class: 'tone-chip-label', text: t.label }),
      el('span', { class: 'tone-chip-desc', text: t.desc }),
    ]);
    chip.addEventListener('click', () => {
      snd.tone = t.id;
      Object.values(toneBtns).forEach(b => b.classList.remove('on'));
      chip.classList.add('on');
      updateComboPreview();
    });
    toneBtns[t.id] = chip;
    toneGrid.appendChild(chip);
  }
  lyricMoodSection.appendChild(toneGrid);

  function updateComboPreview() {
    const genres = [...snd.genres];
    if (!genres.length) {
      comboPreview.textContent = '<- pick a music genre above to see the combo';
      comboPreview.style.opacity = '0.45';
      return;
    }
    const g = genres[0];
    const example = LYRIC_COMBOS[snd.tone]?.[g] || `${snd.tone} ${GENRES.find(x => x.id === g)?.label || g} music`;
    const extra = genres.length > 1 ? ` + ${genres.length - 1} more genre${genres.length > 2 ? 's' : ''}` : '';
    comboPreview.textContent = `-> ${example}${extra}`;
    comboPreview.style.opacity = '';
  }
  updateComboPreview();

  // Custom lyrics textarea
  const lyricsInput = el('textarea', { rows: '5', placeholder: '[verse]\nWrite your own lyrics here...\n\n[chorus]\n...' });
  customLyricsWrap.appendChild(lyricsInput);

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

  // ── LoRA selector ─────────────────────────────────────────────────────
  const loraSection = el('div', { style: 'margin-top:12px' });
  card4.appendChild(loraSection);

  const loraHeader = el('div', { style: 'display:flex; align-items:center; gap:8px; margin-bottom:6px' }, [
    el('label', { text: 'LoRAs', style: 'font-size:.82rem; color:var(--text-3); font-weight:600' }),
    el('span', { id: 'lora-count', style: 'font-size:.72rem; color:var(--text-3)' }),
  ]);
  loraSection.appendChild(loraHeader);
  const loraList = el('div', { style: 'display:flex; flex-direction:column; gap:6px' });
  loraSection.appendChild(loraList);

  let _loadedLoraModel = '';

  async function loadLoras(model) {
    if (model === _loadedLoraModel) return;
    _loadedLoraModel = model;
    loraList.innerHTML = '';
    document.getElementById('lora-count').textContent = '';
    try {
      const data = await api(`/api/fun/loras?model=${encodeURIComponent(model)}`);
      const loras = data.loras || [];
      document.getElementById('lora-count').textContent = loras.length ? `(${loras.length} available)` : '(none in this model\'s directory)';
      for (const lora of loras) {
        const chk = el('input', { type: 'checkbox', style: 'flex-shrink:0' });
        const multIn = el('input', { type: 'number', value: '1.0', min: '0.1', max: '2', step: '0.05',
          style: 'width:58px; text-align:center; padding:3px 5px; font-size:.78rem',
          title: 'Multiplier (strength)', disabled: true });
        chk.addEventListener('change', () => { multIn.disabled = !chk.checked; });
        loraList.appendChild(el('div', {
          style: 'display:flex; align-items:center; gap:8px; padding:5px 8px; background:var(--surface-2); border-radius:var(--r-sm)',
          dataset: { loraPath: lora.path },
        }, [
          chk,
          el('span', { text: lora.name, style: 'flex:1; font-size:.78rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap', title: lora.name }),
          multIn,
        ]));
      }
    } catch (_) {}
  }

  function getSelectedLoras() {
    return [...loraList.querySelectorAll('div[data-lora-path]')]
      .filter(row => row.querySelector('input[type="checkbox"]').checked)
      .map(row => ({
        path: row.dataset.loraPath,
        multiplier: parseFloat(row.querySelector('input[type="number"]').value) || 1.0,
      }));
  }

  modelSelect.addEventListener('change', () => loadLoras(modelSelect.value));
  // Load on init
  setTimeout(() => loadLoras(modelSelect.value), 0);

  const makeItBtn = el('button', { class: 'btn btn-primary', text: '★  Make It!', style: 'margin-top:14px; font-size:1.2rem; padding:14px 36px; width:100%' });
  card4.appendChild(makeItBtn);

  // ── Progress + player in main area ───────────────────────────────────
  const progress = createProgressCard(mainArea);
  const player = createVideoPlayer(mainArea);

  // ── Extended player controls (portfolio nav, version toggle, delete) ──
  let currentJob = null;

  const playerExtras = el('div', { class: 'card fun-player-extras', style: 'display:none' });
  mainArea.insertBefore(playerExtras, player.el);

  // Navigation row: ◀ 2 of 5 ▶
  const navRow = el('div', { class: 'player-nav-row', style: 'display:none' });
  const prevBtn = el('button', { class: 'btn btn-sm', text: '◀ Prev' });
  const navLabel = el('span', { class: 'player-nav-label' });
  const nextBtn = el('button', { class: 'btn btn-sm', text: 'Next ▶' });
  navRow.appendChild(prevBtn);
  navRow.appendChild(navLabel);
  navRow.appendChild(nextBtn);
  playerExtras.appendChild(navRow);

  // Version toggle: with music vs. raw video only
  const audioRow = el('div', { class: 'player-audio-row', style: 'display:none' });
  const btnWithAudio = el('button', { class: 'btn btn-sm player-ver-btn active-ver', text: '🎵 With music' });
  const btnNoAudio   = el('button', { class: 'btn btn-sm player-ver-btn', text: '🔇 Video only (no music)' });
  audioRow.appendChild(btnWithAudio);
  audioRow.appendChild(btnNoAudio);
  playerExtras.appendChild(audioRow);

  // Resubmit / flow row
  const actionRow = el('div', { class: 'player-action-row' });
  actionRow.appendChild(el('button', { class: 'btn btn-sm', text: '⟲ Resubmit',
    onclick() { resubmitCurrentJob(); } }));
  actionRow.appendChild(el('button', { class: 'btn btn-sm', text: '◀ Prequel',
    async onclick() {
      if (!currentJob?.output) return;
      await receiveHandoff({ type: 'prequel', path: currentJob.output, name: currentJob.label });
    } }));
  actionRow.appendChild(el('button', { class: 'btn btn-sm', text: 'Sequel ▶',
    async onclick() {
      if (!currentJob?.output) return;
      await receiveHandoff({ type: 'sequel', path: currentJob.output, name: currentJob.label });
    } }));
  actionRow.appendChild(el('button', { class: 'btn btn-sm', text: '-> Bridges',
    onclick() { sendToBridges(); } }));
  playerExtras.appendChild(actionRow);

  // Delete row
  const deleteRow = el('div', { class: 'player-delete-row' });
  deleteRow.appendChild(el('button', { class: 'btn btn-sm btn-cancel', text: '🗑 Delete this video',
    async onclick() {
      if (!currentJob?.output) return;
      if (!confirm('Delete this video file?')) return;
      try {
        await api('/api/output/delete', { method: 'POST', body: JSON.stringify({ path: currentJob.output }) });
        currentJob.output = null;
        player.hide();
        playerExtras.style.display = 'none';
        renderJobQueue();
        toast('Video deleted', 'success');
      } catch (e) { toast('Delete failed: ' + e.message, 'error'); }
    },
  }));
  deleteRow.appendChild(el('button', { class: 'btn btn-sm btn-cancel', text: '🗑 Delete entire folder (image + videos + audio)',
    async onclick() {
      if (!currentJob?.output) return;
      if (!confirm('Delete the entire job folder?\n\nThis removes: source image, raw video, audio file, and merged video.')) return;
      try {
        await api('/api/output/delete', { method: 'POST', body: JSON.stringify({ path: currentJob.output, folder: true }) });
        currentJob.output = null; currentJob.videoOnly = null; currentJob.audioPath = null;
        player.hide();
        playerExtras.style.display = 'none';
        renderJobQueue();
        toast('Folder deleted', 'success');
      } catch (e) { toast('Delete failed: ' + e.message, 'error'); }
    },
  }));
  playerExtras.appendChild(deleteRow);

  function showJobInPlayer(entry) {
    currentJob = entry;
    player.show(outputPathToUrl(entry.output), entry.output);
    placeholder.style.display = 'none';
    progress.hide();
    playerExtras.style.display = '';
    // Version toggle: only show when raw video is available
    audioRow.style.display = entry.videoOnly ? '' : 'none';
    btnWithAudio.classList.add('active-ver');
    btnNoAudio.classList.remove('active-ver');
    updateNavButtons();
  }

  function updateNavButtons() {
    const done = jobQueue.filter(j => j.status === 'done' && j.output);
    const idx = done.indexOf(currentJob);
    navRow.style.display = done.length > 1 ? '' : 'none';
    navLabel.textContent = done.length > 1 ? `${idx + 1} of ${done.length}` : '';
    prevBtn.disabled = idx <= 0;
    nextBtn.disabled = idx >= done.length - 1;
  }

  prevBtn.addEventListener('click', () => {
    const done = jobQueue.filter(j => j.status === 'done' && j.output);
    const idx = done.indexOf(currentJob);
    if (idx > 0) showJobInPlayer(done[idx - 1]);
  });
  nextBtn.addEventListener('click', () => {
    const done = jobQueue.filter(j => j.status === 'done' && j.output);
    const idx = done.indexOf(currentJob);
    if (idx < done.length - 1) showJobInPlayer(done[idx + 1]);
  });

  btnWithAudio.addEventListener('click', () => {
    if (!currentJob?.output) return;
    player.show(outputPathToUrl(currentJob.output), currentJob.output);
    btnWithAudio.classList.add('active-ver');
    btnNoAudio.classList.remove('active-ver');
  });
  btnNoAudio.addEventListener('click', () => {
    if (!currentJob?.videoOnly) { toast('Raw video not available for this job', 'info'); return; }
    player.show(outputPathToUrl(currentJob.videoOnly), currentJob.videoOnly);
    btnNoAudio.classList.add('active-ver');
    btnWithAudio.classList.remove('active-ver');
  });

  function resubmitCurrentJob() {
    if (!currentJob) return;
    const s = currentJob.settings || {};
    if (s.duration !== undefined) durationSlider.value = s.duration;
    if (s.steps !== undefined) stepsSlider.value = s.steps;
    if (s.guidance !== undefined) guidanceSlider.value = s.guidance;
    if (s.model) modelSelect.value = s.model;
    if (s.resolution) resSelect.value = s.resolution;
    if (currentJob.prompt) {
      state.selectedPrompts.clear();
      selectedPromptEdit.style.display = '';
      selectedPromptEdit.value = currentJob.prompt;
      promptsCard.style.display = '';
      updateMakeItBtnLabel();
    }
    toast('Settings loaded -- tweak them and hit Make It!', 'info');
    sidebar.scrollIntoView({ behavior: 'smooth' });
  }

  function sendToBridges() {
    if (!currentJob?.output) return;
    handoff('bridges', { type: 'video', path: currentJob.output, name: currentJob.label });
    document.querySelector('[data-tab="bridges"]')?.click();
    toast('Video sent to Bridges -- add more clips there', 'info');
  }

  // Lightweight placeholder -- hidden as soon as any job is submitted
  const placeholder = el('div', {
    style: 'text-align:center; padding:14px 10px; color:var(--text-3); font-size:.85rem',
    text: 'Upload a photo and generate prompts to get started.',
  });
  mainArea.appendChild(placeholder);

  // ── Job Queue panel ───────────────────────────────────────────────────
  const queuePanel = el('div', { class: 'card' });
  mainArea.insertBefore(queuePanel, progress.el);

  let jobQueue = []; // {id, label, photoUrl, status, progress, message, output, videoOnly, audioPath, prompt, settings, poller}

  function renderJobQueue() {
    queuePanel.innerHTML = '';
    if (!jobQueue.length) {
      queuePanel.appendChild(el('h3', { style: 'margin-bottom:6px', text: 'Queue' }));
      queuePanel.appendChild(el('div', { style: 'font-size:.82rem; color:var(--text-3); padding:6px 0', text: 'No jobs yet. Select prompts and hit Make It!' }));
      return;
    }

    const pending = jobQueue.filter(j => j.status === 'queued' || j.status === 'running').length;
    queuePanel.appendChild(el('h3', {
      style: 'margin-bottom:10px',
      text: `Queue  --  ${pending} pending`,
    }));

    // Newest first
    for (const j of [...jobQueue].reverse()) {
      const dotClass = { running: 'starting', done: 'running', queued: 'not_configured', error: 'error' }[j.status] || 'unknown';
      const card = el('div', {
        style: 'display:flex; gap:10px; align-items:center; padding:6px 0; border-top:1px solid var(--border)',
      });

      card.appendChild(el('span', { class: `dot ${dotClass}`, style: 'flex-shrink:0' }));

      if (j.status === 'done' && j.output) {
        const previewVid = el('video', {
          muted: 'true', preload: 'metadata',
          style: 'width:64px; height:64px; object-fit:cover; border-radius:var(--r-sm); flex-shrink:0; cursor:pointer',
          title: 'Click to play in main player',
        });
        previewVid.src = outputPathToUrl(j.output) + '#t=0.5';
        previewVid.addEventListener('loadedmetadata', () => {
          previewVid.currentTime = Math.min(0.5, previewVid.duration / 2);
        });
        previewVid.addEventListener('click', () => showJobInPlayer(j));
        card.appendChild(previewVid);
      } else if (j.photoUrl) {
        card.appendChild(el('img', {
          src: j.photoUrl,
          style: 'width:64px; height:64px; object-fit:cover; border-radius:var(--r-sm); flex-shrink:0',
        }));
      }

      const info = el('div', { style: 'flex:1; min-width:0' });
      info.appendChild(el('div', {
        style: 'font-size:.83rem; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis',
        text: j.label,
      }));
      const subText = j.status === 'running'
        ? `${j.progress}%  --  ${j.message}`
        : (j.status === 'done' ? 'Complete' : (j.status === 'error' ? `Error: ${j.message}` : 'Waiting in queue...'));
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
          onclick() { showJobInPlayer(j); },
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

  // ── Serial job poller — polls one job at a time to avoid flooding ────

  let _activePoller = null;

  function _startNextPoller() {
    // Already polling something?
    if (_activePoller) return;
    // Find the next unfinished job without a poller
    const next = jobQueue.find(j => !j.poller && (j.status === 'queued' || j.status === 'running'));
    if (!next) return;

    _activePoller = next;
    next.poller = pollJob(
      next.id,
      job => {
        next.status = 'running';
        next.progress = job.progress;
        next.message = job.message;
        progress.update(job.progress, job.message);
        renderJobQueue();
      },
      job => {
        next.status = 'done';
        next.output = job.output;
        next.videoOnly = job.meta?.video_path || null;
        next.audioPath = job.meta?.audio_path || null;
        next.message = 'Complete!';
        next.progress = 100;
        if (job.output) {
          showJobInPlayer(next);
          toast(`Done: ${next.label}`, 'success');
        }
        renderJobQueue();
        _activePoller = null;
        // Poll the next queued job, or hide progress if all done
        if (!_startNextPoller()) progress.hide();
      },
      err => {
        next.status = 'error';
        next.message = err;
        toast(`Failed: ${err}`, 'error');
        renderJobQueue();
        _activePoller = null;
        if (!_startNextPoller()) progress.hide();
      },
    );
    return true;  // started a poller
  }

  // ── Shared submit logic ───────────────────────────────────────────────

  // Submit a single video job. Returns the entry or throws.
  async function _submitOne(prompt, promptLabel, isText) {
    const photoName = isText ? null : (state.photoPath || '').split(/[\\/]/).pop().replace(/\.[^.]+$/, '').slice(0, 24);
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
      user_direction: [directionInput.value, snd.vocals === 'ai' ? buildToneHint() : ''].filter(Boolean).join(' -- '),
      bpm: null,
      loras: getSelectedLoras(),
    };

    const data = await api('/api/fun/make-it', { method: 'POST', body: JSON.stringify(body) });

    const baseLabel = isText ? `T2V: ${prompt.slice(0, 32)}` : `Fun Video: ${photoName}`;
    const entry = {
      id: data.job_id,
      label: promptLabel ? `${baseLabel} (${promptLabel})` : baseLabel,
      photoUrl: isText ? null : state.photoUrl,
      status: 'queued',
      progress: 0,
      message: 'Waiting in queue...',
      output: null,
      videoOnly: null,
      audioPath: null,
      prompt,
      settings: {
        duration: durationSlider.value,
        steps: stepsSlider.value,
        guidance: guidanceSlider.value,
        model: modelSelect.value,
        resolution: resSelect.value,
      },
      poller: null,
    };
    jobQueue.push(entry);
    _startNextPoller();

    return entry;
  }

  async function submitJob(btn) {
    const isText = inputMode === 'text';
    if (!isText && !state.photoPath) { toast('Upload a photo first', 'error'); return; }
    if (isText && !textPromptArea.value.trim()) { toast('Enter a scene description', 'error'); return; }

    // Build list of {prompt, label} to queue
    let queue = [];
    if (isText) {
      queue = [{ prompt: textPromptArea.value.trim(), label: '' }];
    } else if (state.selectedPrompts.size > 0) {
      const sorted = [...state.selectedPrompts].sort((a, b) => a - b);
      if (sorted.length === 1) {
        const idx = sorted[0];
        const p = selectedPromptEdit.value.trim() || state.prompts[idx]?.prompt || '';
        queue = [{ prompt: p, label: '' }];
      } else {
        queue = sorted.map(i => ({
          prompt: state.prompts[i]?.prompt || '',
          label: state.prompts[i]?.label || `Prompt ${i + 1}`,
        })).filter(x => x.prompt);
      }
    } else if (selectedPromptEdit.style.display !== 'none' && selectedPromptEdit.value.trim()) {
      // Resubmit fallback: edit textarea has a prompt but nothing is selected
      queue = [{ prompt: selectedPromptEdit.value.trim(), label: '' }];
    } else {
      toast('Select at least one prompt first', 'error');
      return;
    }

    if (!queue.length) { toast('No valid prompts to queue', 'error'); return; }

    btn.disabled = true;
    placeholder.style.display = 'none';
    progress.show();
    progress.update(0, queue.length > 1 ? `Queuing ${queue.length} videos...` : 'Queued...');

    try {
      for (const { prompt, label } of queue) {
        await _submitOne(prompt, label, isText);
      }

      btn.disabled = false;
      [makeItBtn, makeItTopBtn].forEach(b => { b.disabled = false; });
      renderJobQueue();

      progress.onCancel(async () => {
        for (const j of jobQueue.filter(j => j.status === 'queued' || j.status === 'running')) {
          j.poller?.stop();
          await stopJob(j.id).catch(() => {});
          j.status = 'error';
          j.message = 'Cancelled';
        }
        progress.hide();
        renderJobQueue();
      });

      if (queue.length > 1) toast(`${queue.length} videos queued!`, 'success');

    } catch (e) {
      btn.disabled = false;
      toast(e.message, 'error');
    }
  }

  makeItBtn.addEventListener('click', () => submitJob(makeItBtn));
  makeItTopBtn.addEventListener('click', () => submitJob(makeItTopBtn));

  // Render initial empty queue state
  renderJobQueue();

  player.onStartOver(() => {
    player.hide();
    playerExtras.style.display = 'none';
    currentJob = null;
    placeholder.style.display = 'none';
    resetPhoto();
  });
}

/**
 * Receive a cross-tab handoff. Called by app.js switchTab after activation,
 * or directly from within this tab (prequel/sequel buttons).
 *
 * Supported types:
 *   image   -- pre-fill the photo input from a file path
 *   sequel  -- extract last frame of a video, pre-fill as photo with direction hint
 *   prequel -- extract first frame of a video, pre-fill as photo with direction hint
 *   concept -- switch to text mode, pre-fill the scene textarea
 */
export async function receiveHandoff(data) {
  if (!_ui.panel) return;

  const { type, path, url, name } = data;

  if (type === 'image' || type === 'sequel' || type === 'prequel') {
    let imgPath = path;
    let imgUrl  = url || (path ? `/output/${path.split(/[\\/]/).pop()}` : null);
    let direction = '';

    if (type === 'sequel' || type === 'prequel') {
      const position = type === 'sequel' ? 0.97 : 0.03;
      try {
        toast(type === 'sequel' ? 'Extracting last frame...' : 'Extracting first frame...', 'info');
        const frame = await api('/api/tools/extract-frame', {
          method: 'POST',
          body: JSON.stringify({ path, position }),
        });
        imgPath   = frame.path;
        imgUrl    = frame.url;
        direction = type === 'sequel'
          ? `Continue the story from: "${name || 'previous video'}"`
          : `What came before: "${name || 'this video'}"`;
      } catch (e) {
        toast('Frame extraction failed: ' + e.message, 'error');
        return;
      }
    }

    _ui.setInputMode?.('photo');
    state.photoPath  = imgPath;
    state.photoUrl   = imgUrl;
    if (_ui.previewImg)  _ui.previewImg.src = imgUrl;
    if (_ui.previewWrap) _ui.previewWrap.style.display = '';
    if (direction && _ui.directionInput) _ui.directionInput.value = direction;

    toast(
      type === 'image'   ? 'Image loaded -- generate prompts to continue' :
      type === 'sequel'  ? 'Last frame loaded -- generate sequel prompts' :
                           'First frame loaded -- generate prequel prompts',
      'success',
    );
    _ui.panel.scrollIntoView?.({ behavior: 'smooth', block: 'start' });

  } else if (type === 'concept') {
    _ui.setInputMode?.('text');
    if (_ui.textPromptArea) _ui.textPromptArea.value = data.text || '';
    toast('Concept loaded -- ready to generate', 'success');
    _ui.panel.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
  }
}
