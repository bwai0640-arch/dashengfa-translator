from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "resources"
SIZE = 256


def rounded_gradient() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gradient = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    pixels = gradient.load()
    for y in range(SIZE):
        ratio = y / (SIZE - 1)
        color = (
            int(73 - 24 * ratio),
            int(118 - 25 * ratio),
            int(235 - 18 * ratio),
            255,
        )
        for x in range(SIZE):
            pixels[x, y] = color
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((8, 8, 248, 248), radius=58, fill=255)
    image.alpha_composite(Image.composite(gradient, Image.new("RGBA", image.size), mask))
    return image


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    image = rounded_gradient()
    draw = ImageDraw.Draw(image)
    font_path = Path("C:/Windows/Fonts/msyhbd.ttc")
    font = ImageFont.truetype(str(font_path), 112)
    draw.text((28, 39), "声", font=font, fill="white", stroke_width=1)

    # A small speaker and two sound waves keep the icon legible in the tray.
    draw.rounded_rectangle((140, 151, 163, 184), radius=4, fill="white")
    draw.polygon([(163, 151), (187, 135), (187, 201), (163, 184)], fill="white")
    draw.arc((170, 144, 216, 192), -50, 50, fill="white", width=8)
    draw.arc((177, 131, 235, 205), -47, 47, fill="white", width=7)

    image.save(OUTPUT / "app_icon.png")
    image.save(
        OUTPUT / "app_icon.ico",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


if __name__ == "__main__":
    main()
