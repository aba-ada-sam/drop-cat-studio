"""Generate Drop Cat Go Studio icon -- gold paw on dark crimson background.

Builds a proper multi-size ICO manually using PNG frames embedded in the
ICO container (supported since Vista -- works in all modern Windows/browsers).
"""
import io
import struct
from PIL import Image, ImageDraw

BG     = (26, 5, 5, 255)      # #1a0505  near-black maroon
PAD    = (212, 160, 23, 255)  # #d4a017  gold
SIZES  = [16, 24, 32, 48, 64, 128, 256]
SCALE  = 4                    # render at 4x then downsample for antialiasing


def rounded_rect(draw, xy, radius, fill):
    x0, y0, x1, y1 = xy
    r = radius
    draw.rectangle([x0 + r, y0, x1 - r, y1], fill=fill)
    draw.rectangle([x0, y0 + r, x1, y1 - r], fill=fill)
    draw.ellipse([x0,         y0,         x0 + 2*r, y0 + 2*r], fill=fill)
    draw.ellipse([x1 - 2*r,   y0,         x1,       y0 + 2*r], fill=fill)
    draw.ellipse([x0,         y1 - 2*r,   x0 + 2*r, y1      ], fill=fill)
    draw.ellipse([x1 - 2*r,   y1 - 2*r,   x1,       y1      ], fill=fill)


def render_paw(size):
    """Draw a paw print at `size` px using 4x supersampling."""
    s = size * SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # --- background rounded square ---
    margin = int(s * 0.03)
    radius = int(s * 0.18)
    rounded_rect(d, [margin, margin, s - margin - 1, s - margin - 1], radius, BG)

    # --- main pad: large ellipse, lower-center ---
    cx, cy = s * 0.50, s * 0.68
    pw, ph = s * 0.33, s * 0.25
    d.ellipse([cx - pw, cy - ph, cx + pw, cy + ph], fill=PAD)

    # --- four toe pads arcing above the main pad ---
    tr = s * 0.085
    for tx, ty in [
        (s * 0.21, s * 0.36),   # outer-left
        (s * 0.37, s * 0.25),   # inner-left
        (s * 0.63, s * 0.25),   # inner-right
        (s * 0.79, s * 0.36),   # outer-right
    ]:
        d.ellipse([tx - tr, ty - tr, tx + tr, ty + tr], fill=PAD)

    return img.resize((size, size), Image.LANCZOS)


def build_ico(frames):
    """Manually build an ICO binary with PNG-compressed frames."""
    # encode each frame as PNG bytes
    png_bufs = []
    for frame in frames:
        buf = io.BytesIO()
        frame.save(buf, format="PNG")
        png_bufs.append(buf.getvalue())

    count = len(frames)
    # ICO header: 6 bytes
    # directory entries: count * 16 bytes
    dir_offset = 6 + count * 16

    header = struct.pack("<HHH", 0, 1, count)

    entries = b""
    image_data = b""
    current_offset = dir_offset
    for i, (frame, data) in enumerate(zip(frames, png_bufs)):
        w = frame.width  if frame.width  < 256 else 0
        h = frame.height if frame.height < 256 else 0
        size_bytes = len(data)
        entries += struct.pack("<BBBBHHII",
            w, h,     # width, height (0 = 256)
            0,        # color count (0 = no palette)
            0,        # reserved
            1,        # planes
            32,       # bit count
            size_bytes,
            current_offset,
        )
        image_data += data
        current_offset += size_bytes

    return header + entries + image_data


frames = [render_paw(sz) for sz in SIZES]
ico_bytes = build_ico(frames)

for path in ["C:/DropCat-Studio/static/favicon.ico",
             "C:/DropCat-Studio/dropcat.ico"]:
    with open(path, "wb") as f:
        f.write(ico_bytes)
    print(f"Wrote {path}  ({len(ico_bytes):,} bytes, {len(frames)} frames)")

# PNG preview at 256 for quick visual check
frames[-1].save("C:/DropCat-Studio/tools/icon_preview_256.png")
print("Wrote tools/icon_preview_256.png")
