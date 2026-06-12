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
    G / right click         throw bug bomb
    Q                       quit

Run:  python3 bug_doom.py
"""

import json
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
FIRE_COOLDOWN = 0.22  # legacy default; per-weapon cooldowns live in WEAPONS
MUZZLE_TIME = 0.09

WEAPONS = [
    dict(name="PISTOL", cooldown=0.30, pellets=1, spread=0.0, damage=1, range=20, cone=0.45),
    dict(name="SHOTGUN", cooldown=0.55, pellets=6, spread=0.10, damage=1, range=10, cone=0.55),
    dict(name="SMG", cooldown=0.09, pellets=1, spread=0.035, damage=1, range=14, cone=0.40),
]
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


# World-space projectile definitions. Content (acid globs, grenades, ...)
# registers kinds here: dict(life, wall="die"|"slide", hit_player=0.0_or_radius,
# player_damage, gravity, friction, color, core, size). "test" exists only for
# headless testing and is never spawned during play.
PROJ_DEFS = {
    "test": dict(life=4.0, wall="die", hit_player=0.45, player_damage=10,
                 gravity=False, friction=0.0, color=pack(150, 235, 60),
                 core=pack(220, 255, 140), size=0.05),
    # spitter acid: slow, dodgeable, never collides with bugs
    "glob": dict(life=4.0, wall="die", hit_player=0.45, player_damage=10,
                 gravity=False, friction=0.0, color=pack(150, 235, 60),
                 core=pack(220, 255, 140), size=0.05),
    # player grenade: skids along walls to rest, detonates on its fuse
    "nade": dict(life=8.0, wall="slide", hit_player=0.0, player_damage=0,
                 gravity=False, friction=2.2, color=pack(26, 30, 26),
                 core=pack(255, 40, 40), size=0.06),
}


def tex_brick(x, y):
    off = 16 if (y // 8) % 2 else 0
    if y % 8 == 0 or (x + off) % 16 == 0:
        return pack(72, 66, 58)
    n = _NOISE[y][x] * 14
    return pack(150 + n, 62 + n * 0.5, 46 + n * 0.4)


def tex_tech(x, y, glow=(70, 215, 175)):
    if x % 16 == 0 or y % 16 == 0:
        return pack(38, 44, 56)
    if (x % 16 in (2, 13)) and (y % 16 in (2, 13)):
        return pack(170, 180, 200)
    if 13 <= y <= 15:
        return pack(*glow)
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

# Arena themes: per-channel multipliers over the base textures plus ceiling
# gradient endpoints. The arena rotates through these every 100 kills.
THEMES = [
    dict(name="THE CODEBASE", mults={"#": (1, 1, 1), "%": (1, 1, 1), "&": (1, 1, 1)},
         glow=(70, 215, 175), floor=(1, 1, 1), ceil_top=(8, 10, 16), ceil_bot=(22, 26, 36)),
    dict(name="INFERNO", mults={"#": (1.35, 0.55, 0.45), "%": (1.2, 0.6, 0.5), "&": (1.3, 0.7, 0.4)},
         glow=(255, 140, 40), floor=(1.25, 0.7, 0.6), ceil_top=(26, 8, 6), ceil_bot=(48, 18, 12)),
    dict(name="CRYO", mults={"#": (0.7, 0.85, 1.3), "%": (0.8, 0.95, 1.35), "&": (0.7, 0.9, 1.25)},
         glow=(120, 220, 255), floor=(0.8, 0.9, 1.2), ceil_top=(10, 14, 26), ceil_bot=(26, 34, 52)),
    dict(name="THE SEWERS", mults={"#": (0.7, 1.1, 0.6), "%": (0.7, 1.05, 0.7), "&": (0.75, 1.25, 0.6)},
         glow=(150, 235, 60), floor=(0.75, 1.05, 0.65), ceil_top=(8, 14, 8), ceil_bot=(18, 30, 16)),
]


def tint(c, m):
    return pack(((c >> 16) & 255) * m[0], ((c >> 8) & 255) * m[1], (c & 255) * m[2])


def build_theme(t):
    """Build the full shaded texture set for a theme (~40-80ms — only ever
    called at a celebration boundary, never mid-frame)."""
    base_fns = {
        "#": tex_brick,
        "%": lambda x, y: tex_tech(x, y, t["glow"]),
        "&": tex_moss,
    }
    walls = {ch: build_shaded_texture(lambda x, y, ch=ch: tint(base_fns[ch](x, y), t["mults"][ch]))
             for ch in "#%&"}
    floor = build_shaded_texture(lambda x, y: tint(tex_floor(x, y), t["floor"]))
    return dict(walls=walls, floor=floor)


THEME_CACHE = {0: dict(walls=WALL_TEX, floor=FLOOR_TEX)}


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
SKITTER_PALETTE = {
    "k": (40, 36, 8), "g": (196, 188, 40), "h": (235, 230, 110),
    "R": (20, 20, 24), "r": (90, 84, 20), "l": (70, 66, 16),
    "w": (240, 240, 200),
}
SPITTER_PALETTE = {
    "k": (30, 8, 8), "g": (180, 70, 40), "h": (230, 130, 60),
    "R": (160, 255, 60), "r": (80, 30, 20), "l": (60, 24, 16),
    "w": (235, 235, 200),
}
BOOMER_PALETTE = {
    "k": (50, 8, 8), "g": (200, 50, 40), "h": (255, 120, 90),
    "R": (255, 230, 80), "r": (90, 20, 16), "l": (70, 16, 12),
    "w": (255, 200, 180),
}
BOSS_PALETTE = {
    "k": (30, 4, 30), "g": (170, 30, 130), "h": (240, 80, 190),
    "R": (255, 250, 120), "r": (80, 14, 60), "l": (60, 10, 46),
    "w": (255, 230, 230),
}

# Data-driven enemy kinds: stats are copied onto each Bug at spawn so the
# per-frame code never touches these dicts.
BUG_KINDS = {
    "grunt": dict(hp=1, speed=1.0, scale=1.0, bite=7),
    "tank": dict(hp=3, speed=0.7, scale=1.0, bite=7),
    "skitter": dict(hp=1, speed=2.0, scale=0.55, bite=4),
    "spitter": dict(hp=2, speed=1.0, scale=1.0, bite=7),
    "boomer": dict(hp=2, speed=1.45, scale=1.0, bite=0),
    "boss": dict(hp=30, speed=0.5, scale=2.6, bite=16),
}

# Death-splatter palettes per kind (default = grunt green).
KIND_GOO = {
    "tank": [pack(148, 62, 200), pack(70, 34, 95), pack(188, 110, 236), pack(255, 214, 40)],
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
KIND_SPRITES = {
    "grunt": BUG_SPRITES,
    "tank": TANK_SPRITES,
    "skitter": [build_sprite(a, SKITTER_PALETTE) for a in BUG_FRAMES_ART],
    "spitter": [build_sprite(a, SPITTER_PALETTE) for a in BUG_FRAMES_ART],
    "boomer": [build_sprite(a, BOOMER_PALETTE) for a in BUG_FRAMES_ART],
    "boss": [build_sprite(a, BOSS_PALETTE) for a in BUG_FRAMES_ART],
}
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


PISTOL_ART = [
    "...kkkkk...",
    "...kgggk...",
    "...kglgk...",
    "...kglgk...",
    "..kkglgkk..",
    "..kdglgdk..",
    ".kkdgggdkk.",
    ".kdddddddk.",
    ".kdddddddk.",
    "kdddddddddk",
    "kdddddddddk",
]
PISTOL_FIRE_ART = [
    "...yYWYy...",
    ".yYWWWWWYy.",
    "..yYWWWYy..",
    "...kYWYk...",
    "..kkglgkk..",
    "..kdglgdk..",
    ".kkdgggdkk.",
    ".kdddddddk.",
    ".kdddddddk.",
    "kdddddddddk",
    "kdddddddddk",
]
SMG_ART = [
    "......kkkkk......",
    "......kgggk......",
    "......kglgk......",
    "......kglgk......",
    ".....kkglgkk.....",
    ".....kdkdkdk.....",
    "....kkdkdkdkk....",
    "...kkkdkdkdkk....",
    "...kkkddddddkk...",
    "...kkkddddddddk..",
    "...kkkdddddddddk.",
    "..kdddddddddddk..",
    ".kdddddddddddddk.",
]
SMG_FIRE_ART = [
    ".......yYWYy.....",
    ".....yYWWWWWYy...",
    "....yYWWWWWWWYy..",
    ".....yYWWWWWYy...",
    ".....kkYWWYkk....",
    ".....kdkdkdk.....",
    "....kkdkdkdkk....",
    "...kkkdkdkdkk....",
    "...kkkddddddkk...",
    "...kkkddddddddk..",
    "...kkkdddddddddk.",
    "..kdddddddddddk..",
    ".kdddddddddddddk.",
]


def build_overlay(art, palette):
    return [[pack(*palette[ch]) if ch in palette else None for ch in line] for line in art]


GUN_SPRITE = build_overlay(GUN_ART, GUN_PALETTE)
GUN_FIRE_SPRITE = build_overlay(GUN_FIRE_ART, GUN_PALETTE)
PISTOL_SPRITE = build_overlay(PISTOL_ART, GUN_PALETTE)
PISTOL_FIRE_SPRITE = build_overlay(PISTOL_FIRE_ART, GUN_PALETTE)
SMG_SPRITE = build_overlay(SMG_ART, GUN_PALETTE)
SMG_FIRE_SPRITE = build_overlay(SMG_FIRE_ART, GUN_PALETTE)
GUN_SPRITES_BY_WEAPON = [
    (PISTOL_SPRITE, PISTOL_FIRE_SPRITE),
    (GUN_SPRITE, GUN_FIRE_SPRITE),
    (SMG_SPRITE, SMG_FIRE_SPRITE),
]

# Pickup art (billboarded in-world like bug sprites)
HEALTH_PAL = {"r": (200, 40, 40), "w": (255, 255, 255), "k": (40, 10, 10)}
HEALTH_ART = [
    ".........",
    "kkkkkkkkk",
    "krrrwrrrk",
    "krrrwrrrk",
    "kwwwwwwwk",
    "krrrwrrrk",
    "krrrwrrrk",
    "kkkkkkkkk",
    ".........",
]
QUAD_PAL = {"p": (190, 70, 255), "P": (240, 180, 255)}
QUAD_ART = [
    "....p....",
    "...ppp...",
    "..pPPPp..",
    ".pPPPPPp.",
    "pPPPPPPPp",
    ".pPPPPPp.",
    "..pPPPp..",
    "...ppp...",
    "....p....",
]
SPEED_PAL = {"y": (255, 220, 40), "Y": (255, 245, 160)}
SPEED_ART = [
    "...Yyyy..",
    "..Yyy....",
    ".Yyy.....",
    ".Yyyyyy..",
    "...Yyy...",
    "..Yyy....",
    ".Yyy.....",
    "Yyy......",
    "Yy.......",
]
INVULN_PAL = {"g": (255, 200, 60), "G": (255, 240, 160)}
INVULN_ART = [
    "....g....",
    "...gGg...",
    "...gGg...",
    "ggggGgggg",
    ".ggGGGgg.",
    "..ggGgg..",
    "..gGgGg..",
    ".gg...gg.",
    "gg.....gg",
]
PICKUP_SPRITES = {
    "health": build_sprite(HEALTH_ART, HEALTH_PAL),
    "quad": build_sprite(QUAD_ART, QUAD_PAL),
    "speed": build_sprite(SPEED_ART, SPEED_PAL),
    "invuln": build_sprite(INVULN_ART, INVULN_PAL),
}
PICKUP_KINDS = ("health", "quad", "speed", "invuln")
PICKUP_WEIGHTS = (45, 20, 20, 15)

# Grenade billboard: dark sphere, blinking red fuse (two frames swap R<->k).
NADE_PAL = {"k": (26, 30, 26), "h": (90, 110, 90), "R": (255, 40, 40)}
NADE_ART_LIT = [
    "..R..",
    ".hkk.",
    "hkkkk",
    "kkkkk",
    ".kkk.",
]
NADE_ART_OFF = [
    "..k..",
    ".hkk.",
    "hkkkk",
    "kkkkk",
    ".kkk.",
]
# projectile kinds rendered as billboarded sprites instead of colored squares
PROJ_SPRITES = {
    "nade": [build_sprite(NADE_ART_LIT, NADE_PAL), build_sprite(NADE_ART_OFF, NADE_PAL)],
}


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
# Persistence (~/.bug_doom_save.json) — must never crash the game
# --------------------------------------------------------------------------

SAVE_PATH = os.path.expanduser("~/.bug_doom_save.json")
PREV_HIGH = [0]  # high score loaded at startup; new Games copy it


def load_save():
    try:
        with open(SAVE_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_save(data):
    try:
        tmp = SAVE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, SAVE_PATH)
    except Exception:
        pass


def record_game_end(g):
    """Merge this run's stats into the save file. Idempotent per Game."""
    if g.stats_recorded:
        return
    g.stats_recorded = True
    data = load_save()  # read-modify-write: never clobber unknown keys
    data["high_kills"] = max(data.get("high_kills", 0), g.score)
    data["total_kills"] = data.get("total_kills", 0) + g.score
    data["games_played"] = data.get("games_played", 0) + 1
    data["longest_run_s"] = max(data.get("longest_run_s", 0.0), time.time() - g.start_time)
    write_save(data)


