import os
import random

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, 'splash.png')

WIDTH, HEIGHT = 480, 150
INK = '#0f0f13'
TRACK = '#1a1a21'
FOOTAGE = ['#2f6b4a', '#34754f', '#3a8057']
MOTION = '#d1893c'
WORDMARK = '#e4e4ec'
QUIET = '#64646f'

SEGMENTS = [(0.00, 0.17), (0.20, 0.41), (0.44, 0.58), (0.61, 0.86), (0.89, 1.00)]
MARKERS = [0.235, 0.505, 0.72, 0.945]


def font(size, bold=False):
    for name in ('seguisb.ttf' if bold else 'segoeui.ttf', 'arialbd.ttf', 'arial.ttf'):
        path = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', name)
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def tracked(draw, position, text, typeface, fill, tracking):
    x, y = position
    for char in text:
        draw.text((x, y), char, font=typeface, fill=fill, anchor='ls')
        x += draw.textlength(char, font=typeface) + tracking
    return x - tracking


def grain(image, strength=7):
    noise = Image.new('L', image.size)
    noise.putdata([random.gauss(128, strength) for _ in range(image.width * image.height)])
    return Image.blend(image, Image.merge('RGB', (noise, noise, noise)), 0.055)


def main():
    image = Image.new('RGB', (WIDTH, HEIGHT), INK)
    draw = ImageDraw.Draw(image)

    strip_h = 12
    strip_y = HEIGHT - strip_h
    draw.rectangle([0, strip_y, WIDTH, HEIGHT], fill=TRACK)
    for index, (start, end) in enumerate(SEGMENTS):
        draw.rectangle([WIDTH * start, strip_y, WIDTH * end, HEIGHT],
                       fill=FOOTAGE[index % len(FOOTAGE)])
    for position in MARKERS:
        x = WIDTH * position
        draw.rounded_rectangle([x - 1.5, strip_y - 7, x + 1.5, HEIGHT],
                               radius=1.5, fill=MOTION)

    image = grain(image)
    draw = ImageDraw.Draw(image)

    baseline = 78
    tracked(draw, (40, baseline), 'CCTV', font(40, bold=True), WORDMARK, 7)
    draw.text((WIDTH - 40, baseline), 'starting', font=font(13),
              fill=QUIET, anchor='rs')

    image.save(OUT)
    print('wrote %s (%dx%d)' % (OUT, WIDTH, HEIGHT))


if __name__ == '__main__':
    main()
