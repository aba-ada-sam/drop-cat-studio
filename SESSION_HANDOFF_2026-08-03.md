# Session Handoff -- 2026-08-03

Executive brief for a new Claude instance picking up work on DropCat Studio (DCS).
Covers: what this project is, how this session left it, what's fixed vs. still
open, and how Andrew likes to work.

## What DropCat Studio is

A personal, local-first AI video-production app. FastAPI backend (`app.py`) +
vanilla JS SPA frontend (`static/js/`, one file per rail tab), served at
`http://127.0.0.1:7860` on Andrew's own machine (RTX 5080). Single-user, no
auth beyond a PIN gate on the separate mobile Admin Review tool. Private repo:
`github.com/aba-ada-sam/drop-cat-studio`.

Pipeline: WanGP does video generation, ACE-Step does music/audio, an LLM
router (`core/llm_router.py`) picks between Anthropic/OpenAI (sanitized for
NSFW content via `core/nsfw_sanitizer.py`) and uncensored backends
(Featherless cloud / local KoboldCpp) depending on config and content. Only
one of WanGP/ACE-Step can hold the GPU at a time -- `core/gpu_orchestrator.py`
enforces that. `core/job_manager.py` is the job queue: GPU-bound job types
are serialized through one queue; everything else (video_tools, upscale,
retime, lipsync) runs on its own thread immediately.

Rail tabs: Chat, Image Studio, Studio Home (`pipeline`), Quick Video
(`express`), Create Videos (`fun-videos`), Music Video, Video Bridges,
Video Tools, Queue. Gallery is a modal overlay, not a rail tab. Settings is
also a modal (gear icon).

## This session's arc (very long, single continuous session)

Started from a screenshot of a triple-toast bug in the AI Manager. Escalated
through several explicit broadenings from Andrew ("make the program better",
"keep going", "fix all the things") into a full audit-and-fix pass across
nearly the entire app. Roughly in order:

1. Fixed the Manager's click-retry toast storm (root cause).
2. Full GUI/UX pass across all 9 tabs -- real bugs, not just cosmetics (see
   `git log` for commit `e47a8a1` and everything after for the exhaustive
   list; too long to repeat here).
3. Built `tools/dcs_status.py` because Andrew called out that I was "blind"
   to whether the app was actually running / needed a restart -- it had been
   giving fragmented, sometimes-wrong answers from separate netstat/curl/git
   checks. This script is now the one authoritative source: process/port
   liveness, PID, git commit drift vs. what the running process booted from,
   full list of uncommitted files (and whether each needs a restart or just
   a page reload), and job-safety-for-restart. **Run this before ever
   claiming anything about whether DCS is running or needs a restart.**
4. Investigated (but did not solve) a clip-vanishing bug in song_video during
   WanGP deadlock recovery -- hardened the merge step to fail loudly instead
   of silently degrading, but the actual disappearance mechanism was never
   found. Matches the prior session's own "genuinely unresolved" conclusion.
