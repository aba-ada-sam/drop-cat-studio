/**
 * Drop Cat Go Studio -- palette-driven AI intent.
 * Each tab registers {getContext, applySettings} on init. When the user types
 * free text in the command palette and picks "Ask AI", we dispatch to the
 * currently active tab's applier.
 */
import { apiFetch, toast } from './toast.js?v=20260419h';

const _appliers = {};
const _HISTORY_KEY = 'dropcat_ai_intent_history';
const _HISTORY_MAX = 5;

export function getHistory() {
  try {
    const raw = localStorage.getItem(_HISTORY_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr.slice(0, _HISTORY_MAX) : [];
  } catch (_) { return []; }
}

function _pushHistory(query) {
  const cur = getHistory().filter(q => q !== query);
  cur.unshift(query);
  try { localStorage.setItem(_HISTORY_KEY, JSON.stringify(cur.slice(0, _HISTORY_MAX))); }
  catch (_) {}
}

export function clearHistory() {
  try { localStorage.removeItem(_HISTORY_KEY); } catch (_) {}
}

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

// Direct bridge for non-AI callers (e.g. gallery "Load Settings") to push
// a settings dict into a specific tab's applier without going through /ai-intent.
export function applySettingsToTab(tabId, settings) {
  const entry = _appliers[tabId];
  if (!entry || !settings || typeof settings !== 'object') return false;
  try { entry.applySettings(settings); return true; }
  catch (_) { return false; }
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
    if (!applied.length) {
      toast(`Couldn't adjust anything for "${q}". Try being more specific.`, 'info', { duration: 6000 });
      return;
    }
    try { entry.applySettings(settings); }
    catch (e) { toast(`Apply failed: ${e.message}`, 'error'); return; }
    _pushHistory(q);
    const summary = applied.length <= 3
      ? applied.join(', ')
      : `${applied.length} settings`;
    toast(`${res.reply || 'Done'} (${summary})`, 'success', { duration: 6000 });
  } catch (e) {
    toast(e.message || 'AI request failed', 'error');
  }
}
