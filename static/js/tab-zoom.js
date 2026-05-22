/**
 * Zoom / Music tab.
 *
 * Three audio modes (explicit toggle):
 *   none -- pure zoom, no audio
 *   ai   -- zoom + AI-generated music (optional: sync clips to beats by generating audio first)
 *   song -- clips beat-matched TO the user's uploaded song
 *
 * Source can be a photo or a video (first/last frame extracted based on direction).
 * Folder batch processing available in none/ai modes.
 */
import { pollJob, stopJob, apiUpload } from './api.js?v=20260505e';
import { el, pathToUrl } from './components.js?v=20260507a';
import { toast, apiFetch } from './shell/toast.js?v=20260518a';
import { handoff } from './handoff.js?v=20260422a';

export function receiveHandoff(data) {
  // no-op
}

export function init(panel) {
  panel.innerHTML = '';
  const root = el('div', {
    style: 'max-width:680px; margin:0 auto; padding:24px 16px; display:flex; flex-direction:column; gap:18px;',
  });
  panel.appendChild(root);

  // -- State -----------------------------------------------------------------
  let _sourcePath   = null;
  let _isVideo      = false;
  let _audioMode    = 'none';   // 'none' | 'ai' | 'song'
  let _songPath     = null;
  let _songDur      = 0;
  let _songAnalysis = null;
  let _lyricsTA     = null;
  let _direction    = 'out';
  let _jobId        = null;
  let _modelData    = {};
  let _gpuVram      = 0;
  let _activeCount  = 0;
  let _clipDur      = 8;
  let _numClips     = 0;
  let _coverage     = 1.0;
  let _qualityPx    = 360;
  let _outW         = 640;
  let _outH         = 352;
  let _steps        = 4;
  let _guidance     = 7.5;
  let _loopMode     = false;
  let _aiVariety    = true;
  let _loopCount    = 0;
  let _stopAfter    = false;
  let _varietyIdx   = 0;
  let _analyzeSeq   = 0;
  let _activePoller = null;
  let _extendPanel  = null;

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
    { label: 'Draft 360P', px: 360, maxSec: 10 },
    { label: '480P',       px: 480, maxSec: 10 },
    { label: '580P',       px: 580, maxSec: 10 },
    { label: '720P',       px: 720, maxSec: 10 },
  ];

  function _computeDims(px) {
    const h = px, w = Math.round(px * 16 / 9);
    const r32 = n => Math.max(32, Math.round(n / 32) * 32);
    return [r32(w), r32(h)];
  }
  [_outW, _outH] = _computeDims(_qualityPx);

  // -- Helpers ---------------------------------------------------------------
  const LABEL = s => el('div', {
    style: 'font-size:11px; font-weight:600; letter-spacing:.08em; text-transform:uppercase; color:var(--text-3);',
    text: s,
  });

  function _chip(label, value, row, onSelect) {
    const b = el('button', {
      style: [
        'flex:1; padding:7px 4px; border-radius:6px; border:1px solid var(--border-2);',
        'background:var(--surface); cursor:pointer; font-size:13px; font-weight:600;',
        'color:var(--text-2); transition:all .12s; white-space:nowrap;',
      ].join(''),
    });
    b.textContent = label;
    b.dataset.value = value;
    b.onclick = () => {
      row.querySelectorAll('button').forEach(x => {
        x.style.borderColor = 'var(--border-2)';
        x.style.background  = 'var(--surface)';
        x.style.color       = 'var(--text-2)';
        delete x.dataset.active;
      });
      b.style.borderColor = 'var(--accent-border)';
      b.style.background  = 'var(--accent-bg)';
      b.style.color       = 'var(--accent)';
      b.dataset.active    = '1';
      onSelect(value);
    };
    return b;
  }

  function _activeValue(row) {
    return row.querySelector('button[data-active]')?.dataset.value;
  }

  function _card(content) {
    const c = el('div', {
      style: 'background:var(--surface); border:1px solid var(--border-2); border-radius:var(--r-lg); padding:16px;',
    });
    c.append(...[content].flat());
    return c;
  }

  // -- Source drop zone ------------------------------------------------------
  // Accepts photos or videos. For zoom, the appropriate frame is extracted:
  //   Zoom Out -> last frame of video  (starts from where video ended)
  //   Zoom In  -> first frame of video (zooms into the opening shot)
  // In 'song' mode, source is optional anchor image (videos rejected).
  const sourceLabelEl = el('div', {
    style: 'font-size:11px; font-weight:600; letter-spacing:.08em; text-transform:uppercase; color:var(--text-3); margin-bottom:6px;',
    text: 'Source',
  });
  const fileInput = el('input', { type: 'file', accept: 'image/*,video/*', style: 'display:none' });
  panel.appendChild(fileInput);

  const previewImg = el('img', {
    style: 'max-height:180px; max-width:100%; border-radius:6px; display:none; object-fit:contain; margin:0 auto;',
  });
  const dropHint = el('div', {
    style: 'display:flex; flex-direction:column; align-items:center; gap:6px; padding:20px 0;',
  });
  const dropIcon   = el('div', { style: 'font-size:32px; color:var(--text-3);', text: '+' });
  const dropTextEl = el('div', { style: 'font-size:13px; color:var(--text-2);', text: 'Drop a photo or video, or click to browse' });
  const dropSubEl  = el('div', { style: 'font-size:11px; color:var(--text-3); display:none;' });
  dropHint.append(dropIcon, dropTextEl, dropSubEl);

  const sourceInfo     = el('div', { style: 'display:none; align-items:center; justify-content:space-between; font-size:12px; color:var(--text-2); padding-top:8px;' });
  const sourceNameEl   = el('span');
  const clearSourceBtn = el('button', { style: 'background:none; border:none; color:var(--red); cursor:pointer; font-size:11px; padding:0;', text: 'clear' });
  clearSourceBtn.onclick = e => { e.stopPropagation(); _clearSource(); };
  sourceInfo.append(sourceNameEl, clearSourceBtn);

  const dropArea = el('div', {
    style: 'border:2px dashed var(--border-2); border-radius:var(--r-lg); cursor:pointer; transition:border-color .15s, background .15s; background:var(--surface);',
  });
  dropArea.append(dropHint, previewImg, sourceInfo);

  dropArea.addEventListener('dragover', e => { e.preventDefault(); dropArea.style.borderColor = 'var(--accent)'; dropArea.style.background = 'var(--accent-bg)'; });
  dropArea.addEventListener('dragleave', () => { dropArea.style.borderColor = 'var(--border-2)'; dropArea.style.background = 'var(--surface)'; });
  dropArea.addEventListener('drop', e => {
    e.preventDefault(); dropArea.style.borderColor = 'var(--border-2)'; dropArea.style.background = 'var(--surface)';
    const f = e.dataTransfer.files[0];
    if (!f) return;
    // Auto-detect song drop on source area
    if (f.type.startsWith('audio/') || /\.(mp3|wav|flac|ogg|m4a|aac|opus|mpeg|mpg)$/i.test(f.name)) {
      _setAudioMode('song');
      _uploadSong(f);
    } else {
      _uploadSource(f);
    }
  });
  dropArea.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => { if (fileInput.files[0]) _uploadSource(fileInput.files[0]); fileInput.value = ''; });

  function _clearSource() {
    _sourcePath = null; _isVideo = false;
    previewImg.style.display = 'none'; previewImg.src = '';
    dropHint.style.display = 'flex'; dropSubEl.style.display = 'none';
    sourceInfo.style.display = 'none';
    _updateBtn();
  }

  async function _uploadSource(file) {
    if (_audioMode === 'song' && file.type.startsWith('video/')) {
      toast('In song mode the source is a visual anchor -- drop an image, not a video', 'error');
      return;
    }
    const isVid = file.type.startsWith('video/');
    dropTextEl.textContent = 'Uploading...';
    dropSubEl.style.display = 'none';
    try {
      const resp = await apiUpload(isVid ? '/api/fun/upload-video' : '/api/fun/upload', [file]);
      const data = resp?.files?.[0];
      if (!data?.path) throw new Error('No path returned');
      _sourcePath = data.path; _isVideo = isVid;

      if (isVid) {
        const timeCode = _direction === 'out' ? -1 : 0.1;
        const fr = await apiFetch('/api/zoom/extract-frame', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ video_path: _sourcePath, time_sec: timeCode }),
        }).catch(() => null);
        if (fr?.frame_url) { previewImg.src = fr.frame_url; previewImg.style.display = 'block'; }
        // Tell the user which frame will be used
        dropSubEl.textContent = _direction === 'out'
          ? 'Using last frame of video -- zoom continues outward from there'
          : 'Using first frame of video -- zoom drives inward from the opening shot';
        dropSubEl.style.display = 'block';
      } else {
        previewImg.src = pathToUrl(data.path);
        previewImg.style.display = 'block';
        dropSubEl.style.display = 'none';
      }

      dropHint.style.display = 'none';
      sourceNameEl.textContent = isVid ? `${file.name} (video)` : file.name;
      sourceInfo.style.display = 'flex';
      _updateBtn();
    } catch (err) {
      toast('Upload failed: ' + err.message, 'error');
      dropTextEl.textContent = _audioMode === 'song' ? 'Drop anchor image (optional)' : 'Drop a photo or video, or click to browse';
    }
  }

  // -- Audio section ---------------------------------------------------------
  // Three-way toggle. Each mode shows its own sub-section.

  const ABTN = 'flex:1; padding:10px 4px; border-radius:6px; border:2px solid var(--border-2); background:var(--surface); cursor:pointer; font-size:13px; font-weight:600; color:var(--text-2); transition:all .15s; text-align:center;';
  const ABTN_ON = 'flex:1; padding:10px 4px; border-radius:6px; border:2px solid var(--accent-border); background:var(--accent-bg); cursor:pointer; font-size:13px; font-weight:600; color:var(--accent); transition:all .15s; text-align:center;';

  const audioNoneBtn = el('button', { style: ABTN_ON, text: 'Zoom Only' });
  const audioAiBtn   = el('button', { style: ABTN, text: 'Zoom + Music' });
  const audioSongBtn = el('button', { style: ABTN, text: 'Music Video' });
  const audioModeRow = el('div', { style: 'display:flex; gap:8px;' }, [audioNoneBtn, audioAiBtn, audioSongBtn]);
  const modeDescEl   = el('div', {
    style: 'font-size:12px; color:var(--text-3); text-align:center; padding-top:4px; min-height:16px;',
    text: 'Spatial zoom video from a photo or video',
  });

  // Sub-section: AI music
  const audioFirstCheck = el('input', { type: 'checkbox' });
  const aiSubSection = el('div', {
    style: 'display:none; flex-direction:column; gap:6px; padding-top:8px; border-top:1px solid var(--border-2); margin-top:8px;',
  });
  aiSubSection.append(
    el('label', {
      style: 'display:flex; align-items:flex-start; gap:8px; cursor:pointer; font-size:13px; color:var(--text-2); user-select:none;',
    }, [
      audioFirstCheck,
      el('span', { text: 'Generate music BEFORE clips and beat-align clips to it -- slower but tighter sync' }),
    ]),
  );

  // Sub-section: My song
  const audioInput = el('input', { type: 'file', accept: 'audio/*,.mp3,.wav,.flac,.ogg,.m4a,.aac,.mpeg,.mpg', style: 'display:none' });
  panel.appendChild(audioInput);

  const songHintText = el('div', { style: 'font-size:12px; color:var(--text-3);', text: 'Drop your song here or click to browse' });
  const songHintArea = el('div', { style: 'display:flex; flex-direction:column; align-items:center; gap:6px; padding:12px 0;' });
  songHintArea.append(el('div', { style: 'font-size:24px; color:var(--text-3);', text: '♪' }), songHintText);

  const songPreview  = el('audio', { controls: true, style: 'display:none; width:100%; margin-top:6px;' });
  const songClearBtn = el('button', {
    style: 'display:none; align-self:flex-end; background:none; border:none; color:var(--red); cursor:pointer; font-size:11px; padding:0; margin-top:4px;',
    text: 'remove song',
  });

  const songDropInner = el('div', {
    style: 'border:1px dashed var(--border-2); border-radius:var(--r-md); cursor:pointer; display:flex; flex-direction:column; align-items:center; transition:border-color .12s, background .12s; background:var(--surface-2);',
  });
  songDropInner.append(songHintArea, songPreview, songClearBtn);

  const songSubSection = el('div', {
    style: 'display:none; flex-direction:column; gap:6px; padding-top:8px; border-top:1px solid var(--border-2); margin-top:8px;',
  });
  songSubSection.appendChild(songDropInner);

  songDropInner.addEventListener('dragover', e => { e.preventDefault(); songDropInner.style.borderColor = 'var(--accent)'; songDropInner.style.background = 'var(--accent-bg)'; });
  songDropInner.addEventListener('dragleave', () => { songDropInner.style.borderColor = 'var(--border-2)'; songDropInner.style.background = 'var(--surface-2)'; });
  songDropInner.addEventListener('drop', e => {
    e.preventDefault(); songDropInner.style.borderColor = 'var(--border-2)'; songDropInner.style.background = 'var(--surface-2)';
    const f = Array.from(e.dataTransfer.files).find(f => f.type.startsWith('audio/') || /\.(mp3|wav|flac|ogg|m4a|aac|opus|mpeg|mpg)$/i.test(f.name));
    if (f) _uploadSong(f);
  });
  songDropInner.addEventListener('click', e => {
    if (songPreview.contains(e.target) || e.target === songPreview || e.target === songClearBtn) return;
    audioInput.click();
  });
  audioInput.addEventListener('change', () => { if (audioInput.files[0]) _uploadSong(audioInput.files[0]); audioInput.value = ''; });
  songClearBtn.addEventListener('click', e => { e.stopPropagation(); _clearSong(); });

  const audioSection = _card([LABEL('Audio'), audioModeRow, modeDescEl, aiSubSection, songSubSection]);

  audioNoneBtn.onclick = () => _setAudioMode('none');
  audioAiBtn.onclick   = () => _setAudioMode('ai');
  audioSongBtn.onclick = () => _setAudioMode('song');

  function _setAudioMode(mode) {
    // Clear song if switching away from song mode
    if (mode !== 'song' && _songPath) _clearSong(false);

    _audioMode = mode;

    audioNoneBtn.setAttribute('style', mode === 'none' ? ABTN_ON : ABTN);
    audioAiBtn.setAttribute('style',   mode === 'ai'   ? ABTN_ON : ABTN);
    audioSongBtn.setAttribute('style', mode === 'song' ? ABTN_ON : ABTN);

    aiSubSection.style.display   = mode === 'ai'   ? 'flex' : 'none';
    songSubSection.style.display = mode === 'song' ? 'flex' : 'none';

    // Mode description
    const _MODE_DESCS = {
      none: 'Spatial zoom video from a photo or video',
      ai:   'Zoom video with AI-composed soundtrack',
      song: 'Full music video synced to your song',
    };
    modeDescEl.textContent = _MODE_DESCS[mode] || '';

    // Source drop zone hint adapts
    if (mode === 'song') {
      dropTextEl.textContent = 'Drop anchor image (optional -- locks visual style)';
      fileInput.accept = 'image/*';
      dropSubEl.style.display = 'none';
      sourceLabelEl.innerHTML = 'Anchor Image <span style="font-weight:400; text-transform:none; letter-spacing:0; color:var(--text-3);">(optional)</span>';
    } else {
      dropTextEl.textContent = 'Drop a photo or video, or click to browse';
      fileInput.accept = 'image/*,video/*';
      sourceLabelEl.textContent = 'Source';
    }

    // Direction (Zoom Out/In) is only relevant for zoom modes -- song-video pipeline does not use it
    dirSection.style.display = mode === 'song' ? 'none' : '';

    // Zoom controls (clip chips + folder batch) shown in non-song modes
    controlsGrid.style.display = mode === 'song' ? 'none' : 'grid';
    batchSection.style.display  = mode === 'song' ? 'none' : 'flex';

    // Song-only sections
    clipSummarySection.style.display = mode === 'song' ? 'flex' : 'none';
    loopSection.style.display        = mode === 'song' ? 'flex' : 'none';

    _updateBtn();
  }

  // -- Song state management -------------------------------------------------
  function _clearSong(resetMode = true) {
    _analyzeSeq++;
    _songPath = null; _songDur = 0; _songAnalysis = null; _lyricsTA = null;
    songPreview.src = ''; songPreview.style.display = 'none';
    songHintArea.style.display = 'flex';
    songClearBtn.style.display = 'none';
    songDropInner.style.borderColor = 'var(--border-2)';
    analysisCard.style.display = 'none';
    analysisCard.innerHTML = '';
    if (resetMode) _setAudioMode('none');
    _updateBtn();
  }

  async function _uploadSong(file) {
    songHintText.textContent = 'Uploading...';
    try {
      const resp = await apiUpload('/api/song-video/upload-audio', [file]);
      const f = resp?.files?.[0];
      if (!f?.path) throw new Error('No path returned');
      songPreview.src = f.url;
      songPreview.style.display = 'block';
      songHintArea.style.display = 'none';
      songClearBtn.style.display = 'block';
      _songPath = f.path;
      _songDur  = f.duration || 0;
      _setAudioMode('song');
      _updateBtn();
      _analyzeAudio(f.path);
    } catch (err) {
      toast('Song upload failed: ' + err.message, 'error');
      songHintText.textContent = 'Drop your song here or click to browse';
    }
  }

  // -- Analysis card (song mode) -- lives inside songSubSection, below the player
  const analysisCard = el('div', {
    style: 'display:none; flex-direction:column; gap:10px; padding-top:10px; border-top:1px solid var(--border-2); margin-top:4px;',
  });
  songSubSection.appendChild(analysisCard);

  function _renderAnalysis(a) {
    analysisCard.innerHTML = '';
    analysisCard.style.display = 'flex';

    const chips = [
      a.duration_display && { icon: '♪', text: a.duration_display },
      a.bpm              && { icon: '♩', text: `${a.bpm} BPM` },
      a.key              && { icon: '#', text: `${a.key} ${a.mode || ''}`.trim() },
      a.mood             && { icon: '*', text: a.mood.charAt(0).toUpperCase() + a.mood.slice(1) },
      a.energy           && { icon: '~', text: `${a.energy} energy` },
    ].filter(Boolean);

    const chipRow = el('div', { style: 'display:flex; gap:6px; flex-wrap:wrap;' });
    chips.forEach(c => chipRow.appendChild(el('span', {
      style: 'display:inline-flex; align-items:center; gap:4px; padding:3px 8px; border-radius:20px; background:var(--surface-2); border:1px solid var(--border-2); font-size:11px; color:var(--text-2);',
      text: `${c.icon} ${c.text}`,
    })));
    analysisCard.appendChild(chipRow);

    const profile = a.energy_profile || [];
    const labels  = a.clip_energy_labels || [];
    if (profile.length > 0) {
      const LABEL_COLOR = { HIGH: '#e05c5c', MED: '#d4a017', LOW: '#5b9bd4' };
      const strip = el('div', { style: 'display:flex; gap:2px; align-items:flex-end; height:32px; width:100%;' });
      profile.forEach((e, i) => {
        const lbl   = labels[i] || (e > 0.7 ? 'HIGH' : e > 0.35 ? 'MED' : 'LOW');
        const color = LABEL_COLOR[lbl] || 'var(--accent)';
        const pct   = Math.max(20, Math.round(e * 100));
        strip.appendChild(el('div', { title: `Clip ${i + 1}: ${lbl}`, style: `flex:1; min-width:3px; border-radius:2px 2px 0 0; height:${pct}%; background:${color};` }));
      });
      analysisCard.appendChild(strip);
    }

    _lyricsTA = el('textarea', {
      rows: '3',
      style: 'width:100%; box-sizing:border-box; resize:vertical; font-size:12px; color:var(--text-2); font-family:inherit; background:var(--surface-2); border:1px solid var(--border-2); border-radius:var(--r-sm); padding:6px 8px;',
      placeholder: 'Detected lyrics (edit to correct, or leave blank)',
    });
    _lyricsTA.value = (a.lyrics_text || '').trim();
    analysisCard.appendChild(el('div', { style: 'display:flex; flex-direction:column; gap:4px;' }, [
      el('div', { style: 'font-size:10px; text-transform:uppercase; letter-spacing:.06em; color:var(--text-3);', text: 'Detected lyrics' }),
      _lyricsTA,
    ]));

    if (a.suggested_clip_dur) {
      const maxSec = QUALITIES.find(q => q.px === _qualityPx)?.maxSec || 10;
      _clipDur = Math.max(8, Math.min(a.suggested_clip_dur, maxSec));
      clipSlider.value = String(_clipDur);
      clipLabel.textContent = `${_clipDur}s`;
    }
    _refreshClipCount();
  }

  async function _analyzeAudio(path) {
    const seq = ++_analyzeSeq;
    analysisCard.innerHTML = '';
    analysisCard.style.display = 'flex';
    analysisCard.appendChild(el('div', { style: 'font-size:12px; color:var(--text-3); padding:4px;', text: 'Analyzing song...' }));
    try {
      const result = await apiFetch('/api/song-video/analyze', { method: 'POST', body: JSON.stringify({ audio_path: path, clip_duration: _clipDur }) });
      if (seq !== _analyzeSeq) return;
      _songAnalysis = result;
      _songDur = result.duration || _songDur;
      _renderAnalysis(result);
    } catch (e) {
      if (seq !== _analyzeSeq) return;
      analysisCard.innerHTML = '';
      analysisCard.appendChild(el('div', { style: 'font-size:12px; color:var(--text-3); font-style:italic; padding:4px;', text: `Analysis unavailable: ${e.message}` }));
      _refreshClipCount();
    }
  }

  // -- Direction (always visible) --------------------------------------------
  const dirRow = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:10px;' });

  function _dirBtn(label, sub, value) {
    const b = el('button', {
      style: 'display:flex; flex-direction:column; align-items:center; gap:4px; padding:14px; border-radius:var(--r-md); border:2px solid var(--border-2); background:var(--surface); cursor:pointer; transition:all .15s;',
    });
    b.append(
      el('span', { style: 'font-size:15px; font-weight:700; color:var(--text);', text: label }),
      el('span', { style: 'font-size:11px; color:var(--text-3);', text: sub }),
    );
    b.dataset.value = value;
    b.onclick = () => {
      [btnOut, btnIn].forEach(x => { x.style.borderColor = 'var(--border-2)'; x.style.background = 'var(--surface)'; });
      b.style.borderColor = 'var(--accent-border)';
      b.style.background  = 'var(--accent-bg)';
      _direction = value;
      // Update video frame note if a video is loaded
      if (_isVideo && _sourcePath && dropSubEl.style.display !== 'none') {
        dropSubEl.textContent = value === 'out'
          ? 'Using last frame of video -- zoom continues outward from there'
          : 'Using first frame of video -- zoom drives inward from the opening shot';
      }
    };
    return b;
  }

  const btnOut = _dirBtn('Zoom Out', 'Reveals surroundings', 'out');
  const btnIn  = _dirBtn('Zoom In',  'Approaches detail',    'in');
  dirRow.append(btnOut, btnIn);
  btnOut.style.borderColor = 'var(--accent-border)';
  btnOut.style.background  = 'var(--accent-bg)';

  const dirSection = el('div', { style: 'display:flex; flex-direction:column; gap:8px;' }, [LABEL('Direction'), dirRow]);

  // -- Clip chips (non-song modes) -------------------------------------------
  const totalEstEl = el('div', { style: 'font-size:11px; color:var(--text-3); text-align:right; align-self:flex-end; padding-bottom:2px;' });
  const stepsRow   = el('div', { style: 'display:flex; gap:6px; flex-wrap:wrap;' });
  const durRow     = el('div', { style: 'display:flex; gap:6px; flex-wrap:wrap;' });

  function _updateEst() {
    const clips = Number(_activeValue(stepsRow)) || 5;
    const secs  = Number(_activeValue(durRow))   || 4;
    const total = clips * secs;
    const m = Math.floor(total / 60), s = total % 60;
    totalEstEl.textContent = `~${m > 0 ? m + 'm ' : ''}${s > 0 ? s + 's' : ''} video`;
  }

  [3, 4, 5, 6, 8, 10, 12].forEach((n, i) => { const b = _chip(n, n, stepsRow, _updateEst); if (i === 2) b.click(); stepsRow.appendChild(b); });
  [4, 5, 6, 8, 10, 12, 15].forEach((n, i) => { const b = _chip(`${n}s`, n, durRow, _updateEst); if (i === 0) b.click(); durRow.appendChild(b); });
  _updateEst();

  const controlsGrid = el('div', { style: 'display:grid; grid-template-columns:1fr 1fr; gap:16px;' });
  const stepsGroup = el('div', { style: 'display:flex; flex-direction:column; gap:8px;' });
  stepsGroup.append(LABEL('Clips in chain'), stepsRow);
  const durGroup = el('div', { style: 'display:flex; flex-direction:column; gap:8px;' });
  durGroup.append(
    el('div', { style: 'display:flex; justify-content:space-between; align-items:baseline;' }, [LABEL('Seconds per clip'), totalEstEl]),
    durRow,
  );
  controlsGrid.append(stepsGroup, durGroup);

  // -- Clip count summary + advanced settings (song mode) --------------------
  const clipSummaryEl = el('div', { style: 'font-size:12px; color:var(--text-3);', text: 'Drop a song above.' });
  const MAX_CLIPS = 50;

  function _refreshClipCount() {
    if (!_songDur) { clipSummaryEl.textContent = 'Drop a song in the Audio section above.'; _numClips = 0; _updateBtn(); return; }
    const coveredDur = _songDur * _coverage;
    const rawNeeded  = Math.ceil(coveredDur / _clipDur);
    _numClips = Math.min(MAX_CLIPS, Math.max(1, rawNeeded));
    const m = Math.floor(_songDur / 60), s = Math.round(_songDur % 60);
    const secsPerClip = _qualityPx <= 360 ? 30 : (_qualityPx <= 480 ? 40 : 55);
    const estMin = Math.round(_numClips * secsPerClip / 60);
    const loopNote = _coverage < 1.0 ? ` (loops ${(1 / _coverage).toFixed(1)}x)` : '';
    clipSummaryEl.textContent = `${_numClips} clips${loopNote} -- est. ~${estMin} min for ${m}:${String(s).padStart(2, '0')} song`;
    _updateBtn();
  }

  const CHIP_BASE = 'border:1px solid var(--border-2); border-radius:6px; padding:4px 10px; font-size:11px; cursor:pointer; background:transparent; color:var(--text-2); transition:background .15s,color .15s;';
  const CHIP_ON   = 'background:var(--accent); border-color:var(--accent); color:#000; font-weight:600;';

  const qualChips = {};
  const qualRow = el('div', { style: 'display:flex; gap:6px; flex-wrap:wrap;' });
  for (const q of QUALITIES) {
    const btn = el('button', { style: CHIP_BASE + (q.px === _qualityPx ? CHIP_ON : ''), text: q.label });
    btn.addEventListener('click', () => {
      _qualityPx = q.px; [_outW, _outH] = _computeDims(_qualityPx); dimsLabel.textContent = `${_outW} x ${_outH}`;
      clipSlider.max = String(q.maxSec);
      if (_clipDur > q.maxSec) { _clipDur = q.maxSec; clipSlider.value = String(_clipDur); clipLabel.textContent = `${_clipDur}s`; _refreshClipCount(); }
      Object.entries(qualChips).forEach(([px, b]) => b.setAttribute('style', CHIP_BASE + (Number(px) === _qualityPx ? CHIP_ON : '')));
    });
    qualChips[q.px] = btn; qualRow.appendChild(btn);
  }

  const clipSlider = el('input', { type: 'range', min: '8', max: '10', value: '8', step: '1', style: 'flex:1; accent-color:var(--accent);' });
  const clipLabel  = el('span', { style: 'font-size:12px; color:var(--accent); font-weight:600; min-width:26px; text-align:right;', text: '8s' });
  clipSlider.addEventListener('input', () => { _clipDur = parseInt(clipSlider.value); clipLabel.textContent = `${_clipDur}s`; _refreshClipCount(); });

  const stepsSlider = el('input', { type: 'range', min: '4', max: '50', value: '4', step: '1', style: 'flex:1; accent-color:var(--accent);' });
  const stepsLabel  = el('span', { style: 'font-size:12px; color:var(--accent); font-weight:600; min-width:26px; text-align:right;', text: '4' });
  stepsSlider.addEventListener('input', () => { _steps = parseInt(stepsSlider.value); stepsLabel.textContent = String(_steps); });

  const guidSlider = el('input', { type: 'range', min: '1', max: '20', value: '7.5', step: '0.5', style: 'flex:1; accent-color:var(--accent);' });
  const guidLabel  = el('span', { style: 'font-size:12px; color:var(--accent); font-weight:600; min-width:26px; text-align:right;', text: '7.5' });
  guidSlider.addEventListener('input', () => { _guidance = parseFloat(guidSlider.value); guidLabel.textContent = String(_guidance); });

  const dimsLabel = el('span', { style: 'font-size:12px; color:var(--accent); font-weight:600;', text: `${_outW} x ${_outH}` });

  const _COV_OPTIONS = [
    { label: 'All unique',      value: 1.0,  title: 'Unique content all the way through' },
    { label: '75% (1.3x loop)', value: 0.75, title: 'Loops 1.3x -- 25% faster to generate' },
    { label: '50% (2x loop)',   value: 0.5,  title: 'Loops 2x -- 50% faster to generate' },
  ];
  const coverageRow = el('div', { style: 'display:flex; gap:6px; flex-wrap:wrap;' });
  _COV_OPTIONS.forEach((opt, idx) => {
    const btn = el('button', { style: CHIP_BASE + (idx === 0 ? CHIP_ON : ''), text: opt.label, title: opt.title });
    btn.addEventListener('click', () => {
      _coverage = opt.value;
      coverageRow.querySelectorAll('button').forEach((b, i) => b.setAttribute('style', CHIP_BASE + (i === idx ? CHIP_ON : '')));
      _refreshClipCount();
    });
    coverageRow.appendChild(btn);
  });

  const advBody = el('div', { style: 'display:none; flex-direction:column; gap:10px; margin-top:4px;' }, [
    el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
      el('div', { style: 'font-size:11px; color:var(--text-3); width:76px; flex-shrink:0;', text: 'Clip length' }), clipSlider, clipLabel,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:10px; flex-wrap:wrap;' }, [
      el('div', { style: 'font-size:11px; color:var(--text-3); width:76px; flex-shrink:0;', text: 'Quality' }), qualRow,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:10px; flex-wrap:wrap;' }, [
      el('div', { style: 'font-size:11px; color:var(--text-3); width:76px; flex-shrink:0;', text: 'Coverage' }), coverageRow,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
      el('div', { style: 'font-size:11px; color:var(--text-3); width:76px; flex-shrink:0;', text: 'Steps' }), stepsSlider, stepsLabel,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:10px;' }, [
      el('div', { style: 'font-size:11px; color:var(--text-3); width:76px; flex-shrink:0;', text: 'Guidance' }), guidSlider, guidLabel,
    ]),
    el('div', { style: 'display:flex; align-items:center; gap:6px;' }, [
      el('span', { style: 'font-size:11px; color:var(--text-3);', text: 'Output:' }), dimsLabel,
    ]),
  ]);
  const advToggle = el('button', { style: 'background:none; border:none; cursor:pointer; font-size:11px; color:var(--text-3); padding:0; align-self:flex-start; opacity:.7;', text: 'Advanced settings' });
  advToggle.addEventListener('click', () => {
    const open = advBody.style.display !== 'none';
    advBody.style.display = open ? 'none' : 'flex';
    advToggle.textContent = open ? 'Advanced settings' : 'Hide advanced';
  });

  const clipSummarySection = el('div', {
    style: 'display:none; background:var(--surface); border:1px solid var(--border-2); border-radius:var(--r-lg); padding:12px 16px; flex-direction:column; gap:8px;',
  });
  clipSummarySection.append(clipSummaryEl, advToggle, advBody);

  // -- Idea textarea ---------------------------------------------------------
  const ideaInput = el('textarea', {
    placeholder: '"reveal a foggy mountain valley"  or  describe the visual style for your song',
    rows: 2,
    style: 'width:100%; box-sizing:border-box; background:var(--surface-2); border:1px solid var(--border-2); border-radius:var(--r-md); padding:10px 12px; color:var(--text); font-size:13px; resize:none; font-family:inherit; outline:none;',
  });

  // -- Model selection -------------------------------------------------------
  const vramNote = el('span', { style: 'font-size:11px; color:var(--text-3);' });
  const modelLabelRow = el('div', { style: 'display:flex; align-items:center; justify-content:space-between;' });
  modelLabelRow.append(LABEL('Model'), vramNote);

  const modelSel = el('select', {
    style: 'width:100%; background:var(--surface-2); border:1px solid var(--border-2); border-radius:var(--r-md); padding:9px 12px; color:var(--text); font-size:13px; cursor:pointer; outline:none; margin-top:6px;',
  });
  const modelWarn = el('div', { style: 'font-size:11px; color:var(--red); display:none; padding-top:4px;' });
  const modelGroup = el('div');
  modelGroup.append(modelLabelRow, modelSel, modelWarn);

  apiFetch('/api/fun/models').then(data => {
    _modelData = data.models || {}; _gpuVram = data.gpu_vram_gb || 0;
    if (_gpuVram) vramNote.textContent = `${_gpuVram} GB GPU`;
    const i2v = Object.entries(_modelData).filter(([, m]) => m.i2v).sort(([a], [b]) => a.localeCompare(b));
    modelSel.innerHTML = '';
    for (const [name, info] of i2v) {
      const fits = !_gpuVram || _gpuVram >= (info.vram_min_gb || 0);
      const opt  = el('option', { value: name });
      opt.textContent = fits ? name : `${name}  (needs ${info.vram_min_gb} GB)`;
      if (!fits) opt.style.color = 'var(--red)';
      modelSel.appendChild(opt);
    }
    const fits = i2v.filter(([, m]) => !_gpuVram || _gpuVram >= (m.vram_min_gb || 0));
    const best = fits.find(([name]) => name === 'LTX-2 Dev19B Distilled') || fits[0];
    if (best) modelSel.value = best[0];
    _checkModelVram();
  }).catch(() => { modelSel.appendChild(el('option', { value: 'LTX-2 Dev19B Distilled', text: 'LTX-2 Dev19B Distilled' })); });

  function _checkModelVram() {
    const info = _modelData[modelSel.value];
    const needs = info?.vram_min_gb || 0;
    if (_gpuVram && needs > _gpuVram) { modelWarn.textContent = `This model needs ${needs} GB -- you have ${_gpuVram} GB. May fail.`; modelWarn.style.display = 'block'; }
    else { modelWarn.style.display = 'none'; }
  }
  modelSel.addEventListener('change', _checkModelVram);

  // -- Loop / variety toggles (song mode) ------------------------------------
  const _CHIP_BTN    = 'border:1px solid var(--border-2); border-radius:6px; padding:5px 12px; font-size:12px; cursor:pointer; background:transparent; color:var(--text-2); transition:background .15s,color .15s,border-color .15s;';
  const _CHIP_BTN_ON = 'background:var(--accent); border-color:var(--accent); color:#000; font-weight:600;';

  const loopBtn       = el('button', { style: _CHIP_BTN, text: 'Loop' });
  const varietyBtn    = el('button', { style: _CHIP_BTN, text: 'AI Variety' });
  const stopAfterBtn  = el('button', { style: 'display:none; border:1px solid var(--border-2); border-radius:6px; padding:5px 12px; font-size:12px; cursor:pointer; background:transparent; color:var(--text-2);', text: 'Stop after this' });
  const loopCountEl   = el('span', { style: 'font-size:11px; color:var(--text-3);', text: '' });
  const loopStatusRow = el('div', { style: 'display:none; align-items:center; gap:10px;' }, [loopCountEl, stopAfterBtn]);
  const loopSection   = el('div', { style: 'display:none; flex-direction:column; gap:8px;' });
  loopSection.append(el('div', { style: 'display:flex; gap:8px; flex-wrap:wrap;' }, [loopBtn, varietyBtn]), loopStatusRow);

  function _updateLoopUI() {
    loopBtn.setAttribute('style',    _CHIP_BTN + (_loopMode  ? _CHIP_BTN_ON : ''));
    varietyBtn.setAttribute('style', _CHIP_BTN + (_aiVariety ? _CHIP_BTN_ON : ''));
  }
  loopBtn.addEventListener('click',    () => { _loopMode  = !_loopMode;  _updateLoopUI(); });
  varietyBtn.addEventListener('click', () => { _aiVariety = !_aiVariety; _updateLoopUI(); });
  _updateLoopUI();
  stopAfterBtn.addEventListener('click', () => { _stopAfter = true; stopAfterBtn.style.display = 'none'; loopCountEl.textContent += ' -- stopping after this...'; });

  // -- Generate button -------------------------------------------------------
  const generateBtn = el('button', {
    disabled: true,
    style: 'padding:14px; border-radius:var(--r-lg); border:none; cursor:not-allowed; font-size:15px; font-weight:700; letter-spacing:.04em; background:var(--circus-red); color:var(--text); opacity:.45; transition:opacity .15s;',
    text: 'Generate Zoom',
  });
  const queueBadge = el('div', { style: 'display:none; font-size:12px; color:var(--text-2); text-align:center; padding-top:4px;' });

  function _updateBtn() {
    let ok = false;
    let label = 'Generate Zoom';
    if (_audioMode === 'song') {
      ok = _numClips > 0;
      label = 'Generate Music Video';
    } else {
      ok = !!_sourcePath;
      label = _audioMode === 'ai'
        ? (_activeCount > 0 ? `+ Add to Queue (${_activeCount} running)` : 'Generate Zoom + Music')
        : (_activeCount > 0 ? `+ Add to Queue (${_activeCount} running)` : 'Generate Zoom');
    }
    generateBtn.disabled = !ok;
    generateBtn.style.opacity = ok ? '1' : '.45';
    generateBtn.style.cursor  = ok ? 'pointer' : 'not-allowed';
    generateBtn.textContent   = label;
  }

  function _incActive() { _activeCount++; _updateBtn(); queueBadge.style.display = 'block'; queueBadge.textContent = `${_activeCount} zoom job${_activeCount > 1 ? 's' : ''} in queue -- see Queue tab`; }
  function _decActive() { _activeCount = Math.max(0, _activeCount - 1); _updateBtn(); if (_activeCount === 0) queueBadge.style.display = 'none'; }

  // -- Progress area ---------------------------------------------------------
  const progressLabel = el('div', { style: 'font-size:13px; color:var(--text-2);' });
  const progressTrack = el('div', { style: 'height:4px; background:var(--border-2); border-radius:2px; overflow:hidden;' });
  const progressFill  = el('div', { style: 'height:100%; width:0%; background:var(--accent); border-radius:2px; transition:width .4s;' });
  progressTrack.appendChild(progressFill);

  const cancelBtn = el('button', { style: 'align-self:flex-start; padding:5px 12px; border-radius:var(--r-sm); border:1px solid var(--border-2); background:transparent; color:var(--text-3); cursor:pointer; font-size:11px;', text: 'Cancel' });
  cancelBtn.onclick = () => {
    if (_jobId) stopJob(_jobId);
    if (_activePoller) { _activePoller.stop?.(); _activePoller = null; }
    _stopAfter = true; _loopMode = false; _updateLoopUI();
    loopStatusRow.style.display = 'none'; _loopCount = 0;
  };

  const progressArea = el('div', { style: 'display:none; flex-direction:column; gap:8px;' });
  progressArea.append(progressLabel, progressTrack, cancelBtn);

  // -- Output area -----------------------------------------------------------
  const videoEl = el('video', { controls: true, loop: true, playsInline: true, src: '', style: 'width:100%; display:block; border-radius:var(--r-lg); background:#000;' });
  const outputActions = el('div', { style: 'display:flex; gap:8px; flex-wrap:wrap;' });
  function _actionBtn(label, fn) {
    const b = el('button', { style: 'padding:7px 14px; border-radius:var(--r-sm); border:1px solid var(--border-2); background:var(--surface); color:var(--text-2); cursor:pointer; font-size:12px; transition:background .12s;', text: label });
    b.onclick = fn; return b;
  }
  const outputArea = el('div', { style: 'display:none; flex-direction:column; gap:12px;' });
  outputArea.append(videoEl, outputActions);

  // -- Folder batch (non-song modes) -----------------------------------------
  let _folderFiles = [], _folderPath = '', _loopActive = false, _loopPollTimer = null;

  const batchDivider = el('div', { style: 'display:flex; align-items:center; gap:10px; color:var(--text-3); font-size:11px; padding-top:4px;' });
  batchDivider.innerHTML = '<hr style="flex:1;border:none;border-top:1px solid var(--border-2)"> or process a whole folder <hr style="flex:1;border:none;border-top:1px solid var(--border-2)">';

  const folderNameEl    = el('div', { style: 'flex:1; font-size:13px; color:var(--text-2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; padding:9px 0;', text: 'No folder selected' });
  const browseFolderBtn = el('button', { text: 'Choose Folder', style: 'padding:9px 16px; border-radius:var(--r-md); border:1px solid var(--accent-border); background:var(--accent-bg); color:var(--accent); cursor:pointer; font-size:13px; font-weight:600; white-space:nowrap;' });
  const loopToggle      = el('label', { style: 'display:flex; align-items:center; gap:8px; cursor:pointer; font-size:13px; color:var(--text-2); user-select:none;' });
  const loopCheck       = el('input', { type: 'checkbox' });
  loopCheck.style.accentColor = 'var(--accent)';
  loopToggle.append(loopCheck, 'Loop continuously');

  const folderStatus = el('div', { style: 'font-size:12px; color:var(--text-3); min-height:16px;' });
  const batchBtn     = el('button', { text: 'Queue All', disabled: true, style: 'padding:11px; border-radius:var(--r-lg); border:none; cursor:not-allowed; font-size:14px; font-weight:700; background:var(--circus-red); color:var(--text); opacity:.45;' });

  function _setBatchReady() {
    const n = _folderFiles.length;
    batchBtn.disabled = !n; batchBtn.style.opacity = n ? '1' : '.45'; batchBtn.style.cursor = n ? 'pointer' : 'not-allowed';
    batchBtn.textContent = n ? (loopCheck.checked ? `Start Loop (${n} files)` : `Queue All ${n} Files`) : 'Queue All';
  }
  loopCheck.addEventListener('change', _setBatchReady);

  async function _scanFolder(folder) {
    folderStatus.textContent = 'Scanning...'; _folderFiles = []; _setBatchReady();
    try {
      const r = await apiFetch('/api/zoom/scan-folder', { method: 'POST', body: JSON.stringify({ folder }) });
      _folderFiles = r.files || [];
      const imgs = _folderFiles.filter(f => !f.is_video).length, vids = _folderFiles.filter(f => f.is_video).length;
      folderStatus.textContent = !_folderFiles.length ? 'No supported files found (jpg, png, mp4, mov, ...)' : [imgs && `${imgs} image${imgs !== 1 ? 's' : ''}`, vids && `${vids} video${vids !== 1 ? 's' : ''}`].filter(Boolean).join(', ') + ' found';
      _setBatchReady();
    } catch (e) { folderStatus.textContent = 'Error: ' + e.message; }
  }

  browseFolderBtn.onclick = async () => {
    if (_loopActive) return;
    try {
      const r = await apiFetch('/api/browse-folder', { method: 'POST' });
      const picked = r.folder || r.path;
      if (picked) { _folderPath = picked; folderNameEl.textContent = picked.split(/[\\/]/).pop() || picked; folderNameEl.title = picked; await _scanFolder(picked); }
    } catch {}
  };

  function _stopLoopPoll() { if (_loopPollTimer) { clearInterval(_loopPollTimer); _loopPollTimer = null; } }

  async function _pollLoopStatus() {
    try {
      const snap = await apiFetch('/api/zoom/folder-loop/status');
      const total = snap.files?.length || _folderFiles.length;
      if (snap.active) {
        batchBtn.textContent = `Stop Loop (${snap.index}/${total})`; batchBtn.disabled = false; batchBtn.style.opacity = '1';
        folderStatus.textContent = snap.current_file ? `Processing: ${snap.current_file} -- ${snap.succeeded} done, ${snap.failed} failed` : 'Running...';
      } else {
        _loopActive = false; _stopLoopPoll(); batchBtn.textContent = `Start Loop (${total} files)`; browseFolderBtn.disabled = false;
        const msg = `Loop ${snap.status === 'done' ? 'done' : 'stopped'} -- ${snap.succeeded} ok, ${snap.failed} failed`;
        folderStatus.textContent = msg; toast(msg, snap.failed ? 'error' : 'success');
      }
    } catch {}
  }

  batchBtn.onclick = async () => {
    if (!_folderFiles.length && !_loopActive) return;
    if (_loopActive) {
      await apiFetch('/api/zoom/folder-loop/stop', { method: 'POST' }).catch(() => {});
      _loopActive = false; _stopLoopPoll(); batchBtn.textContent = `Start Loop (${_folderFiles.length} files)`;
      folderStatus.textContent = 'Loop stopped.'; browseFolderBtn.disabled = false; return;
    }
    const nClips = Number(_activeValue(stepsRow)) || 4, clipDur = Number(_activeValue(durRow)) || 5;
    const body = { folder: _folderPath, zoom_direction: _direction, n_clips: nClips, clip_duration: clipDur, idea: ideaInput.value.trim(), model_name: modelSel.value, skip_audio: _audioMode === 'none', audio_first: (_audioMode === 'ai' && audioFirstCheck.checked), repeat: loopCheck.checked };
    if (loopCheck.checked) {
      batchBtn.disabled = true; browseFolderBtn.disabled = true;
      try {
        await apiFetch('/api/zoom/folder-loop/start', { method: 'POST', body: JSON.stringify(body) });
        _loopActive = true; batchBtn.disabled = false; batchBtn.textContent = `Stop Loop (0/${_folderFiles.length})`;
        folderStatus.textContent = 'Loop started -- processing one file at a time...';
        _loopPollTimer = setInterval(_pollLoopStatus, 5000);
      } catch (e) { batchBtn.disabled = false; browseFolderBtn.disabled = false; toast('Failed to start loop: ' + e.message, 'error'); _setBatchReady(); }
    } else {
      batchBtn.disabled = true; let queued = 0;
      for (const f of _folderFiles) {
        batchBtn.textContent = `Queuing ${queued + 1}/${_folderFiles.length}...`;
        try {
          const res = await apiFetch('/api/zoom/make', { method: 'POST', body: JSON.stringify({ source_path: f.path, ...body }) });
          _incActive();
          pollJob(res.job_id, () => {}, j => {
            _decActive();
            const out = Array.isArray(j.output) ? j.output[0] : j.output;
            if (out) { videoEl.src = pathToUrl(out); videoEl.style.opacity = '1'; videoEl.load(); outputArea.style.display = 'flex'; outputActions.innerHTML = ''; outputActions.append(_actionBtn('Open in folder', () => apiFetch('/api/reveal', { method: 'POST', body: JSON.stringify({ path: out, action: 'explorer' }) }).catch(() => {}))); }
            toast(`Zoom done: ${f.name}`, 'success'); document.dispatchEvent(new CustomEvent('session-updated'));
          }, msg => { _decActive(); toast(`Zoom failed (${f.name}): ${msg}`, 'error'); });
          queued++;
        } catch (e) {
          if (e.status === 429 || /queue.*full/i.test(e.message)) { toast(`Queue full at ${queued + 1}/${_folderFiles.length}`, 'error'); break; }
          toast(`Failed to queue ${f.name}: ${e.message}`, 'error');
        }
      }
      batchBtn.disabled = false; batchBtn.textContent = `Queued ${queued} -- check Queue tab`; setTimeout(_setBatchReady, 4000);
    }
  };

  const batchSection = el('div', { style: 'display:flex; flex-direction:column; gap:10px;' });
  batchSection.append(batchDivider, el('div', { style: 'display:flex; gap:8px; align-items:center;' }, [folderNameEl, browseFolderBtn]), loopToggle, folderStatus, batchBtn);

  // -- Assemble layout -------------------------------------------------------
  root.append(
    _card([sourceLabelEl, dropArea]),
    audioSection,
    dirSection,
    controlsGrid,
    clipSummarySection,
    el('div', { style: 'display:flex; flex-direction:column; gap:6px;' }, [LABEL('What to explore (optional)'), ideaInput]),
    _card(modelGroup),
    loopSection,
    generateBtn,
    progressArea,
    queueBadge,
    batchSection,
    outputArea,
  );

  // Initial state: no audio, zoom only
  _setAudioMode('none');

  // -- Generate: music video (song mode) ------------------------------------
  async function _submitMusicVideo() {
    const varietyTheme = _aiVariety ? _VARIETY_THEMES[(_varietyIdx++) % _VARIETY_THEMES.length] : '';
    const payload = {
      audio_path:     _songPath,
      photo_path:     _sourcePath || '',
      video_prompt:   ideaInput.value.trim(),
      variety_theme:  varietyTheme,
      lyrics_text:    _lyricsTA ? _lyricsTA.value.trim() : '',
      user_direction: 'music video where visual energy matches song dynamics',
      audio_analysis: _songAnalysis || undefined,
      model:          modelSel.value,
      clip_duration:  _clipDur,
      num_clips:      _numClips,
      coverage_ratio: _coverage,
      steps:          _steps,
      guidance:       _guidance,
      output_width:   _outW,
      output_height:  _outH,
    };

    _loopCount++; _stopAfter = false; generateBtn.disabled = true;
    if (_loopMode) { loopStatusRow.style.display = 'flex'; stopAfterBtn.style.display = ''; loopCountEl.textContent = `Video ${_loopCount} generating...`; }

    try {
      const resp = await apiFetch('/api/song-video/generate', { method: 'POST', body: JSON.stringify(payload) });
      _jobId = resp.job_id;
      document.dispatchEvent(new CustomEvent('job-queued', { detail: { job_id: _jobId } }));
      progressFill.style.background = 'var(--accent)';
      progressLabel.textContent = 'Planning story arc...'; progressFill.style.width = '0%'; progressArea.style.display = 'flex';

      const maxPolls = resp.timeout_sec > 0 ? Math.max(400, Math.ceil((resp.timeout_sec + 300) * 1000 / 1500)) : 400;
      _activePoller = pollJob(_jobId,
        j => {
          progressFill.style.width = `${j.progress || 0}%`;
          progressLabel.textContent = j.status === 'queued' ? `In queue -- ${j.queue_position === 0 ? 'up next' : `position ${j.queue_position + 1}`}...` : (j.message || `${j.progress || 0}%`);
        },
        j => {
          _activePoller = null; _jobId = null; progressArea.style.display = 'none';
          const out = Array.isArray(j.output) ? j.output[0] : j.output;
          if (out) {
            videoEl.src = pathToUrl(out); videoEl.style.opacity = '1'; videoEl.load();
            outputArea.style.display = 'flex'; outputActions.innerHTML = '';
            outputActions.append(
              _actionBtn('Open in folder', () => apiFetch('/api/reveal', { method: 'POST', body: JSON.stringify({ path: out, action: 'explorer' }) }).catch(() => {})),
              _actionBtn('Send to Bridges', () => { handoff('bridges', { type: 'video', path: out, url: pathToUrl(out) }); toast('Sent to Bridges', 'success'); }),
            );
          }
          if (_loopMode && !_stopAfter) {
            setTimeout(() => _submitMusicVideo().catch(e => { generateBtn.disabled = false; toast(`Loop stopped: ${e.message}`, 'error'); }), 1500);
          } else {
            generateBtn.disabled = false; loopStatusRow.style.display = 'none'; _loopCount = 0;
          }
          toast('Music video done!', 'success'); document.dispatchEvent(new CustomEvent('session-updated'));
        },
        msg => {
          _activePoller = null; _jobId = null;
          progressFill.style.background = 'var(--red, #c41e3a)'; progressLabel.textContent = `Failed: ${msg}`;
          generateBtn.disabled = false; loopStatusRow.style.display = 'none'; _loopCount = 0;
          toast(`Music video failed: ${msg}`, 'error');
        },
        1500, maxPolls,
      );
    } catch (e) {
      generateBtn.disabled = false;
      toast(e.status === 429 || /queue.*full|full.*queue/i.test(e.message) ? 'Queue is full -- wait for a job to finish' : 'Failed: ' + (e.message || e), 'error');
    }
  }

  // -- Generate: zoom (none / ai modes) -------------------------------------
  generateBtn.onclick = async () => {
    if (_audioMode === 'song') { await _submitMusicVideo(); return; }
    if (!_sourcePath) return;

    const nClips  = Number(_activeValue(stepsRow)) || 4;
    const clipDur = Number(_activeValue(durRow))   || 5;
    let queueDepth = 0;
    try { const qs = await apiFetch('/api/jobs'); queueDepth = (qs.running?.length || 0) + (qs.queued?.length || 0); } catch {}

    generateBtn.disabled = true; generateBtn.textContent = 'Queuing...';

    try {
      const res = await apiFetch('/api/zoom/make', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_path:    _sourcePath,
          zoom_direction: _direction,
          n_clips:        nClips,
          clip_duration:  clipDur,
          idea:           ideaInput.value.trim(),
          model_name:     modelSel.value,
          skip_audio:     _audioMode === 'none',
          audio_first:    _audioMode === 'ai' && audioFirstCheck.checked,
        }),
      });

      _jobId = res.job_id; _incActive();
      progressFill.style.background = 'var(--accent)';
      progressLabel.textContent = queueDepth > 0 ? `Queued (#${queueDepth + 1}) -- waiting for GPU...` : 'Starting...';
      progressFill.style.width = '0%'; progressArea.style.display = 'flex';

      _activePoller = pollJob(res.job_id,
        j => { progressFill.style.width = `${j.progress || 0}%`; progressLabel.textContent = j.message || `${j.progress || 0}%`; },
        j => {
          _activePoller = null; _jobId = null; _decActive(); progressArea.style.display = 'none';
          if (_extendPanel) { _extendPanel.remove(); _extendPanel = null; }
          const out = Array.isArray(j.output) ? j.output[0] : j.output;
          if (out) {
            videoEl.src = pathToUrl(out); videoEl.style.opacity = '1'; videoEl.load();
            outputArea.style.display = 'flex'; outputActions.innerHTML = '';
            outputActions.append(
              _actionBtn('Open in folder', () => apiFetch('/api/reveal', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: out, action: 'explorer' }) }).catch(() => {})),
              _actionBtn('Send to Bridges', () => { handoff('bridges', { type: 'video', path: out, url: pathToUrl(out) }); toast('Sent to Bridges', 'success'); }),
              _actionBtn('Continue zoom...', () => _showExtendPanel(out, _direction)),
            );
          }
          toast(`Zoom ${_direction} complete!`, 'success'); document.dispatchEvent(new CustomEvent('session-updated'));
        },
        msg => { _activePoller = null; _jobId = null; _decActive(); progressArea.style.display = 'none'; progressFill.style.width = '0%'; toast(`Zoom failed: ${msg}`, 'error'); },
      );
    } catch (err) {
      toast(err.status === 429 || /queue.*full|full.*queue/i.test(err.message) ? 'Queue is full -- open the Queue tab and wait for a job to finish' : 'Failed: ' + err.message, 'error');
    } finally { _updateBtn(); }
  };

  // -- Extend/continue panel (zoom modes) ------------------------------------
  function _showExtendPanel(existingVideoPath, capturedDirection) {
    if (_extendPanel) { _extendPanel.remove(); _extendPanel = null; }
    const epanel = el('div', { style: 'background:var(--bg-2); border:1px solid var(--border); border-radius:8px; padding:14px; display:flex; flex-direction:column; gap:10px;' });
    _extendPanel = epanel;
    epanel.append(el('div', { style: 'font-size:12px; font-weight:700; color:var(--text-2); text-transform:uppercase; letter-spacing:.08em;', textContent: 'Continue this zoom' }));
    const clipsRow = el('div', { style: 'display:flex; align-items:center; gap:8px; font-size:13px; color:var(--text-2);' });
    clipsRow.append(el('span', { textContent: 'Additional clips:' }));
    const clipsInput = el('input', { type: 'number', min: '2', max: '10', value: '3', style: 'width:56px; padding:4px 6px; background:var(--bg-1); color:var(--text-1); border:1px solid var(--border); border-radius:4px;' });
    clipsRow.append(clipsInput);
    const durRow2 = el('div', { style: 'display:flex; align-items:center; gap:8px; font-size:13px; color:var(--text-2);' });
    durRow2.append(el('span', { textContent: 'Seconds per clip:' }));
    const durInput = el('input', { type: 'number', min: '3', max: '12', value: '4', style: 'width:56px; padding:4px 6px; background:var(--bg-1); color:var(--text-1); border:1px solid var(--border); border-radius:4px;' });
    durRow2.append(durInput);
    const ideaInput2 = el('textarea', { rows: '2', placeholder: 'Optional: describe what the zoom continues to reveal...', style: 'width:100%; resize:vertical; font-size:13px; background:var(--bg-1); color:var(--text-1); border:1px solid var(--border); border-radius:4px; padding:6px;' });
    const btnRow = el('div', { style: 'display:flex; gap:8px;' });
    const submitBtn = el('button', { class: 'btn btn-primary', textContent: 'Continue zoom' });
    const cancelBtn2 = el('button', { class: 'btn', textContent: 'Cancel', style: 'background:var(--bg-3);' });
    cancelBtn2.onclick = () => { epanel.remove(); _extendPanel = null; };
    submitBtn.onclick = async () => {
      submitBtn.disabled = true; submitBtn.textContent = 'Queuing...';
      try {
        const res = await apiFetch('/api/zoom/extend', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ existing_video_path: existingVideoPath, zoom_direction: capturedDirection, n_clips: Number(clipsInput.value) || 3, clip_duration: Number(durInput.value) || 4, model_name: modelSel.value, idea: ideaInput2.value.trim(), audio_first: _audioMode === 'ai' && audioFirstCheck.checked }) });
        _incActive(); epanel.remove(); _extendPanel = null;
        progressFill.style.background = 'var(--accent)'; progressLabel.textContent = 'Extending zoom...'; progressFill.style.width = '0%'; progressArea.style.display = 'flex';
        pollJob(res.job_id,
          j => { progressFill.style.width = `${j.progress || 0}%`; progressLabel.textContent = j.message || `${j.progress || 0}%`; },
          j => {
            _decActive(); progressArea.style.display = 'none';
            const out = Array.isArray(j.output) ? j.output[0] : j.output;
            if (out) { videoEl.src = pathToUrl(out); videoEl.style.opacity = '1'; videoEl.load(); outputArea.style.display = 'flex'; outputActions.innerHTML = ''; outputActions.append(_actionBtn('Open in folder', () => apiFetch('/api/reveal', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: out, action: 'explorer' }) }).catch(() => {})), _actionBtn('Send to Bridges', () => { handoff('bridges', { type: 'video', path: out, url: pathToUrl(out) }); toast('Sent to Bridges', 'success'); }), _actionBtn('Continue zoom...', () => _showExtendPanel(out, capturedDirection))); }
            toast('Zoom extended!', 'success'); document.dispatchEvent(new CustomEvent('session-updated'));
          },
          msg => { _decActive(); progressArea.style.display = 'none'; progressFill.style.width = '0%'; toast(`Extend failed: ${msg}`, 'error'); },
        );
      } catch (err) {
        toast(err.status === 429 || /queue.*full/i.test(err.message) ? 'Queue full -- wait for a job to finish first' : 'Failed: ' + err.message, 'error');
        if (epanel.isConnected) { submitBtn.disabled = false; submitBtn.textContent = 'Continue zoom'; }
      }
    };
    btnRow.append(submitBtn, cancelBtn2);
    epanel.append(clipsRow, durRow2, ideaInput2, btnRow);
    outputArea.after(epanel);
  }
}
