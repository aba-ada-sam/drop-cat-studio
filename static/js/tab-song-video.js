/**
 * Drop Cat Go Studio -- Music Video tab.
 * Drop a song -> AI analyzes BPM/key/energy -> generates a full-length music video.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js?v=20260505e';
import { el, pathToUrl } from './components.js?v=20260507a';
import { toast, apiFetch } from './shell/toast.js?v=20260503a';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260503g';
export function receiveHandoff(data) {
  // no-op -- this tab doesn't currently receive handoffs
}

export function init(panel) {
  panel.innerHTML = '';
  const root = el('div', {
    style: 'max-width:720px; margin:0 auto; padding:24px 16px; display:flex; flex-direction:column; gap:20px;',
  });
  panel.appendChild(root);

  // -- State -----------------------------------------------------------------
  let _audioPath     = null;
  let _audioDuration = 0;
  let _audioAnalysis = null;
  let _imagePath     = null;
  let _model         = 'LTX-2 Dev19B Distilled';
  let _clipDur       = 8;
  let _numClips      = 0;     // auto-calculated
  let _qualityPx     = 360;   // Draft by default -- fastest, still watchable on screen
  let _outW          = 640;
  let _outH          = 352;
  let _steps         = 4;     // LTX Distilled is designed for 4-8 steps -- 30 is wasted time
  let _guidance      = 7.5;
  let _jobId         = null;
  let _analyzeSeq    = 0;   // incremented on each new analysis; stale responses check against this
  let _lyricsTextarea = null;  // editable lyrics field -- ref set by _renderAnalysis
  let _loopMode      = false;
  let _aiVariety     = true;  // on by default -- every run gets a fresh visual theme
  let _loopCount     = 0;
  let _stopAfter     = false;
  let _varietyIdx    = 0;

  const _VARIETY_THEMES = [
    'Abstract geometric forms and flowing light, motion-blurred energy trails',
    'Wide flat plains at golden hour, crops bending in wind, long shadows reaching east',
    'Urban night -- neon-lit rain-slick streets, city pulse and electric glow',
    'Underwater world -- bioluminescent drift, slow graceful motion, deep blues',
    'Space journey -- nebula clouds, star fields, vast scale, time-lapse universe',
    'Fire and molten metal -- ember cascades, plasma arcs, intense heat shimmer',
    'Dancers and performers -- fabric billowing, choreographic peaks and lulls',
    'Desert and canyon -- heat shimmer, red rock, ancient silent stone',
    'Arctic and aurora -- frozen geometry, northern lights, crystalline stillness',
    'Macro nature -- water droplets, insect wings, pollen in shafts of light',
    'Surreal dreamscape -- floating objects, impossible architecture, fluid reality',
    'Storm and lightning -- dark clouds, electric chaos, rain-drenched intensity',
  ];

  const QUALITIES = [
    { label: 'Draft  360P', px: 360, maxSec: 19 },  // ~640x352 -- fastest, ~35% fewer pixels than 480P
    { label: '480P',        px: 480, maxSec: 19 },
    { label: '580P',        px: 580, maxSec: 19 },
    { label: '720P',        px: 720, maxSec: 14 },   // high res: cap clips shorter to avoid sliding window
  ];

  function _computeDims(px) {
    // Always 16:9 for music video (most common)
    const h = px, w = Math.round(px * 16 / 9);
    const r32 = n => Math.max(32, Math.round(n / 32) * 32);
    return [r32(w), r32(h)];
  }
  [_outW, _outH] = _computeDims(_qualityPx);

  // -- Heading ---------------------------------------------------------------
  root.appendChild(el('div', { style: 'text-align:center; padding-bottom:4px;' }, [
    el('div', { style: 'font-size:1.4rem; font-weight:700; color:var(--text); margin-bottom:6px;', text: 'Music Video' }),
    el('div', { style: 'font-size:.85rem; color:var(--text-3);', text: 'Drop a song. AI reads its energy and chains as many clips as needed to cover the full length.' }),
  ]));

  // -- Song drop zone --------------------------------------------------------
  const audioInput    = el('input', { type: 'file', accept: 'audio/*,.mp3,.wav,.flac,.ogg,.m4a,.aac,.mpeg,.mpg', style: 'display:none' });
  const audioHint     = el('div', { style: 'color:var(--text-3); font-size:.88rem;', text: 'Drop your song here or click to browse (mp3, wav, flac, m4a, mpeg...)' });
  const audioPreview  = el('audio', { controls: '', style: 'display:none; width:100%; margin-top:8px;' });
  const audioClearBtn = el('button', {
    style: 'display:none; position:absolute; top:6px; right:6px; width:24px; height:24px; border-radius:50%; border:none; background:rgba(0,0,0,.65); color:#fff; font-size:15px; line-height:1; cursor:pointer; z-index:2; padding:0;',
    title: 'Remove song', text: 'x',
  });
  const audioDropZone = el('div', { class: 'drop-zone', style: 'position:relative; min-height:80px; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:8px;' },
    [audioHint, audioPreview, audioClearBtn]);
  root.appendChild(audioDropZone);

  // Analysis result card (shown after upload + analysis)
  const analysisCard = el('div', { class: 'card', style: 'display:none; padding:12px 14px; flex-direction:column; gap:8px;' });
  root.appendChild(analysisCard);

  function _renderAnalysis(a) {
    analysisCard.innerHTML = '';

    // -- Header chips ------------------------------------------------------
    const chips = [
      a.duration_display && { icon: '♪', text: a.duration_display },
      a.bpm              && { icon: '♩', text: `${a.bpm} BPM` },
      a.key              && { icon: '♯', text: `${a.key} ${a.mode || ''}`.trim() },
      a.mood             && { icon: '*', text: a.mood.charAt(0).toUpperCase() + a.mood.slice(1) },
      a.energy           && { icon: '⚡', text: `${a.energy} energy` },
    ].filter(Boolean);

    const chipRow = el('div', { style: 'display:flex; gap:8px; flex-wrap:wrap; align-items:center;' });
    chips.forEach(c => {
      chipRow.appendChild(el('span', {
        style: 'display:inline-flex; align-items:center; gap:4px; padding:3px 10px; border-radius:20px; background:var(--bg-raised); border:1px solid var(--border-2); font-size:.78rem; color:var(--text-2);',
        text: `${c.icon} ${c.text}`,
      }));
    });

    if (!a.has_rich_analysis) {
      chipRow.appendChild(el('span', {
        style: 'font-size:.72rem; color:var(--text-3); font-style:italic;',
        text: '(install librosa for BPM/key/energy analysis)',
      }));
    }

    const clipsNote = el('div', {
      style: 'font-size:.78rem; color:var(--text-3);',
      text: `Suggested: ${a.suggested_num_clips} clips x ${a.suggested_clip_dur}s = ~${Math.round(a.suggested_num_clips * a.suggested_clip_dur)}s`,
    });

    analysisCard.appendChild(el('div', { style: 'font-size:.72rem; color:var(--text-3); text-transform:uppercase; letter-spacing:.05em; margin-bottom:2px;', text: 'Song analysis' }));
    analysisCard.appendChild(chipRow);
    analysisCard.appendChild(clipsNote);

    // -- Per-clip energy strip ---------------------------------------------
    // Shows what the AI actually sees when writing motion prompts, so you can
    // spot if the song reads as uniformly MED and adjust the idea accordingly.
    const profile = a.energy_profile || [];
    const labels  = a.clip_energy_labels || [];
    if (profile.length > 0) {
      const LABEL_COLOR = { HIGH: '#e05c5c', MED: '#d4a017', LOW: '#5b9bd4' };

      const stripWrap = el('div', { style: 'display:flex; flex-direction:column; gap:4px;' });
      stripWrap.appendChild(el('div', {
        style: 'font-size:.72rem; color:var(--text-3); text-transform:uppercase; letter-spacing:.05em;',
        text: 'Per-clip energy (what the AI reads)',
      }));

      const strip = el('div', {
        style: 'display:flex; gap:2px; align-items:flex-end; height:36px; width:100%;',
      });

      profile.forEach((e, i) => {
        const lbl   = labels[i] || (e > 0.7 ? 'HIGH' : e > 0.35 ? 'MED' : 'LOW');
        const color = LABEL_COLOR[lbl] || 'var(--accent)';
        const pct   = Math.max(20, Math.round(e * 100));  // min 20% so LOW bars are visible
        const bar   = el('div', {
          title: `Clip ${i + 1}: ${lbl} (${Math.round(e * 100)}%)`,
          style: [
            'flex:1; min-width:4px; border-radius:2px 2px 0 0;',
            `height:${pct}%; background:${color};`,
            'cursor:default; transition:opacity .15s;',
          ].join(' '),
        });
        bar.addEventListener('mouseenter', () => { bar.style.opacity = '0.7'; });
        bar.addEventListener('mouseleave', () => { bar.style.opacity = '1'; });
        strip.appendChild(bar);
      });

      // Label legend
      const legend = el('div', { style: 'display:flex; gap:12px; flex-wrap:wrap;' });
      [['HIGH', '#e05c5c', 'explosive action'], ['MED', '#d4a017', 'dynamic motion'], ['LOW', '#5b9bd4', 'graceful/slow']].forEach(([lbl, col, desc]) => {
        legend.appendChild(el('span', {
          style: `font-size:.7rem; color:${col};`,
          text: `* ${lbl} -- ${desc}`,
        }));
      });

      stripWrap.appendChild(strip);
      stripWrap.appendChild(legend);
      analysisCard.appendChild(stripWrap);
    }

    // -- Lyrics section ----------------------------------------------------
    _lyricsTextarea = el('textarea', {
      rows: '3',
      style: 'width:100%; resize:vertical; font-size:.78rem; color:var(--text-2); font-family:inherit; background:var(--bg-input,var(--bg-raised)); border:1px solid var(--border-2); border-radius:4px; padding:6px 8px; box-sizing:border-box;',
      placeholder: 'Leave blank to auto-detect lyrics during generation, or type theme words to override',
    });
    _lyricsTextarea.value = (a.lyrics_text || '').trim();
    analysisCard.appendChild(el('div', { style: 'display:flex; flex-direction:column; gap:4px; margin-top:4px;' }, [
      el('div', { style: 'font-size:.72rem; color:var(--text-3); text-transform:uppercase; letter-spacing:.05em;', text: 'Detected lyrics -- edit to correct' }),
      _lyricsTextarea,
    ]));

    analysisCard.style.display = 'flex';

    // Apply suggestions to settings; clamp to current quality ceiling
    if (a.suggested_clip_dur) {
      _clipDur = Math.min(a.suggested_clip_dur, parseInt(clipSlider.max) || 20);
      clipSlider.value = String(_clipDur);
      clipLabel.textContent = `${_clipDur}s`;
    }
    _refreshClipCount();
  }

  async function _analyzeAudio(path) {
    const seq = ++_analyzeSeq;
    analysisCard.style.display = 'none';
    analysisCard.innerHTML = '';
    analysisCard.appendChild(el('div', { style: 'font-size:.8rem; color:var(--text-3);', text: 'Analyzing song...' }));
    analysisCard.style.display = 'flex';

    try {
      const result = await apiFetch('/api/song-video/analyze', {
        method: 'POST',
        body: JSON.stringify({ audio_path: path, clip_duration: _clipDur }),
      });
      if (seq !== _analyzeSeq) return;   // superseded by clear or a newer upload
      _audioAnalysis = result;
      _audioDuration = result.duration || _audioDuration;
      _renderAnalysis(result);
    } catch (e) {
      if (seq !== _analyzeSeq) return;
      analysisCard.innerHTML = '';
      analysisCard.appendChild(el('div', { style: 'font-size:.8rem; color:var(--text-3); font-style:italic;', text: `Analysis unavailable: ${e.message}` }));
      _refreshClipCount();
    }
  }

  function _applyAudio(path, url, duration) {
    _audioPath     = path;
    _audioDuration = duration || 0;
    _audioAnalysis = null;
    audioPreview.src = url;
    audioPreview.style.display = '';
    audioHint.style.display = 'none';
    audioClearBtn.style.display = '';
    audioDropZone.classList.add('drop-zone-loaded');
    _refreshClipCount();
    _analyzeAudio(path);
  }

  audioClearBtn.addEventListener('click', e => {
    e.stopPropagation();
    _analyzeSeq++;   // invalidate any in-flight analysis
    _audioPath = null; _audioDuration = 0; _audioAnalysis = null;
    audioPreview.src = ''; audioPreview.style.display = 'none';
    audioHint.style.display = '';
    audioClearBtn.style.display = 'none';
    audioDropZone.classList.remove('drop-zone-loaded', 'drag-over');
    analysisCard.style.display = 'none';
    _refreshClipCount();
  });

  audioDropZone.addEventListener('click', e => {
    // Don't open file picker when clicking the audio player controls
    if (audioPreview.contains(e.target) || e.target === audioPreview) return;
    audioInput.click();
  });
  audioDropZone.addEventListener('dragover', e => { e.preventDefault(); audioDropZone.classList.add('drag-over'); });
  audioDropZone.addEventListener('dragleave', () => audioDropZone.classList.remove('drag-over'));
  audioDropZone.addEventListener('drop', async e => {
    e.preventDefault();
    audioDropZone.classList.remove('drag-over');
    const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('audio/') || /\.(mp3|wav|flac|ogg|m4a|aac|opus|mpeg|mpg)$/i.test(f.name));
    if (!files.length) return;
    try {
      const data = await apiUpload('/api/song-video/upload-audio', files);
      const f = data.files?.[0];
      if (f) _applyAudio(f.path, f.url, f.duration);
    } catch (err) { toast(err.message, 'error'); }
  });
  audioInput.addEventListener('change', async () => {
    if (!audioInput.files?.length) return;
    try {
      const data = await apiUpload('/api/song-video/upload-audio', Array.from(audioInput.files));
      const f = data.files?.[0];
      if (f) _applyAudio(f.path, f.url, f.duration);
    } catch (err) { toast(err.message, 'error'); }
    audioInput.value = '';
  });
  root.appendChild(audioInput);

  // -- Anchor image (optional) -----------------------------------------------
  const imgInput    = el('input', { type: 'file', accept: 'image/*', style: 'display:none' });
  const imgPreview  = el('img', { style: 'display:none; max-height:140px; object-fit:contain; border-radius:6px;' });
  const imgHint     = el('div', { style: 'color:var(--text-3); font-size:.82rem; text-align:center;', text: 'Drop anchor image (optional -- sets the visual style for all clips)' });
  const imgClearBtn = el('button', {
    style: 'display:none; position:absolute; top:4px; right:4px; width:22px; height:22px; border-radius:50%; border:none; background:rgba(0,0,0,.65); color:#fff; font-size:13px; cursor:pointer; z-index:2; padding:0; line-height:1;',
    text: 'x',
  });
  const imgDropZone = el('div', { class: 'drop-zone', style: 'position:relative; min-height:70px; display:flex; align-items:center; justify-content:center;' },
    [imgPreview, imgHint, imgClearBtn]);
  root.appendChild(imgDropZone);
  root.appendChild(imgInput);

  function _resetStoryIdea() {
    // Story idea was written for the previous image -- next generation should
    // re-read the new anchor instead of recycling the old prompt. Lyrics stay
    // because they are tied to the audio, not the image.
    if (typeof ideaInput !== 'undefined' && ideaInput) ideaInput.value = '';
  }

  function _applyImage(path, url) {
    if (path !== _imagePath) _resetStoryIdea();
    _imagePath = path;
    imgPreview.src = url; imgPreview.style.display = '';
    imgHint.style.display = 'none';
    imgClearBtn.style.display = '';
    imgDropZone.classList.add('drop-zone-loaded');
  }
  imgClearBtn.addEventListener('click', e => {
    e.stopPropagation();
    _imagePath = null;
    imgPreview.src = ''; imgPreview.style.display = 'none';
    imgHint.style.display = '';
    imgClearBtn.style.display = 'none';
    imgDropZone.classList.remove('drop-zone-loaded');
    _resetStoryIdea();
  });
  imgDropZone.addEventListener('click', e => {
    if (imgPreview.contains(e.target) || e.target === imgPreview) return;
    imgInput.click();
  });
  imgDropZone.addEventListener('dragover', e => { e.preventDefault(); imgDropZone.classList.add('drag-over'); });
  imgDropZone.addEventListener('dragleave', () => imgDropZone.classList.remove('drag-over'));
  imgDropZone.addEventListener('drop', async e => {
    e.preventDefault(); imgDropZone.classList.remove('drag-over');
    const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('image/'));
    if (!files.length) return;
    try {
      const data = await apiUpload('/api/song-video/upload-image', files);
      const f = data.files?.[0];
      if (f) _applyImage(f.path, f.url);
    } catch (err) { toast(err.message, 'error'); }
  });
  imgInput.addEventListener('change', async () => {
    if (!imgInput.files?.length) return;
    try {
      const data = await apiUpload('/api/song-video/upload-image', Array.from(imgInput.files));
      const f = data.files?.[0];
      if (f) _applyImage(f.path, f.url);
    } catch (err) { toast(err.message, 'error'); }
    imgInput.value = '';
  });

  // -- Story idea ------------------------------------------------------------
  const ideaInput = el('textarea', {
    rows: '3',
    style: 'width:100%; resize:vertical; font-size:.9rem;',
    placeholder: 'Describe the vibe, story, or visual style -- or leave blank and AI will follow the song\'s energy.',
  });
  root.appendChild(el('div', { class: 'card', style: 'padding:12px 14px;' }, [
    el('div', { style: 'font-size:.75rem; color:var(--text-3); margin-bottom:6px; text-transform:uppercase; letter-spacing:.05em;', text: 'Story idea (optional)' }),
    ideaInput,
  ]));

  // -- Settings --------------------------------------------------------------
  const CHIP_BASE = 'border:1px solid var(--border-2); border-radius:6px; padding:4px 10px; font-size:.78rem; cursor:pointer; background:transparent; color:var(--text-2); transition:background .15s,color .15s;';
  const CHIP_ON   = 'background:var(--accent); border-color:var(--accent); color:#000; font-weight:600;';

  // Clip length
  const clipSlider = el('input', { type: 'range', min: '8', max: '19', value: '8', step: '1', style: 'flex:1; accent-color:var(--accent);' });
  const clipLabel  = el('span',  { style: 'font-size:.82rem; color:var(--accent); font-weight:600; min-width:28px; text-align:right;', text: '8s' });

  // Quality chips
  const qualChips = {};
  const qualRow = el('div', { style: 'display:flex; gap:6px; flex-wrap:wrap;' });
  for (const q of QUALITIES) {
    const btn = el('button', { style: CHIP_BASE + (q.px === _qualityPx ? CHIP_ON : ''), text: q.label });
    btn.addEventListener('click', () => {
      _qualityPx = q.px;
      [_outW, _outH] = _computeDims(_qualityPx);
      dimsLabel.textContent = `${_outW} x ${_outH}`;
      // Enforce per-quality clip duration ceiling
      clipSlider.max = String(q.maxSec);
      if (_clipDur > q.maxSec) {
        _clipDur = q.maxSec;
        clipSlider.value = String(_clipDur);
        clipLabel.textContent = `${_clipDur}s`;
        _refreshClipCount();
      }
      Object.entries(qualChips).forEach(([px, b]) =>
        b.setAttribute('style', CHIP_BASE + (Number(px) === _qualityPx ? CHIP_ON : '')));
    });
    qualChips[q.px] = btn;
    qualRow.appendChild(btn);
  }

  // Steps + Guidance
  const stepsSlider = el('input', { type: 'range', min: '4', max: '50', value: '4', step: '1', style: 'flex:1; accent-color:var(--accent);' });
  const stepsLabel  = el('span', { style: 'font-size:.82rem; color:var(--accent); font-weight:600; min-width:28px; text-align:right;', text: '4' });
  stepsSlider.addEventListener('input', () => { _steps = parseInt(stepsSlider.value); stepsLabel.textContent = String(_steps); });

  const guidSlider = el('input', { type: 'range', min: '1', max: '20', value: '7.5', step: '0.5', style: 'flex:1; accent-color:var(--accent);' });
  const guidLabel  = el('span', { style: 'font-size:.82rem; color:var(--accent); font-weight:600; min-width:28px; text-align:right;', text: '7.5' });
  guidSlider.addEventListener('input', () => { _guidance = parseFloat(guidSlider.value); guidLabel.textContent = String(_guidance); });

  const dimsLabel = el('span', { style: 'font-size:.82rem; color:var(--accent); font-weight:600;', text: `${_outW} x ${_outH}` });

  // Clip count summary + time estimate
  const clipSummary = el('div', { style: 'font-size:.8rem; color:var(--text-3);', text: 'Drop a song -> number of clips is calculated automatically from song length ÷ per-clip length.' });
  const timeWarn    = el('div', {
    style: 'display:none; font-size:.75rem; color:var(--accent-warm, #e8a000); background:rgba(232,160,0,.08); border:1px solid rgba(232,160,0,.25); border-radius:6px; padding:8px 12px; line-height:1.5;',
  });

  const MAX_CLIPS = 50;  // matches backend cap -- 50 x 8s = 6.7 min of unique content at minimum clip length

  function _refreshClipCount() {
    if (!_audioDuration) {
      _numClips = 0;
      clipSummary.textContent = 'Drop a song to get started.';
      timeWarn.style.display = 'none';
      return;
    }
    // Generate exactly enough clips to cover the full song -- no looping.
    // Use suggested_num_clips from analysis when available (BPM-aligned),
    // otherwise compute from raw duration ÷ clip length.
    const rawNeeded = Math.ceil(_audioDuration / _clipDur);
    const suggested = _audioAnalysis?.suggested_num_clips;
    _numClips = Math.min(MAX_CLIPS, Math.max(1, suggested || rawNeeded));

    const songMins = Math.floor(_audioDuration / 60), songSecs = Math.round(_audioDuration % 60);
    const songDisplay = `${songMins}:${String(songSecs).padStart(2, '0')}`;
    // Rough per-clip generation time: ~30s at Draft 360P, ~50s at higher quality
    const secsPerClip = _qualityPx <= 360 ? 30 : (_qualityPx <= 480 ? 40 : 55);
    const estMin = Math.round(_numClips * secsPerClip / 60);
    const loops = rawNeeded > _numClips ? ` -- song fills ${(rawNeeded / _numClips).toFixed(1)}x loop` : ' -- no looping';
    clipSummary.textContent = `${_numClips} clips cover ${songDisplay}${loops} -- est. ~${estMin} min`;

    timeWarn.style.display = 'none';
  }

  clipSlider.addEventListener('input', () => {
    _clipDur = parseInt(clipSlider.value);
    clipLabel.textContent = `${_clipDur}s`;
    _refreshClipCount();
    // Re-analyze with new clip duration if we have a file
    if (_audioPath && _audioAnalysis) {
      // Recalculate clip count from existing analysis using new duration
      if (_audioAnalysis.duration) {
        _numClips = Math.min(MAX_CLIPS, Math.max(1, Math.ceil(_audioAnalysis.duration / _clipDur)));
      }
      // Update energy labels if analysis has profile
      if (_audioAnalysis.energy_profile?.length) {
        const newProfile = _resampleProfile(_audioAnalysis.energy_profile, Math.max(1, Math.ceil((_audioAnalysis.duration || 0) / _clipDur)));
        _audioAnalysis = { ..._audioAnalysis, energy_profile: newProfile, suggested_clip_dur: _clipDur, suggested_num_clips: _numClips };
      }
      _refreshClipCount();
    }
  });

  function _resampleProfile(profile, newLen) {
    if (!profile.length) return [];
    const result = [];
    for (let i = 0; i < newLen; i++) {
      const srcIdx = Math.min(profile.length - 1, Math.round(i * profile.length / newLen));
      result.push(profile[srcIdx]);
    }
    return result;
  }

  // Advanced settings -- collapsed by default so the clean path is just drop + generate
  const _advBody = el('div', { style: 'display:none; flex-direction:column; gap:10px; margin-top:4px;' }, [
    el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Per-clip length' }),
      clipSlider, clipLabel,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:10px; flex-wrap:wrap;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Quality' }),
      qualRow,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Steps' }),
      stepsSlider, stepsLabel,
      el('span', { style: 'font-size:.7rem; color:var(--text-3); font-style:italic;', text: '(ignored by LTX-2 Distilled -- model uses a fixed 8+3 schedule)' }),
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
      el('div', { style: 'font-size:.78rem; color:var(--text-3); width:82px; flex-shrink:0;', text: 'Guidance' }),
      guidSlider, guidLabel,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:6px;' }, [
      el('span', { style: 'font-size:.75rem; color:var(--text-3);', text: 'Output:' }),
      dimsLabel,
    ]),
  ]);
  const _advToggle = el('button', {
    style: 'background:none; border:none; cursor:pointer; font-size:.75rem; color:var(--text-3); padding:0; align-self:flex-start; opacity:.7;',
    text: '⚙ Advanced settings ▼',
  });
  _advToggle.addEventListener('click', () => {
    const open = _advBody.style.display !== 'none';
    _advBody.style.display = open ? 'none' : 'flex';
    _advToggle.textContent = open ? '⚙ Advanced settings ▼' : '⚙ Advanced settings ▲';
  });

  root.appendChild(el('div', { class: 'card', style: 'padding:12px 14px; display:flex; flex-direction:column; gap:6px;' }, [
    clipSummary, timeWarn, _advToggle, _advBody,
  ]));

  // -- Create / Queue buttons ------------------------------------------------
  const createBtn = el('button', {
    class: 'btn btn-primary btn-generate',
    text: 'Generate Music Video',
    style: 'font-size:1.1rem; padding:16px; font-weight:700; letter-spacing:.04em; width:100%;',
  });
  const queueBtn = el('button', {
    class: 'btn',
    text: '＋ Add to Queue',
    style: 'display:none; width:100%; margin-top:6px; font-size:.9rem;',
    title: 'Queue another generation with current settings -- it will start after the active job finishes',
  });
  root.appendChild(createBtn);
  root.appendChild(queueBtn);

  // -- Loop + variety toggles -------------------------------------------------
  const _CHIP = 'border:1px solid var(--border-2); border-radius:6px; padding:5px 12px; font-size:.8rem; cursor:pointer; background:transparent; color:var(--text-2); transition:background .15s,color .15s,border-color .15s;';
  const _CHIP_ON = 'background:var(--accent); border-color:var(--accent); color:#000; font-weight:600;';

  const loopBtn    = el('button', { style: _CHIP, title: 'Keep generating new music videos automatically -- all saved to gallery', text: '∞  Loop' });
  const varietyBtn = el('button', { style: _CHIP, title: 'AI picks a different visual theme each loop so every video looks unique', text: '*  AI variety' });

  const stopAfterBtn  = el('button', { class: 'btn btn-sm', style: 'display:none;', text: 'Stop after this' });
  const loopCountEl   = el('span',   { style: 'font-size:.78rem; color:var(--text-3);', text: '' });
  const loopStatusRow = el('div', {
    style: 'display:none; align-items:center; gap:10px; padding:4px 2px;',
  }, [loopCountEl, stopAfterBtn]);

  root.appendChild(el('div', { style: 'display:flex; gap:8px; flex-wrap:wrap; align-items:center;' }, [loopBtn, varietyBtn]));
  root.appendChild(loopStatusRow);

  function _updateLoopUI() {
    loopBtn.setAttribute('style',    _CHIP + (_loopMode  ? _CHIP_ON : ''));
    varietyBtn.setAttribute('style', _CHIP + (_aiVariety ? _CHIP_ON : ''));
  }
  loopBtn.addEventListener('click', () => { _loopMode  = !_loopMode;  _updateLoopUI(); });
  varietyBtn.addEventListener('click', () => { _aiVariety = !_aiVariety; _updateLoopUI(); });
  _updateLoopUI(); // render initial state (variety on by default)
  stopAfterBtn.addEventListener('click', () => {
    _stopAfter = true;
    stopAfterBtn.style.display = 'none';
    loopCountEl.textContent += ' -- stopping after this one...';
  });

  // -- Progress + result -----------------------------------------------------
  const progressWrap = el('div', { style: 'display:none; flex-direction:column; gap:8px;' });
  const progressBar  = el('div', { style: 'height:4px; background:var(--border-2); border-radius:2px; overflow:hidden;' });
  const progressFill = el('div', { style: 'height:100%; width:0%; background:var(--accent); border-radius:2px; transition:width .4s;' });
  const progressMsg  = el('div', { style: 'font-size:.8rem; color:var(--text-3); text-align:center;' });
  const stopBtn      = el('button', { class: 'btn btn-sm', text: '* Stop', style: 'align-self:center;' });
  progressBar.appendChild(progressFill);
  progressWrap.append(progressBar, progressMsg, stopBtn);
  root.appendChild(progressWrap);

  const resultWrap   = el('div', { style: 'display:none; flex-direction:column; gap:10px;' });
  const resultVideo  = el('video', { controls: '', style: 'width:100%; border-radius:8px; background:#000;' });
  const resultBtns   = el('div', { style: 'display:flex; gap:8px; justify-content:center; flex-wrap:wrap;' });
  const newBtn       = el('button', { class: 'btn btn-sm', text: '+ New video' });
  const muteBtn      = el('button', { class: 'btn btn-sm', text: '🔊 Mute' });
  muteBtn.addEventListener('click', () => {
    resultVideo.muted = !resultVideo.muted;
    muteBtn.textContent = resultVideo.muted ? '🔇 Unmute' : '🔊 Mute';
  });
  resultBtns.append(newBtn, muteBtn);
  resultWrap.append(resultVideo, resultBtns);
  root.appendChild(resultWrap);

  function _showProgress(pct, msg) {
    progressWrap.style.display    = 'flex';
    resultWrap.style.display      = 'none';
    progressFill.style.width      = `${pct}%`;
    progressFill.style.background = 'var(--accent)';
    progressMsg.style.color       = 'var(--text-3)';
    progressMsg.textContent       = msg || '';
  }
  function _showResult(videoPath) {
    progressWrap.style.display = 'none';
    resultWrap.style.display   = 'flex';
    resultVideo.src = pathToUrl(videoPath);
    resultVideo.load();
  }
  function _showError(msg) {
    progressFill.style.width      = '100%';
    progressFill.style.background = 'var(--red, #c41e3a)';
    progressMsg.style.color       = 'var(--red, #c41e3a)';
    progressMsg.textContent       = `Failed: ${msg}`;
    stopBtn.style.display         = 'none';
  }
  function _hideProgress() {
    progressWrap.style.display = 'none';
    progressFill.style.background = 'var(--accent)';
    progressMsg.style.color       = 'var(--text-3)';
  }

  newBtn.addEventListener('click', () => {
    resultWrap.style.display = 'none';
    resultVideo.src = '';
    if (!_loopMode) createBtn.disabled = false;
  });

  stopBtn.addEventListener('click', () => {
    _stopAfter = true;
    _loopMode  = false;
    _updateLoopUI();
    loopStatusRow.style.display = 'none';
    _loopCount = 0;
    if (_jobId) {
      stopJob(_jobId).catch(() => {});
      toast('Stopping -- current clip will finish then generation halts', 'info');
    }
  });

  // -- Watcher ---------------------------------------------------------------
  let _activePoller = null;

  function _watchJob(job_id, timeoutSec = 0) {
    if (_activePoller) { _activePoller.stop(); _activePoller = null; }
    _showProgress(2, 'Planning story arc...');
    stopBtn.style.display = '';

    // Derive maxPolls from server-reported timeout so long multi-clip jobs don't
    // time out client-side before the server finishes (default 10 min = 400 polls).
    const maxPolls = timeoutSec > 0
      ? Math.max(400, Math.ceil((timeoutSec + 300) * 1000 / 1500))
      : 400;

    return new Promise(resolve => {
      const poller = pollJob(job_id,
        j => {
          if (j.status === 'queued') {
            const pos   = j.queue_position;
            const label = pos === 0 ? 'up next' : `position ${pos + 1}`;
            _showProgress(2, `In queue -- ${label}...`);
          } else {
            _showProgress(j.progress || 5, j.message || `${j.progress || 5}%`);
          }
        },
        j => {
          _activePoller = null;
          stopBtn.style.display = 'none';
          const out = Array.isArray(j.output) ? j.output[0] : j.output;
          if (out) {
            _showResult(out);
          } else {
            _hideProgress();
            toast('Job finished but produced no output -- check the Queue tab for details', 'error');
          }
          if (_loopMode && !_stopAfter) {
            // Kick off the next generation automatically. _submitJob is async so
            // we wrap it to catch any rejection -- without the wrapper, an API
            // error (e.g. 429 queue-full) is an unhandled rejection that kills
            // the loop silently and leaves createBtn.disabled permanently true.
            setTimeout(() => _submitJob().catch(e => {
              createBtn.disabled = false;
              toast(`Loop stopped: ${e.message || e}`, 'error');
            }), 1500);
          } else {
            createBtn.disabled = false;
            queueBtn.style.display = 'none';
            loopStatusRow.style.display = 'none';
            _loopCount = 0;
          }
          resolve(true);
        },
        err => {
          _activePoller = null;
          stopBtn.style.display = 'none';
          createBtn.disabled = false;
          queueBtn.style.display = 'none';
          loopStatusRow.style.display = 'none';
          _loopCount = 0;
          _showError(err);
          resolve(false);
        },
        1500, maxPolls,
      );
      _activePoller = poller;
    });
  }

  // -- Submit ----------------------------------------------------------------
  function _buildPayload() {
    const varietyTheme = _aiVariety
      ? _VARIETY_THEMES[(_varietyIdx++) % _VARIETY_THEMES.length]
      : '';
    return {
      audio_path:     _audioPath,
      photo_path:     _imagePath || '',
      video_prompt:   ideaInput.value.trim(),
      variety_theme:  varietyTheme,
      lyrics_text:    _lyricsTextarea ? _lyricsTextarea.value.trim() : '',
      user_direction: 'music video where visual energy matches song dynamics',
      audio_analysis: _audioAnalysis || undefined,
      model:          _model,
      clip_duration:  _clipDur,
      num_clips:      _numClips,
      steps:          _steps,
      guidance:       _guidance,
      output_width:   _outW,
      output_height:  _outH,
    };
  }

  async function _submitJob() {
    if (!_audioPath) { toast('Drop a song file first', 'error'); return; }
    if (_numClips <= 0) { toast('Song duration could not be determined -- try re-uploading the file', 'error'); return; }

    _loopCount++;
    _stopAfter = false;
    createBtn.disabled = true;
    queueBtn.style.display = '';

    if (_loopMode) {
      loopStatusRow.style.display = 'flex';
      stopAfterBtn.style.display  = '';
      loopCountEl.textContent     = `Video ${_loopCount} generating...`;
    }

    try {
      const resp = await api('/api/song-video/generate', {
        method: 'POST',
        body: JSON.stringify(_buildPayload()),
      });
      _jobId = resp.job_id;
      document.dispatchEvent(new CustomEvent('job-queued', { detail: { job_id: _jobId } }));
      _watchJob(_jobId, resp.timeout_sec || 0);
    } catch (e) {
      createBtn.disabled = false;
      queueBtn.style.display = 'none';
      loopStatusRow.style.display = 'none';
      _loopCount = 0;
      if (e.status === 429 || /queue.*full|full.*queue/i.test(e.message)) {
        toast('Queue is full -- wait for the current job to finish', 'error');
      } else {
        toast(e.message || 'Submission failed', 'error');
      }
    }
  }

  queueBtn.addEventListener('click', async () => {
    if (!_audioPath) { toast('Drop a song file first', 'error'); return; }
    try {
      const resp = await api('/api/song-video/generate', {
        method: 'POST',
        body: JSON.stringify(_buildPayload()),
      });
      document.dispatchEvent(new CustomEvent('job-queued', { detail: { job_id: resp.job_id } }));
      toast('Added to queue!', 'success');
    } catch (e) {
      toast(e.message || 'Failed to queue job', 'error');
    }
  });

  createBtn.addEventListener('click', _submitJob);

  // -- Palette AI intent + queue-modal branching -----------------------------
  // Keep applier minimal: the high-value fields are the story idea (text
  // prompt) and the slider controls. Audio/image stay as whatever the user
  // currently has loaded -- branching is meant for "redo with a tweak".
  import('./shell/ai-intent.js?v=20260503h').then(({ registerTabAI }) => {
    registerTabAI('song-video', {
      getContext: () => ({
        prompt:        ideaInput.value,
        video_steps:   Number(stepsSlider.value) || _steps,
        video_guidance: Number(guidSlider.value) || _guidance,
        clip_duration: Number(clipSlider.value)  || _clipDur,
      }),
      applySettings: (s) => {
        if (typeof s.video_prompt === 'string' && s.video_prompt.trim()) {
          ideaInput.value = s.video_prompt.trim();
        } else if (typeof s.prompt === 'string' && s.prompt.trim()) {
          ideaInput.value = s.prompt.trim();
        }
        if (typeof s.prompt_append === 'string' && s.prompt_append.trim()) {
          const cur = ideaInput.value.trim();
          ideaInput.value = cur ? `${cur}, ${s.prompt_append.trim()}` : s.prompt_append.trim();
        }
        if (typeof s.video_steps === 'number') {
          _steps = Math.max(4, Math.min(50, s.video_steps));
          stepsSlider.value = String(_steps); stepsLabel.textContent = String(_steps);
        }
        if (typeof s.video_guidance === 'number') {
          _guidance = Math.max(1, Math.min(20, s.video_guidance));
          guidSlider.value = String(_guidance); guidLabel.textContent = String(_guidance);
        }
        if (typeof s.clip_duration === 'number') {
          _clipDur = Math.max(8, Math.min(parseInt(clipSlider.max) || 19, s.clip_duration));
          clipSlider.value = String(_clipDur); clipLabel.textContent = `${_clipDur}s`;
        }
        // photo_path is the extracted last frame passed by _doContinuation
        if (typeof s.photo_path === 'string' && s.photo_path.trim()) {
          _applyImage(s.photo_path, pathToUrl(s.photo_path) || s.photo_path);
        }
      },
    });
  }).catch(() => {});
}
