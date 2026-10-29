"""Coin-like test image: circle on white with a ring, text, small shapes."""
import sys
from PIL import Image, ImageDraw, ImageFont
img = Image.new("RGB", (800, 800), "white")
d = ImageDraw.Draw(img)
d.ellipse((40, 40, 760, 760), fill=(250, 210, 195))
d.ellipse((40, 40, 760, 760), outline=(225, 85, 100), width=14)
d.ellipse((150, 300, 380, 560), fill=(195, 90, 60))
d.ellipse((190, 280, 330, 420), fill=(40, 25, 25))
d.polygon([(450, 600), (600, 260), (700, 600)], fill=(60, 45, 45))
try:
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 90)
except OSError:
    font = ImageFont.load_default()
d.text((230, 120), "HELLO", fill=(195, 90, 60), font=font)
for x in range(120, 700, 110):
    d.ellipse((x, 640, x + 40, 680), fill=(225, 85, 100))
img.save(sys.argv[1])