5. Gallery: fixed cold-storage thumbnail/playback fallback (files move to
   `D:\ColdArchives\DropCat-Studio-Output\` on a schedule, external robocopy
   task, see `logs/output_archive_move.log`), fixed the tab filter dropdown
   (was missing 8 of 10 real categories), fixed a rapid-filter-switch race,
   and made the quick-delete button an actual hard delete (it only deleted
   the DB row before -- Andrew's call: "delete file should delete the file").
6. Ran a 6-agent parallel research pass across everything not yet reviewed:
   Settings, onboarding, admin_review, image2video, retime/lipsync tools,
   video_bridges, AI Manager backend, core job/session/GPU orchestration,
   core media/config utilities. This surfaced ~25 real findings.
7. Fixed the top ~6, then (on "fix all the things") fixed 15 more. See
   commits `8888f7d` and `7220e42` for full descriptions of each -- the
   commit messages are deliberately detailed and are the best record of
   exactly what changed and why.

## Verification discipline (how everything above was actually checked)

Every fix in this session was verified before being called done, using
whichever method fit:
- **Frontend/JS changes**: headless Playwright against the live running
  instance -- load the page, drive the actual UI, check for console/page
  errors, screenshot when visual state mattered.
- **Backend/Python changes that are safe to exercise directly**: real
  function calls against real files (e.g. `probe_file()` against an actual
  video, `nsfw_sanitizer` round-tripped through all 79 pairs, `upscale_video`
  actually run and actually killed mid-render with `tasklist` confirming the
  OS process was gone).
- **Safety-critical changes** (the minor-safety sanitizer fix): mocked the
  Anthropic/OpenAI client boundary and inspected the literal payload that
  would go over the wire, rather than trusting the code by inspection alone.
- **Concurrency/race fixes**: isolated unit tests reproducing the exact old
  buggy sequence first (proving the bug is real), then proving the fix
  closes it (e.g. `job_manager.promote()`'s queue-resurrection bug).

Never claimed a fix worked without one of the above. Where live-testing
wasn't practical or safe, said so explicitly rather than asserting success.

## Explicitly NOT fixed -- flagged, not silently dropped

- **services/manager.py's WanGP deadlock-watchdog kill/lock race**: the
  `_kill_by_port` call runs outside the `_wangp_start_lock` mutex meant to
  serialize worker start/kill, so an overlapping recovery cycle could
  theoretically kill a legitimate freshly-started worker. Declined to fix --
  a wrong change here risks turning "recovers automatically" into "hangs
  forever," which is worse than the current bug. Needs real design thought,
  not a quick patch.
- **core/session.py's session-attribution race**: job output gets
  registered into whatever session is *current at completion*, not
  submission. Confirmed unreachable today -- no shipped UI calls
  `/api/session/new` or `/api/session/switch/{id}`. Worth remembering if a
  session-switcher UI ever gets built.
- **job_manager's timed-out-zombie-thread edge case**: a job that times out
  can leave a zombie thread that, if it later completes, can clobber the
  "timed out" status with stale data. Lower priority, needs an actual hang
  to trigger.
- **The song_video clip-vanishing bug** (see item 4 above) -- root cause
  genuinely not found across two sessions now.
- **D:\ output-folder junction migration**: `C:\DropCat-Studio\output` was
  supposed to become an NTFS junction pointing at `D:\ColdArchives\...` to
  stop needing the scheduled robocopy move. Blocked on one stuck leftover
  folder (`output\2026-05-01`) that resists deletion by every method tried
  (direct delete, .NET delete, cmd rmdir, handle scan, ACL reset, Search
  Indexer restart). Smells like a stale NTFS handle that needs a reboot to
  clear -- not a code problem.
- **`static/js/tab-adobe.js`**: a real, substantial Adobe Premiere/After
  Effects agent feature exists in the codebase but isn't wired into the
  rail (`static/js/app.js`'s `TAB_INIT` map has no entry for it). Not
  deleted, not re-wired -- genuinely unclear whether this is paused
  on purpose or an oversight. Ask Andrew before touching it either way.
- Several lower-severity/cosmetic findings from the research pass not
  judged worth the risk this round (listed in commit `7220e42`'s message).

## How to work with Andrew

- **Verify before claiming.** He has been burned before by claims of "fixed"
  that weren't checked. Never say something works without having actually
  observed it (screenshot, test output, live check) -- describe what you
  changed and let evidence carry the claim, not confidence.
- **Commit and push as work lands**, not batched at the end. Restart the
  app (`POST /api/jobs/save-and-restart`, safe when `dcs_status.py` shows no
  running/queued jobs) to actually pick up backend `.py` changes -- static
  `.js`/`.css` take effect on next page load, no restart needed.
- **Run `tools/dcs_status.py` before any claim about whether DCS is running
  or needs a restart.** Don't reconstruct that picture from separate
  netstat/curl/git calls -- that's exactly what caused confusion before.
- **Flag ambiguous or risky decisions instead of deciding unilaterally.**
  Recent examples: whether "delete" should hard-delete a file (asked,
  Andrew answered "always hard delete, no garbage"), whether to fix the
  WanGP kill-lock race (declined, explained the asymmetric risk), what to
  do about `tab-adobe.js` (left alone, flagged). When in doubt, describe the
  tradeoff in one paragraph and ask, rather than picking a lane silently.
- **He gives broad, escalating mandates** ("make it better", "fix all the
  things") and expects genuine initiative within them, not a request for
  permission on every step -- but a *destructive* or *safety-relevant*
  action still gets flagged first even under a broad mandate. A gallery
  filter change that would have hidden 99.76% of his content was caught and
  reverted before shipping in an earlier part of this session, precisely
  because the pre-ship sanity check is non-negotiable regardless of how
  broad the mandate is.
- **Use TodoWrite for real multi-step work.** This session ran two large
  todo lists (one per research-pass round) and it kept a ~20-item fix queue
  honest across a very long single conversation.
- **ClaudeTeam board**: DCS is Andrew's personal stack, so use the *Studio*
  silo, not the default (Lynn Cove/work) board:
  `CLAUDETEAM_WORLD=studio python "C:\Users\andre\ClaudeTeam\team.py" post <role> "..." --sid <tag>`.
  Getting the env var syntax wrong silently falls back to the wrong board
  (happened once this session -- caught and corrected with a follow-up post,
  not a silent miss).
- **Andrew runs one Claude session per machine now** (this box is "5080").
  Parallelism comes from subagents (Agent tool / Explore), not multiple
  windows. For genuinely large fan-out work, dispatch multiple focused
  research agents in one message so they run concurrently, then read and
  act on the actual findings yourself -- don't let a subagent spawn its own
  further subagents you have no visibility into (this happened once this
  session via a poorly-scoped delegation and had to be redone directly).
- **ASCII-only enforcement is mechanical** on this machine
  (`ascii_guard.py` PreToolUse hook on Write/Edit) -- curly quotes, em
  dashes, ellipsis characters in *any* file (even ones you didn't touch,
  if your edit's `new_string` happens to include an unchanged line
  containing one) will get rejected. Use `--` for em dashes, straight
  quotes, `...` for ellipsis.
- **This machine is under real load** when many background research agents
  are running -- normally-instant operations can take 1-2 seconds instead
  of milliseconds. Don't mistake a slow response for a bug; use generous
  waits in live tests and re-check with more patience before concluding
  something is broken (happened twice this session with Playwright tests
  that looked like real bugs and were actually just timing).

## Where things physically live

- Repo: `C:\DropCat-Studio`, remote `github.com/aba-ada-sam/drop-cat-studio`.
- `tools/dcs_status.py` -- the one-shot status tool, use it constantly.
- `logs/output_archive_move.log` -- proof of the cold-storage archival
  schedule (files move to `D:\ColdArchives\DropCat-Studio-Output\`).
- Prior handoff: `SESSION_HANDOFF_2026-08-02_evening.md` (the session
  before this one -- covers Dev13B motion fix, lip-sync silent-gap fix,
  the outro sting system, and the still-open clip-vanishing bug in more
  detail than repeated here).
- `gallery.db` -- SQLite, ~2093 rows as of this session, 10 distinct `tab`
  values (4 of them legacy/no-longer-written: `song-video`, `fun-videos`,
  `zoom`, `image-gen`).

## Immediate state as of this handoff

Running clean, pid current per `dcs_status.py`, HEAD at `7220e42`, pushed to
GitHub. Only uncommitted item on disk is `features/outro/yarn_burst.py`
(untracked, deliberately -- it's a previous session's rejected "yarn
bursting from the mouth" effect; Andrew killed it explicitly, do not resume
it). No jobs running. Nothing blocking a restart.
