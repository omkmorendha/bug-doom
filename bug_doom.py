#!/usr/bin/env python3
"""BUG DOOM — a textured terminal raycasting shooter. Squash the bugs.

Renders true-pixel graphics in the terminal using Unicode half-blocks (▀):
every character cell is two vertically stacked RGB pixels, drawn with ANSI
truecolor escapes (256-color fallback). No dependencies — stdlib only.

Controls:
    W / S  or  UP / DOWN    move forward / back
    A / D                   strafe left / right
    LEFT / RIGHT            turn
    MOUSE                   aim (move), fire (left click / drag),
                            walk (scroll wheel)
    SPACE                   fire
    Q                       quit

Run:  python3 bug_doom.py
"""

import math
import os
import random
import re
import select
import shutil
import sys
import termios
import time
import tty

TAU = math.tau
FOV = math.pi / 3
HALF_FOV_TAN = math.tan(FOV / 2)
MAX_DEPTH = 24.0
FOG_DIST = 13.0
FIRE_COOLDOWN = 0.22
MUZZLE_TIME = 0.09
PLAYER_START = (16.5, 9.5, 0.0)
TARGET_FPS = 30
MOUSE_SENS = 0.05  # radians per terminal column of mouse travel

# Wall cells: '#' brick, '%' tech panel, '&' mossy stone. '.' is floor.
MAP = [
    "################################",
    "#..............#...............#",
    "#..%%%%........#......&&&&.....#",
    "#..%...........#......&........#",
    "#..%...%%%%....#......&....##..#",
    "#..............#...........##..#",
    "#.......#......#...............#",
    "#.......#......%%%%....&&&&&...#",
    "#.......#..................&...#",
    "#.......#...................&..#",
    "#..&&&&.....................&..#",
    "#..&........%%%%............&..#",
    "#..&...........%......##....&..#",
    "#..&...........%......##.......#",
    "#......###.....%...............#",
    "#......#.......%.....%%%%%%....#",
    "#......#.............%.........#",
    "#..........&&........%.....##..#",
    "#..........&&..................#",
    "################################",
]
assert all(len(r) == len(MAP[0]) for r in MAP), "map rows must be equal width"
MAP_W, MAP_H = len(MAP[0]), len(MAP)

MILESTONES = {
    10: ("PEST CONTROL!", "10 BUGS SQUASHED"),
    20: ("EXTERMINATOR!", "20 BUGS FLATTENED"),
    50: ("DEBUG MASTER!", "50 BUGS OBLITERATED"),
    100: ("CODE IS CLEAN", "100 BUGS DESTROYED"),
}


def is_wall(x, y):
    xi, yi = int(x), int(y)
    if 0 <= yi < MAP_H and 0 <= xi < MAP_W:
        return MAP[yi][xi] != "."
    return True


def cast_ray(px, py, ang):
    """DDA raycast -> (distance, side, wall_char). side=1 is a y-facing wall."""
    cos, sin = math.cos(ang), math.sin(ang)
    map_x, map_y = int(px), int(py)
    delta_x = abs(1.0 / cos) if cos else 1e30
    delta_y = abs(1.0 / sin) if sin else 1e30
    if cos < 0:
        step_x, side_x = -1, (px - map_x) * delta_x
    else:
        step_x, side_x = 1, (map_x + 1.0 - px) * delta_x
    if sin < 0:
        step_y, side_y = -1, (py - map_y) * delta_y
    else:
        step_y, side_y = 1, (map_y + 1.0 - py) * delta_y
    side = 0
    for _ in range(96):
        if side_x < side_y:
            side_x += delta_x
            map_x += step_x
            side = 0
        else:
            side_y += delta_y
            map_y += step_y
            side = 1
        if 0 <= map_y < MAP_H and 0 <= map_x < MAP_W:
            ch = MAP[map_y][map_x]
            if ch != ".":
                dist = (side_x - delta_x) if side == 0 else (side_y - delta_y)
                return max(dist, 0.01), side, ch
        else:
            break
    return MAX_DEPTH, 0, "#"


def angle_diff(a, b):
    return (a - b + math.pi) % TAU - math.pi


# --------------------------------------------------------------------------
# Colors & textures (packed 0xRRGGBB ints)
# --------------------------------------------------------------------------

TEX_SIZE = 32
SHADE_LEVELS = 32
_texrng = random.Random(7)
_NOISE = [[_texrng.uniform(-1, 1) for _ in range(TEX_SIZE)] for _ in range(TEX_SIZE)]


def pack(r, g, b):
    return (max(0, min(255, int(r))) << 16) | (max(0, min(255, int(g))) << 8) | max(0, min(255, int(b)))


def scale_color(c, f):
    return pack(((c >> 16) & 255) * f, ((c >> 8) & 255) * f, (c & 255) * f)


