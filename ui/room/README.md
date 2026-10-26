# Iris's room

The three.js page that shows Iris and her room. It runs on its own for animation work and will be hosted inside the Qt window (`ui/UI Phase 3.md`).

## Run it

```
python -m ui.room.serve 8790
```

from the repo root, or double-click `start.cmd`. The page is at `http://127.0.0.1:8790/?dev`. Assets stay in the folder named by `ui.room.assets` in `core/config.json` and are served under `/assets/`, with `assets/manifest.json` listing every GLB in it.

## Inside Iris

The Qt window (`ui/main.py`) opens on the room whenever `ui.room` is in the config. `ui/room_view.py` serves this folder and the assets to a `QWebEngineView` through the `iris-room://` scheme, with no server, and talks to the page over `QWebChannel`:

- The page sees the host as `avatarDev.host` (`host.js` connects it): it reports `pageReady`, `progress`, `log`, `loadError` and `openChat`, and receives `stateChanged`, `speechLevel`, `commandRequested` and `viewChanged`.
- Only the room camera is used; the test panel and HUD are hidden. Ctrl+Shift+D shows them, as does `?dev` on the URL or `"dev": true` under `ui.room`.
- Clicking the monitor, or pressing Enter, opens the chat. **Back to room** in the chat header, or Esc in an empty composer, goes back.
- The page is loaded once at startup and stays loaded.
- `register_room_scheme()` also sets `QSG_RHI_BACKEND=opengl` unless it is already set, and the renderer keeps its drawing buffer (`preserveDrawingBuffer` in scene.js). With Qt's default Direct3D 11 backend the WebGL canvas drops out for a frame every few seconds (measured 2026-10-26 by grabbing the screen at 30 fps: 5 blank frames in 40 s on D3D11, 1 in 60 s on OpenGL, none in 60 s on OpenGL with the buffer preserved).

## Rooms

`avatar.json` is Iris: her character, clips, gestures, idle life and walking. The room she is in is a separate file named by its `room` key, `rooms/office.json` today. A room file holds the GLB and everything that only makes sense in that room:

- `file`, `floor` (the object she stands on; the room is lowered so its top is at her feet), `walls` (a name pattern that gives the room's extent), `monitor` (a name pattern for the screen she works at; it brightens under the pointer and a click emits `monitorClicked`), `hide`, `noShadowFrom`.
- `camera` and the door timings `doorWatch`, `doorLeave`, `doorSwing`.
- `spots`: `home` (where she stands and works), `door` (just inside the door she uses), `away` (where she waits outside), `closeup`. Each is an offset from an `anchor` object plus a `facing` in degrees.
- `furniture`: approach points for sit and lie, such as `"couch": { "anchor": "Couch_Entry", "alignClip": "sit", "facing": 0 }`.
- `passable`: objects she may walk through, such as doors.

A new room is a new file with the same keys; leave out what the room does not have.

## Add things

- **An idle:** export `Motion/Idle/<Name>.glb`, plus `Home to <Name>.glb` and `<Name> To Home.glb` anywhere under `Motion/`. The page builds the activity, a mirrored twin and the pool entry. One motion per export file, or name the clip in `avatar.json` under `clips`.
- **An outfit:** drop a GLB in `Clothes/Outfits/`. Shoes with a `-HighHeels` motion variant are picked up by name.
- **A gesture or expression:** add it to `gestures` or `expressions` in `avatar.json`.
- **Anything with special needs** (couch motions, walks, turns): an entry in `clips` and `activities` in `avatar.json`, as today, plus the furniture anchor in the room file.

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
| `voice(on)` | work mode: she comes in through the door to her office spot and stays there; off walks her back out |
| `camera(mode)` | orbit, follow, room |
| `command(text)` | the dev command line |
| `on(event, fn)` | events below; returns an unsubscribe |

Events: `ready`, `monitorClicked`, `log { message, isError }`, `motion { name, activity }`, `plan [steps]`, `planDone step`, `state`, `speaking`, `listening`, `outfit key`, `hud text`, `progress { loaded, total }`, `loadError message`.

## Check her without a browser

```
node ui/room/harness.mjs http://127.0.0.1:8790/ 9222 "sit@8;home@8;js:window.avatar.describe()@0"
```

Loads the page in headless Chrome, runs each command, waits the given seconds and prints the HUD and log. `js:` steps evaluate an expression. `DETERMINISTIC=1` seeds the random generator, fixes the clock at 30 frames a second and waits in frames, so two runs of the same code give the same output; it is slow under software rendering.
