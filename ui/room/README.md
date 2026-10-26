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
- The monitor shows a live picture of the chat view: Qt grabs the chat widget within a second of a transcript change, and again whenever the room comes back, and sends it over the bridge as `monitorImage`. The page puts it on the primitive whose material name matches `monitorScreenMaterial` in the room file (default `screen|glass|display`), generating flat UVs if the mesh has none.
- The page is loaded once at startup and stays loaded.
- `ui.room_screen` in the config names the screen Iris fills, by model and serial rather than position. Pick it from the tray menu **Show Iris on**; **Any screen, windowed** clears it and the window opens where it was last closed. If the screen is unplugged she opens windowed on the primary screen and moves back when it returns.
- Qt's scene graph stays on its default Direct3D 11 backend. The OpenGL backend (`QSG_RHI_BACKEND=opengl`) had fewer dropped canvas frames in a small window but renders solid black once the window is maximized or full screen (measured 2026-10-26), so it is not used. On D3D11 the WebGL canvas drops out for a single frame every few seconds; the renderer keeps its drawing buffer (`preserveDrawingBuffer` in scene.js) to soften that.

## Rooms

`avatar.json` is Iris: her character, clips, gestures, idle life and walking. Her rooms are separate files listed under `rooms` (`living` and `office` today), placed together in one world. `room` names the room she lives in and starts in, `workRoom` the one that work mode takes her to. A room file holds the GLB and everything that only makes sense in that room:

- `file`, `origin` (where the room sits in the world, metres) and `rotation` (degrees about the vertical). The office is at the origin and the living room sits to its west, with a three metre gap between the walls that she crosses between the two doors; nothing is modelled there and the camera never shows it.
- `floor` (the object she stands on; the room is lowered so its top is at her feet; only the room she is in shows its floor plane, since every plane covers the whole world), `walls` (a name pattern that gives the room's extent and its zone), `monitor` (a name pattern for the screen she works at; it brightens under the pointer, a click emits `monitorClicked`, and the glass primitive carries the chat picture), `hide`, `noShadowFrom`.
- `camera`, in the room's own coordinates, and the door timings `doorWatch`, `doorLeave`, `doorSwing`.
- `spots`: `home` (where she stands), `door` (just inside the door she uses), `away` (just outside it), `closeup`. Each is an offset from an `anchor` object, or an absolute room position without one, plus a `facing` in degrees where 0 faces the camera.
- `furniture`: approach points for sit and lie, such as `"couch": { "anchor": "Couch_Entry", "alignClip": "sit", "facing": 0 }`. Furniture is reachable from any room; she walks there through the doors.
- `passable`: object names (exact) she may walk through, such as the door meshes that fill the doorways.

She is always in one room, the one whose walls surround her. Crossing a threshold cuts to that room's camera, and `goto <room>`, `work on` and `work off` plan the walk out through one door and in through the next. A new room is a new file with the same keys and an origin that puts its door opposite another room's door; leave out what the room does not have. To look at one room alone, open the page with `?room=rooms/<name>.json`; `?room=<key>` starts her in that room instead.

## Add things

- **An idle:** export `Motion/Idle/<Name>.glb`, plus `Home to <Name>.glb` and `<Name> To Home.glb` anywhere under `Motion/`. The page builds the activity, a mirrored twin and the pool entry. One motion per export file, or name the clip in `avatar.json` under `clips`.
- **An outfit:** drop a GLB in `Clothes/Outfits/` for the everyday wardrobe she can pick from on her own, or in a folder listed under `extraOutfitFolders` in `avatar.json` (`Clothes/Lingerie/` today) for outfits that are only worn when asked for. Both kinds work with `outfit <name>`; `capabilities().outfits` marks each with `everyday`. Shoes with a `-HighHeels` motion variant are picked up by name.
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
