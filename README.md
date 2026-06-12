# BUG DOOM

A Doom-style raycasting shooter that runs entirely in your terminal. Bugs have
infested the codebase — squash them all. The game is endless; celebrations fire
at 10, 20, 50, and a grand finale at 100 kills (then every 100 after that).

No dependencies — Python 3 standard library only.

## Run

```sh
python3 bug_doom.py
```

Works best in a terminal at least 80x24. Bigger window = better view.

## Controls

| Key          | Action                  |
| ------------ | ----------------------- |
| `W` / `S`    | Move forward / back     |
| `A` / `D`    | Strafe left / right     |
| `←` / `→`    | Turn                    |
| `Space`      | Fire                    |
| `Q`          | Quit                    |
| `R`          | Respawn (after death)   |

## Gameplay

- Bugs spawn endlessly and chase you; spawn rate and speed scale with your kill count.
- Line up a bug with the `+` crosshair and fire. Walls block shots.
- After 25 kills, purple **tank bugs** appear — they take 3 hits.
- Bugs bite when adjacent. Health regenerates slowly if you avoid bites for a few seconds.
- Hit 0 HP and you got debugged — press `R` to respawn.
