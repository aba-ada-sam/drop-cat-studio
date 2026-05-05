/**
 * Queue tab -- GPU job queue with full user control.
 * Pause/resume, cancel, retry, promote, dismiss, clear all.
 */
import { api } from './api.js?v=20260505e';
import { toast } from './shell/toast.js?v=20260503a';
import { el, pathToUrl } from './components.js?v=20260429b';

let _root        = null;
let _pollTimer   = null;
let _knownIds    = new Set();
let _paused      = false;
let _lastData    = { running: [], queued: [], completed: [] };

// -- Public --------------------------------------------------------------------

export function init(panel) {
  _root = panel;
  _root.innerHTML = '';
  _root.style.cssText = 'display:flex; flex-direction:column; height:100%; overflow:hidden;';
  _injectStyles();
  _buildShell();
  _poll();
  _startPoll();
}

function _injectStyles() {
  if (document.getElementById('queue-modal-styles')) return;
  const s = document.createElement('style');
  s.id = 'queue-modal-styles';
  s.textContent = `
    @keyframes dcs-pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50%      { opacity: .35; transform: scale(.7); }
    }
  `;
  document.head.appendChild(s);
}

export function pause()  { _stopPoll(); }
export function resume() { _startPoll(); }
export function openJobModal(job) { _showModal(job); }

// -- Shell (toolbar + list area, built once) -----------------------------------

function _buildShell() {
  // -- Toolbar --
  const toolbar = el('div', {
    style: 'display:flex; align-items:center; gap:8px; padding:12px 16px; flex-shrink:0; border-bottom:1px solid var(--border-2);',
  });

  const pauseBtn = el('button', {
    id: 'queue-pause-btn',
    class: 'btn btn-sm',
    text: '⏸ Pause',
    title: 'Finish current job then hold — no new jobs will start',
  });
  pauseBtn.addEventListener('click', async () => {
    if (_paused) {
      const r = await api('/api/jobs/resume', { method: 'POST' }).catch(() => null);
      if (r === null) { toast('Failed to resume queue', 'error'); return; }
      _paused = false;
    } else {
      const r = await api('/api/jobs/pause', { method: 'POST' }).catch(() => null);
      if (r === null) { toast('Failed to pause queue', 'error'); return; }
      _paused = true;
    }
    _syncPauseBtn();
    _poll();
  });

  const cancelAllBtn = el('button', {
    id: 'queue-cancel-all-btn',
    class: 'btn btn-sm',
    text: '✕ Cancel waiting',
    title: 'Cancel all queued jobs (current job keeps running)',
    style: 'display:none;',
  });
  cancelAllBtn.addEventListener('click', async () => {
    cancelAllBtn.disabled = true;
    const r = await api('/api/jobs/cancel-queued', { method: 'POST' }).catch(() => null);
    if (r === null) toast('Failed to cancel queued jobs', 'error');
    else if (r?.cancelled) toast(`Cancelled ${r.cancelled} queued job${r.cancelled > 1 ? 's' : ''}`, 'info');
    cancelAllBtn.disabled = false;
    _poll();
  });

  const clearBtn = el('button', {
    id: 'queue-clear-btn',
    class: 'btn btn-sm',
    text: '🗑 Clear finished',
    title: 'Remove all completed and failed entries',
    style: 'display:none;',
  });
  clearBtn.addEventListener('click', async () => {
    clearBtn.disabled = true;
    await api('/api/jobs', { method: 'DELETE' }).catch(() => toast('Failed to clear finished jobs', 'error'));
    clearBtn.disabled = false;
    _poll();
  });

  const saveBtn = el('button', {
    id: 'queue-save-btn',
    class: 'btn btn-sm',
    text: '💾 Save Queue',
    title: 'Save waiting jobs to disk — restore them after a restart',
  });
  saveBtn.addEventListener('click', async () => {
    saveBtn.disabled = true;
    saveBtn.textContent = '…';
    const r = await api('/api/jobs/save-queue', { method: 'POST' }).catch(() => null);
    saveBtn.disabled = false;
    if (r?.saved != null) {
      toast(`Saved ${r.saved} job${r.saved !== 1 ? 's' : ''} to disk`, 'info');
      _checkRestoreBtn();
    } else {
      toast('Save failed', 'error');
    }
    saveBtn.textContent = '💾 Save Queue';
  });

  const restoreBtn = el('button', {
    id: 'queue-restore-btn',
    class: 'btn btn-sm',
    title: 'Re-queue jobs saved before last restart',
    style: 'display:none; background:var(--accent); color:var(--bg-base);',
  });
  restoreBtn.addEventListener('click', async () => {
    restoreBtn.disabled = true;
    restoreBtn.textContent = '…';
    const r = await api('/api/jobs/restore-queue', { method: 'POST' }).catch(() => null);
    restoreBtn.disabled = false;
    restoreBtn.style.display = 'none';
    if (r?.restored != null) {
      const msg = r.failed > 0
        ? `Restored ${r.restored} job${r.restored !== 1 ? 's' : ''} (${r.failed} failed)`
        : `Restored ${r.restored} job${r.restored !== 1 ? 's' : ''}`;
      toast(msg, 'info');
      _poll();
    } else {
      toast('Restore failed', 'error');
    }
  });

  toolbar.append(pauseBtn, cancelAllBtn, clearBtn, saveBtn, restoreBtn);
  _root.appendChild(toolbar);

  // Check for a saved queue on first render
  _checkRestoreBtn();

  // -- Scrollable list --
  const list = el('div', {
    id: 'queue-list',
    style: 'flex:1; overflow-y:auto; padding:12px 16px; display:flex; flex-direction:column; gap:8px;',
  });
  _root.appendChild(list);

  // -- Empty state --
  const empty = el('div', {
    id: 'queue-empty',
    style: 'display:none; flex-direction:column; align-items:center; justify-content:center; height:100%; gap:8px; color:var(--text-3); font-size:.85rem;',
  });
  empty.appendChild(el('div', { style: 'font-size:2rem;', text: '✓' }));
  empty.appendChild(el('div', { text: 'Queue is clear' }));
  list.appendChild(empty);
}

