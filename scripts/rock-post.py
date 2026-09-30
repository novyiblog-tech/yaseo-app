"""Постобработка рендера камня: свечение трещин и растворение краёв в фон страницы.

    python3 scripts/rock-post.py SRC OUT.webp hero|wide [--no-bloom] [--crop x0,y0,x1,y1]

SRC — рендер Blender (scripts/render-rock.py) или готовая картинка из нейросети:
у неё трещины уже светятся (--no-bloom), а знак генератора в углу срезается --crop.

Нужен Pillow (системный python3). Фон страницы — #0a0a0a, края кадра уходят в него,
чтобы картинка не читалась прямоугольником на тёмной теме.
"""
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter

BG = (10, 10, 10)
src, out, variant = sys.argv[1], sys.argv[2], sys.argv[3]
opts = sys.argv[4:]
im = Image.open(src).convert("RGB")
if "--crop" in opts:
    im = im.crop(tuple(int(v) for v in opts[opts.index("--crop") + 1].split(",")))
w, h = im.size

# bloom: берём только яркое (трещины и их отражения), размываем в двух радиусах и складываем
if "--no-bloom" not in opts:
    bright = im.point(lambda v: 0 if v < 150 else int((v - 150) * 255 / 105))
    halo = ImageChops.add(bright.filter(ImageFilter.GaussianBlur(w * 0.006)),
                          bright.filter(ImageFilter.GaussianBlur(w * 0.022)), scale=1.6)
    im = ImageChops.screen(im, halo)

# маска краёв: мягкий овал; у героя сильнее гасим верх (там стоит карточка) и низ
mask = Image.new("L", (w, h), 0)
d = ImageDraw.Draw(mask)
if variant == "hero":
    d.ellipse((-w * 0.02, h * 0.2, w * 1.02, h * 0.88), fill=255)
else:
    d.ellipse((w * 0.02, -h * 0.25, w * 0.98, h * 0.86), fill=255)
mask = mask.filter(ImageFilter.GaussianBlur(min(w, h) * 0.09))
# плюс линейное гашение у каждой стороны: у самой рамки кадра — ровно фон
edges = Image.new("L", (w, h), 0)
m = int(min(w, h) * 0.2)
ramp = Image.linear_gradient("L").resize((1, m))  # 0 → 255 сверху вниз
col = Image.new("L", (1, h), 255)
col.paste(ramp, (0, 0))
col.paste(ramp.transpose(Image.FLIP_TOP_BOTTOM), (0, h - m))
row = Image.new("L", (w, 1), 255)
row.paste(ramp.rotate(90, expand=True), (0, 0))  # поворот против часовой: слева 0 → 255
row.paste(ramp.rotate(90, expand=True).transpose(Image.FLIP_LEFT_RIGHT), (w - m, 0))
edges = ImageChops.multiply(col.resize((w, h)), row.resize((w, h)))
mask = ImageChops.multiply(mask, edges)
im = Image.composite(im, Image.new("RGB", (w, h), BG), mask)
# чёрное в кадре — это #000, поднимаем до фона страницы
im = ImageChops.lighter(im, Image.new("RGB", (w, h), BG))
im.save(out, "WEBP", quality=82, method=6)
print(out, im.size)
