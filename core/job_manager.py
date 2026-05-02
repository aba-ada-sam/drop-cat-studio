"""Unified job queue with sequential GPU access enforcement.

All features that use WanGP (Fun Videos, Video Bridges) share a single
GPU queue so only one generation runs at a time. Non-GPU jobs (Image2Video,
Video Tools, SD Prompts) run on their own independent threads.
"""
import logging
import threading
import time
import uuid
from collections import deque
from typing import Callable

from core import config as cfg

log = logging.getLogger(__name__)

# Job types
JOB_I2V = "i2v"
JOB_FUN_VIDEO = "fun_video"
JOB_BRIDGE = "bridge"
JOB_VIDEO_TOOL = "video_tool"
JOB_SD_PROMPT = "sd_prompt"

# Types that require exclusive GPU access
GPU_JOB_TYPES = {JOB_FUN_VIDEO, JOB_BRIDGE}


class Job:
    """Represents a single processing job."""

    def __init__(self, job_type: str, label: str = ""):
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
        self.stop_event = threading.Event()
        self._worker_fn: Callable | None = None
        self._worker_args: tuple = ()
        self._worker_kwargs: dict = {}

    def update(self, **kwargs):
        """Thread-safe attribute update."""
        for k, v in kwargs.items():
            setattr(self, k, v)

    def to_dict(self) -> dict:
        """Serialize for API response (excludes internal fields)."""
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
        **kwargs,
    ) -> Job:
        """Submit a new job. Returns the Job immediately.

        Raises RuntimeError if the GPU queue is full (GPU job types only).

        worker_fn signature: worker_fn(job: Job, *args, **kwargs)
        The function should update job.progress, job.message, etc.
        It should check job.stop_event.is_set() periodically.
        """
        job = Job(job_type, label=label)
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
            job.status = "cancelled"
            job.message = "Cancelled by user"
            # Remove from GPU queue
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
            for jid in list(self._gpu_queue):
                job = self._jobs.get(jid)
                if job and job.status == "queued":
                    job.status = "cancelled"
                    job.message = "Cancelled by user"
                    job.stop_event.set()
                    count += 1
            self._gpu_queue.clear()
            return count

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
            [j.to_dict() for j in all_jobs if j.status == "running"],
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
                    job_id = self._gpu_queue[0]
                    job = self.get(job_id)

                    if job is None or job.status == "cancelled":
                        self._gpu_queue.popleft()
                        continue

                    job.status = "running"
                    job.message = "Starting..."
                    log.info("GPU job %s (%s) starting", job.id, job.type)

                    timeout = cfg.get("gpu_job_timeout_seconds") or 600
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

                    self._gpu_queue.popleft()

                    # VRAM cleanup between GPU jobs
                    import gc
                    gc.collect()
                    try:
                        import torch
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                    except Exception:
                        pass  # CUDA RuntimeError must not kill the worker thread
                    time.sleep(1)

            except Exception as _worker_exc:
                log.exception(
                    "GPU queue worker caught unexpected exception — "
                    "continuing after 2s (job %s may need retry): %s",
                    job_id if "job_id" in dir() else "?", _worker_exc,
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
            job.status = "error"
            job.error = str(e)
            job.message = f"Error: {e}"
