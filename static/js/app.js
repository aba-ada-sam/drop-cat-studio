/**
 * Drop Cat Go Studio -- Main shell controller.
 * Owns: tab routing, service polling, split pane, gallery,
 *       command palette, keyboard shortcuts, modals, settings.
 */

// tab-imports.js removed — import is handled per-tab
import { init as initFunVideos, receiveHandoff as funHandoff } from './tab-fun-videos.js?v=20260426a';
import { init as initBridges,   receiveHandoff as bridgesHandoff } from './tab-bridges.js?v=20260423d';
import { init as initSdPrompts, receiveHandoff as sdPromptsHandoff } from './tab-sd-prompts.js?v=20260423e';
import { init as initPipeline  } from './tab-pipeline.js?v=20260422f';
import { init as initVideoTools, initBatch as initVideoToolsBatch } from './panel-video-tools.js?v=20260422h';
import { consumeHandoff } from './handoff.js?v=20260422a';
import { toast, apiFetch, openErrorLog } from './shell/toast.js?v=20260421c';
import { init as initGallery, refresh as refreshGallery } from './shell/gallery.js?v=20260422m';
import { open as openPalette, close as closePalette, registerItems } from './shell/command-palette.js?v=20260421c';
import './shell/ai-intent.js?v=20260421c';
import { register as registerShortcut, getShortcuts } from './shell/shortcuts.js?v=20260421c';
import { init as initPresets, promptAndSave as savePreset } from './shell/presets.js?v=20260421c';

// ── Tab module map ──────────────────────────────────────────────────────────
const TAB_INIT = {
  'pipeline':          initPipeline,
  'fun-videos':        initFunVideos,
  'bridges':           initBridges,
  'sd-prompts':        initSdPrompts,
  'video-tools':       initVideoTools,
  'video-tools-batch': initVideoToolsBatch,
};
const TAB_HANDOFF = {
  'fun-videos':        funHandoff,
  'bridges':           bridgesHandoff,
  'sd-prompts':        sdPromptsHandoff,
  'video-tools':       null,
  'video-tools-batch': null,
};
const _tabInitialized = new Set();

// ── Pipeline breadcrumb ───────────────────────────────────────────────────────
// Injected once into each of the 4 pipeline panels so users always know
// where they are and what comes next.
const PIPELINE_STEPS = [
  { id: 'sd-prompts',        num: '01', label: 'Generate Images'  },
  { id: 'fun-videos',        num: '02', label: 'Create Videos'    },
  { id: 'video-tools',       num: '03', label: 'Audio'            },
  { id: 'video-tools-batch', num: '04', label: 'Batch Processing' },
];

function _buildPipelineBar(activeTabId) {
  const bar = document.createElement('div');
  bar.className = 'pipeline-bar';

  // Step pills
  const pills = document.createElement('div');
  pills.className = 'pipeline-bar-pills';
  PIPELINE_STEPS.forEach((step, i) => {
    if (i > 0) {
      const sep = document.createElement('span');
      sep.className = 'pipeline-bar-sep';
      sep.textContent = '\u2192';
      pills.appendChild(sep);
    }
    const pill = document.createElement('button');
    pill.className = 'pipeline-bar-pill' + (step.id === activeTabId ? ' active' : '');
    pill.title = `Go to Step ${step.num}: ${step.label}`;
    pill.innerHTML = `<span class="pipeline-bar-num">${step.num}</span><span class="pipeline-bar-pill-label">${step.label}</span>`;
    pill.addEventListener('click', () => switchTab(step.id));
    pills.appendChild(pill);
  });
  bar.appendChild(pills);

  // Next-step shortcut (only shown if there's a next step)
  const curIdx = PIPELINE_STEPS.findIndex(s => s.id === activeTabId);
  const next = PIPELINE_STEPS[curIdx + 1];
  if (next) {
    const nextBtn = document.createElement('button');
    nextBtn.className = 'pipeline-bar-next btn btn-sm';
    nextBtn.title = `Go to Step ${next.num}: ${next.label}`;
    nextBtn.textContent = `${next.num} ${next.label} \u2192`;
    nextBtn.addEventListener('click', () => switchTab(next.id));
    bar.appendChild(nextBtn);
  } else {
    // On the last step, show a subtle "done" indicator
    const done = document.createElement('span');
    done.className = 'pipeline-bar-done';
    done.textContent = '\u2713 Final step';
    bar.appendChild(done);
  }

  return bar;
}

