/**
 * Drop Cat Go Studio — Studio Home
 * The front door: concept input + numbered pipeline walkthrough + recent work.
 * Routes the user's raw idea to sd-prompts via handoff.
 */
import { el } from './components.js?v=20260429b';
import { handoff } from './handoff.js?v=20260422a';

// ── Module state (reset on each init) ──────────────────────────────────────
let _svcInterval = null;
let _stepCards   = [];   // [{step, dot, msg}]

// ── Pipeline step definitions ─────────────────────────────────────────────
const STEPS = [
  {
    num: '01', icon: '', label: 'Generate Images',
    hint: 'Turn any text idea into AI images with Stable Diffusion',
    tab: 'sd-prompts', svc: 'forge',
    svcLabels: {
      running:        'Forge SD ready',
      not_running:    'Forge offline',
      starting:       'Forge starting…',
      not_configured: 'Not configured',
      unknown:        'Checking…',
    },
  },
  {
    num: '02', icon: '', label: 'Create Videos',
    hint: 'Animate images with AI motion. Add AI-generated music with a single prompt.',
    tab: 'fun-videos', svc: 'wangp',
    svcLabels: {
      running:        'WanGP ready',
      ready:          'WanGP configured',
      not_running:    'WanGP offline',
      not_configured: 'Set path in Settings',
      unknown:        'Checking…',
    },
  },
  {
    num: '03', icon: '', label: 'Create Transitions',
    hint: 'Create cinematic bridge clips between scenes',
    tab: 'bridges', svc: 'wangp',
    svcLabels: {
      running:        'WanGP ready',
      ready:          'WanGP configured',
      not_running:    'WanGP offline',
      not_configured: 'Set path in Settings',
      unknown:        'Checking…',
    },
  },
  {
    num: '04', icon: '', label: 'Audio',
    hint: 'Add AI-generated music to your videos. Batch reverse, speed-ramp, upscale.',
    tab: 'video-tools', svc: 'acestep',
    svcLabels: {
      running:        'ACE-Step ready',
      not_running:    'ACE-Step offline',
      starting:       'ACE-Step starting…',
      not_configured: 'Set path in Settings',
      unknown:        'Checking…',
    },
  },
];

// ── Init ──────────────────────────────────────────────────────────────────
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
}

// ── Hero ──────────────────────────────────────────────────────────────────
function _buildHero(root) {
  const hero = el('div', { class: 'pipeline-hero' });
  root.appendChild(hero);

  const inner = el('div', { class: 'pipeline-hero-inner' });
  hero.appendChild(inner);

  inner.appendChild(el('p', { class: 'pipeline-hero-eyebrow', text: 'Drop Cat Go Studio' }));
  inner.appendChild(el('h1', { class: 'pipeline-hero-title', text: 'What do you want to create?' }));
  inner.appendChild(el('p', {
    class: 'pipeline-hero-sub',
    text: 'Type your idea below and the AI will write the prompt, generate the images, animate the video, and add the music — one step at a time.',
  }));

  const wrap = el('div', { class: 'pipeline-concept-wrap' });
  inner.appendChild(wrap);

  const conceptTA = el('textarea', {
    class: 'pipeline-concept-ta',
    rows: '3',
    placeholder: 'e.g. "a lone astronaut discovering an alien jungle at dusk"  ·  Ctrl+Enter to start',
  });
  wrap.appendChild(conceptTA);

  const btnRow = el('div', { class: 'pipeline-concept-btns' });
  wrap.appendChild(btnRow);

  const goBtn = el('button', {
    class: 'btn btn-primary pipeline-concept-go',
    text: 'Generate Images from this Idea',
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
  handoff('sd-prompts', { type: 'concept', text });
  document.querySelector('[data-tab="sd-prompts"]')?.click();
}

// ── Pipeline steps ────────────────────────────────────────────────────────
function _buildSteps(root) {
  const section = el('div', { class: 'pipeline-steps-section' });
  root.appendChild(section);

  // Section heading
  const heading = el('div', { class: 'pipeline-section-heading' });
  heading.appendChild(el('span', { text: 'THE PIPELINE' }));
  heading.appendChild(el('span', { class: 'pipeline-section-sub', text: '— do these in order, or jump to any step' }));
  section.appendChild(heading);

  const row = el('div', { class: 'pipeline-steps-row' });
  section.appendChild(row);

  STEPS.forEach((step, i) => {
    // Connector arrow between cards
    if (i > 0) {
      row.appendChild(el('div', { class: 'pipeline-step-arrow', 'aria-hidden': 'true', text: '→' }));
    }

    const card = el('div', {
      class: 'pipeline-step-card',
      role: 'button',
      tabindex: '0',
      'aria-label': `Go to step ${step.num}: ${step.label}`,
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
    const msg = el('span', { class: 'pipeline-step-svc-msg', text: 'Checking…' });
    svcRow.appendChild(dot);
    svcRow.appendChild(msg);
    card.appendChild(svcRow);

    const openBtn = el('button', {
      class: 'btn btn-sm pipeline-step-open',
      text: 'Open →',
      // Prevent card click from double-firing
      onclick(e) { e.stopPropagation(); document.querySelector(`[data-tab="${step.tab}"]`)?.click(); },
    });
    card.appendChild(openBtn);

    _stepCards.push({ step, dot, msg });
  });
}

// ── Recent work ─────────────────────────────────────────────────────────────
async function _buildRecent(root) {
  const section = el('div', { class: 'pipeline-recent-section' });
  root.appendChild(section);

  const heading = el('div', { class: 'pipeline-section-heading' });
  heading.appendChild(el('span', { text: 'RECENT WORK' }));
  section.appendChild(heading);

  const grid = el('div', { class: 'pipeline-recent-grid' });
  section.appendChild(grid);

  // Show skeleton placeholder while loading
  for (let i = 0; i < 6; i++) {
    grid.appendChild(el('div', { class: 'pipeline-recent-thumb pipeline-recent-skel' }));
  }

  try {
    const data = await fetch('/api/gallery').then(r => r.json());
    const items = (data.items || []).slice(0, 10);
    grid.innerHTML = '';

    if (!items.length) {
      grid.appendChild(el('div', {
        class: 'pipeline-recent-empty',
        text: '…  Nothing yet — your generations will appear here.',
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

      // Click → open gallery detail overlay
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

// ── Service status polling ─────────────────────────────────────────────────
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
