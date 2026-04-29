/**
 * Queue tab — live view of running, pending, and recent GPU jobs.
 * Polls /api/jobs every 2s. Shows thumbnail, prompt, progress bar,
 * and cancel button for each job.
 */
import { api } from './api.js?v=20260414';
import { toast } from './shell/toast.js?v=20260429d';
import { el, pathToUrl } from './components.js?v=20260429b';

let _root = null;
let _pollTimer = null;
let _knownIds = new Set();    // jobs we've already seen complete → dispatched gallery refresh
let _clearedIds = new Set();  // completed job IDs the user has dismissed

// ── Public ────────────────────────────────────────────────────────────────────

export function init(panel) {
  _root = panel;
  _root.innerHTML = '';
  _root.style.cssText = 'padding:16px; display:flex; flex-direction:column; gap:12px;';
  _render({ running: [], queued: [], completed: [] });
  _startPoll();
}

export function pause()  { _stopPoll(); }
export function resume() { _startPoll(); }

// Called from the sidebar job feed so clicking a mini-card opens the detail modal.
export function openJobModal(job) { _showModal(job); }

// ── Polling ───────────────────────────────────────────────────────────────────

function _startPoll() {
  if (_pollTimer) return;
  _poll();
  _pollTimer = setInterval(_poll, 2000);
}

function _stopPoll() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

async function _poll() {
  try {
    const data = await api('/api/jobs');
    _render(data);
    _updateRailHint(data);
    _notifyCompletions(data);
    _updateClearBtn(data);
  } catch (_) {}
}

function _updateRailHint(data) {
  const hint = document.getElementById('queue-rail-hint');
  if (!hint) return;
  const n = (data.running?.length || 0) + (data.queued?.length || 0);
  if (n === 0)      hint.textContent = '(no items processing)';
  else if (n === 1) hint.textContent = '(1 item processing)';
  else              hint.textContent = `(${n} items processing)`;
}

function _updateClearBtn(data) {
  const bar = document.getElementById('queue-clear-bar');
  if (!bar) return;
  const visible = (data.completed || []).some(j => !_clearedIds.has(j.id));
  bar.classList.toggle('visible', visible);
}

function _notifyCompletions(data) {
  const done = data.completed || [];
  for (const job of done) {
    if (!_knownIds.has(job.id) && job.status === 'done' && job.output) {
      _knownIds.add(job.id);
      document.dispatchEvent(new CustomEvent('session-updated'));
    }
  }
  if (_knownIds.size > 200) {
    const arr = [..._knownIds];
    _knownIds = new Set(arr.slice(arr.length - 100));
  }
}

// ── Clear completed ────────────────────────────────────────────────────────────
// Wire up immediately — ES modules execute after DOM is parsed, so no DOMContentLoaded needed.
{
  const btn = document.getElementById('btn-clear-completed');
  if (btn) {
    btn.addEventListener('click', () => {
      if (_root) {
        _root.querySelectorAll('[data-job-id][data-done]')
          .forEach(c => _clearedIds.add(c.dataset.jobId));
      }
      _poll();
    });
  }
}

// ── Rendering ─────────────────────────────────────────────────────────────────

function _render(data) {
  if (!_root) return;
  const running   = data.running   || [];
  const queued    = data.queued    || [];
  const completed = (data.completed || [])
    .filter(j => !_clearedIds.has(j.id))
    .slice(0, 12);
  const total = running.length + queued.length;

  _root.innerHTML = '';

  if (total === 0 && completed.length === 0) return;

  if (total > 0) {
    _root.appendChild(_sectionHead(`Active  ·  ${total} job${total !== 1 ? 's' : ''}`));
    for (const job of [...running, ...queued]) {
      _root.appendChild(_jobCard(job, true));
    }
  }

  if (completed.length > 0) {
    _root.appendChild(_sectionHead('Completed — click to open'));
    for (const job of completed) {
      _root.appendChild(_jobCard(job, false));
    }
  }
}

function _sectionHead(text) {
  return el('div', {
    style: 'font-size:.7rem; text-transform:uppercase; letter-spacing:.08em; color:var(--text-3); padding:4px 0 2px;',
    text,
  });
}

function _bestOutput(job) {
  if (!job.output) return null;
  const outputs = Array.isArray(job.output) ? job.output : [job.output];
  return outputs.length > 1 ? outputs[1] : outputs[0];
}