// ── Splash ──────────────────────────────────────────────────────────────────
const SPLASH_BLOCKING_STATES = new Set(['unknown']);
const SPLASH_LOADING_STATES  = new Set(['unknown', 'starting']);

function svcStateToCheck(state) {
  if (state === 'running') return 'ok';
  if (state === 'ready')   return 'ok';
  if (state === 'error')   return 'err';
  if (SPLASH_LOADING_STATES.has(state)) return 'loading';
  return 'warn';
}

const SVC_SPLASH_TEXT = {
  forge:   { running: 'Forge SD running', starting: 'Forge SD starting...', not_running: 'Forge SD not detected', not_configured: 'Forge not configured', unknown: 'Checking Forge SD...' },
  wangp:   { running: 'WanGP ready', starting: 'WanGP loading model...', ready: 'WanGP configured', not_configured: 'WanGP not configured', not_running: 'WanGP not running', error: 'WanGP error', unknown: 'Checking WanGP...' },
  acestep: { running: 'ACE-Step running', ready: 'ACE-Step ready', starting: 'ACE-Step starting...', not_configured: 'ACE-Step not configured', not_running: 'ACE-Step not running', error: 'ACE-Step error', unknown: 'Checking ACE-Step...' },
};

function safeStorage(fn, fallback = undefined) {
  try { return fn(); } catch (_) { return fallback; }
}

