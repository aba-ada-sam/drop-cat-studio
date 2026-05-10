/**
 * tab-adobe.js -- Adobe Creative Suite Agent tab
 *
 * UI flow:
 *  1. Status pills show whether Premiere / AE panels are connected
 *  2. User types a goal in plain English and clicks "Make a Plan"
 *  3. AI generates a step-by-step plan displayed as readable cards
 *  4. User clicks "Run Plan" -- each step updates live with pass/fail
 *  5. Results summary at the bottom
 */

import { apiFetch, toast }          from './shell/toast.js?v=20260503a';
import { el }                       from './components.js?v=20260507a';
import { pollJob }                  from './api.js?v=20260505e';

let _panel    = null;
let _tasks    = [];      // current planned task list
let _jobId    = null;
let _polling  = false;

// ── Init ─────────────────────────────────────────────────────────────────────

export function init(panel) {
  _panel = panel;
  _render();
  _checkStatus();
  // Re-check connection status every 10s
  setInterval(_checkStatus, 10000);
}

// ── Status ────────────────────────────────────────────────────────────────────

async function _checkStatus() {
  try {
    const s = await apiFetch('/api/adobe/status', { context: 'adobeStatus' });
    _updatePill('pr',  s.premiere?.connected,     'Premiere Pro');
    _updatePill('ae',  s.aftereffects?.connected, 'After Effects');
  } catch (_) {
    _updatePill('pr',  false, 'Premiere Pro');
    _updatePill('ae',  false, 'After Effects');
  }
}

function _updatePill(id, connected, label) {
  const pill = _panel.querySelector(`#adobe-pill-${id}`);
  if (!pill) return;
  const dot  = pill.querySelector('.adobe-dot');
  const txt  = pill.querySelector('.adobe-pill-label');
  dot.className = 'adobe-dot ' + (connected ? 'connected' : 'disconnected');
  txt.textContent = label + (connected ? '' : ' -- panel not open');
}

// ── Render ────────────────────────────────────────────────────────────────────

