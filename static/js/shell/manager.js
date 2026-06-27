/**
 * Drop Cat Go Studio -- AI Manager.
 *
 * An autonomous in-app agent. The user types a goal in plain English; the
 * Manager drives the REAL UI to accomplish it -- navigating tabs, filling
 * controls, clicking buttons, watching the queue -- narrating as it goes.
 *
 * Architecture: the browser owns the agent loop. Each step we snapshot the
 * live screen, POST {goal, screen, history} to /api/manager/think, get back the
 * single next action, execute it against the DOM, re-snapshot, and repeat until
 * the brain says done (or the user hits Stop). All DOM control is client-side so
 * the user can literally watch it work.
 */
import { apiFetch } from './toast.js?v=20260620a';

const MAX_STEPS = 48;          // hard cap per task -- prevents runaway loops
const SETTLE_MS = 700;         // wait for lazy tab init / async renders after a UI change
const CHAT_KEY = 'dropcat_manager_chat';
const CHAT_MAX = 24;

let _running = false;
let _stop = false;
let _askResolver = null;
let _chat = _loadChat();

// -- chat persistence --------------------------------------------------------
function _loadChat() {
  try {
    const raw = localStorage.getItem(CHAT_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr.slice(-CHAT_MAX) : [];
  } catch (_) { return []; }
}
function _saveChat() {
  try { localStorage.setItem(CHAT_KEY, JSON.stringify(_chat.slice(-CHAT_MAX))); } catch (_) {}
}
function _pushChat(role, text) {
  _chat.push({ role, text });
  if (_chat.length > CHAT_MAX) _chat = _chat.slice(-CHAT_MAX);
  _saveChat();
}

// -- DOM helpers -------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const _sleep = (ms) => new Promise(r => setTimeout(r, ms));

function _visible(el) {
  if (!el) return false;
  const cs = getComputedStyle(el);
  if (cs.display === 'none' || cs.visibility === 'hidden' || el.hidden) return false;
  if (el.offsetParent === null && cs.position !== 'fixed') return false;
  const r = el.getBoundingClientRect();
  return r.width > 1 && r.height > 1;
}

function _labelFor(el) {
  if (el.id) {
    try {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && l.textContent.trim()) return l.textContent.trim();
    } catch (_) {}
  }
  const wrap = el.closest('label');
  if (wrap && wrap.textContent.trim()) return wrap.textContent.trim().slice(0, 80);
  return (el.getAttribute('aria-label') || el.title || el.placeholder || el.name || '').slice(0, 80);
}

function _isActive(el) {
  const c = el.className && el.className.toString ? el.className.toString() : '';
  return /\b(active|selected|on|checked)\b/.test(c)
    || el.getAttribute('aria-pressed') === 'true'
    || el.getAttribute('aria-current') === 'page'
    || el.getAttribute('aria-current') === 'true';
}

/**
 * The active surface the Manager operates on: a visible modal, else the open
 * gallery overlay, else the active tab panel.
 */
function _activeSurface() {
  const modal = [...document.querySelectorAll('.modal-overlay, #service-panel-overlay, #error-log-overlay')]
    .find(m => m.classList.contains('open') || _visible(m));
  if (modal && _visible(modal)) return modal;
  const gal = $('gallery-overlay');
  if (gal && gal.getAttribute('aria-hidden') !== 'true' && _visible(gal)) return gal;
  return document.querySelector('.tab-panel.active') || document.querySelector('.tab-panel');
}

function _activeTabId() {
  const gal = $('gallery-overlay');
  if (gal && gal.getAttribute('aria-hidden') !== 'true') return 'gallery';
  return document.querySelector('.rail-tab.active[data-tab]')?.dataset.tab || 'pipeline';
}

/**
 * Snapshot the live screen into a compact structure the brain can reason over.
 * Tags each interactable element with a data-mgr-ref so set_field/click can
 * find it again. Refs are only valid until the next read.
 */
