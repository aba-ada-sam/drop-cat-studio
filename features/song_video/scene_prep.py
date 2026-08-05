"""Scene-prep pass: per-run vision director + deterministic executors.

Implements SCENE_PREP_SPEC_2026-08-05.md. PRIME RULE: character-general,
never character-coded -- identity comes from the user's subject pixels;
nothing pixel-precise comes from the LLM; nothing judgment-shaped comes
from the detectors.

The pass runs ONCE per job, before any render:
  1. DIRECTOR (haiku vision, one call per anchor image): strict-JSON scene
     judgment -- subject_query, distractor_queries (hands+object together),
     scene_description (truthful describe-the-image prompt), wants_occlusion,
     background_cast minimal-motion directions.
  2. GEOMETRY (local, deterministic): GroundingDINO text->box, SAM box->mask
     via the local Forge extension (the proven route).
  3. EXECUTORS here: subject matte -> per-scene DOF keep-sharp mask
     (dilate 31 + blur 24, the ratified build); scene_description -> the
     per-scene prompt for chain.py --scene-prompts.
     (Distractor inpaint sweep + plate/graft generation remain manual-
     assisted for now -- they land in this module when ratified.)
  4. FAIL-SAFE: any stage detecting nothing changes nothing. A run with
     zero detections renders exactly as today.

CLI:
  python -m features.song_video.scene_prep analyze <image> [image2 ...]
     -> writes <image>.sceneprep.json + <image>.dofmask.png next to each
        input (video-res masks are scaled by the caller), prints a summary.
"""
import base64
import io
import json
import logging
import os
import sys
import urllib.request

log = logging.getLogger("scene_prep")

FORGE = "http://127.0.0.1:7861"
JUDGE_MODEL = "claude-haiku-4-5-20251001"

DIRECTOR_PROMPT = """You are the scene director for a music-video pipeline. Look at this image and answer as STRICT JSON only (no prose).

{
  "subject_query": "<short text query naming the single SINGING subject, e.g. 'purple alien' -- the most visually distinctive noun phrase>",
  "distractor_queries": ["<queries for objects that would distract from singing: things held in hands (ALWAYS name the hands together with the object, e.g. 'dark object in the man's hands'), floating/unidentifiable items. Empty list if none>"],
  "scene_description": "<one truthful sentence describing THIS image's SETTING and background only -- do NOT name or describe the singing subject itself (the prompt names it separately); describe only what is actually visible>",
  "wants_occlusion": <true if the scene has natural foreground elements (grass, wheat, objects) that could sit IN FRONT of the subject to sell belonging; else false>,
  "background_cast": ["<for each background PERSON/CREATURE visible: a minimal-motion direction like 'stands calmly at the crates, slowly nodding'. Empty list if none>"]
}"""


def _api_key():
    k = os.environ.get("ANTHROPIC_API_KEY")
    if k:
        return k
    try:
        return json.load(open(r"C:\JSON Credentials\QB_WC_credentials.json"))["anthropic_key"]
    except Exception:
        return None


def director(image_path):
    """One haiku vision call -> the scene judgment dict, or None (fail-safe)."""
    key = _api_key()
    if not key:
        log.warning("scene_prep: no API key -- director skipped")
        return None
    try:
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
        img.thumbnail((1280, 1280))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        body = json.dumps({
            "model": JUDGE_MODEL, "max_tokens": 500,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.b64encode(buf.getvalue()).decode()}},
                {"type": "text", "text": DIRECTOR_PROMPT},
            ]}],
        }).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
                                     headers={"x-api-key": key,
                                              "anthropic-version": "2023-06-01",
                                              "content-type": "application/json"})
        res = json.loads(urllib.request.urlopen(req, timeout=90).read())
        txt = res["content"][0]["text"]
        txt = txt[txt.find("{"):txt.rfind("}") + 1]
        v = json.loads(txt)
        if not v.get("subject_query"):
            return None
        return v
    except Exception as e:
        log.warning("scene_prep: director failed (%s) -- pass changes nothing", e)
        return None


def subject_mask(image_path, subject_query):
    """DINO text->box, SAM box->mask on the local Forge route. Returns a PIL
    'L' mask at image size (largest candidate), or None (fail-safe)."""
    try:
        from PIL import Image
        import numpy as np
        img = Image.open(image_path).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        payload = {
            "sam_model_name": "sam_vit_b_01ec64.pth",
            "input_image": base64.b64encode(buf.getvalue()).decode(),
            "dino_enabled": True,
            "dino_model_name": "GroundingDINO_SwinT_OGC (694MB)",
            "dino_text_prompt": subject_query,
            "dino_box_threshold": 0.3,
        }
        req = urllib.request.Request(FORGE + "/sam/sam-predict",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        res = json.loads(urllib.request.urlopen(req, timeout=300).read())
        best, area = None, -1
        for m64 in res.get("masks", []):
            m = np.array(Image.open(io.BytesIO(base64.b64decode(m64))).convert("L").resize(img.size))
            a = int((m > 127).sum())
            if a > area:
                best, area = m, a
        frac = area / float(img.width * img.height)
        if best is None or not (0.03 < frac < 0.65):
            log.warning("scene_prep: subject mask implausible (%.1f%%) -- skipped", 100 * frac)
            return None
        return Image.fromarray(((best > 127) * 255).astype("uint8"))
    except Exception as e:
        log.warning("scene_prep: subject mask failed (%s)", e)
        return None


def dof_mask_from_subject(mask):
    """The ratified DOF keep-sharp build: dilate ~31 + feather 24."""
    from PIL import ImageFilter
    return mask.filter(ImageFilter.MaxFilter(31)).filter(ImageFilter.GaussianBlur(24))


def analyze(image_path):
    """Full pass for one anchor image. Writes sidecar artifacts, returns dict."""
    out = {"image": os.path.abspath(image_path), "director": None,
           "dof_mask": None, "scene_prompt": None}
    d = director(image_path)
    if not d:
        return out          # fail-safe: nothing detected, nothing changes
    out["director"] = d
    out["scene_prompt"] = (
        "A single subject, centered, facing the camera, mouth opening and "
        "closing in precise sync with the singing vocals, "
        + d["scene_description"].rstrip(". ")
        + (". " + " ".join("Behind him, " + b + "." for b in d.get("background_cast", []))
           if d.get("background_cast") else "")
        + " Soft cinematic lighting, steady framing.")
    m = subject_mask(image_path, d["subject_query"])
    if m is not None:
        dof = dof_mask_from_subject(m)
        mask_path = os.path.splitext(image_path)[0] + ".dofmask.png"
        dof.save(mask_path)
        out["dof_mask"] = mask_path
    side = os.path.splitext(image_path)[0] + ".sceneprep.json"
    json.dump(out, open(side, "w"), indent=1)
    return out


def main(argv):
    if len(argv) < 3 or argv[1] != "analyze":
        raise SystemExit(__doc__)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for p in argv[2:]:
        r = analyze(p)
        d = r["director"] or {}
        print(f"\n== {os.path.basename(p)}")
        print(f"  subject:     {d.get('subject_query')}")
        print(f"  distractors: {d.get('distractor_queries')}")
        print(f"  occlusion:   {d.get('wants_occlusion')}")
        print(f"  cast:        {d.get('background_cast')}")
        print(f"  dof mask:    {r['dof_mask']}")
        print(f"  prompt:      {r['scene_prompt']}")


if __name__ == "__main__":
    main(sys.argv)
