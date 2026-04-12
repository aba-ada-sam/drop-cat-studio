"""Generate Drop Cat Go Studio assets — circus / burlesque theme."""
from PIL import Image, ImageDraw, ImageFont
import math, os

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# ── Circus palette ────────────────────────────────────────────────────────────
BG       = (13,  6,  6)      # near-black with red tinge
SURFACE  = (30, 10, 10)      # deep burgundy-dark
CRIMSON  = (139,  0,  0)     # dark circus red
RED      = (196, 30, 58)     # bright circus red
GOLD     = (212, 160, 23)    # rich gold
GOLD2    = (232, 192, 64)    # bright gold
CREAM    = (240, 230, 208)   # old poster cream
TAN      = (180, 140, 90)    # warm tan
DARK     = (90,  40, 40)     # dark reddish


def draw_cat_logo(draw, cx, cy, size, body_col, accent_col):
    """Cat head inside a film frame, circus colours."""
    s  = size
    fw = int(s * 0.82)
    fh = int(s * 0.68)
    fx = cx - fw // 2
    fy = cy - fh // 2 + int(s * 0.04)
    r  = int(s * 0.12)

    # Film frame — double border for ornate look
    draw.rounded_rectangle([fx - 3, fy - 3, fx + fw + 3, fy + fh + 3],
                            radius=r + 2, outline=accent_col, width=max(2, int(s * 0.025)))
    draw.rounded_rectangle([fx, fy, fx + fw, fy + fh],
                            radius=r, outline=body_col, width=max(2, int(s * 0.04)))

    # Sprocket holes
    hole = int(s * 0.065)
    gap  = int(s * 0.14)
    n    = 4
    sy   = fy + (fh - n * gap) // 2 + int(gap * 0.1)
    for i in range(n):
        hy = sy + i * gap
        for lx in [fx + int(s * 0.03), fx + fw - int(s * 0.03) - hole]:
            draw.rounded_rectangle([lx, hy, lx + hole, hy + hole],
                                    radius=2, fill=BG, outline=accent_col,
                                    width=max(1, int(s * 0.022)))

    # Cat head
    hcx, hcy, hr = cx, cy + int(s * 0.06), int(s * 0.20)
    draw.ellipse([hcx - hr, hcy - hr, hcx + hr, hcy + hr], fill=body_col)

    # Ears
    ew, eh = int(s * 0.09), int(s * 0.14)
    for ex in [-1, 1]:
        bl = hcx + ex * int(hr * 0.55) - ew // 2
        br = bl + ew
        by = hcy - int(hr * 0.62)
        tx = hcx + ex * int(hr * 0.58)
        ty = by - eh
        draw.polygon([(bl, by), (br, by), (tx, ty)], fill=body_col)
        iw = int(ew * 0.52)
        il = hcx + ex * int(hr * 0.55) - iw // 2
        draw.polygon([(il, by), (il + iw, by), (tx, ty - int(eh*0.3))], fill=SURFACE)

    # Eyes
    ey_y = hcy - int(hr * 0.10)
    for ex in [-1, 1]:
        ecx = hcx + ex * int(hr * 0.42)
        ew2, eh2 = int(hr * 0.28), int(hr * 0.20)
        draw.ellipse([ecx - ew2, ey_y - eh2, ecx + ew2, ey_y + eh2], fill=BG)
        draw.ellipse([ecx - int(ew2*0.42), ey_y - int(eh2*0.65),
                      ecx + int(ew2*0.42), ey_y + int(eh2*0.65)], fill=DARK)

    # Nose + whiskers
    ny = ey_y + int(hr * 0.38)
    ns = int(hr * 0.09)
    draw.polygon([(hcx, ny-ns), (hcx-int(ns*1.1), ny+int(ns*0.6)),
                  (hcx+int(ns*1.1), ny+int(ns*0.6))], fill=BG)
    wlen = int(hr * 0.55)
    wt   = max(1, int(s * 0.016))
    for ex in [-1, 1]:
        for ang in [-0.12, 0.0, 0.12]:
            dx = int(ex * math.cos(ang) * wlen)
            dy = int(math.sin(ang) * wlen * 0.35)
            ox = hcx + ex * int(hr * 0.14)
            draw.line([(ox, ny + int(ns*0.2)), (ox+dx, ny+int(ns*0.2)+dy)],
                      fill=accent_col, width=wt)

    # Play-button triangle
    ts  = int(hr * 0.30)
    tcx = hcx + int(hr * 0.60)
    tcy = hcy + int(hr * 0.55)
    draw.polygon([(tcx-int(ts*0.6), tcy-ts),
                  (tcx-int(ts*0.6), tcy+ts),
                  (tcx+int(ts*0.85), tcy)], fill=BG)


def ornament_border(draw, x0, y0, x1, y1, col, width=2):
    """Decorative double-line border."""
    draw.rectangle([x0, y0, x1, y1], outline=col, width=width)
    pad = 4
    draw.rectangle([x0+pad, y0+pad, x1-pad, y1-pad], outline=(*col, 80), width=1)


