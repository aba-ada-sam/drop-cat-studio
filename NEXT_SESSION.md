# Next Session Notes
Generated: 2026-05-20. Read this before touching anything.

---

## Hardware confirmed: RTX 5080 (15.9 GB VRAM)

| Model | Status | Notes |
|-------|--------|-------|
| LTX-2 Distilled (8 steps) | WORKS | ~30s/clip, scene-hold calm, atmospheric only |
| LTX-2 Dev13B (40 steps) | UNTESTED on this machine | vram_min_gb=10, should work |
| Wan I2V 14B | DEADLOCKS | WanGP budget 13GB < model 15.87GB -- hangs at Step 0 |
| ACE-Step music | WORKS | ~6s for 17s track, cold start ~22s |

---

## First things to test next session

### 1. LTX-2 Dev13B motion quality (CRITICAL -- never GPU-tested)
Auto-pick now routes action prompts to Dev13B. It has never actually run on this machine.

- Express tab, action photo (person, animal), type: "person sprinting down the road"
- Expected: ~3 min, real visible physical motion
- Log: `[auto-pick] '...' -> LTX-2 Dev13B (dynamic)`
- If Dev13B also fails: the hardware ceiling is Ken Burns-only; document and move on

### 2. Zoom tab end-to-end
Never GPU-tested. Smoke tests only.

- Zoom Out from portrait photo, 4 steps (model auto-selects LTX Dev13B on 15.9GB)
- Watch: camera pulls back, no jump cuts, output appears in gallery + From Session pickers
- Log: `[zoom] Arc planned via vision (4 clips)`

---

## Known open issues

**Wan I2V on RTX 5080 -- hardware limit, not a code bug.**
WanGP caps VRAM budget at 80% = 13GB. Wan I2V 14B needs 15.87GB.
Deadlocks at Step 0 every time. Requires 20GB+ card or a future smaller Wan I2V model.

**Zoom-in texture consistency.**
Each clip invents new detail independently -- clip 3 texture may differ from clip 2.
No fix without multi-frame conditioning. Document and accept for now.

---

## State of the codebase (2026-05-20)

- `gentle` motion style removed. Only `calm` (scene-hold, LTX atmospheric) and `dynamic` (story arc, LTX Dev13B or Wan) exist.
- Auto-pick: action -> LTX Dev13B + dynamic, calm/long_story -> LTX Distilled + calm.
- Wan I2V vram_min_gb = 20 (correctly warns as incompatible on 15.9GB card).
- Zoom tab: full new tab with correct CSS variables, VRAM-aware model selector.
- 26 smoke tests passing. All Python syntax clean.
- manager.pyw: 4s delegation guard prevents quit dialog on launch/Keep Running.
