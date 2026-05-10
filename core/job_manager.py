"""Unified job queue with sequential GPU access enforcement.

All features that use WanGP (Fun Videos, Video Bridges) share a single
GPU queue so only one generation runs at a time. Non-GPU jobs (Image2Video,
Video Tools, SD Prompts) run on their own independent threads.
"""
import json
import logging
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable

from core import config as cfg

log = logging.getLogger(__name__)

QUEUE_SAVE_FILE = Path(__file__).resolve().parent.parent / "queue_save.json"

# Job types
JOB_I2V = "i2v"
JOB_FUN_VIDEO = "fun_video"
JOB_FUN_MULTI_VIDEO = "fun_multi_video"
JOB_BRIDGE = "bridge"
JOB_VIDEO_TOOL = "video_tool"
JOB_SD_PROMPT = "sd_prompt"

# Types that require exclusive GPU access
GPU_JOB_TYPES = {JOB_FUN_VIDEO, JOB_FUN_MULTI_VIDEO, JOB_BRIDGE}


class Job:
    """Represents a single processing job."""

    def __init__(self, job_type: str, label: str = "", timeout_seconds: int | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.type = job_type
        self.label = label
        self.status = "queued"      # queued | running | done | error | stopped | cancelled
        self.progress = 0           # 0-100
        self.message = ""
        self.output = None          # output path(s)
        self.meta: dict = {}        # feature-specific metadata
        self.error: str | None = None
        self.created_at = time.time()
        self.started_at: float | None = None   # set when status -> running
        self.finished_at: float | None = None  # set when terminal (done/error/stopped/cancelled)
        self.stop_event = threading.Event()
        self.timeout_seconds: int | None = timeout_seconds  # overrides gpu_job_timeout_seconds
        self._worker_fn: Callable | None = None
        self._worker_args: tuple = ()
        self._worker_kwargs: dict = {}

    def update(self, **kwargs):
        """Thread-safe attribute update."""
        for k, v in kwargs.items():
            setattr(self, k, v)

    def to_dict(self) -> dict:
        """Serialize for API response (excludes internal fields)."""
        # Compute live elapsed: started → finished if done, else started → now
        elapsed = None
        if self.started_at is not None:
            end = self.finished_at if self.finished_at is not None else time.time()
            elapsed = max(0.0, end - self.started_at)
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "output": self.output,
            "meta": self.meta,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": elapsed,
        }


