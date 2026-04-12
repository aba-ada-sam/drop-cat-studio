/**
 * Drop Cat Go Studio — Cross-tab handoff store.
 *
 * One pending handoff at a time. A tab "sends" by calling handoff(),
 * then navigates. The destination tab calls consumeHandoff() when it
 * activates (via app.js switchTab) and receives the payload.
 *
 * Usage:
 *   import { handoff, consumeHandoff } from './handoff.js';
 *
 *   // sender:
 *   handoff('fun-videos', { type: 'image', path: '/output/...' });
 *   document.querySelector('[data-tab="fun-videos"]').click();
 *
 *   // receiver (called by app.js switchTab after tab activates):
 *   const data = consumeHandoff('fun-videos');
 *   if (data) applyHandoff(data);
 */

let _pending = null;

export function handoff(target, data) {
  _pending = { target, ...data };
}

export function consumeHandoff(target) {
  if (_pending?.target === target) {
    const d = { ..._pending };
    _pending = null;
    return d;
  }
  return null;
}
