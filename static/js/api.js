/**
 * Drop Cat Go Studio -- Shared API helpers.
 * Fetch wrapper, file upload, and generic job polling.
 */

/** JSON fetch wrapper with error handling. */
export async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...opts.headers },
    ...opts,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || err.detail || `HTTP ${res.status}`);
  }
  return res.json().catch(() => { throw new Error(`Non-JSON response from server (${res.status})`); });
}

/** Upload files via FormData. Returns parsed JSON response. */
export async function apiUpload(path, files) {
  const form = new FormData();
  for (const f of files) {
    form.append('files', f);
  }
  const res = await fetch(path, { method: 'POST', body: form });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || err.detail || `HTTP ${res.status}`);
  }
  return res.json().catch(() => { throw new Error(`Non-JSON response from server (${res.status})`); });
}

/**
 * Poll a job until completion.
 * @param {string} jobId
 * @param {(job: object) => void} onProgress - Called on each poll with job state
 * @param {(job: object) => void} onDone - Called when job completes successfully
 * @param {(error: string) => void} onError - Called on job failure
 * @param {number} interval - Poll interval in ms (default 1500)
 * @returns {{ stop: () => void }} - Call stop() to cancel polling
 */
export function pollJob(jobId, onProgress, onDone, onError, interval = 1500) {
  let timer = null;
  let stopped = false;

  let _netErrors = 0;

  async function tick() {
    if (stopped) return;
    try {
      const job = await api(`/api/jobs/${jobId}`);
      if (stopped) return;
      _netErrors = 0; // reset on any successful response

      if (job.status === 'done') {
        window.dispatchEvent(new CustomEvent('session-updated'));
        onDone(job);
        return;
      }
      if (job.status === 'error' || job.status === 'stopped' || job.status === 'cancelled') {
        onError(job.error || job.message || `Job ${job.status}`);
        return;
      }
      // Guard against null/unknown status -- avoids endless poll loop on server errors
      if (job.status && job.status !== 'running' && job.status !== 'queued' && job.status !== 'preparing') {
        onError(`Unexpected job status: ${job.status}`);
        return;
      }
      // No client-side wall-clock cutoff: multi-hour jobs (AI upscales, large
      // folder batches) are normal, and the server's status is authoritative --
      // when a job finishes, dies, or is stopped it flips to done/error/stopped
      // (handled above), which is what ends the poll. A genuinely wedged job is
      // the user's call to cancel, not something we time out from the browser.
      onProgress(job);
      timer = setTimeout(tick, interval);
    } catch (e) {
      if (stopped) return;
      // Only retry genuine network errors (TypeError = server unreachable / restarting).
      // HTTP errors like "Job not found" (404) are definitive -- retrying won't help.
      if (e instanceof TypeError && ++_netErrors <= 8) {
        timer = setTimeout(tick, 5000);
      } else {
        onError(e.message);
      }
    }
  }

  timer = setTimeout(tick, 300); // First poll quickly

  return {
    stop() {
      stopped = true;
      if (timer) clearTimeout(timer);
    },
  };
}

/** Stop a running job. */
export async function stopJob(jobId) {
  return api(`/api/jobs/${jobId}/stop`, { method: 'POST' });
}
