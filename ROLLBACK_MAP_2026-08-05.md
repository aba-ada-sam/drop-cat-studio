# DropCat-Studio song_video -- Calibrated Rollback Map

Generated 2026-08-05. Read-only research: no edits, commits, reverts, GPU, or app-boot
were performed to produce this file. Nothing below has been executed.

Scope: `C:\DropCat-Studio` branch master, git history 2026-07-29 through present,
filtered to `features/song_video/`, `services/`, `core/wangp_models.py` (35 commits).
Cross-referenced against the full history of `C:\DCMVS-restored` (7 commits, the
reference implementation DCS has been porting from).

Methodology: `git log --since=2026-07-29` on the three paths above for the commit list;
`git log -S`/`-G` (pickaxe) across full history, unrestricted by path or date, for the
four provenance questions; `git show`/`git show --stat` for hunk-level detail; direct
`Read`/`Grep` of the current tree for what actually runs at HEAD.

-------------------------------------------------------------------------------

## Verdict

A calibrated rollback here is not a `git revert` of recent history. All four failure
mechanisms named in the brief -- the loop-fill merge, the beat-planned start-time drift,
the arc-duration override, and MuseTalk running after the native merge -- predate the
2026-07-29 window by two to twelve weeks (details below). There is no commit dated
"last week" to revert them to; reverting anything in-window leaves all four exactly as
they are today, because none of the in-window commits touched their root code.

