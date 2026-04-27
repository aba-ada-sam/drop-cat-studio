/**
 * Drop Cat Go Studio — Express mode.
 * Drop an image, describe your idea, click Create. Everything else is automatic.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js?v=20260414';
import { createVideoPlayer, el, pathToUrl } from './components.js?v=20260426c';
import { toast } from './shell/toast.js?v=20260421c';
import { pushFromTab as pushToGallery } from './shell/gallery.js?v=20260426g';

export function init(panel) {
  panel.innerHTML = '';
  const root = el('div', { style: 'max-width:680px; margin:0 auto; padding:24px 16px; display:flex; flex-direction:column; gap:20px;' });
  panel.appendChild(root);

  // ── Heading ───────────────────────────────────────────────────────────────
  root.appendChild(el('div', { style: 'text-align:center; padding-bottom:8px;' }, [
    el('div', { style: 'font-size:1.4rem; font-weight:700; color:var(--text); margin-bottom:6px;', text: 'Create a video' }),
    el('div', { style: 'font-size:.85rem; color:var(--text-3);', text: 'Drop an image, describe what you want, click Create.' }),
  ]));

  // ── Image drop zone ───────────────────────────────────────────────────────
  const imgInput = el('input', { type: 'file', accept: 'image/*', style: 'display:none' });
  root.appendChild(imgInput);

  let _imagePath = null;
  const preview = el('img', { style: 'display:none; width:100%; max-height:260px; object-fit:contain; border-radius:8px; background:var(--bg-raised);' });
  const dropZone = el('div', {
    style: 'border:2px dashed var(--border-2); border-radius:10px; padding:40px 20px; text-align:center; cursor:pointer; transition:border-color .15s; position:relative;',
  }, [
    preview,
    el('div', { class: 'drop-hint', style: 'color:var(--text-3); font-size:.88rem;', text: 'Drop an image here or click to browse' }),
  ]);
  root.appendChild(dropZone);

  function _applyImage(path, url) {
    _imagePath = path;
    preview.src = url;
    preview.style.display = '';
    dropZone.querySelector('.drop-hint').style.display = 'none';
    dropZone.style.borderColor = 'var(--accent)';
    dropZone.style.padding = '8px';
  }

  dropZone.addEventListener('click', () => imgInput.click());
  dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.style.borderColor = 'var(--accent)'; });
  dropZone.addEventListener('dragleave', () => { if (!_imagePath) dropZone.style.borderColor = 'var(--border-2)'; });
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

  // ── Idea input ────────────────────────────────────────────────────────────
  const ideaInput = el('textarea', {
    rows: '3',
    style: 'width:100%; resize:vertical; font-size:.95rem;',
    placeholder: 'Describe your idea, mood, or style — or leave blank to let AI decide.',
  });
  root.appendChild(el('div', { class: 'card', style: 'padding:14px;' }, [
    el('div', { style: 'font-size:.8rem; color:var(--text-3); margin-bottom:6px;', text: 'Your idea (optional)' }),
    ideaInput,
  ]));

  // ── Create button ─────────────────────────────────────────────────────────
  const createBtn = el('button', {
    class: 'btn btn-primary btn-generate',
    text: 'Create',
    style: 'width:100%; font-size:1.15rem; padding:16px; font-weight:700; letter-spacing:.04em;',
  });
  root.appendChild(createBtn);

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

  const startOverBtn = el('button', { class: 'btn btn-sm', text: 'Start over', style: 'margin-top:8px;' });
  resultArea.appendChild(startOverBtn);
  startOverBtn.addEventListener('click', _reset);

  function _reset() {
    _jobId = null; _imagePath = null; _rawPath = null; _mixPath = null;
    preview.style.display = 'none'; preview.src = '';
    dropZone.querySelector('.drop-hint').style.display = '';
    dropZone.style.borderColor = 'var(--border-2)';
    dropZone.style.padding = '40px 20px';
    ideaInput.value = '';
    createBtn.disabled = false;
    progressArea.style.display = 'none';
    resultArea.style.display = 'none';
    resultTabBar.style.display = 'none';
    player.hide();
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

    // Auto-generate a prompt from the image + idea
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
          photo_path:  _imagePath,
          video_prompt: motionPrompt,
          music_prompt: '',
          model:        'LTX-2 Dev19B Distilled',
          duration:     14,
          steps:        30,
          guidance:     7.5,
          seed:         -1,
          skip_audio:   false,
          instrumental: true,
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
            pushToGallery('fun-videos', best, motionPrompt, null, {});
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
