/**
 * Drop Cat Go Studio -- Persistent generation gallery (WS2).
 * Renders in #split-gallery. Pulls from /api/gallery and listens for new items.
 */

import { apiFetch, toast } from './toast.js?v=20260518a';
import { applySettingsToTab } from './ai-intent.js?v=20260503h';
import { handoff } from '../handoff.js?v=20260422a';
import { pathToUrl } from '../components.js?v=20260507a';
import { VideoStretchTool } from '../components/video-stretch.js?v=20260528b';
import { mountLipSyncTool } from '../components/lipsync-tool.js?v=20260528b';

let _items = [];
let _totalItems = 0;
let _loadedOffset = 0;
const _PAGE_SIZE = 100;
let _filters = { tab: '', search: '' };
let _containerEl = null;
let _detailItem = null;
let _preview = null; // { url, prompt, actions: [{label, onClick}] }

export function init(containerEl) {
  _containerEl = containerEl;
  _render();
  _load();

  // Allow any module to open a gallery item by id via a window event.
  // tab-pipeline.js dispatches this when a recent-work thumbnail is clicked.
  window.addEventListener('gallery:open-item', e => {
    const id = e.detail?.id;
    if (id == null) return;
    const item = _items.find(i => String(i.id) === String(id));
    if (item) _openDetail(item);
  });
}

// Called by tabs after generation to show the result prominently in the right panel.
// actions: [{label: '🎬 Create Video', primary: true, onClick: fn}, ...]
export function setPreview(url, prompt, actions = []) {
  _preview = { url, prompt, actions };
  _renderPreview();
  _renderGrid();
}

export async function addItem(item) {
  try {
    const saved = await apiFetch('/api/gallery', {
      method: 'POST',
      body: JSON.stringify(item),
      context: 'gallery.save',
    });
    _items.unshift(saved);
    _renderGrid();
  } catch (e) {
    console.warn('[gallery] addItem failed:', e?.message || e);
  }
}

// Tabs call this when a generation succeeds. Converts a filesystem path to
// the /output/... URL the app serves, and bundles settings as metadata so
// "Load Settings" from the gallery item can replay them.
export function pushFromTab(tab, savedPath, prompt, seed, settings) {
  const url = pathToUrl(savedPath);
  if (!url) return;
  return addItem({
    tab,
    url,
    prompt: prompt || '',
    model: settings?.model || '',
    seed: typeof seed === 'number' ? seed : null,
    metadata: { path: savedPath, settings: settings || {} },
  });
}

function _apiUrl(offset) {
  const p = new URLSearchParams({ limit: _PAGE_SIZE, offset });
  if (_filters.tab)    p.set('tab', _filters.tab);
  if (_filters.search) p.set('search', _filters.search);
  return `/api/gallery?${p}`;
}

async function _load() {
  try {
    const data = await apiFetch(_apiUrl(0), { context: 'gallery.load' });
    _items = data.items || [];
    _totalItems = data.total ?? _items.length;
    _loadedOffset = _items.length;
    _renderGrid();
  } catch (e) {
    console.warn('[gallery] load failed:', e?.message || e);
  }
}

async function _loadMore() {
  try {
    const data = await apiFetch(_apiUrl(_loadedOffset), { context: 'gallery.load-more' });
    const more = data.items || [];
    _items = _items.concat(more);
    _totalItems = data.total ?? _items.length;
    _loadedOffset = _items.length;
    _renderGrid();
  } catch (e) {
    console.warn('[gallery] loadMore failed:', e?.message || e);
  }
}

function _render() {
  _containerEl.innerHTML = `
    <div id="gallery-preview-area"></div>
    <div class="gallery-toolbar" id="gallery-toolbar">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:8px 12px;border-bottom:1px solid var(--border)">
        <input type="search" id="gallery-search" placeholder="Search generations..." style="flex:1;min-width:120px;font-size:.82rem">
        <select id="gallery-tab-filter" style="font-size:.82rem">
          <option value="">All tabs</option>
          <option value="sd-prompts">SD Prompts</option>
          <option value="image-gen">Image Gen</option>
          <option value="create-videos">Videos</option>
        </select>
      </div>
    </div>
    <div class="gallery-grid" id="gallery-grid"></div>`;

  _containerEl.querySelector('#gallery-search')?.addEventListener('input', e => {
    _filters.search = e.target.value;
    _items = []; _loadedOffset = 0; _totalItems = 0;
    _load();
  });
  _containerEl.querySelector('#gallery-tab-filter')?.addEventListener('change', e => {
    _filters.tab = e.target.value;
    _items = []; _loadedOffset = 0; _totalItems = 0;
    _load();
  });
}

