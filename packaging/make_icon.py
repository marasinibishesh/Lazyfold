"""Draw SlothFile's app icon (a friendly sloth face on a green tile) as a
multi-size .ico. Run: python packaging/make_icon.py"""

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent


def draw(size: int = 1024) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Green rounded tile.
    d.rounded_rectangle([0.04 * s, 0.04 * s, 0.96 * s, 0.96 * s], radius=0.2 * s, fill=(79, 111, 82, 255))
    # Fur (brown head) and light face mask.
    d.ellipse([0.16 * s, 0.18 * s, 0.84 * s, 0.86 * s], fill=(139, 104, 74, 255))
    d.ellipse([0.24 * s, 0.30 * s, 0.76 * s, 0.80 * s], fill=(236, 218, 190, 255))
    # Sleepy eye patches with closed eyes.
    for cx in (0.375, 0.625):
        d.ellipse([(cx - 0.1) * s, 0.42 * s, (cx + 0.1) * s, 0.58 * s], fill=(92, 66, 45, 255))
        d.arc([(cx - 0.055) * s, 0.47 * s, (cx + 0.055) * s, 0.55 * s], start=20, end=160,
              fill=(245, 235, 220, 255), width=max(2, int(0.022 * s)))
    # Nose and smile.
    d.ellipse([0.47 * s, 0.58 * s, 0.53 * s, 0.63 * s], fill=(60, 42, 30, 255))
    d.arc([0.42 * s, 0.58 * s, 0.58 * s, 0.71 * s], start=30, end=150, fill=(60, 42, 30, 255),
          width=max(2, int(0.018 * s)))
    return img


if __name__ == "__main__":
    big = draw()
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    big.save(HERE / "slothfile.ico", sizes=sizes)
    big.resize((256, 256), Image.LANCZOS).save(HERE / "slothfile.png")
    print("wrote", HERE / "slothfile.ico")
