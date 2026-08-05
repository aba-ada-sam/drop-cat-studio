"""Mobile aesthetic-decision pages for Andrew (2026-08-05 flow).

Andrew's ruling: aesthetic decisions while he is out reach him as a Telegram
message with a LINK he can open on his phone and answer with a tap or a
comment. dropcatgo.com hosts the page; his ANSWER travels his own Telegram
chat with Buddy (typed "1"/"2"/comment, or the one-tap share link on each
choice card) -> telegram-pipe -> the studio board -> the manager session.
No server-side write-back, no polling, and the 5080 NEVER touches
dropcatgo directly (publishing goes through the warehouse two-hop, one
batched connection per publish).

Usage (from the manager session):
  python tools/mobile_decide.py build <decision-id> spec.json
     spec.json: {"question": "...", "note": "...", "options":
                 [{"label": "v14 (new)", "media": "path.mp4"}, ...]}
  -> builds C:\\DropCat-Studio\\output\\decide\\<decision-id>\\ (index.html
     + copied media), prints the ONE warehouse publish command and the
     Telegram notify command. Nothing is uploaded or sent by this script.
"""
import json, os, shutil, sys, html

BOT = "workerbuddy_lcff_bot"
SITE_BASE = "https://dropcatgo.com/decide"
DOCROOT = "/home/dropcatg/public_html/decide"          # created on first publish
OUT_BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "output", "decide")

PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title>
<style>
  body {{ background:#111; color:#eee; font-family:system-ui,Segoe UI,Arial,sans-serif;
         margin:0 auto; max-width:720px; padding:16px; }}
  h1 {{ font-size:19px; line-height:1.4; }}
  .note {{ color:#aaa; font-size:14px; line-height:1.5; }}
  .how {{ background:#1c2733; border:1px solid #2d4356; border-radius:10px;
          padding:10px 14px; font-size:14px; color:#cde; margin:14px 0; }}
  .card {{ background:#191919; border:1px solid #333; border-radius:12px;
           padding:12px; margin:16px 0; }}
  .pick {{ display:block; text-align:center; background:#2563eb; color:#fff;
           text-decoration:none; font-size:17px; font-weight:bold;
           padding:12px; border-radius:10px; margin-top:10px; }}
  .num {{ color:#ffd479; font-size:16px; font-weight:bold; }}
  video, img {{ width:100%; border-radius:8px; background:#000; }}
</style>
</head>
<body>
<h1>{question}</h1>
{note_html}
<div class="how">Answer in the Buddy chat: type the number (or any comment),
or tap a blue button to send it in one step.</div>
{cards}
</body>
</html>
"""

CARD = """<div class="card">
  <div class="num">{n} -- {label}</div>
  {media_tag}
  <a class="pick" href="https://t.me/share/url?url={reply}&text={reply}">Pick {n}: send "{reply}" in Telegram</a>
</div>
"""


def media_tag(fname):
    ext = fname.rsplit(".", 1)[-1].lower()
    if ext in ("mp4", "webm", "mov"):
        return f'<video controls playsinline preload="metadata" src="{fname}"></video>'
    return f'<img src="{fname}">'


def build(decision_id, spec_path):
    spec = json.load(open(spec_path))
    out = os.path.join(OUT_BASE, decision_id)
    os.makedirs(out, exist_ok=True)
    cards = []
    for i, opt in enumerate(spec["options"], 1):
        src = opt["media"]
        fname = f"{i:02d}_" + os.path.basename(src).replace(" ", "_")
        shutil.copy2(src, os.path.join(out, fname))
        reply = f"DECIDE {decision_id} {i}"
        cards.append(CARD.format(n=i, label=html.escape(opt.get("label", f"option {i}")),
                                 media_tag=media_tag(fname),
                                 reply=reply.replace(" ", "%20")))
    note = spec.get("note", "")
    note_html = f'<div class="note">{html.escape(note)}</div>' if note else ""
    page = PAGE.format(title=html.escape(spec["question"])[:60],
                       question=html.escape(spec["question"]),
                       note_html=note_html, cards="".join(cards))
    with open(os.path.join(out, "index.html"), "w", encoding="ascii",
              errors="strict") as f:
        f.write(page)
    url = f"{SITE_BASE}/{decision_id}/"
    print(f"BUILT {out}")
    print("\nPUBLISH (one batched warehouse two-hop; from the 5080):")
    print(f'  scp -r "{out}" warehouse-laptop:/tmp/decide_{decision_id}/ '
          f'&& ssh warehouse-laptop "ssh dropcatg@dropcatgo \'mkdir -p {DOCROOT}\' '
          f'&& scp -r /tmp/decide_{decision_id}/* dropcatg@dropcatgo:{DOCROOT}/{decision_id}/ '
          f'&& ssh dropcatg@dropcatgo \'ls {DOCROOT}/{decision_id}/\' '
          f'&& rm -rf /tmp/decide_{decision_id}"')
    print("\nNOTIFY (Telegram, 1-2 sentences max):")
    print(f'  cd C:\\Users\\andre\\ClaudeBuddy && python send_message.py '
          f'"Aesthetic pick needed: {url} -- reply 1/2 (or a comment)."')
    print("\nANSWER arrives as 'DECIDE {id} <n>' or free text via telegram-pipe "
          "on the studio board.")


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "build":
        raise SystemExit(__doc__)
    build(sys.argv[2], sys.argv[3])
