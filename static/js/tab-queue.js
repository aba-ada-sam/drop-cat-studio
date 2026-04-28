/**
 * Queue tab — live view of running, pending, and recent GPU jobs.
 * Polls /api/jobs every 2s. Shows thumbnail, prompt, progress bar,
 * and cancel button for each job.
 */
import { api } from './api.js?v=20260414';
import { toast } from './shell/toast.js?v=20260421c';
import { el } from './components.js?v=20260421a';

let _root = null;
let _pollTimer = null;
let _knownIds = new Set();   // jobs we've already seen complete → dispatched gallery refresh

// ── Public ────────────────────────────────────────────────────────────────────

export function init(panel) {
  _root = panel;
  _root.innerHTML = '';
  _root.style.cssText = 'padding:16px; display:flex; flex-direction:column; gap:12px;';
  _render({ running: [], queued: [], completed: [] });
  _startPoll();
}

// Called by app.js when the tab is navigated away from — pause polling
export function pause() { _stopPoll(); }
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
  } catch (_) {}
}

function _updateRailHint(data) {
  const hint = document.getElementById('queue-rail-hint');
  if (!hint) return;
  const running = data.running?.length || 0;
  const queued  = data.queued?.length  || 0;
  if (running)       hint.textContent = `${running} running${queued ? `, ${queued} waiting` : ''}`;
  else if (queued)   hint.textContent = `${queued} waiting`;
  else               hint.textContent = 'Idle';
}

function _notifyCompletions(data) {
  const done = data.completed || [];
  for (const job of done) {
    if (!_knownIds.has(job.id) && job.status === 'done' && job.output) {
      _knownIds.add(job.id);
      // Signal gallery to refresh (same event pollJob dispatches)
      document.dispatchEvent(new CustomEvent('session-updated'));
    }
  }
  // Cap set size so it doesn't grow forever
  if (_knownIds.size > 200) {
    const arr = [..._knownIds];
    _knownIds = new Set(arr.slice(arr.length - 100));
  }
}

// ── Rendering ─────────────────────────────────────────────────────────────────

function _render(data) {
  if (!_root) return;
  const running   = data.running   || [];
  const queued    = data.queued    || [];
  const completed = (data.completed || []).slice(0, 12);
  const total = running.length + queued.length;

  _root.innerHTML = '';

  if (total === 0 && completed.length === 0) {
    return;
  }

  if (total > 0) {
    _root.appendChild(_sectionHead(`Active  ·  ${total} job${total !== 1 ? 's' : ''}`));
    for (const job of [...running, ...queued]) {
      _root.appendChild(_jobCard(job, true));
    }
  }

  if (completed.length > 0) {
    _root.appendChild(_sectionHead('Recent'));
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

function _jobCard(job, active) {
  const isRunning = job.status === 'running';
  const isFailed  = job.status === 'error' || job.status === 'stopped';
  const isDone    = job.status === 'done';

  const card = el('div', {
    style: `display:flex; gap:12px; align-items:flex-start; background:var(--surface-2);
            border:1px solid var(--border-2); border-radius:8px; padding:10px 12px;
            opacity:${isFailed ? '.5' : '1'};`,
  });

  // Thumbnail
  const thumb = el('div', {
    style: `width:72px; height:48px; flex-shrink:0; border-radius:5px;
            background:var(--surface-3); overflow:hidden; display:flex;
            align-items:center; justify-content:center;`,
  });
  const srcImg = job.meta?.source_image;
  if (srcImg) {
    const img = el('img', {
      style: 'width:100%; height:100%; object-fit:cover; border-radius:5px;',
    });
    img.src = `/api/thumbnail?path=${encodeURIComponent(srcImg)}&size=120`;
    img.onerror = () => { img.style.display = 'none'; };
    thumb.appendChild(img);
  } else {
    thumb.appendChild(el('span', { style: 'font-size:1.4rem;', text: '🎬' }));
  }
  card.appendChild(thumb);

  // Body
  const body = el('div', { style: 'flex:1; min-width:0; display:flex; flex-direction:column; gap:5px;' });

  // Label + status chip
  const top = el('div', { style: 'display:flex; align-items:center; gap:8px; flex-wrap:wrap;' });
  top.appendChild(el('div', {
    style: 'font-size:.82rem; font-weight:600; color:var(--text-1); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:180px;',
    text: job.label || job.type,
  }));
  top.appendChild(_statusChip(job.status, job.queue_position));
  body.appendChild(top);

  // Prompt preview
  const prompt = job.meta?.prompt;
  if (prompt) {
    body.appendChild(el('div', {
      style: 'font-size:.75rem; color:var(--text-3); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;',
      text: prompt,
    }));
  }

  // Progress bar (running only)
  if (isRunning) {
    const barWrap = el('div', {
      style: 'height:4px; background:var(--surface-3); border-radius:2px; overflow:hidden; margin-top:2px;',
    });
    const bar = el('div', {
      style: `height:100%; border-radius:2px; background:var(--accent);
              width:${job.progress || 2}%; transition:width .5s ease;`,
    });
    barWrap.appendChild(bar);
    body.appendChild(barWrap);

    if (job.message) {
      body.appendChild(el('div', {
        style: 'font-size:.72rem; color:var(--text-3);',
        text: job.message,
      }));
    }
  }

  // Done — show output link
  if (isDone && job.output) {
    const outPath = Array.isArray(job.output) ? job.output[0] : job.output;
    const link = el('a', {
      style: 'font-size:.75rem; color:var(--accent); cursor:pointer;',
      text: '→ View in Gallery',
    });
    link.href = '#';
    link.addEventListener('click', e => {
      e.preventDefault();
      document.querySelector('#btn-gallery-rail')?.click();
    });
    body.appendChild(link);
  }

  card.appendChild(body);

  // Cancel button (active jobs only)
  if (active) {
    const cancelBtn = el('button', {
      class: 'btn btn-sm',
      text: '✕',
      title: 'Cancel',
      style: 'flex-shrink:0; padding:4px 8px; font-size:.8rem;',
    });
    cancelBtn.addEventListener('click', async () => {
      cancelBtn.disabled = true;
      try {
        await api(`/api/jobs/${job.id}/stop`, { method: 'POST' });
        toast('Job canceled', 'info');
        card.style.opacity = '.4';
      } catch (e) {
        toast(e.message, 'error');
        cancelBtn.disabled = false;
      }
    });
    card.appendChild(cancelBtn);
  }

  return card;
}

function _statusChip(status, queuePos) {
  const map = {
    running:   ['Running',   'var(--accent)'],
    queued:    ['Queued',    'var(--text-3)'],
    done:      ['Done',      '#4caf50'],
    error:     ['Failed',    'var(--red, #c41e3a)'],
    stopped:   ['Canceled',  'var(--text-3)'],
    cancelled: ['Canceled',  'var(--text-3)'],
  };
  const [label, color] = map[status] || ['Unknown', 'var(--text-3)'];
  const display = (status === 'queued' && queuePos != null)
    ? `#${queuePos + 1} in queue`
    : label;
  return el('span', {
    style: `font-size:.68rem; padding:2px 6px; border-radius:4px;
            border:1px solid ${color}; color:${color}; white-space:nowrap;`,
    text: display,
  });
}
