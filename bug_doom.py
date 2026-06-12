#!/usr/bin/env python3
"""BUG DOOM — a terminal raycasting shooter. Squash the bugs, save the codebase.

Controls:
    W / S        move forward / back
    A / D        strafe left / right
    LEFT / RIGHT turn
    SPACE        fire
    Q            quit

Run:  python3 bug_doom.py
"""

import curses
import locale
import math
import random
import time

locale.setlocale(locale.LC_ALL, "")

MAP = [
    "################################",
    "#..............#...............#",
    "#..####........#......####.....#",
    "#..#...........#......#........#",
    "#..#...####....#......#....##..#",
    "#..............#...........##..#",
    "#.......#......#...............#",
    "#.......#......####....#####...#",
    "#.......#..................#...#",
    "#.......#...................#..#",
    "#..####.....................#..#",
    "#..#........####............#..#",
    "#..#...........#......##....#..#",
    "#..#...........#......##.......#",
    "#......###.....#...............#",
    "#......#.......#.....######....#",
    "#......#.............#.........#",
    "#..........##........#.....##..#",
    "#..........##..................#",
    "################################",
]
assert all(len(r) == len(MAP[0]) for r in MAP), "map rows must be equal width"

MAP_W, MAP_H = len(MAP[0]), len(MAP)
FOV = math.pi / 3
MAX_DEPTH = 24.0
FIRE_COOLDOWN = 0.22
MUZZLE_TIME = 0.08
PLAYER_START = (16.5, 9.5, 0.0)  # x, y, angle

MILESTONES = {
    10: ("PEST CONTROL!", "10 bugs squashed"),
    20: ("EXTERMINATOR!", "20 bugs flattened"),
    50: ("DEBUG MASTER!", "50 bugs obliterated"),
    100: ("*** CODE IS CLEAN ***", "100 BUGS DESTROYED — LEGENDARY"),
}

FLOOR_CHARS = "  ..,,::"
CONFETTI = "*+o~'^x%#@!."


def is_wall(x, y):
    xi, yi = int(x), int(y)
    if 0 <= yi < MAP_H and 0 <= xi < MAP_W:
        return MAP[yi][xi] == "#"
    return True


def cast_ray(px, py, ang):
    """DDA raycast. Returns (distance, side) where side=1 means a y-facing wall."""
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
        if is_wall(map_x, map_y):
            dist = (side_x - delta_x) if side == 0 else (side_y - delta_y)
            return max(dist, 0.01), side
    return MAX_DEPTH, 0


def angle_diff(a, b):
    return (a - b + math.pi) % (2 * math.pi) - math.pi


def sprite_for(h):
    if h <= 2:
        return ["}o{"]
    if h <= 4:
        return ["\\../",
                "(oo)"]
    if h <= 8:
        return ["\\\\ //",
                " \\V/ ",
                "(o.o)",
                "/|||\\"]
    return [" \\\\   // ",
            "  \\\\.//  ",
            " /=====\\ ",
            "( o   o )",
            " \\=====/ ",
            " //|||\\\\ "]


class Bug:
    def __init__(self, x, y, tank=False):
        self.x, self.y = x, y
        self.tank = tank
        self.hp = 3 if tank else 1
        self.phase = random.uniform(0, math.tau)
        self.bite_cd = 0.0
        self.flash = 0.0