async function runSplash() {
  const splash = document.getElementById('splash');
  const app    = document.getElementById('app');
  const isFirstVisit = safeStorage(() => !localStorage.getItem('dropcat_visited'), true);

  function setCheck(id, state, text) {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = `splash-check ${state}`;
    const dot = el.querySelector('.chk-dot');
    const span = el.querySelector('.chk-text');
    if (dot)  dot.className = 'chk-dot';
    if (span) span.textContent = text;
    else if (el.childNodes[1]) el.childNodes[1].textContent = text;
  }

  function exitSplash() {
    window.dispatchEvent(new Event('dcs:ready'));
    splash.classList.add('fade-out');
    setTimeout(() => {
      splash.style.display = 'none';
      app.style.display = 'flex';
    }, 500);
    if (isFirstVisit) {
      safeStorage(() => localStorage.setItem('dropcat_visited', '1'));
      setTimeout(() => openModal('modal-help'), 600);
    }
  }

  const loadAnywayDiv = document.getElementById('splash-load-anyway');
  const loadAnywayBtn = document.getElementById('btn-load-anyway');
  let pollInterval = null;
  let settled = false;

  const _hatAdvanced = new Set();
  const HAT_TOTAL = 6;
  function advanceHatOnce(key) {
    if (_hatAdvanced.has(key)) return;
    _hatAdvanced.add(key);
    const bar = document.getElementById('hat-fill-bar');
    if (bar) bar.style.height = Math.round(_hatAdvanced.size / HAT_TOTAL * 100) + '%';
  }

  const _splashT0 = Date.now();
  const ENTRANCE_MS = 900;

  function doExit() {
    if (settled) return;
    settled = true;
    if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
    const bar = document.getElementById('hat-fill-bar');
    if (bar) bar.style.height = '100%';
    if (!document.querySelector('.splash-logo-wrap')) { exitSplash(); return; }
    const remaining = Math.max(0, ENTRANCE_MS - (Date.now() - _splashT0));
    setTimeout(() => {
      document.getElementById('eye-left')?.classList.add('blinking');
      document.getElementById('eye-right')?.classList.add('blinking');
      setTimeout(() => {
        document.getElementById('cheshire-smile')?.classList.add('smiling');
        setTimeout(exitSplash, 750);
      }, 600);
    }, remaining + 200);
  }

  loadAnywayBtn?.addEventListener('click', doExit);
  const loadAnywayTimer = setTimeout(() => {
    if (!settled && loadAnywayDiv) loadAnywayDiv.classList.remove('hidden');
  }, 5000);
  setTimeout(() => { if (!settled) doExit(); }, 90000);

  try {
    ['chk-server','chk-ffmpeg','chk-ollama','chk-forge','chk-wangp','chk-acestep'].forEach(id => {
      const el = document.getElementById(id);
      const current = el?.querySelector('.chk-text')?.textContent || '';
      setCheck(id, 'loading', current);
    });

    setCheck('chk-server', 'loading', 'Connecting...');
    const _ctrl = new AbortController();
    const _tid  = setTimeout(() => _ctrl.abort(), 12000);
    const sys = await fetch('/api/system', { signal: _ctrl.signal }).then(r => r.json());
    clearTimeout(_tid);
    setCheck('chk-server', 'ok', 'Server running');
    advanceHatOnce('server');

    setCheck('chk-ffmpeg', sys.ffmpeg ? 'ok' : 'warn', sys.ffmpeg ? 'ffmpeg ready' : 'ffmpeg not found');
    advanceHatOnce('ffmpeg');

    const ol = sys.ollama || {};
    if (ol.available) {
      setCheck('chk-ollama', 'ok', `Ollama ready -- ${(ol.models || []).join(', ') || 'models loaded'}`);
    } else {
      setCheck('chk-ollama', 'err', 'Ollama not running');
    }
    advanceHatOnce('ollama');

    function updateServiceChecks(svcs) {
      const map = { forge: 'chk-forge', wangp: 'chk-wangp', acestep: 'chk-acestep' };
      for (const [name, id] of Object.entries(map)) {
        const info  = svcs[name] || {};
        const state = info.state || 'unknown';
        const text  = (SVC_SPLASH_TEXT[name] || {})[state] || info.message || state;
        setCheck(id, svcStateToCheck(state), text);
        if (!SPLASH_BLOCKING_STATES.has(state)) advanceHatOnce(name);
      }
    }

    function allSettled(svcs) {
      return ['forge','wangp','acestep'].every(n => !SPLASH_BLOCKING_STATES.has((svcs[n] || {}).state || 'unknown'));
    }

    updateServiceChecks(sys.services || {});
    const minShowUntil = Date.now() + 1200;

    function tryExit(svcs) {
      if (!allSettled(svcs)) return;
      const wait = Math.max(0, minShowUntil - Date.now());
      setTimeout(() => { clearTimeout(loadAnywayTimer); doExit(); }, wait);
    }
    tryExit(sys.services || {});

    pollInterval = setInterval(async () => {
      if (settled) return;
      try {
        const svcs = await fetch('/api/services').then(r => r.json());
        updateServiceChecks(svcs);
        tryExit(svcs);
      } catch (_) {}
    }, 2000);
  } catch (e) {
    clearTimeout(loadAnywayTimer);
    if (pollInterval) clearInterval(pollInterval);
    document.getElementById('splash-error')?.classList.remove('hidden');
    const errMsg = document.querySelector('.splash-err-msg');
    if (errMsg) errMsg.textContent = 'Cannot connect. Make sure launch.bat is running.';
  }
}

// ── State ───────────────────────────────────────────────────────────────────
const state = {
  activeTab:    'sd-prompts',
  logOpen:      false,
  logSeq:       0,
  config:       {},
  galleryOpen:  false,
};