def tex_brick(x, y):
    off = 16 if (y // 8) % 2 else 0
    if y % 8 == 0 or (x + off) % 16 == 0:
        return pack(72, 66, 58)
    n = _NOISE[y][x] * 14
    return pack(150 + n, 62 + n * 0.5, 46 + n * 0.4)


def tex_tech(x, y):
    if x % 16 == 0 or y % 16 == 0:
        return pack(38, 44, 56)
    if (x % 16 in (2, 13)) and (y % 16 in (2, 13)):
        return pack(170, 180, 200)
    if 13 <= y <= 15:
        return pack(70, 215, 175)
    n = _NOISE[y][x] * 8
    return pack(76 + n, 86 + n, 102 + n)


def tex_moss(x, y):
    if x % 16 == 0 or y % 16 == 0:
        return pack(58, 56, 48)
    n = _NOISE[y][x]
    if n > 0.35 or _NOISE[(y + 11) % TEX_SIZE][(x + 7) % TEX_SIZE] > 0.55:
        return pack(66 + n * 10, 126 + n * 14, 52)
    return pack(116 + n * 12, 110 + n * 12, 98 + n * 10)


def tex_floor(x, y):
    if x % 16 == 0 or y % 16 == 0:
        return pack(26, 28, 28)
    n = _NOISE[y][x] * 6
    base = 58 if ((x // 16) + (y // 16)) % 2 else 42
    return pack(base + n, base + n - 2, base + n - 6)


def build_shaded_texture(fn):
    """[level][ (ty<<5) | tx ] -> packed color, level 0 = darkest."""
    base = [fn(x, y) for y in range(TEX_SIZE) for x in range(TEX_SIZE)]
    levels = []
    for lvl in range(SHADE_LEVELS):
        f = 0.04 + 0.96 * (lvl / (SHADE_LEVELS - 1)) ** 1.25
        levels.append([scale_color(c, f) for c in base])
    return levels


WALL_TEX = {
    "#": build_shaded_texture(tex_brick),
    "%": build_shaded_texture(tex_tech),
    "&": build_shaded_texture(tex_moss),
}
FLOOR_TEX = build_shaded_texture(tex_floor)


def shade_level(dist):
    return max(0, min(SHADE_LEVELS - 1, int((SHADE_LEVELS - 1) * (1.0 - dist / FOG_DIST))))


# --------------------------------------------------------------------------
# Sprites (pixel art)
# --------------------------------------------------------------------------

BUG_FRAMES_ART = [
    [
        ".r...........r.",
        "..r.........r..",
        "...kkkkkkkkk...",
        "..khhhhhhhhhk..",
        ".kggRRgggRRggk.",
        ".kggRRgggRRggk.",
        "lkgggggggggggkl",
        ".lkgggggggggkl.",
        "..lkkkkkkkkkl..",
        ".l..w..k..w..l.",
        "l....w...w....l",
    ],
    [
        ".r...........r.",
        "..r.........r..",
        "...kkkkkkkkk...",
        "..khhhhhhhhhk..",
        ".kggRRgggRRggk.",
        ".kggRRgggRRggk.",
        ".lkgggggggggkl.",
        "l.lkgggggggkl.l",
        ".l.kkkkkkkkk.l.",
        "..l.w..k..w.l..",
        "....w.....w....",
    ],
]

BUG_PALETTE = {
    "k": (15, 32, 12), "g": (72, 172, 58), "h": (108, 212, 88),
    "R": (242, 44, 38), "r": (40, 80, 30), "l": (28, 56, 22),
    "w": (228, 228, 198),
}
TANK_PALETTE = {
    "k": (28, 12, 40), "g": (148, 62, 200), "h": (188, 110, 236),
    "R": (255, 214, 40), "r": (70, 34, 95), "l": (56, 26, 78),
    "w": (228, 228, 198),
}


def build_sprite(art, palette):
    """[level][row][col] -> packed color or None (transparent)."""
    base = []
    for line in art:
        row = []
        for ch in line:
            row.append(pack(*palette[ch]) if ch in palette else None)
        base.append(row)
    levels = []
    for lvl in range(SHADE_LEVELS):
        f = 0.10 + 0.90 * (lvl / (SHADE_LEVELS - 1)) ** 1.1
        levels.append([[scale_color(c, f) if c is not None else None for c in row] for row in base])
    return levels


BUG_SPRITES = [build_sprite(a, BUG_PALETTE) for a in BUG_FRAMES_ART]
TANK_SPRITES = [build_sprite(a, TANK_PALETTE) for a in BUG_FRAMES_ART]
FLASH_SPRITES = [
    [[[pack(255, 90, 60) if c is not None else None for c in row] for row in lvls[SHADE_LEVELS - 1]]] * SHADE_LEVELS
    for lvls in BUG_SPRITES
]

GUN_PALETTE = {
    "k": (12, 12, 14), "g": (104, 110, 122), "l": (165, 172, 184),
    "d": (54, 57, 64), "b": (122, 82, 42), "y": (255, 196, 32),
    "Y": (255, 238, 120), "W": (255, 255, 255),
}
GUN_ART = [
    "......kkkkk......",
    "......kgggk......",
    "......kglgk......",
    "......kglgk......",
    ".....kkglgkk.....",
    ".....kdglgdk.....",
    "....kkdgggdkk....",
    "....kbbbbbbbk....",
    "....kbkbbbkbk....",
    "...kddddddddkk...",
    "...kdddddddddk...",
    "..kdddddddddddk..",
    ".kdddddddddddddk.",
]
GUN_FIRE_ART = [
    ".......yYWYy.....",
    ".....yYWWWWWYy...",
    "....yYWWWWWWWYy..",
    ".....yYWWWWWYy...",
    ".....kkYWWYkk....",
    ".....kdglgdk.....",
    "....kkdgggdkk....",
    "....kbbbbbbbk....",
    "....kbkbbbkbk....",
    "...kddddddddkk...",
    "...kdddddddddk...",
    "..kdddddddddddk..",
    ".kdddddddddddddk.",
]


def build_overlay(art, palette):
    return [[pack(*palette[ch]) if ch in palette else None for ch in line] for line in art]


GUN_SPRITE = build_overlay(GUN_ART, GUN_PALETTE)
GUN_FIRE_SPRITE = build_overlay(GUN_FIRE_ART, GUN_PALETTE)


# --------------------------------------------------------------------------
# 5x5 bitmap font for big milestone text
# --------------------------------------------------------------------------

FONT = {
    "A": (0b01110, 0b10001, 0b11111, 0b10001, 0b10001),
    "B": (0b11110, 0b10001, 0b11110, 0b10001, 0b11110),
    "C": (0b01111, 0b10000, 0b10000, 0b10000, 0b01111),
    "D": (0b11110, 0b10001, 0b10001, 0b10001, 0b11110),
    "E": (0b11111, 0b10000, 0b11110, 0b10000, 0b11111),
    "F": (0b11111, 0b10000, 0b11110, 0b10000, 0b10000),
    "G": (0b01111, 0b10000, 0b10011, 0b10001, 0b01111),
    "H": (0b10001, 0b10001, 0b11111, 0b10001, 0b10001),
    "I": (0b11111, 0b00100, 0b00100, 0b00100, 0b11111),
    "J": (0b00111, 0b00010, 0b00010, 0b10010, 0b01100),
    "K": (0b10010, 0b10100, 0b11000, 0b10100, 0b10010),
    "L": (0b10000, 0b10000, 0b10000, 0b10000, 0b11111),
    "M": (0b10001, 0b11011, 0b10101, 0b10001, 0b10001),
    "N": (0b10001, 0b11001, 0b10101, 0b10011, 0b10001),
    "O": (0b01110, 0b10001, 0b10001, 0b10001, 0b01110),
    "P": (0b11110, 0b10001, 0b11110, 0b10000, 0b10000),
    "Q": (0b01110, 0b10001, 0b10101, 0b10010, 0b01101),
    "R": (0b11110, 0b10001, 0b11110, 0b10100, 0b10010),
    "S": (0b01111, 0b10000, 0b01110, 0b00001, 0b11110),
    "T": (0b11111, 0b00100, 0b00100, 0b00100, 0b00100),
    "U": (0b10001, 0b10001, 0b10001, 0b10001, 0b01110),
    "V": (0b10001, 0b10001, 0b10001, 0b01010, 0b00100),
    "W": (0b10001, 0b10001, 0b10101, 0b11011, 0b10001),
    "X": (0b10001, 0b01010, 0b00100, 0b01010, 0b10001),
    "Y": (0b10001, 0b01010, 0b00100, 0b00100, 0b00100),
    "Z": (0b11111, 0b00010, 0b00100, 0b01000, 0b11111),
    "0": (0b01110, 0b10011, 0b10101, 0b11001, 0b01110),
    "1": (0b00100, 0b01100, 0b00100, 0b00100, 0b01110),
    "2": (0b01110, 0b10001, 0b00110, 0b01000, 0b11111),
    "3": (0b11110, 0b00001, 0b00110, 0b00001, 0b11110),
    "4": (0b10010, 0b10010, 0b11111, 0b00010, 0b00010),
    "5": (0b11111, 0b10000, 0b11110, 0b00001, 0b11110),
    "6": (0b01110, 0b10000, 0b11110, 0b10001, 0b01110),
    "7": (0b11111, 0b00010, 0b00100, 0b01000, 0b01000),
    "8": (0b01110, 0b10001, 0b01110, 0b10001, 0b01110),
    "9": (0b01110, 0b10001, 0b01111, 0b00001, 0b01110),
    "!": (0b00100, 0b00100, 0b00100, 0b00000, 0b00100),
    "*": (0b10101, 0b01110, 0b11111, 0b01110, 0b10101),
    "-": (0b00000, 0b00000, 0b01110, 0b00000, 0b00000),
    ".": (0b00000, 0b00000, 0b00000, 0b00000, 0b00100),
    " ": (0, 0, 0, 0, 0),
}


def draw_text(fb, fb_w, fb_h, text, cy, scale, color):
    text = text.upper()
    width = len(text) * 6 * scale - scale
    x0 = (fb_w - width) // 2
    shadow = pack(8, 8, 12)
    for pass_color, ox, oy in ((shadow, scale, scale), (color, 0, 0)):
        for i, ch in enumerate(text):
            glyph = FONT.get(ch)
            if not glyph:
                continue
            gx = x0 + i * 6 * scale + ox
            for ry in range(5):
                bits = glyph[ry]
                if not bits:
                    continue
                for rx in range(5):
                    if bits & (1 << (4 - rx)):
                        for dy in range(scale):
                            yy = cy + ry * scale + dy + oy
                            if 0 <= yy < fb_h:
                                row = fb[yy]
                                for dx in range(scale):
                                    xx = gx + rx * scale + dx
                                    if 0 <= xx < fb_w:
                                        row[xx] = pass_color


# --------------------------------------------------------------------------
# Game logic
# --------------------------------------------------------------------------

class Bug:
    def __init__(self, x, y, tank=False):
        self.x, self.y = x, y
        self.tank = tank
        self.hp = 3 if tank else 1
        self.phase = random.uniform(0, TAU)
        self.bite_cd = 0.0
        self.flash = 0.0


class Game:
    def __init__(self):
        self.px, self.py, self.pa = PLAYER_START
        self.hp = 100.0
        self.score = 0
        self.bugs = []
        self.particles = []  # [x, y, vx, vy, life, color]
        self.spawn_timer = 0.0
        self.last_shot = -1.0
        self.muzzle = 0.0
        self.bite_flash = 0.0
        self.last_bite = -10.0
        self.bob = 0.0
        self.celebrated = set()

    def try_move(self, nx, ny):
        r = 0.2
        if not (is_wall(nx + r, self.py) or is_wall(nx - r, self.py)):
            self.px = nx
        if not (is_wall(self.px, ny + r) or is_wall(self.px, ny - r)):
            self.py = ny

    def walk(self, forward, strafe):
        ang = self.pa
        dx = math.cos(ang) * forward + math.cos(ang + math.pi / 2) * strafe
        dy = math.sin(ang) * forward + math.sin(ang + math.pi / 2) * strafe
        self.try_move(self.px + dx, self.py + dy)
        self.bob += 0.5

    def spawn_bug(self):
        for _ in range(40):
            x = random.uniform(1.5, MAP_W - 1.5)
            y = random.uniform(1.5, MAP_H - 1.5)
            if is_wall(x, y):
                continue
            if math.hypot(x - self.px, y - self.py) < 6:
                continue
            tank = self.score >= 25 and random.random() < 0.25
            self.bugs.append(Bug(x, y, tank))
            return

    def update_bugs(self, dt, now):
        speed = 1.0 + min(self.score * 0.015, 1.5)
        for b in self.bugs:
            b.bite_cd = max(0.0, b.bite_cd - dt)
            b.flash = max(0.0, b.flash - dt)
            dx, dy = self.px - b.x, self.py - b.y
            dist = math.hypot(dx, dy)
            ang = math.atan2(dy, dx)
            if dist > 2.5:
                ang += math.sin(now * 2 + b.phase) * 0.6
            if dist > 0.5:  # stop at biting range so bugs stay shootable
                spd = speed * (0.7 if b.tank else 1.0) * dt
                nx, ny = b.x + math.cos(ang) * spd, b.y + math.sin(ang) * spd
                if not is_wall(nx, b.y):
                    b.x = nx
                if not is_wall(b.x, ny):
                    b.y = ny
            if dist < 0.75 and b.bite_cd <= 0:
                self.hp -= 8
                b.bite_cd = 1.0
                self.bite_flash = 0.3
                self.last_bite = now

    def shoot(self, now):
        """Returns (killed_bug, dist) — (None, None) on a miss or non-lethal hit."""
        self.last_shot = now
        self.muzzle = MUZZLE_TIME
        best = None
        for b in self.bugs:
            dx, dy = b.x - self.px, b.y - self.py
            dist = math.hypot(dx, dy)
            if dist > 18 or dist < 0.1:
                continue
            ang = math.atan2(dy, dx)
            if abs(angle_diff(ang, self.pa)) > max(math.atan2(0.45, dist), 0.04):
                continue
            wall_d, _, _ = cast_ray(self.px, self.py, ang)
            if wall_d < dist - 0.2:
                continue
            if best is None or dist < best[0]:
                best = (dist, b)
        if best:
            bug = best[1]
            bug.hp -= 1
            bug.flash = 0.15
            if bug.hp <= 0:
                self.bugs.remove(bug)
                self.score += 1
                return bug, best[0]
        return None, None

    def next_milestone(self):
        for m in sorted(MILESTONES):
            if self.score < m:
                return m
        return ((self.score // 100) + 1) * 100


# --------------------------------------------------------------------------
# Terminal I/O
# --------------------------------------------------------------------------

ARROW_KEYS = {0x41: "UP", 0x42: "DOWN", 0x43: "RIGHT", 0x44: "LEFT"}
# SGR mouse report: ESC [ < code ; col ; row M (press/motion) or m (release)
MOUSE_RE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
# any other CSI sequence (modified arrows like ESC[1;2D, function keys, ...)
CSI_RE = re.compile(rb"\x1b\[[0-9;<=>?]*[ -/]*[@-~]")
PARTIAL_ESC_RE = re.compile(rb"\x1b(\[|O)?[0-9;<=>?]*$")


def parse_input(buf):
    """Parse raw stdin bytes -> (events, leftover_bytes).

    Events are key strings ('w', ' ', 'LEFT', ...) or mouse tuples:
    ('move', col, row, button_bits), ('click', col, row), ('scroll', +1/-1).
    Unrecognized escape sequences are swallowed whole — their bytes must
    never fall through as literal keypresses.
    """
    events = []
    i, n = 0, len(buf)
    while i < n:
        b = buf[i]
        if b == 0x1B:
            m = MOUSE_RE.match(buf, i)
            if m:
                code, x, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if code & 64:
                    events.append(("scroll", 1 if (code & 1) == 0 else -1))
                elif code & 32:
                    events.append(("move", x, y, code & 3))
                elif m.group(4) == b"M" and (code & 3) == 0:
                    events.append(("click", x, y))
                i = m.end()
                continue
            # plain (ESC [ X) and application-mode (ESC O X) arrows
            if i + 2 < n and buf[i + 1] in (0x5B, 0x4F) and buf[i + 2] in ARROW_KEYS:
                events.append(ARROW_KEYS[buf[i + 2]])
                i += 3
                continue
            m = CSI_RE.match(buf, i)
            if m:  # some other escape sequence — discard it entirely
                i = m.end()
                continue
            # possibly a sequence split across reads — keep for the next one
            if n - i < 24 and PARTIAL_ESC_RE.match(buf, i):
                break
            i += 1
            continue
        events.append(chr(b).lower())
        i += 1
    return events, buf[i:]


class Term:
    def __enter__(self):
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        self.buf = b""
        self.stall = 0
        tty.setcbreak(self.fd)
        # alt screen, hide cursor, no autowrap, SGR any-motion mouse tracking
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[?1003h\x1b[?1006h\x1b[2J")
        sys.stdout.flush()
        return self

    def __exit__(self, *exc):
        sys.stdout.write("\x1b[?1006l\x1b[?1003l\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        # a key still held at quit keeps repeating into the tty; keep
        # swallowing input until the keyboard goes quiet so nothing leaks
        # into the shell after exit
        deadline = time.time() + 1.0
        last_input = time.time()
        while time.time() < deadline and time.time() - last_input < 0.15:
            if select.select([self.fd], [], [], 0.05)[0]:
                try:
                    if os.read(self.fd, 4096):
                        last_input = time.time()
                except OSError:
                    break
        termios.tcflush(self.fd, termios.TCIFLUSH)
        termios.tcsetattr(self.fd, termios.TCSAFLUSH, self.old)

    def read_keys(self):
        got_new = False
        while select.select([self.fd], [], [], 0)[0]:
            data = os.read(self.fd, 1024)
            if not data:
                break
            self.buf += data
            got_new = True
        # a retained partial escape sequence that never completes (e.g. a
        # bare ESC keypress) must not wedge the parser: drop it after a few
        # frames with no continuation bytes
        if self.buf and not got_new:
            self.stall += 1
            if self.stall >= 3:
                self.buf = self.buf[1:]
                self.stall = 0
        else:
            self.stall = 0
        events, self.buf = parse_input(self.buf)
        return events


# --------------------------------------------------------------------------
# Renderer
# --------------------------------------------------------------------------

TRUECOLOR = any(t in os.environ.get("COLORTERM", "").lower() for t in ("truecolor", "24bit"))


def to_256(r, g, b):
    if abs(r - g) < 12 and abs(g - b) < 12:
        v = (r + g + b) // 3
        if v < 8:
            return 16
        if v > 238:
            return 231
        return 232 + (v - 8) // 10
    return 16 + 36 * (r * 6 // 256) + 6 * (g * 6 // 256) + (b * 6 // 256)


class Renderer:
    def __init__(self):
        self.cols = self.rows = 0
        self.fb_w = self.fb_h = 0
        self.pad = ""
        self._cell_cache = {}
        self._fg_cache = {}

    def resize(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.fb_w = min(cols, 180)
        self.fb_h = min(2 * (rows - 2), 130)
        self.pad = " " * max(0, (cols - self.fb_w) // 2)
        half = self.fb_h // 2
        # ceiling gradient (normal + muzzle-lit variants)
        self.ceil_rows, self.ceil_rows_lit = [], []
        for y in range(half):
            t = y / max(1, half - 1)
            c = pack(8 + 14 * t, 10 + 16 * t, 16 + 20 * t)
            self.ceil_rows.append([c] * self.fb_w)
            self.ceil_rows_lit.append([pack(24 + 20 * t, 24 + 22 * t, 28 + 24 * t)] * self.fb_w)
        # per-row floor distance and shade level
        self.row_dist, self.row_lvl = [0.0] * self.fb_h, [0] * self.fb_h
        for y in range(half, self.fb_h):
            d = (self.fb_h * 0.5) / max(0.5, y - self.fb_h * 0.5)
            self.row_dist[y] = d
            self.row_lvl[y] = shade_level(d)

    def check_size(self):
        size = shutil.get_terminal_size()
        if (size.columns, size.lines) != (self.cols, self.rows):
            self.resize(size.columns, size.lines)

    def fg(self, c):
        s = self._fg_cache.get(c)
        if s is None:
            r, g, b = (c >> 16) & 255, (c >> 8) & 255, c & 255
            s = f"\x1b[38;2;{r};{g};{b}m" if TRUECOLOR else f"\x1b[38;5;{to_256(r, g, b)}m"
            self._fg_cache[c] = s
        return s

    def cell(self, top, bot):
        key = (top << 24) | bot
        s = self._cell_cache.get(key)
        if s is None:
            tr, tg, tb = (top >> 16) & 255, (top >> 8) & 255, top & 255
            br, bg, bb = (bot >> 16) & 255, (bot >> 8) & 255, bot & 255
            if TRUECOLOR:
                s = f"\x1b[38;2;{tr};{tg};{tb};48;2;{br};{bg};{bb}m▀"
            else:
                s = f"\x1b[38;5;{to_256(tr, tg, tb)};48;5;{to_256(br, bg, bb)}m▀"
            if len(self._cell_cache) > 60000:
                self._cell_cache.clear()
            self._cell_cache[key] = s
        return s

    # --- world rendering ------------------------------------------------

    def render_world(self, g, now):
        fb_w, fb_h = self.fb_w, self.fb_h
        half = fb_h // 2
        boost = 5 if g.muzzle > 0 else 0
        ceil = self.ceil_rows_lit if boost else self.ceil_rows
        fb = [ceil[y][:] for y in range(half)]
        fb += [[0] * fb_w for _ in range(fb_h - half)]

        # textured floor (scanline casting)
        dirx, diry = math.cos(g.pa), math.sin(g.pa)
        planex, planey = -diry * HALF_FOV_TAN, dirx * HALF_FOV_TAN
        rd0x, rd0y = dirx - planex, diry - planey
        rd1x, rd1y = dirx + planex, diry + planey
        px, py = g.px, g.py
        row_dist, row_lvl = self.row_dist, self.row_lvl
        max_lvl = SHADE_LEVELS - 1
        for y in range(half, fb_h):
            d = row_dist[y]
            tex = FLOOR_TEX[min(max_lvl, row_lvl[y] + boost)]
            fx, fy = px + d * rd0x, py + d * rd0y
            sx = d * (rd1x - rd0x) / fb_w
            sy = d * (rd1y - rd0y) / fb_w
            row = fb[y]
            for x in range(fb_w):
                row[x] = tex[((int(fy * 32) & 31) << 5) | (int(fx * 32) & 31)]
                fx += sx
                fy += sy

        # walls
        zbuf = [MAX_DEPTH] * fb_w
        pa = g.pa
        for col in range(fb_w):
            ray = pa - FOV / 2 + FOV * col / fb_w
            dist, side, wch = cast_ray(px, py, ray)
            if side == 0:
                wall_x = py + dist * math.sin(ray)
            else:
                wall_x = px + dist * math.cos(ray)
            tx = int((wall_x % 1.0) * TEX_SIZE) & 31
            perp = max(0.05, dist * math.cos(ray - pa))
            zbuf[col] = perp
            line_h = int(fb_h / perp)
            top = (fb_h - line_h) // 2
            lvl = min(max_lvl, shade_level(perp) + boost - (3 if side else 0))
            tex = WALL_TEX[wch][max(0, lvl)]
            step = TEX_SIZE / line_h
            y0, y1 = max(0, top), min(fb_h, top + line_h)
            tpos = (y0 - top) * step
            for y in range(y0, y1):
                fb[y][col] = tex[((int(tpos) & 31) << 5) | tx]
                tpos += step

        # bug sprites, far to near
        order = sorted(g.bugs, key=lambda b: -((b.x - px) ** 2 + (b.y - py) ** 2))
        for b in order:
            dx, dy = b.x - px, b.y - py
            dist = max(math.hypot(dx, dy), 0.2)
            diff = angle_diff(math.atan2(dy, dx), pa)
            if abs(diff) > FOV / 2 + 0.5:
                continue
            frame = int(now * 7 + b.phase * 3) % 2
            if b.flash > 0:
                spr = FLASH_SPRITES[frame][0]
            else:
                lvl = shade_level(dist)
                spr = (TANK_SPRITES if b.tank else BUG_SPRITES)[frame][lvl]
            th, tw = len(spr), len(spr[0])
            sh = max(2, int(fb_h * 0.62 / dist))
            sw = max(2, int(sh * tw / th * 1.05))
            sx_c = int((0.5 + diff / FOV) * fb_w)
            bottom = int(fb_h / 2 + fb_h / (2 * dist))
            x0 = sx_c - sw // 2
            for x in range(max(0, x0), min(fb_w, x0 + sw)):
                if zbuf[x] <= dist - 0.1:
                    continue
                tx = (x - x0) * tw // sw
                for yi in range(sh):
                    y = bottom - sh + yi
                    if 0 <= y < fb_h:
                        c = spr[yi * th // sh][tx]
                        if c is not None:
                            fb[y][x] = c

        # particles (screen-space)
        for p in g.particles:
            ix, iy = int(p[0]), int(p[1])
            if 0 <= iy < fb_h:
                row = fb[iy]
                if 0 <= ix < fb_w:
                    row[ix] = p[5]
                if 0 <= ix + 1 < fb_w:
                    row[ix + 1] = p[5]

        # crosshair
        cx, cy = fb_w // 2, fb_h // 2
        if g.muzzle <= 0:
            white, dark = pack(240, 240, 240), pack(20, 20, 20)
            for ox, oy in ((-3, 0), (3, 0), (0, -3), (0, 3)):
                if 0 <= cy + oy < fb_h and 0 <= cx + ox < fb_w:
                    fb[cy + oy][cx + ox] = dark
            for ox, oy in ((-2, 0), (2, 0), (0, -2), (0, 2), (0, 0)):
                if 0 <= cy + oy < fb_h and 0 <= cx + ox < fb_w:
                    fb[cy + oy][cx + ox] = white

        # gun overlay with walk bob
        gun = GUN_FIRE_SPRITE if g.muzzle > 0 else GUN_SPRITE
        gh, gw = len(gun), len(gun[0])
        scale = max(1.0, fb_w / 64)
        dw, dh = int(gw * scale), int(gh * scale)
        bob = int(math.sin(g.bob) * 2)
        gx0 = (fb_w - dw) // 2 + int(math.cos(g.bob * 0.5) * 2)
        gy0 = fb_h - dh + bob
        for y in range(max(0, gy0), fb_h):
            srow = gun[min(gh - 1, (y - gy0) * gh // dh)]
            row = fb[y]
            for x in range(max(0, gx0), min(fb_w, gx0 + dw)):
                c = srow[min(gw - 1, (x - gx0) * gw // dw)]
                if c is not None:
                    row[x] = c

        # red damage tint
        tint = 0.0
        if g.bite_flash > 0:
            tint = 0.45 * min(1.0, g.bite_flash / 0.3)
        if g.hp < 30:
            tint = max(tint, 0.18 + 0.1 * math.sin(now * 6))
        if tint > 0:
            inv = 1.0 - tint
            radd = int(200 * tint)
            for y in range(fb_h):
                row = fb[y]
                for x in range(fb_w):
                    c = row[x]
                    row[x] = pack(((c >> 16) & 255) * inv + radd,
                                  ((c >> 8) & 255) * inv,
                                  (c & 255) * inv)
        return fb

    # --- composition ------------------------------------------------------

    def compose(self, fb, hud, help_line):
        parts = ["\x1b[H\x1b[0m", hud, "\x1b[0m\x1b[K\r\n"]
        pad = self.pad
        cell = self.cell
        for i in range(0, self.fb_h, 2):
            top, bot = fb[i], fb[i + 1]
            parts.append(pad)
            lt = lb = -1
            run = []
            ap = run.append
            for x in range(self.fb_w):
                t, b = top[x], bot[x]
                if t != lt or b != lb:
                    ap(cell(t, b))
                    lt, lb = t, b
                else:
                    ap("▀")
            parts.append("".join(run))
            parts.append("\x1b[0m\x1b[K\r\n")
        parts.append("\x1b[0m\x1b[J")
        parts.append(f"\x1b[{self.rows};1H")
        parts.append(help_line)
        parts.append("\x1b[0m\x1b[K")
        return "".join(parts)

    def hud_line(self, g):
        hp = max(0, int(g.hp))
        segs = hp // 10
        if hp > 60:
            bar_c = pack(80, 220, 80)
        elif hp > 30:
            bar_c = pack(235, 200, 60)
        else:
            bar_c = pack(240, 70, 60)
        dim = self.fg(pack(110, 110, 120))
        bar = self.fg(bar_c) + "█" * segs + self.fg(pack(50, 50, 56)) + "░" * (10 - segs)
        return (f" {self.fg(pack(255, 210, 70))}☠ KILLS {g.score:<4}"
                f"{dim}│ {self.fg(pack(240, 80, 80))}♥ {bar}{self.fg(bar_c)} {hp:>3}"
                f" {dim}│ NEXT MILESTONE {self.fg(pack(120, 200, 255))}{g.next_milestone()}")

    def help_line(self, fps):
        dim = self.fg(pack(95, 95, 105))
        return f"{dim} WASD move · mouse/◄► aim · click/SPACE fire · Q quit{' ' * 8}{fps:>2.0f} fps"


def darken_fb(fb, red=False):
    if red:
        return [[((c >> 2) & 0x3F3F3F) + 0x280000 for c in row] for row in fb]
    return [[(c >> 1) & 0x7F7F7F for c in row] for row in fb]


# --------------------------------------------------------------------------
# Overlay scenes: celebrations & game over
# --------------------------------------------------------------------------

CONFETTI_COLORS = [
    pack(255, 80, 80), pack(255, 200, 60), pack(90, 230, 90),
    pack(90, 180, 255), pack(220, 110, 255), pack(255, 255, 255),
]


def celebrate(term, rnd, g, title, sub, grand=False):
    base = darken_fb(rnd.render_world(g, time.time()))
    fb_w, fb_h = rnd.fb_w, rnd.fb_h
    duration = 5.0 if grand else 2.8
    start = last = time.time()
    confetti, rings = [], []
    ring_timer = 0.0
    title_scale = max(1, (fb_w - 8) // (6 * len(title)))
    while time.time() - start < duration:
        now = time.time()
        dt = min(now - last, 0.1)
        last = now
        for k in term.read_keys():
            skip = (isinstance(k, str) and k) or (isinstance(k, tuple) and k[0] == "click")
            if now - start > 0.7 and skip:
                return
        rnd.check_size()
        if (rnd.fb_w, rnd.fb_h) != (fb_w, fb_h):
            base = darken_fb(rnd.render_world(g, now))
            fb_w, fb_h = rnd.fb_w, rnd.fb_h
        fb = [row[:] for row in base]
        # falling confetti
        for _ in range(6 if grand else 3):
            confetti.append([random.uniform(0, fb_w), 0.0,
                             random.uniform(-6, 6), random.uniform(14, 30),
                             random.choice(CONFETTI_COLORS)])
        for p in confetti:
            p[0] += p[2] * dt
            p[1] += p[3] * dt
        confetti = [p for p in confetti if p[1] < fb_h]
        for p in confetti:
            ix, iy = int(p[0]), int(p[1])
            if 0 <= ix < fb_w and 0 <= iy < fb_h:
                fb[iy][ix] = p[4]
        # firework rings for the grand finale
        if grand:
            ring_timer -= dt
            if ring_timer <= 0:
                rings.append([random.uniform(fb_w * 0.2, fb_w * 0.8),
                              random.uniform(fb_h * 0.15, fb_h * 0.45),
                              0.0, random.choice(CONFETTI_COLORS)])
                ring_timer = 0.35
            for ring in rings:
                ring[2] += 26 * dt
                r = ring[2]
                for i in range(26):
                    a = TAU * i / 26
                    ix, iy = int(ring[0] + math.cos(a) * r), int(ring[1] + math.sin(a) * r * 0.7)
                    if 0 <= ix < fb_w and 0 <= iy < fb_h:
                        fb[iy][ix] = ring[3]
            rings = [ring for ring in rings if ring[2] < fb_h * 0.6]
        # text
        blink = pack(255, 230, 90) if int(now * 4) % 2 else pack(255, 255, 255)
        ty = fb_h // 3
        draw_text(fb, fb_w, fb_h, title, ty, title_scale, blink if grand else pack(255, 220, 80))
        draw_text(fb, fb_w, fb_h, sub, ty + title_scale * 7, 1, pack(220, 220, 230))
        draw_text(fb, fb_w, fb_h, f"KILLS {g.score}", ty + title_scale * 7 + 8, 1, pack(140, 235, 140))
        sys.stdout.write(rnd.compose(fb, rnd.hud_line(g), rnd.help_line(0)))
        sys.stdout.flush()
        time.sleep(max(0.0, 1 / TARGET_FPS - (time.time() - now)))


def game_over(term, rnd, g):
    base = darken_fb(rnd.render_world(g, time.time()), red=True)
    while True:
        now = time.time()
        rnd.check_size()
        fb = [row[:] for row in base]
        fb_w, fb_h = rnd.fb_w, rnd.fb_h
        ty = fb_h // 3
        draw_text(fb, fb_w, fb_h, "YOU GOT", ty, 2, pack(255, 70, 60))
        draw_text(fb, fb_w, fb_h, "DEBUGGED", ty + 14, 2, pack(255, 70, 60))
        draw_text(fb, fb_w, fb_h, f"FINAL KILLS {g.score}", ty + 30, 1, pack(230, 230, 230))
        if int(now * 2) % 2:
            draw_text(fb, fb_w, fb_h, "R RESPAWN - Q QUIT", ty + 40, 1, pack(255, 210, 80))
        sys.stdout.write(rnd.compose(fb, rnd.hud_line(g), rnd.help_line(0)))
        sys.stdout.flush()
        for k in term.read_keys():
            if k == "r":
                return "restart"
            if k == "q":
                return "quit"
        time.sleep(0.05)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def kill_burst(rnd, g, bug, dist):
    diff = angle_diff(math.atan2(bug.y - g.py, bug.x - g.px), g.pa)
    sx = (0.5 + diff / FOV) * rnd.fb_w
    sh = rnd.fb_h * 0.62 / dist
    sy = rnd.fb_h / 2 + rnd.fb_h / (2 * dist) - sh / 2
    goo = [pack(110, 220, 70), pack(70, 170, 50), pack(180, 240, 120), pack(255, 220, 80)]
    n = max(8, int(30 / (1 + dist * 0.3)))
    for _ in range(n):
        a = random.uniform(0, TAU)
        sp = random.uniform(6, 40) / (1 + dist * 0.15)
        g.particles.append([sx, sy, math.cos(a) * sp, math.sin(a) * sp - 12,
                            random.uniform(0.3, 0.8), random.choice(goo)])


def play(term, rnd):
    g = Game()
    last = time.time()
    fps = TARGET_FPS
    mouse_x = None
    while True:
        now = time.time()
        dt = min(now - last, 0.1)
        last = now
        rnd.check_size()
        if rnd.cols < 50 or rnd.rows < 16:
            sys.stdout.write("\x1b[H\x1b[2J\x1b[0mTerminal too small — need at least 50x16. (Q quits)")
            sys.stdout.flush()
            if "q" in term.read_keys():
                return "quit"
            time.sleep(0.2)
            continue

        want_fire = False
        for k in term.read_keys():
            if isinstance(k, tuple):
                if k[0] == "move":
                    if mouse_x is not None:
                        g.pa += (k[1] - mouse_x) * MOUSE_SENS
                    mouse_x = k[1]
                    if k[3] == 0:  # left button held while dragging
                        want_fire = True
                elif k[0] == "click":
                    mouse_x = k[1]
                    want_fire = True
                elif k[0] == "scroll":
                    g.walk(0.22 * k[1], 0)
            elif k == "q":
                return "quit"
            elif k in ("w", "UP"):
                g.walk(0.35, 0)
            elif k in ("s", "DOWN"):
                g.walk(-0.35, 0)
            elif k == "a":
                g.walk(0, -0.3)
            elif k == "d":
                g.walk(0, 0.3)
            elif k == "LEFT":
                g.pa -= 0.11
            elif k == "RIGHT":
                g.pa += 0.11
            elif k == " ":
                want_fire = True

        if want_fire and now - g.last_shot >= FIRE_COOLDOWN:
            killed, kd = g.shoot(now)
            if killed:
                kill_burst(rnd, g, killed, kd)

        g.spawn_timer -= dt
        cap = min(4 + g.score // 8, 14)
        if g.spawn_timer <= 0 and len(g.bugs) < cap:
            g.spawn_bug()
            g.spawn_timer = max(0.6, 2.5 - g.score * 0.02)

        g.update_bugs(dt, now)

        if now - g.last_bite > 3 and g.hp < 100:
            g.hp = min(100.0, g.hp + 2 * dt)

        g.muzzle = max(0.0, g.muzzle - dt)
        g.bite_flash = max(0.0, g.bite_flash - dt)
        for p in g.particles:
            p[0] += p[2] * dt
            p[1] += p[3] * dt
            p[3] += 55 * dt
            p[4] -= dt
        g.particles = [p for p in g.particles if p[4] > 0]

        if g.hp <= 0:
            res = game_over(term, rnd, g)
            if res == "restart":
                g = Game()
                last = time.time()
                continue
            return "quit"

        ms = None
        if g.score in MILESTONES and g.score not in g.celebrated:
            ms = (g.score, *MILESTONES[g.score])
        elif g.score >= 200 and g.score % 100 == 0 and g.score not in g.celebrated:
            ms = (g.score, "UNSTOPPABLE!", f"{g.score} BUGS AND COUNTING")
        if ms:
            g.celebrated.add(ms[0])
            celebrate(term, rnd, g, ms[1], ms[2], grand=ms[0] >= 100)
            last = time.time()

        fb = rnd.render_world(g, now)
        sys.stdout.write(rnd.compose(fb, rnd.hud_line(g), rnd.help_line(fps)))
        sys.stdout.flush()
        frame = time.time() - now
        fps = 0.9 * fps + 0.1 * (1.0 / max(frame, 1e-6))
        time.sleep(max(0.0, 1 / TARGET_FPS - frame))


def main():
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        print("BUG DOOM needs an interactive terminal.")
        return
    try:
        with Term() as term:
            rnd = Renderer()
            rnd.check_size()
            play(term, rnd)
    except KeyboardInterrupt:
        pass
    print("Thanks for playing BUG DOOM. The codebase is safe... for now.")


if __name__ == "__main__":
    main()