function readScreen() {
  // clear old refs
  document.querySelectorAll('[data-mgr-ref]').forEach(el => el.removeAttribute('data-mgr-ref'));
  const surface = _activeSurface();
  const tab = _activeTabId();
  const title = surface?.getAttribute('aria-label')
    || document.querySelector('.rail-tab.active[data-tab] span')?.textContent
    || tab;

  const controls = [];
  const buttons = [];
  let n = 0;

  if (surface) {
    const els = surface.querySelectorAll('input, select, textarea, button, [role="button"], a[href]');
    for (const el of els) {
      if (!_visible(el)) continue;
      if (controls.length + buttons.length >= 70) break;
      const ref = 'r' + (++n);
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute('type') || '').toLowerCase();

      if (tag === 'button' || el.getAttribute('role') === 'button' || (tag === 'a')) {
        const label = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60) || _labelFor(el);
        if (!label) continue;
        el.setAttribute('data-mgr-ref', ref);
        const b = { ref, label };
        if (_isActive(el)) b.active = true;
        if (el.disabled) b.disabled = true;
        buttons.push(b);
        continue;
      }

      el.setAttribute('data-mgr-ref', ref);
      const c = { ref, label: _labelFor(el) };
      if (tag === 'select') {
        c.kind = 'select';
        c.value = el.value;
        c.options = [...el.options].slice(0, 24).map(o => o.textContent.trim() || o.value);
      } else if (tag === 'textarea') {
        c.kind = 'text';
        c.value = (el.value || '').slice(0, 200);
      } else if (type === 'checkbox') {
        c.kind = 'checkbox';
        c.value = el.checked;
      } else if (type === 'radio') {
        c.kind = 'radio';
        c.value = el.checked;
        c.name = el.name;
      } else if (type === 'range') {
        c.kind = 'slider';
        c.value = el.value;
        c.min = el.min; c.max = el.max; c.step = el.step;
      } else if (type === 'file') {
        c.kind = 'file';
        c.value = '(user must drop/choose a file)';
      } else {
        c.kind = type || 'text';
        c.value = (el.value || '').slice(0, 200);
      }
      if (el.disabled) c.disabled = true;
      controls.push(c);
    }
  }

  // Short visible-text context (labels, hints, current selections, status).
  let text = '';
  try {
    text = (surface?.innerText || '').replace(/\s+\n/g, '\n').replace(/\n{2,}/g, '\n').trim().slice(0, 1000);
  } catch (_) {}

  return { tab, title: (title || '').toString().trim(), controls, buttons, text };
}

// -- action executors --------------------------------------------------------
function _findRef(ref) { return document.querySelector(`[data-mgr-ref="${CSS.escape(ref)}"]`); }

async function _navigate(tab) {
  if (tab === 'gallery') { $('btn-gallery-rail')?.click(); await _sleep(SETTLE_MS); return `opened gallery`; }
  const btn = document.querySelector(`.rail-tab[data-tab="${tab}"]`);
  if (!btn) return `unknown tab: ${tab}`;
  btn.click();
  await _sleep(SETTLE_MS);
  return `navigated to ${tab}`;
}

function _setNative(el, value) { el.value = value; }