function _renderPreview() {
  const area = _containerEl?.querySelector('#gallery-preview-area');
  if (!area) return;
  const toolbar = _containerEl?.querySelector('#gallery-toolbar');

  if (!_preview) {
    area.innerHTML = '';
    if (toolbar) toolbar.style.display = '';
    return;
  }

  const isVideo = /\.(mp4|webm|mov)$/i.test(_preview.url || '');
  const mediaSrc = _preview.url;
  const prompt = _preview.prompt || '';

  const btnHtml = _preview.actions.map((a, i) =>
    `<button class="btn btn-sm${a.primary ? ' btn-primary' : ''}" data-preview-action="${i}">${_esc(a.label)}</button>`
  ).join('');

  area.innerHTML = `
    <div style="display:flex;flex-direction:column;background:var(--surface-2);border-bottom:1px solid var(--border)">
      ${isVideo
        ? `<video src="${_esc(mediaSrc)}" controls style="width:100%;max-height:55vh;object-fit:contain;background:#000;display:block"></video>`
        : `<img src="${_esc(mediaSrc)}" alt="" style="width:100%;max-height:55vh;object-fit:contain;background:var(--bg);display:block">`}
      <div style="padding:10px 12px;display:flex;flex-direction:column;gap:8px">
        ${btnHtml}
        ${prompt ? `<p style="font-size:.75rem;color:var(--text-3);margin:0;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical">${_esc(prompt)}</p>` : ''}
      </div>
    </div>`;

  _preview.actions.forEach((a, i) => {
    area.querySelector(`[data-preview-action="${i}"]`)?.addEventListener('click', a.onClick);
  });

  // Hide the search toolbar when preview is active -- history is below
  if (toolbar) toolbar.style.display = 'none';
}

function _filtered() {
  return _items.filter(item => {
    if (_filters.tab && item.tab !== _filters.tab) return false;
    if (_filters.search) {
      const q = _filters.search.toLowerCase();
      return (item.prompt || '').toLowerCase().includes(q) ||
             (item.model  || '').toLowerCase().includes(q);
    }
    return true;
  });
}

function _renderGrid() {
  const grid = _containerEl?.querySelector('#gallery-grid');
  if (!grid) return;
  const items = _items;

  if (!items.length) {
    if (_preview) {
      grid.innerHTML = ''; // history section is empty but preview is showing above
    } else {
      grid.innerHTML = `<div class="gallery-empty" style="padding:40px;text-align:center;color:var(--text-3);grid-column:1/-1">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="opacity:.2;margin-bottom:12px"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg>
        <p>No generations yet.<br>Create something to see it here.</p>
      </div>`;
    }
    return;
  }

  // When preview is active, show a small "History" heading above the grid
  if (_preview) {
    grid.innerHTML = `<div style="grid-column:1/-1;padding:6px 12px 2px;font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text-3)">History</div>`;
    for (const item of items) grid.appendChild(_makeCard(item));
    return;
  }

  grid.innerHTML = '';
  for (const item of items) {
    grid.appendChild(_makeCard(item));
  }
  // "Load more" row when there are more items in the DB than we've fetched
  if (_loadedOffset < _totalItems) {
    const remaining = _totalItems - _loadedOffset;
    const btn = document.createElement('div');
    btn.style.cssText = 'grid-column:1/-1;padding:12px;text-align:center';
    btn.innerHTML = `<button class="btn btn-sm" id="gallery-load-more">Load ${Math.min(remaining, _PAGE_SIZE)} more (${remaining} remaining)</button>`;
    btn.querySelector('button').addEventListener('click', _loadMore);
    grid.appendChild(btn);
  }
}

