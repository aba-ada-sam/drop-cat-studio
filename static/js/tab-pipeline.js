/**
 * Drop Cat Go Studio -- Studio Home
 * The front door: concept input + numbered pipeline walkthrough + recent work.
 * Routes the user's raw idea to the Quick Video (express) tab via handoff.
 */
import { el } from './components.js?v=20260806a';
import { handoff } from './handoff.js?v=20260806a';

// -- Module state (reset on each init) --------------------------------------
let _svcInterval = null;
let _stepCards   = [];   // [{step, dot, msg}]
let _recentGrid  = null; // the "Recent Work" grid element, kept for refresh
let _listenersBound = false; // init() only runs once per app.js's TAB_INIT guard, but guard anyway

// -- Pipeline step definitions ---------------------------------------------
const STEPS = [
  {
    num: '01', icon: '', label: 'Create Videos',
    hint: 'Animate your photos with AI motion. Add AI-generated music with a single prompt.',
    tab: 'create-videos', svc: 'wangp',
    svcLabels: {
      running:        'WanGP ready',
      ready:          'WanGP configured',
      not_running:    'WanGP offline',
      not_configured: 'Set path in Settings',
      unknown:        'Checking...',
    },
  },
  {
    num: '02', icon: '', label: 'Video Tools',
    hint: 'Add AI-generated music to your videos. Batch reverse, speed-ramp, upscale.',
    tab: 'video-tools', svc: 'acestep',
    svcLabels: {
      running:        'ACE-Step ready',
      not_running:    'ACE-Step offline',
      starting:       'ACE-Step starting...',
      not_configured: 'Set path in Settings',
      unknown:        'Checking...',
    },
  },
];

// -- Init ------------------------------------------------------------------
export function init(panel) {
  panel.innerHTML = '';

  // Clean up any prior polling
  if (_svcInterval) { clearInterval(_svcInterval); _svcInterval = null; }
  _stepCards = [];

  const root = el('div', { class: 'pipeline-root' });
  panel.appendChild(root);

  _buildHero(root);
  _buildSteps(root);
  _buildRecent(root);

  // Live service status on the step cards
  _pollServices();
  _svcInterval = setInterval(_pollServices, 8000);

  // app.js's TAB_INIT only calls init() once per tab (see _tabInitialized) --
  // the panel is hidden/shown via CSS on every later visit, never rebuilt.
  // Without this, the very first /api/gallery fetch above is the ONLY one
  // that ever runs: anything generated afterward (on this tab or any other)
  // never appears in "Recent Work" again for the rest of the session, no
  // matter how many times the user comes back to Studio Home. Refresh on
  // both "something new landed" and "user came back to look" -- listened on
  // document AND window since call sites in this codebase dispatch
  // session-updated on both targets inconsistently.
  if (!_listenersBound) {
    _listenersBound = true;
    let _firstActivation = true; // the init() call above already fetched once for this activation
    const refresh = () => _refreshRecent();
    document.addEventListener('session-updated', refresh);
    window.addEventListener('session-updated', refresh);
    document.addEventListener('dcs:tab-activated', e => {
      const isPipeline = e.detail?.tab === 'pipeline';
      if (isPipeline && _firstActivation) { _firstActivation = false; return; }
      // Stop burning a poll every 8s while the user is on a different tab --
      // Queue tab gets an explicit pause()/resume() wired into app.js's
      // switchTab(); this tab isn't part of that, so it self-manages off
      // the same tab-activated broadcast every switchTab() already sends.
      if (_svcInterval) { clearInterval(_svcInterval); _svcInterval = null; }
      if (!isPipeline) return;
      _refreshRecent();
      _pollServices();
      _svcInterval = setInterval(_pollServices, 8000);
    });
  }
}

