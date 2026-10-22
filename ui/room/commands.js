// File: ui/room/commands.js

import * as THREE from 'three';

export function createCommands(ctx) {
  const { config, log, activities, gestureDefs, expressions, morphNames, room, outfitCatalog, motion } = ctx;

  const HELP = [
    'home (walk back to the home spot) · work (same as voice on) · idle · halt / stop (home pose where she stands) · closeup',
    'walk [relaxed] [paces] · goto <spot> [relaxed] · sit · lie (halt / home stands her up)',
    `Gestures: ${Object.keys(gestureDefs).join(', ').replace(/_/g, ' ')} · breathing on|off · life on|off · listen on|off`,
    'turn left|right|around [degrees] · dance (walks to the center first) · outfit <name> · outfits',
    'walkto <x> <z> [relaxed] · reset (back to the home spot)',
    'goto <spot> [relaxed] · spots · room on|off · map on|off · camera orbit|follow|room',
    'voice on|off (she goes to her work spot and stays; camera moves to the voice shot)',
    'camera shot (use the current view as the voice shot and print it for avatar.json)',
    'speed 0.5 · fade 0.3 · mouth 0.4 · freeze (bind pose)',
    'blink on|off · look on|off · talk on|off',
    'morph <name> <0..1> · morphs <filter>',
    `Expressions: ${[...expressions.keys()].join(', ')} <0..1|off> · neutral`,
    '  Typing one switches the others off; the sliders can be mixed.',
  ].join('\n');

  const onOff = (word) => word === 'on' || word === 'true' || word === '1';
  function runCommand(text) {
    const line = text.trim();
    if (!line) return;
    log(`> ${line}`);
    const [head, ...rest] = line.split(/\s+/);
    const command = head.toLowerCase();
    if (command === 'help' || command === '?') return log(HELP);
    if (command === 'stop' || command === 'halt') return ctx.goHome();
    if (command === 'home') return ctx.returnHome();
    if (command === 'work') return ctx.setVoice(true);
    if (command === 'closeup') return ctx.closeUp();
    if (command === 'outfit') return ctx.setOutfit(rest.join(' '));
    if (command === 'outfits') return log([...outfitCatalog.values()].map((e) => e.label).join(', '));
    if (command === 'turn') {
      const dir = rest[0];
      const degrees = Number(rest[1]) || (dir === 'around' ? 180 : 90);
      const angle = THREE.MathUtils.degToRad(dir === 'right' ? -degrees : degrees);
      if (!['left', 'right', 'around'].includes(dir)) return log('Usage: turn left|right|around [degrees]', true);
      const standing = ctx.activityOf(motion.name);
      const idling = standing.phase === 'loop' && ctx.lifeConfig.pool.includes(standing.act);
      const settle = idling ? activities[standing.act].loop : config.autoIdle !== false ? activities[ctx.pickIdle()]?.loop ?? 'home' : 'home';
      if (motion.name === 'home' || idling) return ctx.playTurn(angle, settle, true) || log('Turn clips are not set up', true);
      if (ctx.isWalkLoop(motion.name)) return ctx.playTurn(angle, motion.name) || log('Turn clips are not set up', true);
      return log('Turns work from home (Halt first) or while walking.', true);
    }
    if (command === 'freeze') return ctx.stopAll();
    if (command === 'speed') return ctx.setSpeed(Number(rest[0]) || 1);
    if (command === 'fade') return ctx.setFade(Number(rest[0]) || 0);
    if (command === 'mouth') return ctx.setMouth(Number(rest[0]) || 0);
    if (command === 'blink') return ctx.setBlink(onOff(rest[0]));
    if (command === 'look') return ctx.setLook(onOff(rest[0]));
    if (command === 'talk') return ctx.setTalk(onOff(rest[0]));
    if (command === 'morph') {
      if (!morphNames.includes(rest[0])) return log(`No morph "${rest[0]}"`, true);
      return ctx.setExplorerMorph(rest[0], Number(rest[1]) || 0);
    }
    if (command === 'walk') {
      const style = rest.includes('relaxed') ? 'relaxed' : 'normal';
      const n = rest.map(Number).find((v) => Number.isFinite(v) && v > 0);
      return ctx.startWalk({ style, paces: n ?? null });
    }
    if (command === 'walkto') {
      const style = rest.includes('relaxed') ? 'relaxed' : 'normal';
      const [x, z] = rest.map(Number).filter((v) => Number.isFinite(v));
      if (x === undefined || z === undefined) return log('Usage: walkto <x> <z> [relaxed]   (meters; she starts at 0 0 facing +z)', true);
      return ctx.startWalk({ style, target: { x, z } });
    }
    if (command === 'goto') {
      const style = rest.includes('relaxed') ? 'relaxed' : 'normal';
      const name = rest.filter((w) => w !== 'relaxed').join('_');
      return ctx.goToSpot(name, style);
    }
    if (command === 'spots') return log(room.spots.size ? [...room.spots].map(([n, v]) => `${n} (${v.x.toFixed(2)}, ${v.z.toFixed(2)})`).join('\n') : 'No room loaded.');
    if (command === 'room') return ctx.setRoomVisible(onOff(rest[0]));
    if (command === 'roomcam') return ctx.setCameraMode('room');
    if (command === 'map') return ctx.setMapVisible(onOff(rest[0]));
    if (command === 'voice') return ctx.setVoice(onOff(rest[0] ?? 'on'));
    if (command === 'camera' && rest[0] === 'shot') return log(`"voiceShot": ${JSON.stringify(ctx.captureVoiceShot())}`);
    if (command === 'camera') {
      if (!['orbit', 'follow', 'room'].includes(rest[0])) return log('Usage: camera orbit|follow|room', true);
      return ctx.setCameraMode(rest[0]);
    }
    if (command === 'reset') return ctx.resetPosition();
    const gestureName = gestureDefs[`${command}_${rest[0]}`] ? `${command}_${rest[0]}` : command;
    if (gestureDefs[gestureName]) return ctx.playGesture(gestureName);
    if (command === 'breathing') return ctx.setBreathing(onOff(rest[0]));
    if (command === 'life') return ctx.setLife(onOff(rest[0]));
    if (command === 'listen' || command === 'listening') return ctx.setListening(onOff(rest[0] ?? 'on'));
    if (command === 'idle') return ctx.request('idle');
    if (command === 'neutral') {
      for (const name of expressions.keys()) ctx.setExpression(name, 0);
      return;
    }
    if (expressions.has(command)) {
      const v = rest[0] === undefined ? 1 : rest[0] === 'off' ? 0 : Number(rest[0]) || 0;
      for (const name of expressions.keys()) if (name !== command) ctx.setExpression(name, 0);
      return ctx.setExpression(command, Math.min(1, Math.max(0, v)));
    }
    if (command === 'morphs') {
      const filter = (rest[0] || '').toLowerCase();
      return log(morphNames.filter((n) => n.toLowerCase().includes(filter)).join(', '));
    }
    if (activities[command]) return ctx.request(command);
    ctx.runSequence(line.split(/\s*,\s*|\s+then\s+|\s+/).filter(Boolean));
  }

  Object.assign(ctx, { HELP, runCommand });
}
