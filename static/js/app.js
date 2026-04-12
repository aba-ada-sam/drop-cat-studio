/**
 * Drop Cat Go Studio — Main application controller.
 * Tab routing, service polling, shared state, modals.
 */

import { init as initFunVideos, receiveHandoff as funHandoff } from './tab-fun-videos.js';
import { init as initBridges, receiveHandoff as bridgesHandoff } from './tab-bridges.js';
import { init as initSdPrompts } from './tab-sd-prompts.js';
import { init as initImage2Video } from './panel-image2video.js';
import { init as initVideoTools } from './panel-video-tools.js';
import { init as initWildcards } from './panel-wildcards.js';
import { init as initPostProcessing } from './tab-post-processing.js';
import { consumeHandoff } from './handoff.js';

// ── Tab module initializers ─────────────────────────────────────────────────
const TAB_INIT = {
  'fun-videos':       initFunVideos,
  'bridges':          initBridges,
  'sd-prompts':       initSdPrompts,
  'image2video':      initImage2Video,
  'video-tools':      initVideoTools,
  'wildcards':        initWildcards,
  'post-processing':  initPostProcessing,
};
const TAB_HANDOFF = {
  'fun-videos': funHandoff,
  'bridges': bridgesHandoff,
};
const _tabInitialized = new Set();

// ── Splash screen ─────────────────────────────────────────────────────────────

// States that mean a service is still starting (not yet settled)
const SPLASH_LOADING_STATES = new Set(['unknown', 'starting']);

// Map service state -> visual check state
function svcStateToCheck(state) {
  if (state === 'running') return 'ok';
  if (state === 'error')   return 'err';
  if (SPLASH_LOADING_STATES.has(state)) return 'loading';
  return 'warn'; // not_configured, not_running, ready, etc.
}

// Map service state -> human text for splash
const SVC_SPLASH_TEXT = {
  forge: {
    running:        'Forge SD running',
    starting:       'Forge SD starting...',
    not_running:    'Forge SD not detected — SD Prompts unavailable',
    not_configured: 'Forge not configured',
    unknown:        'Checking Forge SD...',
  },
  wangp: {
    running:        'WanGP ready',
    starting:       'WanGP loading model...',
    ready:          'WanGP configured',
    not_configured: 'WanGP not configured — set path in Settings',
    not_running:    'WanGP not running',
    error:          'WanGP error',
    unknown:        'Checking WanGP...',
  },
  acestep: {
    running:        'ACE-Step running',
    starting:       'ACE-Step starting...',
    not_configured: 'ACE-Step not configured — music generation unavailable',
    not_running:    'ACE-Step not running',
    error:          'ACE-Step error',
    unknown:        'Checking ACE-Step...',
  },
  void: {
    running:        'VOID inpainting ready',
    starting:       'VOID loading model (~10 GB first download)...',
    not_configured: 'VOID not configured — will auto-download on first use',
    not_running:    'VOID not running — start in Services tab',
    error:          'VOID error',
    unknown:        'Checking VOID...',
  },
};

