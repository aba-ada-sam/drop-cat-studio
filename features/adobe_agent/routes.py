"""Adobe Agent feature routes -- /api/adobe/*"""
import asyncio
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from features.adobe_agent.client import status_both, run_op, PREMIERE_PORT, AE_PORT
from features.adobe_agent.planner import plan_goal

log = logging.getLogger(__name__)
router = APIRouter()

_PORT_FOR = {
    "premiere": PREMIERE_PORT,
    "pr":       PREMIERE_PORT,
    "ppro":     PREMIERE_PORT,
    "aftereffects": AE_PORT,
    "ae":           AE_PORT,
    "aeft":         AE_PORT,
}


# GET /api/adobe/status -- which panels are online
@router.get("/status")
async def adobe_status():
    result = await asyncio.to_thread(status_both)
    return result


# POST /api/adobe/plan -- turn a text goal into a task list
@router.post("/plan")
async def adobe_plan(request: Request):
    body = await request.json()
    goal = (body.get("goal") or "").strip()
    if not goal:
        return JSONResponse({"error": "goal is required"}, 400)

    def _plan():
        from app import get_llm_router
        llm = get_llm_router()
        return plan_goal(goal, llm)

    try:
        tasks = await asyncio.to_thread(_plan)
        return {"ok": True, "tasks": tasks}
    except Exception as exc:
        log.exception("[adobe] plan failed: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, 500)


# POST /api/adobe/run -- execute a task list, streaming progress via job
@router.post("/run")
async def adobe_run(request: Request):
    body  = await request.json()
    tasks = body.get("tasks") or []
    if not tasks:
        return JSONResponse({"error": "tasks list is required"}, 400)

    def _get_jm():
        from app import _g
        return _g.get("job_manager")

    jm = _get_jm()
    if jm is None:
        return JSONResponse({"error": "job manager not ready"}, 503)

    from core.job_manager import JOB_VIDEO_TOOL  # non-GPU job type

    def worker(job, task_list):
        total   = len(task_list)
        results = []
        job.update(progress=0, message=f"Starting {total} steps...")

        for i, task in enumerate(task_list, 1):
            if job.stop_event.is_set():
                break

            app_key  = (task.get("app") or "").lower()
            op       = task.get("op") or ""
            args     = task.get("args") or {}
            label    = task.get("label") or f"{app_key}.{op}"
            port     = _PORT_FOR.get(app_key)

            pct = int((i - 1) / total * 100)
            job.update(progress=pct, message=f"Step {i}/{total}: {label}")

            if port is None:
                results.append({"step": i, "label": label, "status": "skipped",
                                 "error": f"Unknown app: {app_key}"})
                continue

            try:
                data = run_op(port, op, args)
                results.append({"step": i, "label": label, "status": "ok", "data": data})
            except Exception as exc:
                results.append({"step": i, "label": label, "status": "error",
                                 "error": str(exc)})
                log.error("[adobe] step %d failed: %s -- %s", i, label, exc)
                if task.get("abort_on_error", True):
                    job.update(progress=pct, message=f"Aborted at step {i}: {exc}")
                    job.meta["results"] = results
                    job.update(status="error", error=str(exc))
                    return

        job.meta["results"] = results
        ok_count   = sum(1 for r in results if r["status"] == "ok")
        fail_count = sum(1 for r in results if r["status"] == "error")
        job.update(progress=100,
                   message=f"Done: {ok_count} succeeded, {fail_count} failed",
                   output="completed")

    job = jm.submit(JOB_VIDEO_TOOL, worker, tasks, label="Adobe tasks")
    return {"ok": True, "job_id": job.id}


# POST /api/adobe/run-step -- execute a single operation directly (for testing)
@router.post("/run-step")
async def adobe_run_step(request: Request):
    body    = await request.json()
    app_key = (body.get("app") or "").lower()
    op      = body.get("op") or ""
    args    = body.get("args") or {}
    port    = _PORT_FOR.get(app_key)

    if not port:
        return JSONResponse({"ok": False, "error": f"Unknown app: {app_key}"}, 400)
    if not op:
        return JSONResponse({"ok": False, "error": "op is required"}, 400)

    try:
        data = await asyncio.to_thread(run_op, port, op, args)
        return {"ok": True, "data": data}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 500)
