/**
 * Drop Cat Go Studio -- Preset / recipe system (WS8).
 * Save/restore full tab configurations. Per-tab getSettings()/applySettings().
 * Presets persisted to /api/presets (disk-backed SQLite on server).
 */

import { apiFetch, toast } from './toast.js?v=20260429d';
import { registerItems } from './command-palette.js?v=20260421c';

// Tab settings providers: { getSettings(), applySettings(s) }
const _providers = {};
let _presets = [];

export function registerPresetProvider(tabId, provider) {
  _providers[tabId] = provider;
}

export async function init() {
  await _load();
  _registerPaletteItems();
}

async function _load() {
  try {
    const data = await apiFetch('/api/presets', { context: 'presets.load' });
    _presets = data.presets || [];
  } catch (_) { _presets = []; }
}

function _registerPaletteItems() {
  const items = _presets.map(p => ({
    label: `Preset: ${p.name}`,
    hint:  p.tab,
    group: 'Presets',
    icon:  '&#128190;',
    action: () => applyPreset(p.id),
  }));
  registerItems(items);
}

export async function savePreset(tabId, name) {
  const provider = _providers[tabId];
  if (!provider) { toast('No preset provider for this tab', 'error'); return; }
  const settings = provider.getSettings ? provider.getSettings() : {};
  try {
    const saved = await apiFetch('/api/presets', {
      method: 'POST',
      body: JSON.stringify({ tab: tabId, name, settings }),
      context: 'presets.save',
    });
    _presets.unshift(saved);
    toast(`Preset "${name}" saved`, 'success');
    _registerPaletteItems();
    return saved;
  } catch (_) {}
}

export async function applyPreset(presetId) {
  const preset = _presets.find(p => p.id === presetId);
  if (!preset) { toast('Preset not found', 'error'); return; }
  const provider = _providers[preset.tab];
  if (!provider?.applySettings) { toast(`No provider for tab: ${preset.tab}`, 'error'); return; }
  provider.applySettings(preset.settings || {});
  toast(`Applied preset: ${preset.name}`, 'success');
}

export async function deletePreset(presetId) {
  try {
    await apiFetch(`/api/presets/${presetId}`, { method: 'DELETE', context: 'presets.delete' });
    _presets = _presets.filter(p => p.id !== presetId);
    toast('Preset deleted', 'success');
    _registerPaletteItems();
  } catch (_) {}
}

export function getPresets(tabId) {
  return tabId ? _presets.filter(p => p.tab === tabId) : [..._presets];
}

/** Prompt user for a name and save the current tab's preset. */
export async function promptAndSave(tabId) {
  const name = window.prompt('Preset name:');
  if (!name?.trim()) return;
  return savePreset(tabId, name.trim());
}

/** Export presets as a JSON download. */
export function exportPresets(tabId) {
  const data = tabId ? getPresets(tabId) : _presets;
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `dropcat-presets-${tabId || 'all'}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/** Import presets from a JSON file. */
export async function importPresets(file, tabId) {
  try {
    const text = await file.text();
    const data = JSON.parse(text);
    const items = Array.isArray(data) ? data : [data];
    let count = 0;
    for (const item of items) {
      if (!item.name || !item.settings) continue;
      await apiFetch('/api/presets', {
        method: 'POST',
        body: JSON.stringify({ tab: tabId || item.tab || 'sd-prompts', name: item.name, settings: item.settings }),
        context: 'presets.import',
      });
      count++;
    }
    await _load();
    toast(`Imported ${count} preset${count !== 1 ? 's' : ''}`, 'success');
    _registerPaletteItems();
  } catch (e) {
    toast(`Import failed: ${e.message}`, 'error');
  }
}