class Game:
    def __init__(self):
        self.px, self.py, self.pa = PLAYER_START
        self.hp = 100.0
        self.score = 0
        self.bugs = []
        self.spawn_timer = 0.0
        self.last_shot = -1.0
        self.muzzle = 0.0
        self.splat = 0.0
        self.bite_flash = 0.0
        self.last_bite = -10.0
        self.celebrated = set()

    # --- movement -------------------------------------------------------
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

    # --- bugs -----------------------------------------------------------
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
            wall_d, _ = cast_ray(self.px, self.py, ang)
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
                self.splat = 0.35
                return True
        return False

    def next_milestone(self):
        for m in sorted(MILESTONES):
            if self.score < m:
                return m
        return ((self.score // 100) + 1) * 100


# --- rendering ------------------------------------------------------------

def put(buf, y, x, ch, attr):
    if 0 <= y < len(buf) and 0 <= x < len(buf[0]):
        buf[y][x] = (ch, attr)


def put_text(buf, y, x, text, attr):
    for i, ch in enumerate(text):
        put(buf, y, x + i, ch, attr)


def flush(stdscr, buf):
    for y, row in enumerate(buf):
        x = 0
        n = len(row)
        while x < n:
            attr = row[x][1]
            j = x + 1
            while j < n and row[j][1] == attr:
                j += 1
            s = "".join(c for c, _ in row[x:j])
            try:
                stdscr.addstr(y, x, s, attr)
            except curses.error:
                pass
            x = j
    stdscr.refresh()


def wall_char(dist, side):
    if dist < 3:
        ch = "█"
    elif dist < 6:
        ch = "▓"
    elif dist < 10:
        ch = "▒"
    elif dist < 16:
        ch = "░"
    else:
        ch = "·"
    return ch


def render(stdscr, g, colors, now):
    H, W = stdscr.getmaxyx()
    W = max(2, W - 1)
    if W < 40 or H < 14:
        stdscr.erase()
        try:
            stdscr.addstr(0, 0, "Terminal too small — need at least 40x14")
        except curses.error:
            pass
        stdscr.refresh()
        return

    horizon = H // 2
    wall_attr = colors["white"]
    floor_attr = colors["green"] | curses.A_DIM

    buf = []
    for y in range(H):
        if y <= horizon:
            buf.append([(" ", 0)] * W)
        else:
            t = (y - horizon) / max(1, H - horizon)
            ch = FLOOR_CHARS[min(len(FLOOR_CHARS) - 1, int(t * len(FLOOR_CHARS)))]
            buf.append([(ch, floor_attr)] * W)

    # walls
    zbuf = [MAX_DEPTH] * W
    for col in range(W):
        ray = g.pa - FOV / 2 + FOV * col / W
        dist, side = cast_ray(g.px, g.py, ray)
        dist *= math.cos(ray - g.pa)  # fisheye correction
        dist = max(dist, 0.05)
        zbuf[col] = dist
        wall_h = min(H * 4, int(H / dist))
        top = (H - wall_h) // 2
        ch = wall_char(dist, side)
        attr = wall_attr | (curses.A_DIM if side else 0)
        for y in range(max(0, top), min(H, top + wall_h)):
            buf[y][col] = (ch, attr)

    # bugs (far to near)
    for b in sorted(g.bugs, key=lambda b: -math.hypot(b.x - g.px, b.y - g.py)):
        dx, dy = b.x - g.px, b.y - g.py
        dist = max(math.hypot(dx, dy), 0.2)
        diff = angle_diff(math.atan2(dy, dx), g.pa)
        if abs(diff) > FOV / 2 + 0.4:
            continue
        sx = int((0.5 + diff / FOV) * W)
        h = int(H / dist)
        art = sprite_for(h)
        if b.flash > 0:
            attr = colors["red"] | curses.A_BOLD
        elif b.tank:
            attr = colors["magenta"] | curses.A_BOLD
        else:
            attr = colors["green"] | curses.A_BOLD
        bottom = min(H - 1, (H + min(h, H)) // 2)
        for r, line in enumerate(art):
            y = bottom - (len(art) - 1 - r)
            x0 = sx - len(line) // 2
            for c, ch in enumerate(line):
                x = x0 + c
                if ch != " " and 0 <= x < W and dist < zbuf[x]:
                    put(buf, y, x, ch, attr)

    # crosshair + muzzle flash
    cy, cx = H // 2, W // 2
    if g.muzzle > 0:
        put_text(buf, cy - 1, cx - 1, "\\|/", colors["yellow"] | curses.A_BOLD)
        put_text(buf, cy, cx - 1, "-✶-", colors["yellow"] | curses.A_BOLD)
    else:
        put(buf, cy, cx, "+", colors["yellow"] | curses.A_BOLD)
    if g.splat > 0:
        put_text(buf, cy + 2, cx - 3, "*SPLAT*", colors["yellow"] | curses.A_BOLD)

    # gun
    gun = [" \\|/ ", " ███ ", "█████"] if g.muzzle > 0 else ["  ▲  ", " ███ ", "█████"]
    gattr = colors["cyan"] | (curses.A_BOLD if g.muzzle > 0 else 0)
    for r, line in enumerate(gun):
        put_text(buf, H - 3 + r, cx - len(line) // 2, line, gattr)

    # HUD
    hp = max(0, int(g.hp))
    bar = "█" * (hp // 10) + "·" * (10 - hp // 10)
    hud = f" KILLS {g.score}  │  HP [{bar}] {hp}  │  NEXT MILESTONE {g.next_milestone()}  │  Q quit "
    put_text(buf, 0, max(0, (W - len(hud)) // 2), hud[:W], colors["white"] | curses.A_REVERSE)
    if g.bite_flash > 0:
        put_text(buf, H - 5, cx - 7, "!! BUG BITE !!", colors["red"] | curses.A_BOLD | curses.A_BLINK)

    flush(stdscr, buf)


# --- overlays --------------------------------------------------------------

def draw_box(stdscr, lines, attrs, H, W):
    bw = max(len(s) for s in lines) + 6
    by = max(0, H // 2 - len(lines) // 2 - 1)
    bx = max(0, (W - bw) // 2)
    border = attrs[0]
    try:
        stdscr.addstr(by, bx, "╔" + "═" * (bw - 2) + "╗", border)
        for i, line in enumerate(lines):
            pad = (bw - 2 - len(line))
            text = " " * (pad // 2) + line + " " * (pad - pad // 2)
            stdscr.addstr(by + 1 + i, bx, "║" + text + "║", attrs[min(i, len(attrs) - 1)])
        stdscr.addstr(by + 1 + len(lines), bx, "╚" + "═" * (bw - 2) + "╝", border)
    except curses.error:
        pass


def celebrate(stdscr, colors, title, sub, kills, grand=False):
    rainbow = [colors[c] for c in ("red", "yellow", "green", "cyan", "blue", "magenta")]
    duration = 4.5 if grand else 2.4
    start = time.time()
    while stdscr.getch() != -1:
        pass
    while time.time() - start < duration:
        H, W = stdscr.getmaxyx()
        stdscr.erase()
        n = (W * H) // (4 if grand else 8)
        for _ in range(n):
            y, x = random.randrange(max(1, H - 1)), random.randrange(max(1, W - 1))
            attr = random.choice(rainbow) | (curses.A_BOLD if random.random() < 0.5 else 0)
            try:
                stdscr.addstr(y, x, random.choice(CONFETTI), attr)
            except curses.error:
                pass
        tcolor = random.choice(rainbow) if grand else colors["yellow"]
        lines = ["", title, sub, "", f"TOTAL KILLS: {kills}", ""]
        if grand:
            lines.insert(1, "★ ★ ★ ★ ★")
            lines.append("★ ★ ★ ★ ★")
        draw_box(stdscr, lines, [tcolor | curses.A_BOLD], H, W)
        stdscr.refresh()
        if time.time() - start > 0.6 and stdscr.getch() != -1:
            break
        curses.napms(60)
    while stdscr.getch() != -1:
        pass


def game_over(stdscr, colors, kills):
    while stdscr.getch() != -1:
        pass
    while True:
        H, W = stdscr.getmaxyx()
        stdscr.erase()
        draw_box(stdscr, [
            "",
            "Y O U   G O T   D E B U G G E D",
            "",
            f"Final kills: {kills}",
            "",
            "[R] respawn      [Q] quit",
            "",
        ], [colors["red"] | curses.A_BOLD], H, W)
        stdscr.refresh()
        k = stdscr.getch()
        if k in (ord("r"), ord("R")):
            return "restart"
        if k in (ord("q"), ord("Q")):
            return "quit"
        curses.napms(50)


# --- main loop --------------------------------------------------------------

def play(stdscr, colors):
    g = Game()
    last = time.time()
    while True:
        now = time.time()
        dt = min(now - last, 0.1)
        last = now

        want_fire = False
        while True:
            k = stdscr.getch()
            if k == -1:
                break
            if k in (ord("q"), ord("Q")):
                return "quit"
            elif k in (ord("w"), ord("W")):
                g.walk(0.35, 0)
            elif k in (ord("s"), ord("S")):
                g.walk(-0.35, 0)
            elif k in (ord("a"), ord("A")):
                g.walk(0, -0.3)
            elif k in (ord("d"), ord("D")):
                g.walk(0, 0.3)
            elif k == curses.KEY_LEFT:
                g.pa -= 0.11
            elif k == curses.KEY_RIGHT:
                g.pa += 0.11
            elif k == ord(" "):
                want_fire = True

        if want_fire and now - g.last_shot >= FIRE_COOLDOWN:
            g.shoot(now)

        # spawning
        g.spawn_timer -= dt
        cap = min(4 + g.score // 8, 14)
        if g.spawn_timer <= 0 and len(g.bugs) < cap:
            g.spawn_bug()
            g.spawn_timer = max(0.6, 2.5 - g.score * 0.02)

        g.update_bugs(dt, now)

        # slow regen when not being chewed on
        if now - g.last_bite > 3 and g.hp < 100:
            g.hp = min(100, g.hp + 2 * dt)

        g.muzzle = max(0.0, g.muzzle - dt)
        g.splat = max(0.0, g.splat - dt)
        g.bite_flash = max(0.0, g.bite_flash - dt)

        if g.hp <= 0:
            return game_over(stdscr, colors, g.score)

        # milestone celebrations
        ms = None
        if g.score in MILESTONES and g.score not in g.celebrated:
            ms = (g.score, *MILESTONES[g.score])
        elif g.score >= 200 and g.score % 100 == 0 and g.score not in g.celebrated:
            ms = (g.score, "UNSTOPPABLE!", f"{g.score} bugs and counting")
        if ms:
            g.celebrated.add(ms[0])
            celebrate(stdscr, colors, ms[1], ms[2], g.score, grand=ms[0] >= 100)
            last = time.time()

        render(stdscr, g, colors, now)
        elapsed = time.time() - now
        curses.napms(max(1, int((1 / 30 - elapsed) * 1000)))


def run(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    curses.start_color()
    curses.use_default_colors()
    names = ["white", "green", "red", "yellow", "magenta", "cyan", "blue"]
    consts = [curses.COLOR_WHITE, curses.COLOR_GREEN, curses.COLOR_RED,
              curses.COLOR_YELLOW, curses.COLOR_MAGENTA, curses.COLOR_CYAN,
              curses.COLOR_BLUE]
    colors = {}
    for i, (name, c) in enumerate(zip(names, consts), start=1):
        try:
            curses.init_pair(i, c, -1)
            colors[name] = curses.color_pair(i)
        except curses.error:
            colors[name] = 0
    while True:
        if play(stdscr, colors) == "quit":
            return


if __name__ == "__main__":
    try:
        curses.wrapper(run)
        print("Thanks for playing BUG DOOM. The codebase is safe... for now.")
    except KeyboardInterrupt:
        pass
