"""Build the 48-second illustrated product video. Development-only Pillow/ffmpeg.

Run from the repo root: python3 scripts/build_walkthrough.py
The visuals use fictional data and reproduce the product's actual status labels.
No browser account, SMS, payment, external asset or voice service is used.
"""
import math
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'static' / 'media'
OUT.mkdir(parents=True, exist_ok=True)
W, H, FPS, DURATION = 1280, 720, 24, 48
BG, INK, MUTED = '#f4faf7', '#0f172a', '#526474'
GREEN, DARK, LIGHT, LINE = '#00916e', '#077a5c', '#e3f5ee', '#dbe7e2'
FONT = '/usr/share/fonts/opentype/urw-base35/NimbusSans-Regular.otf'
BOLD = '/usr/share/fonts/opentype/urw-base35/NimbusSans-Bold.otf'
fonts = {}


def font(size, bold=False):
    key = (size, bold)
    if key not in fonts:
        fonts[key] = ImageFont.truetype(BOLD if bold else FONT, size)
    return fonts[key]


def txt(im, text, x, y, size=26, color=INK, bold=False):
    ImageDraw.Draw(im).text((x, y), text, font=font(size, bold), fill=color, spacing=9)


def wrap(text, width, size=26, bold=False):
    result, line = [], ''
    for word in text.split():
        trial = (line + ' ' + word).strip()
        if font(size, bold).getlength(trial) > width and line:
            result.append(line); line = word
        else:
            line = trial
    return '\n'.join(result + [line])


def rect(im, box, fill='white', radius=20, outline=None, width=1):
    ImageDraw.Draw(im).rounded_rectangle(box, radius, fill=fill, outline=outline, width=width)


def check(im, x, y, color=GREEN, scale=1):
    ImageDraw.Draw(im).line([(x, y+10*scale), (x+8*scale, y+18*scale), (x+23*scale, y)], fill=color, width=max(3, int(4*scale)))


logo = Image.open(ROOT / 'static' / 'logo-mark.webp').convert('RGBA').crop((330, 350, 1240, 1270)).resize((44, 44), Image.Resampling.LANCZOS)
# Existing brand asset has a white surround: convert just its near-white pixels.
pixels = logo.load()
for y in range(44):
    for x in range(44):
        r, g, b, a = pixels[x, y]
        if min(r, g, b) > 240:
            pixels[x, y] = (r, g, b, 0)


def base(step):
    im = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(im)
    d.ellipse((870, -180, 1500, 620), fill='#eaf6f0')
    im.paste(logo, (56, 34), logo)
    txt(im, 'TuitionPing', 110, 39, 30, bold=True)
    txt(im, 'PRODUCT WALKTHROUGH', 57, 111, 16, DARK, True)
    labels = ['Remind', 'Report', 'Verify']
    for i, label in enumerate(labels):
        x = 750 + i*156
        active = i+1 <= step
        d.ellipse((x, 41, x+26, 67), fill=GREEN if active else '#d7e5dd')
        txt(im, str(i+1), x+8, 45, 16, 'white' if active else MUTED, True)
        txt(im, label, x+36, 45, 20, DARK if active else MUTED, active)
    d.line((56, 656, 1224, 656), fill=LINE, width=1)
    txt(im, 'Illustrated demo · Fictional data · No texts or payments sent', 57, 678, 17, MUTED)
    txt(im, 'tuitionping.com', 1072, 678, 17, DARK, True)
    return im


def pill(im, label, x, y, color=LIGHT, ink=DARK, size=23):
    width = int(font(size, True).getlength(label)) + 34
    rect(im, (x, y, x+width, y+43), color, 21)
    txt(im, label, x+17, y+10, size, ink, True)


