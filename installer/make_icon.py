"""
Generate the Web Watcher app icon (multi-resolution .ico + a .png).

Design: a magnifying glass in the app's accent blue on a dark rounded square — legible from
256px down to 16px. Rendered large with 4x supersampling, then downsampled per icon size for
crisp small versions. Output:
    web_watcher/dashboard/static/icon.ico   (16/24/32/48/64/128/256 — shortcuts, taskbar, favicon)
    web_watcher/dashboard/static/icon.png   (256 — window/webview icon)

Run:  python installer/make_icon.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT   = Path(__file__).resolve().parent.parent
OUTDIR = ROOT / "web_watcher" / "dashboard" / "static"

BG_DARK  = (15, 17, 23, 255)     # #0f1117  app background
BG_TILE  = (26, 32, 48, 255)     # slightly lifted panel so the square reads on dark desktops
ACCENT   = (96, 165, 250, 255)   # #60a5fa  accent blue
ACCENT_HI = (147, 197, 253, 255) # #93c5fd  lighter highlight


def _rounded(size: int, radius: int, fill) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=fill)
    return img


def render(px: int = 1024) -> Image.Image:
    """Render the icon at high resolution (downsample later)."""
    img = _rounded(px, radius=int(px * 0.22), fill=BG_TILE)
    d = ImageDraw.Draw(img)

    # Subtle inner border for definition.
    d.rounded_rectangle([px*0.03, px*0.03, px*0.97, px*0.97],
                        radius=int(px * 0.20), outline=(255, 255, 255, 26),
                        width=max(2, px // 200))

    # Magnifying glass: ring + handle, centered slightly up-left.
    cx, cy = px * 0.44, px * 0.42
    r = px * 0.20
    ring_w = max(4, int(px * 0.075))
    # Glass fill (faint accent tint) then the ring.
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(96, 165, 250, 38))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ACCENT, width=ring_w)
    # Highlight arc on the ring (top-left) for a bit of life.
    d.arc([cx - r, cy - r, cx + r, cy + r], start=170, end=250,
          fill=ACCENT_HI, width=ring_w)

    # Handle: thick rounded line from the ring toward bottom-right.
    hx0 = cx + r * 0.72
    hy0 = cy + r * 0.72
    hx1 = cx + r * 1.75
    hy1 = cy + r * 1.75
    d.line([hx0, hy0, hx1, hy1], fill=ACCENT, width=int(ring_w * 1.35))
    # Rounded caps.
    cap = ring_w * 0.7
    d.ellipse([hx1 - cap, hy1 - cap, hx1 + cap, hy1 + cap], fill=ACCENT)

    return img


def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    base = render(1024)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    icons = [base.resize((s, s), Image.LANCZOS) for s in sizes]
    ico_path = OUTDIR / "icon.ico"
    icons[-1].save(ico_path, format="ICO",
                   sizes=[(s, s) for s in sizes],
                   append_images=icons[:-1])
    png_path = OUTDIR / "icon.png"
    base.resize((256, 256), Image.LANCZOS).save(png_path, format="PNG")
    print(f"wrote {ico_path}  ({ico_path.stat().st_size} bytes)")
    print(f"wrote {png_path}")


if __name__ == "__main__":
    main()