def corner_stars(draw, x0, y0, x1, y1, col, size=12):
    """Tiny 4-point stars at each corner."""
    for cx, cy in [(x0+size, y0+size), (x1-size, y0+size),
                   (x0+size, y1-size), (x1-size, y1-size)]:
        for ang in [0, 90, 180, 270]:
            rad = math.radians(ang)
            draw.line([(cx, cy),
                       (cx + int(math.cos(rad)*size*0.7),
                        cy + int(math.sin(rad)*size*0.7))],
                      fill=col, width=2)


def make_logo(size=512):
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    m    = int(size * 0.04)
    r    = int(size * 0.16)
    cx, cy = size // 2, size // 2

    # Background with ornate border
    draw.rounded_rectangle([m, m, size-m, size-m], radius=r, fill=SURFACE)
    draw.rounded_rectangle([m, m, size-m, size-m], radius=r,
                            outline=GOLD, width=max(3, int(size*0.014)))
    draw.rounded_rectangle([m+6, m+6, size-m-6, size-m-6], radius=r-4,
                            outline=(*GOLD, 60), width=1)

    # Crimson glow
    for g in range(16, 0, -1):
        gr = int(size*0.34) + (16-g)*3
        draw.ellipse([cx-gr, cy-gr+int(size*0.02), cx+gr, cy+gr+int(size*0.02)],
                     fill=(*RED, int(g*2)))
    # Gold glow overlay
    for g in range(10, 0, -1):
        gr = int(size*0.28) + (10-g)*2
        draw.ellipse([cx-gr, cy-gr+int(size*0.02), cx+gr, cy+gr+int(size*0.02)],
                     fill=(*GOLD, int(g*3)))

    # Corner stars
    corner_stars(draw, m+10, m+10, size-m-10, size-m-10, GOLD, int(size*0.022))

    # Cat logo
    draw_cat_logo(draw, cx, cy - int(size*0.04), int(size*0.72), GOLD, CREAM)

    # Load fonts
    font_dir = "C:/Windows/Fonts/"
    try:
        font_name  = ImageFont.truetype(font_dir + "Impact.ttf",      int(size*0.095))
        font_sub   = ImageFont.truetype(font_dir + "Arial.ttf",        int(size*0.048))
        font_ital  = ImageFont.truetype(font_dir + "Ariali.ttf",       int(size*0.052))
    except Exception:
        font_name = font_sub = font_ital = ImageFont.load_default()

    # "Andrew's" — small italic above
    andy_y = cy + int(size * 0.335)
    draw.text((cx+2, andy_y+2), "Andrew's", font=font_ital, fill=(0,0,0,100), anchor="mm")
    draw.text((cx,   andy_y),   "Andrew's", font=font_ital, fill=CREAM,        anchor="mm")

    # Ornamental divider
    div_y = andy_y + int(size * 0.058)
    dw    = int(size * 0.30)
    draw.line([(cx-dw, div_y), (cx+dw, div_y)], fill=GOLD, width=max(1, int(size*0.01)))
    draw.ellipse([cx-4, div_y-4, cx+4, div_y+4], fill=GOLD)

    # "DROP CAT GO" — big Impact
    title_y = div_y + int(size * 0.072)
    for dx, dy, col in [(2, 2, (0,0,0,120)), (0, 0, GOLD)]:
        draw.text((cx+dx, title_y+dy), "DROP CAT GO", font=font_name, fill=col, anchor="mm")

    # "STUDIO" — smaller, spaced
    studio_y = title_y + int(size * 0.095)
    draw.text((cx+1, studio_y+1), "S T U D I O", font=font_sub, fill=(0,0,0,100), anchor="mm")
    draw.text((cx,   studio_y),   "S T U D I O", font=font_sub, fill=CREAM,        anchor="mm")

    return img


logo512 = make_logo(512)
logo512.save(os.path.join(STATIC, "logo-512.png"))
logo192 = make_logo(192)
logo192.save(os.path.join(STATIC, "logo-192.png"))
print("logo-512.png  logo-192.png")


