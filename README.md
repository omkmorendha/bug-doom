# BUG DOOM

A Doom-style raycasting shooter that runs entirely in your terminal — with
**real textured pixel graphics**. The renderer uses Unicode half-blocks (`▀`)
so every character cell becomes two RGB pixels, drawn with ANSI truecolor
escapes. Procedural brick / tech / moss wall textures, floor casting, distance
fog, animated pixel-art bug sprites, a pump shotgun, and particle splatter.

Bugs have infested the codebase — squash them all. The game is endless;
confetti-and-fireworks celebrations fire at 10, 20, 50, and a grand finale at
100 kills (then every 100 after that).

No dependencies — Python 3 standard library only.

## Run

```sh
python3 bug_doom.py
```

- Best in a truecolor terminal (iTerm2, Kitty, WezTerm, Ghostty, recent
  VS Code). Falls back to 256 colors elsewhere (e.g. macOS Terminal.app).
- Bigger window = higher resolution. 120×35 or more looks great.

## Controls

| Input          | Action                          |
| -------------- | ------------------------------- |
| `W` / `S`      | Move forward / back             |
| `A` / `D`      | Strafe left / right             |
| `←` / `→`      | Turn                            |
| Mouse move     | Aim                             |
| Left click     | Fire (hold + drag to spray)     |
| Scroll wheel   | Walk forward / back             |
| `Space`        | Fire                            |
| `G` / right-click | Throw bug bomb               |
| `1` / `2` / `3`| Switch weapon (pistol / shotgun / SMG) |
| `M`            | Toggle minimap                  |
| `Q`            | Quit                            |
| `R`            | Respawn (after death)           |

## Gameplay

- Bugs spawn endlessly and chase you; spawn rate and speed scale with your kill count.
- Line up a bug with the crosshair and fire. Walls block shots.
- Three weapons: pistol = accurate long range, shotgun = close-range burst,
  SMG = hold-fire spray (hold click-drag or space to stream shots).
- Squashed bugs sometimes drop pickups — walk over them. Four kinds: health
  packs (+25), **Quad Damage** (8s of 4x), **Speed Boost** (8s of 1.6x), and
  **Invincibility** (6s, gold-blinking HP bar).
- Bug bombs: `G` or right-click lobs a grenade that skids to rest and
  detonates — +1 grenade every 15 kills, max 4. Watch the self-damage.
- After 25 kills, purple **tank bugs** appear — they take 3 hits.
- At 12 kills, packs of fast yellow **skitterers** join the hunt — they flank.
- At 18 kills, orange **spitters** lob acid from range — strafe to dodge.
- At 30 kills, red **boomers** charge and explode — shoot them at a distance,
  ideally inside the swarm.
- Every 100 kills teleports the arena to a new zone — Inferno, Cryo, the Sewers.
- Corpses pile up — wade through the goo.
- Bugs bite when adjacent. Health regenerates slowly if you avoid bites for a few seconds.
- Hit 0 HP and you got debugged — press `R` to respawn.

## Bosses

Every 100 kills starting at 50 (50, 150, 250...), the borders flash red, the
arena warns **HUGE BUG INCOMING**, and a huge pink boss bug spawns at the far
side of the map. Bosses soak 30+ hits (10 more per appearance), bite for 16,
and periodically summon a ring of grunts. A magenta HP bar tracks the fight;
squashing the boss is worth 10 kills and its own celebration.
