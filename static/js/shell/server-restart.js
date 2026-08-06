/**
 * Drop Cat Go Studio -- shared "wait for the server to come back after a
 * restart" helpers.
 *
 * Three call sites restart the server and need to detect when it's back:
 * the Settings modal's "Restart Server" button, the floating restart banner
 * (app.js), and the Queue tab's "Save & Restart" button (tab-queue.js).
 * All three used to hand-roll (or, for Queue, never implement at all --
 * H13) the same poll loop. This module is the one implementation.
 *
 * captureServerBaseline() -- read boot_git_hash/pid BEFORE the restart is
 *   triggered, so pollUntilRestarted() can tell "same server, still up"
 *   apart from "new server, actually restarted" (a hash-only check misses
 *   a same-code reboot).
 *
 * pollUntilRestarted(baseline, opts) -- poll /api/system every
 *   opts.intervalMs (default 2000ms) until a restart is confirmed by ANY
 *   of: new boot_git_hash, new pid, or the server was observed down and
 *   answers again. Calls opts.onPollFail(streak) on every consecutive miss
 *   so callers can update their UI FAST instead of staying silent until
 *   the deadline (C4) -- e.g. show "waiting for server..." a few seconds
 *   in instead of nothing for up to 90s. opts.onTimeout() fires once if
 *   opts.deadlineMs (default 90000ms) passes with no restart seen.
 *   opts.onRestarted() fires once confirmed, before the reload.
 *   opts.reload (default true) -- location.reload() once confirmed; pass
 *   false to stay on the current page (the Settings modal button does this
 *   so it doesn't lose the user's place).
 *
 * restartAndReconnect(opts) -- convenience for callers that trigger their
 *   OWN restart via POST /api/app/restart (the header button and the
 *   banner): captures the baseline, runs opts.onBeforePost() as an
 *   abort/confirm gate (return false to cancel), POSTs, then calls
 *   pollUntilRestarted(). The Queue tab triggers its restart via
 *   /api/jobs/save-and-restart instead (it needs to save the queue first),
 *   so it calls captureServerBaseline() + pollUntilRestarted() directly
 *   around its own POST rather than using this wrapper.
 */

export async function captureServerBaseline() {
  // Retried once: a single blip on the baseline read must not count as
  // "the server was down" -- combined with a no-op restart it would fake a
  // down-then-up success and report a restart that didn't happen.
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const sys = await fetch('/api/system', { cache: 'no-store' }).then(r => r.json());
      return { hash: sys.boot_git_hash || null, pid: sys.pid || null, alreadyDown: false };
    } catch (_) { /* retry once */ }
  }
  // Couldn't read a baseline at all -- server already dead (e.g. clicked
  // after a crash). Any successful poll below means it came back.
  return { hash: null, pid: null, alreadyDown: true };
}

export function pollUntilRestarted(baseline, opts = {}) {
  const deadline = Date.now() + (opts.deadlineMs ?? 90000);
  const interval = opts.intervalMs ?? 2000;
  let sawDown   = !!baseline.alreadyDown;
  let pollFails = 0;

  return new Promise((resolve) => {
    const timer = setInterval(async () => {
      if (Date.now() > deadline) {
        clearInterval(timer);
        opts.onTimeout?.();
        resolve(false);
        return;
      }
      try {
        const ctrl = new AbortController();
        const tid  = setTimeout(() => ctrl.abort(), 1500);
        const sys  = await fetch('/api/system', { cache: 'no-store', signal: ctrl.signal }).then(r => r.json());
        clearTimeout(tid);
        // Restart confirmed by ANY of: new git hash, new server PID, or the
        // server was observed down and answers again. Hash alone misses a
        // same-code reboot (stale banner reporting failure on success).
        const restarted =
          (sys.boot_git_hash && baseline.hash !== null && sys.boot_git_hash !== baseline.hash) ||
          (sys.pid && baseline.pid !== null && sys.pid !== baseline.pid) ||
          sawDown;
        if (restarted) {
          clearInterval(timer);
          opts.onRestarted?.();
          if (opts.reload ?? true) location.reload();
          resolve(true);
          return;
        }
        pollFails = 0;
      } catch (_) {
        // Require two consecutive misses before declaring "down" -- one
        // slow/aborted response must not fake a down-then-up success.
        pollFails += 1;
        if (pollFails >= 2) sawDown = true;
        opts.onPollFail?.(pollFails);
      }
    }, interval);
  });
}

export async function restartAndReconnect(opts = {}) {
  const baseline = await captureServerBaseline();
  if (opts.onBeforePost) {
    const proceed = await opts.onBeforePost();
    if (proceed === false) return false;
  }
  try {
    await fetch('/api/app/restart', { method: 'POST' });
  } catch (_) {
    // The connection can drop mid-response while app.py is exiting --
    // expected, not a failure. Keep polling regardless.
  }
  opts.onPosted?.();
  return pollUntilRestarted(baseline, opts);
}
