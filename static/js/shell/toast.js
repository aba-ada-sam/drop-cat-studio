/**
 * Drop Cat Go Studio -- Unified toast + error log system (WS7).
 * Exports: toast(), apiFetch(), openErrorLog(), clearErrorLog()
 */

const _MAX_ERRORS = 100;
const _errors = [];
let _errorCount = 0;
let _toastWrap = null;
let _errorBadge = null;

function _getWrap() {
  if (!_toastWrap) _toastWrap = document.getElementById('toast-wrap');
  return _toastWrap;
}

function _updateBadge() {
  if (!_errorBadge) _errorBadge = document.querySelector('#btn-error-log .error-badge');
  const btn = document.getElementById('btn-error-log');
  if (!btn) return;
  _errorCount = _errors.length;
  btn.classList.toggle('has-errors', _errorCount > 0);
  if (_errorBadge) _errorBadge.textContent = _errorCount > 9 ? '9+' : _errorCount;
}

/**
 * Show a toast notification.
 * @param {string} msg
 * @param {'success'|'error'|'info'|'progress'} level
 * @param {object} opts  { id, duration, sticky, retry, onRetry, details, progress }
 * @returns {object} controller { update(pct), dismiss() }
 */
export function toast(msg, level = 'info', opts = {}) {
  const wrap = _getWrap();
  if (!wrap) return { update() {}, dismiss() {} };

  // Deduplicate by id
  if (opts.id) {
    const existing = wrap.querySelector(`[data-toast-id="${opts.id}"]`);
    if (existing) existing.remove();
  }

  const el = document.createElement('div');
  el.className = `toast ${level}`;
  if (opts.id) el.dataset.toastId = opts.id;

  // Sticky: errors stick by default, success/info auto-dismiss
  const sticky = opts.sticky ?? (level === 'error');
  const duration = opts.duration ?? (level === 'success' ? 3500 : level === 'info' ? 4000 : null);

  const inner = document.createElement('div');
  inner.className = 'toast-inner';

  const msgEl = document.createElement('span');
  msgEl.className = 'toast-msg';
  msgEl.textContent = msg;
  inner.appendChild(msgEl);

  const closeBtn = document.createElement('button');
  closeBtn.className = 'toast-close';
  closeBtn.setAttribute('aria-label', 'Dismiss');
  closeBtn.textContent = '\xD7';
  closeBtn.addEventListener('click', () => dismiss());
  inner.appendChild(closeBtn);

  el.appendChild(inner);

  // Action buttons (retry, view details)
  if (opts.onRetry || opts.details) {
    const actionsEl = document.createElement('div');
    actionsEl.className = 'toast-actions';
    if (opts.onRetry) {
      const retryBtn = document.createElement('button');
      retryBtn.className = 'toast-btn';
      retryBtn.textContent = 'Retry';
      retryBtn.addEventListener('click', () => { dismiss(); opts.onRetry(); });
      actionsEl.appendChild(retryBtn);
    }
    if (opts.details) {
      const detBtn = document.createElement('button');
      detBtn.className = 'toast-btn';
      detBtn.textContent = 'Details';
      detBtn.addEventListener('click', () => {
        _logError(msg, opts.context || '', opts.details);
        openErrorLog();
      });
      actionsEl.appendChild(detBtn);
    }
    el.appendChild(actionsEl);
  }

  // Progress bar for progress toasts
  let progressBar = null;
  if (level === 'progress') {
    progressBar = document.createElement('div');
    progressBar.className = 'toast-progress-bar';
    progressBar.style.width = `${opts.progress ?? 0}%`;
    el.appendChild(progressBar);
  }

  wrap.appendChild(el);

  let timer = null;
  if (!sticky && duration) {
    timer = setTimeout(() => dismiss(), duration);
  }

  function dismiss() {
    if (timer) clearTimeout(timer);
    el.style.opacity = '0';
    el.style.transform = 'translateX(18px)';
    el.style.transition = `opacity ${120}ms ease, transform ${120}ms ease`;
    setTimeout(() => el.remove(), 130);
  }

  function update(pct, newMsg) {
    if (progressBar) progressBar.style.width = `${Math.min(100, pct)}%`;
    if (newMsg) msgEl.textContent = newMsg;
    if (pct >= 100) setTimeout(dismiss, 600);
  }

  return { dismiss, update };
}

function _logError(msg, context, details) {
  const entry = { time: new Date().toLocaleTimeString(), msg, context, details: String(details || '') };
  _errors.unshift(entry);
  if (_errors.length > _MAX_ERRORS) _errors.length = _MAX_ERRORS;
  _updateBadge();

  // If error log is open, prepend entry
  const container = document.getElementById('error-log-entries');
  if (container) _prependErrorEntry(container, entry);
}

function _prependErrorEntry(container, entry) {
  const div = document.createElement('div');
  div.className = 'error-log-entry';
  div.innerHTML = `<span class="elog-time">${entry.time}</span>` +
    (entry.context ? `<span class="elog-ctx">[${entry.context}]</span>` : '') +
    `<span class="elog-msg">${_escHtml(entry.msg)}</span>` +
    (entry.details ? `<div style="margin-top:2px;color:var(--text-3);word-break:break-all">${_escHtml(entry.details)}</div>` : '');
  container.prepend(div);
}

export function openErrorLog() {
  let overlay = document.getElementById('error-log-overlay');
  if (!overlay) return;
  overlay.classList.add('open');

  const container = document.getElementById('error-log-entries');
  if (!container) return;
  container.innerHTML = '';
  for (const entry of _errors) _prependErrorEntry(container, entry);
}

export function clearErrorLog() {
  _errors.length = 0;
  _updateBadge();
  const container = document.getElementById('error-log-entries');
  if (container) container.innerHTML = '';
}

/**
 * Central fetch wrapper — auto-toasts errors, supports retry.
 * Drop-in replacement for fetch() calls that expect JSON.
 */
export async function apiFetch(path, opts = {}) {
  const context = opts.context || path;
  try {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json', ...opts.headers },
      ...opts,
    });
    if (!res.ok) {
      let errMsg = `Server error ${res.status}`;
      let details = '';
      try {
        const d = await res.json();
        errMsg = d.detail || d.error || errMsg;
        details = JSON.stringify(d);
      } catch (_) {}
      _logError(errMsg, context, details);
      toast(errMsg, 'error', {
        context,
        details,
        onRetry: opts.onRetry,
      });
      throw new Error(errMsg);
    }
    return res.json();
  } catch (e) {
    if (e.name === 'TypeError') {
      // Network error (offline, refused)
      const errMsg = `Network error: ${context}`;
      _logError(errMsg, context, e.message);
      toast(errMsg, 'error', {
        context,
        details: e.message,
        onRetry: opts.onRetry,
      });
    }
    throw e;
  }
}

function _escHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

// Wire up close button on error log
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('btn-clear-errors')?.addEventListener('click', clearErrorLog);
  document.getElementById('error-log-overlay')?.addEventListener('click', e => {
    if (e.target === e.currentTarget) e.currentTarget.classList.remove('open');
  });
  document.getElementById('btn-close-error-log')?.addEventListener('click', () => {
    document.getElementById('error-log-overlay')?.classList.remove('open');
  });
  document.getElementById('btn-error-log')?.addEventListener('click', openErrorLog);
  _updateBadge();
});
