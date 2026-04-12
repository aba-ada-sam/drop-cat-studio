/**
 * Drop Cat Go Studio — Image to Video panel.
 * Ken Burns slideshow generator with drag-to-reorder images.
 */
import { api, apiUpload, pollJob, stopJob } from './api.js';
import { toast, createDropZone, createProgressCard, createVideoPlayer, createSlider, createSelect, el } from './components.js';

let images = [];

export function init(panel) {
  panel.innerHTML = '';
  const layout = el('div', { class: 'two-panel' });
  panel.appendChild(layout);

  // ── Left: Images ──────────────────────────────────────────────────────
  const left = el('div', { class: 'card' });
  layout.appendChild(left);
  left.appendChild(el('h3', { text: 'Images' }));

  createDropZone(left, {
    accept: 'image/*',
    label: 'Drop images here or click to browse',
    async onFiles(files) {
      try {
        const data = await apiUpload('/api/i2v/upload', files);
        for (const img of data.images || []) {
          images.push({ ...img, motion: 'random' });
        }
        renderList();
        toast(`${files.length} image(s) added`, 'success');
      } catch (e) { toast(e.message, 'error'); }
    },
  });

  const imageList = el('div', { class: 'file-list' });
  left.appendChild(imageList);

  function renderList() {
    imageList.innerHTML = '';
    images.forEach((img, i) => {
      const item = el('div', { class: 'file-item' }, [
        el('span', { class: 'name', text: img.name || `Image ${i + 1}` }),
        el('select', { class: 'meta', onchange(e) { img.motion = e.target.value; } },
          ['random', 'zoom_in', 'zoom_out', 'still'].map(m =>
            el('option', { value: m, text: m, ...(m === img.motion ? { selected: 'true' } : {}) })
          )
        ),
        el('button', { class: 'remove', text: '✕', onclick() { images.splice(i, 1); renderList(); } }),
      ]);
      imageList.appendChild(item);
    });
  }

  // ── Right: Settings + Generate ────────────────────────────────────────
  const right = el('div');
  layout.appendChild(right);

  const settingsCard = el('div', { class: 'card' });
  right.appendChild(settingsCard);
  settingsCard.appendChild(el('h3', { text: 'Settings' }));

  const zoom = createSlider(settingsCard, { label: 'Ken Burns Zoom', min: 0, max: 20, step: 1, value: 5, unit: '%' });
  const dur = createSlider(settingsCard, { label: 'Duration per Image', min: 1, max: 10, step: 0.5, value: 3, unit: 's' });
  const fade = createSlider(settingsCard, { label: 'Crossfade', min: 0, max: 2, step: 0.1, value: 0.5, unit: 's' });
  const res = createSelect(settingsCard, { label: 'Resolution', options: ['1280x720', '1920x1080', '854x480'], value: '1280x720' });
  const aspect = createSelect(settingsCard, { label: 'Aspect Mode', options: ['auto', 'fixed', 'source'], value: 'auto' });
  const fit = createSelect(settingsCard, { label: 'Fit Mode', options: ['contain', 'cover'], value: 'contain' });
  const mode = createSelect(settingsCard, { label: 'Output Mode', options: ['combined', 'separate'], value: 'combined' });

  const genBtn = el('button', { class: 'btn btn-primary', text: 'Generate Video', style: 'margin-top:12px' });
  settingsCard.appendChild(genBtn);

  const progress = createProgressCard(right);
  const player = createVideoPlayer(right);

  genBtn.addEventListener('click', async () => {
    if (!images.length) { toast('Add images first', 'error'); return; }
    genBtn.disabled = true;
    progress.show();
    player.hide();

    try {
      const data = await api('/api/i2v/generate', {
        method: 'POST',
        body: JSON.stringify({
          images: images.map(img => ({
            path: img.path,
            name: img.name,
            width: img.width,
            height: img.height,
            motion: img.motion,
          })),
          settings: {
            ken_burns_zoom: zoom.value,
            img_dur: dur.value,
            fade_dur: fade.value,
            output_res: res.value,
            aspect_mode: aspect.value,
            fit_mode: fit.value,
            output_mode: mode.value,
          },
        }),
      });

      progress.onCancel(async () => { await stopJob(data.job_id); genBtn.disabled = false; });
      pollJob(data.job_id,
        job => progress.update(job.progress, job.message),
        job => { progress.hide(); genBtn.disabled = false; if (job.output) player.show(`/output/${job.output}`); toast('Video created!', 'success'); },
        err => { progress.hide(); genBtn.disabled = false; toast(err, 'error'); },
      );
    } catch (e) { progress.hide(); genBtn.disabled = false; toast(e.message, 'error'); }
  });

  player.onStartOver(() => { player.hide(); images = []; renderList(); });
}
