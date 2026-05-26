"""Promo stage matching the Theodosia docs dark theme: Rose Pine (main) palette,
Inria Serif display + Funnel Sans body (the docs fonts), the terminal floating
on the right in a Rose Pine surface card. Prints the overlay rect for ffmpeg.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1920, 1080
# Rose Pine (main) palette, the docs' dark flavor.
BASE_TOP, BASE_BOT = "#1c1a2a", "#13121c"
SURFACE = (31, 29, 46)  # #1f1d2e card fill
TEXT = "#e0def4"
SUBTLE = "#908caa"
MUTED = "#6e6a86"
IRIS = "#c4a7e7"
GOLD = "#f6c177"
BORDER = "#403d52"

F = "/tmp/fonts"
f_word = ImageFont.truetype(f"{F}/InriaSerif-Bold.ttf", 82)
f_tag = ImageFont.truetype(f"{F}/FunnelSans.ttf", 31)
f_body = ImageFont.truetype(f"{F}/FunnelSans.ttf", 25)
f_foot = ImageFont.truetype(f"{F}/FunnelSans.ttf", 23)

# Background gradient (base tones).
img = Image.new("RGB", (W, H), BASE_BOT)
t = tuple(int(BASE_TOP[i : i + 2], 16) for i in (1, 3, 5))
b = tuple(int(BASE_BOT[i : i + 2], 16) for i in (1, 3, 5))
px = img.load()
for y in range(H):
    r = y / (H - 1)
    c = tuple(int(t[i] + (b[i] - t[i]) * r) for i in range(3))
    for x in range(W):
        px[x, y] = c
img = img.convert("RGBA")

# Terminal slot: native 1080x760 on the right.
TX, TY, TW, TH = 762, 160, 1080, 760
pad = 16
cx, cy, cw, ch = TX - pad, TY - pad, TW + 2 * pad, TH + 2 * pad
radius = 22

shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
ImageDraw.Draw(shadow).rounded_rectangle(
    [cx, cy + 18, cx + cw, cy + ch + 18], radius=radius + 6, fill=(0, 0, 0, 170)
)
img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(30)))
ImageDraw.Draw(img).rounded_rectangle(
    [cx, cy, cx + cw, cy + ch], radius=radius, fill=SURFACE + (255,), outline=BORDER, width=2
)

d = ImageDraw.Draw(img)
LX = 96
d.text((LX, 280), "Theodosia", font=f_word, fill=TEXT)
ww = d.textlength("Theodosia", font=f_word)
d.rectangle([LX, 398, LX + ww, 403], fill=GOLD)
d.text((LX, 424), "Put an AI agent on rails.", font=f_tag, fill=IRIS)

for i, line in enumerate(
    [
        "The agent can only take the next",
        "allowed step. Every step is recorded",
        "to a tamper-evident log you can verify.",
    ]
):
    d.text((LX, 500 + i * 37), line, font=f_body, fill=SUBTLE)

d.text((LX, 654), "pip install theodosia", font=f_foot, fill=GOLD)
d.text((LX, 692), "github.com/msradam/theodosia", font=f_foot, fill=MUTED)

img.convert("RGB").save("/tmp/stage.png")
print(f"OVERLAY {TX} {TY} {TW} {TH}")

# Build:
#   fonts: Inria Serif + Funnel Sans TTFs from github.com/google/fonts -> /tmp/fonts
#   python3 demos/promo_stage.py            # -> /tmp/stage.png
#   ffmpeg -y -loop 1 -t 29 -i /tmp/stage.png -i demos/hero_video.mp4 \
#     -filter_complex "[0:v][1:v]overlay=762:160[v]" -map "[v]" -r 30 \
#     -c:v libx264 -pix_fmt yuv420p -movflags +faststart demos/hero_promo.mp4