Of the 35 in-window commits touching `features/song_video/`, `services/`, and
`core/wangp_models.py`: 20 are KEEP-SAFETY (hard-fails, screens, the sync-floor disarm,
tests, RECIPE.json, watchdog/reliability fixes -- exactly the class the brief says to
keep), 9 are KEEP-PROVEN (resolution/guidance/anchor/chaining/sliding-window fixes that
collectively ARE the render recipe last night's blind labels validated), 5 are NEUTRAL
(self-corrected same night, superseded by a later commit, or Andrew's own explicit
product call), and only ONE hunk in the entire window is a genuine REVERT-CANDIDATE:
the SYNC-OR-DIE hard floor added in `74a579c` (2026-08-04 21:07) -- and it was already
disarmed six commits later, same night, by `bac5ef5` (21:00 -> 22:36, under an hour).
As of HEAD there is nothing live left to revert; `SYNC_ENFORCE` just needs to stay unset.

So "calibrated rollback" in this codebase does not mean going backward. The two
structural bugs that are actually live right now -- the start-time/trim drift and the
arc-duration override, both from 2026-05-24/05-25 -- need a forward fix, and the parts
that fix needs already exist in the tree, written and tested this week but not yet wired
in: `features/song_video/sing_grid.py`'s `assembled_start_times()`/`timeline_drift()`
(added `555236f`, 2026-08-04) proves the drift arithmetically and drives it to 0.0ms in
`tests/test_sing_grid.py` group C, but `features/song_video/pipeline.py`'s own
`_clip_start_times` computation (`run_song_prep`, current lines ~538-549) has not been
updated to use it. Closing that gap, plus reconciling the two independent reads of the
LLM story-arc's per-clip `"duration"` field (`pipeline.py` ~1322 and ~1506-1508) against
`clip_durations[i]` from the beat plan / `tiers.py`, is the concrete, file-level fix --
see the action list at the end.

One more finding that bears directly on the rollback story: `features/song_video/tiers.py`
(the module defining the 30s admin/user product shape, added `74a579c`/`89e8ac3`) is
fully tested but has **zero callers** in this repo outside its own test
(`tests/test_tiers.py`) -- `routes.py` and `pipeline.py` do not import it. The live HTTP
route's own defaults (`routes.py:294,300`) are `lip_sync=False`, `auto_lipsync=True`,
independent of any tier. If last night's blind-label session ran through DCMVS's
`chain.py` CLI or a direct pipeline call rather than DCS's `/api/song-video/generate`
route (plausible given the ledger's "confirmed on a clean cloud env" / "pod seed gate
pass" language), then the state that produced Andrew's approved takes and the state
DCS's own UI currently ships may not be the same state. This needs a direct answer
before any further tuning: what code path actually produced last night's approved take.

-------------------------------------------------------------------------------

## Four provenance answers

### (a) Merge-to-full-song loop-fill ("looping as a last resort")

- General "loop video to fill the full song" concept: commit `a4db7b8`, **2026-05-04**,
  "feat(song-video): loop clips to fill full song -- remove complexity"
  (`features/song_video/pipeline.py`). This is the origin of the design -- OLDER than
  the window by about 12 weeks.
- The exact log line and the "last resort" demotion (three-way trim/freeze/loop choice,
  loop only when `gap > max(2.0, video_dur * 0.5)`): commit `f9fef20`,
  **2026-07-27 20:54:51 -0400** (`features/song_video/pipeline.py`). This is TWO
  calendar days before the 07-29 window opens, and it is a fix that SHRINKS how often
  looping fires -- before this commit, any shortfall always looped (`-stream_loop -1`,
  restarting from clip 1 and doubling an un-synced mouth over the song's back half);
  after it, loop is the fallback of last resort behind trim (video over-covers) and
  freeze (small gap, holds the last frame).
- No commit inside 2026-07-29..present touches this logic at all. Verified by scanning
  every in-window diff hunk touching `pipeline.py`'s merge/loop/fill code: the only
  in-window commit near the merge function is `9a65e5a` (2026-08-03), and its change is
  an unrelated pre-merge "are all clip files still on disk" guard, not the fill logic.
- **SAY SO**: both the mechanism and its most recent refinement predate the window. A
  rollback to any 07-29-or-later commit changes nothing here, because nothing in that
  range touched it.

### (b) Beat-planned non-contiguous clip start times ("clips misplaced against the song")

- Root mechanism: `_clip_start_times` is a running cumulative sum of the beat-planned
  `clip_durations`, corrected only for the 0.12s cross-fade
  (`corrected = max(0.0, _start_t - _idx * _SONG_XFADE_DUR)`, current `pipeline.py`
  lines 538-549). This traces to commit `3fc77e8`, **2026-05-05**,
  "feat(song-video): LTX-2 audio-conditioned video generation", reaffirmed through
  `5fa5293` (2026-05-25). OLD.
- The separate per-clip boundary trim actually removed from every rendered clip before
  assembly -- 0.08s head + 0.20s tail = 0.28s -- was added in commit `8fad25d`,
  **2026-05-25 17:09:04 -0400**, "Fix clip seams: head-trim 0.25s from clips 1+..."
  (`features/song_video/pipeline.py`, current lines ~1656-1695). Also OLD, and it was
  never folded into the start-time formula above.
- Net effect, confirmed still true at HEAD: a structural ~0.28s-per-clip-index drift
  between where the pipeline believes a clip lands (used to cut its conditioning audio
  slice) and where it actually lands after trim + crossfade. This is precisely
  `LIPSYNC_LEDGER.md`'s 2026-08-04 entry "THE DRIFT HAS TWO INDEPENDENT SOURCES IN DCS"
  (source 1), and `features/song_video/sing_grid.py` (added `555236f`, 2026-08-04)
  reproduces it arithmetically in `tests/test_sing_grid.py` group C: 3.08s of cumulative
  error by clip 12.
- This week's Tier-2 port (`555236f`, `87008b5`) diagnosed this precisely and wrote the
  reconciliation math, but as of HEAD that math has NOT been wired back into
  `pipeline.py`'s own `_clip_start_times` computation -- the bug is newly and precisely
  DOCUMENTED, not yet fixed.
- **SAY SO**: the root cause is about 10 weeks old (2026-05-25), far older than "a week
  ago." Nothing in the 07-29-to-now window introduced it. The window's only contribution
  is diagnosing it exactly and building (but not yet shipping) the fix.

### (c) Arc per-clip durations overriding requested clip_duration

- Introduced verbatim in commit `8ba2df2`, **2026-05-24 19:15:22 -0400**, "Fix song
  pipeline: handle dict arc entries from snap_durations_to_beats"
  (`features/song_video/pipeline.py`):
  `_arc_dur = _arc_entry.get("duration") if isinstance(_arc_entry, dict) else None`
  `this_dur = float(_arc_dur) if _arc_dur else (clip_durations[i] if i < len(clip_durations) else clip_dur)`
  `this_dur = max(4.0, min(12.0, this_dur))`
  This exact logic (now at `pipeline.py` lines ~1506-1508) still governs the actual
  render length today, effectively unchanged.
- `git log -S"_arc_dur"` across all history returns exactly this one commit -- the
  string has never been touched since it was introduced.
- One in-window commit, `64bccfd` (2026-08-03), partially mitigated the FALLOUT: it
  clamped the pre-cut audio slice duration (`_sdur`, used to extract the conditioning
  clip) to the same `[4, 12]` range as `this_dur`, closing a worse case where the two
  could differ by several seconds outright. It did not remove the override -- the arc's
  `"duration"` still wins over `clip_durations[i]` (the beat plan / tier's assigned
  length) whenever the arc supplies one.
- **SAY SO**: this is a ~10-week-old design decision, not a new-this-week regression. It
  predates the window by ten weeks and predates "a week ago" by roughly nine.

### (d) MuseTalk auto_lipsync post-pass running after the native path's merge

- The wiring itself (MuseTalk called after `merged = _merge_video_audio_trim(...)`
  returns): commit `8ba4d22`, **2026-05-29 14:48:42 -0400**, "Wire MuseTalk lip-sync
  post-pass into the song-video pipeline". OLD.
- Explicit opt-in gating at the pipeline layer (`settings.get("auto_lipsync", False)`):
  commit `39f6993`, same day, **2026-05-29 17:07:15 -0400**. OLD.
- The exact log line `"[lipsync] driving on N sung phrases"` traces to commit
  `b7229e5`, **2026-07-09 01:40:25 -0400**, "Music video: sing to the words, not the
  background music" (`features/lipsync/runner.py`, new `features/lipsync/vocal_activity.py`).
  OLD -- three weeks before the window.
- Confirmed structurally unchanged at HEAD: `features/song_video/pipeline.py` still
  calls the MuseTalk post-pass only after the merge succeeds (current lines ~1837-1874,
  comment header "Lip sync post-pass (MuseTalk)"). The three in-window commits that
  touch `auto_lipsync` (`7829855`, `530e962`, `c134c63`; all 2026-08-03) only change
  FACE-FRAMING behavior on that path, not its position relative to the merge.
- **Correction to a natural assumption, found while verifying this**: `auto_lipsync` is
  NOT merely a dormant opt-in. At the route layer, `features/song_video/routes.py:300`
  defaults it to `True` ("ON by default") with an explicit comment: *"Runs AFTER native
  conditioning -- the two stack (Andrew: 'Native + MuseTalk both')."* That default and
  comment trace to commit `abb7583`, **2026-06-19 14:57:29 -0400** -- also OLD, and also
  older than the 2026-08-02 commit (`2e1a063`) that turned native `lip_sync` off by
  default for deadlock reasons; `2e1a063` explicitly left `auto_lipsync`'s default alone.
  So for any job that does not explicitly pass `auto_lipsync=false`, the stack is live
  and by design, not a leftover.
- **SAY SO**: both the ordering and the stacking are old (10 and 7 weeks respectively)
  and the stacking is a deliberate, standing instruction from Andrew, not an accident or
  a regression. If the stack itself (native conditioning immediately followed by a
  MuseTalk re-inpaint of the mouth region) is now suspected of causing visible defects,
  that is a product question to put back to Andrew, not a bug to silently revert --
  especially since `c134c63` (2026-08-03) already documents MuseTalk as "the wrong sync
  engine for creature subjects regardless (fundamental paste-box)," a finding that
  postdates the original June stacking directive by seven weeks and may be reason enough
  to revisit it deliberately.

-------------------------------------------------------------------------------

## Annotated commit table (DCS, 2026-07-29 -> present)

Tags: KEEP-SAFETY = screens/energy/disarm/tests/ledger/docs (per the brief, keep
outright). KEEP-PROVEN = render-recipe behavior that last night's blind labels vindicate
or that is load-bearing infrastructure for that recipe. REVERT-CANDIDATE =
selection/assembly/merge/product behavior postdating Andrew's last-liked output and not
yet validated. NEUTRAL = everything else (UI/unrelated fixes, self-corrected same night,
superseded by a later commit, or Andrew's own explicit directive).

| Date (local) | Hash | What it changed | Tag | Files |
|---|---|---|---|---|
| 07-29 21:45 | 2a581bc | No silent CPU fallback for GPU-bound work (ACE-Step/MuseTalk/Real-ESRGAN error instead of silently grinding on CPU) | KEEP-SAFETY | services/manager.py, services/acestep_patches/ |
| 07-29 22:54 | 1920968 | Fix mouth-box artifact: tw/th side-channel bug meant a false 360p->580p upscale fired on every lip-sync clip; generate natively at 960x544 | KEEP-PROVEN | features/song_video/pipeline.py |
| 07-29 23:07 | e49696b | Gate audio_scale/input_video_strength on the clip's actual audio slice, not the job-wide flag (prevents "loose" motion with no real conditioning) | KEEP-PROVEN | features/song_video/pipeline.py |
| 07-30 00:00 | 87883d5 | Use LTX-2 native SE (start+end keyframe) mode on every lip-sync clip, including clip 0 -- DCMVS's proven anchor recipe | KEEP-PROVEN | features/song_video/pipeline.py |
| 07-30 20:51 | 92619aa | Fix token-mismatch check only firing under busy=True (masked a dead worker as "job finished with nothing"); fix retry-succeeded clip still being discarded | KEEP-SAFETY | features/song_video/pipeline.py |
| 08-02 11:18 | 8041845 | Watchdog: run the stuck-at-step-0 check even when this process's own WanGP handle is stale (was silently skipping detection after a crash/relaunch) | KEEP-SAFETY | services/manager.py |
| 08-02 11:19 | 83881b3 | Block 960x544 on lip-sync (thought to hang); re-enable periodic chain reanchor | NEUTRAL -- both hunks superseded in-window: 960x544 block reversed by 530e962 then made the DEFAULT by 5d835ab; reanchor-every-2 is moot once 5d835ab removes chaining from the lip-sync path entirely | features/song_video/pipeline.py |
| 08-02 11:20 | 2e1a063 | Flip native `lip_sync` default to False at the route (deterministic WanGP step-0 deadlock); leaves `auto_lipsync` default (True) untouched | KEEP-SAFETY | features/song_video/routes.py |
| 08-02 12:17 | 029a4e7 | Propagate the lip_sync=False default to the remaining call sites (batch, Create Videos, Express) | KEEP-SAFETY | features/song_video/routes.py |
| 08-03 08:47 | e47a8a1 | GUI/UX audit pass; in-scope hunk is a batch_runner reconnect-after-restart KeyError fix | NEUTRAL | features/song_video/batch_runner.py |
| 08-03 08:59 | 9a65e5a | Raise a clear error instead of silently substituting clip_paths[0], or masking a missing clip file, when merge inputs are bad | KEEP-SAFETY | features/song_video/pipeline.py |
| 08-03 12:03 | 64bccfd | Clamp the pre-cut lip-sync audio slice duration to the same [4,12] range as the render length (mitigates fallout of finding (c), does not remove the override) | KEEP-SAFETY | features/song_video/pipeline.py, batch_runner.py |
| 08-03 19:54 | 87917c7 | Raise librosa analysis cap 300s -> 900s so beat data isn't lost past 5 minutes | NEUTRAL -- moot at current 30s tier length | features/song_video/audio_analyzer.py |
| 08-03 21:09 | 7829855 | Run face-framing for auto_lipsync too, not just native lip_sync | NEUTRAL -- same-night side effect (silently re-framed the DEFAULT path into face close-ups) corrected a few commits later by c134c63 | features/song_video/pipeline.py |
| 08-03 21:37 | 530e962 | Honor explicit resolution override on lip_sync jobs (the hang justifying the 960x544 block was a driver/TDR issue, verified fixed on two real job IDs) | KEEP-PROVEN | features/song_video/pipeline.py |
| 08-03 21:47 | 32c1ad7 | Clamp video guidance_scale to the model's registered value (3.0) on lip_sync jobs -- unclamped it reached 7.5 and fought source identity | KEEP-PROVEN | features/song_video/pipeline.py |
| 08-03 21:49 | 86abbee | mouth_sync_score: motion becomes a soft ranking factor below the 1.2 floor instead of hard-zeroing the candidate; verified against 6 real takes | KEEP-PROVEN, flagged -- this is part of the same mouth_sync_score machinery bac5ef5 later found taste-inverted at the FLOOR level; this specific sub-change was independently verified and later ported back into DCMVS (d94c55b), but see action item 4 | features/song_video/sync_qc.py |
| 08-03 21:54 | c134c63 | Restore default-path face-framing to native-lip_sync only, undoing 7829855's unintended default-path side effect exactly | NEUTRAL (self-correction) | features/song_video/pipeline.py |
| 08-03 22:25 | ee38f88 | Force sliding_window_size 481 + vae_config=1 (DCMVS parity) -- SAFE_DEFAULTS' 129 silently fragmented audio<->video cross-attention on every clip over 129 frames since 2026-04-27 | KEEP-PROVEN -- foundational to the now-validated render stack | core/wangp_models.py, services/wangp_worker.py |
| 08-04 00:54 | 5d835ab | Tier-1 DCMVS port: no chaining on lip_sync path (every clip from pristine source), 960x544 default | KEEP-PROVEN -- this is "anchor handling," the brief's own example of blind-label-vindicated behavior | features/song_video/pipeline.py |
| 08-04 19:49 | c1b5536 | Port artifact SCREENS (ribbon/red-strand detectors) into DCS proper | KEEP-SAFETY | features/song_video/artifact_screens.py (new) |
| 08-04 19:54 | 555236f | Port frame-grid math (sing_grid.py) + energy-vs-label window gate (window_energy.py); also the module that PROVES finding (b)'s drift arithmetically | KEEP-SAFETY | features/song_video/sing_grid.py, window_energy.py (new) |
| 08-04 20:18 | 87008b5 | Red-team fixes on the Tier-2 port: window-energy mean-vs-percentage bug, sing_grid budget/ceiling breaches, unguarded rmtree, xfade-constant mismatch inside the new module | KEEP-SAFETY | artifact_screens.py, sing_grid.py, window_energy.py |
| 08-04 20:31 | 2e3aa5b | Wire Tier-2 into the render path + hard-fail all six silent raw-mix-conditioning degradations | MIXED: hard-fails = KEEP-SAFETY (explicit KEEP item); "screens before ranking, not after" reorder = NEUTRAL, not independently blind-label-tested, not implicated in Andrew's specific sync-floor rejection | features/song_video/pipeline.py, services/wangp_worker.py |
| 08-04 20:33 | 6f16648 | Self-caught regression on 2e3aa5b: implicit-default jobs degrade to unconditioned render instead of crashing; explicit lip_sync requests still hard-fail | KEEP-SAFETY | features/song_video/pipeline.py |
| 08-04 20:47 | 5500721 | Red-team fixes on the wired path: infested takes held back as ranked last resort instead of triggering an unscreened restart-retry; window-energy check was a measured no-op, now wired with real intervals; worker refusal message now reaches the user | KEEP-SAFETY -- makes the screening apparatus function as designed, does not add a new taste heuristic | features/song_video/pipeline.py |
| 08-04 21:07 | 74a579c | SYNC-OR-DIE hard floor (require synced AND rank >= 0.12 on voiced windows, else raise) + 60s admin / 30s user tiers.py + best_of_n cap 5->10 | MIXED: sync-or-die hunk = REVERT-CANDIDATE, proven taste-inverted by Andrew's own blind labels the same night ("his one approved take, rank 0.001, is refused by the floor; all three that clear it he rejected") -- but already neutralized by bac5ef5 71 minutes later; tiers.py + best_of_n hunks = NEUTRAL/KEEP infrastructure, Andrew's own product-shape call, unrelated to the taste inversion | features/song_video/pipeline.py, routes.py, tiers.py (new) |
| 08-04 21:10 | e199399 | Surface truncation (clips_requested vs delivered) in job.meta/logs when sync-or-die shortens output, instead of an unremarkable "complete" message | KEEP-SAFETY | features/song_video/pipeline.py |
| 08-04 21:20 | a7a79f0 | Clamp resolution to what the GPU can finish (step-0 deadlock fix); never evict a worker reporting busy | KEEP-SAFETY | features/song_video/pipeline.py, services/manager.py |
| 08-04 21:35 | ce3b0d1 | Rule-6 pass on a7a79f0: fixes a real timeout-scoping bug in _worker_is_busy() (a trickling response could hold it open 16s+ against a 3s deadline); adds resolution-clamp visibility to job.meta | KEEP-SAFETY | features/song_video/pipeline.py, services/manager.py |
| 08-04 22:36 | bac5ef5 | Disarm the SYNC-OR-DIE hard floor behind SYNC_ENFORCE (advisory-only default) -- explicitly named KEEP in the brief | KEEP-SAFETY | features/song_video/pipeline.py |
| 08-04 22:55 | 89e8ac3 | Tiers: 30s for everyone (Andrew superseded his own 60s-admin spec from ~90 minutes earlier) | NEUTRAL -- Andrew's own explicit, same-night product call; not part of the failure cluster | features/song_video/tiers.py |
| 08-04 23:02 | 409b268 | RECIPE.json + loader, parity-tested | KEEP-SAFETY -- explicit KEEP item | features/song_video/recipe.py (new) |
| 08-04 23:22 | 32e6d6e | window_energy: energy-at-creation hard-fail (whole-stem + per-slice), abort loudly on silent guides | KEEP-SAFETY -- explicit KEEP item | features/song_video/window_energy.py |
| 08-04 23:22 | 3b8c23b | Wire the energy hard-fail into both creation points; clarify ribbon-holdback log wording | KEEP-SAFETY | features/song_video/pipeline.py |

Totals: 20 KEEP-SAFETY, 9 KEEP-PROVEN, 5 NEUTRAL, 1 REVERT-CANDIDATE-hunk (already
neutralized same night). No commit in this window is a live, un-mitigated
REVERT-CANDIDATE as of HEAD.

-------------------------------------------------------------------------------

## C:\DCMVS-restored -- full history, for context only (not tagged; this repo is the
## reference implementation DCS ports FROM, not something Andrew is rolling back)

| Date | Hash | What |
|---|---|---|
| 2026-05-31 17:57 | 679cb5a | Initial commit: DCMVS lip-sync pipeline at milestone v1.0 |
| 2026-05-31 18:01 | 119cf10 | README: manager->satellite architecture, the lip-sync fix (sync_qc + best-of-N) |
| 2026-06-03 20:31 | e209fc5 | Musical seam timing: cuts land on vocal rests/beats (+ catch up v2.0-v2.5) |
| 2026-06-04 19:58 | d8d81a0 | v2.6-v2.9: gate fix, action shots, single-take + clip Edit/Regenerate, retry quality |
| 2026-07-29 23:19 | d9c1456 | Point worker_url at satellite's current IP; force vae_config=1 on every worker launch |
| 2026-08-03 22:16 | d94c55b | sync_qc: motion as soft ranking factor in mouth_sync_score (port OF DCS 86abbee) |
| 2026-08-04 22:04 | 10c2a80 | chain: expose se_end_anchor and audio_cfg_scale on the CLI |

Two things worth noting: `d94c55b` shows DCS's `86abbee` (motion-as-soft-ranking-factor)
was verified enough to port back INTO the reference implementation, same direction of
travel as `ee38f88`/`5d835ab` porting DCMVS's sliding-window/anchor recipe INTO DCS --
by 2026-08-03/04 the two codebases are converging on one recipe, not diverging. And
`10c2a80` exposing `se_end_anchor`/`audio_cfg_scale` on DCMVS's CLI is consistent with
DCMVS's `chain.py` being the tool actually used for direct, scriptable render testing
(including, plausibly, last night's blind-label session) -- see the verdict's open
question about which code path produced the approved take.

-------------------------------------------------------------------------------

## Proposed action list, ordered by confidence

Nothing below has been executed. All are read-only-safe to review; several explicitly
require an Andrew decision before any code changes.

### High confidence -- safe, directly closes a named gap, no new risk

1. **Do nothing to bac5ef5.** Leave `SYNC_ENFORCE` unset (advisory-only). This is
   already the correct, disarmed state; there is no live regression left to revert.
2. **Wire `sing_grid.py`'s reconciled math into `pipeline.py`'s `_clip_start_times`.**
   File-level change (not a revert): in `run_song_prep()` (`features/song_video/pipeline.py`
   ~lines 538-549), replace the xfade-only correction with `sing_grid.assembled_start_times()`
   / an equivalent that also accounts for the 0.28s head+tail trim. Closes finding (b),
   source 1. The arithmetic and its test (`tests/test_sing_grid.py` group C, already
   green) already exist.
3. **Make one number own "how long is this clip."** Either stop `_arc_entry.get("duration")`
   from overriding `clip_durations[i]` in the `this_dur` computation
   (`features/song_video/pipeline.py` ~1506-1508), or explicitly reconcile the arc's
   value against the tier/beat-plan value at the top of `run_song_prep` before either
   number is used for start-time math, slice extraction, or render length. Closes
   finding (c) and finding (b) source 2 together, since they share one root cause.
4. **Answer the tiers.py wiring question directly** (see Verdict): confirm what code
   path actually produced last night's approved, blind-label-winning take -- DCS's
   `/api/song-video/generate` route, or a direct DCMVS `chain.py`/pipeline call. If it
   was the route, `tiers.py` needs a caller before its 30s/best_of_n shape is real in
   the product; if it wasn't, the route needs updating to match whatever WAS used before
   any further tuning is judged against it.

### Medium confidence -- worth doing, needs an Andrew decision first

5. **Ask Andrew whether the native+MuseTalk stack (finding d) should still run.** It is
   his own 2026-06-19 standing instruction ("Native + MuseTalk both"), not a bug, but
   `c134c63`'s later finding that MuseTalk is "the wrong sync engine for creature
   subjects regardless (fundamental paste-box)" postdates that instruction by seven
   weeks. A targeted blind-label ablation (native-only vs. stacked) would settle it
   cheaply before touching any code.
6. **Re-test `sync_qc.mouth_sync_score`'s ranking, not just the floor, before trusting
   it beyond advisory logging.** `bac5ef5` disarmed the FLOOR (0.12 cutoff); it did not
   establish whether the underlying rank/score correlates with Andrew's taste at all.
   If a wider ablation shows the score itself is inverted (not just the floor), the fix
   is a new or reweighted scorer -- explicitly NOT reverting `86abbee`, whose specific
   motion-softening change was independently verified on 6 real takes and has since been
   ported back into DCMVS (`d94c55b`).
7. **Revisit "screens before ranking, not after"** (`2e3aa5b`'s selection-order change)
   once (6) is answered -- it is plausible but not yet independently validated either
   way, and untangling it from the floor-inversion finding needs its own read.

### Low confidence / not recommended

8. **Do not `git revert` anything dated before 2026-07-27** to chase findings (a)-(d).
   Confirmed older than the window in every case; the tree is 30+ commits ahead of any
   of them within the filtered paths alone (300+ unfiltered), so a revert would conflict
   heavily and would strip out validated fixes it depends on (e.g. `ee38f88`'s
   sliding-window parity, which postdates and does not know about the pre-05-25 code).
9. **Do not touch `89e8ac3`** (tiers: 30s for everyone) chasing "product shape" --
   that failure cluster is about the arc-duration override (finding c), not the tier
   length Andrew explicitly set himself the same night.
