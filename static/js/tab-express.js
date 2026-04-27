/**
 * Drop Cat Go Studio — Express mode.
 * Drop an image, describe your idea, click Create. Everything else is automatic.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js?v=20260414';
import { createVideoPlayer, el, pathToUrl } from './components.js?v=20260426c';
import { toast, apiFetch } from './shell/toast.js?v=20260421c';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260426g';
import { handoff } from './handoff.js?v=20260422a';

// Module-level so receiveHandoff can call _applyImageFn even after init
let _applyImageFn = null;

export function receiveHandoff(data) {
  if (!_applyImageFn) return;
  if (data?.type === 'image' && data.path) {
    _applyImageFn(data.path, data.url || pathToUrl(data.path));
  }
}

export function init(panel) {
  panel.innerHTML = '';
  const root = el('div', { style: 'max-width:680px; margin:0 auto; padding:24px 16px; display:flex; flex-direction:column; gap:20px;' });
  panel.appendChild(root);

  // ── Heading ───────────────────────────────────────────────────────────────
  root.appendChild(el('div', { style: 'text-align:center; padding-bottom:4px;' }, [
    el('div', { style: 'font-size:1.4rem; font-weight:700; color:var(--text); margin-bottom:6px;', text: 'Create a video' }),
    el('div', { style: 'font-size:.85rem; color:var(--text-3);', text: 'Drop an image, describe what you want, click Create.' }),
  ]));

  // ── Image drop zone ───────────────────────────────────────────────────────
  const imgInput = el('input', { type: 'file', accept: 'image/*', style: 'display:none' });
  root.appendChild(imgInput);

  let _imagePath = null;
  const preview = el('img', { style: 'display:none; width:100%; max-height:260px; object-fit:contain; border-radius:8px; background:var(--bg-raised);' });
  const dropHint = el('div', { style: 'color:var(--text-3); font-size:.88rem;', text: 'Drop an image here or click to browse' });
  const dropZone = el('div', { class: 'drop-zone', style: 'position:relative;' }, [preview, dropHint]);
  root.appendChild(dropZone);

  function _applyImage(path, url) {
    _imagePath = path;
    preview.src = url;
    preview.style.display = '';
    dropHint.style.display = 'none';
    dropZone.classList.add('drop-zone-loaded');
  }
  _applyImageFn = _applyImage;

  dropZone.addEventListener('click', () => imgInput.click());
  dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
  dropZone.addEventListener('drop', () => dropZone.classList.remove('drag-over'));
  dropZone.addEventListener('drop', async e => {
    e.preventDefault();
    const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('image/'));
    if (!files.length) return;
    try {
      const data = await apiUpload('/api/fun/upload', files);
      const f = data.files?.[0];
      if (f) _applyImage(f.path, f.url || pathToUrl(f.path));
    } catch (err) { toast(err.message, 'error'); }
  });
  imgInput.addEventListener('change', async () => {
    if (!imgInput.files?.length) return;
    try {
      const data = await apiUpload('/api/fun/upload', Array.from(imgInput.files));
      const f = data.files?.[0];
      if (f) _applyImage(f.path, f.url || pathToUrl(f.path));
    } catch (err) { toast(err.message, 'error'); }
    imgInput.value = '';
  });

  // ── Recent images strip ───────────────────────────────────────────────────
  const recentWrap = el('div', { style: 'display:none;' });
  const recentRow  = el('div', { style: 'display:flex; gap:6px; overflow-x:auto; padding:2px 0;' });
  recentWrap.appendChild(el('div', { style: 'font-size:.72rem; color:var(--text-3); margin-bottom:5px;', text: 'Recent images — click to use' }));
  recentWrap.appendChild(recentRow);
  root.appendChild(recentWrap);

  async function _loadRecent() {
    try {
      const data = await apiFetch('/api/gallery?limit=16', { context: 'express.recent' });
      const images = (data.items || []).filter(i => !/\.(mp4|webm|mov)/i.test(i.url));
      if (!images.length) return;
      recentRow.innerHTML = '';
      for (const item of images.slice(0, 12)) {
        const thumb = el('img', {
          src: item.thumbnail || item.url,
          class: 'gallery-thumb',
          title: item.prompt || 'Use this image',
        });
        thumb.addEventListener('click', () => {
          const path = item.metadata?.path || item.url;
          _applyImage(path, item.url);
        });
        recentRow.appendChild(thumb);
      }
      recentWrap.style.display = '';
    } catch (_) {}
  }
  _loadRecent();

  // ── Idea + Lyric direction ────────────────────────────────────────────────
  const ideaInput = el('textarea', {
    rows: '3',
    style: 'width:100%; resize:vertical; font-size:.95rem;',
    placeholder: 'Describe your idea, mood, or style — or leave blank to let AI decide.',
  });
  const lyricInput = el('input', {
    type: 'text',
    style: 'width:100%; font-size:.82rem; margin-top:8px;',
    placeholder: 'Lyric direction (optional) — e.g. "playful adventure, upbeat, about the dog"',
  });
  root.appendChild(el('div', { class: 'card', style: 'padding:14px;' }, [
    el('div', { style: 'font-size:.8rem; color:var(--text-3); margin-bottom:6px;', text: 'Your idea (optional)' }),
    ideaInput,
    el('div', { style: 'font-size:.78rem; color:var(--text-3); margin-top:10px; margin-bottom:2px;', text: 'Lyric direction (optional)' }),
    lyricInput,
  ]));

  // ── Create button ─────────────────────────────────────────────────────────
  const createBtn = el('button', {
    class: 'btn btn-primary btn-generate',
    text: 'Create',
    style: 'width:100%; font-size:1.15rem; padding:16px; font-weight:700; letter-spacing:.04em;',
  });
  root.appendChild(createBtn);

  // Subtle escape hatch to the full Create Videos tab
  const advLink = el('div', { style: 'text-align:center; margin-top:-10px;' });
  const advBtn  = el('button', {
    style: 'background:none; border:none; cursor:pointer; font-size:.75rem; color:var(--text-3); padding:4px 8px;',
    text: 'Want more control?  Open Create Videos →',
  });
  advLink.appendChild(advBtn);
  root.appendChild(advLink);
  advBtn.addEventListener('click', () => {
    if (_imagePath) handoff('fun-videos', { type: 'image', path: _imagePath, url: pathToUrl(_imagePath) });
    document.querySelector('.rail-tab[data-tab="fun-videos"]')?.click();
  });

  // ── Progress ──────────────────────────────────────────────────────────────
  const progressArea = el('div', { class: 'card', style: 'display:none; padding:16px;' });
  root.appendChild(progressArea);

  const progressLabel = el('div', { style: 'font-size:.85rem; color:var(--accent); margin-bottom:10px;' });
  const progressBar   = el('div', { style: 'height:4px; background:var(--border-2); border-radius:2px; overflow:hidden;' }, [
    el('div', { class: 'express-bar', style: 'height:100%; background:var(--accent); width:0%; transition:width .4s;' }),
  ]);
  const cancelBtn = el('button', { class: 'btn btn-sm', text: 'Cancel', style: 'margin-top:10px;' });
  progressArea.appendChild(progressLabel);
  progressArea.appendChild(progressBar);
  progressArea.appendChild(cancelBtn);

  let _jobId = null;
  cancelBtn.addEventListener('click', async () => {
    if (_jobId) { await stopJob(_jobId).catch(() => {}); }
    _reset();
  });

  // ── Result ────────────────────────────────────────────────────────────────
  const resultArea = el('div', { style: 'display:none;' });
  root.appendChild(resultArea);

  const resultTabBar = el('div', { class: 'result-tabs', style: 'display:none;' });
  const tabMix = el('button', { class: 'result-tab active', text: 'With music' });
  const tabRaw = el('button', { class: 'result-tab', text: 'Raw video' });
  resultTabBar.appendChild(tabMix);
  resultTabBar.appendChild(tabRaw);
  resultArea.appendChild(resultTabBar);

  const playerWrap = el('div');
  resultArea.appendChild(playerWrap);
  const player = createVideoPlayer(playerWrap);

  let _rawPath = null, _mixPath = null;

  function _showTab(which) {
    tabMix.classList.toggle('active', which === 'mix');
    tabRaw.classList.toggle('active', which === 'raw');
    const p = which === 'mix' ? _mixPath : _rawPath;
    if (p) player.show(pathToUrl(p), p);
  }
  tabMix.addEventListener('click', () => _showTab('mix'));
  tabRaw.addEventListener('click', () => _showTab('raw'));

  // Post-result action row
  const resultActions = el('div', { style: 'display:flex; gap:8px; margin-top:8px; flex-wrap:wrap;' });
  resultArea.appendChild(resultActions);

  const startOverBtn = el('button', { class: 'btn btn-sm', text: 'Start over' });
  startOverBtn.addEventListener('click', _reset);
  resultActions.appendChild(startOverBtn);

  const tweakBtn = el('button', { class: 'btn btn-sm', text: 'Tweak in Create Videos →' });
  tweakBtn.addEventListener('click', () => {
    if (_imagePath) handoff('fun-videos', { type: 'image', path: _imagePath, url: pathToUrl(_imagePath) });
    document.querySelector('.rail-tab[data-tab="fun-videos"]')?.click();
  });
  resultActions.appendChild(tweakBtn);

  function _reset() {
    _jobId = null; _imagePath = null; _rawPath = null; _mixPath = null;
    preview.style.display = 'none'; preview.src = '';
    dropHint.style.display = '';
    dropZone.classList.remove('drop-zone-loaded', 'drag-over');
    ideaInput.value = '';
    lyricInput.value = '';
    createBtn.disabled = false;
    progressArea.style.display = 'none';
    resultArea.style.display = 'none';
    resultTabBar.style.display = 'none';
    player.hide();
    _loadRecent();
  }

  // ── Create ────────────────────────────────────────────────────────────────
  createBtn.addEventListener('click', async () => {
    if (!_imagePath) {
      dropZone.style.borderColor = 'var(--red)';
      setTimeout(() => { if (!_imagePath) dropZone.style.borderColor = 'var(--border-2)'; }, 2000);
      toast('Drop an image first', 'error');
      return;
    }

    createBtn.disabled = true;
    progressArea.style.display = '';
    progressLabel.textContent = 'Submitting…';
    progressBar.querySelector('.express-bar').style.width = '5%';

    // Auto-generate a motion prompt from the image + idea if not provided
    let motionPrompt = ideaInput.value.trim();
    if (!motionPrompt) {
      try {
        progressLabel.textContent = 'Reading image…';
        const data = await api('/api/fun/generate-prompts', {
          method: 'POST',
          body: JSON.stringify({ image_path: _imagePath, num_prompts: 1, creativity: 8, max_tokens: 400 }),
        });
        const p = data.prompts?.[0];
        motionPrompt = (typeof p === 'string' ? p : p?.prompt) || 'Camera slowly pushes in, gentle movement, warm light';
      } catch (_) {
        motionPrompt = 'Camera slowly pushes in, gentle movement, warm light';
      }
    }

    progressBar.querySelector('.express-bar').style.width = '15%';
    progressLabel.textContent = 'Generating video…';

    try {
      const { job_id } = await api('/api/fun/make-it', {
        method: 'POST',
        body: JSON.stringify({
          photo_path:   _imagePath,
          video_prompt: motionPrompt,
          music_prompt: '',
          lyrics:       lyricInput.value.trim(),
          model:        'LTX-2 Dev19B Distilled',
          duration:     14,
          steps:        30,
          guidance:     7.5,
          seed:         -1,
          skip_audio:   false,
          instrumental: false,
        }),
      });
      _jobId = job_id;

      pollJob(job_id,
        j => {
          const pct = Math.max(15, Math.min(95, j.progress || 0));
          progressBar.querySelector('.express-bar').style.width = `${pct}%`;
          progressLabel.textContent = j.message || (j.status === 'queued' ? 'Queued — waiting for GPU…' : 'Working…');
        },
        j => {
          progressArea.style.display = 'none';
          createBtn.disabled = false;
          if (j.output) {
            const outputs = Array.isArray(j.output) ? j.output : [j.output];
            _rawPath = outputs[0];
            _mixPath = outputs.length > 1 ? outputs[1] : null;
            const best = _mixPath || _rawPath;
            resultArea.style.display = '';
            if (_mixPath) {
              resultTabBar.style.display = 'flex';
              _showTab('mix');
            } else {
              player.show(pathToUrl(_rawPath), _rawPath);
            }
            pushToGallery('express', best, motionPrompt, null, {});
            toast('Done!', 'success');
          }
        },
        err => {
          progressArea.style.display = 'none';
          createBtn.disabled = false;
          toast(err, 'error');
        },
      );
    } catch (e) {
      progressArea.style.display = 'none';
      createBtn.disabled = false;
      toast(e.message, 'error');
    }
  });
}
