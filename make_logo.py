"""
Drop Cat Go Studio — Circus Poster Logo Generator
Renders a full circus-style poster logo at multiple sizes and exports
logo-512.png, logo-256.png, logo-192.png, favicon.ico
"""

from PIL import Image, ImageDraw, ImageFont, ImageFilter
import math, os, shutil

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
FONTS  = "C:/Windows/Fonts/"

# ── Palette ───────────────────────────────────────────────────────────────────
BG_DARK   = ( 10,   3,   3)   # near-black with red warmth
BG_MID    = ( 28,   8,   8)   # deep burgundy
CRIMSON   = (160,  16,  32)   # circus red (slightly desaturated for print look)
CRIMSON_B = (196,  28,  48)   # bright red
GOLD      = (200, 148,  18)   # antique gold
GOLD_B    = (230, 185,  55)   # bright gold highlight
GOLD_C    = (252, 220, 100)   # pale gold / highlight
CREAM     = (242, 232, 210)   # old paper cream
TAN       = (180, 145,  90)   # warm tan
DARK_WARM = ( 70,  30,  20)   # shadow tone


def load_font(name, size, fallback="Arial.ttf"):
    """Try to load a font, fall back gracefully."""
    for candidate in [name, fallback, "ArialBd.ttf", "Arial.ttf"]:
        try:
            return ImageFont.truetype(FONTS + candidate, size)
        except Exception:
            pass
    return ImageFont.load_default()


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i]-a[i])*t) for i in range(len(a)))


def draw_gradient_rect(draw, x0, y0, x1, y1, col_top, col_bot):
    """Vertical gradient fill."""
    h = y1 - y0
    for y in range(h):
        t   = y / max(h-1, 1)
        col = lerp(col_top, col_bot, t)
        draw.line([(x0, y0+y), (x1, y0+y)], fill=col)


def radial_vignette(img, strength=0.55):
    """Darken edges — classic poster effect."""
    w, h   = img.size
    cx, cy = w/2, h/2
    pix    = img.load()
    for y in range(h):
        for x in range(w):
            dx = (x-cx)/cx
            dy = (y-cy)/cy
            d  = min(1.0, math.sqrt(dx*dx + dy*dy))
            fade = 1.0 - strength * (d**1.8)
            r, g, b, a = pix[x, y]
            pix[x, y] = (int(r*fade), int(g*fade), int(b*fade), a)
    return img


def ornate_border(draw, x0, y0, x1, y1, gold, crimson, lw=3):
    """Double-line gold/red ornamental border."""
    pad = lw + 3
    # Outer gold line
    draw.rectangle([x0,    y0,    x1,    y1],    outline=gold,    width=lw)
    # Inner red line
    draw.rectangle([x0+pad, y0+pad, x1-pad, y1-pad], outline=crimson, width=max(1, lw-1))
    # Extra thin gold inset
    draw.rectangle([x0+pad*2, y0+pad*2, x1-pad*2, y1-pad*2],
                   outline=(*gold, 80), width=1)