// ── Tab routing ─────────────────────────────────────────────────────────────
function switchTab(tabId) {
  state.activeTab = tabId;

  // Update rail buttons (Gallery has no data-tab so it's handled separately)
  document.querySelectorAll('.rail-tab[data-tab]').forEach(btn => {
    const active = btn.dataset.tab === tabId;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-current', active ? 'page' : 'false');
  });
  // Keep gallery button state in sync with overlay state
  document.getElementById('btn-gallery-rail')?.classList.toggle('active', state.galleryOpen);

  // Update panels
  document.querySelectorAll('.tab-panel').forEach(panel => {
    panel.classList.toggle('active', panel.id === `panel-${tabId}`);
  });

  // Initialize on first visit
  if (!_tabInitialized.has(tabId) && TAB_INIT[tabId]) {
    const panel = document.getElementById(`panel-${tabId}`);
    if (panel) {
      try {
        TAB_INIT[tabId](panel);
        _tabInitialized.add(tabId);
        // Inject pipeline position bar into the 4 main pipeline steps
        if (PIPELINE_STEPS.some(s => s.id === tabId)) {
          panel.prepend(_buildPipelineBar(tabId));
        }
      } catch (err) {
        console.error(`[${tabId}] Init failed:`, err);
        panel.innerHTML = `<div class="error-banner">Tab failed to load. Refresh.<br><small>${escHtml(err.message)}</small></div>`;
      }
    }
  }

  // Dispatch handoff
  const handoffData = consumeHandoff(tabId);
  if (handoffData && TAB_HANDOFF[tabId]) TAB_HANDOFF[tabId](handoffData);
}

// ── Service polling ─────────────────────────────────────────────────────────
const SERVICE_MESSAGES = {
  acestep: { not_configured: 'Not configured -- set ACE-Step path in Settings', not_running: 'Not running -- set path in Settings' },
  forge:   { not_running: 'Not detected -- start Forge with --api flag', starting: 'Starting (~60s)...', not_configured: 'Not configured' },
  wangp:   { not_configured: 'Not configured -- set WanGP path in Settings', ready: 'Configured -- worker starts on first use' },
};

// Latest service state for service panel
const _svcState = {};

async function pollServices() {
  try {
    const data = await fetch('/api/services').then(r => r.json());
    let anyProblem = false;
    for (const [name, info] of Object.entries(data)) {
      _svcState[name] = info;
      const dotClass = info.state || 'unknown';
      if (!['running','ready','ok'].includes(dotClass)) anyProblem = true;

      const pillDot = document.querySelector(`#pill-${name} .dot`);
      if (pillDot) { pillDot.className = 'dot'; pillDot.classList.add(dotClass); }

      const svcId = name === 'forge' ? 'dot-forge-svc' : `dot-${name}`;
      const svcDot = document.getElementById(svcId);
      if (svcDot) { svcDot.className = 'dot'; svcDot.classList.add(dotClass); }

      const override = SERVICE_MESSAGES[name]?.[info.state];
      const displayMsg = override || info.message || info.state;
      const msgId = name === 'forge' ? 'msg-forge-svc' : `msg-${name}`;
      const msgEl = document.getElementById(msgId);
      if (msgEl) msgEl.textContent = displayMsg;

      const pill = document.getElementById(`pill-${name}`);
      if (pill) pill.title = displayMsg;
    }

    // Update service panel if open
    if (document.getElementById('service-panel-overlay')?.classList.contains('open')) {
      renderServicePanel();
    }
  } catch (_) {}
}

// ── Service panel ───────────────────────────────────────────────────────────
function openServicePanel() {
  document.getElementById('service-panel-overlay')?.classList.add('open');
  renderServicePanel();
}

function closeServicePanel() {
  document.getElementById('service-panel-overlay')?.classList.remove('open');
}