// GUI-02: safe localStorage wrapper — SecurityError in private/sandboxed contexts
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
    const dot  = el.querySelector('.chk-dot');
    const span = el.querySelector('.chk-text');
    if (dot)  dot.className = 'chk-dot';
    if (span) span.textContent = text;
    else if (el.childNodes[1]) el.childNodes[1].textContent = text;
  }

  function exitSplash() {
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

  // "Load anyway" button — appears after 8s
  const loadAnywayDiv = document.getElementById('splash-load-anyway');
  const loadAnywayBtn = document.getElementById('btn-load-anyway');
  let pollInterval = null;
  let settled = false;

  // Track when cat entrance animation completes so doExit knows whether to wait
  const _splashT0 = Date.now();
  const ENTRANCE_MS = 950; // matches cat-entrance duration in CSS

  function doExit() {
    if (settled) return;
    settled = true;
    if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
    const logo = document.querySelector('.splash-logo');
    if (!logo) { exitSplash(); return; }

    // How long until the entrance animation finishes (may already be done)
    const remaining = Math.max(0, ENTRANCE_MS - (Date.now() - _splashT0));

    // GUI-03: go directly to hat-tip — the logo-pulsing class (3s loop) was
    // removed after 350ms causing a jarring cut before a single cycle finished.
    setTimeout(() => {
      logo.classList.add('hat-tip');
      setTimeout(exitSplash, 1700);
    }, remaining);
  }

  if (loadAnywayBtn) {
    loadAnywayBtn.addEventListener('click', doExit);
  }
  const loadAnywayTimer = setTimeout(() => {
    if (!settled && loadAnywayDiv) loadAnywayDiv.classList.remove('hidden');
  }, 8000);

  try {
    // Mark all as loading
    ['chk-server','chk-ffmpeg','chk-ollama','chk-forge','chk-wangp','chk-acestep'].forEach(id => {
      const el = document.getElementById(id);
      const current = el?.querySelector('.chk-text')?.textContent
                   || el?.childNodes[1]?.textContent || '';
      setCheck(id, 'loading', current);
    });

    // 1. Connect to server + instant checks
    setCheck('chk-server', 'loading', 'Connecting...');
    const sys = await fetch('/api/system').then(r => r.json());
    setCheck('chk-server', 'ok', 'Server running');

    // 2. ffmpeg
    setCheck('chk-ffmpeg',
      sys.ffmpeg ? 'ok' : 'warn',
      sys.ffmpeg ? 'ffmpeg ready' : 'ffmpeg not found — install and add to PATH'
    );

    // 3. Ollama
    const ol = sys.ollama || {};
    if (ol.available) {
      const modelList = (ol.models || []).join(', ') || 'models loaded';
      setCheck('chk-ollama', 'ok', `Ollama ready — ${modelList}`);
    } else {
      setCheck('chk-ollama', 'err', 'Ollama not running — start Ollama first');
    }

    // 4-6. Services — poll until settled
    function updateServiceChecks(svcs) {
      const map = {
        forge:    'chk-forge',
        wangp:    'chk-wangp',
        acestep:  'chk-acestep',
      };
      for (const [name, id] of Object.entries(map)) {
        const info  = svcs[name] || {};
        const state = info.state || 'unknown';
        const texts = SVC_SPLASH_TEXT[name] || {};
        const text  = texts[state] || info.message || state;
        setCheck(id, svcStateToCheck(state), text);
      }
    }

    function allSettled(svcs) {
      return ['forge', 'wangp', 'acestep'].every(name => {
        const state = (svcs[name] || {}).state || 'unknown';
        return !SPLASH_LOADING_STATES.has(state);
      });
    }

    // Initial service states
    updateServiceChecks(sys.services || {});

    // Always show splash for at least 2.5s so the user can read the checklist
    const minShowUntil = Date.now() + 2500;

    function tryExit(svcs) {
      if (!allSettled(svcs)) return;
      const wait = Math.max(0, minShowUntil - Date.now());
      setTimeout(() => { clearTimeout(loadAnywayTimer); doExit(); }, wait);
    }

    tryExit(sys.services || {});

    // Poll until all services settle
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
    const errDiv = document.getElementById('splash-error');
    const errMsg = document.querySelector('.splash-err-msg');
    if (errDiv) errDiv.classList.remove('hidden');
    if (errMsg) errMsg.textContent =
      'Cannot connect to Drop Cat Go Studio server. Make sure launch.bat is running.';
  }
}

// ── State ────────────────────────────────────────────────────────────────────

const state = {
  activeTab: 'sd-prompts',
  logOpen: true,
  logSeq: 0,
  config: {},
};

