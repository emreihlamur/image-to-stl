"""Generate a synthetic multi-colour test image."""
import sys
from PIL import Image, ImageDraw
img = Image.new("RGB", (600, 400), (250, 250, 250))
d = ImageDraw.Draw(img)
d.ellipse((40, 40, 360, 360), fill=(220, 40, 30))
d.ellipse((120, 120, 280, 280), fill=(250, 250, 250))
d.ellipse((170, 170, 230, 230), fill=(20, 60, 160))
d.polygon([(400, 350), (480, 60), (560, 350)], fill=(30, 160, 70))
d.rectangle((420, 250, 540, 300), fill=(240, 200, 40))
for k in range(8):  # diagonal pinch pattern
    d.rectangle((380 + k * 10, 20 + k * 10, 389 + k * 10, 29 + k * 10), fill=(20, 60, 160))
img.save(sys.argv[1])
