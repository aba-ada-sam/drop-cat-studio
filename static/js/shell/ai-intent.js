/**
 * Drop Cat Go Studio -- palette-driven AI intent.
 * Each tab registers {getContext, applySettings} on init. When the user types
 * free text in the command palette and picks "Ask AI", we dispatch to the
 * currently active tab's applier.
 */
import { apiFetch, toast } from './toast.js?v=20260419e';

const _appliers = {};

export function registerTabAI(tabId, { getContext, applySettings }) {
  if (!tabId || typeof applySettings !== 'function') return;
  _appliers[tabId] = { getContext: getContext || (() => ({})), applySettings };
}

function _activeTabId() {
  return document.querySelector('.rail-tab.active')?.dataset.tab || '';
}

export function hasApplier(tabId) {
  return !!_appliers[tabId];
}

export function activeTabHasApplier() {
  return hasApplier(_activeTabId());
}

export async function askAI(query) {
  const q = (query || '').trim();
  if (!q) return;
  const tabId = _activeTabId();
  const entry = _appliers[tabId];
  if (!entry) {
    toast(`AI assist not wired for ${tabId || 'this tab'}`, 'info');
    return;
  }
  let context = {};
  try { context = entry.getContext() || {}; } catch (_) {}
  try {
    const res = await apiFetch('/api/ai-intent', {
      method: 'POST',
      body: JSON.stringify({ tab: tabId, query: q, context }),
    });
    const settings = res.settings || {};
    const applied = Object.keys(settings);
    if (applied.length) {
      try { entry.applySettings(settings); }
      catch (e) { toast(`Apply failed: ${e.message}`, 'error'); return; }
    }
    const changes = applied.length ? ` (${applied.length} change${applied.length > 1 ? 's' : ''})` : '';
    toast(`${res.reply || 'Done'}${changes}`, 'success', { duration: 6000 });
  } catch (e) {
    toast(e.message || 'AI request failed', 'error');
  }
}
