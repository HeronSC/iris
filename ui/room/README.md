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
- Only the room camera is used; the test panel and HUD are hidden. Ctrl+Shift+D shows them, as does `?dev` on the URL or `"dev": true` under `ui.room`. The panel's motion buttons are Talk (the wake-word toggle, same as the wake hotkey; in a plain browser it toggles work mode), Home, Sit and Stand. The dev command line, the room toggles and the mouth and audio-file tests were removed on 2026-10-27; the page log now reaches only the console and the `iris.room` logger.
- Clicking the monitor, or pressing Enter, opens the chat. **Back to room** in the chat header, or Esc in an empty composer, goes back.
- The monitor shows a live picture of the chat view: Qt grabs the chat widget within a second of a transcript change, and again whenever the room comes back, and sends it over the bridge as `monitorImage`. The page puts it on the primitive whose material name matches `monitorScreenMaterial` in the room file (default `screen|glass|display`), generating flat UVs if the mesh has none.
- The page is loaded once at startup and stays loaded.
- Voice drives her: turning the wake word on (Ctrl+Alt+W or the panel's Talk button) sends her to the work room, the wake word itself turns listening on, hearing and transcribing become `listening` and `thinking`, speaking is polled from the voice service and moves her mouth (a generic talking mouth until real speech levels arrive), and "goodbye", the wake hotkey or a stopped conversation walk her back to her room. A conversation that merely times out leaves her at her desk waiting for the wake word.
- `ui.room_screen` in the config names the screen Iris fills, by model and serial rather than position. Pick it from the tray menu **Show Iris on**; **Any screen, windowed** clears it and the window opens where it was last closed. If the screen is unplugged she opens windowed on the primary screen and moves back when it returns.
- The page is hosted by a Qt Quick `WebEngineView` (`ui/room_host.qml`) in its own native window inside the Qt widget, with the scene graph's threaded render loop (`QSG_RENDER_LOOP=threaded`, set in `register_room_scheme()`) on the default Direct3D 11 backend. `qwebchannel.js` is injected as a user script from Qt's resources and the page console goes to the `iris.room` logger. Measured full screen on 2026-10-27 over 10 s of walking: the widget `QWebEngineView` (a `QQuickWidget` underneath) delivered 22 animation frames later than 25 ms (p99 33 ms); the Quick view delivered 1 (p99 17 ms). The Quick view with the default render loop, and the widget view with the threaded loop, both dropped to 30 fps. The OpenGL backend rendered black full screen (2026-10-26) and is not used. `frameCap` in `avatar.json` is 0: capping by skipping animation frames made walking and the mouth judder, so the all-day cap (Phase 3 step 7) needs another approach, such as pausing while the window is hidden. The renderer still keeps its drawing buffer (`preserveDrawingBuffer` in scene.js).

## Rooms

`avatar.json` is Iris: her character, clips, gestures, idle life and walking. Her rooms are separate files listed under `rooms` (`living` and `office` today), placed together in one world. `room` names the room she lives in and starts in, `workRoom` the one that work mode takes her to. A room file holds the GLB and everything that only makes sense in that room:

- `file`, `origin` (where the room sits in the world, metres) and `rotation` (degrees about the vertical). The office is at the origin and the living room sits to its west, with a three metre gap between the walls that she crosses between the two doors; nothing is modelled there and the camera never shows it.
- `floor` (the object she stands on; the room is lowered so its top is at her feet; only the room she is in shows its floor plane, since every plane covers the whole world), `walls` (a name pattern that gives the room's extent and its zone), `monitor` (a name pattern for the screen she works at; it brightens under the pointer, a click emits `monitorClicked`, and the glass primitive carries the chat picture), `hide`, `noShadowFrom`.
- `camera`, in the room's own coordinates, and the door timings `doorWatch`, `doorLeave`, `doorSwing`.
- `spots`: `home` (where she stands), `door` (just inside the door she uses), `away` (just outside it), `closeup`. Each is an offset from an `anchor` object, or an absolute room position without one, plus a `facing` in degrees where 0 faces the camera.
- `sun`: a directional light that enters through the window and is the room's only shadow caster. `elevation` is its height in degrees, `azimuth` where it stands in degrees from straight behind the window (positive toward the east wall), plus `color`, `intensity` and `shadowSize` (2048 by default; 4096 is sharper but cost 15 fps on the RTX 4060 Ti with the 700k-triangle living room). The walls and ceiling must cast shadows for it to work, so `noShadowFrom` lists only the window view, the floor and shells. `lights` overrides the global `lighting` levels (`exposure`, `environment`, `key`, `rim`, `fill`, `face`; `face` is a shadowless spot mounted on the camera and aimed at her chest, scaled with distance so she reads the same from any camera, and narrow enough to leave the room alone) and the hemisphere `sky` and `ground` colours while she is in that room, plus `bounce`, a shadowless light shining straight up at `bounceColor` (the ground colour if unset) that stands in for light reflected off the floor, which is what lights a ceiling; without a sun the follow light casts her shadow as before. `from` names a sun light exported from Blender (glTF "Punctual Lights" must be on in the export) and then its direction replaces the two angles. Point and spot lights in the file are the room's lamps: multiplied by `lightScale`, shadow-casting when named in `shadowLights`, and switched by `lamps`: they are off while the sun's intensity is at or above `onBelow` and come on at `level` times their exported strength when it drops below (`alwaysOn` keeps them on in daylight). `setLamps(level)` and the `lamps` command change the level live; `sun <elevation> [azimuth] [intensity] [color]` moves the sun. With the sun down and the lamps on, or the sun up and the lamps off, the living room renders at 55 to 60 fps full screen; with both on it drops to about 44, which is why they alternate. `setSun({ elevation, azimuth, color, intensity })` changes the current room's sun live, which is the hook for a time of day and for lamps that come up as it sets.
- Rendering: `ambientOcclusion` in `avatar.json` (`on`, `radius`, `scale`, `thickness`, `intensity`) turns on a ground-truth ambient occlusion pass (three.js GTAO) through an effect composer with 4x multisampling; `ao off` and `ao 1.2` switch it at runtime. `lighting.toneMapping` picks `aces` (default), `agx`, `neutral` or `none`; `tonemap agx` switches live. The post-processing and shader files under `vendor/three/examples/jsm` were copied from the full three.js r186 package that the avatar test app carries.
- `furniture`: approach points for sit, such as `"couch": { "anchor": "Sofa_Sit_Entry", "facing": 0 }`. The anchor is an empty in the room file placed where she stands at the first frame of the enter clip; she walks there, settles facing `facing`, and the clip plays from that spot. The exit clip (`stand_up`) is aligned to the enter clip with `alignTo`, so it starts where the enter clip ended as long as both were exported from the same scene placement. Furniture is reachable from any room; she walks there through the doors.
- `passable`: object names (exact) she may walk through, such as the door meshes that fill the doorways.

Doors open for her. Every `passable` object with a mesh is a door: when she walks toward it and comes within `doors.openAt` metres (2.2) it opens, and once she is `doors.closeAt` metres (2.6) past it, or stops, it closes. A door with a clip under `Motion/Doors/` whose tracks name the door object (`Door1_0` matches `Door1`) plays that clip to open and the same clip backwards to close; a door without a clip slides along its wall by its own width over `doors.slideSeconds` (0.8). Both settings live under `doors` in `avatar.json`.

She is always in one room, the one whose walls surround her. Crossing a threshold cuts to that room's camera, and `goto <room>`, `work on` and `work off` plan the walk out through one door and in through the next. A new room is a new file with the same keys and an origin that puts its door opposite another room's door; leave out what the room does not have. To look at one room alone, open the page with `?room=rooms/<name>.json`; `?room=<key>` starts her in that room instead.

## Add things

- **An idle:** export `Motion/Idle/<Name>.glb`, plus `Home to <Name>.glb` and `<Name> To Home.glb` anywhere under `Motion/`. The page builds the activity, a mirrored twin and the pool entry. One motion per export file, or name the clip in `avatar.json` under `clips`.
- **An outfit:** drop a GLB in `Clothes/Outfits/` for the everyday wardrobe she can pick from on her own, or in a folder listed under `extraOutfitFolders` in `avatar.json` (`Clothes/Lingerie/` today) for outfits that are only worn when asked for. Both kinds work with `outfit <name>`; `capabilities().outfits` marks each with `everyday`. Shoes with a `-HighHeels` motion variant are picked up by name.
- **A gesture or expression:** add it to `gestures` or `expressions` in `avatar.json`.
- **Anything with special needs** (couch motions, walks, turns): an entry in `clips` and `activities` in `avatar.json`, as today, plus the furniture anchor in the room file. `clip` is the clip's exported name or `longest`; iClone renames clips on every export, so export one motion per file and use `longest`. A file that holds extra motions is an export to fix, not something the page works around.

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