// -- Hero ------------------------------------------------------------------
function _buildHero(root) {
  const hero = el('div', { class: 'pipeline-hero' });
  root.appendChild(hero);

  const inner = el('div', { class: 'pipeline-hero-inner' });
  hero.appendChild(inner);

  inner.appendChild(el('p', { class: 'pipeline-hero-eyebrow', text: 'Drop Cat Go Studio' }));
  inner.appendChild(el('h1', { class: 'pipeline-hero-title', text: 'What do you want to create?' }));
  inner.appendChild(el('p', {
    class: 'pipeline-hero-sub',
    text: 'Type your idea below to start a Quick Video -- it writes the prompt, generates the images, animates it, and adds the music for you.',
  }));

  const wrap = el('div', { class: 'pipeline-concept-wrap' });
  inner.appendChild(wrap);

  const conceptTA = el('textarea', {
    class: 'pipeline-concept-ta',
    rows: '3',
    placeholder: 'e.g. "a lone astronaut discovering an alien jungle at dusk"  .  Ctrl+Enter to start',
  });
  wrap.appendChild(conceptTA);

  const btnRow = el('div', { class: 'pipeline-concept-btns' });
  wrap.appendChild(btnRow);

  const goBtn = el('button', {
    class: 'btn btn-primary pipeline-concept-go',
    text: 'Start a Quick Video from this Idea',
    onclick() { _launchConcept(conceptTA.value.trim()); },
  });
  btnRow.appendChild(goBtn);

  btnRow.appendChild(el('span', { class: 'pipeline-concept-hint', text: 'or jump to any step below' }));

  conceptTA.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      _launchConcept(conceptTA.value.trim());
    }
  });

  // Persist draft across tab switches
  try {
    const saved = localStorage.getItem('dcg_pipeline_draft');
    if (saved) conceptTA.value = saved;
  } catch (_) {}
  conceptTA.addEventListener('input', () => {
    try { localStorage.setItem('dcg_pipeline_draft', conceptTA.value); } catch (_) {}
  });
}

function _launchConcept(text) {
  if (!text) return;
  handoff('express', { type: 'concept', text });
  document.querySelector('[data-tab="express"]')?.click();
}

// -- Pipeline steps --------------------------------------------------------
function _buildSteps(root) {
  const section = el('div', { class: 'pipeline-steps-section' });
  root.appendChild(section);

  // Section heading
  const heading = el('div', { class: 'pipeline-section-heading' });
  heading.appendChild(el('span', { text: 'QUICK LINKS' }));
  heading.appendChild(el('span', { class: 'pipeline-section-sub', text: '-- jump straight to a specific tool' }));
  section.appendChild(heading);

  const row = el('div', { class: 'pipeline-steps-row' });
  section.appendChild(row);

  STEPS.forEach((step, i) => {
    // Connector arrow between cards
    if (i > 0) {
      row.appendChild(el('div', { class: 'pipeline-step-arrow', 'aria-hidden': 'true', text: '->' }));
    }

    const card = el('div', {
      class: 'pipeline-step-card',
      role: 'button',
      tabindex: '0',
      'aria-label': `Open ${step.label}`,
      onclick() { document.querySelector(`[data-tab="${step.tab}"]`)?.click(); },
    });
    card.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); card.click(); }
    });
    row.appendChild(card);

    card.appendChild(el('div', { class: 'pipeline-step-num', text: step.num }));
    card.appendChild(el('div', { class: 'pipeline-step-icon', text: step.icon, 'aria-hidden': 'true' }));
    card.appendChild(el('div', { class: 'pipeline-step-label', text: step.label }));
    card.appendChild(el('div', { class: 'pipeline-step-hint', text: step.hint }));

    // Service status row
    const svcRow = el('div', { class: 'pipeline-step-svc' });
    const dot = el('span', { class: 'dot unknown', 'aria-hidden': 'true' });
    const msg = el('span', { class: 'pipeline-step-svc-msg', text: 'Checking...' });
    svcRow.appendChild(dot);
    svcRow.appendChild(msg);
    card.appendChild(svcRow);

    const openBtn = el('button', {
      class: 'btn btn-sm pipeline-step-open',
      text: 'Open ->',
      // Prevent card click from double-firing
      onclick(e) { e.stopPropagation(); document.querySelector(`[data-tab="${step.tab}"]`)?.click(); },
    });
    card.appendChild(openBtn);

    _stepCards.push({ step, dot, msg });
  });
}

