/**
 * Drop Cat Go Studio -- Command palette (WS1 + WS9).
 * Ctrl+K to open. Fuzzy search across tabs, actions, presets.
 * Free-text queries also surface an "Ask AI" row that sends the query to
 * /api/ai-intent for the active tab.
 */
import { askAI, activeTabHasApplier } from './ai-intent.js?v=20260419e';

const _items = [];
let _selected = 0;
let _filtered = [];
let _lastQuery = '';
let _open = false;

export function registerItem(item) {
  // item: { label, hint?, icon?, group, action }
  _items.push(item);
}

export function registerItems(items) {
  _items.push(...items);
}

export function open() {
  const overlay = document.getElementById('cmd-palette-overlay');
  if (!overlay) return;
  overlay.classList.add('open');
  _open = true;
  const input = document.getElementById('palette-input');
  if (input) { input.value = ''; input.focus(); }
  _filter('');
}

export function close() {
  document.getElementById('cmd-palette-overlay')?.classList.remove('open');
  _open = false;
}

export function isOpen() { return _open; }

function _filter(query) {
  const q = query.toLowerCase().trim();
  _lastQuery = query.trim();
  const matches = q
    ? _items.filter(i => i.label.toLowerCase().includes(q) || (i.hint || '').toLowerCase().includes(q) || (i.group || '').toLowerCase().includes(q))
    : _items;
  // Append "Ask AI" pseudo-item when there's a query and the active tab has
  // an AI applier registered. Ordered last so registered matches win on Enter.
  if (q && activeTabHasApplier()) {
    _filtered = [...matches, {
      label: `Ask AI: "${_lastQuery}"`,
      group: 'AI',
      icon: '&#10022;',  // ✦
      hint: 'Natural-language tweak for this tab',
      action: () => askAI(_lastQuery),
    }];
  } else {
    _filtered = matches;
  }
  _selected = 0;
  _render();
}

function _render() {
  const container = document.getElementById('palette-results');
  if (!container) return;
  container.innerHTML = '';

  if (!_filtered.length) {
    const empty = document.createElement('div');
    empty.style.cssText = 'padding:16px;text-align:center;color:var(--text-3);font-size:.85rem';
    empty.textContent = 'No results';
    container.appendChild(empty);
    return;
  }

  // Group items
  const groups = {};
  for (const item of _filtered) {
    const g = item.group || 'Actions';
    if (!groups[g]) groups[g] = [];
    groups[g].push(item);
  }

  let idx = 0;
  for (const [group, items] of Object.entries(groups)) {
    const label = document.createElement('div');
    label.className = 'palette-group-label';
    label.textContent = group;
    container.appendChild(label);

    for (const item of items) {
      const el = document.createElement('div');
      el.className = `palette-item${idx === _selected ? ' selected' : ''}`;
      el.dataset.idx = idx;

      const iconEl = document.createElement('span');
      iconEl.className = 'palette-item-icon';
      iconEl.innerHTML = item.icon || '&#8227;';
      el.appendChild(iconEl);

      const labelEl = document.createElement('span');
      labelEl.className = 'palette-item-label';
      labelEl.textContent = item.label;
      el.appendChild(labelEl);

      if (item.hint) {
        const hintEl = document.createElement('span');
        hintEl.className = 'palette-item-hint';
        hintEl.textContent = item.hint;
        el.appendChild(hintEl);
      }

      el.addEventListener('mouseenter', () => {
        _selected = idx;
        _highlightSelected();
      });
      el.addEventListener('click', () => { _execute(item); close(); });

      container.appendChild(el);
      idx++;
    }
  }
}

function _highlightSelected() {
  document.querySelectorAll('#palette-results .palette-item').forEach((el, i) => {
    el.classList.toggle('selected', Number(el.dataset.idx) === _selected);
  });
  const sel = document.querySelector('#palette-results .palette-item.selected');
  sel?.scrollIntoView({ block: 'nearest' });
}

function _execute(item) {
  if (item.action) item.action();
}

// Wire up DOM after load
document.addEventListener('DOMContentLoaded', () => {
  const overlay = document.getElementById('cmd-palette-overlay');
  const input   = document.getElementById('palette-input');
  if (!overlay || !input) return;

  overlay.addEventListener('click', e => {
    if (e.target === overlay) close();
  });

  input.addEventListener('input', e => _filter(e.target.value));

  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      _selected = Math.min(_selected + 1, _filtered.length - 1);
      _highlightSelected();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      _selected = Math.max(_selected - 1, 0);
      _highlightSelected();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const item = _filtered[_selected];
      if (item) { _execute(item); close(); }
    } else if (e.key === 'Escape') {
      close();
    }
  });
});
