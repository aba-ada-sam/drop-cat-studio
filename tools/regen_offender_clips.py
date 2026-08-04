#!/usr/bin/env python3
"""Surgically regenerate instrument-following clips in a chain-rendered song video.

Tier-2 fix (2026-08-04): for each OFFENDER clip (mouth follows instruments during
vocal gaps), re-render JUST that clip via make_clip.py with a PRE-GATED audio
slice -- silero phrase-level gating (DCS vocal_activity.voiced_intervals, finer
than chain.py's RMS gate, catches quiet vocals) with DCMVS's soft-edge lesson
(0.18s trapezoid ramps, hard gates make the mouth pop) and its over-mute breaker
(>85% muted => use ungated slice). Then splice the winners back into the full
video at exact cut points (scene-detected near the estimated boundary) and remux
the ORIGINAL song once.

Usage (local or pod; only stdlib + ffmpeg + the DCS repo on sys.path):
  python tools/regen_offender_clips.py \
    --video Desktop/DCS_Review/dcmvs_adam_friends_alien.mp4 \
    --song  uploads/0cac7571_Adam_Friends.mpeg \
    --stem  output/2026-08-03/songvid_awm_alien_sour_af683aca9174/guide_vocals.wav \
    --image uploads/awm_alien_source.png \
    --offenders 2:17.05:36.76 7:115.58:135.29 \
    --worker http://127.0.0.1:7899 \
    --make-clip C:/DCMVS-restored/make_clip.py \
    --sync-qc  C:/DCMVS-restored/sync_qc.py \
    --takes 3 --out final_fixed.mp4

--offenders entries are INDEX:START:END in seconds (from the offender report).
Exit codes: 0 ok, 1 fatal, 2 no offender improved (original kept).
"""
import argparse, json, math, os, shutil, subprocess, sys, tempfile
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
RAMP = 0.18          # DCMVS soft-edge seconds
OVERMUTE_FRAC = 0.85 # DCMVS circuit breaker


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError("cmd failed: %s\n%s" % (" ".join(map(str, cmd)), r.stderr[-1200:]))
    return r


def probe_duration(path):
    r = run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(r.stdout.strip())


def voiced_intervals_for(stem, repo_root):
    sys.path.insert(0, str(repo_root))
    from features.lipsync.vocal_activity import voiced_intervals
    return voiced_intervals(str(stem))


def trapezoid_volume_expr(keep_spans, total):
    """ffmpeg volume=eval=frame expression: 1.0 inside keep spans with RAMP
    fades at the edges, 0 outside. Mirrors chain.py's mute_regions approach."""
    terms = []
    for (s, e) in keep_spans:
        s0, s1 = max(0.0, s - RAMP), s
        e0, e1 = e, min(total, e + RAMP)
        up = "min(1,(t-%.3f)/%.3f)" % (s0, max(RAMP, 1e-3))
        down = "min(1,(%.3f-t)/%.3f)" % (e1, max(RAMP, 1e-3))
        terms.append("between(t,%.3f,%.3f)*min(%s,%s)" % (s0, e1, up, down))
    return "max(0,min(1,%s))" % ("+".join(terms) if terms else "0")


def build_gated_slice(song, stem_intervals, t0, t1, out_wav):
    """Cut [t0,t1) from the song's ISOLATED-STEM timeline with phrase-level
    soft gating. Spans are shifted into slice-local time.

    Three regimes:
    - phrases present, most survive -> soft-gated slice (the normal case)
    - phrases present but the gate would mute >85% -> ungated slice (DCMVS's
      over-mute breaker: that pattern means the SEPARATION is bad, and a bad
      gate is worse than none)
    - ZERO phrases in the window (genuinely instrumental, e.g. outro clips)
      -> SILENT slice, so conditioning drives the mouth to REST. Ungating
      here would recreate instrument-following -- the breaker is protection
      against bad separation, not a license to sing to the band."""
    dur = t1 - t0
    local = [(max(0.0, s - t0), min(dur, e - t0))
             for (s, e) in stem_intervals if e > t0 and s < t1]
    if not local:
        run([FFMPEG, "-y", "-f", "lavfi", "-i",
             "anullsrc=r=44100:cl=stereo", "-t", "%.3f" % dur,
             "-c:a", "pcm_s16le", str(out_wav)])
        return "silent"
    kept = sum(e - s for s, e in local)
    if dur - kept > OVERMUTE_FRAC * dur:
        vol = None  # over-mute breaker: condition on the ungated slice
    else:
        vol = trapezoid_volume_expr(local, dur)
    cmd = [FFMPEG, "-y", "-ss", "%.3f" % t0, "-t", "%.3f" % dur, "-i", str(song),
           "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le"]
    if vol:
        cmd += ["-af", "volume='%s':eval=frame" % vol]
    cmd += [str(out_wav)]
    run(cmd)
    return "gated" if vol else "ungated-breaker"


def exact_cut(video, approx_t, window=1.5):
    """Find the hard-cut frame nearest approx_t via scene detection."""
    if approx_t <= 0.05:
        return 0.0
    r = subprocess.run(
        [FFMPEG, "-ss", "%.3f" % (approx_t - window), "-t", "%.3f" % (2 * window),
         "-i", str(video), "-vf", "select='gt(scene,0.30)',showinfo", "-f", "null", "-"],
        capture_output=True, text=True)
    best, best_d = approx_t, window + 1
    for line in r.stderr.splitlines():
        if "pts_time:" in line:
            try:
                t_local = float(line.split("pts_time:")[1].split()[0])
            except (ValueError, IndexError):
                continue
            t_abs = approx_t - window + t_local
            if abs(t_abs - approx_t) < best_d:
                best, best_d = t_abs, abs(t_abs - approx_t)
    return best