// -- Recent work -------------------------------------------------------------
function _buildRecent(root) {
  const section = el('div', { class: 'pipeline-recent-section' });
  root.appendChild(section);

  const heading = el('div', { class: 'pipeline-section-heading' });
  heading.appendChild(el('span', { text: 'RECENT WORK' }));
  section.appendChild(heading);

  const grid = el('div', { class: 'pipeline-recent-grid' });
  section.appendChild(grid);
  _recentGrid = grid;

  // Show skeleton placeholder while loading
  for (let i = 0; i < 6; i++) {
    grid.appendChild(el('div', { class: 'pipeline-recent-thumb pipeline-recent-skel' }));
  }

  _refreshRecent();
}

// Re-fetches and repopulates the Recent Work grid. Called on first build,
// and again on every 'session-updated'/return-to-this-tab signal -- see
// init()'s listener setup for why this can't just be a one-shot fetch.
async function _refreshRecent() {
  const grid = _recentGrid;
  if (!grid) return;
  try {
    const data = await fetch('/api/gallery').then(r => r.json());
    const items = (data.items || []).slice(0, 10);
    grid.innerHTML = '';

    if (!items.length) {
      grid.appendChild(el('div', {
        class: 'pipeline-recent-empty',
        text: '...  Nothing yet -- your generations will appear here.',
      }));
      return;
    }

    for (const item of items) {
      const thumb = el('div', {
        class: 'pipeline-recent-thumb',
        title: item.prompt || '',
        role: 'button',
        tabindex: '0',
        'aria-label': `Open ${item.prompt ? '"' + item.prompt.slice(0, 60) + '"' : 'this item'} in gallery`,
      });

      // Click -> open gallery detail overlay
      const _openItem = () => {
        window.dispatchEvent(new CustomEvent('gallery:open-item', { detail: { id: item.id } }));
      };
      thumb.addEventListener('click', _openItem);
      thumb.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _openItem(); }
      });

      const isVideo = /\.(mp4|webm|mov)$/i.test(item.url || '');

      if (isVideo) {
        const v = document.createElement('video');
        v.src = item.url;
        v.muted = true;
        v.preload = 'none';
        v.style.cssText = 'width:100%;height:100%;object-fit:cover;display:block';
        thumb.appendChild(v);
        thumb.addEventListener('mouseenter', () => v.play().catch(() => {}));
        thumb.addEventListener('mouseleave', () => { v.pause(); v.currentTime = 0; });
      } else {
        const img = document.createElement('img');
        img.src = item.url;
        img.alt = item.prompt || '';
        img.loading = 'lazy';
        img.style.cssText = 'width:100%;height:100%;object-fit:cover;display:block';
        thumb.appendChild(img);
      }

      grid.appendChild(thumb);
    }
  } catch (_) {
    grid.innerHTML = '';
    grid.appendChild(el('div', { class: 'pipeline-recent-empty', text: 'Could not load recent work.' }));
  }
}

// -- Service status polling -------------------------------------------------
async function _pollServices() {
  if (!_stepCards.length) return;
  try {
    const data = await fetch('/api/services').then(r => r.json());
    for (const { step, dot, msg } of _stepCards) {
      // Steps with no service dependency (e.g. ffmpeg-only tools)
      if (!step.svc) {
        dot.className = 'dot running';
        msg.textContent = step.staticStatus || 'ready';
        continue;
      }
      const info  = data[step.svc] || {};
      const state = info.state || 'unknown';
      dot.className = `dot ${state}`;
      msg.textContent = step.svcLabels[state] || info.message || state;
    }
  } catch (_) {}
}
