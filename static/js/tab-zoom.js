/**
 * Infinite Zoom tab -- zoom in or out from a photo or video.
 *
 * Source -> direction toggle -> clip count -> Generate
 * Chains WanGP clips with lossless frame anchoring (no re-anchor).
 */
import { api, pollJob, stopJob } from './api.js?v=20260520a';
import { el, pathToUrl } from './components.js?v=20260520a';
import { toast, apiFetch } from './shell/toast.js?v=20260520a';

const VERSION = '20260520a';

export function init(panel) {
  panel.innerHTML = '';
  const root = el('div', {
    style: 'max-width:720px; margin:0 auto; padding:24px 16px; display:flex; flex-direction:column; gap:22px;',
  });
  panel.appendChild(root);

  // -- State ------------------------------------------------------------------
  let _sourcePath = null;      // absolute path to photo or video
  let _sourceThumb = null;     // data URL or /uploads URL for preview
  let _isVideo = false;
  let _direction = 'out';
  let _jobId = null;
  let _stopRequested = false;

  // -- Source drop zone -------------------------------------------------------
  const sourceSection = el('div', { style: 'display:flex; flex-direction:column; gap:10px;' });

  const dropLabel = el('div', {
    style: 'color:var(--text-muted); font-size:12px; letter-spacing:.08em; text-transform:uppercase;',
  });
  dropLabel.textContent = 'Source photo or video';
  sourceSection.appendChild(dropLabel);

  const dropArea = el('div', {
    style: [
      'border:2px dashed var(--border); border-radius:10px; padding:32px 20px;',
      'text-align:center; cursor:pointer; color:var(--text-muted);',
      'transition:border-color .2s, background .2s; background:var(--bg-card);',
      'position:relative; min-height:120px; display:flex; align-items:center;',
      'justify-content:center; gap:12px; flex-direction:column;',
    ].join(''),
  });

  const dropIcon = el('div', { style: 'font-size:32px; opacity:.5;' });
  dropIcon.textContent = '+';
  const dropText = el('div', { style: 'font-size:14px;' });
  dropText.textContent = 'Drop a photo or video here, or click to browse';

  const previewImg = el('img', {
    style: 'max-height:160px; max-width:100%; border-radius:6px; display:none; object-fit:contain;',
  });

  const sourceNameRow = el('div', {
    style: 'font-size:12px; color:var(--text-muted); display:none; align-items:center; gap:8px;',
  });
  const sourceNameLabel = el('span');
  const clearBtn = el('button', {
    style: 'background:none; border:none; color:var(--crimson); cursor:pointer; font-size:11px; padding:0;',
  });
  clearBtn.textContent = 'clear';
  clearBtn.onclick = e => { e.stopPropagation(); _clearSource(); };
  sourceNameRow.append(sourceNameLabel, clearBtn);

  dropArea.append(dropIcon, dropText, previewImg);
  sourceSection.append(dropArea, sourceNameRow);

  // File input
  const fileInput = el('input', { type: 'file', accept: 'image/*,video/*', style: 'display:none;' });
  panel.appendChild(fileInput);

  dropArea.addEventListener('dragover', e => {
    e.preventDefault();
    dropArea.style.borderColor = 'var(--gold)';
    dropArea.style.background = 'var(--bg-card-hover, rgba(212,160,23,.06))';
  });
  dropArea.addEventListener('dragleave', () => {
    dropArea.style.borderColor = 'var(--border)';
    dropArea.style.background = 'var(--bg-card)';
  });
  dropArea.addEventListener('drop', e => {
    e.preventDefault();
    dropArea.style.borderColor = 'var(--border)';
    dropArea.style.background = 'var(--bg-card)';
    const file = e.dataTransfer.files[0];
    if (file) _uploadFile(file);
  });
  dropArea.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    if (fileInput.files[0]) _uploadFile(fileInput.files[0]);
    fileInput.value = '';
  });

  function _clearSource() {
    _sourcePath = null;
    _sourceThumb = null;
    _isVideo = false;
    previewImg.style.display = 'none';
    previewImg.src = '';
    dropIcon.style.display = '';
    dropText.style.display = '';
    sourceNameRow.style.display = 'none';
    generateBtn.disabled = true;
  }

  async function _uploadFile(file) {
    const isVid = file.type.startsWith('video/');
    dropText.textContent = 'Uploading...';
    dropIcon.style.display = 'none';

    try {
      const fd = new FormData();
      fd.append('file', file);
      const res = await fetch('/api/upload', { method: 'POST', body: fd });
      if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
      const data = await res.json();
      _sourcePath = data.path;
      _isVideo = isVid;

      if (isVid) {
        // Extract first/last frame for preview
        const pos = _direction === 'out' ? -1 : 0.1;
        const fr = await apiFetch('/api/zoom/extract-frame', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ video_path: _sourcePath, time_sec: pos }),
        }).catch(() => null);
        _sourceThumb = fr?.frame_url || null;
        if (_sourceThumb) {
          previewImg.src = _sourceThumb;
          previewImg.style.display = 'block';
        }
      } else {
        previewImg.src = pathToUrl(data.path);
        previewImg.style.display = 'block';
        _sourceThumb = previewImg.src;
      }

      dropIcon.style.display = 'none';
      dropText.style.display = 'none';
      sourceNameRow.style.display = 'flex';
      sourceNameLabel.textContent = file.name;
      generateBtn.disabled = false;

    } catch (err) {
      toast('Upload failed: ' + err.message, 'error');
      dropText.textContent = 'Drop a photo or video here, or click to browse';
      dropIcon.style.display = '';
    }
  }

  // -- Direction toggle -------------------------------------------------------
  const dirSection = el('div', { style: 'display:flex; flex-direction:column; gap:10px;' });
  const dirLabel = el('div', {
    style: 'color:var(--text-muted); font-size:12px; letter-spacing:.08em; text-transform:uppercase;',
  });
  dirLabel.textContent = 'Direction';
  dirSection.appendChild(dirLabel);

  const dirToggle = el('div', {
    style: 'display:grid; grid-template-columns:1fr 1fr; gap:8px;',
  });

  function _makeDirectionBtn(label, icon, value, description) {
    const btn = el('button', {
      style: [
        'display:flex; flex-direction:column; align-items:center; gap:6px;',
        'padding:14px 12px; border-radius:8px; border:2px solid var(--border);',
        'background:var(--bg-card); cursor:pointer; transition:all .15s;',
        'color:var(--text-muted);',
      ].join(''),
    });
    const ico = el('span', { style: 'font-size:22px; line-height:1;' });
    ico.textContent = icon;
    const lbl = el('span', { style: 'font-size:13px; font-weight:600; color:var(--text);' });
    lbl.textContent = label;
    const desc = el('span', { style: 'font-size:10px; text-align:center; opacity:.65;' });
    desc.textContent = description;
    btn.append(ico, lbl, desc);
    btn.dataset.value = value;
    btn.onclick = () => _setDirection(value);
    return btn;
  }

  const btnOut = _makeDirectionBtn('Zoom Out', '->', 'out', 'Reveals surroundings');
  const btnIn  = _makeDirectionBtn('Zoom In',  '<-', 'in',  'Approaches detail');
  dirToggle.append(btnOut, btnIn);
  dirSection.appendChild(dirToggle);

  function _setDirection(dir) {
    _direction = dir;
    [btnOut, btnIn].forEach(b => {
      const active = b.dataset.value === dir;
      b.style.borderColor = active ? 'var(--gold)' : 'var(--border)';
      b.style.background  = active ? 'rgba(212,160,23,.1)' : 'var(--bg-card)';
      b.style.color       = active ? 'var(--gold)' : 'var(--text-muted)';
    });
  }
  _setDirection('out');

  // -- Controls ---------------------------------------------------------------
  const controlsRow = el('div', {
    style: 'display:grid; grid-template-columns:1fr 1fr; gap:16px;',
  });

  // Clip count
  const clipGroup = el('div', { style: 'display:flex; flex-direction:column; gap:6px;' });
  const clipLabel = el('label', {
    style: 'color:var(--text-muted); font-size:12px; letter-spacing:.08em; text-transform:uppercase;',
  });
  clipLabel.textContent = 'Zoom steps';

  const clipRow = el('div', { style: 'display:flex; gap:6px;' });
  [3, 4, 5, 6].forEach(n => {
    const b = el('button', {
      style: [
        'flex:1; padding:7px 0; border-radius:6px; border:2px solid var(--border);',
        'background:var(--bg-card); cursor:pointer; font-size:13px; font-weight:600;',
        'color:var(--text-muted); transition:all .15s;',
      ].join(''),
    });
    b.textContent = n;
    b.dataset.clips = n;
    b.onclick = () => {
      clipRow.querySelectorAll('button').forEach(x => {
        x.style.borderColor = 'var(--border)';
        x.style.background  = 'var(--bg-card)';
        x.style.color       = 'var(--text-muted)';
      });
      b.style.borderColor = 'var(--gold)';
      b.style.background  = 'rgba(212,160,23,.1)';
      b.style.color       = 'var(--gold)';
    };
    if (n === 5) b.click();
    clipRow.appendChild(b);
  });
  clipGroup.append(clipLabel, clipRow);

  // Clip duration
  const durGroup = el('div', { style: 'display:flex; flex-direction:column; gap:6px;' });
  const durLabel = el('label', {
    style: 'color:var(--text-muted); font-size:12px; letter-spacing:.08em; text-transform:uppercase;',
  });
  durLabel.textContent = 'Seconds per step';

  const durRow = el('div', { style: 'display:flex; gap:6px;' });
  [4, 5, 6, 8].forEach(n => {
    const b = el('button', {
      style: [
        'flex:1; padding:7px 0; border-radius:6px; border:2px solid var(--border);',
        'background:var(--bg-card); cursor:pointer; font-size:13px; font-weight:600;',
        'color:var(--text-muted); transition:all .15s;',
      ].join(''),
    });
    b.textContent = n;
    b.dataset.dur = n;
    b.onclick = () => {
      durRow.querySelectorAll('button').forEach(x => {
        x.style.borderColor = 'var(--border)';
        x.style.background  = 'var(--bg-card)';
        x.style.color       = 'var(--text-muted)';
      });
      b.style.borderColor = 'var(--gold)';
      b.style.background  = 'rgba(212,160,23,.1)';
      b.style.color       = 'var(--gold)';
    };
    if (n === 5) b.click();
    durRow.appendChild(b);
  });
  durGroup.append(durLabel, durRow);

  controlsRow.append(clipGroup, durGroup);

  // -- Idea field -------------------------------------------------------------
  const ideaGroup = el('div', { style: 'display:flex; flex-direction:column; gap:6px;' });
  const ideaLabel = el('label', {
    style: 'color:var(--text-muted); font-size:12px; letter-spacing:.08em; text-transform:uppercase;',
  });
  ideaLabel.textContent = 'What the zoom reveals (optional)';
  const ideaInput = el('textarea', {
    placeholder: 'e.g. "zoom out to reveal a foggy mountain valley" or "zoom into the ring on her finger"',
    rows: 2,
    style: [
      'width:100%; box-sizing:border-box; background:var(--bg-card); border:1px solid var(--border);',
      'border-radius:8px; padding:10px 12px; color:var(--text); font-size:13px;',
      'resize:vertical; font-family:inherit;',
    ].join(''),
  });
  ideaGroup.append(ideaLabel, ideaInput);

  // -- Generate button --------------------------------------------------------
  const generateBtn = el('button', {
    disabled: true,
    style: [
      'padding:14px; border-radius:10px; border:none; cursor:pointer; font-size:15px;',
      'font-weight:700; letter-spacing:.04em; background:var(--crimson); color:#fff;',
      'transition:all .15s; opacity:.5;',
    ].join(''),
  });
  generateBtn.textContent = 'Generate Zoom';
  generateBtn.addEventListener('change', () => {});
  panel.addEventListener('dcs:source-ready', () => { generateBtn.disabled = false; });

  // Enable/disable styles
  const _updateBtnState = () => {
    const disabled = !_sourcePath;
    generateBtn.disabled = disabled;
    generateBtn.style.opacity = disabled ? '.5' : '1';
    generateBtn.style.cursor  = disabled ? 'not-allowed' : 'pointer';
  };

  // -- Progress area ----------------------------------------------------------
  const progressArea = el('div', { style: 'display:none; flex-direction:column; gap:10px;' });

  const progressLabel = el('div', {
    style: 'font-size:13px; color:var(--text-muted);',
  });
  const progressBar = el('div', {
    style: 'height:6px; background:var(--border); border-radius:3px; overflow:hidden;',
  });
  const progressFill = el('div', {
    style: 'height:100%; width:0%; background:var(--gold); border-radius:3px; transition:width .4s;',
  });
  progressBar.appendChild(progressFill);

  const stopBtn = el('button', {
    style: [
      'align-self:flex-start; padding:6px 14px; border-radius:6px; border:1px solid var(--border);',
      'background:transparent; color:var(--text-muted); cursor:pointer; font-size:12px;',
    ].join(''),
  });
  stopBtn.textContent = 'Cancel';
  stopBtn.onclick = () => {
    if (_jobId) { stopJob(_jobId); _stopRequested = true; }
  };

  progressArea.append(progressLabel, progressBar, stopBtn);

  // -- Output area ------------------------------------------------------------
  const outputArea = el('div', { style: 'display:none; flex-direction:column; gap:14px;' });

  const videoWrap = el('div', {
    style: 'border-radius:10px; overflow:hidden; background:#000; position:relative;',
  });
  const videoEl = el('video', {
    controls: true, loop: true, playsInline: true,
    style: 'width:100%; display:block; max-height:480px;',
  });
  videoWrap.appendChild(videoEl);

  const outputActions = el('div', { style: 'display:flex; gap:10px; flex-wrap:wrap;' });

  function _makeAction(label, icon, fn) {
    const b = el('button', {
      style: [
        'display:flex; align-items:center; gap:6px; padding:8px 14px;',
        'border-radius:7px; border:1px solid var(--border); background:var(--bg-card);',
        'color:var(--text); cursor:pointer; font-size:13px; transition:background .15s;',
      ].join(''),
    });
    b.innerHTML = `<span>${icon}</span><span>${label}</span>`;
    b.onclick = fn;
    return b;
  }

  outputArea.append(videoWrap, outputActions);

  // -- Assemble panel ---------------------------------------------------------
  root.append(sourceSection, dirSection, controlsRow, ideaGroup, generateBtn, progressArea, outputArea);

  // -- Generate logic ---------------------------------------------------------
  generateBtn.onclick = async () => {
    if (!_sourcePath) { toast('Drop a photo or video first', 'warn'); return; }

    const nClips = parseInt(clipRow.querySelector('button[style*="gold"]')?.dataset.clips || '5');
    const clipDur = parseFloat(durRow.querySelector('button[style*="gold"]')?.dataset.dur || '5');

    generateBtn.style.display = 'none';
    progressArea.style.display = 'flex';
    outputArea.style.display = 'none';
    _stopRequested = false;
    progressFill.style.width = '0%';
    progressLabel.textContent = 'Submitting...';

    try {
      const res = await apiFetch('/api/zoom/make', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_path:    _sourcePath,
          zoom_direction: _direction,
          n_clips:        nClips,
          clip_duration:  clipDur,
          idea:           ideaInput.value.trim(),
          skip_audio:     false,
        }),
      });

      _jobId = res.job_id;

      await pollJob(_jobId, {
        onProgress(pct, msg) {
          progressFill.style.width = pct + '%';
          progressLabel.textContent = msg || 'Working...';
        },
        onDone(outputPath) {
          progressArea.style.display = 'none';
          outputArea.style.display   = 'flex';
          generateBtn.style.display  = '';
          _updateBtnState();

          const url = pathToUrl(outputPath);
          videoEl.src = url;
          videoEl.load();

          outputActions.innerHTML = '';
          outputActions.append(
            _makeAction('Open in folder', '[>]', () =>
              apiFetch('/api/open-folder', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: outputPath }),
              }).catch(() => {})),
            _makeAction('Send to Bridges', '[~]', () => {
              document.dispatchEvent(new CustomEvent('dcs:handoff', {
                detail: { from: 'zoom', to: 'bridges', path: outputPath },
              }));
              toast('Sent to Bridges tab', 'success');
            }),
          );

          toast(`Zoom ${_direction} complete!`, 'success');
          document.dispatchEvent(new CustomEvent('session-updated'));
        },
        onError(msg) {
          progressArea.style.display = 'none';
          generateBtn.style.display  = '';
          _updateBtnState();
          toast('Error: ' + msg, 'error');
        },
      });

    } catch (err) {
      progressArea.style.display = 'none';
      generateBtn.style.display  = '';
      _updateBtnState();
      toast('Failed to start: ' + err.message, 'error');
    }
  };
}
