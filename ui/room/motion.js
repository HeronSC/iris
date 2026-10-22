// File: ui/room/motion.js

import * as THREE from 'three';

export function createMotion(ctx) {
  const { config, model, presets, heelSuffix, log, startFeet, feetMidNow, rig, camera, controls, marker, room, furniture, home, heel } = ctx;

  const mixer = new THREE.AnimationMixer(model);
  const motion = { current: null, name: '', next: null, queue: [], speed: 1, fade: 0.4, loopRaw: true };
  const travelLast = new Map();
  const walk = { halt: false, paces: null, target: null, waypoints: [], arrive: null, steps: 0, turnRate: Math.PI / 2, facingSign: 1, stride: 1 };
  const activities = config.activities || {};
  const flow = { target: null, faceYaw: null, after: [], glide: null };
  const actionStarted = new WeakMap();
  const clock = { frame: 0 };
  ctx.footLock = null;

  function footLockable(name) {
    const p = presets.get(name);
    return config.footLock !== false && p && !p.travel && !p.rootMotion && !p.alignTo;
  }

  function startClip(name, clip, { loop, next = null }) {
    const action = mixer.clipAction(clip);
    const previous = motion.current;
    action.reset();
    action.setLoop(loop ? THREE.LoopRepeat : THREE.LoopOnce, Infinity);
    action.clampWhenFinished = true;
    action.timeScale = motion.speed;
    action.setEffectiveWeight(1);
    action.play();
    actionStarted.set(action, clock.frame);
    travelLast.set(action, action.time);
    if (previous && previous !== action) {
      if (motion.fade > 0) previous.crossFadeTo(action, motion.fade, false);
      else previous.stop();
    }
    ctx.footLock = null;
    if (previous && previous !== action && footLockable(name) && startFeet.get(clip)) {
      const now = feetMidNow();
      const start = startFeet.get(clip);
      if (now) ctx.footLock = { action, dx: now.x - start.x, dz: now.z - start.z, applied: 0, fade: motion.fade, elapsed: 0, started: false };
    }
    motion.current = action;
    motion.name = name;
    motion.next = next;
  }

  function play(name, { once = false } = {}) {
    const preset = presets.get(name);
    if (!preset) {
      log(`Unknown motion "${name}". Try: ${[...presets.keys()].join(', ')}`, true);
      return false;
    }
    const heelsVersion = heel.angle > 0 ? presets.get(`${name}${heelSuffix}`) : null;
    startClip(name, (heelsVersion || preset).clip, { loop: once ? false : preset.loop, next: preset.next });
    return true;
  }

  function runSequence(names) {
    const unknown = names.filter((n) => !presets.has(n));
    if (unknown.length) {
      log(`Unknown motion: ${unknown.join(', ')}`, true);
      return;
    }
    motion.queue = names.slice(1);
    play(names[0], { once: motion.queue.length > 0 });
  }

  function stopAll() {
    mixer.stopAllAction();
    motion.current = null;
    motion.name = '';
    motion.queue = [];
    ctx.clearWalkPlan();
  }

  function activityOf(name) {
    for (const [act, a] of Object.entries(activities)) {
      if (name === a.loop) return { act, phase: 'loop' };
      if (name === a.enter) return { act, phase: 'enter' };
      if (name === a.exit) return { act, phase: 'exit' };
      if (name === a.once) return { act, phase: 'once' };
    }
    return { act: 'home', phase: name === 'home' ? 'loop' : 'other' };
  }

  function parentOf(act) {
    return act === 'home' ? null : activities[act]?.parent || 'home';
  }

  function chainOf(act) {
    const chain = [];
    for (let a = act; a; a = parentOf(a)) chain.unshift(a);
    return chain;
  }

  function nextStep(from, to) {
    const a = chainOf(from);
    const b = chainOf(to);
    let i = 0;
    while (i < a.length && i < b.length && a[i] === b[i]) i++;
    if (a.length > i) return { type: 'exit', act: a[a.length - 1] };
    if (b.length > i) return { type: 'enter', act: b[i] };
    return null;
  }

  function playSettled(act) {
    play(act === 'home' ? 'home' : activities[act].loop);
  }

  function pickIdle() {
    const pool = ctx.lifeConfig.pool.filter((n) => activities[n]);
    return pool.length ? pool[Math.floor(Math.random() * pool.length)] : null;
  }

  function request(target) {
    if (target === 'idle' && !activities.idle) {
      target = pickIdle();
      if (!target) return log('No idle motions found under the motion folder.', true);
    }
    if (target !== 'home' && !activities[target]) return log(`Unknown activity "${target}"`, true);
    if (target !== 'home' && !ctx.lifeConfig.pool.includes(target) && !ctx.isWalkLoop(activities[target].loop) && ctx.pinned(target)) return;
    motion.queue = [];
    flow.faceYaw = null;
    flow.glide = null;
    flow.after = [];
    if (target === 'home') {
      walk.paces = null;
      walk.target = null;
      marker.visible = false;
    }
    flow.target = target;
    advance();
  }

  function advance() {
    if (!flow.target || ctx.isTurn(motion.name)) return;
    let { act, phase } = activityOf(motion.name);
    if (phase === 'enter' || phase === 'exit') return;
    if (phase === 'once') act = 'home';
    const step = nextStep(act, flow.target);
    if (!step) {
      flow.target = null;
      if (act === 'home' && motion.name !== 'home') play('home');
      return;
    }
    const a = activities[step.act];
    if (step.type === 'exit') {
      if (ctx.isWalkLoop(a.loop)) walk.halt = true;
      else if (a.exit) play(a.exit);
      else {
        playSettled(parentOf(step.act));
        advance();
      }
      return;
    }
    if (ctx.isWalkLoop(a.loop) && walk.target && motion.name === 'home') {
      const aim = walk.waypoints[0] || walk.target;
      const want = Math.atan2(aim.x - rig.position.x, aim.z - rig.position.z);
      const yaw = facingYaw();
      const diff = Math.atan2(Math.sin(want - yaw), Math.cos(want - yaw));
      if (Math.abs(diff) > THREE.MathUtils.degToRad(ctx.turns.minAngle ?? 35) && ctx.playTurn(diff, a.enter, ctx.routeRemaining() > 1.2)) return;
      if (Math.abs(diff) > 0.6) {
        const desired = flow.target;
        flow.target = null;
        flow.faceYaw = want;
        flow.after.unshift(() => {
          flow.target = desired;
          advance();
        });
        return;
      }
    }
    if (a.approach && !atApproach(a.approach)) {
      goApproach(a.approach);
      return;
    }
    if (a.once) {
      flow.target = null;
      play(a.once);
      return;
    }
    if (a.enter) return play(a.enter);
    play(a.loop);
    advance();
  }

  const _forward = new THREE.Vector3();
  function facingYaw() {
    _forward.set(0, 0, walk.facingSign).applyQuaternion(rig.quaternion);
    return Math.atan2(_forward.x, _forward.z);
  }

  function atApproach(name) {
    const f = furniture.get(name);
    if (!f) return true;
    const yaw = facingYaw();
    const turn = Math.atan2(Math.sin(f.facing - yaw), Math.cos(f.facing - yaw));
    return Math.hypot(rig.position.x - f.entry.x, rig.position.z - f.entry.z) < 0.08 && Math.abs(turn) < 0.05;
  }

  function goApproach(name) {
    const f = furniture.get(name);
    const desired = flow.target;
    const far = Math.hypot(rig.position.x - f.entry.x, rig.position.z - f.entry.z) > ctx.stepAside;
    const sitDown = () => {
      flow.target = desired;
      advance();
    };
    if (far) {
      ctx.startWalk({ style: 'normal', target: { x: f.entry.x, z: f.entry.z } });
      flow.after.push(() => {
        flow.faceYaw = f.facing;
        flow.after.push(sitDown);
      });
    } else {
      flow.target = null;
      if (motion.name !== 'home') play('home');
      settleAt(f.entry, f.facing);
      flow.after.push(sitDown);
    }
  }

  mixer.addEventListener('finished', (e) => {
    if (e.action !== motion.current) return;
    if (motion.queue.length) {
      const name = motion.queue.shift();
      play(name, { once: motion.queue.length > 0 });
      return;
    }
    const { act, phase } = activityOf(motion.name);
    if (phase === 'exit') {
      playSettled(parentOf(act));
      advance();
      return;
    }
    if (phase === 'enter') {
      play(activities[act].loop);
      advance();
      return;
    }
    if (motion.next) {
      const abandoned = ctx.isTurn(motion.name) && ctx.leadsToWalk(motion.next) && !(walk.target && ctx.isWalkLoop(activities[flow.target]?.loop));
      play(abandoned ? 'home' : motion.next);
      advance();
    }
  });

  function goHome() {
    request('home');
  }

  function closeUp() {
    if (ctx.pinned('close-up')) return;
    const spot = { x: 0, z: 4.4, facing: 0, ...config.closeup };
    if (room.camera) ctx.setCameraMode('room');
    walkToSpot(spot);
    flow.after.push(() => {
      ctx.view.closeup = true;
      if (room.camera) return;
      ctx.setCameraMode('orbit');
      ctx.aimPoint(controls.target);
      camera.position.set(spot.x, controls.target.y, spot.z + 1.4);
    });
  }

  const distanceTo = (spot) => Math.hypot(rig.position.x - spot.x, rig.position.z - spot.z);

  function returnHome() {
    if (ctx.pinned('home')) return;
    walkToSpot(home);
  }

  function walkToSpot(spot) {
    const yaw = THREE.MathUtils.degToRad(spot.facing);
    if (distanceTo(spot) > ctx.stepAside) {
      ctx.startWalk({ style: 'normal', target: { x: spot.x, z: spot.z }, homeward: true });
      flow.after.push(() => { flow.faceYaw = yaw; });
    } else {
      request('home');
      settleAt(spot, yaw);
    }
  }

  function settleAt(spot, yaw) {
    flow.glide = new THREE.Vector3(spot.x, 0, spot.z);
    flow.faceYaw = yaw;
  }

  function setSpeed(v) {
    motion.speed = v;
    if (motion.current) motion.current.timeScale = v;
    ctx.emit('speed', v);
  }

  function setFade(v) {
    motion.fade = v;
    ctx.emit('fade', v);
  }

  function setLoopRaw(on) {
    motion.loopRaw = on;
    ctx.emit('loopRaw', on);
  }

  Object.assign(ctx, { mixer, motion, travelLast, walk, flow, activities, actionStarted, clock, footLockable, startClip, play, runSequence, stopAll, activityOf, parentOf, chainOf, nextStep, playSettled, pickIdle, request, advance, facingYaw, atApproach, goApproach, goHome, closeUp, distanceTo, returnHome, walkToSpot, settleAt, setSpeed, setFade, setLoopRaw });
}
