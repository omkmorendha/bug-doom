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
| `Q`            | Quit                            |
| `R`            | Respawn (after death)           |

## Gameplay

- Bugs spawn endlessly and chase you; spawn rate and speed scale with your kill count.
- Line up a bug with the crosshair and fire. Walls block shots.
- After 25 kills, purple **tank bugs** appear — they take 3 hits.
- Bugs bite when adjacent. Health regenerates slowly if you avoid bites for a few seconds.
- Hit 0 HP and you got debugged — press `R` to respawn.