async function _checkRestoreBtn() {
  const btn = document.getElementById('queue-restore-btn');
  if (!btn) return;
  try {
    const info = await api('/api/jobs/save-queue');
    if (info?.has_save && info.count > 0) {
      btn.textContent = `♻ Restore ${info.count} saved job${info.count !== 1 ? 's' : ''}`;
      btn.style.display = '';
    } else {
      btn.style.display = 'none';
    }
  } catch (_) {
    btn.style.display = 'none';
  }
}

function _syncPauseBtn() {
  const btn = document.getElementById('queue-pause-btn');
  if (!btn) return;
  btn.textContent = _paused ? '▶ Resume' : '⏸ Pause';
  btn.title = _paused
    ? 'Resume — start processing queued jobs again'
    : 'Finish current job then hold — no new jobs will start';
  btn.style.background = _paused ? 'var(--accent)' : '';
  btn.style.color      = _paused ? 'var(--bg-base)' : '';
}

// -- Polling -------------------------------------------------------------------

function _startPoll() {
  if (_pollTimer) return;
  _pollTimer = setInterval(_poll, 2000);
}
function _stopPoll() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

async function _poll() {
  try {
    const data = await api('/api/jobs');
    _lastData = data;
    _paused = data.paused ?? _paused;
    _syncPauseBtn();
    _render(data);
    _updateRailHint(data);
    _notifyCompletions(data);
  } catch (_) {}
}

function _updateRailHint(data) {
  const hint = document.getElementById('queue-rail-hint');
  if (!hint) return;
  const nr = data.running?.length || 0;
  const nq = data.queued?.length  || 0;
  if (nr === 0 && nq === 0)    hint.textContent = '(idle)';
  else if (data.paused)        hint.textContent = `(paused${nq ? ` · ${nq} waiting` : ''})`;
  else if (nr > 0 && nq === 0) hint.textContent = '(generating)';
  else if (nr > 0)             hint.textContent = `(generating + ${nq} waiting)`;
  else                         hint.textContent = `(${nq} queued)`;
}