def phone(im, reply=False, ack=False, message=True):
    rect(im, (778, 138, 1203, 632), '#e0ebe5', 34)
    rect(im, (767, 127, 1192, 621), INK, 34)
    rect(im, (779, 139, 1180, 609), 'white', 25)
    rect(im, (912, 144, 1048, 159), INK, 8)
    txt(im, 'Sunshine Daycare', 809, 183, 28, bold=True)
    txt(im, 'TuitionPing reminders', 809, 221, 20, MUTED)
    ImageDraw.Draw(im).line((802, 260, 1157, 260), fill=LINE, width=2)
    if message:
        rect(im, (801, 282, 1146, 439), '#eef2f6', 17)
        txt(im, 'TuitionPing', 820, 300, 18, MUTED, True)
        txt(im, 'Hi Sarah, tuition of $1,200\nis due on Nov 1 for\nSunshine Daycare. Thanks!', 820, 331, 24)
    if reply:
        rect(im, (1021, 454, 1157, 509), GREEN, 17)
        txt(im, 'PAID', 1061, 467, 28, 'white', True)
    if ack:
        rect(im, (801, 523, 1146, 586), '#eef2f6', 15)
        txt(im, 'Payment reported. The provider\nwill verify it against their records.', 819, 535, 19)


def dashboard(im, paid=False, checked=False):
    rect(im, (68, 264, 1218, 533), '#e1ece6', 22)
    rect(im, (56, 252, 1206, 521), 'white', 20, LINE)
    txt(im, 'Sunshine Daycare', 84, 276, 30, bold=True)
    txt(im, 'Provider dashboard · Sample family', 84, 316, 20, MUTED)
    ImageDraw.Draw(im).line((84, 355, 1178, 355), fill=LINE, width=1)
    columns = [(85, 'Child'), (291, 'Tuition'), (474, 'Due'), (604, 'Status'), (859, 'Actions')]
    for x, title in columns:
        txt(im, title, x, 375, 19, MUTED, True)
    txt(im, 'Avery', 85, 423, 27, bold=True)
    txt(im, '$1,200', 291, 423, 27)
    txt(im, 'Nov 1', 474, 423, 26)
    pill(im, 'Paid' if paid else 'Reported paid', 603, 415, LIGHT if paid else '#fff0c2', DARK if paid else '#805314', 23)
    if not paid:
        rect(im, (855, 407, 1179, 467), GREEN if checked else 'white', 11, GREEN, 2)
        txt(im, 'Confirm payment received', 871, 427, 23, 'white' if checked else DARK, True)
    else:
        check(im, 866, 429)
        txt(im, 'Payment confirmed', 901, 427, 23, DARK, True)


def cursor(im, x, y):
    points = [(x,y),(x,y+29),(x+8,y+22),(x+15,y+35),(x+21,y+31),(x+14,y+19),(x+27,y+18)]
    ImageDraw.Draw(im).polygon(points, fill=INK, outline='white', width=2)


