/**
 * Drop Cat Go Studio -- shared "is this service cold?" pre-submit check.
 *
 * M18: several tabs submit a GPU job with no idea whether the service that
 * will run it is even loaded, so a cold WanGP/ACE-Step/Forge shows up as
 * minutes of fake progress with no explanation. This helper lets any tab
 * ask, in one call, "is <service> warm enough that I should submit without
 * warning the user" -- and if not, hands back a ready-to-display sentence
 * instead of a bare state string.
 *
 * checkServiceWarning(serviceName, opts?) -> Promise<string|null>
 *
 *   serviceName   any key /api/services returns: 'wangp' | 'acestep' |
 *                 'forge' | 'featherless'.
 *   opts.okStates array of states considered "warm, nothing to say"
 *                 (default: ['running']). Pass e.g. ['running','ready'] for
 *                 a service where "ready" (configured, starts on first
 *                 use) shouldn't count as cold.
 *   opts.customMessages  { [state]: string } -- override/extend the
 *                 built-in per-state wording for this call site.
 *
 * Returns a short, user-facing warning string when the service is NOT in
 * an okState, or null when it's warm (or the check itself couldn't be
 * completed -- fails OPEN so a flaky /api/services fetch never blocks
 * submission just because the status probe failed).
 *
 * This only ANSWERS the question -- it does not toast, block, or disable
 * anything. Callers decide what to do with the string (toast it, show it
 * inline, etc).
 *
 * Example:
 *   import { checkServiceWarning } from './shell/service-check.js';
 *   const warn = await checkServiceWarning('wangp', { okStates: ['running', 'ready'] });
 *   if (warn) toast(warn, 'info');
 */

const _DEFAULT_MESSAGES = {
  wangp: {
    not_configured: 'WanGP is not configured -- video generation will fail until it is set up in Settings.',
    not_running:    'WanGP is not running yet -- this job will wait for it to load (2-3 min) before it starts.',
    ready:          'WanGP is configured but not loaded yet -- this job will wait for it to load (2-3 min).',
    starting:       'WanGP is still loading -- this job will wait for it.',
    error:          'WanGP reported an error -- check Settings or the service panel before submitting.',
    unknown:        'WanGP status is still being checked.',
  },
  acestep: {
    not_configured: 'ACE-Step is not configured -- music generation will fail until it is set up in Settings.',
    not_running:    'ACE-Step is not running -- it starts automatically when music is needed (adds ~90s the first time).',
    starting:       'ACE-Step is still loading -- music generation will wait for it.',
    error:          'ACE-Step reported an error -- check the service panel before submitting.',
    unknown:        'ACE-Step status is still being checked.',
  },
  forge: {
    not_running: 'Forge is not running -- start it from its own GUI before using Chat or Image Studio.',
    unknown:     'Forge status is still being checked.',
  },
  featherless: {
    not_running:    'The uncensored AI backend is not reachable -- NSFW prompts may fail.',
    not_configured: 'The uncensored AI backend is not configured -- NSFW prompts may fail.',
  },
};

export async function checkServiceWarning(serviceName, opts = {}) {
  const okStates = opts.okStates || ['running'];
  try {
    const r = await fetch('/api/services', { cache: 'no-store' });
    if (!r.ok) return null;
    const data = await r.json();
    const info = data[serviceName];
    if (!info) return null;
    const state = info.state || 'unknown';
    if (okStates.includes(state)) return null;
    const messages = { ..._DEFAULT_MESSAGES[serviceName], ...(opts.customMessages || {}) };
    return messages[state] || info.message || `${serviceName} is ${state}.`;
  } catch (_) {
    return null; // fail open -- a status-check failure must never block submission
  }
}