function renderServicePanel() {
  const body = document.getElementById('svc-panel-body');
  if (!body) return;
  body.innerHTML = '';

  const SVC_NAMES = {
    forge:   'Forge SD',
    wangp:   'WanGP',
    acestep: 'ACE-Step',
    ollama:  'Ollama',
  };
  const SVC_HINTS = {
    forge:   'Stable Diffusion image generation',
    wangp:   'AI video generation',
    acestep: 'AI music generation',
    ollama:  'Local LLM for prompt enhancement',
  };

  for (const [name, label] of Object.entries(SVC_NAMES)) {
    const info = _svcState[name] || {};
    const state = info.state || 'unknown';

    const card = document.createElement('div');
    card.className = 'svc-detail-card';

    const latency = info.latency_ms != null ? `${info.latency_ms}ms` : '--';
    const lastCheck = info.last_check ? new Date(info.last_check * 1000).toLocaleTimeString() : '--';
    const model = info.model || info.loaded_model || '--';
    const vram = info.vram_mb ? `${Math.round(info.vram_mb / 1024 * 10) / 10} GB` : '--';
    const logLines = (info.recent_logs || []).join('\n') || '(no recent logs)';

    card.innerHTML = `
      <div class="svc-detail-header">
        <span class="dot ${state}" style="width:10px;height:10px" aria-hidden="true"></span>
        <h3>${label}</h3>
        <span style="font-size:.72rem;color:var(--text-3)">${state}</span>
      </div>
      <div class="svc-detail-body">
        <div class="svc-meta-row"><span>Hint</span><span>${SVC_HINTS[name]}</span></div>
        <div class="svc-meta-row"><span>Latency</span><span>${latency}</span></div>
        <div class="svc-meta-row"><span>Last check</span><span>${lastCheck}</span></div>
        ${model !== '--' ? `<div class="svc-meta-row"><span>Model</span><span>${model}</span></div>` : ''}
        ${vram  !== '--' ? `<div class="svc-meta-row"><span>GPU VRAM</span><span>${vram}</span></div>`  : ''}
        <div class="svc-log-lines">${escHtml(logLines)}</div>
      </div>
      <div class="svc-detail-actions">
        <button class="btn btn-sm svc-start" data-svc="${name}">Start</button>
        <button class="btn btn-sm svc-stop"  data-svc="${name}">Stop</button>
        <button class="btn btn-sm svc-restart" data-svc="${name}">Restart</button>
        ${info.url ? `<a href="${info.url}" target="_blank" class="btn btn-sm">Open UI</a>` : ''}
      </div>`;

    card.querySelectorAll('.svc-start').forEach(b => b.addEventListener('click', () => svcAction('start', b.dataset.svc)));
    card.querySelectorAll('.svc-stop').forEach(b  => b.addEventListener('click', () => svcAction('stop',  b.dataset.svc)));
    card.querySelectorAll('.svc-restart').forEach(b => b.addEventListener('click', () => svcAction('restart', b.dataset.svc)));

    body.appendChild(card);
  }
}

// ── Log polling ─────────────────────────────────────────────────────────────
async function pollLogs() {
  if (!state.logOpen) return;
  try {
    const data = await fetch(`/api/logs?since=${state.logSeq}`).then(r => r.json());
    const container = document.getElementById('log-entries');
    for (const entry of (data.logs || [])) {
      const div = document.createElement('div');
      div.className = `log-entry ${entry.level}`;
      div.innerHTML = `<span class="time">${entry.time}</span>${escHtml(entry.msg)}`;
      container.prepend(div);
      state.logSeq = Math.max(state.logSeq, entry.seq || 0);
    }
  } catch (_) {}
}


// ── Modals ──────────────────────────────────────────────────────────────────
function openModal(id)  { document.getElementById(id)?.classList.add('open');    }
function closeModal(id) { document.getElementById(id)?.classList.remove('open'); }

// ── Settings ────────────────────────────────────────────────────────────────
async function loadConfig() {
  try {
    state.config = await apiFetch('/api/config', { context: 'loadConfig' });
    for (const [key, val] of Object.entries(state.config)) {
      const el = document.getElementById(`cfg-${key}`);
      if (el) el.value = val;
    }
    const llm = await apiFetch('/api/llm/config', { context: 'loadLLM' });
    onLLMProviderChange(llm.provider || 'ollama');
    if (llm.anthropic_key_hint) {
      const h = document.getElementById('anthropic-key-hint');
      if (h) h.textContent = `Current key: ${llm.anthropic_key_hint}`;
    }
  } catch (_) {}
}

function onLLMProviderChange(provider) {
  const useAnthropic = provider === 'anthropic';
  const toggle = document.getElementById('provider-toggle-input');
  if (toggle) toggle.checked = useAnthropic;
  document.getElementById('llm-ollama-section').style.display    = useAnthropic ? 'none' : '';
  document.getElementById('llm-anthropic-section').style.display = useAnthropic ? ''     : 'none';
  ['ollama','anthropic'].forEach(p => {
    const side = document.getElementById(`provider-side-${p}`);
    if (side) {
      side.classList.toggle('active',   p === provider);
      side.classList.toggle('inactive', p !== provider);
    }
  });
}

