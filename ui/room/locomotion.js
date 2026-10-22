// File: ui/room/locomotion.js

import * as THREE from 'three';

export function createLocomotion(ctx) {
  const { config, presets, rig, mixer, marker, log, motion, walk, flow, activities, travelLast, room, home, camera, controls, nav, planRoute, showRoute } = ctx;
  const stepAside = config.walking?.stepAside ?? 0.4;
  const strideRange = config.walking?.strideRange ?? [0.6, 1.3];
  const turns = config.turns || {};
  const travelByClip = new Map([...presets.values()].filter((p) => p.travel).map((p) => [p.clip, p.travel]));
  const rootMotionByClip = new Map([...presets.values()].filter((p) => p.rootMotion).map((p) => [p.clip, p.rootMotion]));
  const turnState = new Map();

  function clearWalkPlan() {
    walk.halt = false;
    walk.paces = null;
    walk.target = null;
    walk.waypoints = [];
    walk.arrive = null;
    marker.visible = false;
  }

  function travelAt(info, t) {
    return Array.from(info.interp.evaluate(Math.min(info.end, Math.max(info.start, t))));
  }

  function crossed(t0, t1, mark, wrapped) {
    return wrapped ? mark > t0 || mark <= t1 : mark > t0 && mark <= t1;
  }

  function isWalkLoop(name) {
    return Boolean(presets.get(name)?.stop);
  }

  function leadsToWalk(name) {
    const next = presets.get(name)?.next;
    return Boolean(next && isWalkLoop(next));
  }

  function routeRemaining() {
    if (!walk.target) return 0;
    let d = 0;
    let p = rig.position;
    for (const w of [...walk.waypoints, walk.target]) {
      d += Math.hypot(w.x - p.x, w.z - p.z);
      p = w;
    }
    return d;
  }

  const _travel = new THREE.Vector3();
  const _forward = new THREE.Vector3();
  const _yAxis = new THREE.Vector3(0, 1, 0);
  const _turnMove = new THREE.Vector3();
  let _turnYaw = 0;
  const _warp = new THREE.Vector3();
  function fitStride() {
    const preset = presets.get(motion.name);
    const action = motion.current;
    const info = preset?.travel;
    const stopInfo = preset?.stop && presets.get(preset.stop)?.travel;
    if (!walk.target || !action || !info || !stopInfo || walk.halt) return 1;
    const left = routeRemaining() - stopInfo.distance;
    if (left <= 0) return 1;
    const exits = [...(preset.exits || []), info.end].sort((a, b) => a - b);
    const chunk = info.distance / exits.length;
    const t = action.time;
    const next = exits.find((e) => e > t + 1e-4) ?? info.end;
    const p = travelAt(info, t);
    const q = travelAt(info, next);
    const toExit = Math.hypot(q[0] - p[0], q[2] - p[2]);
    if (toExit < chunk * 0.1) return walk.stride;
    const k0 = Math.max(0, Math.floor((left - toExit) / chunk));
    let best = 1;
    let cost = Infinity;
    for (const k of [k0, k0 + 1]) {
      const span = toExit + k * chunk;
      if (span < 1e-3) continue;
      const stride = left / span;
      const c = Math.abs(Math.log(stride)) * (stride > 1 ? 1.15 : 1);
      if (c < cost) {
        cost = c;
        best = stride;
      }
    }
    return THREE.MathUtils.clamp(best, strideRange[0], strideRange[1]);
  }

  function updateLocomotion(dt) {
    walk.stride = isWalkLoop(motion.name) ? walk.stride + (fitStride() - walk.stride) * (1 - Math.exp(-dt * 4)) : 1;
    _travel.set(0, 0, 0);
    _warp.set(0, 0, 0);
    _turnMove.set(0, 0, 0);
    _turnYaw = 0;
    if (isTurn(motion.name) && leadsToWalk(motion.next) && motion.current.time >= motion.current.getClip().duration - (turns.handoff ?? 0.25)) mixer.dispatchEvent({ type: 'finished', action: motion.current });
    if (walk.arrive && walk.arrive.clip !== motion.current?.getClip()) {
      walk.arrive = null;
      marker.visible = false;
    }
    let exitReached = false;
    for (const [action, last] of travelLast) {
      const info = travelByClip.get(action.getClip());
      const weight = action.enabled ? action.getEffectiveWeight() : 0;
      const t = action.time;
      travelLast.set(action, t);
      const rm = rootMotionByClip.get(action.getClip());
      if (rm && weight > 0) {
        const state = turnState.get(action) || { warp: 1, startYaw: rig.rotation.y };
        const a = rm.interp.evaluate(Math.min(rm.end, Math.max(rm.start, last))).slice();
        const b = rm.interp.evaluate(Math.min(rm.end, Math.max(rm.start, t))).slice();
        const local = new THREE.Vector3(b[0] - a[0], 0, b[2] - a[2]).applyAxisAngle(_yAxis, state.startYaw);
        _turnMove.add(local);
        _turnYaw += (b[1] - a[1]) * state.warp;
        continue;
      }
      if (!info || weight <= 0) continue;
      const wrapped = action.loop !== THREE.LoopOnce && t < last - 1e-6;
      const a = travelAt(info, last);
      const b = travelAt(info, t);
      if (walk.arrive && action === motion.current && walk.arrive.clip === action.getClip()) {
        const end = travelAt(info, info.end);
        const step = Math.hypot(b[0] - a[0], b[2] - a[2]);
        const left = Math.hypot(end[0] - b[0], end[2] - b[2]);
        if (step + left > 1e-5) {
          const fraction = step / (step + left);
          _warp.x += (walk.arrive.point.x - rig.position.x) * fraction * weight;
          _warp.z += (walk.arrive.point.z - rig.position.z) * fraction * weight;
        }
        continue;
      }
      const scaled = weight * (action === motion.current ? walk.stride : 1);
      if (wrapped) {
        const end = travelAt(info, info.end);
        const start = travelAt(info, info.start);
        _travel.x += ((end[0] - a[0]) + (b[0] - start[0])) * scaled;
        _travel.z += ((end[2] - a[2]) + (b[2] - start[2])) * scaled;
      } else {
        _travel.x += (b[0] - a[0]) * scaled;
        _travel.z += (b[2] - a[2]) * scaled;
      }
      if (action === motion.current) {
        walk.steps += info.steps.filter((s) => crossed(last, t, s, wrapped)).length;
        const preset = presets.get(motion.name);
        if (preset && preset.stop) {
          const exits = [...(preset.exits || []), info.end];
          exitReached = exits.some((e) => crossed(last, t, e, wrapped));
        }
      }
    }
    if (ctx.footLock) {
      const lock = ctx.footLock;
      if (lock.action !== motion.current || !lock.action.enabled) ctx.footLock = null;
      else {
        if (lock.started) lock.elapsed += dt;
        lock.started = true;
        const w = lock.fade > 0 ? Math.min(1, lock.elapsed / lock.fade) : 1;
        const step = w - lock.applied;
        lock.applied = w;
        _travel.x += lock.dx * step;
        _travel.z += lock.dz * step;
        if (w >= 1) ctx.footLock = null;
      }
    }
    _travel.applyQuaternion(rig.quaternion);
    rig.position.add(_travel).add(_warp).add(_turnMove);
    rig.rotation.y += _turnYaw;

    if (flow.glide && motion.name === 'home' && !flow.target && !walk.arrive) {
      const dx = flow.glide.x - rig.position.x;
      const dz = flow.glide.z - rig.position.z;
      const gap = Math.hypot(dx, dz);
      if (gap < 0.005) flow.glide = null;
      else {
        const step = Math.min(gap, 0.5 * dt) / gap;
        rig.position.x += dx * step;
        rig.position.z += dz * step;
      }
    }
    if (flow.faceYaw !== null && motion.name === 'home' && !flow.target && !walk.arrive) {
      _forward.set(0, 0, walk.facingSign).applyQuaternion(rig.quaternion);
      const want = flow.faceYaw;
      const have = Math.atan2(_forward.x, _forward.z);
      const diff = Math.atan2(Math.sin(want - have), Math.cos(want - have));
      if (Math.abs(diff) < 0.01) flow.faceYaw = null;
      else if (Math.abs(diff) > THREE.MathUtils.degToRad(turns.minAngle ?? 35) && playTurn(diff, 'home')) return;
      else rig.rotation.y += Math.sign(diff) * Math.min(Math.abs(diff), THREE.MathUtils.clamp(Math.abs(diff) * 3, 0.4, 2.5) * dt);
    }
    if (flow.after.length && motion.name === 'home' && flow.faceYaw === null && !flow.glide && !flow.target && !walk.arrive) flow.after.shift()();

    while (walk.waypoints.length && Math.hypot(walk.waypoints[0].x - rig.position.x, walk.waypoints[0].z - rig.position.z) < 0.45) walk.waypoints.shift();
    if (walk.target && !_turnYaw && (isWalkLoop(motion.name) || leadsToWalk(motion.name))) {
      const aim = walk.waypoints[0] || walk.target;
      _forward.set(0, 0, walk.facingSign).applyQuaternion(rig.quaternion);
      const want = Math.atan2(aim.x - rig.position.x, aim.z - rig.position.z);
      const have = Math.atan2(_forward.x, _forward.z);
      const diff = Math.atan2(Math.sin(want - have), Math.cos(want - have));
      const remaining = Math.hypot(walk.target.x - rig.position.x, walk.target.z - rig.position.z);
      if (isWalkLoop(motion.name) && remaining > 1.2 && Math.abs(diff) > THREE.MathUtils.degToRad(turns.walkingAngle ?? 70) && playTurn(diff, motion.name)) return;
      rig.rotation.y += Math.max(-walk.turnRate * dt, Math.min(walk.turnRate * dt, diff));
    }

    const preset = presets.get(motion.name);
    if (exitReached && preset && preset.stop) {
      const stopInfo = presets.get(preset.stop)?.travel;
      const exits = [...(preset.exits || []), preset.travel.end];
      const chunkSteps = preset.travel.steps.length / exits.length;
      const chunkDistance = preset.travel.distance / exits.length;
      let stopNow = walk.halt;
      if (walk.paces !== null && stopInfo) stopNow ||= walk.paces - walk.steps - stopInfo.steps.length < chunkSteps / 2;
      if (walk.target && stopInfo) {
        const remaining = routeRemaining() - stopInfo.distance;
        stopNow ||= remaining < chunkDistance / 2;
      }
      if (stopNow) {
        const fade = motion.fade;
        motion.fade = 0.25;
        ctx.play(preset.stop);
        motion.fade = fade;
        if (walk.target) walk.arrive = { point: walk.target.clone(), clip: presets.get(preset.stop).clip };
        if (flow.target && isWalkLoop(activities[flow.target]?.loop)) flow.target = null;
        walk.halt = false;
        walk.paces = null;
        walk.target = null;
      }
    }
  }

  function isTurn(name) {
    return [turns.left, turns.right, turns.around, turns.aroundRight, turns.standingAround, turns.standingAroundLeft].includes(name);
  }

  function playTurn(angle, next, stepAllowed = false) {
    const pool = isWalkLoop(next) ? [turns.around, turns.aroundRight] : stepAllowed ? [turns.standingAround, turns.standingAroundLeft] : [];
    const arounds = pool.filter((n) => presets.get(n)?.rootMotion);
    const around = Math.abs(angle) > THREE.MathUtils.degToRad(140) ? arounds.find((n) => Math.sign(presets.get(n).rootMotion.yaw) === Math.sign(angle)) : null;
    const name = around || (angle > 0 ? turns.left : turns.right);
    const preset = presets.get(name);
    if (!preset || !preset.rootMotion) return false;
    const warp = THREE.MathUtils.clamp(angle / preset.rootMotion.yaw, 0.4, 2.2);
    const fade = motion.fade;
    motion.fade = 0.3;
    ctx.startClip(name, preset.clip, { loop: false, next });
    motion.fade = fade;
    turnState.set(motion.current, { warp, startYaw: rig.rotation.y });
    return true;
  }

  function startWalk({ style = 'normal', paces = null, target = null, homeward = false } = {}) {
    if (!homeward && ctx.pinned('walk')) return;
    const loopName = style === 'relaxed' ? 'walk_relaxed' : 'walk';
    const startName = style === 'relaxed' ? 'home_to_walk_relaxed' : 'home_to_walk';
    if (!presets.has(loopName) || !presets.has(startName)) return log(`Walk clips for "${style}" are not set up`, true);
    clearWalkPlan();
    ctx.view.closeup = false;
    walk.steps = 0;
    walk.paces = paces;
    if (target) {
      walk.target = new THREE.Vector3(target.x, 0, target.z);
      const route = planRoute(rig.position, walk.target);
      if (route && route.length) {
        walk.target = route[route.length - 1];
        walk.waypoints = route.slice(0, -1);
        showRoute([rig.position.clone(), ...route]);
      } else {
        if (nav) log('No clear route found; walking straight.', true);
        showRoute(null);
      }
      marker.position.set(walk.target.x, 0.003, walk.target.z);
      marker.visible = true;
    }
    if (isWalkLoop(motion.name)) {
      flow.target = null;
      if (motion.name !== loopName) ctx.play(loopName);
      return;
    }
    ctx.request(style === 'relaxed' ? 'walk_relaxed' : 'walk');
  }

  function haltWalk() {
    ctx.request('home');
  }

  function resetPosition() {
    if (ctx.pinned('reset')) return;
    clearWalkPlan();
    ctx.view.closeup = false;
    const delta = new THREE.Vector3(home.x, 0, home.z).sub(rig.position);
    rig.position.set(home.x, 0, home.z);
    rig.rotation.y = THREE.MathUtils.degToRad(home.facing) + (walk.facingSign > 0 ? 0 : Math.PI);
    camera.position.add(delta);
    controls.target.add(delta);
  }

  const autoIdleState = { settled: 0 };
  function autoIdle(dt) {
    const ready = config.autoIdle !== false && (activities.idle || ctx.lifeConfig.pool.length) && motion.name === 'home' && !flow.target && !flow.after.length && flow.faceYaw === null && !flow.glide && !walk.target && !walk.arrive && !motion.queue.length;
    autoIdleState.settled = ready ? autoIdleState.settled + dt : 0;
    if (autoIdleState.settled > (config.autoIdleDelay ?? 1.0)) {
      autoIdleState.settled = 0;
      ctx.request('idle');
    }
  }

  function goToSpot(name, style = 'normal') {
    if (ctx.pinned('goto')) return;
    const spot = room.spots.get(name.toLowerCase());
    if (!spot) return log(`No spot "${name}". Spots: ${[...room.spots.keys()].join(', ')}`, true);
    log(`Walking to ${name} (${spot.x.toFixed(2)}, ${spot.z.toFixed(2)})`);
    startWalk({ style, target: { x: spot.x, z: spot.z } });
  }

  Object.assign(ctx, { stepAside, strideRange, turns, travelByClip, rootMotionByClip, turnState, clearWalkPlan, travelAt, isWalkLoop, leadsToWalk, routeRemaining, fitStride, updateLocomotion, isTurn, playTurn, startWalk, haltWalk, resetPosition, autoIdle, goToSpot });
}
