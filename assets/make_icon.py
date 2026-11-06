"""Barnraise listing icon: a timber barn frame, cream on barn red. 1024x1024, full bleed."""
import sys

from PIL import Image, ImageDraw

S = 4  # supersampling factor for clean edges
SIZE = 1024
RED, CREAM, SHADOW = (158, 43, 37), (246, 236, 220), (118, 30, 26)
STROKE = 52


def p(x: float, y: float) -> tuple[int, int]:
    return (round(x * S), round(y * S))


def bar(d: ImageDraw.ImageDraw, x: float, top: float, bottom: float) -> None:
    """A vertical timber with rounded ends."""
    r = STROKE / 2
    d.rounded_rectangle([p(x - r, top - r), p(x + r, bottom + r)], radius=r * S, fill=CREAM)


img = Image.new("RGB", (SIZE * S, SIZE * S), RED)
d = ImageDraw.Draw(img)

base, eave = 780, 520
left, right, mid = 236, 788, 512
d.rounded_rectangle([p(170, base + 34), p(854, base + 78)], radius=22 * S, fill=SHADOW)

# Gambrel roof: eave, roof break, ridge, roof break, eave.
roof = [p(left, eave), p(320, 338), p(mid, 228), p(704, 338), p(right, eave)]
d.line(roof, fill=CREAM, width=STROKE * S, joint="curve")
for x, y in (roof[0], roof[-1]):  # round the roof's ends where it meets the walls
    r = STROKE / 2 * S
    d.ellipse([x - r, y - r, x + r, y + r], fill=CREAM)

# Tie beam across the eaves.
d.line([p(left, eave), p(right, eave)], fill=CREAM, width=STROKE * S)

# Walls and posts, raised side by side.
bar(d, left, eave + 6, base)
bar(d, right, eave + 6, base)
for x in (374, 650):
    bar(d, x, eave + 6, base)  # tops hidden inside the tie beam
bar(d, mid, 228, base)  # king post up to the ridge

img = img.resize((SIZE, SIZE), Image.LANCZOS)
img.save(sys.argv[1], optimize=True)
img.resize((128, 128), Image.LANCZOS).save(sys.argv[1].replace(".png", "-128.png"))
img.resize((48, 48), Image.LANCZOS).save(sys.argv[1].replace(".png", "-48.png"))
