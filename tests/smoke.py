"""Smoke tests for Drop Cat Go Studio.

Runs end-to-end against an in-process TestClient. No external services are
exercised -- LLM calls, WanGP, Forge, ACE-Step are all skipped or mocked.

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
    print("\nDrop Cat Go Studio -- smoke tests\n" + "=" * 48)

    # -- Import smoke ------------------------------------------------------
    print("\n[imports]")

    def import_app():
        import app  # noqa: F401 -- exercises module-level side effects
    _test("import app", import_app)

    def import_features():
        from features.sd_prompts import routes as _a  # noqa: F401
        from features.fun_videos import routes as _b  # noqa: F401
        from features.video_bridges import routes as _c  # noqa: F401
        from features.image2video import routes as _d  # noqa: F401
        from features.video_tools import routes as _e  # noqa: F401
        from features.zoom import routes as _f  # noqa: F401
        from features.zoom import pipeline as _g  # noqa: F401
    _test("import feature routes", import_features)

    def import_core():
        from core import config, llm_router, wildcards, job_manager  # noqa: F401
        from core import nsfw_sanitizer  # noqa: F401
    _test("import core", import_core)

    # -- HTTP smoke (in-process via TestClient) ----------------------------
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

        # -- AI intent validation (no LLM call -- 400 paths) ----------------
        def ai_intent_empty_query():
            r = client.post("/api/ai-intent", json={"tab": "sd-prompts", "query": ""})
            assert r.status_code == 400, r.status_code
        _test("POST /api/ai-intent empty query -> 400", ai_intent_empty_query)

        def ai_intent_bogus_tab():
            r = client.post("/api/ai-intent", json={"tab": "bogus", "query": "x"})
            assert r.status_code == 400, r.status_code
        _test("POST /api/ai-intent bogus tab -> 400", ai_intent_bogus_tab)

        # -- Gallery round-trip --------------------------------------------
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

        # -- Presets round-trip --------------------------------------------
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

        # -- Prompt enhance validation -------------------------------------
        def enhance_empty_idea():
            r = client.post("/api/prompts/enhance", json={"idea": "", "provider": "local"})
            assert r.status_code == 400, r.status_code
        _test("POST /api/prompts/enhance empty idea -> 400", enhance_empty_idea)

        # -- Wildcards endpoint --------------------------------------------
        def wildcards_list():
            r = client.get("/api/prompts/wildcards")
            assert r.status_code == 200, r.status_code
            data = r.json()
            assert isinstance(data, dict), f"expected dict, got {type(data).__name__}"
            # Inline wildcards should always be discoverable (the ones in core/wildcards.py)
            payload_s = str(data).lower()
            assert "camera" in payload_s or "mood" in payload_s, "no inline wildcards surfaced"
        _test("GET /api/prompts/wildcards", wildcards_list)

        # -- GPU orchestrator status endpoint ------------------------------
        def gpu_status_shape():
            r = client.get("/api/gpu/status")
            assert r.status_code == 200, r.status_code
            data = r.json()
            assert isinstance(data, dict), f"expected dict, got {type(data).__name__}"
            assert "current" in data, "missing 'current' key"
            assert "history" in data, "missing 'history' key"
            assert isinstance(data["history"], list), "history must be list"
            assert data["current"] is None or data["current"] in ("wangp", "acestep", "forge", "ollama"), \
                f"unexpected current value: {data['current']!r}"
        _test("GET /api/gpu/status returns expected shape", gpu_status_shape)

        # -- Loop Folder endpoint contract ---------------------------------
        def list_folder_validation():
            # No path -> 400
            r = client.get("/api/fun/list-folder")
            assert r.status_code == 400, f"empty path expected 400, got {r.status_code}"
            # Bogus path -> 400
            r = client.get("/api/fun/list-folder?path=/no/such/folder/exists/here")
            assert r.status_code == 400, f"bad path expected 400, got {r.status_code}"
            # Path wrapped in double quotes (Windows 'Copy as path') -> 400
            # because the underlying folder doesn't exist, NOT because of the
            # quotes. The error message must mention the unquoted form so we
            # know _clean_user_path ran. Andrew 2026-05-12 hit this on a real
            # quoted path; regression-locking the strip behaviour.
            quoted = '"C:\\Users\\andre\\Desktop\\fake-no-such-folder"'
            r = client.get(f"/api/fun/list-folder?path={quoted}")
            assert r.status_code == 400, r.status_code
            detail = r.json().get("detail", "")
            assert '"' not in detail, f"quotes leaked into error detail: {detail!r}"
        _test("GET /api/fun/list-folder validates input", list_folder_validation)

        # -- Folder loop endpoints contract --------------------------------
        def folder_loop_status_shape():
            r = client.get("/api/fun/folder-loop/status")
            assert r.status_code == 200, r.status_code
            d = r.json()
            for k in ("active", "total", "index", "lap", "succeeded", "failed",
                     "status", "heartbeat_timeout_sec"):
                assert k in d, f"missing key {k!r} in status snapshot"
            assert isinstance(d["active"], bool)
            assert d["status"] in ("idle", "running", "stopping", "stopped",
                                   "done", "error")
        _test("GET /api/fun/folder-loop/status shape", folder_loop_status_shape)

        def folder_loop_start_validates():
            # No folder -> 400
            r = client.post("/api/fun/folder-loop/start", json={})
            assert r.status_code == 400, r.status_code
            # Bogus folder -> 400
            r = client.post("/api/fun/folder-loop/start",
                            json={"folder": "/no/such/path/xyz"})
            assert r.status_code == 400, r.status_code
        _test("POST /api/fun/folder-loop/start validates input", folder_loop_start_validates)

        # -- Zoom route validation -----------------------------------------
        print("\n[zoom]")

        def zoom_make_no_source():
            r = client.post("/api/zoom/make", json={})
            assert r.status_code == 400, f"expected 400, got {r.status_code}"
        _test("POST /api/zoom/make no source -> 400", zoom_make_no_source)

        def zoom_make_bad_direction():
            r = client.post("/api/zoom/make", json={
                "source_path": "/tmp/fake.jpg",
                "zoom_direction": "sideways",
            })
            assert r.status_code == 400, f"expected 400, got {r.status_code}"
        _test("POST /api/zoom/make bad direction -> 400", zoom_make_bad_direction)

        def zoom_extract_frame_no_path():
            r = client.post("/api/zoom/extract-frame", json={})
            assert r.status_code == 400, f"expected 400, got {r.status_code}"
        _test("POST /api/zoom/extract-frame no path -> 400", zoom_extract_frame_no_path)

        def zoom_extract_frame_missing_file():
            r = client.post("/api/zoom/extract-frame",
                            json={"video_path": "/no/such/video.mp4"})
            assert r.status_code == 400, f"expected 400, got {r.status_code}"
        _test("POST /api/zoom/extract-frame missing file -> 400", zoom_extract_frame_missing_file)

        def zoom_upload_image():
            import io
            from PIL import Image
            buf = io.BytesIO()
            Image.new("RGB", (64, 64), color=(100, 150, 200)).save(buf, format="PNG")
            buf.seek(0)
            r = client.post(
                "/api/fun/upload",
                files={"files": ("smoke_test.png", buf, "image/png")},
            )
            assert r.status_code == 200, f"upload failed: {r.status_code} {r.text[:200]}"
            data = r.json()
            assert "files" in data and len(data["files"]) == 1, f"unexpected shape: {data}"
            saved = data["files"][0]
            assert "path" in saved, f"no path in response: {saved}"
            assert saved["path"].endswith(".png"), f"unexpected extension: {saved['path']}"
            # Clean up
            try:
                Path(saved["path"]).unlink(missing_ok=True)
            except Exception:
                pass
        _test("POST /api/fun/upload image round-trip", zoom_upload_image)

        # -- VRAM + models endpoint ----------------------------------------
        print("\n[models + vram]")

        def models_endpoint_shape():
            r = client.get("/api/fun/models")
            assert r.status_code == 200, r.status_code
            data = r.json()
            assert "models" in data, "missing 'models' key"
            assert "default" in data, "missing 'default' key"
            assert "gpu_vram_gb" in data, "missing 'gpu_vram_gb' key -- VRAM not exposed"
            models = data["models"]
            assert len(models) >= 4, f"expected at least 4 models, got {len(models)}"
            for name, info in models.items():
                assert "vram_min_gb" in info, f"{name} missing vram_min_gb"
                assert isinstance(info["vram_min_gb"], (int, float)), f"{name} vram_min_gb not a number"
                assert info["vram_min_gb"] > 0, f"{name} vram_min_gb must be positive"
        _test("GET /api/fun/models has vram_min_gb + gpu_vram_gb", models_endpoint_shape)

        def wan_i2v_threshold_correct():
            r = client.get("/api/fun/models")
            assert r.status_code == 200
            models = r.json()["models"]
            wan480 = models.get("Wan2.1-I2V-14B-480P", {})
            wan720 = models.get("Wan2.1-I2V-14B-720P", {})
            assert wan480.get("vram_min_gb", 0) >= 20, \
                f"Wan I2V 480P vram_min_gb should be >=20 (deadlocks on 16GB), got {wan480.get('vram_min_gb')}"
            assert wan720.get("vram_min_gb", 0) >= 20, \
                f"Wan I2V 720P vram_min_gb should be >=20, got {wan720.get('vram_min_gb')}"
        _test("Wan I2V vram_min_gb >= 20 (confirmed deadlock on 15.9 GB)", wan_i2v_threshold_correct)

        def ltx_threshold_reasonable():
            r = client.get("/api/fun/models")
            assert r.status_code == 200
            models = r.json()["models"]
            ltx = models.get("LTX-2 Dev19B Distilled", {})
            assert 8 <= ltx.get("vram_min_gb", 0) <= 12, \
                f"LTX-2 Distilled vram_min_gb should be 8-12, got {ltx.get('vram_min_gb')}"
        _test("LTX-2 vram_min_gb reasonable (8-12 GB)", ltx_threshold_reasonable)

        def system_endpoint_has_vram():
            r = client.get("/api/system")
            assert r.status_code == 200, r.status_code
            data = r.json()
            assert "gpu_vram_gb" in data, "gpu_vram_gb missing from /api/system"
        _test("GET /api/system includes gpu_vram_gb", system_endpoint_has_vram)

        def auto_pick_always_ltx():
            from features.fun_videos.routes import _get_pick_to_model
            pick_map = _get_pick_to_model()
            for bucket, (model, motion) in pick_map.items():
                assert "LTX" in model, \
                    f"auto-pick bucket '{bucket}' -> '{model}' -- Express must always use LTX for speed"
        _test("auto-pick always resolves to LTX (no Wan I2V for Express)", auto_pick_always_ltx)

    # -- Summary -----------------------------------------------------------
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