function _render() {
  _panel.innerHTML = '';
  _panel.style.cssText = 'display:flex;flex-direction:column;height:100%;overflow:hidden;padding:20px 24px;gap:16px;';

  // -- Header --
  const header = el('div', { style: 'flex-shrink:0' });
  header.innerHTML = `
    <h2 style="font-size:1.1rem;color:var(--gold);margin-bottom:4px">Adobe Creative Suite</h2>
    <p style="font-size:.8rem;color:var(--text-muted)">
      Describe what you want to do in Premiere Pro or After Effects.
      The AI will plan and execute it for you.
    </p>`;
  _panel.appendChild(header);

  // -- Connection pills --
  const pills = el('div', { style: 'display:flex;gap:8px;flex-shrink:0;flex-wrap:wrap' });
  pills.appendChild(_makePill('pr',  'Premiere Pro'));
  pills.appendChild(_makePill('ae',  'After Effects'));
  _panel.appendChild(pills);

  const pillNote = el('p', {
    style: 'font-size:.72rem;color:var(--text-muted);margin-top:-8px;flex-shrink:0'
  });
  pillNote.textContent = 'Open both apps and enable the panel: Window > Extensions > DropCat Adobe Agent';
  _panel.appendChild(pillNote);

  // -- Goal input --
  const goalWrap = el('div', { style: 'flex-shrink:0' });
  goalWrap.innerHTML = `
    <label style="display:block;font-size:.8rem;color:var(--text-muted);margin-bottom:6px">
      What do you want to do?
    </label>
    <textarea id="adobe-goal"
      placeholder="e.g. Add a cross dissolve between every clip on the main timeline, then lower the audio on clip 3 by 6 dB"
      style="width:100%;height:90px;resize:vertical;background:var(--surface-2);border:1px solid var(--border-1);
             border-radius:6px;padding:10px 12px;font-size:.85rem;color:var(--text);
             font-family:inherit;line-height:1.5"></textarea>`;
  _panel.appendChild(goalWrap);

  // -- Plan button --
  const planBtn = el('button', {
    id: 'adobe-plan-btn',
    className: 'btn btn-primary',
    style: 'align-self:flex-start;flex-shrink:0',
    textContent: 'Make a Plan',
    onclick: _onPlan,
  });
  _panel.appendChild(planBtn);

  // -- Scrollable lower area: plan + results --
  const scroll = el('div', {
    style: 'flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:12px;min-height:0'
  });

  // Plan preview
  const planSection = el('div', { id: 'adobe-plan-section', style: 'display:none' });
  planSection.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
      <h3 style="font-size:.85rem;color:var(--gold)">Planned Steps</h3>
      <button id="adobe-run-btn" class="btn btn-primary btn-sm">Run Plan</button>
    </div>
    <div id="adobe-plan-list" style="display:flex;flex-direction:column;gap:6px"></div>`;
  scroll.appendChild(planSection);

  // Results summary
  const resultsSection = el('div', { id: 'adobe-results-section', style: 'display:none' });
  resultsSection.innerHTML = `
    <h3 style="font-size:.85rem;color:var(--gold);margin-bottom:8px">Results</h3>
    <div id="adobe-results-list" style="display:flex;flex-direction:column;gap:4px"></div>
    <div id="adobe-summary" style="margin-top:10px;font-size:.8rem;color:var(--text-muted)"></div>`;
  scroll.appendChild(resultsSection);

  _panel.appendChild(scroll);

  // Wire run button (after DOM exists)
  setTimeout(() => {
    const runBtn = _panel.querySelector('#adobe-run-btn');
    if (runBtn) runBtn.onclick = _onRun;
  }, 0);
}

function _makePill(id, label) {
  const pill = el('div', {
    id: `adobe-pill-${id}`,
    style: 'display:flex;align-items:center;gap:7px;padding:5px 12px;' +
           'background:var(--surface-2);border:1px solid var(--border-1);border-radius:20px;' +
           'font-size:.75rem;color:var(--text-muted)'
  });
  const dot = el('span', { className: 'adobe-dot disconnected' });
  const lbl = el('span', { className: 'adobe-pill-label', textContent: label });
  pill.appendChild(dot);
  pill.appendChild(lbl);
  return pill;
}

// ── Plan ─────────────────────────────────────────────────────────────────────

async function _onPlan() {
  const goal = (_panel.querySelector('#adobe-goal')?.value || '').trim();
  if (!goal) { toast('Describe what you want to do first', 'warning'); return; }

  const btn = _panel.querySelector('#adobe-plan-btn');
  btn.disabled = true;
  btn.textContent = 'Planning...';
  _hidePlan();
  _hideResults();

  try {
    const data = await apiFetch('/api/adobe/plan',
      { method: 'POST', body: JSON.stringify({ goal }), context: 'adobePlan' });

    if (!data.ok || !data.tasks?.length) {
      toast('Could not generate a plan -- try rephrasing', 'error');
      return;
    }

    _tasks = data.tasks;
    _showPlan(_tasks);
    toast(`Plan ready: ${_tasks.length} step${_tasks.length !== 1 ? 's' : ''}`, 'success');
  } catch (_) {
    toast('Planning failed -- check AI connection in Settings', 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Make a Plan';
  }
}

function _showPlan(tasks) {
  const section = _panel.querySelector('#adobe-plan-section');
  const list    = _panel.querySelector('#adobe-plan-list');
  if (!section || !list) return;
  list.innerHTML = '';

  tasks.forEach((task, i) => {
    const label  = task.label || `${task.app}.${task.op}`;
    const appTag = _appTag(task.app);
    const card   = el('div', {
      id: `adobe-step-${i}`,
      style: 'display:flex;align-items:flex-start;gap:10px;padding:8px 12px;' +
             'background:var(--surface-2);border:1px solid var(--border-1);border-radius:6px'
    });
    card.innerHTML = `
      <span style="flex-shrink:0;width:20px;height:20px;border-radius:50%;background:var(--border-2);
                   display:flex;align-items:center;justify-content:center;font-size:.7rem;
                   color:var(--text-muted);margin-top:1px">${i + 1}</span>
      ${appTag}
      <span style="font-size:.82rem;color:var(--text);line-height:1.4">${escHtml(label)}</span>`;
    list.appendChild(card);
  });

  section.style.display = '';
}

function _hidePlan() {
  const s = _panel.querySelector('#adobe-plan-section');
  if (s) s.style.display = 'none';
}

// ── Run ──────────────────────────────────────────────────────────────────────

async function _onRun() {
  if (!_tasks.length) { toast('Generate a plan first', 'warning'); return; }
  if (_polling) return;

  const runBtn = _panel.querySelector('#adobe-run-btn');
  if (runBtn) { runBtn.disabled = true; runBtn.textContent = 'Running...'; }

  _hideResults();
  _resetStepIcons();

  try {
    const data = await apiFetch('/api/adobe/run',
      { method: 'POST', body: JSON.stringify({ tasks: _tasks }), context: 'adobeRun' });

    if (!data.ok || !data.job_id) {
      toast('Could not start run -- try again', 'error');
      if (runBtn) { runBtn.disabled = false; runBtn.textContent = 'Run Plan'; }
      return;
    }

    _jobId   = data.job_id;
    _polling = true;
    _pollProgress(_jobId);

  } catch (_) {
    if (runBtn) { runBtn.disabled = false; runBtn.textContent = 'Run Plan'; }
  }
}

function _pollProgress(jobId) {
  pollJob(
    jobId,
    (job) => {
      const results = job.meta?.results || [];
      results.forEach((r, i) => _updateStepIcon(i, r.status));
    },
    (job) => {
      _polling = false;
      const runBtn = _panel.querySelector('#adobe-run-btn');
      if (runBtn) { runBtn.disabled = false; runBtn.textContent = 'Run Plan'; }
      const results = job.meta?.results || [];
      results.forEach((r, i) => _updateStepIcon(i, r.status));
      _showResults(results);
      const fails = results.filter(r => r.status === 'error').length;
      toast(fails ? `Done with ${fails} error${fails > 1 ? 's' : ''}` : 'All steps complete', fails ? 'warning' : 'success');
    },
    (job) => {
      _polling = false;
      const runBtn = _panel.querySelector('#adobe-run-btn');
      if (runBtn) { runBtn.disabled = false; runBtn.textContent = 'Run Plan'; }
      const results = job.meta?.results || [];
      results.forEach((r, i) => _updateStepIcon(i, r.status));
      _showResults(results);
      toast('Run stopped: ' + (job.error || 'unknown error'), 'error');
    },
  );
}

// ── Step icon updates ─────────────────────────────────────────────────────────

const _STATUS_ICON = {
  ok:      { symbol: 'OK',   color: '#4caf50' },
  error:   { symbol: 'Fail', color: '#e55'    },
  skipped: { symbol: 'Skip', color: '#888'    },
};

function _resetStepIcons() {
  _tasks.forEach((_, i) => {
    const bullet = _panel.querySelector(`#adobe-step-${i} span:first-child`);
    if (bullet) {
      bullet.style.background = 'var(--border-2)';
      bullet.style.color      = 'var(--text-muted)';
      bullet.textContent      = String(i + 1);
    }
  });
}

