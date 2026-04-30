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
  const nr = data.running?.length || 0;
  const nq = data.queued?.length  || 0;
  if (nr === 0 && nq === 0)    hint.textContent = '(idle)';
  else if (nr > 0 && nq === 0) hint.textContent = '(generating)';
  else if (nr > 0)             hint.textContent = `(generating + ${nq} waiting)`;
  else                         hint.textContent = `(${nq} queued)`;
}

function _updateClearBtn(data) {
  const bar = document.getElementById('queue-clear-bar');
  if (!bar) return;
  // Include failed jobs in the "anything to clear?" check
  const all = [...(data.completed || []), ...(data.running || []).filter(j => j.status === 'error'), ...(data.queued || []).filter(j => j.status === 'error')];
  const visible = all.some(j => !_clearedIds.has(j.id));
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
{
  const btn = document.getElementById('btn-clear-completed');
  if (btn) {
    btn.addEventListener('click', async () => {
      await api('/api/jobs', { method: 'DELETE' }).catch(() => {});
      _clearedIds.clear();
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

  _root.innerHTML = '';

  if (running.length === 0 && queued.length === 0 && completed.length === 0) return;

  // ── Now Generating (max 1) ─────────────────────────────────────────────
  if (running.length > 0) {
    _root.appendChild(_sectionHead('▶  Now Generating'));
    for (const job of running) {
      _root.appendChild(_jobCard(job, true));
    }
  }

  // ── In Queue ───────────────────────────────────────────────────────────
  if (queued.length > 0) {
    _root.appendChild(_sectionHead(
      `⋯  In Queue · ${queued.length} waiting${running.length === 0 ? ' (no job running yet)' : ''}`,
    ));
    for (const job of queued) {
      _root.appendChild(_jobCard(job, true));
    }
  }

  // ── Completed / Failed ─────────────────────────────────────────────────
  if (completed.length > 0) {
    const nFailed = completed.filter(j => j.status !== 'done').length;
    const label = nFailed > 0
      ? `Completed · ${nFailed} failed — ✕ to dismiss`
      : 'Completed — click to open';
    _root.appendChild(_sectionHead(label));
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
  const isRunning   = job.status === 'running';
  const isFailed    = job.status === 'error' || job.status === 'stopped' || job.status === 'cancelled';
  const isDone      = job.status === 'done';
  const isClearable = isDone || isFailed;
  const isClickable = isDone || isRunning || job.status === 'queued';
  const borderColor = isFailed ? 'var(--red, #c41e3a)' : 'var(--border-2)';

  const card = el('div', {
    style: `display:flex; gap:12px; align-items:flex-start; background:var(--surface-2);
            border:1px solid ${borderColor}; border-radius:8px; padding:10px 12px;
            ${isClickable ? 'cursor:pointer; transition:border-color .15s, background .15s;' : ''}`,
  });
  card.dataset.jobId = job.id;
  if (isClearable) card.dataset.clearable = '1';
  if (isDone)      card.dataset.done      = '1';

  if (isClickable) {
    card.addEventListener('click', e => {
      if (e.target.closest('button')) return;
      _showModal(job);
    });
    card.addEventListener('mouseenter', () => {
      card.style.borderColor = isDone ? 'var(--accent)' : 'var(--red, #c41e3a)';
      card.style.background  = 'var(--surface-3, rgba(255,255,255,.07))';
    });
    card.addEventListener('mouseleave', () => {
      card.style.borderColor = borderColor;
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
      text: 'waiting in queue…',
    }));
  }
  if (isFailed && (job.error || job.message)) {
    const errText = (job.error || job.message || '').split(';')[0].trim().slice(0, 120);
    body.appendChild(el('div', {
      style: 'font-size:.72rem; color:var(--red, #c41e3a); margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;',
      text: errText,
      title: job.error || job.message,
    }));
  }

  card.appendChild(body);

  // Right-side action button
  if (isClearable) {
    // X to dismiss finished/failed jobs — calls server so it survives page refresh
    const xBtn = el('button', {
      class: 'btn btn-sm', text: '✕', title: 'Dismiss',
      style: 'flex-shrink:0; padding:4px 8px; font-size:.8rem; opacity:.6;',
    });
    xBtn.addEventListener('click', async e => {
      e.stopPropagation();
      xBtn.disabled = true;
      await api(`/api/jobs/${job.id}`, { method: 'DELETE' }).catch(() => {});
      card.remove();
    });
    card.appendChild(xBtn);
  } else if (active) {
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