async function saveSettings() {
  try {
    const fields = ['wan2gp_root','acestep_root','sd_wildcards_dir','ollama_host','ollama_fast_model','ollama_power_model'];
    const body = {};
    for (const key of fields) {
      const el = document.getElementById(`cfg-${key}`);
      if (el) body[key] = el.value;
    }
    await apiFetch('/api/config', { method: 'POST', body: JSON.stringify(body), context: 'saveSettings' });
    const ollamaBody = {};
    for (const k of ['ollama_host','ollama_fast_model','ollama_power_model']) if (body[k] !== undefined) ollamaBody[k] = body[k];
    if (Object.keys(ollamaBody).length) {
      await apiFetch('/api/ollama/config', { method: 'POST', body: JSON.stringify(ollamaBody), context: 'saveOllama' });
    }
    const llmBody = { provider: document.getElementById('provider-toggle-input')?.checked ? 'anthropic' : 'ollama' };
    const anthropicKey = document.getElementById('cfg-anthropic_key')?.value;
    const openaiKey    = document.getElementById('cfg-openai_key')?.value;
    if (anthropicKey) llmBody.anthropic_key = anthropicKey;
    if (openaiKey)    llmBody.openai_key    = openaiKey;
    await apiFetch('/api/llm/config', { method: 'POST', body: JSON.stringify(llmBody), context: 'saveLLM' });
    closeModal('modal-settings');
    toast('Settings saved', 'success');
    loadConfig();
  } catch (_) {}
}

async function loadOllamaModels() {
  try {
    const data = await apiFetch('/api/ollama/models', { context: 'loadModels' });
    const models = data.models || [];
    for (const selId of ['cfg-ollama_fast_model','cfg-ollama_power_model']) {
      const sel = document.getElementById(selId);
      if (!sel) continue;
      const current = sel.value || state.config?.[selId.replace('cfg-', '')] || '';
      sel.innerHTML = models.map(m => `<option value="${m}"${m === current ? ' selected' : ''}>${m}</option>`).join('') || '<option value="">No models found</option>';
    }
  } catch (_) {}
}

async function validatePath(type) {
  const key = type === 'wan' ? 'wan2gp_root' : 'acestep_root';
  const path = document.getElementById(`cfg-${key}`)?.value || '';
  const endpoint = type === 'wan' ? '/api/config/validate-wangp' : '/api/config/validate-acestep';
  try {
    const data = await apiFetch(endpoint, { method: 'POST', body: JSON.stringify({ path }), context: 'validatePath' });
    const msgEl = document.getElementById(`val-${type}`);
    if (msgEl) { msgEl.textContent = data.message || ''; msgEl.className = `validation-msg ${data.ok ? 'ok' : 'err'}`; }
  } catch (_) {}
}

// ── Service actions ─────────────────────────────────────────────────────────
async function svcAction(action, name) {
  const label = action === 'start' ? 'Starting' : action === 'stop' ? 'Stopping' : 'Restarting';
  toast(`${label} ${name}...`, 'info');
  try {
    const data = await apiFetch(`/api/services/${action}/${name}`, { method: 'POST', context: `svc.${action}.${name}` });
    toast(data.message || `${name} ${action} initiated`, 'success');
  } catch (_) {}
  setTimeout(pollServices, 1000);
}

// ── Gallery overlay ──────────────────────────────────────────────────────────
function _galleryOpen() {
  const ov  = document.getElementById('gallery-overlay');
  if (!ov) return;
  ov.setAttribute('aria-hidden', 'false');
  document.getElementById('btn-gallery-rail')?.classList.add('active');
  state.galleryOpen = true;
  refreshGallery();
}

function _galleryClose() {
  const ov  = document.getElementById('gallery-overlay');
  if (!ov) return;
  ov.setAttribute('aria-hidden', 'true');
  document.getElementById('btn-gallery-rail')?.classList.remove('active');
  state.galleryOpen = false;
}

