/**
 * Drop Cat Go Studio — Main application controller.
 * Tab routing, service polling, shared state, modals.
 */

import { init as initFunVideos, receiveHandoff as funHandoff } from './tab-fun-videos.js';
import { init as initBridges, receiveHandoff as bridgesHandoff } from './tab-bridges.js';
import { init as initSdPrompts, receiveHandoff as sdPromptsHandoff } from './tab-sd-prompts.js';
import { init as initImage2Video } from './panel-image2video.js';
import { init as initVideoTools } from './panel-video-tools.js';
import { init as initWildcards } from './panel-wildcards.js';
import { consumeHandoff } from './handoff.js';

// ── Tab module initializers ─────────────────────────────────────────────────
const TAB_INIT = {
  'fun-videos':       initFunVideos,
  'bridges':          initBridges,
  'sd-prompts':       initSdPrompts,
  'image2video':      initImage2Video,
  'video-tools':      initVideoTools,
  'wildcards':        initWildcards,
};
const TAB_HANDOFF = {
  'fun-videos':      funHandoff,
  'bridges':         bridgesHandoff,
  'sd-prompts':      sdPromptsHandoff,
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

  // "Load anyway" button — appears after 5s
  const loadAnywayDiv = document.getElementById('splash-load-anyway');
  const loadAnywayBtn = document.getElementById('btn-load-anyway');
  let pollInterval = null;
  let settled = false;

  // Hat fill: one segment per service check (6 total)
  const _hatAdvanced = new Set();
  const HAT_TOTAL = 6;
  function advanceHatOnce(key) {
    if (_hatAdvanced.has(key)) return;
    _hatAdvanced.add(key);
    const bar = document.getElementById('hat-fill-bar');
    if (bar) bar.style.height = Math.round(_hatAdvanced.size / HAT_TOTAL * 100) + '%';
  }

  // Track when cat entrance animation completes so doExit knows whether to wait
  const _splashT0 = Date.now();
  const ENTRANCE_MS = 900; // matches cat-entrance .9s in CSS

  function doExit() {
    if (settled) return;
    settled = true;
    if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }

    // Ensure hat is 100% filled before the exit sequence
    const bar = document.getElementById('hat-fill-bar');
    if (bar) bar.style.height = '100%';

    if (!document.querySelector('.splash-logo-wrap')) { exitSplash(); return; }

    const remaining = Math.max(0, ENTRANCE_MS - (Date.now() - _splashT0));

    // Wait for entrance to finish, then: blink → smile → fade
    setTimeout(() => {
      document.getElementById('eye-left')?.classList.add('blinking');
      document.getElementById('eye-right')?.classList.add('blinking');
      setTimeout(() => {
        document.getElementById('cheshire-smile')?.classList.add('smiling');
        setTimeout(exitSplash, 750);
      }, 600);
    }, remaining + 200);
  }

  if (loadAnywayBtn) {
    loadAnywayBtn.addEventListener('click', doExit);
  }
  const loadAnywayTimer = setTimeout(() => {
    if (!settled && loadAnywayDiv) loadAnywayDiv.classList.remove('hidden');
  }, 5000);

  // Hard failsafe: exit after 12s no matter what
  setTimeout(() => { if (!settled) doExit(); }, 12000);

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
    advanceHatOnce('server');

    // 2. ffmpeg
    setCheck('chk-ffmpeg',
      sys.ffmpeg ? 'ok' : 'warn',
      sys.ffmpeg ? 'ffmpeg ready' : 'ffmpeg not found — install and add to PATH'
    );
    advanceHatOnce('ffmpeg');

    // 3. Ollama
    const ol = sys.ollama || {};
    if (ol.available) {
      const modelList = (ol.models || []).join(', ') || 'models loaded';
      setCheck('chk-ollama', 'ok', `Ollama ready — ${modelList}`);
    } else {
      setCheck('chk-ollama', 'err', 'Ollama not running — start Ollama first');
    }
    advanceHatOnce('ollama');

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
        // Advance hat segment when this service is no longer in a loading state
        if (!SPLASH_LOADING_STATES.has(state)) advanceHatOnce(name);
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
  activeTab: 'sd-prompts', // default tab (SD Studio = combined prompts + image gen)
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
      try {
        TAB_INIT[tabId](panel);
        _tabInitialized.add(tabId);
      } catch (err) {
        console.error(`[${tabId}] Init failed:`, err);
        panel.innerHTML =
          `<div class="error-banner">This tab failed to load. Refresh the page.<br><small>${escHtml(err.message)}</small></div>`;
      }
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
    // Newest entries at top — prepend each so the last (newest) ends up first
    const entries = data.logs || [];
    for (const entry of entries) {
      const div = document.createElement('div');
      div.className = `log-entry ${entry.level}`;
      div.innerHTML = `<span class="time">${entry.time}</span>${escHtml(entry.msg)}`;
      container.prepend(div);
      state.logSeq = Math.max(state.logSeq, entry.seq || 0);
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

async function svcAction(action, name) {
  toast(`${action === 'start' ? 'Starting' : action === 'stop' ? 'Stopping' : 'Restarting'} ${name}...`, 'info');
  const data = await api(`/api/services/${action}/${name}`, { method: 'POST' });
  if (data.ok) {
    toast(data.message || `${name} ${action} initiated`, 'success');
  } else {
    toast(data.error || `Failed to ${action} ${name}`, 'error');
  }
  setTimeout(pollServices, 1000);
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

  // Service start/stop/restart buttons
  document.querySelectorAll('.svc-start').forEach(btn =>
    btn.addEventListener('click', () => svcAction('start', btn.dataset.svc)));
  document.querySelectorAll('.svc-stop').forEach(btn =>
    btn.addEventListener('click', () => svcAction('stop', btn.dataset.svc)));
  document.querySelectorAll('.svc-restart').forEach(btn =>
    btn.addEventListener('click', () => svcAction('restart', btn.dataset.svc)));

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
