"""Per-scene DOF (depth-of-field) finishing pass -- STATUS: NOT IMPLEMENTED.

RECIPE.json's "production.dof_finish" field and LIPSYNC_LEDGER.md's
2026-08-05 ~21:30 entry (engine repo commit 0cc1ade) describe the RATIFIED
method IN PROSE:

    "masked gblur sigma 3.2 outside the subject matte (dilate 31 + blur 24),
    every delivery. PER-SCENE since 2026-08-05 (one matte per scene anchor,
    DINO+SAM). METHOD CORRECTED same night: applied in ONE pass against a
    FRAME-EXACT MASK VIDEO (per-segment mask stills rendered to exact frame
    counts, concat, single maskedmerge; zero video re-timing). The earlier
    trim+concat segmented pass is BANNED for delivery -- its second-based
    trims rounded per cut and accumulated ~0.25s of A/V drift by the last
    clips (Andrew caught it at 50-55s on v13 AND v15)."

...but no script or literal ffmpeg command implementing that pass exists
anywhere searched:
    - engine repo (C:/DropCat-Studio-V2/engine, dcmvs-lipsync) git log --all
      --oneline: 11 commits (README/chain.py/sync_qc/services only) -- zero
      hits for maskedmerge/dofmask/gblur/sigma=3.2/dilate 31.
    - commit 0cc1ade itself (`git show --stat 0cc1ade` in the main V2/v1
      repos, since that hash is a repo commit, not an engine-repo one) only
      touches LIPSYNC_LEDGER.md + RECIPE.json -- the postmortem write-up and
      one recipe-string correction, no code.
    - review/ (C:/DropCat-Studio-V2/review) contains only
      render_v16_detached.ps1 -- no DOF script, review/assets/ is empty.
    - grep -r for maskedmerge/dofmask/sigma=3.2/dilate 31/gblur=sigma across
      engine/, review/, and the whole V2 app tree: the only hits are
      features/song_video/scene_prep.py (builds the STATIC per-scene
      <image>.dofmask.png MASK IMAGE via dof_mask_from_subject() -- dilate
      31 + feather 24, matches the recipe -- but this is the matte, not the
      apply-to-video pass) and the unrelated features/outro/sting.py gblur
      (a fade-defocus title-card effect, different feature entirely).

The delivery that used this pass (chain60_v15_dof2.mp4) was produced by hand
in-session -- the ledger's own words are "reproduced by the MANAGER in the
finishing step" -- not from a saved, reusable script.

Per REVIEW_FINDINGS_2026-08-05.md's build instructions: DO NOT invent the
ffmpeg filter chain from the prose description above. This module is a
placeholder until someone with access to that session's actual shell
history (or a fresh, human-judged re-derivation) recovers the exact
frame-exact mask-video + single-pass maskedmerge command and ports it here
VERBATIM, with a LIPSYNC_LEDGER.md entry per that file's own change rule.

Segmented trim+concat is explicitly BANNED (see the quote above) -- do not
implement that as a stand-in; it reproduces the exact drift bug the ledger
entry fixed.
"""
from __future__ import annotations


class DofFinishNotImplemented(NotImplementedError):
    """Raised by every entry point in this module. See module docstring --
    the ratified method is documented in prose only; no script or literal
    ffmpeg command has been recovered to port verbatim."""


def apply_dof_finish(*_args, **_kwargs) -> str:
    """Would apply the ratified per-scene DOF finishing pass to a rendered
    chain.py output. NOT IMPLEMENTED -- see module docstring."""
    raise DofFinishNotImplemented(
        "DOF finish is not scripted yet. See LIPSYNC_LEDGER.md's 2026-08-05 "
        "~21:30 entry (engine repo commit 0cc1ade) for the ratified METHOD "
        "(prose description only, no command/script found in engine/ or "
        "review/). Recover or re-derive the exact frame-exact mask-video + "
        "single-pass maskedmerge command from that session, get it "
        "human-judged, and port it here verbatim -- do not invent the "
        "ffmpeg filter chain from the description. Segmented trim+concat is "
        "BANNED (causes ~0.25s A/V drift, see the same ledger entry)."
    )


def main() -> int:
    import sys
    print(
        "dof_finish.py: NOT IMPLEMENTED -- see module docstring for what is "
        "and isn't known. Do not paper over this with an invented ffmpeg "
        "filter chain.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
