/**
 * Drop Cat Go Studio -- Keyboard shortcuts (WS9).
 * Registers global keyboard shortcuts. Disabled when focus is in text inputs.
 */

const _shortcuts = [];

/**
 * Register a shortcut.
 * @param {object} def { key, ctrl?, shift?, alt?, action, description, global? }
 *   global: if true, fires even in text inputs (e.g. Ctrl+K)
 */
export function register(def) {
  _shortcuts.push(def);
}

function _matches(e, def) {
  const ctrlOrMeta = e.ctrlKey || e.metaKey;
  if (def.ctrl  && !ctrlOrMeta) return false;
  if (!def.ctrl && ctrlOrMeta && !def.global) return false;
  if (def.shift  !== undefined && def.shift  !== e.shiftKey) return false;
  if (def.alt    !== undefined && def.alt    !== e.altKey)   return false;
  return e.key === def.key || e.code === def.key;
}

function _inTextInput(e) {
  const tag = e.target?.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' ||
         e.target?.isContentEditable;
}

document.addEventListener('keydown', e => {
  for (const def of _shortcuts) {
    if (!_matches(e, def)) continue;
    if (_inTextInput(e) && !def.global) continue;
    e.preventDefault();
    def.action(e);
    break;
  }
});

export function getShortcuts() { return [..._shortcuts]; }