def scene(index, variant=0):
    im = base(min(index, 3))
    if index == 0:
        txt(im, 'A reminder.\nA reply.\nA verified payment.', 56, 165, 55, bold=True)
        txt(im, 'See the tuition workflow in 48 seconds.', 59, 412, 27, MUTED)
        pill(im, 'Remind → Report → Verify', 58, 475, size=24)
        phone(im)
    elif index == 1:
        txt(im, '01 / REMIND', 58, 166, 22, DARK, True)
        txt(im, 'The reminder\narrives by text.', 56, 208, 56, bold=True)
        txt(im, 'TuitionPing uses your program name,\ntuition amount and due date.', 59, 374, 28, MUTED)
        pill(im, 'No parent app needed', 58, 481)
        phone(im, message=variant > 0)
    elif index == 2:
        txt(im, '02 / REPORT', 58, 166, 22, DARK, True)
        txt(im, 'The parent\nreplies PAID.', 56, 208, 56, bold=True)
        txt(im, 'Their reply flags the payment\nfor your review.', 59, 374, 28, MUTED)
        pill(im, 'Reported paid', 58, 475, '#fff0c2', '#805314')
        txt(im, 'A report still needs verification.', 59, 538, 25, MUTED)
        phone(im, reply=variant > 0, ack=variant > 1)
    elif index == 3:
        txt(im, '03 / VERIFY', 58, 160, 22, DARK, True)
        txt(im, 'Check that the payment arrived.', 56, 199, 48, bold=True)
        dashboard(im)
        rect(im, (58, 553, 1206, 629), LIGHT, 14)
        if variant == 0:
            txt(im, 'First, open the payment records you already use.', 81, 575, 29, DARK, True)
        else:
            check(im, 87, 579)
            txt(im, 'Your payment records: Sarah M. · $1,200 received', 126, 575, 29, DARK, True)
    elif index == 4:
        txt(im, '03 / CONFIRM', 58, 160, 22, DARK, True)
        txt(im, 'Then confirm payment received.', 56, 199, 48, bold=True)
        dashboard(im, paid=variant > 1, checked=variant == 1)
        if variant > 1:
            pill(im, 'Paid · Verified by the provider', 58, 561, size=26)
        else:
            txt(im, 'Use the confirmation button on the family’s row.', 60, 569, 28, MUTED)
    else:
        txt(im, 'Less chasing.\nClearer payment status.', 56, 174, 59, bold=True)
        for i, label in enumerate(['Reminder sent', 'PAID reported', 'Payment verified']):
            x = 57 + i*393
            rect(im, (x, 353, x+365, 439), 'white', 15, LINE)
            check(im, x+22, 384)
            txt(im, label, x+62, 379, 27, DARK, True)
        rect(im, (57, 483, 1223, 580), GREEN, 18)
        txt(im, 'Try it yourself: tuitionping.com/demo', 90, 512, 36, 'white', True)
        txt(im, 'Keep your payment method. TuitionPing handles reminders.', 59, 611, 24, MUTED)
    return im


# Each change is legible for several seconds; short dissolves introduce stages.
keys = [(0,0,0),(4,1,0),(5.0,1,1),(14,2,0),(15.2,2,1),(17.0,2,2),
        (24,3,0),(28,3,1),(36,4,0),(38,4,1),(39,4,2),(43,5,0)]
frames = [scene(index, variant) for _, index, variant in keys]
frames[0].save(OUT / 'tuitionping-walkthrough-poster.webp', quality=88)
qa = ROOT.parent / 'growth-output' / 'walkthrough-qa'
qa.mkdir(parents=True, exist_ok=True)
for n in (0,2,5,7,8,10,11):
    frames[n].save(qa / f'scene-{n:02d}.png')


def render(t):
    k = max(i for i, key in enumerate(keys) if t >= key[0])
    im = frames[k].copy()
    if k and t - keys[k][0] < .4:
        alpha = (t - keys[k][0])/.4
        im = Image.blend(frames[k-1], im, alpha*alpha*(3-2*alpha))
    if 36.8 <= t < 39:
        progress = min(1, max(0, (t - 36.8)/1.1))
        eased = 1-(1-progress)**3
        x, y = 1177-150*eased, 581-140*eased
        if t >= 38:
            radius = int(16 + 25*((t-38) % 1))
            ImageDraw.Draw(im).ellipse((x-radius,y-radius,x+radius,y+radius), outline=GREEN, width=3)
        cursor(im, x, y)
    ImageDraw.Draw(im).rectangle((0, 713, int(W*t/DURATION), 719), fill=GREEN)
    return im


if __name__ == '__main__':
    command = ['ffmpeg','-hide_banner','-loglevel','error','-y',
        '-f','rawvideo','-pix_fmt','rgb24','-s',f'{W}x{H}','-r',str(FPS),'-i','-',
        '-an','-c:v','libx264','-preset','fast','-crf','22','-pix_fmt','yuv420p',
        '-movflags','+faststart',str(OUT/'tuitionping-walkthrough.mp4')]
    with subprocess.Popen(command, stdin=subprocess.PIPE) as process:
        for i in range(FPS*DURATION):
            process.stdin.write(render(i/FPS).tobytes())
        process.stdin.close()
        if process.wait() != 0:
            raise SystemExit('Video encoding failed')
    print(f'Created {DURATION}s / {W}×{H} / H.264 video: {OUT}')