// ── API helper ───────────────────────────────────────────────────────────────

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...opts.headers },
    ...opts,
  });
  if (!res.ok) {
    let msg = `Server error ${res.status}`;
    try { const d = await res.json(); msg = d.detail || d.error || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

// ── Tab routing ──────────────────────────────────────────────────────────────

function switchTab(tabId) {
  state.activeTab = tabId;

  // Update tab buttons
  document.querySelectorAll('.tab, .dropdown-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabId);
  });

  // Update panels
  document.querySelectorAll('.tab-panel').forEach(panel => {
    panel.classList.toggle('active', panel.id === `panel-${tabId}`);
  });

  // Initialize tab module on first visit
  if (!_tabInitialized.has(tabId) && TAB_INIT[tabId]) {
    const panel = document.getElementById(`panel-${tabId}`);
    if (panel) {
      TAB_INIT[tabId](panel);
      _tabInitialized.add(tabId);
    }
  }

  // Dispatch any pending handoff to this tab
  const handoffData = consumeHandoff(tabId);
  if (handoffData && TAB_HANDOFF[tabId]) {
    TAB_HANDOFF[tabId](handoffData);
  }
}

// ── Service status polling ───────────────────────────────────────────────────

// Human-readable messages for service states that need explanation
const SERVICE_MESSAGES = {
  acestep: {
    not_configured: 'Not configured — open Settings and set the ACE-Step folder path to enable music generation',
    not_running:    'Not running — set path in Settings and it will auto-start',
  },
  forge: {
    not_running:    'Not detected — start Forge with the --api flag for SD image generation',
    starting:       'Starting up, please wait (~60s)...',
    not_configured: 'Not configured — Forge should be at C:\\forge',
  },
  wangp: {
    not_configured: 'Not configured — open Settings and set the WanGP folder path',
    ready:          'Configured — worker will start on first use',
  },
  void: {
    not_configured: 'Will auto-download model from HuggingFace on first use (netflix/void-model, ~10 GB)',
    not_running:    'Not running — click Start Worker to launch',
    starting:       'Loading VOID model (may download ~10 GB on first run)...',
  },
};

async function pollServices() {
  try {
    const data = await api('/api/services');
    for (const [name, info] of Object.entries(data)) {
      const dotClass = info.state || 'unknown';

      // Header pill dots
      const pillDot = document.querySelector(`#pill-${name} .dot`);
      if (pillDot) { pillDot.className = 'dot'; pillDot.classList.add(dotClass); }

      // Service tab dots (forge uses id 'forge-svc' to avoid conflict with header pill)
      const svcId = name === 'forge' ? 'dot-forge-svc' : `dot-${name}`;
      const svcDot = document.getElementById(svcId);
      if (svcDot) { svcDot.className = 'dot'; svcDot.classList.add(dotClass); }

      // Messages — use human override if available, fall back to server message
      const override = SERVICE_MESSAGES[name]?.[info.state];
      const displayMsg = override || info.message || info.state;

      const msgId = name === 'forge' ? 'msg-forge-svc' : `msg-${name}`;
      const msgEl = document.getElementById(msgId);
      if (msgEl) msgEl.textContent = displayMsg;

      // Also update header pill title attribute for hover tooltip
      const pill = document.getElementById(`pill-${name}`);
      if (pill) pill.title = displayMsg;
    }
  } catch (e) { /* server not ready */ }
}

// ── Log polling ──────────────────────────────────────────────────────────────

async function pollLogs() {
  if (!state.logOpen) return;
  try {
    const data = await api(`/api/logs?since=${state.logSeq}`);
    const container = document.getElementById('log-entries');
    for (const entry of data.logs || []) {
      const div = document.createElement('div');
      div.className = `log-entry ${entry.level}`;
      div.innerHTML = `<span class="time">${entry.time}</span>${escHtml(entry.msg)}`;
      container.appendChild(div);
      state.logSeq = Math.max(state.logSeq, entry.seq || 0);
    }
    // Auto-scroll — only snap to bottom if the user is already near the bottom
    const logViewport = document.getElementById('log-content');
    if (logViewport) {
      const distFromBottom = logViewport.scrollHeight - logViewport.scrollTop - logViewport.clientHeight;
      if (distFromBottom < 60) logViewport.scrollTop = logViewport.scrollHeight;
    }
  } catch (e) {}
}

// ── System info ──────────────────────────────────────────────────────────────

async function loadSystemInfo() {
  try {
    const data = await api('/api/system');
    const el = document.getElementById('system-info');
    if (!el) return;
    const lines = [];
    lines.push(`FFmpeg: ${data.ffmpeg ? 'Available' : 'NOT FOUND'}`);
    if (data.encoders?.length) {
      const hw = data.encoders.filter(e => e.hw).map(e => e.label);
      lines.push(`GPU encoders: ${hw.length ? hw.join(', ') : 'None (CPU only)'}`);
    }
    const ol = data.ollama || {};
    lines.push(`Ollama: ${ol.available ? 'Connected — ' + (ol.models || []).join(', ') : 'NOT running'}`);
    el.innerHTML = lines.map(l => `<div>${l}</div>`).join('');
  } catch (e) {}
}

// ── Modals ───────────────────────────────────────────────────────────────────

function openModal(id) {
  document.getElementById(id)?.classList.add('open');
}

function closeModal(id) {
  document.getElementById(id)?.classList.remove('open');
}

// ── Settings ─────────────────────────────────────────────────────────────────

async function loadConfig() {
  state.config = await api('/api/config');
  // Populate settings inputs
  for (const [key, val] of Object.entries(state.config)) {
    const el = document.getElementById(`cfg-${key}`);
    if (el) el.value = val;
  }
  // Load LLM provider status
  try {
    const llm = await api('/api/llm/config');
    onLLMProviderChange(llm.provider || 'ollama');
    if (llm.anthropic_key_hint) {
      const h = document.getElementById('anthropic-key-hint');
      if (h) h.textContent = `Current key: ${llm.anthropic_key_hint}`;
    }
    if (llm.openai_key_hint) {
      const h = document.getElementById('openai-key-hint');
      if (h) h.textContent = `Current key: ${llm.openai_key_hint}`;
    }
  } catch (e) {}
}

function onLLMProviderChange(provider) {
  const useAnthropic = provider === 'anthropic';
  const toggle = document.getElementById('provider-toggle-input');
  if (toggle) toggle.checked = useAnthropic;
  document.getElementById('llm-ollama-section').style.display    = useAnthropic ? 'none' : '';
  document.getElementById('llm-anthropic-section').style.display = useAnthropic ? ''     : 'none';
  const ollamaSide     = document.getElementById('provider-side-ollama');
  const anthropicSide  = document.getElementById('provider-side-anthropic');
  if (ollamaSide)    { ollamaSide.classList.toggle('active', !useAnthropic); ollamaSide.classList.toggle('inactive', useAnthropic); }
  if (anthropicSide) { anthropicSide.classList.toggle('active', useAnthropic); anthropicSide.classList.toggle('inactive', !useAnthropic); }
}

async function saveSettings() {
  try {
    const fields = [
      'wan2gp_root', 'acestep_root', 'sd_wildcards_dir',
      'ollama_host', 'ollama_fast_model', 'ollama_power_model',
      'void_model_dir',
    ];
    const body = {};
    for (const key of fields) {
      const el = document.getElementById(`cfg-${key}`);
      if (el) body[key] = el.value;
    }
    // Save standard config
    await api('/api/config', { method: 'POST', body: JSON.stringify(body) });
    // Hot-reload Ollama config if changed
    const ollamaKeys = ['ollama_host', 'ollama_fast_model', 'ollama_power_model'];
    const ollamaBody = {};
    for (const k of ollamaKeys) if (body[k] !== undefined) ollamaBody[k] = body[k];
    if (Object.keys(ollamaBody).length) {
      await api('/api/ollama/config', { method: 'POST', body: JSON.stringify(ollamaBody) });
    }
    // Save LLM provider + keys
    const llmBody = {
      provider: document.getElementById('provider-toggle-input')?.checked ? 'anthropic' : 'ollama',
    };
    const anthropicKey = document.getElementById('cfg-anthropic_key')?.value;
    const openaiKey    = document.getElementById('cfg-openai_key')?.value;
    if (anthropicKey) llmBody.anthropic_key = anthropicKey;
    if (openaiKey)    llmBody.openai_key    = openaiKey;
    await api('/api/llm/config', { method: 'POST', body: JSON.stringify(llmBody) });

    closeModal('modal-settings');
    toast('Settings saved', 'success');
    loadConfig();
  } catch (e) {
    toast(`Settings save failed: ${e.message}`, 'error');
  }
}

async function loadOllamaModels() {
  try {
    const data = await api('/api/ollama/models');
    const models = data.models || [];
    for (const selId of ['cfg-ollama_fast_model', 'cfg-ollama_power_model']) {
      const sel = document.getElementById(selId);
      if (!sel) continue;
      const current = sel.value || state.config?.[selId.replace('cfg-', '')] || '';
      sel.innerHTML = models.map(m =>
        `<option value="${m}" ${m === current ? 'selected' : ''}>${m}</option>`
      ).join('') || '<option value="">No models found</option>';
    }
  } catch (e) {}
}

async function validatePath(type) {
  const key = type === 'wan' ? 'wan2gp_root' : 'acestep_root';
  const path = document.getElementById(`cfg-${key}`)?.value || '';
  const endpoint = type === 'wan' ? '/api/config/validate-wangp' : '/api/config/validate-acestep';
  const data = await api(endpoint, { method: 'POST', body: JSON.stringify({ path }) });
  const msgEl = document.getElementById(`val-${type}`);
  if (msgEl) {
    msgEl.textContent = data.message || '';
    msgEl.className = `validation-msg ${data.ok ? 'ok' : 'err'}`;
  }
}

// ── Service start buttons ────────────────────────────────────────────────────

async function startService(name) {
  toast(`Starting ${name}...`, 'info');
  const data = await api(`/api/services/start/${name}`, { method: 'POST' });
  if (data.ok) {
    toast(`${name} started`, 'success');
  } else {
    toast(data.error || `Failed to start ${name}`, 'error');
  }
  pollServices();
}

// ── Toast ────────────────────────────────────────────────────────────────────

function toast(msg, level = 'info') {
  const wrap = document.getElementById('toast-wrap');
  const el = document.createElement('div');
  el.className = `toast ${level}`;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

// ── Utilities ────────────────────────────────────────────────────────────────

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// ── Init ─────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  // Run splash — waits until all services are settled or user clicks "Load anyway"
  runSplash();
  // Tab clicks
  document.querySelectorAll('[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  // GUI-01: click-based dropdown with keyboard support.
  // CSS drives visibility via aria-expanded; JS sets the attribute.
  const dropTrigger = document.querySelector('.dropdown-trigger');
  if (dropTrigger) {
    dropTrigger.setAttribute('aria-haspopup', 'true');
    dropTrigger.setAttribute('aria-expanded', 'false');
    const dropMenu = dropTrigger.nextElementSibling;
    if (dropMenu) {
      dropMenu.setAttribute('role', 'menu');
      dropMenu.querySelectorAll('.dropdown-item').forEach(item => item.setAttribute('role', 'menuitem'));
    }
    dropTrigger.addEventListener('click', e => {
      e.stopPropagation();
      const open = dropTrigger.getAttribute('aria-expanded') === 'true';
      dropTrigger.setAttribute('aria-expanded', String(!open));
    });
    // Close on outside click or Escape
    document.addEventListener('click', () => dropTrigger.setAttribute('aria-expanded', 'false'));
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') dropTrigger.setAttribute('aria-expanded', 'false');
    });
    // Trap Tab within open menu and close after selection
    if (dropMenu) {
      dropMenu.addEventListener('keydown', e => {
        const items = [...dropMenu.querySelectorAll('.dropdown-item')];
        const idx = items.indexOf(document.activeElement);
        if (e.key === 'ArrowDown') { e.preventDefault(); items[(idx + 1) % items.length]?.focus(); }
        if (e.key === 'ArrowUp')   { e.preventDefault(); items[(idx - 1 + items.length) % items.length]?.focus(); }
      });
      dropMenu.querySelectorAll('.dropdown-item').forEach(item => {
        item.addEventListener('click', () => dropTrigger.setAttribute('aria-expanded', 'false'));
      });
    }
  }

  // Log toggle — starts open
  const logToggle = document.getElementById('log-toggle');
  const logContent = document.getElementById('log-content');
  if (logToggle)  logToggle.classList.add('open');
  if (logContent) logContent.classList.add('open');
  logToggle?.addEventListener('click', () => {
    state.logOpen = !state.logOpen;
    logToggle.classList.toggle('open', state.logOpen);
    logContent.classList.toggle('open', state.logOpen);
  });

  // Modal open/close
  document.getElementById('btn-settings')?.addEventListener('click', () => {
    loadConfig();
    loadOllamaModels();
    openModal('modal-settings');
  });
  document.getElementById('btn-help')?.addEventListener('click', () => openModal('modal-help'));
  document.querySelectorAll('.modal-close').forEach(btn => {
    btn.addEventListener('click', () => btn.closest('.modal-overlay')?.classList.remove('open'));
  });
  document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', e => {
      if (e.target === overlay) overlay.classList.remove('open');
    });
  });

  // AI provider toggle
  document.getElementById('provider-toggle-input')?.addEventListener('change', e => {
    onLLMProviderChange(e.target.checked ? 'anthropic' : 'ollama');
  });

  // Settings actions
  document.getElementById('btn-save-settings')?.addEventListener('click', saveSettings);
  document.getElementById('btn-validate-wan')?.addEventListener('click', () => validatePath('wan'));
  document.getElementById('btn-validate-ace')?.addEventListener('click', () => validatePath('ace'));

  // Service start buttons
  document.getElementById('btn-start-wangp')?.addEventListener('click', () => startService('wangp'));
  document.getElementById('btn-start-acestep')?.addEventListener('click', () => startService('acestep'));
  document.getElementById('btn-start-void')?.addEventListener('click', () => startService('void'));

  // Initialize default tab
  switchTab('sd-prompts');

  // Initial loads
  loadConfig();
  pollServices();
  pollLogs();       // populate log immediately, don't wait 2s
  loadSystemInfo();

  // Polling intervals
  setInterval(pollServices, 5000);
  setInterval(pollLogs, 2000);
  setInterval(loadSystemInfo, 30000);
});