function _galleryToggle() {
  state.galleryOpen ? _galleryClose() : _galleryOpen();
}

// ── Rail collapse ─────────────────────────────────────────────────────────────
function initRailToggle() {
  const rail = document.getElementById('app-rail');
  const btn  = document.getElementById('rail-toggle');
  if (!rail || !btn) return;
  const saved = safeStorage(() => localStorage.getItem('dropcat_rail_collapsed'), 'false');
  if (saved === 'true') rail.classList.add('collapsed');

  btn.addEventListener('click', () => {
    rail.classList.toggle('collapsed');
    safeStorage(() => localStorage.setItem('dropcat_rail_collapsed', String(rail.classList.contains('collapsed'))));
  });
}

// ── Keyboard shortcuts registration ─────────────────────────────────────────
function initShortcuts() {
  const SHORTCUTS = [
    { key: 'k', ctrl: true, global: true, action: () => openPalette(), description: 'Command palette' },
    { key: '?',  shift: true, action: () => openModal('modal-help'), description: 'Keyboard shortcuts' },
    { key: 'Escape', global: true, action: () => {
      closePalette();
      closeModal('modal-settings');
      closeModal('modal-help');
      closeServicePanel();
      document.getElementById('gallery-detail-overlay')?.classList.remove('open');
      document.getElementById('error-log-overlay')?.classList.remove('open');
    }, description: 'Close / cancel' },
    { key: '0', action: () => switchTab('pipeline'),   description: 'Studio Home' },
    { key: '1', action: () => switchTab('sd-prompts'),  description: 'Generate Images (Step 01)' },
    { key: '2', action: () => switchTab('fun-videos'),  description: 'Create Videos (Step 02)' },
    { key: '3', action: () => switchTab('video-tools'),       description: 'Audio (Step 03)' },
    { key: '4', action: () => switchTab('video-tools-batch'), description: 'Batch Processing (Step 04)' },
    { key: 'E', ctrl: true, shift: true, global: true, action: openErrorLog, description: 'Error log' },
    { key: 's', ctrl: true, global: true, action: () => savePreset(state.activeTab), description: 'Save preset' },
  ];

  for (const s of SHORTCUTS) registerShortcut(s);

  // Populate shortcut cheat sheet
  const display = document.getElementById('shortcuts-display');
  if (display) {
    display.innerHTML = '';
    for (const s of SHORTCUTS) {
      const row = document.createElement('div');
      row.className = 'shortcut-row';
      const keyCombo = [s.ctrl && 'Ctrl', s.shift && 'Shift', s.alt && 'Alt', s.key].filter(Boolean).join('+');
      row.innerHTML = `<span>${s.description}</span><span class="shortcut-keys"><kbd>${escHtml(keyCombo)}</kbd></span>`;
      display.appendChild(row);
    }
  }
}

// ── Command palette items ─────────────────────────────────────────────────────
function initPaletteItems() {
  registerItems([
    { label: 'Studio Home',      group: 'Tabs',   hint: '0', action: () => switchTab('pipeline') },
    { label: 'Generate Images',  group: 'Tabs',   hint: '1', action: () => switchTab('sd-prompts') },
    { label: 'Create Videos',    group: 'Tabs',   hint: '2', action: () => switchTab('fun-videos') },
    { label: 'Audio',            group: 'Tabs',   hint: '3', action: () => switchTab('video-tools') },
    { label: 'Batch Processing', group: 'Tabs',   hint: '4', action: () => switchTab('video-tools-batch') },
    { label: 'Settings',        group: 'Actions', hint: 'Ctrl+,', action: () => { loadConfig(); loadOllamaModels(); openModal('modal-settings'); } },
    { label: 'Error Log',       group: 'Actions', hint: 'Ctrl+Shift+E', action: openErrorLog },
    { label: 'Service Health',  group: 'Actions', action: openServicePanel },
    { label: 'Start Forge SD',  group: 'Actions', action: () => svcAction('start', 'forge') },
    { label: 'Start WanGP',     group: 'Actions', action: () => svcAction('start', 'wangp') },
    { label: 'Start ACE-Step',  group: 'Actions', action: () => svcAction('start', 'acestep') },
    { label: 'Refresh Gallery', group: 'Actions', action: refreshGallery },
  ]);
}

