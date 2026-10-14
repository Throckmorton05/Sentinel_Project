#!/usr/bin/env python3
"""Recreate Plymouth PNGs from the existing Sentinel SVG (developer-only)."""
from pathlib import Path
from io import BytesIO
import cairosvg
from PIL import Image, ImageDraw, ImageFilter, ImageFont

project = Path(__file__).resolve().parents[2]
output = project / 'assets/Boot'
output.mkdir(parents=True, exist_ok=True)
scale = 3
shield = Image.open(BytesIO(cairosvg.svg2png(
    url=str(project / 'assets/Sentinel_Shield_Minimal_Solid-Visor.svg'),
    output_width=120 * scale, output_height=114 * scale))).convert('RGBA')
# Reuse the exact vector silhouette; only this boot asset receives the blue glow.
mask = Image.new('L', (360 * scale, 190 * scale))
mask.paste(shield.getchannel('A'), (120 * scale, 20 * scale))
watermark = Image.new('RGBA', mask.size)
for radius, opacity in ((12, .45), (5, .70)):
    layer = Image.new('RGBA', mask.size, (98, 185, 255, 0))
    layer.putalpha(mask.filter(ImageFilter.GaussianBlur(radius * scale)).point(lambda a: int(a * opacity)))
    watermark.alpha_composite(layer)
light = Image.new('RGBA', mask.size, (151, 216, 255, 0))
light.putalpha(mask)
watermark.alpha_composite(light)
draw = ImageDraw.Draw(watermark)
font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 29 * scale)
draw.text((180 * scale, 159 * scale), 'Sentinel', font=font, fill='white', anchor='mm')
watermark.resize((360, 190), Image.Resampling.LANCZOS).save(output / 'watermark.png')
# Both names are used by different upstream two-step theme versions.
for frame in range(60):
    image = Image.new('RGBA', (128, 128))
    draw = ImageDraw.Draw(image)
    for segment in range(12):
        start = frame * 6 + segment * 30
        draw.arc((26, 26, 102, 102), start, start + 16,
                 fill=(151, 216, 255, 35 + segment * 20), width=6)
    image = image.resize((32, 32), Image.Resampling.LANCZOS)
    image.save(output / f'animation-{frame + 1:04}.png')
    image.save(output / f'throbber-{frame + 1:04}.png')
print(output)
