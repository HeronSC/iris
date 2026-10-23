# Iris UI — Phase 3: The Room

Iris gets a home of her own. A screen dedicated to Iris shows her room, the three.js scene from the avatar test, with none of the test controls. Inside the room is a monitor showing the chat UI. While the room is showing, Iris is in conversation mode and waits for her wake word. Clicking the monitor opens the chat, and conversation mode stops. Closing the chat goes back to the room, and conversation mode resumes.

No code yet. This is the design.

---

## 1. What exists today

**The room** — `E:\Reallusion\Iris\avatar_test` (index.html, app.js, avatar.json, served by `serve.py` on port 8790):

- Room model `Building\office.glb` (the office, re-exported 2026-10-22; 56 MB before that) and character `Iris.glb` (89 MB), plus the motion GLBs, all loaded at startup.
- A state machine for idle, walk, sit, lie and dance; nav-grid pathing between `Spot_*` empties; idle life (glances, fidgets, blinks).
- Procedural gestures: nod, shake, tilt, laugh, surprise and others.
- Expressions from morph mixes: smile, concerned, skeptical and others.
- A `Listening` toggle (`setListening`, app.js:2875) that turns her attention to the camera.
- Mouth movement driven by audio volume (`audioValue`, app.js:2466). The test page says this is "the same way Piper's output would".
- `window.avatar` (app.js:3010) exposes `runCommand`, `playGesture`, `setExpression`, `goToSpot`, `setCameraMode` and more, so a host page can drive her.
- The room has a sofa (`Pink_Sofa`), a folding screen, doors and a window. **There is no desk, monitor or chair.**
- **No connection to Iris:** no socket, no events, no microphone.
- **The folder is not in git.**

**The chat UI** — `ui/main.py`, PySide6:

- `IrisWindow`: transcript, composer, details stack, tray, Ctrl+K palette, Ctrl+Alt+I hotkey.
- The details stack already contains a `QWebEngineView` (`ResultsPanel`), so Qt WebEngine is in the stack (checklist 6).
- The window always opens maximized on whatever screen Windows picks. Its geometry is saved but never restored (UI Phase 2 §16, still open).
- Voice signals come from `VoiceController` (`stateChanged`, `woke`, `wakeChanged`, `conversationEnded`). Thinking comes from `ProcessThread.eventSignal`.
- Speaking has no signal; it is only the polled `voice.speaking`.
- `Launch-Iris.cmd` starts the console REPL, not this UI. `Run-Iris-Dev.cmd` starts the UI.

---

## 2. The shape of it

**One window, one process, two views.** `IrisWindow` gains a top-level stack:

| View | What it is | Voice |
|---|---|---|
| **Room** (default) | A full-screen `QWebEngineView` running the room page | Wake word armed. Conversation mode on wake. |
| **Chat** | Today's window content, unchanged: transcript, composer, details | Conversation mode off. Wake word disarmed. |

Both views share the same `IrisApplication`, session and transcript. Whatever was said in the room is in the chat when you open it, and the reverse.

**Why not two programs.** The room could stay a separate browser page talking to Iris over HTTP. But the core has no chat endpoint or event stream, the voice already lives in the Qt process, and a second program means two windows to keep on one screen. Hosting the room in the Qt window needs none of that.

**Bridge.** Qt and the page talk through `QWebChannel`:

- **Qt → page:** voice and turn state, speech levels for the mouth, chat snapshots for the monitor, and cards (§6).
- **Page → Qt:** "monitor clicked", "page ready", and load progress.

The page drives the avatar through the existing `window.avatar` calls.

**Loading the page.** ES modules do not load from `file://`. The room page and its assets are therefore served to the view through a Qt URL-scheme handler (for example `iris-room://`) from inside the process, with no extra server. The room code moves into the repo under `ui/room/` so it is versioned. The large GLBs stay in `E:\Reallusion\Iris`, and config holds that path.

---

## 3. Startup and the dedicated screen