# --------------------------------------------------------------------------
# Game logic
# --------------------------------------------------------------------------

class Bug:
    def __init__(self, x, y, kind="grunt"):
        self.x, self.y = x, y
        self.kind = kind
        k = BUG_KINDS[kind]
        self.hp = k["hp"]
        self.speed_mult = k["speed"]
        self.scale = k["scale"]
        self.bite_dmg = k["bite"]
        self.max_hp = self.hp
        self.phase = random.uniform(0, TAU)
        self.bite_cd = 0.0
        self.flash = 0.0
        # pack AI: half chase directly, a quarter flank each side
        self.flank = random.choice((-1.0, 0.0, 0.0, 1.0))
        self.spit_cd = random.uniform(1.0, 2.0)
        self.strafe_dir = 1 if self.phase > math.pi else -1
        self.fuse = -1.0  # boomers: negative = unarmed
        self.summon_cd = 6.0  # bosses


class Pickup:
    def __init__(self, x, y, kind, born):
        self.x, self.y = x, y
        self.kind = kind
        self.born = born


class Game:
    def __init__(self):
        self.px, self.py, self.pa = PLAYER_START
        self.hp = 100.0
        self.score = 0
        self.bugs = []
        self.pickups = []
        self.projectiles = []  # dicts: kind, x, y, vx, vy, age, fuse
        self.particles = []  # [x, y, vx, vy, life, color]
        self.spawn_timer = 0.0
        self.last_shot = -1.0
        self.weapon = 1  # shotgun default
        self.muzzle = 0.0
        self.bite_flash = 0.0
        self.last_bite = -10.0
        self.bob = 0.0
        self.celebrated = set()
        self.next_boss = 50
        self.boss_warn = 0.0
        self.grenades = 2
        self.nade_cd = 0.0
        self.kills_since_nade = 0
        self.quad_until = 0.0
        self.speed_until = 0.0
        self.invuln_until = 0.0
        # run stats (recorded to the save file at game end)
        self.start_time = time.time()
        self.shots_fired = 0
        self.shots_hit = 0
        self.damage_taken = 0.0
        self.kills_by_kind = {}
        self.best_combo = 0  # maintained by a future combo system
        self.stats_recorded = False
        self.prev_high = PREV_HIGH[0]

    def try_move(self, nx, ny):
        r = 0.2
        if not (is_wall(nx + r, self.py) or is_wall(nx - r, self.py)):
            self.px = nx
        if not (is_wall(self.px, ny + r) or is_wall(self.px, ny - r)):
            self.py = ny

    def walk(self, forward, strafe):
        if time.time() < self.speed_until:
            forward *= 1.6
            strafe *= 1.6
        ang = self.pa
        dx = math.cos(ang) * forward + math.cos(ang + math.pi / 2) * strafe
        dy = math.sin(ang) * forward + math.sin(ang + math.pi / 2) * strafe
        self.try_move(self.px + dx, self.py + dy)
        self.bob += 0.5

    def find_spawn(self, min_player_dist=6.0, attempts=40):
        for _ in range(attempts):
            x = random.uniform(1.5, MAP_W - 1.5)
            y = random.uniform(1.5, MAP_H - 1.5)
            if is_wall(x, y):
                continue
            if math.hypot(x - self.px, y - self.py) < min_player_dist:
                continue
            return x, y
        return None

    def pick_kind(self):
        if self.score >= 25 and random.random() < 0.25:
            return "tank"
        elif self.score >= 30 and random.random() < 0.15:
            return "boomer"
        elif self.score >= 18 and random.random() < 0.20:
            return "spitter"
        elif self.score >= 12 and random.random() < 0.30:
            return "skitter"
        return "grunt"

    def spawn_bug(self):
        pos = self.find_spawn()
        if not pos:
            return
        kind = self.pick_kind()
        if kind == "skitter":  # skitterers arrive as a pack of 3
            for _ in range(3):
                x = pos[0] + random.uniform(-0.6, 0.6)
                y = pos[1] + random.uniform(-0.6, 0.6)
                if not is_wall(x, y):
                    self.bugs.append(Bug(x, y, "skitter"))
        else:
            self.bugs.append(Bug(pos[0], pos[1], kind))

    def hurt(self, dmg, now):
        """Central player-damage funnel: invulnerability (and any future
        armor) lives here. Returns True if damage was applied."""
        if now < self.invuln_until:
            return False
        self.hp -= dmg
        self.damage_taken += dmg
        self.bite_flash = 0.3
        self.last_bite = now
        return True

    def _step(self, b, ang, spd, sep):
        """Per-axis wall-checked move (+ separation blend). Returns the
        number of blocked axes; both blocked = corner, re-roll the weave."""
        mx, my = math.cos(ang) * spd, math.sin(ang) * spd
        if sep:
            mx += sep[0]
            my += sep[1]
        blocked = 0
        nx = b.x + mx
        if is_wall(nx, b.y):
            blocked += 1
        else:
            b.x = nx
        ny = b.y + my
        if is_wall(b.x, ny):
            blocked += 1
        else:
            b.y = ny
        if blocked == 2:
            b.phase += 1.7
        return blocked

    def detonate_boomer(self, x, y, now, bug=None):
        """Boomer AoE, shared by the fuse-expiry and shot-at-range paths.
        Returns explode_at's kill list for FX/chains in the caller."""
        if bug is not None and bug in self.bugs:
            self.bugs.remove(bug)
        pd = max(0.0, 24 - 12 * math.hypot(x - self.px, y - self.py))
        kills = self.explode_at(x, y, now, bug_damage=2, bug_radius=1.8,
                                player_damage=pd, player_radius=2.2)
        self.muzzle = 0.12  # reuse the muzzle scene-boost as a light flash
        return kills

    def update_bugs(self, dt, now):
        """AI/movement step. Returns boomer detonations as (x, y, kills)."""
        speed = 1.0 + min(self.score * 0.015, 1.5)
        detonations = []
        bugs = self.bugs
        # separation: nearest-neighbour repulsion so packs don't stack.
        # O(n^2) with n <= 22; sqrt only inside the 0.9 radius.
        n = len(bugs)
        sep = [None] * n
        for i in range(n):
            b = bugs[i]
            if b.kind == "boss":
                continue
            bx, by = b.x, b.y
            best, bo = 1e9, None
            for j in range(n):
                if j == i:
                    continue
                o = bugs[j]
                d2 = (bx - o.x) ** 2 + (by - o.y) ** 2
                if d2 < best:
                    best, bo = d2, o
            if bo is not None and best < 0.81:
                d = math.sqrt(best)
                away = math.atan2(by - bo.y, bx - bo.x)
                f = (0.9 - d) * 1.5 * dt
                sep[i] = (math.cos(away) * f, math.sin(away) * f)
        for i, b in enumerate(list(bugs)):
            if detonations and b not in self.bugs:
                continue  # chained away by an earlier detonation this frame
            b.bite_cd = max(0.0, b.bite_cd - dt)
            b.flash = max(0.0, b.flash - dt)
            dx, dy = self.px - b.x, self.py - b.y
            dist = math.hypot(dx, dy)
            ang = math.atan2(dy, dx)
            kind = b.kind
            spd = speed * b.speed_mult * dt
            s = sep[i] if i < n else None
            if kind == "boomer":
                # straight-line kamikaze: no weave/flank, arms at close range
                if b.fuse >= 0:
                    b.fuse -= dt
                    if b.fuse <= 0:
                        x, y = b.x, b.y
                        detonations.append((x, y, self.detonate_boomer(x, y, now, bug=b)))
                elif dist < 1.3:
                    b.fuse = 0.45
                else:
                    self._step(b, ang, spd, s)
                continue  # bite=0; the explosion is the payload
            if kind == "spitter":
                b.spit_cd -= dt
                if (b.spit_cd <= 0 and 3.0 < dist < 11.0
                        and sum(1 for p in self.projectiles if p["kind"] == "glob") < 12
                        and cast_ray(b.x, b.y, ang)[0] > dist):
                    a = math.atan2(dy, dx) + random.uniform(-0.05, 0.05)
                    self.projectiles.append(dict(kind="glob", x=b.x, y=b.y,
                                                 vx=math.cos(a) * 4.5, vy=math.sin(a) * 4.5,
                                                 age=0.0, fuse=None))
                    b.spit_cd = 2.2
                    b.flash = 0.08  # white flash doubles as the wind-up tell
                if dist > 9.0:
                    self._step(b, ang + math.sin(now * 2 + b.phase) * 0.6, spd, s)
                elif dist < 4.0:
                    self._step(b, ang, -spd, s)  # back away, hold range
                elif self._step(b, ang + math.pi / 2 * b.strafe_dir, 0.4 * dt, s):
                    b.strafe_dir = -b.strafe_dir
            elif kind == "boss":
                b.summon_cd -= dt
                if b.summon_cd <= 0:
                    b.summon_cd = 7.0
                    for a in (0.0, TAU / 3, 2 * TAU / 3):
                        if len(self.bugs) >= 22:
                            break
                        sx, sy = b.x + math.cos(a) * 1.2, b.y + math.sin(a) * 1.2
                        if not is_wall(sx, sy):
                            nb = Bug(sx, sy, "grunt")
                            nb.flash = 0.2  # materialize white-hot
                            self.bugs.append(nb)
                if dist > 0.5:  # straight chase: no weave/flank/separation
                    self._step(b, ang, spd, None)
            else:
                # grunts / tanks / skitterers: flank wide, weave in, chase
                if b.flank != 0 and dist > 3.0:
                    off = 2.2 * b.flank
                    tx = self.px - math.sin(ang) * off
                    ty = self.py + math.cos(ang) * off
                    ang = math.atan2(ty - b.y, tx - b.x)
                if dist > 2.5:
                    if kind == "skitter":
                        ang += math.sin(now * 5 + b.phase) * 1.1
                    else:
                        ang += math.sin(now * 2 + b.phase) * 0.6
                if dist > 0.5:  # stop at biting range so bugs stay shootable
                    self._step(b, ang, spd, s)
            if b.bite_dmg and dist < 0.75 and b.bite_cd <= 0:
                self.hurt(b.bite_dmg, now)
                b.bite_cd = 1.5 if kind == "boss" else 1.0
        return detonations

    def shoot(self, now):
        """Fire the current weapon. Returns a list of hit events, one tuple
        (bug, dist, killed_bool) per pellet that connected (empty = whiff)."""
        self.shots_fired += 1
        self.last_shot = now
        self.muzzle = MUZZLE_TIME
        w = WEAPONS[self.weapon]
        dmg = w["damage"] * (4 if now < self.quad_until else 1)
        events = []
        for _ in range(w["pellets"]):
            a = self.pa + random.uniform(-w["spread"], w["spread"])
            best = None
            for b in self.bugs:
                if b.hp <= 0:
                    continue
                dx, dy = b.x - self.px, b.y - self.py
                dist = math.hypot(dx, dy)
                if dist > w["range"] or dist < 0.1:
                    continue
                ang = math.atan2(dy, dx)
                if abs(angle_diff(ang, a)) > max(math.atan2(w["cone"] * b.scale, dist), 0.04):
                    continue
                wall_d, _, _ = cast_ray(self.px, self.py, ang)
                if wall_d < dist - 0.2:
                    continue
                if best is None or dist < best[0]:
                    best = (dist, b)
            if best:
                bug = best[1]
                bug.hp -= dmg
                bug.flash = 0.15
                if bug.hp <= 0:
                    self.bugs.remove(bug)
                    self.score += 1
                    self.kills_by_kind[bug.kind] = self.kills_by_kind.get(bug.kind, 0) + 1
                    events.append((bug, best[0], True))
                else:
                    events.append((bug, best[0], False))
        if events:
            self.shots_hit += 1  # once per trigger pull, not per pellet
        return events

    def update_projectiles(self, dt, now):
        """Integrate world-space projectiles. Returns a list of events
        ("wall"|"expired"|"hit_player"|"fuse", proj) for the caller."""
        events = []
        dead = []
        for p in self.projectiles:
            d = PROJ_DEFS[p["kind"]]
            if d["friction"]:
                f = 1 - d["friction"] * dt
                p["vx"] *= f
                p["vy"] *= f
            alive = True
            nx = p["x"] + p["vx"] * dt
            if is_wall(nx, p["y"]):
                if d["wall"] == "die":
                    alive = False
                    events.append(("wall", p))
                else:  # slide
                    p["vx"] = 0.0
            else:
                p["x"] = nx
            if alive:
                ny = p["y"] + p["vy"] * dt
                if is_wall(p["x"], ny):
                    if d["wall"] == "die":
                        alive = False
                        events.append(("wall", p))
                    else:
                        p["vy"] = 0.0
                else:
                    p["y"] = ny
            p["age"] += dt
            if alive and p["age"] > d["life"]:
                alive = False
                events.append(("expired", p))
            if alive and p.get("fuse") is not None:
                p["fuse"] -= dt
                if p["fuse"] <= 0:
                    alive = False
                    events.append(("fuse", p))
            if alive and d["hit_player"] and \
                    math.hypot(p["x"] - self.px, p["y"] - self.py) < d["hit_player"]:
                alive = False
                events.append(("hit_player", p))
            if not alive:
                dead.append(p)
        for p in dead:
            self.projectiles.remove(p)
        return events

    def explode_at(self, x, y, now, bug_damage, bug_radius, player_damage, player_radius):
        """AoE damage. Returns [(killed_bug, dist_from_player), ...]."""
        killed = []
        for b in list(self.bugs):
            if math.hypot(b.x - x, b.y - y) < bug_radius:
                b.hp -= bug_damage
                b.flash = 0.15
                if b.hp <= 0 and b in self.bugs:
                    self.bugs.remove(b)
                    self.score += 1
                    self.kills_by_kind[b.kind] = self.kills_by_kind.get(b.kind, 0) + 1
                    killed.append((b, math.hypot(b.x - self.px, b.y - self.py)))
        if player_damage and math.hypot(self.px - x, self.py - y) < player_radius:
            self.hurt(player_damage, now)
        return killed

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
                elif m.group(4) == b"M" and (code & 3) == 2:
                    events.append(("rclick", x, y))
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
        self.wall_tex = WALL_TEX
        self.floor_tex = FLOOR_TEX
        self.ceil_top = (8, 10, 16)
        self.ceil_bot = (22, 26, 36)
        self.theme_idx = 0

    def set_theme(self, idx):
        idx %= len(THEMES)
        if idx == self.theme_idx:
            return
        th = THEME_CACHE.get(idx)
        if th is None:
            th = THEME_CACHE[idx] = build_theme(THEMES[idx])
        self.wall_tex = th["walls"]
        self.floor_tex = th["floor"]
        t = THEMES[idx]
        self.ceil_top, self.ceil_bot = t["ceil_top"], t["ceil_bot"]
        self.theme_idx = idx
        if self.cols:  # rebuild the ceiling gradient rows
            self.resize(self.cols, self.rows)

    def resize(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.fb_w = min(cols, 180)
        self.fb_h = min(2 * (rows - 2), 130)
        self.pad = " " * max(0, (cols - self.fb_w) // 2)
        half = self.fb_h // 2
        # ceiling gradient (normal + muzzle-lit variants)
        (tr, tg, tb), (br, bg, bb) = self.ceil_top, self.ceil_bot
        self.ceil_rows, self.ceil_rows_lit = [], []
        for y in range(half):
            t = y / max(1, half - 1)
            r, g, b = tr + (br - tr) * t, tg + (bg - tg) * t, tb + (bb - tb) * t
            self.ceil_rows.append([pack(r, g, b)] * self.fb_w)
            self.ceil_rows_lit.append([pack(r + 16, g + 14, b + 12)] * self.fb_w)
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

    def _blit_billboard(self, fb, zbuf, spr, dist, sx_c, bottom, sh, sw):
        """Depth-tested sprite blit anchored at `bottom`, centered on sx_c."""
        fb_w, fb_h = self.fb_w, self.fb_h
        th, tw = len(spr), len(spr[0])
        x0 = sx_c - sw // 2
        yi0 = max(0, sh - bottom)  # clamp to on-screen rows up front
        yi1 = min(sh, fb_h - bottom + sh)
        for x in range(max(0, x0), min(fb_w, x0 + sw)):
            if zbuf[x] <= dist - 0.1:
                continue
            tx = (x - x0) * tw // sw
            for yi in range(yi0, yi1):
                c = spr[yi * th // sh][tx]
                if c is not None:
                    fb[bottom - sh + yi][x] = c

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
        floor_tex, wall_tex = self.floor_tex, self.wall_tex
        for y in range(half, fb_h):
            d = row_dist[y]
            tex = floor_tex[min(max_lvl, row_lvl[y] + boost)]
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
            tex = wall_tex[wch][max(0, lvl)]
            step = TEX_SIZE / line_h
            y0, y1 = max(0, top), min(fb_h, top + line_h)
            tpos = (y0 - top) * step
            for y in range(y0, y1):
                fb[y][col] = tex[((int(tpos) & 31) << 5) | tx]
                tpos += step

        # billboard entities (bugs, pickups, projectiles), far to near
        draw = [((b.x - px) ** 2 + (b.y - py) ** 2, "bug", b) for b in g.bugs]
        draw += [((p.x - px) ** 2 + (p.y - py) ** 2, "pickup", p) for p in g.pickups]
        draw += [((p["x"] - px) ** 2 + (p["y"] - py) ** 2, "proj", p) for p in g.projectiles]
        draw.sort(key=lambda e: -e[0])
        for _, tag, obj in draw:
            if tag == "bug":
                b = obj
                dx, dy = b.x - px, b.y - py
                dist = max(math.hypot(dx, dy), 0.2)
                diff = angle_diff(math.atan2(dy, dx), pa)
                if abs(diff) > FOV / 2 + 0.5:
                    continue
                frame = int(now * (14 if b.kind == "skitter" else 7) + b.phase * 3) % 2
                if b.flash > 0 or (b.fuse >= 0 and int(now * 12) % 2):
                    spr = FLASH_SPRITES[frame][0]  # hit flash / armed-boomer strobe
                else:
                    lvl = shade_level(dist)
                    spr = KIND_SPRITES[b.kind][frame][lvl]
                th, tw = len(spr), len(spr[0])
                sh = max(2, int(fb_h * 0.62 / dist))
                sw = max(2, int(sh * tw / th * 1.05))
                sh = max(2, int(sh * b.scale))
                sw = max(2, int(sw * b.scale))
                sx_c = int((0.5 + diff / FOV) * fb_w)
                bottom = int(fb_h / 2 + fb_h / (2 * dist))
                self._blit_billboard(fb, zbuf, spr, dist, sx_c, bottom, sh, sw)
            elif tag == "pickup":
                p = obj
                dx, dy = p.x - px, p.y - py
                dist = max(math.hypot(dx, dy), 0.2)
                diff = angle_diff(math.atan2(dy, dx), pa)
                if abs(diff) > FOV / 2 + 0.5:
                    continue
                spr = PICKUP_SPRITES[p.kind][shade_level(dist)]
                th, tw = len(spr), len(spr[0])
                sh = max(2, int(fb_h * 0.30 / dist))
                sw = max(2, int(sh * tw / th))
                sx_c = int((0.5 + diff / FOV) * fb_w)
                bottom = int(fb_h / 2 + fb_h / (2 * dist)) - int(math.sin(now * 3 + p.born) * 2)
                self._blit_billboard(fb, zbuf, spr, dist, sx_c, bottom, sh, sw)
            elif tag == "proj":
                p = obj
                d = PROJ_DEFS[p["kind"]]
                dx, dy = p["x"] - px, p["y"] - py
                dist = max(math.hypot(dx, dy), 0.2)
                diff = angle_diff(math.atan2(dy, dx), pa)
                if abs(diff) > FOV / 2 + 0.5:
                    continue
                frames = PROJ_SPRITES.get(p["kind"])
                if frames:  # sprite-billboarded projectile (grenades)
                    spr = frames[int(now * 8) % 2][shade_level(dist)]
                    th, tw = len(spr), len(spr[0])
                    sh = max(2, int(fb_h * 0.12 / dist))
                    sw = max(2, int(sh * tw / th))
                    sx_c = int((0.5 + diff / FOV) * fb_w)
                    bottom = int(fb_h / 2 + fb_h / (2 * dist))
                    self._blit_billboard(fb, zbuf, spr, dist, sx_c, bottom, sh, sw)
                    continue
                r = max(1, int(self.fb_h * d["size"] / dist))
                sx_c = int((0.5 + diff / FOV) * fb_w)
                cy_p = int(self.fb_h / 2 + self.fb_h / (2 * dist) - self.fb_h * 0.18 / dist)
                color, core = d["color"], d["core"]
                for x in range(max(0, sx_c - r), min(fb_w, sx_c + r + 1)):
                    if zbuf[x] <= dist - 0.1:
                        continue
                    for y in range(max(0, cy_p - r), min(fb_h, cy_p + r + 1)):
                        fb[y][x] = color
                if 0 <= sx_c < fb_w and 0 <= cy_p < fb_h and zbuf[sx_c] > dist - 0.1:
                    fb[cy_p][sx_c] = core

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
        normal, fire = GUN_SPRITES_BY_WEAPON[g.weapon]
        gun = fire if g.muzzle > 0 else normal
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
        now = time.time()
        hp = max(0, int(g.hp))
        segs = hp // 10
        if now < g.invuln_until and int(now * 4) % 2:
            bar_c = pack(255, 200, 60)  # invulnerable: blink gold
        elif hp > 60:
            bar_c = pack(80, 220, 80)
        elif hp > 30:
            bar_c = pack(235, 200, 60)
        else:
            bar_c = pack(240, 70, 60)
        dim = self.fg(pack(110, 110, 120))
        bar = self.fg(bar_c) + "█" * segs + self.fg(pack(50, 50, 56)) + "░" * (10 - segs)
        s = (f" {self.fg(pack(255, 210, 70))}☠ KILLS {g.score:<4}"
             f"{dim}│ {self.fg(pack(240, 80, 80))}♥ {bar}{self.fg(bar_c)} {hp:>3}"
             f" {dim}│ NEXT MILESTONE {self.fg(pack(120, 200, 255))}{g.next_milestone()}"
             f" {dim}│ {self.fg(pack(200, 200, 120))}{WEAPONS[g.weapon]['name']}"
             f" {dim}│ {self.fg(pack(160, 220, 120))}◉ x{g.grenades}")
        if self.cols >= 90:
            for until, tag, col in ((g.quad_until, "Q", pack(190, 70, 255)),
                                    (g.speed_until, "S", pack(255, 220, 40)),
                                    (g.invuln_until, "I", pack(255, 200, 60))):
                if now < until:
                    s += f" {self.fg(col)}{tag}{int(until - now) + 1}"
        return s

    def help_line(self, fps):
        dim = self.fg(pack(95, 95, 105))
        return f"{dim} WASD move · mouse/◄► aim · click/SPACE fire · G nade · 1-3 weapon · Q quit{' ' * 8}{fps:>2.0f} fps"


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

def world_to_screen(rnd, g, x, y):
    """Project a world point to (screen_x, sprite_bottom_y, dist, visible)."""
    dx, dy = x - g.px, y - g.py
    dist = max(math.hypot(dx, dy), 0.2)
    diff = angle_diff(math.atan2(dy, dx), g.pa)
    sx = (0.5 + diff / FOV) * rnd.fb_w
    bottom = rnd.fb_h / 2 + rnd.fb_h / (2 * dist)
    visible = abs(diff) < FOV / 2 + 0.5
    return sx, bottom, dist, visible


def kill_burst(rnd, g, bug, dist):
    sx, bottom, _, _ = world_to_screen(rnd, g, bug.x, bug.y)
    sh = rnd.fb_h * 0.62 / dist
    sy = bottom - sh / 2
    if time.time() < g.quad_until:  # quad damage: everything splatters purple
        goo = [pack(190, 70, 255), pack(120, 40, 180), pack(240, 180, 255), pack(255, 255, 255)]
    else:
        goo = KIND_GOO.get(bug.kind,
                           [pack(110, 220, 70), pack(70, 170, 50), pack(180, 240, 120), pack(255, 220, 80)])
    n = max(8, int(30 / (1 + dist * 0.3)))
    for _ in range(n):
        a = random.uniform(0, TAU)
        sp = random.uniform(6, 40) / (1 + dist * 0.15)
        g.particles.append([sx, sy, math.cos(a) * sp, math.sin(a) * sp - 12,
                            random.uniform(0.3, 0.8), random.choice(goo)])


def explosion_burst(rnd, g, x, y, n=35, colors=None):
    if colors is None:
        colors = [pack(255, 200, 60), pack(255, 140, 40), pack(255, 255, 200), pack(120, 120, 120)]
    sx, bottom, dist, visible = world_to_screen(rnd, g, x, y)
    if not visible:
        return
    sy = bottom - (rnd.fb_h * 0.4 / dist) / 2
    for _ in range(n):
        a = random.uniform(0, TAU)
        sp = random.uniform(6, 46) / (1 + dist * 0.15)
        g.particles.append([sx, sy, math.cos(a) * sp, math.sin(a) * sp - 12,
                            random.uniform(0.3, 0.8), random.choice(colors)])


BOOMER_BURST = [pack(255, 200, 60), pack(255, 120, 40), pack(255, 255, 200), pack(120, 30, 20)]


def handle_kill_fx(rnd, g, bug, dist, now):
    """Per-kill effects shared by every kill source (gunfire, boomer chains,
    grenades): splatter, drop roll, boomer chain detonations, boss bonus.
    Returns True if a boss died somewhere in the chain."""
    boss_died = bug.kind == "boss"
    if boss_died:
        g.score += 9  # boss is worth 10 total; the kill itself scored 1
        kill_burst(rnd, g, bug, dist)
    kill_burst(rnd, g, bug, dist)
    if len(g.pickups) < 4 and random.random() < 0.18:
        kind = random.choices(PICKUP_KINDS, weights=PICKUP_WEIGHTS)[0]
        g.pickups.append(Pickup(bug.x, bug.y, kind, now))
    if bug.kind == "boomer":  # a dead boomer is a free grenade
        kills = g.detonate_boomer(bug.x, bug.y, now)
        explosion_burst(rnd, g, bug.x, bug.y, n=70, colors=BOOMER_BURST)
        for kb, kd in kills:
            if handle_kill_fx(rnd, g, kb, max(kd, 0.3), now):
                boss_died = True
    return boss_died


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
                record_game_end(g)
                return "quit"
            time.sleep(0.2)
            continue

        want_fire = False
        want_nade = False
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
                elif k[0] == "rclick":
                    mouse_x = k[1]
                    want_nade = True
                elif k[0] == "scroll":
                    g.walk(0.22 * k[1], 0)
            elif k == "q":
                record_game_end(g)
                return "quit"
            elif k == "g":
                want_nade = True
            elif k in ("1", "2", "3"):
                g.weapon = int(k) - 1
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

        boss_died = False
        kills_before = sum(g.kills_by_kind.values())

        if want_fire and now - g.last_shot >= WEAPONS[g.weapon]["cooldown"]:
            for bug, dist, killed in g.shoot(now):
                if killed and handle_kill_fx(rnd, g, bug, dist, now):
                    boss_died = True

        g.nade_cd = max(0.0, g.nade_cd - dt)
        if want_nade and g.grenades > 0 and g.nade_cd <= 0:
            g.projectiles.append(dict(kind="nade", x=g.px, y=g.py,
                                      vx=math.cos(g.pa) * 7.0, vy=math.sin(g.pa) * 7.0,
                                      age=0.0, fuse=1.1))
            g.grenades -= 1
            g.nade_cd = 0.8

        # boss: warn for 2s, then spawn at the floor cell farthest away
        boss_alive = any(b.kind == "boss" for b in g.bugs)
        if g.score >= g.next_boss and not boss_alive and g.boss_warn <= 0:
            g.boss_warn = 2.0
            g.next_boss += 100  # bosses at 50, 150, 250...
        if g.boss_warn > 0:
            g.boss_warn -= dt
            if g.boss_warn <= 0:
                best = None
                for cy in range(MAP_H):
                    for cx in range(MAP_W):
                        if MAP[cy][cx] == ".":
                            d = math.hypot(cx + 0.5 - g.px, cy + 0.5 - g.py)
                            if best is None or d > best[0]:
                                best = (d, cx + 0.5, cy + 0.5)
                b = Bug(best[1], best[2], "boss")
                b.hp = b.max_hp = 30 + 10 * ((g.next_boss - 100) // 100)
                b.summon_cd = 6.0
                g.bugs.append(b)
                boss_alive = True

        g.spawn_timer -= dt
        cap = min(5 + g.score // 8, 18)
        if g.spawn_timer <= 0 and g.boss_warn <= 0:
            normal = sum(1 for b in g.bugs if b.kind != "boss")
            if normal < cap and (not boss_alive or len(g.bugs) < 22):
                g.spawn_bug()
                g.spawn_timer = max(0.6, 2.5 - g.score * 0.02)

        for x, y, kills in g.update_bugs(dt, now):  # boomer fuse detonations
            explosion_burst(rnd, g, x, y, n=70, colors=BOOMER_BURST)
            for kb, kd in kills:
                if handle_kill_fx(rnd, g, kb, max(kd, 0.3), now):
                    boss_died = True

        # grenades cook off early when a bug wanders into the blast heart
        for p in g.projectiles:
            if p["kind"] == "nade" and p["fuse"] is not None and p["fuse"] > 0:
                nx, ny = p["x"], p["y"]
                for b in g.bugs:
                    if (b.x - nx) ** 2 + (b.y - ny) ** 2 < 0.49:
                        p["fuse"] = 0.0
                        break

        for ev, p in g.update_projectiles(dt, now):
            if ev == "hit_player":
                g.hurt(PROJ_DEFS[p["kind"]]["player_damage"], now)
            elif ev == "fuse" and p["kind"] == "nade":
                kills = g.explode_at(p["x"], p["y"], now, bug_damage=3, bug_radius=2.6,
                                     player_damage=12, player_radius=1.6)
                explosion_burst(rnd, g, p["x"], p["y"], n=40,
                                colors=[pack(255, 220, 80), pack(255, 140, 40), pack(120, 120, 120)])
                for kb, kd in kills:
                    if handle_kill_fx(rnd, g, kb, max(kd, 0.3), now):
                        boss_died = True

        # earn a grenade every 15 kills (counter-based: robust to multi-kills)
        g.kills_since_nade += sum(g.kills_by_kind.values()) - kills_before
        while g.kills_since_nade >= 15:
            g.kills_since_nade -= 15
            g.grenades = min(4, g.grenades + 1)

        # pickups: expire, then collect
        g.pickups = [p for p in g.pickups if now - p.born < 12.0]
        kept = []
        for p in g.pickups:
            if math.hypot(p.x - g.px, p.y - g.py) < 0.6:
                if p.kind == "health":
                    g.hp = min(100.0, g.hp + 25)
                elif p.kind == "quad":
                    g.quad_until = now + 8.0
                elif p.kind == "speed":
                    g.speed_until = now + 8.0
                elif p.kind == "invuln":
                    g.invuln_until = now + 6.0
            else:
                kept.append(p)
        g.pickups = kept

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
            record_game_end(g)
            res = game_over(term, rnd, g)
            if res == "restart":
                g = Game()
                rnd.set_theme(0)
                last = time.time()
                continue
            return "quit"

        # crossing-based milestone detection (boss kills jump score by 10)
        cands = [m for m in MILESTONES if g.score >= m and m not in g.celebrated]
        h = (g.score // 100) * 100
        if h >= 200 and h not in g.celebrated:
            cands.append(h)
        if cands:
            m = max(cands)
            g.celebrated.add(m)
            title, sub = MILESTONES.get(m) or ("UNSTOPPABLE!", f"{g.score} BUGS AND COUNTING")
            if m >= 100 and m % 100 == 0:  # every 100 kills: rotate the arena
                idx = (m // 100) % len(THEMES)
                rnd.set_theme(idx)
                sub = f"ENTERING {THEMES[idx]['name']}"
            celebrate(term, rnd, g, title, sub, grand=m >= 100)
            last = time.time()
        elif boss_died:
            celebrate(term, rnd, g, "BOSS SQUASHED!", f"{g.score} KILLS", grand=False)
            last = time.time()

        fb = rnd.render_world(g, now)
        if g.boss_warn > 0 and int(now * 6) % 2:  # incoming-boss klaxon
            wc = pack(180, 30, 30)
            fbw, fbh = rnd.fb_w, rnd.fb_h
            for y in (0, 1, fbh - 2, fbh - 1):
                fb[y] = [wc] * fbw
            for row in fb:
                row[0] = row[1] = row[fbw - 2] = row[fbw - 1] = wc
            draw_text(fb, fbw, fbh, "HUGE BUG INCOMING", fbh // 4, 1, pack(255, 80, 60))
        boss = next((b for b in g.bugs if b.kind == "boss"), None)
        if boss:  # boss HP bar across the top
            fbw = rnd.fb_w
            bw = int(fbw * 0.6)
            x0 = (fbw - bw) // 2
            border, fill, empty = pack(10, 10, 10), pack(220, 40, 200), pack(40, 16, 40)
            fillw = int(bw * boss.hp / boss.max_hp)
            r2, r3, r4 = fb[2], fb[3], fb[4]
            for x in range(bw):
                r2[x0 + x] = r4[x0 + x] = border
                r3[x0 + x] = fill if x < fillw else empty
            if x0 > 0:
                r3[x0 - 1] = border
            if x0 + bw < fbw:
                r3[x0 + bw] = border
            draw_text(fb, fbw, rnd.fb_h, "BOSS", 6, 1, pack(230, 150, 255))
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
            save_data = load_save()
            PREV_HIGH[0] = save_data.get("high_kills", 0)
            play(term, rnd)
    except KeyboardInterrupt:
        pass
    print("Thanks for playing BUG DOOM. The codebase is safe... for now.")


if __name__ == "__main__":
    main()