class JobManager:
    """Manages job lifecycle across all features.

    GPU jobs (fun_video, bridge) are queued and run sequentially.
    Non-GPU jobs run immediately in their own threads.
    """

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._paused = False   # when True, GPU worker finishes current job then idles

        # GPU queue
        self._gpu_queue: deque[str] = deque()
        self._gpu_event = threading.Event()
        self._gpu_worker = threading.Thread(
            target=self._gpu_queue_worker, daemon=True,
        )
        self._gpu_worker.start()

    def submit(
        self,
        job_type: str,
        worker_fn: Callable,
        *args,
        label: str = "",
        timeout_seconds: int | None = None,
        **kwargs,
    ) -> Job:
        """Submit a new job. Returns the Job immediately.

        Raises RuntimeError if the GPU queue is full (GPU job types only).

        worker_fn signature: worker_fn(job: Job, *args, **kwargs)
        The function should update job.progress, job.message, etc.
        It should check job.stop_event.is_set() periodically.
        """
        job = Job(job_type, label=label, timeout_seconds=timeout_seconds)
        job._worker_fn = worker_fn
        job._worker_args = args
        job._worker_kwargs = kwargs

        with self._lock:
            if job_type in GPU_JOB_TYPES:
                max_depth = int(cfg.get("gpu_queue_max_depth") or 3)
                active_gpu = sum(
                    1 for j in self._jobs.values()
                    if j.type in GPU_JOB_TYPES and j.status in ("running", "queued")
                )
                if active_gpu >= max_depth:
                    raise RuntimeError(
                        f"Queue is full ({active_gpu}/{max_depth} video jobs) — "
                        f"wait for a video to finish before adding more"
                    )
            self._jobs[job.id] = job
            if job_type in GPU_JOB_TYPES:
                self._gpu_queue.append(job.id)

        if job_type in GPU_JOB_TYPES:
            self._gpu_event.set()
            log.info("Job %s (%s) queued for GPU — position %d",
                     job.id, job_type, len(self._gpu_queue))
        else:
            # Non-GPU: run immediately in a thread
            job.status = "running"
            job.started_at = time.time()
            t = threading.Thread(
                target=self._run_job, args=(job,), daemon=True,
            )
            t.start()

        return job

    def get(self, job_id: str) -> Job | None:
        """Get a job by ID."""
        with self._lock:
            return self._jobs.get(job_id)

    def stop(self, job_id: str) -> bool:
        """Request a job to stop. Returns True if the job was found."""
        job = self.get(job_id)
        if job is None:
            return False
        job.stop_event.set()
        if job.status == "queued":
            with self._lock:
                job.status = "cancelled"
                job.message = "Cancelled by user"
                job.finished_at = time.time()
                try:
                    self._gpu_queue.remove(job_id)
                except ValueError:
                    pass
        return True

    def dismiss(self, job_id: str) -> bool:
        """Permanently remove a finished/failed job from memory. Returns True if removed."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in ("running", "queued"):
                return False  # refuse to remove active jobs
            del self._jobs[job_id]
            return True

    def dismiss_all_finished(self) -> int:
        """Remove all completed/failed/cancelled jobs. Returns count removed."""
        with self._lock:
            removable = [
                jid for jid, j in self._jobs.items()
                if j.status in ("done", "error", "stopped", "cancelled")
            ]
            for jid in removable:
                del self._jobs[jid]
            return len(removable)

    def pause(self):
        """Pause the GPU queue — current job finishes, no new jobs start until resume()."""
        self._paused = True

    def resume(self):
        """Resume the GPU queue."""
        self._paused = False
        self._gpu_event.set()

    def cancel_all_queued(self) -> int:
        """Cancel every waiting (not yet running) GPU job. Returns count cancelled."""
        with self._lock:
            count = 0
            now = time.time()
            for jid in list(self._gpu_queue):
                job = self._jobs.get(jid)
                if job and job.status == "queued":
                    job.status = "cancelled"
                    job.message = "Cancelled by user"
                    job.finished_at = now
                    job.stop_event.set()
                    count += 1
            self._gpu_queue.clear()
            return count

    def submit_with_prep(
        self,
        job_type: str,
        prep_fn: Callable,
        gpu_fn: Callable,
        *args,
        label: str = "",
        timeout_seconds: int | None = None,
        **kwargs,
    ) -> Job:
        """Submit a GPU job with a non-blocking prep phase.

        prep_fn(job, *args, **kwargs) runs immediately in a background thread
        with no GPU lock — it can run concurrently with other GPU jobs in the
        queue. When prep finishes the job automatically enters the GPU queue
        and gpu_fn runs under the normal GPU lock.

        Both functions receive the same Job object and *args/**kwargs, so
        prep_fn can write results into a mutable args dict for gpu_fn to read.
        """
        job = Job(job_type, label=label, timeout_seconds=timeout_seconds)
        job._worker_fn = gpu_fn
        job._worker_args = args
        job._worker_kwargs = kwargs

        with self._lock:
            if job_type in GPU_JOB_TYPES:
                max_depth = int(cfg.get("gpu_queue_max_depth") or 3)
                # Only count jobs that actually hold or are waiting for the GPU.
                # "preparing" jobs are doing CPU work (LLM calls) and have not
                # yet entered the GPU queue -- counting them would block new
                # submissions while the GPU sits idle.
                active_gpu = sum(
                    1 for j in self._jobs.values()
                    if j.type in GPU_JOB_TYPES and j.status in ("running", "queued")
                )
                if active_gpu >= max_depth:
                    raise RuntimeError(
                        f"Queue is full ({active_gpu}/{max_depth} video jobs) -- "
                        f"wait for a video to finish before adding more"
                    )
            self._jobs[job.id] = job

        def _run_prep():
            job.status = "preparing"
            job.started_at = time.time()
            try:
                prep_fn(job, *args, **kwargs)
            except Exception as e:
                log.exception("Prep phase failed for job %s: %s", job.id, e)
                job.status = "error"
                job.error = str(e)
                job.message = f"Prep failed: {e}"
                job.finished_at = time.time()
                return

            if job.stop_event.is_set():
                job.status = "stopped"
                job.message = "Stopped"
                job.finished_at = time.time()
                return

            # Prep done — hand off to GPU queue
            job.status = "queued"
            job.message = "Waiting for GPU…"
            with self._lock:
                self._gpu_queue.append(job.id)
            self._gpu_event.set()
            log.info("Job %s (%s) prep done, queued for GPU — position %d",
                     job.id, job_type, len(self._gpu_queue))

        t = threading.Thread(target=_run_prep, daemon=True)
        t.start()
        log.info("Job %s (%s) prep phase starting", job.id, job_type)
        return job

    def retry(self, job_id: str):
        """Re-submit a failed/stopped job with the same worker and arguments."""
        with self._lock:
            original = self._jobs.get(job_id)
            if original is None or original.status in ("running", "queued"):
                return None
            if original._worker_fn is None:
                return None
        # Submit outside the lock to avoid deadlock in submit()
        try:
            new_job = self.submit(
                original.type,
                original._worker_fn,
                *original._worker_args,
                label=original.label,
                **original._worker_kwargs,
            )
            new_job.meta.update(original.meta)
            return new_job
        except RuntimeError:
            return None

    def promote(self, job_id: str) -> bool:
        """Move a queued job to the front of the GPU queue. Returns True if moved."""
        with self._lock:
            if job_id not in self._gpu_queue:
                return False
            self._gpu_queue.remove(job_id)
            self._gpu_queue.appendleft(job_id)
            return True

    def queue_position(self, job_id: str) -> int | None:
        """Return queue position for a GPU job. 0 = running, 1+ = waiting. None if not queued."""
        try:
            idx = list(self._gpu_queue).index(job_id)
            return idx
        except ValueError:
            return None

    def is_gpu_busy(self) -> bool:
        """Return True if a GPU job is currently running (not just queued)."""
        with self._lock:
            return any(
                j.type in GPU_JOB_TYPES and j.status == "running"
                for j in self._jobs.values()
            )

    def get_job_info(self, job_id: str) -> dict | None:
        """Get job dict with queue position included."""
        job = self.get(job_id)
        if job is None:
            return None
        info = job.to_dict()
        pos = self.queue_position(job_id)
        info["queue_position"] = pos
        return info

    def list_jobs(self, job_type: str | None = None, limit: int = 50) -> list[dict]:
        """List jobs, optionally filtered by type."""
        with self._lock:
            jobs = list(self._jobs.values())
        if job_type:
            jobs = [j for j in jobs if j.type == job_type]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return [j.to_dict() for j in jobs[:limit]]

    def queue_status(self) -> dict:
        """Return current queue state."""
        with self._lock:
            all_jobs = list(self._jobs.values())
        running = sorted(
            [j.to_dict() for j in all_jobs if j.status in ("running", "preparing")],
            key=lambda j: j["created_at"],
        )
        queued = sorted(
            [j.to_dict() for j in all_jobs if j.status == "queued"],
            key=lambda j: j["created_at"],  # oldest first = next-to-run at top
        )
        recent = [
            j.to_dict() for j in all_jobs
            if j.status in ("done", "error", "stopped", "cancelled")
        ]
        recent.sort(key=lambda j: j["created_at"], reverse=True)
        return {
            "running": running,
            "queued": queued,
            "completed": recent[:20],
            "gpu_queue_length": len(self._gpu_queue),
            "paused": self._paused,
        }

    def save_queue(self) -> int:
        """Write all waiting (queued) jobs to QUEUE_SAVE_FILE. Returns count saved."""
        with self._lock:
            waiting = [j for j in self._jobs.values() if j.status == "queued"]
        waiting.sort(key=lambda j: j.created_at)

        records = []
        for job in waiting:
            feature = job.meta.get("feature")
            if not feature:
                continue
            if len(job._worker_args) < 2:
                continue
            # Strip internal prep-phase keys (_story_arc, _clip_durations, etc.) —
            # prep will re-run on restore so fresh values are computed.
            raw_settings = job._worker_args[1]
            settings = {k: v for k, v in raw_settings.items() if not k.startswith("_")} \
                if isinstance(raw_settings, dict) else raw_settings
            try:
                serialized = [job._worker_args[0], settings]
                json.dumps(serialized, default=str)
            except Exception as e:
                log.warning("[queue-save] Job %s not JSON-serializable, skipping: %s", job.id, e)
                continue
            records.append({
                "feature":         feature,
                "label":           job.label,
                "timeout_seconds": job.timeout_seconds,
                "args":            serialized,
            })

        QUEUE_SAVE_FILE.write_text(
            json.dumps({"saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "jobs": records},
                       indent=2, default=str),
            encoding="utf-8",
        )
        log.info("[queue-save] Saved %d queued jobs to %s", len(records), QUEUE_SAVE_FILE)
        return len(records)

    def restore_queue(self, registry: dict) -> tuple[int, int]:
        """Re-submit jobs from QUEUE_SAVE_FILE using registry[feature](args, label, timeout).

        Returns (restored_count, failed_count).
        """
        if not QUEUE_SAVE_FILE.exists():
            return 0, 0
        try:
            data = json.loads(QUEUE_SAVE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.error("[queue-restore] Could not read save file: %s", e)
            return 0, 0

        restored = failed = 0
        for record in data.get("jobs", []):
            feature = record.get("feature")
            handler = registry.get(feature)
            if not handler:
                log.warning("[queue-restore] No restore handler for %r — skipping", feature)
                failed += 1
                continue
            try:
                job = handler(
                    record.get("args", []),
                    label=record.get("label", feature),
                    timeout_seconds=record.get("timeout_seconds"),
                )
                if job:
                    restored += 1
                    log.info("[queue-restore] Restored %r job — new id %s", feature, job.id)
            except Exception as e:
                log.error("[queue-restore] Failed to restore %r job: %s", feature, e)
                failed += 1

        log.info("[queue-restore] Restored %d, failed %d", restored, failed)
        try:
            QUEUE_SAVE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return restored, failed

    def cleanup(self, max_age_hours: int = 24):
        """Remove old completed jobs from memory."""
        cutoff = time.time() - (max_age_hours * 3600)
        with self._lock:
            to_remove = [
                jid for jid, j in self._jobs.items()
                if j.status in ("done", "error", "stopped", "cancelled")
                and j.created_at < cutoff
            ]
            for jid in to_remove:
                del self._jobs[jid]

    # ── Internal ──────────────────────────────────────────────────────────

    def _gpu_queue_worker(self):
        """Background thread that processes GPU jobs sequentially.

        The outer while True is wrapped in a broad except so no exception —
        including CUDA RuntimeErrors from torch.cuda.empty_cache() — can ever
        kill this thread.  A dead worker means every subsequent GPU job hangs
        as 'queued' forever, which is far worse than logging and continuing.
        """
        while True:
            try:
                self._gpu_event.wait()
                self._gpu_event.clear()

                while self._gpu_queue and not self._paused:
                    with self._lock:
                        if not self._gpu_queue:
                            break
                        job_id = self._gpu_queue[0]
                    job = self.get(job_id)

                    if job is None or job.status == "cancelled":
                        with self._lock:
                            try:
                                if self._gpu_queue and self._gpu_queue[0] == job_id:
                                    self._gpu_queue.popleft()
                            except IndexError:
                                pass
                        continue

                    job.status = "running"
                    job.message = "Starting..."
                    # Set started_at if prep didn't already (direct GPU submit)
                    if job.started_at is None:
                        job.started_at = time.time()
                    log.info("GPU job %s (%s) starting", job.id, job.type)

                    timeout = job.timeout_seconds or cfg.get("gpu_job_timeout_seconds") or 1800
                    worker = threading.Thread(
                        target=self._run_job, args=(job,), daemon=True,
                    )
                    worker.start()
                    worker.join(timeout=timeout)
                    if worker.is_alive():
                        job.stop_event.set()
                        job.status = "error"
                        job.error = f"Job timed out after {timeout} seconds"
                        job.message = f"Timed out after {timeout}s"
                        job.finished_at = time.time()
                        log.error("Job %s timed out after %ds", job.id, timeout)
                        # Wait for the thread to actually exit before starting the next
                        # GPU job. Without this wait, the old thread can still be blocking
                        # on a WanGP HTTP call when the next job submits, causing two
                        # simultaneous WanGP generations and guaranteed VRAM OOM.
                        log.info("Waiting up to 30s for timed-out job thread to exit...")
                        worker.join(timeout=30)
                        if worker.is_alive():
                            # Thread is stuck — most likely blocked on a WanGP HTTP call
                            # (e.g. polling /status or /generate while WanGP is mid-generation).
                            # Restarting the WanGP worker closes the listening socket, which
                            # causes the blocked HTTP call to raise a ConnectionError and lets
                            # the thread exit on its next stop_check iteration.
                            log.warning(
                                "Job %s thread still alive after 30s grace — "
                                "restarting WanGP worker to unblock stuck connection",
                                job.id,
                            )
                            try:
                                from services import manager as _svc
                                _svc.stop_service("wangp")
                            except Exception as _e:
                                log.warning("Could not stop WanGP to unblock stuck thread: %s", _e)
                            worker.join(timeout=15)
                            if worker.is_alive():
                                log.error(
                                    "Job %s thread STILL alive after WanGP stop — "
                                    "proceeding anyway; VRAM contention possible",
                                    job.id,
                                )
                                job.message = (
                                    f"Timed out after {timeout}s — WanGP restart failed; "
                                    "restart the app if the next job hangs"
                                )

                    with self._lock:
                        try:
                            if self._gpu_queue and self._gpu_queue[0] == job_id:
                                self._gpu_queue.popleft()
                        except IndexError:
                            pass

                    # VRAM cleanup between GPU jobs
                    import gc
                    gc.collect()
                    try:
                        import torch
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                    except Exception as _cuda_exc:
                        # CUDA RuntimeError must not kill the worker thread
                        log.debug("torch.cuda.empty_cache failed: %s", _cuda_exc)
                    time.sleep(1)

            except Exception as _worker_exc:
                log.exception(
                    "GPU queue worker caught unexpected exception — "
                    "continuing after 2s (job %s may need retry): %s",
                    locals().get('job_id', '?'), _worker_exc,
                )
                time.sleep(2)

    def _run_job(self, job: Job):
        """Execute a job's worker function with error handling."""
        try:
            job._worker_fn(job, *job._worker_args, **job._worker_kwargs)
            if job.stop_event.is_set():
                job.status = "stopped"
                job.message = "Stopped by user"
            elif job.status == "running":
                job.status = "done"
                job.progress = 100
                if not job.message:
                    job.message = "Complete"
        except Exception as e:
            log.exception("Job %s failed: %s", job.id, e)
            if job.status not in ("done", "stopped", "cancelled"):
                job.status = "error"
                job.error = str(e)
                job.message = f"Error: {e}"
        finally:
            # Single source of truth for terminal timestamp -- runs whether the
            # worker returned, raised, or hit the stop event.
            if job.finished_at is None:
                job.finished_at = time.time()
