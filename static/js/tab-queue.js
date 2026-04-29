/**
 * Queue tab — live view of running, pending, and recent GPU jobs.
 * Polls /api/jobs every 2s. Shows thumbnail, prompt, progress bar,
 * and cancel button for each job.
 */
import { api } from './api.js?v=20260414';
import { toast } from './shell/toast.js?v=20260421c';
import { el, pathToUrl } from './components.js?v=20260421a';

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

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('btn-clear-completed')?.addEventListener('click', () => {
    // Mark all currently visible completed jobs as cleared
    if (_root) {
      const cards = _root.querySelectorAll('[data-job-id][data-done]');
      cards.forEach(c => _clearedIds.add(c.dataset.jobId));
    }
    // Force immediate re-render
    _poll();
  });
});

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

function _openOutput(job) {
  const out = _bestOutput(job);
  if (!out) { toast('No output file', 'info'); return; }
  const url = pathToUrl(out);
  if (!url) { toast('Could not resolve path', 'error'); return; }
  window.open(url, '_blank');
}

function _jobCard(job, active) {
  const isRunning = job.status === 'running';
  const isFailed  = job.status === 'error' || job.status === 'stopped';
  const isDone    = job.status === 'done';

  const card = el('div', {
    style: `display:flex; gap:12px; align-items:flex-start; background:var(--surface-2);
            border:1px solid var(--border-2); border-radius:8px; padding:10px 12px;
            opacity:${isFailed ? '.5' : '1'};
            ${isDone ? 'cursor:pointer; transition:border-color .15s, background .15s;' : ''}`,
  });
  if (isDone) {
    card.dataset.jobId = job.id;
    card.dataset.done  = '1';
    card.addEventListener('click', () => _openOutput(job));
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
  const thumbSrc = outPath
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
    const bar     = el('div', { style: `height:100%; border-radius:2px; background:var(--accent); width:${job.progress || 2}%; transition:width .5s ease;` });
    barWrap.appendChild(bar);
    body.appendChild(barWrap);
    if (job.message) body.appendChild(el('div', { style: 'font-size:.72rem; color:var(--text-3);', text: job.message }));
  }

  if (isDone) {
    body.appendChild(el('div', {
      style: 'font-size:.72rem; color:var(--accent); opacity:.8;',
      text: '↗ click to open',
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
