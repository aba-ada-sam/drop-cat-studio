"""Smoke tests for Drop Cat Go Studio.

Runs end-to-end against an in-process TestClient. No external services are
exercised — LLM calls, WanGP, Forge, ACE-Step are all skipped or mocked.

Usage:
    python tests/smoke.py          # prints each test + pass/fail, exits nonzero on any failure
    python -m pytest tests/smoke.py   # if pytest is installed

Philosophy: catch import errors, route-registration gaps, response-shape
regressions, and validation bugs. Anything that requires a live GPU or a
warm Ollama model is out of scope.
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

# Ensure the project root is on sys.path when run as a script.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Isolate DB files + config from the dev environment. Must be set BEFORE
# importing `app` so the module-level paths resolve to the temp dir.
_TMP = Path(tempfile.mkdtemp(prefix="dcs-smoke-"))
os.environ.setdefault("DROPCAT_TMP", str(_TMP))


def _setup_isolated_paths():
    """Redirect SQLite stores and config into a temp dir so tests don't
    touch Andrew's real gallery / presets / config.json."""
    import app as _app
    # Override the module-level DB paths so /api/gallery and /api/presets
    # operate on fresh empty databases.
    _app._PRESETS_DB = _TMP / "presets.db"
    # gallery uses a helper that builds the path from APP_DIR; override by
    # setting the env var the helper reads, if any. Here we just swap the
    # constant the endpoint refers to (see app.py:_gallery_db).
    if hasattr(_app, "_GALLERY_DB"):
        _app._GALLERY_DB = _TMP / "gallery.db"


_FAILED: list[tuple[str, str]] = []
_PASSED: list[str] = []


def _test(name: str, fn):
    try:
        fn()
        _PASSED.append(name)
        print(f"  PASS  {name}")
    except AssertionError as e:
        _FAILED.append((name, str(e) or "assertion failed"))
        print(f"  FAIL  {name}: {e}")
    except Exception as e:
        _FAILED.append((name, f"{type(e).__name__}: {e}"))
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)


def main() -> int:
    print("\nDrop Cat Go Studio — smoke tests\n" + "=" * 48)

    # ── Import smoke ──────────────────────────────────────────────────────
    print("\n[imports]")

    def import_app():
        import app  # noqa: F401 — exercises module-level side effects
    _test("import app", import_app)

    def import_features():
        from features.sd_prompts import routes as _a  # noqa: F401
        from features.fun_videos import routes as _b  # noqa: F401
        from features.video_bridges import routes as _c  # noqa: F401
        from features.image2video import routes as _d  # noqa: F401
        from features.video_tools import routes as _e  # noqa: F401
    _test("import feature routes", import_features)

    def import_core():
        from core import config, llm_router, wildcards, job_manager  # noqa: F401
        from core import nsfw_sanitizer  # noqa: F401
    _test("import core", import_core)

    # ── HTTP smoke (in-process via TestClient) ────────────────────────────
    print("\n[http]")

    _setup_isolated_paths()
    from fastapi.testclient import TestClient
    import app as _app

    with TestClient(_app.app) as client:
        def system_info():
            r = client.get("/api/system")
            assert r.status_code == 200, r.status_code
            data = r.json()
            assert "services" in data and "ollama" in data, f"missing keys: {list(data)}"
            assert "encoders" in data, "no encoders"
        _test("GET /api/system", system_info)

        def config_get():
            r = client.get("/api/config")
            assert r.status_code == 200, r.status_code
            assert isinstance(r.json(), dict), "config is not a dict"
        _test("GET /api/config", config_get)

        def static_serves():
            r = client.get("/static/js/app.js")
            assert r.status_code == 200, r.status_code
            assert "export" in r.text or "import" in r.text, "app.js looks empty"
        _test("GET /static/js/app.js", static_serves)

        # ── AI intent validation (no LLM call — 400 paths) ────────────────
        def ai_intent_empty_query():
            r = client.post("/api/ai-intent", json={"tab": "sd-prompts", "query": ""})
            assert r.status_code == 400, r.status_code
        _test("POST /api/ai-intent empty query -> 400", ai_intent_empty_query)

        def ai_intent_bogus_tab():
            r = client.post("/api/ai-intent", json={"tab": "bogus", "query": "x"})
            assert r.status_code == 400, r.status_code
        _test("POST /api/ai-intent bogus tab -> 400", ai_intent_bogus_tab)

        # ── Gallery round-trip ────────────────────────────────────────────
        def gallery_roundtrip():
            payload = {
                "tab": "sd-prompts",
                "url": "/output/unit-test.png",
                "prompt": "unit test prompt",
                "model": "test-model",
                "seed": 42,
                "metadata": {"settings": {"steps": 30}},
            }
            r = client.post("/api/gallery", json=payload)
            assert r.status_code == 200, r.status_code
            item_id = r.json()["id"]

            r = client.get("/api/gallery?tab=sd-prompts")
            assert r.status_code == 200, r.status_code
            items = r.json()["items"]
            assert any(i["id"] == item_id for i in items), "created item not in list"

            r = client.delete(f"/api/gallery/{item_id}")
            assert r.status_code == 200, r.status_code
        _test("gallery POST/GET/DELETE round-trip", gallery_roundtrip)

        # ── Presets round-trip ────────────────────────────────────────────
        def presets_roundtrip():
            payload = {
                "tab": "sd-prompts",
                "name": "smoke-test",
                "settings": {"steps": 40, "cfg": 7.5},
            }
            r = client.post("/api/presets", json=payload)
            assert r.status_code == 200, r.status_code
            preset_id = r.json()["id"]

            r = client.get("/api/presets?tab=sd-prompts")
            assert r.status_code == 200, r.status_code
            assert any(p["id"] == preset_id for p in r.json()["presets"])

            r = client.delete(f"/api/presets/{preset_id}")
            assert r.status_code == 200, r.status_code
        _test("presets POST/GET/DELETE round-trip", presets_roundtrip)

        # ── Prompt enhance validation ─────────────────────────────────────
        def enhance_empty_idea():
            r = client.post("/api/prompts/enhance", json={"idea": "", "provider": "local"})
            assert r.status_code == 400, r.status_code
        _test("POST /api/prompts/enhance empty idea -> 400", enhance_empty_idea)

        # ── Wildcards endpoint ────────────────────────────────────────────
        def wildcards_list():
            r = client.get("/api/prompts/wildcards")
            assert r.status_code == 200, r.status_code
            data = r.json()
            assert isinstance(data, dict), f"expected dict, got {type(data).__name__}"
            # Inline wildcards should always be discoverable (the ones in core/wildcards.py)
            payload_s = str(data).lower()
            assert "camera" in payload_s or "mood" in payload_s, "no inline wildcards surfaced"
        _test("GET /api/prompts/wildcards", wildcards_list)

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "=" * 48)
    print(f"  {len(_PASSED)} passed, {len(_FAILED)} failed")
    if _FAILED:
        print("\nFailures:")
        for name, reason in _FAILED:
            print(f"  - {name}: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