function _showModal(job) {
  document.getElementById('queue-job-modal')?.remove();

  const isDone   = job.status === 'done';
  const isActive = job.status === 'running' || job.status === 'queued';

  const overlay = el('div', {
    id: 'queue-job-modal',
    style: 'position:fixed; inset:0; z-index:9000; background:rgba(0,0,0,.82); display:flex; align-items:center; justify-content:center;',
  });
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

  const box = el('div', {
    style: 'background:var(--surface-1); border-radius:12px; padding:20px; max-width:min(860px,90vw); width:100%; display:flex; flex-direction:column; gap:14px; max-height:90vh; overflow:auto;',
  });

  // Header row
  const hdr = el('div', { style: 'display:flex; align-items:center; gap:10px;' });
  hdr.appendChild(el('span', { style: 'font-size:.95rem; font-weight:700; flex:1; color:var(--text-1);', text: job.label || job.type }));
  hdr.appendChild(_statusChip(job.status, job.queue_position));
  const closeX = el('button', { class: 'btn btn-sm', text: '✕', style: 'padding:2px 8px;' });
  closeX.addEventListener('click', () => overlay.remove());
  hdr.appendChild(closeX);
  box.appendChild(hdr);

  // Video player for completed jobs
  if (isDone) {
    const out = _bestOutput(job);
    const url = out ? pathToUrl(out) : null;
    if (url) {
      const vid = document.createElement('video');
      vid.controls = true;
      vid.autoplay = true;
      vid.style.cssText = 'width:100%; max-height:60vh; border-radius:8px; background:#000;';
      vid.src = url;
      box.appendChild(vid);
    } else {
      box.appendChild(el('div', { style: 'color:var(--text-3); font-size:.85rem;', text: 'No output file recorded.' }));
    }
  }

  // Progress bar for running jobs
  if (isActive) {
    const pct = job.progress || 0;
    const barWrap = el('div', { style: 'height:8px; background:var(--surface-3); border-radius:4px; overflow:hidden;' });
    const bar     = el('div', { style: `height:100%; background:var(--accent); width:${pct}%; border-radius:4px; transition:width .5s;` });
    barWrap.appendChild(bar);
    box.appendChild(barWrap);
  }

  // Current message / step
  if (job.message) {
    box.appendChild(el('div', {
      style: 'font-size:.85rem; color:var(--text-2); padding:8px 10px; background:var(--surface-2); border-radius:6px;',
      text: job.message,
    }));
  }

  // Prompt
  if (job.meta?.prompt) {
    const p = el('div', { style: 'display:flex; flex-direction:column; gap:4px;' });
    p.appendChild(el('span', { style: 'font-size:.72rem; text-transform:uppercase; letter-spacing:.06em; color:var(--text-3);', text: 'Prompt' }));
    p.appendChild(el('span', { style: 'font-size:.82rem; color:var(--text-2);', text: job.meta.prompt }));
    box.appendChild(p);
  }

  // Source image thumbnail for active jobs
  if (isActive && job.meta?.source_image) {
    const src = `/api/thumbnail?path=${encodeURIComponent(job.meta.source_image)}&size=240`;
    const img = el('img', { style: 'max-width:160px; border-radius:6px; align-self:flex-start;' });
    img.src = src;
    img.onerror = () => img.remove();
    box.appendChild(img);
  }

  overlay.appendChild(box);
  document.body.appendChild(overlay);
}