function _makeCard(item) {
  const card = document.createElement('div');
  card.className = 'gallery-item';
  card.setAttribute('tabindex', '0');
  card.setAttribute('role', 'button');
  card.setAttribute('aria-label', `Open ${item.tab} generation from ${item.created_at || ''}`);

  const TAB_LABELS = {
    'sd-prompts': 'SD', 'image-gen': 'IMG', 'create-videos': 'VID', 'bridges': 'BRG',
  };
  const badge = TAB_LABELS[item.tab] || item.tab?.toUpperCase() || '?';

  const isVideo = /\.(mp4|webm|mov)$/i.test(item.url || '');
  const isAudio = /\.(mp3|wav|ogg|flac)$/i.test(item.url || '');

  let mediaEl;
  if (isVideo) {
    mediaEl = document.createElement('video');
    mediaEl.src = item.url;
    mediaEl.preload = 'none';
    mediaEl.poster = item.url
        ? `/api/thumbnail?path=${encodeURIComponent(item.url)}&size=300`
        : (item.thumbnail || '');
    mediaEl.style.cssText = 'width:100%;height:100%;object-fit:cover';
    mediaEl.muted = true;
    card.addEventListener('mouseenter', () => mediaEl.play().catch(() => {}));
    card.addEventListener('mouseleave', () => { mediaEl.pause(); mediaEl.currentTime = 0; });
  } else if (isAudio) {
    mediaEl = document.createElement('div');
    mediaEl.style.cssText = 'width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:var(--accent);font-size:2rem';
    mediaEl.textContent = '\u266B';
  } else {
    mediaEl = document.createElement('img');
    mediaEl.src = item.url;
    mediaEl.alt = item.prompt || '';
    mediaEl.loading = 'lazy';
    mediaEl.style.cssText = 'width:100%;height:100%;object-fit:cover';
  }
  card.appendChild(mediaEl);

  const badgeEl = document.createElement('div');
  badgeEl.className = 'gallery-item-badge';
  badgeEl.textContent = badge;
  card.appendChild(badgeEl);

  const actions = document.createElement('div');
  actions.className = 'gallery-item-actions';
  actions.innerHTML = `
    <button class="gallery-fav${item.favorite ? ' on' : ''}" title="Favorite" aria-label="Toggle favorite">\u2605</button>
    <span style="flex:1"></span>
    <button class="btn-icon-xs remove" title="Delete" aria-label="Delete">&#128465;</button>`;

  actions.querySelector('.gallery-fav').addEventListener('click', async e => {
    e.stopPropagation();
    const btn = e.currentTarget;
    const newVal = !item.favorite;
    item.favorite = newVal;
    btn.classList.toggle('on', newVal);
    try {
      await apiFetch(`/api/gallery/${item.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ favorite: newVal }),
        context: 'gallery.favorite',
      });
    } catch (_) {
      // revert local state so the star reflects actual server state
      item.favorite = !newVal;
      btn.classList.toggle('on', item.favorite);
    }
  });

  actions.querySelector('.remove').addEventListener('click', async e => {
    e.stopPropagation();
    if (!confirm('Delete this generation?')) return;
    try {
      await apiFetch(`/api/gallery/${item.id}`, { method: 'DELETE', context: 'gallery.delete' });
      _items = _items.filter(i => i.id !== item.id);
      _renderGrid();
    } catch (_) {
      // error already toasted by apiFetch; leave item in grid
    }
  });

  card.appendChild(actions);

  card.addEventListener('click', () => _openDetail(item));
  card.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _openDetail(item); }
  });

  return card;
}

function _openDetail(item) {
  _detailItem = item;
  let overlay = document.getElementById('gallery-detail-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'gallery-detail-overlay';
    overlay.className = 'gallery-detail-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    document.body.appendChild(overlay);
    overlay.addEventListener('click', e => {
      if (e.target === overlay) {
        overlay.querySelector('video')?.pause();
        overlay.classList.remove('open');
      }
    });
  }

  const isVideo = /\.(mp4|webm|mov)$/i.test(item.url || '');
  const meta = item.metadata || {};

  function _closeOverlay() {
    overlay.querySelector('video')?.pause();
    overlay.classList.remove('open');
  }

  overlay.innerHTML = `
    <div class="gallery-detail">
      <div class="gallery-detail-media">
        ${isVideo
          ? `<video src="${_esc(item.url)}" controls style="max-width:100%;max-height:90vh;object-fit:contain"></video>`
          : `<img src="${_esc(item.url)}" alt="${_esc(item.prompt || '')}" style="max-width:100%;max-height:90vh;object-fit:contain">`}
      </div>
      <div class="gallery-detail-sidebar">
        <button class="btn-icon modal-close" style="align-self:flex-end" aria-label="Close">&times;</button>
        <div class="gallery-meta-block">
          <strong>Prompt</strong><span>${_esc(item.prompt || '')}</span>
        </div>
        ${meta.model ? `<div class="gallery-meta-block"><strong>Model</strong><span>${_esc(meta.model)}</span></div>` : ''}
        ${meta.seed  ? `<div class="gallery-meta-block"><strong>Seed</strong><span>${meta.seed}</span></div>` : ''}
        ${meta.clips ? `<div class="gallery-meta-block"><strong>Clips</strong><span>${meta.clips}</span></div>` : ''}
        ${meta.duration_sec ? `<div class="gallery-meta-block"><strong>Output length</strong><span>${Math.round(meta.duration_sec)}s</span></div>` : ''}
        ${meta.elapsed_seconds ? `<div class="gallery-meta-block"><strong>Compute time</strong><span>${_formatGalleryDuration(meta.elapsed_seconds)}</span></div>` : ''}
        ${item.created_at ? `<div class="gallery-meta-block"><strong>Created</strong><span>${new Date(item.created_at).toLocaleString()}</span></div>` : ''}
        ${item.tab ? `<div class="gallery-meta-block"><strong>Source</strong><span>${_esc(item.tab)}</span></div>` : ''}
        <div style="display:flex;flex-direction:column;gap:8px;margin-top:8px">
          ${!isVideo ? `<button class="btn btn-primary btn-sm" id="gd-make-video">-> Make Video</button>` : ''}
          <a href="${_esc(item.url)}" download class="btn btn-sm">Download</a>
          <button class="btn btn-sm" id="gd-load-settings">Load Settings</button>
          <button class="btn btn-sm" id="gd-branch">Branch &amp; Tweak</button>
          <button class="btn btn-sm btn-danger" id="gd-delete">Delete File</button>
        </div>
        ${isVideo ? `<div id="gd-stretch-slot" style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border-2)"></div>` : ''}
        ${isVideo ? `<div id="gd-lipsync-slot" style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border-2)"></div>` : ''}
      </div>
    </div>`;

  overlay.querySelector('.modal-close').addEventListener('click', _closeOverlay);
  overlay.querySelector('#gd-make-video')?.addEventListener('click', () => {
    const path = item.metadata?.path || item.url;
    handoff('express', { type: 'image', path, url: item.url });
    overlay.classList.remove('open');
    document.getElementById('btn-gallery-close')?.click();
    document.querySelector('.rail-tab[data-tab="express"]')?.click();
    toast('Image loaded -- click Create!', 'info');
  });
  overlay.querySelector('#gd-load-settings')?.addEventListener('click', () => {
    _loadItemSettings(item);
    overlay.classList.remove('open');
    toast('Settings loaded from gallery item', 'success');
  });
  overlay.querySelector('#gd-branch')?.addEventListener('click', () => {
    overlay.classList.remove('open');
    // Close the main gallery overlay so the tab is visible
    document.getElementById('btn-gallery-close')?.click();
    if (item.tab) {
      // Navigate first so the tab initialises (lazy init on first visit)
      document.querySelector(`.rail-tab[data-tab="${item.tab}"]`)?.click();
      // Apply settings after the tab's async registerTabAI() Promise resolves
      setTimeout(() => {
        const ok = _loadItemSettings(item);
        if (!ok) toast(`Open the ${item.tab} tab first`, 'info');
      }, 80);
    } else {
      _loadItemSettings(item);
    }
    toast('Branched -- tweak and re-generate', 'info');
  });
  overlay.querySelector('#gd-delete')?.addEventListener('click', async () => {
    if (!confirm('Delete this file permanently? This cannot be undone.')) return;
    _closeOverlay();
    const filePath = item.metadata?.path;
    await Promise.all([
      apiFetch(`/api/gallery/${item.id}`, { method: 'DELETE', context: 'gallery.delete' }).catch(() => {}),
      filePath ? apiFetch('/api/output/delete', {
        method: 'POST',
        body: JSON.stringify({ path: filePath }),
        context: 'gallery.delete-file',
      }).catch(() => {}) : Promise.resolve(),
    ]);
    _items = _items.filter(i => i.id !== item.id);
    _renderGrid();
    toast('File deleted', 'success');
  });

  overlay.classList.add('open');

  // Manual Stretch & Lock tool for video items
  if (isVideo) {
    const slot = overlay.querySelector('#gd-stretch-slot');
    const vid  = overlay.querySelector('video');
    if (slot && vid) {
      try {
        new VideoStretchTool(slot, {
          videoUrl: item.url,
          videoPath: item.metadata?.path || item.url,
          videoEl: vid,
        });
      } catch (e) { console.error('VideoStretchTool init failed', e); }
    }
    const lipSlot = overlay.querySelector('#gd-lipsync-slot');
    if (lipSlot) {
      mountLipSyncTool(lipSlot, { videoPath: item.metadata?.path || item.url })
        .catch(e => console.error('LipSync mount failed', e));
    }
  }
}

function _loadItemSettings(item) {
  if (!item.metadata?.settings || !item.tab) return false;
  const settings = item.metadata.settings;
  if (!Object.keys(settings).length) return false;
  const ok = applySettingsToTab(item.tab, settings);
  return ok;
}

function _esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function _formatGalleryDuration(sec) {
  if (sec == null || sec < 0) return '';
  const total = Math.round(sec);
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60);
  const s = total % 60;
  if (m < 60) return `${m}m ${String(s).padStart(2, '0')}s`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return `${h}h ${String(mm).padStart(2, '0')}m`;
}

export function refresh() { _load(); }