def score(sync_qc_py, py, clip, audio):
    r = subprocess.run([py, str(sync_qc_py), str(clip), "--audio", str(audio),
                        "--json", str(clip) + ".score.json"],
                       capture_output=True, text=True)
    try:
        d = json.load(open(str(clip) + ".score.json"))[0]
        return d
    except Exception:
        return {"error": r.stderr[-200:] or "score failed"}


def mouth_rank(d):
    if not d or d.get("error"):
        return 0.0
    m = d.get("total_motion", 0)
    if m < 0.15:
        return 0.0
    mw = min(1.0, m / 1.2)
    gauss = math.exp(-(((d.get("sync_y", 0) - 0.68) / 0.18) ** 2))
    return max(0.0, d.get("sync_contrast", 0)) * gauss * mw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--song", required=True)
    ap.add_argument("--stem", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--offenders", nargs="+", required=True,
                    help="INDEX:START:END triplets, seconds")
    ap.add_argument("--worker", default="http://127.0.0.1:7899")
    ap.add_argument("--make-clip", required=True)
    ap.add_argument("--sync-qc", required=True)
    ap.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--takes", type=int, default=3)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workdir", default=None)
    a = ap.parse_args()

    wd = Path(a.workdir or tempfile.mkdtemp(prefix="regen_"))
    wd.mkdir(parents=True, exist_ok=True)
    py = sys.executable

    print("[regen] voiced intervals (silero, DCS gate) ...")
    intervals = voiced_intervals_for(a.stem, a.repo_root)
    print("[regen] %d phrases" % len(intervals))

    total = probe_duration(a.video)
    offenders = []
    for spec in a.offenders:
        idx, t0, t1 = spec.split(":")
        offenders.append((int(idx), float(t0), float(t1)))
    offenders.sort(key=lambda x: x[1])

    replacements = []  # (t0_exact, t1_exact, new_clip_path)
    for (idx, t0a, t1a) in offenders:
        t0, t1 = exact_cut(a.video, t0a), exact_cut(a.video, t1a)
        dur = t1 - t0
        print("[regen] clip %d: %.2f-%.2f (%.2fs, cuts snapped from %.2f/%.2f)"
              % (idx, t0, t1, dur, t0a, t1a))
        wav = wd / ("slice_%02d.wav" % idx)
        gated = build_gated_slice(a.song, intervals, t0, t1, wav)
        print("[regen]   slice gated=%s" % gated)
        best, best_rank = None, -1.0
        for take in range(a.takes):
            out = wd / ("clip_%02d_take%d.mp4" % (idx, take))
            r = subprocess.run([py, a.make_clip, "--image", a.image, "--audio",
                                str(wav), "--seed", "-1", "--worker", a.worker,
                                "--out", str(out)], capture_output=True, text=True)
            if r.returncode != 0 or not out.exists():
                print("[regen]   take %d RENDER FAILED" % take)
                continue
            d = score(a.sync_qc, py, out, wav)
            rank = mouth_rank(d)
            print("[regen]   take %d verdict=%s rank=%.4f" % (take, d.get("verdict"), rank))
            if rank > best_rank:
                best, best_rank = out, rank
            if d.get("verdict") == "synced":
                print("[regen]   early accept")
                best = out
                break
        if best is None:
            print("[regen] clip %d: NO usable take -- keeping original" % idx)
            continue
        replacements.append((t0, t1, best))

    if not replacements:
        print("[regen] nothing improved; original kept")
        sys.exit(2)

    print("[regen] splicing %d replacement(s) ..." % len(replacements))
    segs, cursor, n = [], 0.0, 0
    inputs, filters = [], []
    for (t0, t1, clip) in replacements:
        if t0 > cursor + 0.02:
            segs.append(("orig", cursor, t0))
        segs.append(("new", clip, t1 - t0))
        cursor = t1
    if cursor < total - 0.02:
        segs.append(("orig", cursor, total))

    for s in segs:
        if s[0] == "orig":
            inputs += ["-i", a.video]
            filters.append("[%d:v]trim=start=%.3f:end=%.3f,setpts=PTS-STARTPTS[v%d];"
                           % (n, s[1], s[2], n))
        else:
            inputs += ["-i", str(s[1])]
            filters.append("[%d:v]scale=%s,setpts=PTS-STARTPTS[v%d];"
                           % (n, "iw:ih", n))
        n += 1
    fc = "".join(filters) + "".join("[v%d]" % i for i in range(n)) + \
         "concat=n=%d:v=1:a=0[v]" % n
    stitched = wd / "stitched_fixed.mp4"
    run([FFMPEG, "-y"] + inputs + ["-filter_complex", fc, "-map", "[v]",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-pix_fmt", "yuv420p", "-vsync", "cfr", str(stitched)])
    run([FFMPEG, "-y", "-i", str(stitched), "-i", a.song, "-map", "0:v",
         "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", "-shortest", a.out])
    print("[regen] DONE -> %s" % a.out)


if __name__ == "__main__":
    main()
