/**
 * Lip Sync tool (MuseTalk post-pass).
 *
 * Re-syncs the mouth of an existing video to its audio. Renders only when the
 * backend reports MuseTalk is installed (GET /api/lipsync/status). One click ->
 * POST /api/lipsync/run -> poll the job -> show the synced result.
 *
 * Needs a clear frontal face in the source video; abstract/non-face content
 * won't sync (MuseTalk no-ops or errors, surfaced in the status line).
 *
 *   mountLipSyncTool(container, { videoPath, audioPath, onApplied })
 */
import { el } from '../components.js?v=20260507a';
import { apiFetch, toast } from '../shell/toast.js?v=20260518a';

export async function mountLipSyncTool(container, opts = {}) {
  const videoPath = opts.videoPath || '';
  if (!videoPath) return;

  // Availability gate: only show the tool if MuseTalk is installed.
  let available = false;
  try { available = (await apiFetch('/api/lipsync/status')).available; } catch (_) {}
  if (!available) return;

  container.innerHTML = '';
  const title = el('div', { style: 'font-size:13px; font-weight:700; color:var(--gold); margin-bottom:4px;', text: 'Lip Sync' });
  const hint = el('div', { style: 'font-size:11px; color:var(--text-3); margin-bottom:6px;',
    text: 'Re-sync the character’s mouth to the audio (MuseTalk). Needs a clear frontal face — abstract/non-face shots won’t sync.' });

  const btn = el('button', { class: 'btn btn-sm', style: 'background:var(--circus-red); color:#fff; font-weight:700;', text: 'Lip-sync to audio' });
  const status = el('div', { style: 'font-size:11px; color:var(--text-3); min-height:14px; margin-top:6px;' });
  const bar = el('div', { style: 'height:3px; background:var(--accent); width:0%; border-radius:2px; transition:width .3s;' });
  const barWrap = el('div', { style: 'display:none; height:3px; background:var(--border-2); border-radius:2px; overflow:hidden; margin-top:4px;' }, [bar]);

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    status.textContent = 'Submitting…';
    barWrap.style.display = 'block'; bar.style.width = '5%';
    try {
      const r = await apiFetch('/api/lipsync/run', {
        method: 'POST',
        body: JSON.stringify({ video_path: videoPath, audio_path: opts.audioPath || undefined }),
      });
      if (!r.job_id) throw new Error(r.error || 'No job started');
      const poll = setInterval(async () => {
        try {
          const j = await apiFetch(`/api/jobs/${r.job_id}`);
          bar.style.width = (j.progress || 5) + '%';
          status.textContent = j.message || 'Lip-syncing…';
          if (j.status === 'done') {
            clearInterval(poll);
            barWrap.style.display = 'none';
            status.textContent = 'Saved: ' + (j.output || '');
            btn.disabled = false;
            document.dispatchEvent(new Event('session-updated'));
            toast('Lip-synced video saved', 'success');
            if (opts.onApplied) opts.onApplied(j.output);
          } else if (j.status === 'error' || j.status === 'stopped') {
            clearInterval(poll);
            barWrap.style.display = 'none';
            status.textContent = 'Failed: ' + (j.error || j.message || j.status);
            btn.disabled = false;
          }
        } catch (_) {}
      }, 1500);
    } catch (e) {
      barWrap.style.display = 'none';
      status.textContent = 'Error: ' + e.message;
      btn.disabled = false;
    }
  });

  container.append(title, hint, btn, barWrap, status);
}
