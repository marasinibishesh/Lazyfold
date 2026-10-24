"""Draw Lazyfold's brand assets from one design:
  packaging/lazyfold.ico   multi-size app icon (16-256 px)
  packaging/lazyfold.icns  macOS app icon (16-1024 px), for the .app bundle
  packaging/lazyfold.png   256 px icon
  packaging/logo-512.png    512 px logo
  packaging/splash.png      splash screen shown while the app starts
Run: python packaging/make_brand.py  (Pillow; Segoe UI from Windows for the wordmark)
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
GREEN_TOP, GREEN_BOTTOM = (64, 140, 100), (30, 86, 60)
CREAM, BACK, FOLD = (246, 236, 220), (226, 209, 180), (205, 184, 150)


def _version() -> str:
    for line in (ROOT / "lazyfold" / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"')
    return ""


def _quad(p0, p1, p2, n: int = 12):
    """Points along a quadratic Bezier (for rounded corners)."""
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1])
            for t in (i / n for i in range(n + 1))]


def _poly(pts, s: float):
    return [(x * s / 100, y * s / 100) for x, y in pts]


def logo(size: int = 1024) -> Image.Image:
    """The mark: a relaxed folder with its corner folded down and a drowsy
    "z z" on a green rounded tile. Drawn on a 100-unit grid; ui.html has the
    same shapes as an SVG <symbol id="logo">."""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / (s - 1)
        gd.line([(0, y), (s, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(GREEN_TOP, GREEN_BOTTOM)) + (255,))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0.03 * s, 0.03 * s, 0.97 * s, 0.97 * s], radius=0.23 * s, fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    k = s / 100
    # Back of the folder with its tab.
    d.rounded_rectangle([20 * k, 26 * k, 46 * k, 40 * k], radius=4 * k, fill=BACK + (255,))
    d.rounded_rectangle([20 * k, 31 * k, 80 * k, 74 * k], radius=6 * k, fill=BACK + (255,))
    # Front panel, its bottom-right corner folded over.
    front = (_quad((18, 44), (18, 38), (24, 38))[:-1] + _quad((76, 38), (82, 38), (82, 44))[:-1]
             + [(82, 62), (66, 78)] + _quad((24, 78), (18, 78), (18, 72)))
    d.polygon(_poly(front, s), fill=CREAM + (255,))
    fold = [(66, 78), (66, 66)] + _quad((66, 66), (66, 62), (70, 62))[1:] + [(82, 62)]
    d.polygon(_poly(fold, s), fill=FOLD + (255,))
    # Drowsy "z z".
    w = max(2, round(3.2 * k))
    for pts in (((58, 15), (68, 15), (58, 25), (68, 25)), ((73, 9), (80, 9), (73, 16), (80, 16))):
        d.line(_poly(pts, s), fill=CREAM + (255,), width=w, joint="curve")
    return img


def _font(name: str, px: int) -> ImageFont.FreeTypeFont:
    for path in (Path("C:/Windows/Fonts") / name, Path("C:/Windows/Fonts/segoeui.ttf")):
        if path.exists():
            return ImageFont.truetype(str(path), px)
    return ImageFont.load_default()


def splash(w: int = 640, h: int = 360) -> Image.Image:
    sc = 2  # draw at 2x, then downscale for smooth edges
    W, H = w * sc, h * sc
    img = Image.new("RGB", (W, H), (250, 249, 246))
    d = ImageDraw.Draw(img)
    # Soft brand glow behind the logo.
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W * 0.06, H * 0.12, W * 0.46, H * 0.88], fill=GREEN_TOP + (38,))
    img.paste(glow.filter(ImageFilter.GaussianBlur(60 * sc)), (0, 0), glow.filter(ImageFilter.GaussianBlur(60 * sc)))
    mark = logo(1024).resize((150 * sc, 150 * sc), Image.LANCZOS)
    img.paste(mark, (int(W * 0.13), int((H - mark.height) / 2) - 10 * sc), mark)
    x = int(W * 0.13) + mark.width + 34 * sc
    d.text((x, H * 0.33), "Lazyfold", font=_font("seguisb.ttf", 46 * sc), fill=(27, 27, 27))
    d.text((x, H * 0.33 + 62 * sc), "Tidy files safely, right on your PC", font=_font("segoeui.ttf", 17 * sc),
           fill=(96, 96, 96))
    d.text((x, H * 0.33 + 96 * sc), "Starting...", font=_font("segoeui.ttf", 13 * sc), fill=(140, 140, 140))
    foot = f"by Bishesh Marasini   ·   version {_version()}"
    d.text((28 * sc, H - 40 * sc), foot, font=_font("segoeui.ttf", 12 * sc), fill=(150, 150, 150))
    d.rectangle([0, H - 6 * sc, W, H], fill=GREEN_BOTTOM)
    return img.resize((w, h), Image.LANCZOS)


def write_icns(big: Image.Image, path: Path = HERE / "lazyfold.icns") -> None:
    """macOS icon: Pillow writes every ICNS size (16-1024 px, with @2x) from
    one 1024 px image. Also runnable on its own, from the committed logo:
    python -c "from make_brand import *; write_icns(Image.open(HERE / 'logo-512.png'))"."""
    big.convert("RGBA").resize((1024, 1024), Image.LANCZOS).save(path, format="ICNS")


if __name__ == "__main__":
    big = logo()
    big.save(HERE / "lazyfold.ico", sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48),
                                            (64, 64), (128, 128), (256, 256)])
    write_icns(big)
    big.resize((256, 256), Image.LANCZOS).save(HERE / "lazyfold.png")
    big.resize((512, 512), Image.LANCZOS).save(HERE / "logo-512.png")
    splash().save(HERE / "splash.png")
    print("wrote icon, logos and splash in", HERE)
