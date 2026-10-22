# Iris's room

The three.js page that shows Iris and her room. It runs on its own for animation work and will be hosted inside the Qt window (`ui/UI Phase 3.md`).

## Run it

```
python -m ui.room.serve 8790
```

from the repo root, or double-click `start.cmd`. The page is at `http://127.0.0.1:8790/`. Assets stay in the folder named by `ui.room.assets` in `core/config.json` and are served under `/assets/`, with `assets/manifest.json` listing every GLB in it.

## Add things

- **An idle:** export `Motion/Idle/<Name>.glb`, plus `Home to <Name>.glb` and `<Name> To Home.glb` anywhere under `Motion/`. The page builds the activity, a mirrored twin and the pool entry. One motion per export file, or name the clip in `avatar.json` under `clips`.
- **An outfit:** drop a GLB in `Clothes/Outfits/`. Shoes with a `-HighHeels` motion variant are picked up by name.
- **A gesture or expression:** add it to `gestures` or `expressions` in `avatar.json`.
- **Anything with special needs** (couch motions, walks, turns): an entry in `clips` and `activities` in `avatar.json`, as today.

## Drive her

`window.avatar` is the surface a host uses; `window.avatarDev` is the whole context for the panel and the console.

| Call | Does |
|---|---|
| `ready` | Promise that resolves when she is loaded |
| `capabilities()` | activities, idles, gestures, expressions, outfits, spots, turns, each `{ key, label }` |
| `describe()` | activity, phase, motion, plan, busy, position, outfit, listening, speaking, voice, camera |
| `do(activity)` | sit, lie, dance, idle, home, or an idle by name; the walk to the furniture is planned for her |
| `home()` | walk back to the home spot |
| `goTo(spot | { x, z }, style?)` | walk somewhere |
| `turn(direction, degrees?)` | left, right, around |
| `gesture(name)` | nod, shake, tilt, laugh, ... |
| `express(name, weight = 1)` / `neutral()` | one expression at a time |
| `outfit(name)` | change clothes |
| `setState({ listening, speaking, thinking })` | body language from Iris's state |
| `speechLevel(0..1)` | mouth level while speaking, send 20 to 30 a second |
| `voice(on)` | voice mode: walks to the work spot and holds the voice camera |
| `camera(mode)` | orbit, follow, room |
| `command(text)` | the dev command line |
| `on(event, fn)` | events below; returns an unsubscribe |

Events: `ready`, `log { message, isError }`, `motion { name, activity }`, `plan [steps]`, `planDone step`, `state`, `speaking`, `listening`, `outfit key`, `hud text`, `progress { loaded, total }`, `loadError message`.

## Check her without a browser

```
node ui/room/harness.mjs http://127.0.0.1:8790/ 9222 "sit@8;home@8;js:window.avatar.describe()@0"
```

Loads the page in headless Chrome, runs each command, waits the given seconds and prints the HUD and log. `js:` steps evaluate an expression. `DETERMINISTIC=1` seeds the random generator, fixes the clock at 30 frames a second and waits in frames, so two runs of the same code give the same output; it is slow under software rendering.