function _notifyCompletions(data) {
  for (const job of (data.completed || [])) {
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

// -- Rendering -----------------------------------------------------------------

function _render(data) {
  const list = document.getElementById('queue-list');
  const empty = document.getElementById('queue-empty');
  if (!list) return;

  const running   = data.running   || [];
  const queued    = data.queued    || [];
  const completed = data.completed || [];
  const total = running.length + queued.length + completed.length;

  // Show/hide toolbar buttons
  const cancelAllBtn = document.getElementById('queue-cancel-all-btn');
  const clearBtn     = document.getElementById('queue-clear-btn');
  const saveBtn      = document.getElementById('queue-save-btn');
  if (cancelAllBtn) cancelAllBtn.style.display = queued.length > 0 ? '' : 'none';
  if (clearBtn)     clearBtn.style.display     = completed.length > 0 ? '' : 'none';
  if (saveBtn)      saveBtn.style.display      = '';

  // Empty state
  if (empty) empty.style.display = total === 0 ? 'flex' : 'none';

  // Re-render cards — preserve existing DOM nodes by job id to avoid flicker
  const existing = new Map();
  list.querySelectorAll('[data-job-id]').forEach(n => existing.set(n.dataset.jobId, n));

  const ordered = [];
  if (running.length)   ordered.push({ head: _paused ? '▶  Running (last before pause)' : '▶  Now Generating', jobs: running, active: true });
  if (queued.length)    ordered.push({ head: `⋯  Waiting · ${queued.length}${_paused ? '  —  PAUSED' : ''}`, jobs: queued, active: true });
  if (completed.length) {
    const nFailed = completed.filter(j => j.status !== 'done').length;
    const head = nFailed > 0 ? `Finished · ${nFailed} failed` : 'Finished';
    ordered.push({ head, jobs: completed, active: false });
  }

  list.innerHTML = '';
  if (total === 0) { list.appendChild(empty); return; }

  for (const section of ordered) {
    list.appendChild(_sectionHead(section.head));
    section.jobs.forEach((job, idx) => {
      const card = _jobCard(job, section.active, idx, section.jobs.length);
      list.appendChild(card);
    });
  }
  list.appendChild(empty); // keep in DOM (display:none)
}

function _sectionHead(text) {
  return el('div', {
    style: 'font-size:.7rem; text-transform:uppercase; letter-spacing:.08em; color:var(--text-3); padding:8px 0 2px; margin-top:4px;',
    text,
  });
}

// -- Job card ------------------------------------------------------------------

function _jobCard(job, active, idx, total) {
  const isRunning   = job.status === 'running';
  const isFailed    = job.status === 'error' || job.status === 'stopped' || job.status === 'cancelled';
  const isDone      = job.status === 'done';
  const isQueued    = job.status === 'queued';
  const isClearable = isDone || isFailed;
  const borderColor = isFailed ? 'var(--red, #c41e3a)' : 'var(--border-2)';

  const card = el('div', {
    'data-job-id': job.id,
    style: `display:flex; gap:10px; align-items:flex-start; background:var(--surface-2);
            border:1px solid ${borderColor}; border-radius:8px; padding:10px 12px;
            cursor:pointer; transition:border-color .15s, background .15s;`,
  });
  if (isClearable) card.dataset.clearable = '1';

  card.addEventListener('click', e => {
    if (e.target.closest('button')) return;
    _showModal(job);
  });
  card.addEventListener('mouseenter', () => {
    card.style.borderColor = isFailed ? '#e05' : 'var(--accent)';
    card.style.background  = 'var(--surface-3, rgba(255,255,255,.06))';
  });
  card.addEventListener('mouseleave', () => {
    card.style.borderColor = borderColor;
    card.style.background  = 'var(--surface-2)';
  });

  // Thumbnail
  const thumb = el('div', {
    style: 'width:64px; height:42px; flex-shrink:0; border-radius:5px; background:var(--surface-3); overflow:hidden; display:flex; align-items:center; justify-content:center;',
  });
  const srcImg  = job.meta?.source_image;
  const outPath = isDone ? _bestOutput(job) : null;
  const isVidOut = outPath && /\.(mp4|webm|mov)$/i.test(outPath);
  const thumbSrc = (!isVidOut && outPath)
    ? `/api/thumbnail?path=${encodeURIComponent(outPath)}&size=120`
    : srcImg ? `/api/thumbnail?path=${encodeURIComponent(srcImg)}&size=120` : null;
  if (thumbSrc) {
    const img = el('img', { style: 'width:100%; height:100%; object-fit:cover; border-radius:5px;' });
    img.src = thumbSrc;
    img.onerror = () => { img.style.display = 'none'; thumb.appendChild(el('span', { text: '🎬', style: 'font-size:1.3rem;' })); };
    thumb.appendChild(img);
  } else {
    thumb.appendChild(el('span', { text: '🎬', style: 'font-size:1.3rem;' }));
  }
  card.appendChild(thumb);

  // Body
  const body = el('div', { style: 'flex:1; min-width:0; display:flex; flex-direction:column; gap:4px;' });

  const top = el('div', { style: 'display:flex; align-items:center; gap:8px;' });
  top.appendChild(el('div', {
    style: 'font-size:.82rem; font-weight:600; color:var(--text-1); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; flex:1; min-width:0;',
    text: job.label || job.type,
  }));
  top.appendChild(_statusChip(job));
  body.appendChild(top);

  if (job.meta?.prompt) {
    body.appendChild(el('div', {
      style: 'font-size:.74rem; color:var(--text-3); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;',
      text: job.meta.prompt,
    }));
  }

  if (isRunning) {
    const pct = job.progress || 0;
    const barWrap = el('div', { style: 'height:4px; background:var(--surface-3); border-radius:2px; overflow:hidden; margin-top:2px;' });
    barWrap.appendChild(el('div', { style: `height:100%; border-radius:2px; background:var(--accent); width:${pct}%; transition:width .5s ease;` }));
    body.appendChild(barWrap);
    if (job.message) body.appendChild(el('div', { style: 'font-size:.71rem; color:var(--text-3);', text: job.message }));
  }

  if (isDone) body.appendChild(el('div', { style: 'font-size:.71rem; color:var(--accent); opacity:.8;', text: '▶ click to watch' }));

  if (isFailed && (job.error || job.message)) {
    const errText = (job.error || job.message || '').split(';')[0].trim().slice(0, 140);
    body.appendChild(el('div', {
      style: 'font-size:.71rem; color:var(--red, #c41e3a); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;',
      text: errText, title: job.error || job.message,
    }));
  }

  card.appendChild(body);

  // Action buttons column
  const actions = el('div', { style: 'display:flex; flex-direction:column; gap:4px; flex-shrink:0;' });

  if (isClearable) {
    if (isFailed) {
      // Retry
      const retryBtn = el('button', { class: 'btn btn-sm', text: '↺ Retry', title: 'Re-submit with same settings', style: 'font-size:.75rem; padding:3px 8px;' });
      retryBtn.addEventListener('click', async e => {
        e.stopPropagation();
        retryBtn.disabled = true;
        retryBtn.textContent = '…';
        const r = await api(`/api/jobs/${job.id}/retry`, { method: 'POST' }).catch(() => null);
        if (r?.ok) { toast('Retrying…', 'info'); _poll(); }
        else { toast('Could not retry this job', 'error'); retryBtn.disabled = false; retryBtn.textContent = '↺ Retry'; }
      });
      actions.appendChild(retryBtn);
    }
    // Dismiss
    const xBtn = el('button', { class: 'btn btn-sm', text: '✕', title: 'Dismiss', style: 'font-size:.8rem; padding:3px 8px; opacity:.55;' });
    xBtn.addEventListener('click', async e => {
      e.stopPropagation();
      xBtn.disabled = true;
      await api(`/api/jobs/${job.id}`, { method: 'DELETE' }).catch(() => {});
      card.remove();
    });
    actions.appendChild(xBtn);
  } else if (active) {
    if (isQueued && idx > 0) {
      // Promote to front
      const upBtn = el('button', { class: 'btn btn-sm', text: '↑ Next', title: 'Move to front of queue', style: 'font-size:.75rem; padding:3px 8px;' });
      upBtn.addEventListener('click', async e => {
        e.stopPropagation();
        await api(`/api/jobs/${job.id}/promote`, { method: 'POST' }).catch(() => {});
        _poll();
      });
      actions.appendChild(upBtn);
    }
    // Cancel
    const cancelBtn = el('button', { class: 'btn btn-sm', text: '✕ Cancel', title: 'Cancel this job', style: 'font-size:.75rem; padding:3px 8px;' });
    cancelBtn.addEventListener('click', async e => {
      e.stopPropagation();
      cancelBtn.disabled = true;
      await api(`/api/jobs/${job.id}/stop`, { method: 'POST' }).catch(() => {});
      toast('Job cancelled', 'info');
      card.style.opacity = '.4';
    });
    actions.appendChild(cancelBtn);
  }

  if (actions.children.length) card.appendChild(actions);
  return card;
}

function _bestOutput(job) {
  if (!job.output) return null;
  const out = Array.isArray(job.output) ? job.output : [job.output];
  return out.length > 1 ? out[1] : out[0];
}

function _statusChip(job) {
  const map = {
    preparing: ['Preparing',  'var(--accent)'],
    running:   ['Generating', 'var(--accent)'],
    queued:    ['Waiting',    'var(--text-3)'],
    done:      ['Done',       '#4caf50'],
    error:     ['Failed',     'var(--red, #c41e3a)'],
    stopped:   ['Cancelled',  'var(--text-3)'],
    cancelled: ['Cancelled',  'var(--text-3)'],
  };
  const [label, color] = map[job.status] || ['Unknown', 'var(--text-3)'];
  const display = job.status === 'queued' && job.queue_position != null
    ? `#${job.queue_position + 1}`
    : label;
  return el('span', {
    style: `font-size:.66rem; padding:2px 5px; border-radius:4px; border:1px solid ${color}; color:${color}; white-space:nowrap; flex-shrink:0;`,
    text: display,
  });
}

// -- Detail modal --------------------------------------------------------------
//
// Rebuilt to be LIVE: while an active job is open, we re-poll /api/jobs/{id}
// every 1.5 s and refresh the progress bar, stage chip, ETA countdown, and
// the strip of clip thumbnails as new clips finish. For done/failed jobs we
// add an AI feedback box that branches the source tab with the user's note.

const _STAGE_LABELS = {
  'analyzing':       'Analyzing audio',
  'planning':        'Planning story arc',
  'waiting-gpu':     'Waiting for GPU',
  'generating':      'Generating clips',
  'concatenating':   'Concatenating clips',
  'merging':         'Merging audio',
};

let _modalState = null;  // { jobId, refreshTimer, etaTimer, lastEta, ... }

function _stopModalTimers() {
  if (!_modalState) return;
  if (_modalState.refreshTimer) clearInterval(_modalState.refreshTimer);
  if (_modalState.etaTimer)     clearInterval(_modalState.etaTimer);
  _modalState = null;
}

function _parseMessage(msg) {
  // "Clip 3/5 -- step 4/8 -- ~2m 30s left" -> structured fields.
  const out = { clipNum: null, clipTotal: null, step: null, stepTotal: null, etaSec: null };
  if (!msg) return out;
  const clip = msg.match(/Clip\s+(\d+)\s*\/\s*(\d+)/i);
  if (clip) { out.clipNum = +clip[1]; out.clipTotal = +clip[2]; }
  const step = msg.match(/step\s+(\d+)\s*\/\s*(\d+)/i);
  if (step) { out.step = +step[1]; out.stepTotal = +step[2]; }
  const eta  = msg.match(/~?\s*(?:(\d+)\s*m)?\s*(\d+)\s*s\s*(?:left|remaining)?/i);
  if (eta) {
    const m = parseInt(eta[1] || '0', 10);
    const s = parseInt(eta[2] || '0', 10);
    out.etaSec = m * 60 + s;
  }
  return out;
}

function _formatEta(sec) {
  if (sec == null || sec < 0) return '';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m > 0 ? `${m}m ${String(s).padStart(2, '0')}s` : `${s}s`;
}

function _showModal(job) {
  _stopModalTimers();
  document.getElementById('queue-job-modal')?.remove();

  const overlay = el('div', {
    id: 'queue-job-modal',
    style: 'position:fixed; inset:0; z-index:9000; background:rgba(0,0,0,.82); display:flex; align-items:center; justify-content:center;',
  });
  function _close() {
    _stopModalTimers();
    overlay.querySelector('video')?.pause();
    overlay.remove();
  }
  overlay.addEventListener('click', e => { if (e.target === overlay) _close(); });

  const box = el('div', {
    style: 'background:var(--surface-1); border-radius:12px; padding:20px; max-width:min(860px,90vw); width:100%; display:flex; flex-direction:column; gap:14px; max-height:90vh; overflow:auto;',
  });

  // -- Header --
  const hdr        = el('div', { style: 'display:flex; align-items:center; gap:10px;' });
  const titleEl    = el('span', { style: 'font-size:.95rem; font-weight:700; flex:1; color:var(--text-1);', text: job.label || job.type });
  const chipSlot   = el('span', { style: 'display:inline-flex; align-items:center;' });
  chipSlot.appendChild(_statusChip(job));
  const closeX     = el('button', { class: 'btn btn-sm', text: 'X', style: 'padding:2px 8px;' });
  closeX.addEventListener('click', _close);
  hdr.appendChild(titleEl); hdr.appendChild(chipSlot); hdr.appendChild(closeX);
  box.appendChild(hdr);

  // -- Output video (when done) --
  const videoSlot = el('div');
  box.appendChild(videoSlot);

  // -- Live progress block --
  const liveBlock   = el('div', { style: 'display:flex; flex-direction:column; gap:10px;' });
  const stageRow    = el('div', { style: 'display:flex; align-items:center; gap:8px; font-size:.82rem;' });
  const heartbeat   = el('span', {
    style: 'display:inline-block; width:9px; height:9px; border-radius:50%; background:var(--accent); animation:dcs-pulse 1.1s ease-in-out infinite;',
  });
  const stageLabel  = el('span', { style: 'font-weight:600; color:var(--text-1);', text: '' });
  const stepBadge   = el('span', { style: 'font-size:.72rem; padding:2px 7px; border-radius:4px; background:var(--surface-3); color:var(--text-2);', text: '' });
  const etaBadge    = el('span', { style: 'margin-left:auto; font-variant-numeric:tabular-nums; color:var(--accent); font-weight:600;', text: '' });
  stageRow.appendChild(heartbeat); stageRow.appendChild(stageLabel); stageRow.appendChild(stepBadge); stageRow.appendChild(etaBadge);

  const barWrap = el('div', { style: 'height:8px; background:var(--surface-3); border-radius:4px; overflow:hidden;' });
  const barFill = el('div', { style: 'height:100%; background:var(--accent); width:0%; border-radius:4px; transition:width .5s;' });
  barWrap.appendChild(barFill);

  const messageEl = el('div', {
    style: 'font-size:.82rem; color:var(--text-2); padding:8px 10px; background:var(--surface-2); border-radius:6px; min-height:20px;',
  });

  liveBlock.appendChild(stageRow);
  liveBlock.appendChild(barWrap);
  liveBlock.appendChild(messageEl);
  box.appendChild(liveBlock);

  // -- Source image + prompt (compact) --
  const metaRow = el('div', { style: 'display:flex; gap:12px; align-items:flex-start;' });
  const srcImg  = el('img', { style: 'display:none; max-width:140px; max-height:100px; border-radius:6px; object-fit:cover;' });
  srcImg.onerror = () => { srcImg.style.display = 'none'; };
  const promptCol = el('div', { style: 'flex:1; display:flex; flex-direction:column; gap:4px;' });
  metaRow.appendChild(srcImg); metaRow.appendChild(promptCol);
  box.appendChild(metaRow);

  // -- Error block --
  const errorEl = el('div', {
    style: 'display:none; font-size:.82rem; color:var(--red, #c41e3a); padding:10px 12px; background:var(--surface-2); border-radius:6px; white-space:pre-wrap; word-break:break-word;',
  });
  box.appendChild(errorEl);

  // -- AI feedback / branch (rendered when terminal) --
  const feedbackBlock = el('div', { style: 'display:none; flex-direction:column; gap:8px; padding:12px; background:var(--surface-2); border-radius:8px;' });
  const feedbackTitle = el('div', { style: 'font-size:.75rem; text-transform:uppercase; letter-spacing:.05em; color:var(--text-3);', text: 'Make another version' });
  const feedbackHelp  = el('div', { style: 'font-size:.74rem; color:var(--text-3); line-height:1.4;', text: 'Describe what you want changed and click Tweak. The source tab opens with the original settings pre-loaded and the AI adjusts them per your note before you click Generate.' });
  const feedbackInput = el('textarea', {
    rows: '2',
    placeholder: 'e.g. slower pace, darker mood, less camera motion',
    style: 'width:100%; resize:vertical; font-size:.85rem;',
  });
  const feedbackBtnRow = el('div', { style: 'display:flex; gap:8px; flex-wrap:wrap;' });
  const tweakBtn        = el('button', { class: 'btn btn-primary btn-sm', text: 'Tweak this video' });
  const rerunBtn        = el('button', { class: 'btn btn-sm', text: 'Re-run same settings' });
  const contBtn         = el('button', { class: 'btn btn-sm', text: 'Create continuation', title: 'Extract the last frame of this video and use it as the start image for the next generation' });
  feedbackBtnRow.appendChild(tweakBtn); feedbackBtnRow.appendChild(rerunBtn); feedbackBtnRow.appendChild(contBtn);
  feedbackBlock.appendChild(feedbackTitle);
  feedbackBlock.appendChild(feedbackHelp);
  feedbackBlock.appendChild(feedbackInput);
  feedbackBlock.appendChild(feedbackBtnRow);
  box.appendChild(feedbackBlock);

  tweakBtn.addEventListener('click', () => _doBranch(job, feedbackInput.value.trim(), _close));
  rerunBtn.addEventListener('click', () => _doBranch(job, '', _close));
  contBtn.addEventListener('click', () => _doContinuation(job, _close));

  overlay.appendChild(box);
  document.body.appendChild(overlay);

  // -- Initial render + start the live refresh loop --
  _modalState = { jobId: job.id, lastEta: null, lastEtaAt: 0 };
  _renderModal(job, { stageLabel, stepBadge, etaBadge, barFill, messageEl,
    srcImg, promptCol, errorEl, videoSlot, chipSlot, feedbackBlock, heartbeat });
  _startModalRefresh({ stageLabel, stepBadge, etaBadge, barFill, messageEl,
    srcImg, promptCol, errorEl, videoSlot, chipSlot, feedbackBlock, heartbeat });
}

function _renderModal(job, els) {
  const isDone   = job.status === 'done';
  const isActive = job.status === 'running' || job.status === 'queued' || job.status === 'preparing';
  const isFailed = job.status === 'error' || job.status === 'stopped';

  // Status chip (re-render in case status changed)
  els.chipSlot.innerHTML = '';
  els.chipSlot.appendChild(_statusChip(job));

  // Heartbeat: pulsing while active, static while terminal
  els.heartbeat.style.animation = isActive ? 'dcs-pulse 1.1s ease-in-out infinite' : 'none';
  els.heartbeat.style.background = isFailed ? 'var(--red, #c41e3a)' : isDone ? '#3fa84a' : 'var(--accent)';

  // Stage label
  const stage = job.meta?.stage || (isDone ? 'done' : isFailed ? 'failed' : 'running');
  const parsed = _parseMessage(job.message);
  let stageText = _STAGE_LABELS[stage] || (isDone ? 'Complete' : isFailed ? 'Stopped' : 'Working');
  if (stage === 'generating' && parsed.clipNum && parsed.clipTotal) {
    stageText = `Clip ${parsed.clipNum} of ${parsed.clipTotal}`;
  }
  els.stageLabel.textContent = stageText;

  // Step badge ("step 4/8" inside a clip)
  if (parsed.step && parsed.stepTotal) {
    els.stepBadge.textContent = `step ${parsed.step}/${parsed.stepTotal}`;
    els.stepBadge.style.display = '';
  } else {
    els.stepBadge.style.display = 'none';
  }

  // ETA: track in modalState so the tick-down timer can update between polls
  if (parsed.etaSec != null) {
    _modalState.lastEta   = parsed.etaSec;
    _modalState.lastEtaAt = Date.now();
    els.etaBadge.textContent = _formatEta(parsed.etaSec) + ' left';
  } else if (!isActive) {
    els.etaBadge.textContent = '';
  }

  // Progress bar
  const pct = Math.max(0, Math.min(100, job.progress || 0));
  els.barFill.style.width = `${pct}%`;
  if (isDone)   els.barFill.style.background = '#3fa84a';
  if (isFailed) els.barFill.style.background = 'var(--red, #c41e3a)';

  // Message line
  els.messageEl.textContent = job.message || '';
  els.messageEl.style.display = job.message ? '' : 'none';

  // Source image
  if (job.meta?.source_image) {
    const url = `/api/thumbnail?path=${encodeURIComponent(job.meta.source_image)}&size=280`;
    if (els.srcImg.src !== url) els.srcImg.src = url;
    els.srcImg.style.display = '';
  } else {
    els.srcImg.style.display = 'none';
  }

  // Prompt
  els.promptCol.innerHTML = '';
  if (job.meta?.prompt) {
    els.promptCol.appendChild(el('span', { style: 'font-size:.7rem; text-transform:uppercase; letter-spacing:.06em; color:var(--text-3);', text: 'Prompt' }));
    els.promptCol.appendChild(el('span', { style: 'font-size:.82rem; color:var(--text-2); line-height:1.4;', text: job.meta.prompt }));
  }

  // Error
  if (isFailed && (job.error || job.message)) {
    els.errorEl.textContent = job.error || job.message;
    els.errorEl.style.display = '';
  } else {
    els.errorEl.style.display = 'none';
  }

  // Output video (only render once, on the transition into done)
  if (isDone && !els.videoSlot.querySelector('video')) {
    els.videoSlot.innerHTML = '';
    const out = _bestOutput(job);
    if (out) {
      const vid = document.createElement('video');
      vid.controls = true;
      vid.style.cssText = 'width:100%; max-height:55vh; border-radius:8px; background:#000;';
      vid.src = pathToUrl(out);
      els.videoSlot.appendChild(vid);
    }
  } else if (!isDone && els.videoSlot.querySelector('video')) {
    els.videoSlot.innerHTML = '';
  }

  // Feedback box: only useful once the job is terminal AND we have settings to branch with
  const hasSettings = !!(job.meta?.settings && Object.keys(job.meta.settings).length);
  if ((isDone || isFailed) && hasSettings) {
    els.feedbackBlock.style.display = 'flex';
  } else {
    els.feedbackBlock.style.display = 'none';
  }
}

function _startModalRefresh(els) {
  if (!_modalState) return;

  // Re-poll the job every 1.5 s
  _modalState.refreshTimer = setInterval(async () => {
    if (!_modalState) return;
    try {
      const fresh = await api(`/api/jobs/${_modalState.jobId}`);
      if (fresh && fresh.id) _renderModal(fresh, els);
    } catch (_) { /* network blip; next tick will retry */ }
  }, 1500);

  // Tick the ETA down every second between polls so the number visibly counts
  _modalState.etaTimer = setInterval(() => {
    if (!_modalState || _modalState.lastEta == null) return;
    const elapsed = (Date.now() - _modalState.lastEtaAt) / 1000;
    const remaining = Math.max(0, _modalState.lastEta - elapsed);
    els.etaBadge.textContent = remaining > 0 ? `${_formatEta(remaining)} left` : '';
  }, 1000);
}

async function _doContinuation(job, closeFn) {
  const output = _bestOutput(job);
  if (!output) {
    toast('No output video found for this job', 'error');
    return;
  }

  const tabId    = job.meta?.feature === 'song_video' ? 'song-video' : job.meta?.feature || '';
  const settings = job.meta?.settings || {};
  if (!tabId || !Object.keys(settings).length) {
    toast('No source tab or settings to continue from', 'error');
    return;
  }

  const contBtn = document.querySelector('#queue-job-modal .btn[title*="last frame"]');
  if (contBtn) { contBtn.disabled = true; contBtn.textContent = '...'; }

  let frameData;
  try {
    frameData = await api('/api/song-video/extract-frame', {
      method: 'POST',
      body: JSON.stringify({ video_path: output }),
    });
  } catch (e) {
    toast(`Could not extract last frame: ${e.message}`, 'error');
    if (contBtn) { contBtn.disabled = false; contBtn.textContent = 'Create continuation'; }
    return;
  }

  // Navigate to the source tab and apply settings with photo_path overridden
  // to the extracted last frame so the next generation starts from where this
  // one ended.
  const tabBtn = document.querySelector(`.rail-tab[data-tab="${tabId}"]`);
  if (tabBtn) tabBtn.click();

  const { applySettingsToTab } = await import('./shell/ai-intent.js?v=20260503h');
  setTimeout(() => {
    const merged = { ...settings, photo_path: frameData.path };
    const ok = applySettingsToTab(tabId, merged);
    if (!ok) {
      toast(`Open the ${tabId} tab first`, 'info');
      return;
    }
    toast('Continuation ready -- adjust the story idea then click Generate.', 'success');
  }, 100);

  closeFn();
}

async function _doBranch(job, feedback, closeFn) {
  const tabId   = job.meta?.feature === 'song_video' ? 'song-video'
              : job.meta?.feature === 'fun_videos'  ? 'fun-videos'
              : job.meta?.feature || '';
  const settings = job.meta?.settings || {};
  if (!tabId || !Object.keys(settings).length) {
    toast('No source tab or settings to branch from', 'error');
    return;
  }

  // Navigate to the source tab so its lazy-init runs and registers its applier
  const tabBtn = document.querySelector(`.rail-tab[data-tab="${tabId}"]`);
  if (tabBtn) tabBtn.click();

  // Apply the original settings, then optionally run the feedback through askAI
  // so the AI mutates them per the user's note before they hit Generate.
  const { applySettingsToTab, askAI } = await import('./shell/ai-intent.js?v=20260503h');
  setTimeout(async () => {
    const ok = applySettingsToTab(tabId, settings);
    if (!ok) {
      toast(`Open the ${tabId} tab first`, 'info');
      return;
    }
    if (feedback) {
      try { await askAI(feedback); }
      catch (e) { toast(`AI feedback failed: ${e.message}`, 'warn'); }
    }
    toast(feedback ? 'Branched with feedback. Click Generate to run.' : 'Branched. Tweak and click Generate.', 'success');
  }, 100);

  closeFn();
}