def corner_ornament(draw, cx, cy, size, gold, gold_b):
    """Four-pointed star / compass rose at a corner."""
    s = size
    for ang in [0, 90, 180, 270]:
        rad = math.radians(ang)
        draw.line([(cx, cy),
                   (cx + int(math.cos(rad)*s*1.0),
                    cy + int(math.sin(rad)*s*1.0))],
                  fill=gold, width=max(1, s//6))
    for ang in [45, 135, 225, 315]:
        rad = math.radians(ang)
        draw.line([(cx, cy),
                   (cx + int(math.cos(rad)*s*0.65),
                    cy + int(math.sin(rad)*s*0.65))],
                  fill=gold_b, width=max(1, s//9))
    draw.ellipse([cx-s//4, cy-s//4, cx+s//4, cy+s//4], fill=gold_b)


def star_row(draw, cx, cy, n, spacing, r, col):
    """Horizontal row of 5-point stars."""
    total_w = (n-1) * spacing
    sx      = cx - total_w//2
    for i in range(n):
        x = sx + i * spacing
        pts = []
        for j in range(10):
            ang  = math.radians(-90 + j*36)
            rad  = r if j%2==0 else r*0.45
            pts.append((x + int(math.cos(ang)*rad),
                        cy + int(math.sin(ang)*rad)))
        draw.polygon(pts, fill=col)


def draw_scroll_banner(draw, cx, cy, w, h, fill, border, text, font, text_col):
    """Scroll/ribbon banner with curled ends."""
    hw = w//2
    hh = h//2
    # Main body
    draw.rounded_rectangle([cx-hw, cy-hh, cx+hw, cy+hh],
                            radius=hh//2, fill=fill, outline=border, width=2)
    # Left curl shadow
    curl_w = int(w*0.08)
    draw.ellipse([cx-hw-curl_w//2, cy-hh+2, cx-hw+curl_w, cy+hh-2],
                 fill=(*fill[:3], 160))
    draw.arc([cx-hw-curl_w//2, cy-hh+2, cx-hw+curl_w, cy+hh-2],
             start=270, end=90, fill=border, width=2)
    # Right curl shadow
    draw.ellipse([cx+hw-curl_w, cy-hh+2, cx+hw+curl_w//2, cy+hh-2],
                 fill=(*fill[:3], 160))
    draw.arc([cx+hw-curl_w, cy-hh+2, cx+hw+curl_w//2, cy+hh-2],
             start=90, end=270, fill=border, width=2)
    # Text
    draw.text((cx, cy), text, font=font, fill=text_col, anchor="mm")


def draw_cat(draw, cx, cy, size, body, eye_bg, whisker_col, accent):
    """
    Circus cat in top hat. Draw order (back to front):
      hat crown shadow → hat crown → hat band → hat brim →
      ears → head shadow → head → collar → eyes → nose → mouth → whiskers
    """
    s   = size
    hr  = int(s * 0.36)          # head radius

    HAT_W   = int(s * 0.30)      # crown width — narrower for taller look
    HAT_H   = int(s * 0.46)      # crown height — taller hat
    BRIM_W  = int(s * 0.60)      # brim width
    BRIM_H  = int(s * 0.07)      # brim thickness
    HAT_COL = (50, 20, 10)       # dark chocolate

    # Brim sits exactly on top of the head circle
    brim_cy = cy - hr + int(hr * 0.10)   # slightly into the head for snug fit
    brim_t  = brim_cy - BRIM_H // 2
    brim_b  = brim_cy + BRIM_H // 2

    crown_b = brim_t              # crown base = top of brim
    crown_t = crown_b - HAT_H     # crown top
    crown_l = cx - HAT_W // 2
    crown_r = cx + HAT_W // 2

    # 1. Crown drop-shadow
    so = int(s * 0.022)
    draw.rectangle([crown_l+so, crown_t+so, crown_r+so, crown_b+so],
                   fill=(*DARK_WARM, 100))

    # 2. Crown body — paint column by column for a subtle left-lit gradient
    for xi in range(crown_r - crown_l):
        t_val = xi / max(crown_r - crown_l - 1, 1)
        # Left face brighter, right face darker
        bright = lerp((78, 34, 14), HAT_COL, min(1.0, t_val * 1.6))
        draw.line([(crown_l + xi, crown_t), (crown_l + xi, crown_b)], fill=bright)
    # Re-draw outline on top
    draw.rectangle([crown_l, crown_t, crown_r, crown_b],
                   outline=GOLD_B, width=max(2, int(s*0.012)))

    # 3. Hat band (crimson stripe near brim)
    band_h = int(HAT_H * 0.18)
    band_t = crown_b - band_h - int(HAT_H * 0.06)
    draw.rectangle([crown_l+2, band_t, crown_r-2, band_t+band_h], fill=CRIMSON_B)
    draw.line([(crown_l+2, band_t),           (crown_r-2, band_t)],           fill=GOLD_B, width=max(1,int(s*0.007)))
    draw.line([(crown_l+2, band_t+band_h), (crown_r-2, band_t+band_h)], fill=GOLD_B, width=max(1,int(s*0.007)))

    # 4. Brim (wide, flat — drawn after crown so it overlaps cleanly)
    brim_l = cx - BRIM_W // 2
    draw.rounded_rectangle([brim_l, brim_t, brim_l+BRIM_W, brim_b],
                            radius=BRIM_H//2,
                            fill=HAT_COL, outline=GOLD_B, width=max(2, int(s*0.012)))

    # 5. Ears (poke out from sides, drawn before head)
    for sx in [-1, 1]:
        # Base of ear sits at the upper side of head
        ear_base_x  = cx + sx * int(hr * 0.56)
        ear_base_y  = cy - int(hr * 0.60)
        ear_half    = int(s * 0.14)
        ear_tip_x   = ear_base_x + sx * int(s * 0.04)
        ear_tip_y   = ear_base_y - int(s * 0.20)
        draw.polygon([
            (ear_base_x - ear_half, ear_base_y),
            (ear_base_x + ear_half, ear_base_y),
            (ear_tip_x, ear_tip_y)
        ], fill=body)
        # Inner ear
        iw = int(ear_half * 0.55)
        draw.polygon([
            (ear_base_x - iw, ear_base_y - int(s*0.02)),
            (ear_base_x + iw, ear_base_y - int(s*0.02)),
            (ear_tip_x, ear_tip_y + int(s*0.06))
        ], fill=CRIMSON)

    # 6. Head shadow
    so = int(s * 0.030)
    draw.ellipse([cx-hr+so, cy-hr+so, cx+hr+so, cy+hr+so], fill=(*DARK_WARM, 85))

    # 7. Head (covers ear bases and brim bottom edge for snug hat fit)
    draw.ellipse([cx-hr, cy-hr, cx+hr, cy+hr], fill=body)

    # ── Film-strip collar ──
    col_h  = int(s * 0.14)
    col_y0 = cy + int(hr * 0.66)
    col_y1 = col_y0 + col_h
    col_x0 = cx - int(s * 0.46)
    col_x1 = cx + int(s * 0.46)
    draw.rounded_rectangle([col_x0, col_y0, col_x1, col_y1],
                            radius=col_h//2, fill=DARK_WARM, outline=GOLD, width=2)
    # Sprocket holes in collar
    n_holes = 6
    gap     = (col_x1 - col_x0 - int(s*.04)) // n_holes
    hs      = int(s * 0.025)
    for i in range(n_holes):
        hx = col_x0 + int(s*.025) + i*gap + gap//2
        hy = (col_y0 + col_y1) // 2
        draw.ellipse([hx-hs, hy-hs, hx+hs, hy+hs], fill=BG_MID)

    # ── Eyes ──
    ey     = cy - int(hr * 0.12)
    for sx in [-1, 1]:
        ex  = cx + sx * int(hr * 0.42)
        ew  = int(hr * 0.30)
        eh  = int(hr * 0.22)
        # White of eye
        draw.ellipse([ex-ew, ey-eh, ex+ew, ey+eh], fill=CREAM)
        # Iris — vertical slit (cat eye)
        iw  = int(ew * 0.55)
        ih  = int(eh * 0.88)
        draw.ellipse([ex-iw, ey-ih, ex+iw, ey+ih], fill=accent)
        # Pupil (vertical slit)
        pw  = int(ew * 0.18)
        draw.ellipse([ex-pw, ey-ih, ex+pw, ey+ih], fill=DARK_WARM)
        # Catchlight
        cl  = int(ew * 0.12)
        draw.ellipse([ex+int(ew*.15)-cl, ey-int(eh*.35)-cl,
                      ex+int(ew*.15)+cl, ey-int(eh*.35)+cl], fill=CREAM)

    # ── Nose ──
    ny = ey + int(hr * 0.40)
    ns = int(s * 0.05)
    draw.polygon([(cx, ny-ns*0.7), (cx-ns, ny+ns*0.5), (cx+ns, ny+ns*0.5)],
                 fill=CRIMSON_B)

    # ── Mouth ──
    mw = int(s * 0.12)
    my = ny + int(s * 0.04)
    draw.arc([cx-mw, my-mw//2, cx,      my+mw//2], start=180, end=270, fill=whisker_col, width=max(1,int(s*.018)))
    draw.arc([cx,    my-mw//2, cx+mw, my+mw//2], start=270, end=360, fill=whisker_col, width=max(1,int(s*.018)))

    # ── Whiskers ──
    wlen = int(hr * 0.88)
    wt   = max(2, int(s * 0.025))
    for sx in [-1, 1]:
        for ang_off in [-0.18, 0.0, 0.18]:
            dx = int(sx * math.cos(ang_off) * wlen)
            dy = int(math.sin(ang_off) * wlen * 0.28)
            ox = cx + sx * int(hr * 0.16)
            draw.line([(ox, ny), (ox+dx, ny+dy)], fill=whisker_col, width=wt)

    # Hat already drawn above in correct z-order (steps 1-3)


# ── Main logo function ─────────────────────────────────────────────────────────

def make_logo(size=512):
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    m    = int(size * 0.038)   # outer margin
    cx, cy = size//2, size//2

    # ── Background gradient ──
    draw_gradient_rect(draw, m, m, size-m, size-m, BG_MID, BG_DARK)

    # ── Corner ornaments ──
    co = int(size * 0.095)
    corners = [(m+co, m+co), (size-m-co, m+co),
               (m+co, size-m-co), (size-m-co, size-m-co)]
    for ccx, ccy in corners:
        corner_ornament(draw, ccx, ccy, int(size*0.048), GOLD, GOLD_B)

    # ── Ornate double border ──
    ornate_border(draw, m, m, size-m, size-m, GOLD, CRIMSON, lw=max(2, int(size*0.012)))

    # ── Load fonts ──
    f_impact   = load_font("Impact.ttf",        int(size*0.115))
    f_title    = load_font("Impact.ttf",        int(size*0.092))
    f_sub      = load_font("ArialBd.ttf",       int(size*0.052))
    f_tiny     = load_font("ArialBd.ttf",       int(size*0.038))
    f_italic   = load_font("Georgiab.ttf",       int(size*0.040))
    f_ital_sm  = load_font("Georgiab.ttf",       int(size*0.032))

    # ── "Andrew's" banner scroll ──
    banner_y  = m + int(size * 0.115)
    banner_h  = int(size * 0.076)
    banner_w  = int(size * 0.62)
    draw_scroll_banner(draw, cx, banner_y, banner_w, banner_h,
                       CRIMSON, GOLD, "Andrew's", f_italic, CREAM)

    # ── Decorative star rows ──
    star_y1 = banner_y + banner_h//2 + int(size * 0.054)
    star_row(draw, cx, star_y1, 7, int(size*0.058), int(size*0.018), GOLD_B)

    # ── Cat illustration ──
    # Positioned so hat clears the star row and collar clears the title text
    cat_cy = cy + int(size * 0.08)
    cat_sz = int(size * 0.44)
    draw_cat(draw, cx, cat_cy, cat_sz, GOLD, BG_DARK, TAN, CRIMSON_B)

    # ── Bottom text block ──
    # "DROP CAT GO"
    title_y = size - m - int(size * 0.25)

    # Shadow pass
    for ox, oy in [(3,3),(2,2),(1,1)]:
        draw.text((cx+ox, title_y+oy), "DROP CAT GO",
                  font=f_impact, fill=(*DARK_WARM, 180), anchor="mm")
    # Coloured pass — gradient effect via two offset layers
    draw.text((cx+1, title_y+1), "DROP CAT GO",
              font=f_impact, fill=CRIMSON, anchor="mm")
    draw.text((cx,   title_y),   "DROP CAT GO",
              font=f_impact, fill=CREAM,   anchor="mm")

    # Divider line with diamond
    div_y  = title_y + int(size * 0.088)
    dw     = int(size * 0.34)
    lw     = max(1, int(size * 0.008))
    draw.line([(cx-dw, div_y), (cx-int(size*.04), div_y)], fill=GOLD, width=lw)
    draw.line([(cx+int(size*.04), div_y), (cx+dw, div_y)], fill=GOLD, width=lw)
    # Diamond
    ds = int(size * 0.022)
    draw.polygon([(cx, div_y-ds), (cx+ds, div_y),
                  (cx, div_y+ds), (cx-ds, div_y)], fill=GOLD_B)

    # "STUDIO"
    studio_y = div_y + int(size * 0.060)
    draw.text((cx+1, studio_y+1), "S T U D I O",
              font=f_sub, fill=(*DARK_WARM, 160), anchor="mm")
    draw.text((cx,   studio_y),   "S T U D I O",
              font=f_sub, fill=GOLD_B, anchor="mm")

    # ── Thin bottom star row ──
    draw.line([(m + int(size*.14), studio_y + int(size*.048)),
               (size-m-int(size*.14), studio_y + int(size*.048))],
              fill=(*GOLD, 60), width=1)

    # ── Apply vignette ──
    img = radial_vignette(img, strength=0.42)

    return img


# ── Render & save ─────────────────────────────────────────────────────────────

print("Rendering logo...")
logo512 = make_logo(512)
logo512.save(os.path.join(STATIC, "logo-512.png"))
print("  logo-512.png")

logo256 = make_logo(256)
logo256.save(os.path.join(STATIC, "logo-256.png"))
print("  logo-256.png")

logo192 = make_logo(192)
logo192.save(os.path.join(STATIC, "logo-192.png"))
print("  logo-192.png")

# Multi-size .ico — 16, 32, 48, 64, 128, 256
ico_sizes = [16, 32, 48, 64, 128, 256]
ico_frames = []
for s in ico_sizes:
    frame = make_logo(max(s, 64))  # render at 64+ for quality then resize
    if s < 64:
        frame = frame.resize((s, s), Image.LANCZOS)
    else:
        frame = frame.resize((s, s), Image.LANCZOS)
    # Convert to RGBA for ico
    ico_frames.append(frame.convert("RGBA"))

ico_frames[0].save(
    os.path.join(STATIC, "favicon.ico"),
    format="ICO",
    append_images=ico_frames[1:],
    sizes=[(s, s) for s in ico_sizes],
)
print("  favicon.ico  (multi-size: 16 32 48 64 128 256)")

# ── Desktop icon: red circle with DCG monogram ──────────────────────────────

def make_dcg_icon(size=256):
    """Simple red circle with white DCG text — for desktop shortcut."""
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    RED  = (180, 20, 30)
    pad  = max(1, size // 32)
    draw.ellipse([pad, pad, size - pad, size - pad], fill=RED)
    font_size = int(size * 0.46)
    font = load_font("Impact.ttf", font_size)
    draw.text((size // 2, size // 2), "DCG", font=font, fill=(255, 255, 255), anchor="mm")
    return img

# Save a high-res source and let Pillow auto-downscale into multi-size ICO
make_dcg_icon(256).save(
    os.path.join(os.path.dirname(STATIC), "dropcat.ico"),
    format="ICO",
    sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print("  dropcat.ico  (red circle DCG — desktop shortcut)")

print("\nDone.")