- **Choosing the screen.** Config `ui.room_screen` names the screen by its identity — the `QScreen` name, model and serial — not by its index, which changes when monitors are re-plugged. It can be set from the tray menu (**Show Iris on ▸** list of screens) or with `/ui screen`.
- **Opening.** Iris opens frameless and full screen on that screen, in the room view, every time.
- **Screen missing.** If the screen is off or unplugged, Iris opens windowed on the primary screen. When the screen comes back (`screenAdded`), she moves back to it.
- **Launch.** `Launch-Iris.cmd` and the desktop shortcut start the Qt UI. Iris also starts with Windows (decision 6).
- **Fixes on the way.** The windowed chat (§5) restores its geometry, which closes UI Phase 2 §16.
- **Loading.** The GLBs take a few seconds. The room loads once at startup behind the existing progress bar and stays loaded for the life of the app. Going back to the room is then instant.

---

## 4. The room view

- **No test controls.** The `<aside>` panel, the HUD and mouse-follow are gone. The camera is fixed on the room camera (`cameraMode: room`).
- **Developer panel.** The same page with the test panel is still available behind a key (Ctrl+Shift+D) or a `?dev` URL flag, so animation work keeps using one codebase.
- **New in the room model** (Henry, in Blender): a desk, a chair and a monitor, placed so the monitor is clearly visible from the room camera.
  - The monitor's screen is its own mesh, named `Monitor_Screen`, with UVs filling 0–1 so a picture maps onto it cleanly.
  - A `Spot_Desk` empty lets her walk to the desk if wanted later.
- **Hover and click.** The pointer is raycast against `Monitor_Screen`. On hover, the screen brightens slightly and the cursor becomes a hand. On click, the room tells Qt "monitor clicked".

---

## 5. Monitor and transitions

**What the monitor shows:** a **live picture of the chat view** (decision 1).

- Qt keeps the chat view laid out at the monitor's aspect ratio and renders it offscreen (`QWidget.grab`). It sends the image to the page as the monitor's texture.
- The picture is refreshed only when the transcript changes, and at most once a second.
- From across the room the text isn't readable, and doesn't need to be. It looks like Iris's screen, and it makes the transition seamless.

**Opening the chat** (about 0.7 s):

1. The camera eases toward the monitor until the screen fills the view.
2. The room crossfades into the chat view, which shows the same picture the monitor showed.
3. The room's render loop pauses.

**Closing the chat:**

- Triggers: a **Back to room** button in the header, Esc in an empty composer, or the idle timeout (decision 7).
- The room resumes zoomed in on the monitor and eases back out to the room camera.

---

## 6. Voice and Iris's body language

**Room view.** The room is conversation mode with the wake word armed:

1. Asleep, waiting for "Iris" (`wake-asleep`).
2. She hears the wake word, turns to face the room camera, and listening goes on.
3. She hears you and transcribes.
4. She thinks, and tools run.
5. She speaks.
6. Conversation mode carries on for follow-ups, under its existing timeout, then she goes back to waiting for the wake word.

**Chat view.** Conversation mode stops:

- The microphone closes and the wake word is disarmed.
- Any reply being spoken stops, and typed turns are not spoken, which matches today's `speak_replies: voice`.
- Push-to-talk (Ctrl+Alt+T) still works in the chat view. The wake word does not (decision 3).

**State to body language.** These are driven by the existing voice and turn signals through the bridge:

| Iris state | Signal | What she does |
|---|---|---|
| Waiting for the wake word | `wake-asleep` | Idle life, some wandering, sitting when she likes (decision 4) |
| Woke | `woke` | Turns to the camera, `setListening(true)`, small attentive expression |
| Hearing you | `conversation-hearing` | Listening stays on, small nods |
| Transcribing or thinking | `transcribing`, IrisEvent `thinking` | Glances away, small tilt |
| Running a tool | tool event | Glances at the monitor |
| Speaking | **new speaking signal** | Mouth from speech levels, soft smile |
| Error or "couldn't" | IrisEvent `error`, failed tool | Concerned expression |
| Waiting on a confirmation | `awaiting_confirmation` | Asks aloud and waits for yes or no (the control stays in front of the model) |
| Conversation ended | `conversationEnded` | Listening off, back to idle |

**Two additions to core:**

- **A speaking signal.** The voice service gets `on_speaking(start/stop)`, replacing the polled flag.
- **Speech levels.** While Piper plays, the speaker computes a volume envelope from the audio it plays, about 30 values a second, and sends it to the page. That replaces the test page's audio-file analyser with the real output.