def make_favicon():
    size = 64
    img  = Image.new("RGBA", (size, size), (0,0,0,0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, size-1, size-1], radius=int(size*0.16), fill=SURFACE,
                            outline=GOLD, width=2)
    draw_cat_logo(draw, size//2, size//2, int(size*0.84), GOLD, CREAM)
    return img.resize((32,32), Image.LANCZOS)

make_favicon().save(os.path.join(STATIC, "favicon.ico"), format="ICO", sizes=[(32,32)])
print("favicon.ico")


def tab_icon(sym_fn, name):
    size = 96
    img  = Image.new("RGBA", (size,size), (0,0,0,0))
    draw = ImageDraw.Draw(img)
    r    = int(size*0.22)
    draw.rounded_rectangle([2,2,size-2,size-2], radius=r,
                            fill=(*SURFACE, 230), outline=(*GOLD, 110), width=2)
    sym_fn(draw, size//2, size//2, size)
    img.resize((48,48), Image.LANCZOS).save(os.path.join(STATIC, f"icon-{name}.png"))
    print(f"icon-{name}.png")


def sym_camera(draw, cx, cy, s):
    bw, bh = int(s*.52), int(s*.36)
    bx, by = cx-bw//2, cy-bh//2
    draw.rounded_rectangle([bx,by,bx+bw,by+bh], radius=6, fill=GOLD)
    lr = int(s*.13)
    draw.ellipse([cx-lr,cy-lr,cx+lr,cy+lr], fill=BG, outline=GOLD2, width=2)
    draw.ellipse([cx-lr//2,cy-lr//2,cx+lr//2,cy+lr//2], fill=GOLD2)
    bump=int(s*.16)
    draw.rounded_rectangle([cx-bump//2,by-int(s*.10),cx+bump//2,by+2], radius=3, fill=GOLD)

def sym_bridge(draw, cx, cy, s):
    for ox in [-int(s*.15), int(s*.15)]:
        rw,rh = int(s*.22),int(s*.14)
        draw.rounded_rectangle([cx+ox-rw,cy-rh,cx+ox+rw,cy+rh], radius=rh, outline=GOLD, width=int(s*.06))
    draw.line([(cx-int(s*.07),cy),(cx+int(s*.07),cy)], fill=GOLD, width=int(s*.06))

def sym_prompt(draw, cx, cy, s):
    dw,dh = int(s*.40),int(s*.48)
    dx,dy = cx-dw//2,cy-dh//2
    draw.rounded_rectangle([dx,dy,dx+dw,dy+dh], radius=4, fill=(*GOLD,30), outline=GOLD, width=2)
    for i,fw in enumerate([0.7,0.9,0.6,0.8]):
        lw=int(dw*fw); ly=dy+int(dh*.20)+i*int(dh*.20)
        draw.line([(cx-lw//2,ly),(cx-lw//2+lw,ly)], fill=GOLD, width=max(2,int(s*.04)))
    for ang in range(0,360,45):
        rad=math.radians(ang); r1,r2=int(s*.12),int(s*.06)
        sx,sy=cx+int(dw*.35),dy-int(s*.04)
        draw.line([(sx+int(math.cos(rad)*r2),sy+int(math.sin(rad)*r2)),
                   (sx+int(math.cos(rad)*r1),sy+int(math.sin(rad)*r1))], fill=GOLD2, width=2)

def sym_filmstrip(draw, cx, cy, s):
    fw,fh=int(s*.70),int(s*.40); fx,fy=cx-fw//2,cy-fh//2
    draw.rounded_rectangle([fx,fy,fx+fw,fy+fh], radius=4, fill=(*GOLD,40), outline=GOLD, width=2)
    fw2,n=int(fw*.22),3; gap=(fw-n*fw2)//(n+1)
    for i in range(n):
        ox=fx+gap+i*(fw2+gap)
        draw.rounded_rectangle([ox,fy+int(fh*.2),ox+fw2,fy+int(fh*.8)], radius=2, fill=GOLD)
    for ix in [0.2,0.5,0.8]:
        for iy in [0.08,0.88]:
            hx,hy,hs=fx+int(fw*ix),fy+int(fh*iy),int(s*.04)
            draw.ellipse([hx-hs,hy-hs,hx+hs,hy+hs], fill=BG)

def sym_tools(draw, cx, cy, s):
    r=int(s*.20)
    draw.arc([cx-r,cy-r,cx+r,cy+r], start=30, end=300, fill=GOLD, width=int(s*.08))
    draw.polygon([(cx-r+int(s*.04),cy-int(s*.06)),(cx-r-int(s*.04),cy-int(s*.04)),
                  (cx-r-int(s*.04),cy+int(s*.04))], fill=GOLD)
    for i,lw in enumerate([.30,.36,.28]):
        ly=cy+int(s*.22)+i*int(s*.07)
        draw.line([(cx+int(s*.06),ly),(cx+int(s*.06)+int(s*lw),ly)], fill=GOLD, width=max(2,int(s*.045)))

def sym_wand(draw, cx, cy, s):
    wx1,wy1=cx-int(s*.22),cy+int(s*.22); wx2,wy2=cx+int(s*.22),cy-int(s*.22)
    draw.line([(wx1,wy1),(wx2,wy2)], fill=GOLD, width=max(3,int(s*.07)))
    pts=[]
    for i in range(10):
        ang=math.radians(-90+i*36); r=int(s*.14) if i%2==0 else int(s*.07)
        pts.append((wx2+int(math.cos(ang)*r), wy2+int(math.sin(ang)*r)))
    draw.polygon(pts, fill=GOLD2)
    for sx,sy,sr in [(cx-int(s*.18),cy-int(s*.18),int(s*.05)),
                     (cx+int(s*.28),cy,int(s*.04)),
                     (cx,cy+int(s*.30),int(s*.04))]:
        draw.ellipse([sx-sr,sy-sr,sx+sr,sy+sr], fill=GOLD)

tab_icon(sym_camera,    "fun-videos")
tab_icon(sym_bridge,    "bridges")
tab_icon(sym_prompt,    "sd-prompts")
tab_icon(sym_filmstrip, "image2video")
tab_icon(sym_tools,     "video-tools")
tab_icon(sym_wand,      "wildcards")

print("\nAll circus-themed assets generated.")