function _jobCard(job, active) {
  const isRunning = job.status === 'running';
  const isFailed  = job.status === 'error' || job.status === 'stopped';
  const isDone    = job.status === 'done';
  const isClickable = isDone || isRunning || job.status === 'queued';

  const card = el('div', {
    style: `display:flex; gap:12px; align-items:flex-start; background:var(--surface-2);
            border:1px solid var(--border-2); border-radius:8px; padding:10px 12px;
            opacity:${isFailed ? '.5' : '1'};
            ${isClickable ? 'cursor:pointer; transition:border-color .15s, background .15s;' : ''}`,
  });
  if (isClickable) {
    card.dataset.jobId = job.id;
    if (isDone) card.dataset.done = '1';
    card.addEventListener('click', e => {
      if (e.target.closest('button')) return; // don't open modal when clicking Cancel
      _showModal(job);
    });
    card.addEventListener('mouseenter', () => {
      card.style.borderColor = 'var(--accent)';
      card.style.background  = 'var(--surface-3, rgba(255,255,255,.07))';
    });
    card.addEventListener('mouseleave', () => {
      card.style.borderColor = 'var(--border-2)';
      card.style.background  = 'var(--surface-2)';
    });
  }

  // Thumbnail — for done jobs, prefer output frame; fall back to source image
  const thumb = el('div', {
    style: `width:72px; height:48px; flex-shrink:0; border-radius:5px;
            background:var(--surface-3); overflow:hidden; display:flex;
            align-items:center; justify-content:center;`,
  });

  const srcImg = job.meta?.source_image;
  const outPath = isDone ? _bestOutput(job) : null;
  const isVideoOut = outPath && /\.(mp4|webm|mov|avi|mkv)$/i.test(outPath);
  const thumbSrc = (!isVideoOut && outPath)
    ? `/api/thumbnail?path=${encodeURIComponent(outPath)}&size=120`
    : srcImg
      ? `/api/thumbnail?path=${encodeURIComponent(srcImg)}&size=120`
      : null;

  if (thumbSrc) {
    const img = el('img', { style: 'width:100%; height:100%; object-fit:cover; border-radius:5px;' });
    img.src = thumbSrc;
    img.onerror = () => {
      img.style.display = 'none';
      thumb.appendChild(el('span', { style: 'font-size:1.4rem;', text: '🎬' }));
    };
    thumb.appendChild(img);
  } else {
    thumb.appendChild(el('span', { style: 'font-size:1.4rem;', text: '🎬' }));
  }
  card.appendChild(thumb);

  // Body
  const body = el('div', { style: 'flex:1; min-width:0; display:flex; flex-direction:column; gap:5px;' });

  const top = el('div', { style: 'display:flex; align-items:center; gap:8px; flex-wrap:wrap;' });
  top.appendChild(el('div', {
    style: 'font-size:.82rem; font-weight:600; color:var(--text-1); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:180px;',
    text: job.label || job.type,
  }));
  top.appendChild(_statusChip(job.status, job.queue_position));
  body.appendChild(top);

  const prompt = job.meta?.prompt;
  if (prompt) {
    body.appendChild(el('div', {
      style: 'font-size:.75rem; color:var(--text-3); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;',
      text: prompt,
    }));
  }

  if (isRunning) {
    const barWrap = el('div', { style: 'height:4px; background:var(--surface-3); border-radius:2px; overflow:hidden; margin-top:2px;' });
    const bar     = el('div', { style: `height:100%; border-radius:2px; background:var(--accent); width:${job.progress || 0}%; transition:width .5s ease;` });
    barWrap.appendChild(bar);
    body.appendChild(barWrap);
    if (job.message) body.appendChild(el('div', { style: 'font-size:.72rem; color:var(--text-3);', text: job.message }));
  }

  if (isDone) {
    body.appendChild(el('div', {
      style: 'font-size:.72rem; color:var(--accent); opacity:.8;',
      text: '▶ click to watch',
    }));
  }
  if (job.status === 'queued') {
    body.appendChild(el('div', {
      style: 'font-size:.72rem; color:var(--text-3); opacity:.7;',
      text: 'click for details',
    }));
  }

  card.appendChild(body);

  if (active) {
    const cancelBtn = el('button', {
      class: 'btn btn-sm', text: '✕', title: 'Cancel',
      style: 'flex-shrink:0; padding:4px 8px; font-size:.8rem;',
    });
    cancelBtn.addEventListener('click', async e => {
      e.stopPropagation();
      cancelBtn.disabled = true;
      try {
        await api(`/api/jobs/${job.id}/stop`, { method: 'POST' });
        toast('Job canceled', 'info');
        card.style.opacity = '.4';
      } catch (err) {
        toast(err.message, 'error');
        cancelBtn.disabled = false;
      }
    });
    card.appendChild(cancelBtn);
  }

  return card;
}

function _statusChip(status, queuePos) {
  const map = {
    running:   ['Running',  'var(--accent)'],
    queued:    ['Queued',   'var(--text-3)'],
    done:      ['Done',     '#4caf50'],
    error:     ['Failed',   'var(--red, #c41e3a)'],
    stopped:   ['Canceled', 'var(--text-3)'],
    cancelled: ['Canceled', 'var(--text-3)'],
  };
  const [label, color] = map[status] || ['Unknown', 'var(--text-3)'];
  const display = (status === 'queued' && queuePos != null) ? `#${queuePos + 1} in queue` : label;
  return el('span', {
    style: `font-size:.68rem; padding:2px 6px; border-radius:4px; border:1px solid ${color}; color:${color}; white-space:nowrap;`,
    text: display,
  });
}