Emotion tags from the model, matching expressions to what she says, stay in Phase F. Until then, expressions come from state, as in the table.

---

## 7. Things that need a screen while in the room

"Pull up the driveway camera", a forecast, a file list: the monitor is too small for these at room distance. So (decision 2):

- **A card over the room.** A panel floats in front of the scene with the camera snapshot, the forecast, or the top results. It is rendered by the same result contract (`to_detail`) the details pane uses.
- Iris says "it's on the screen".
- The card fades after a while, or on "close that" (a tool) or a click. Clicking the card opens the chat view with the full details.
- **Watcher notifications** in the room are spoken, as now with the `voice` channel. They also leave a small badge on the monitor until the chat is opened.

---

## 8. Running all day

- **GPU sharing.** The 4060 Ti (16 GB) already runs qwen3 and Whisper. The room adds a WebGL scene that is always on. Measures:
  - Cap the room at 30 fps when idle and 60 fps during transitions.
  - Pause rendering while the chat view is up, the screen is asleep, or the PC is locked.
  - Check VRAM use with the GLBs loaded before settling on texture sizes.
- **Shadows.** The single 2048² shadow map stays. The room's static shadows should be baked in Blender (the `Bake` folder suggests this is under way), so only Iris casts live shadows.
- **The screen stays on.** Henry keeps his screens on, so Iris does not manage display sleep (decision 5). Night and atmosphere come later.

---

## 9. Order of work

1. **Move the room into the repo.**
   - Copy into `ui/room/` with `// File:` headers; app.js already has no comments, so it passes the house rules otherwise.
   - Commit it before changing anything.
   - Point the asset path at `E:\Reallusion\Iris`.
2. **Room view in Qt:**
   - the scheme handler, `QWebEngineView` and `QWebChannel`
   - the developer panel behind a key
   - the room camera only
3. **Screen choice:** frameless full screen on `ui.room_screen`, the missing-screen fallback, geometry restore, and the launcher fix.
4. **Voice states to body language,** plus the new speaking signal and speech levels for the mouth.
5. **Monitor asset** (Henry, in Blender), then click, live texture and transitions.
6. **Cards over the room** and the notification badge.
7. **All-day behaviour:** frame cap, pausing, start with Windows, and the chat idle return.

Steps 1–4 need no new art. Step 5 waits on the monitor model.

This phase is independent of Phase A (`PhaseA-NaturalConversation.md`) and can run alongside it. Conversation in the room will only feel right once Phase A lands.

---

## 10. Decisions (Henry, 2026-10-19)

1. **The monitor shows a live picture of the chat view.** Henry models the desk and monitor in Blender, with a `Monitor_Screen` mesh (§4).
2. **Visual results in room mode appear as a card over the room** (§7).
3. **Push-to-talk works in the chat view, by hotkey only.** The wake word stays off there.
4. **Idle behaviour:** while inactive she idles, wanders some, and sits when she wants.
   - **Eventually the model decides what she does.** That design is deferred to a later phase.
   - **Until then,** idle life plus an occasional wander to a `Spot_*` or a sit on the sofa, on a simple random timer.
5. **The screen stays on all the time.** Henry keeps his screens on, so Iris does nothing about display sleep. Atmosphere (lighting by time of day, night behaviour) is a later phase. Rendering still pauses while the PC is locked or the chat view is up.
6. **Iris starts with Windows, straight into the room.** This uses a shortcut in the user's Startup folder, the same mechanism as the desktop shortcut, with no installer or service change.
7. **The chat view returns to the room by itself after a few minutes with no typing** (default 5, `ui.chat_idle_return_minutes`), as long as nothing is lost:
   - Switching views never ends or splits the session. The transcript, the details pane, the scroll position and any unsent draft in the composer are all still there when the chat is opened again.
   - Across a restart (reboot, start with Windows), today's config opens a new session (`conversation.resume_last_session: false`). Topic memory (Phase 11) still recalls earlier subjects when they come up.
   - Set `resume_last_session` to true if Henry wants the chat to reopen exactly where it was after a reboot. That is a one-line config change, left for Henry to decide after living with it.