function _updateStepIcon(i, status) {
  const info   = _STATUS_ICON[status];
  if (!info) return;
  const bullet = _panel.querySelector(`#adobe-step-${i} span:first-child`);
  if (!bullet) return;
  bullet.style.background = info.color;
  bullet.style.color      = '#fff';
  bullet.textContent      = info.symbol;
}

// ── Results panel ────────────────────────────────────────────────────────────

function _showResults(results) {
  const section = _panel.querySelector('#adobe-results-section');
  const list    = _panel.querySelector('#adobe-results-list');
  const summary = _panel.querySelector('#adobe-summary');
  if (!section || !list || !summary) return;

  list.innerHTML = '';
  results.forEach((r) => {
    if (r.status === 'error') {
      const row = el('div', {
        style: 'font-size:.78rem;color:#e77;padding:4px 8px;background:rgba(238,85,85,.08);' +
               'border-radius:4px;border-left:2px solid #e55'
      });
      row.textContent = `Step ${r.step} (${r.label || ''}): ${r.error}`;
      list.appendChild(row);
    }
  });

  const ok   = results.filter(r => r.status === 'ok').length;
  const fail = results.filter(r => r.status === 'error').length;
  const skip = results.filter(r => r.status === 'skipped').length;
  summary.textContent = `${ok} succeeded  ${fail} failed  ${skip} skipped`;
  section.style.display = '';
}

function _hideResults() {
  const s = _panel.querySelector('#adobe-results-section');
  if (s) s.style.display = 'none';
}

// ── Util ──────────────────────────────────────────────────────────────────────

function _appTag(app) {
  const isPr = (app || '').toLowerCase().startsWith('prem') || app === 'pr' || app === 'ppro';
  const label = isPr ? 'Pr' : 'Ae';
  const bg    = isPr ? '#2a4a8a' : '#1a3a6a';
  return `<span style="flex-shrink:0;padding:1px 6px;border-radius:3px;font-size:.68rem;
                font-weight:700;background:${bg};color:#8ab4f8;margin-top:2px">${label}</span>`;
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