function _setField(ref, value) {
  const el = _findRef(ref);
  if (!el) return `no control with ref ${ref} (re-read the screen)`;
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  const v = value == null ? '' : String(value);
  try {
    if (tag === 'select') {
      const opt = [...el.options].find(o =>
        o.value === v || o.textContent.trim().toLowerCase() === v.trim().toLowerCase());
      el.value = opt ? opt.value : v;
      el.dispatchEvent(new Event('change', { bubbles: true }));
    } else if (type === 'checkbox') {
      const want = /^(true|on|yes|1|checked|enable[d]?)$/i.test(v);
      if (el.checked !== want) el.click();
    } else if (type === 'radio') {
      el.checked = true;
      el.dispatchEvent(new Event('change', { bubbles: true }));
      el.click();
    } else if (type === 'range') {
      _setNative(el, v);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    } else if (type === 'file') {
      return `can't set a file picker programmatically -- ask the user to drop the file`;
    } else {
      el.focus();
      _setNative(el, v);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return `set "${_labelFor(el) || ref}" = ${v.slice(0, 60)}`;
  } catch (e) { return `set_field failed: ${e.message}`; }
}

async function _click(ref) {
  const el = _findRef(ref);
  if (!el) return `no element with ref ${ref} (re-read the screen)`;
  if (el.disabled) return `element ${ref} is disabled`;
  try {
    el.scrollIntoView({ block: 'center', behavior: 'instant' in el ? 'instant' : 'auto' });
  } catch (_) {}
  const label = (el.textContent || _labelFor(el) || ref).trim().slice(0, 50);
  el.click();
  await _sleep(450);
  return `clicked "${label}"`;
}

// -- chat / panel UI ---------------------------------------------------------
function _renderChat() {
  const logEl = $('mgr-log');
  if (!logEl) return;
  logEl.innerHTML = '';
  for (const m of _chat) {
    const row = document.createElement('div');
    row.className = `mgr-msg mgr-${m.role}`;
    if (m.role === 'thought') row.textContent = m.text;
    else if (m.role === 'act') row.textContent = '› ' + m.text;
    else row.textContent = m.text;
    logEl.appendChild(row);
  }
  logEl.scrollTop = logEl.scrollHeight;
}

function _addMsg(role, text) {
  if (role === 'user' || role === 'manager') _pushChat(role, text);
  // thoughts/acts are ephemeral trace -- shown but not persisted to long memory
  const logEl = $('mgr-log');
  if (!logEl) return;
  const row = document.createElement('div');
  row.className = `mgr-msg mgr-${role}`;
  if (role === 'act') row.textContent = '› ' + text;
  else row.textContent = text;
  logEl.appendChild(row);
  logEl.scrollTop = logEl.scrollHeight;
}

function _setStatus(text) {
  const s = $('mgr-status');
  if (s) s.textContent = text || '';
}

function _showPanel(show) {
  const p = $('mgr-panel');
  if (p) p.hidden = !show;
}

function _setRunning(on) {
  _running = on;
  const send = $('mgr-send'), stop = $('mgr-stop'), input = $('mgr-input');
  if (send) send.hidden = on;
  if (stop) stop.hidden = !on;
  if (input) input.disabled = on;
  $('manager')?.classList.toggle('mgr-busy', on);
  if (!on) _setStatus('');
}

// ask-the-user pause
function _waitForReply() { return new Promise(res => { _askResolver = res; }); }
function _showAsk(show) {
  const a = $('mgr-ask');
  if (a) a.hidden = !show;
  if (show) setTimeout(() => $('mgr-ask-input')?.focus(), 30);
}
function _submitAsk() {
  const inp = $('mgr-ask-input');
  const txt = (inp?.value || '').trim();
  if (!txt || !_askResolver) return;
  inp.value = '';
  _showAsk(false);
  const r = _askResolver; _askResolver = null;
  r(txt);
}

// -- the agent loop ----------------------------------------------------------
async function runGoal(goal) {
  if (_running) return;
  _stop = false;
  _addMsg('user', goal);
  _showPanel(true);
  _setRunning(true);
  _setStatus('Thinking…');

  const history = [];
  let screen = readScreen();
  let lastSig = '';
  let repeat = 0;

  try {
    for (let step = 0; step < MAX_STEPS; step++) {
      if (_stop) { _addMsg('manager', 'Stopped.'); break; }

      let res;
      try {
        res = await apiFetch('/api/manager/think', {
          method: 'POST',
          body: JSON.stringify({ goal, screen, history, chat: _chat.slice(-8) }),
          silent: true,
        });
      } catch (e) {
        _addMsg('manager', `I hit an error reaching my brain: ${e.message}`);
        break;
      }

      const thought = (res.thought || '').trim();
      const action = res.action || {};
      const type = action.type;
      if (thought) _addMsg('thought', thought);

      // loop guard: identical action repeated 3x
      const sig = JSON.stringify(action);
      if (sig === lastSig) { repeat++; } else { repeat = 0; lastSig = sig; }
      if (repeat >= 2) {
        _addMsg('manager', "I seem to be stuck repeating myself, so I'll stop. Try rephrasing or tell me the next step.");
        break;
      }

      let result = '';
      if (type === 'navigate') {
        _setStatus(`Opening ${action.tab}…`);
        _addMsg('act', `open ${action.tab}`);
        result = await _navigate(action.tab);
        screen = readScreen();
      } else if (type === 'read_screen') {
        _setStatus('Reading the screen…');
        screen = readScreen();
        result = `screen: ${screen.controls.length} controls, ${screen.buttons.length} buttons on "${screen.title}"`;
      } else if (type === 'set_field') {
        _setStatus('Filling a field…');
        result = _setField(action.ref, action.value);
        _addMsg('act', result);
        await _sleep(120);
        screen = readScreen();
      } else if (type === 'click') {
        _setStatus('Clicking…');
        result = await _click(action.ref);
        _addMsg('act', result);
        screen = readScreen();
      } else if (type === 'say') {
        _addMsg('manager', action.message || '…');
        result = 'told the user';
      } else if (type === 'ask') {
        _addMsg('manager', action.question || 'I need a bit more info.');
        _setStatus('Waiting for you…');
        _showAsk(true);
        const reply = await _waitForReply();
        if (_stop || reply == null) { _addMsg('manager', 'Stopped.'); break; }
        _addMsg('user', reply);
        result = `user replied: ${reply}`;
        // keep screen as-is
      } else if (type === 'done') {
        if (action.summary) _addMsg('manager', action.summary);
        result = 'done';
        history.push({ action, result });
        break;
      } else {
        // unknown action -- treat its payload as a narration
        _addMsg('manager', action.message || JSON.stringify(action));
        result = 'unknown action';
      }

      history.push({ action, result });
      _setStatus('Thinking…');
      await _sleep(150);

      if (step === MAX_STEPS - 1) {
        _addMsg('manager', "I've taken a lot of steps without finishing — pausing so I don't run away. Tell me how to continue.");
      }
    }
  } finally {
    _setRunning(false);
    _showAsk(false);
    _askResolver = null;
  }
}

function _onSend() {
  const inp = $('mgr-input');
  const goal = (inp?.value || '').trim();
  if (!goal || _running) return;
  inp.value = '';
  runGoal(goal);
}

function _onStop() {
  _stop = true;
  _setStatus('Stopping…');
  if (_askResolver) { const r = _askResolver; _askResolver = null; r(null); }
}

export function initManager() {
  if (!$('manager')) return;             // header markup absent -- nothing to wire
  _renderChat();
  $('mgr-send')?.addEventListener('click', _onSend);
  $('mgr-stop')?.addEventListener('click', _onStop);
  $('mgr-input')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _onSend(); }
  });
  $('mgr-ask-send')?.addEventListener('click', _submitAsk);
  $('mgr-ask-input')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _submitAsk(); }
  });
  // Toggle the transcript panel by clicking the spark, and a clear-history affordance.
  $('mgr-spark')?.addEventListener('click', () => {
    const p = $('mgr-panel');
    if (p) p.hidden = !p.hidden;
  });
  $('mgr-clear')?.addEventListener('click', () => {
    _chat = []; _saveChat(); _renderChat();
  });
}
