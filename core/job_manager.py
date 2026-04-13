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

        worker_fn signature: worker_fn(job: Job, *args, **kwargs)
        The function should update job.progress, job.message, etc.
        It should check job.stop_event.is_set() periodically.
        """
        job = Job(job_type, label=label)
        job._worker_fn = worker_fn
        job._worker_args = args
        job._worker_kwargs = kwargs

        with self._lock:
            self._jobs[job.id] = job

        if job_type in GPU_JOB_TYPES:
            self._gpu_queue.append(job.id)
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

    def queue_position(self, job_id: str) -> int | None:
        """Return queue position for a GPU job. 0 = running, 1+ = waiting. None if not queued."""
        try:
            idx = list(self._gpu_queue).index(job_id)
            return idx
        except ValueError:
            return None

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
        running = [j.to_dict() for j in all_jobs if j.status == "running"]
        queued = [j.to_dict() for j in all_jobs if j.status == "queued"]
        recent = [
            j.to_dict() for j in all_jobs
            if j.status in ("done", "error", "stopped")
        ]
        recent.sort(key=lambda j: j["created_at"], reverse=True)
        return {
            "running": running,
            "queued": queued,
            "completed": recent[:20],
            "gpu_queue_length": len(self._gpu_queue),
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
        """Background thread that processes GPU jobs sequentially."""
        while True:
            self._gpu_event.wait()
            self._gpu_event.clear()

            while self._gpu_queue:
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

                self._gpu_queue.popleft()

                # Brief pause between GPU jobs for VRAM cleanup
                import gc
                gc.collect()
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except ImportError:
                    pass
                time.sleep(1)

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