// ── Utilities ───────────────────────────────────────────────────────────────
function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}


// ── Client-side error logging ────────────────────────────────────────────────
function _reportClientError(message, source, lineno) {
  fetch('/api/logs/client', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: String(message), source: String(source || ''), lineno: lineno || 0 }),
  }).catch(() => {});
}
window.onerror = (msg, src, line) => { _reportClientError(msg, src, line); return false; };
window.addEventListener('unhandledrejection', e => {
  _reportClientError(e.reason?.message || String(e.reason), 'promise', 0);
});

document.addEventListener('DOMContentLoaded', () => {
  // Hide startup spinner when app is ready
  window.addEventListener('dcs:ready', () => {
    const s = document.getElementById('startup-spinner');
    if (s) s.style.display = 'none';
  }, { once: true });

  runSplash();

  // Rail tab clicks
  document.querySelectorAll('.rail-tab[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  // Rail toggle
  initRailToggle();

  // Split pane
  try {
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const k = localStorage.key(i);
      if (k && k.startsWith('dropcat_dir_')) localStorage.removeItem(k);
    }
  } catch (_) {}

  // Gallery overlay
  const galleryEl = document.getElementById('split-gallery');
  if (galleryEl) initGallery(galleryEl);

  // Pulse Gallery button after a generation so user knows something's there
  window.addEventListener('gallery:preview-updated', () => {
    const btn = document.getElementById('btn-gallery-rail');
    if (!state.galleryOpen && btn) {
      btn.classList.add('gallery-btn-pulse');
      btn.addEventListener('animationend', () => btn.classList.remove('gallery-btn-pulse'), { once: true });
    }
  });

  // Presets
  initPresets();

  // Keyboard shortcuts
  initShortcuts();

  // Command palette items
  initPaletteItems();

  // Header / rail buttons
  document.getElementById('btn-gallery-rail')?.addEventListener('click', _galleryToggle);
  document.getElementById('btn-gallery-close')?.addEventListener('click', _galleryClose);

  document.getElementById('service-cluster-btn')?.addEventListener('click', openServicePanel);

  document.getElementById('btn-close-svc-panel')?.addEventListener('click', closeServicePanel);
  document.getElementById('service-panel-overlay')?.addEventListener('click', e => {
    if (e.target.id === 'service-panel-overlay') closeServicePanel();
  });

  // Log toggle — closed by default, preference persisted in localStorage
  const logToggle  = document.getElementById('log-toggle');
  const logContent = document.getElementById('log-content');
  state.logOpen = false;
  logToggle?.setAttribute('aria-expanded', 'false');
  logToggle?.addEventListener('click', () => {
    state.logOpen = !state.logOpen;
    logToggle.classList.toggle('open', state.logOpen);
    logContent.classList.toggle('open', state.logOpen);
    logToggle.setAttribute('aria-expanded', String(state.logOpen));
  });

  // Modals
  document.getElementById('btn-settings')?.addEventListener('click', () => { loadConfig(); loadOllamaModels(); openModal('modal-settings'); });
  document.querySelectorAll('.modal-close').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = btn.closest('.modal-overlay') || btn.closest('[id$="-overlay"]');
      target?.classList.remove('open');
    });
  });
  document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.classList.remove('open'); });
  });

  // Settings
  document.getElementById('provider-toggle-input')?.addEventListener('change', e => onLLMProviderChange(e.target.checked ? 'anthropic' : 'ollama'));
  document.getElementById('btn-save-settings')?.addEventListener('click', saveSettings);
  document.getElementById('btn-validate-wan')?.addEventListener('click', () => validatePath('wan'));
  document.getElementById('btn-validate-ace')?.addEventListener('click', () => validatePath('ace'));

  // Boot default tab
  switchTab('sd-prompts');
  loadConfig();
  pollServices();
  pollLogs();

  setInterval(pollServices, 5000);
  setInterval(pollLogs,     2000);
});
