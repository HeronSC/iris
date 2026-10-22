// File: ui/room/avatar.js

import { createEvents } from './events.js';
import { createScene } from './scene.js';
import { loadAssets } from './loading.js';
import { createCharacter } from './character.js';
import { loadRoom, placeSpots } from './room.js';
import { createOutfits } from './outfits.js';
import { createMorphs } from './morphs.js';
import { createClips } from './clips.js';
import { createNavigation } from './navigation.js';
import { createMotion } from './motion.js';
import { createLocomotion } from './locomotion.js';
import { createFace } from './face.js';
import { createIdleLife } from './idle_life.js';
import { createIdleGraph } from './idle_graph.js';
import { createCameraModes } from './camera.js';
import { createCommands } from './commands.js';
import { createLoop } from './loop.js';

async function init(ctx, container, configUrl) {
  ctx.config = await (await fetch(configUrl)).json();
  const { config } = ctx;
  createScene(ctx, container);
  await loadAssets(ctx);
  createCharacter(ctx);
  loadRoom(ctx);
  await createOutfits(ctx);
  createMorphs(ctx);
  createClips(ctx);
  createNavigation(ctx);
  placeSpots(ctx);
  createMotion(ctx);
  createLocomotion(ctx);
  createFace(ctx);
  createIdleLife(ctx);
  createIdleGraph(ctx);
  createCameraModes(ctx);
  createCommands(ctx);
  createLoop(ctx);

  if (ctx.room.scene) ctx.setRoomVisible(config.room.show !== false);
  ctx.setCameraMode(ctx.room.camera && config.room?.cameraMode !== 'orbit' ? config.room?.cameraMode || 'room' : 'follow');
  ctx.setLife(ctx.life.on);
  ctx.ready = true;
  ctx.emit('ready');
  ctx.log(`Loaded ${ctx.files.length} files, ${ctx.rawClips.size} clips, ${ctx.morphNames.length} morphs. Type "help".`);
  if (config.start) {
    if (ctx.activities[config.start] || config.start === 'home') ctx.request(config.start);
    else ctx.runSequence([config.start]);
  }
  ctx.buildIdleGraph();
  ctx.startLoop();
}

export function createAvatar() {
  const ctx = { ready: false };
  createEvents(ctx);
  ctx.init = (container, configUrl = 'avatar.json') => init(ctx, container, configUrl);
  return ctx;
}
