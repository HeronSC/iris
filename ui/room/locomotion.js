// File: ui/room/locomotion.js

import * as THREE from 'three';

export function createLocomotion(ctx) {
  const { config, presets, rig, mixer, marker, log, motion, walk, flow, activities, travelLast, camera, controls, nav, planRoute, showRoute } = ctx;
  const stepAside = config.walking?.stepAside ?? 0.4;
  const strideRange = config.walking?.strideRange ?? [0.6, 1.3];
  const turns = config.turns || {};
  const travelByClip = new Map([...presets.values()].filter((p) => p.travel).map((p) => [p.clip, p.travel]));
  const rootMotionByClip = new Map([...presets.values()].filter((p) => p.rootMotion).map((p) => [p.clip, p.rootMotion]));
  const turnState = new Map();
  let footLock = null;

  const yawDifference = (want, have) => Math.atan2(Math.sin(want - have), Math.cos(want - have));

  function lockFeet(action, dx = 0, dz = 0, fade = 0) {
    footLock = action ? { action, dx, dz, applied: 0, fade, elapsed: 0, started: false } : null;
  }

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

  function isTurn(name) {
    return [turns.left, turns.right, turns.around, turns.aroundRight, turns.standingAround, turns.standingAroundLeft, turns.back, turns.backRight].includes(name);
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
  const _yAxis = new THREE.Vector3(0, 1, 0);
  const _turnMove = new THREE.Vector3();
  const _warp = new THREE.Vector3();
  let _turnYaw = 0;

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

  function gatherClipMotion() {
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
    return exitReached;
  }

  function applyFootLock(dt) {
    if (!footLock) return;
    if (footLock.action !== motion.current || !footLock.action.enabled) {
      footLock = null;
      return;
    }
    if (footLock.started) footLock.elapsed += dt;
    footLock.started = true;
    const w = footLock.fade > 0 ? Math.min(1, footLock.elapsed / footLock.fade) : 1;
    const step = w - footLock.applied;
    footLock.applied = w;
    _travel.x += footLock.dx * step;
    _travel.z += footLock.dz * step;
    if (w >= 1) footLock = null;
  }

  function steerAlongRoute(dt) {
    while (walk.waypoints.length && Math.hypot(walk.waypoints[0].x - rig.position.x, walk.waypoints[0].z - rig.position.z) < 0.45) walk.waypoints.shift();
    if (!walk.target || _turnYaw || !(isWalkLoop(motion.name) || leadsToWalk(motion.name))) return false;
    const aim = walk.waypoints[0] || walk.target;
    const want = Math.atan2(aim.x - rig.position.x, aim.z - rig.position.z);
    const diff = yawDifference(want, ctx.facingYaw());
    const remaining = Math.hypot(walk.target.x - rig.position.x, walk.target.z - rig.position.z);
    if (isWalkLoop(motion.name) && remaining > 1.2 && Math.abs(diff) > THREE.MathUtils.degToRad(turns.walkingAngle ?? 70) && playTurn(diff, motion.name)) return true;
    rig.rotation.y += Math.max(-walk.turnRate * dt, Math.min(walk.turnRate * dt, diff));
    return false;
  }

  function stopIfDue(exitReached) {
    const preset = presets.get(motion.name);
    if (!exitReached || !preset || !preset.stop) return;
    const stopInfo = presets.get(preset.stop)?.travel;
    const exits = [...(preset.exits || []), preset.travel.end];
    const chunkSteps = preset.travel.steps.length / exits.length;
    const chunkDistance = preset.travel.distance / exits.length;
    let stopNow = walk.halt;
    if (walk.paces !== null && stopInfo) stopNow ||= walk.paces - walk.steps - stopInfo.steps.length < chunkSteps / 2;
    if (walk.target && stopInfo) stopNow ||= routeRemaining() - stopInfo.distance < chunkDistance / 2;
    if (!stopNow) return;
    ctx.play(preset.stop, { fade: 0.25 });
    if (walk.target) walk.arrive = { point: walk.target.clone(), clip: presets.get(preset.stop).clip };
    if (flow.target && isWalkLoop(activities[flow.target]?.loop)) flow.target = null;
    walk.halt = false;
    walk.paces = null;
    walk.target = null;
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
    const exitReached = gatherClipMotion();
    applyFootLock(dt);
    _travel.applyQuaternion(rig.quaternion);
    rig.position.add(_travel).add(_warp).add(_turnMove);
    rig.rotation.y += _turnYaw;
    if (steerAlongRoute(dt)) return;
    stopIfDue(exitReached);
  }

  function turnToward(yaw, dt) {
    const diff = yawDifference(yaw, ctx.facingYaw());
    if (Math.abs(diff) < 0.01) return true;
    if (Math.abs(diff) > THREE.MathUtils.degToRad(turns.minAngle ?? 35) && playTurn(diff, 'home')) return false;
    rig.rotation.y += Math.sign(diff) * Math.min(Math.abs(diff), THREE.MathUtils.clamp(Math.abs(diff) * 3, 0.4, 2.5) * dt);
    return false;
  }

  function glideToward(point, dt) {
    const dx = point.x - rig.position.x;
    const dz = point.z - rig.position.z;
    const gap = Math.hypot(dx, dz);
    if (gap < 0.005) return true;
    const step = Math.min(gap, 0.5 * dt) / gap;
    rig.position.x += dx * step;
    rig.position.z += dz * step;
    return false;
  }

  function playTurn(angle, next, stepAllowed = false) {
    const pool = isWalkLoop(next) ? [turns.around, turns.aroundRight] : stepAllowed ? [turns.standingAround, turns.standingAroundLeft] : [];
    const arounds = pool.filter((n) => presets.get(n)?.rootMotion);
    const around = Math.abs(angle) > THREE.MathUtils.degToRad(140) ? arounds.find((n) => Math.sign(presets.get(n).rootMotion.yaw) === Math.sign(angle)) : null;
    const backs = next === 'home' ? [turns.back, turns.backRight].filter((n) => presets.get(n)?.rootMotion) : [];
    const back = backs.find((n) => Math.sign(presets.get(n).rootMotion.yaw) === Math.sign(angle));
    const name = around || back || (angle > 0 ? turns.left : turns.right);
    const preset = presets.get(name);
    if (!preset || !preset.rootMotion) return false;
    const warp = THREE.MathUtils.clamp(angle / preset.rootMotion.yaw, 0.4, 2.2);
    ctx.startClip(name, preset.clip, { loop: false, next, fade: 0.3 });
    turnState.set(motion.current, { warp, startYaw: rig.rotation.y });
    return true;
  }

  function beginWalk({ style = 'normal', paces = null, target = null, via = null }) {
    const loopName = style === 'normal' ? 'walk' : `walk_${style}`;
    const startName = `home_to_${loopName}`;
    if (!presets.has(loopName) || !presets.has(startName)) {
      log(`Walk clips for "${style}" are not set up`, true);
      return false;
    }
    clearWalkPlan();
    walk.steps = 0;
    walk.paces = paces;
    if (target) {
      const route = [];
      let from = rig.position;
      let straight = false;
      for (const leg of [...(via || []), target].map((p) => new THREE.Vector3(p.x, 0, p.z))) {
        const part = planRoute(from, leg);
        if (part && part.length) route.push(...part);
        else {
          route.push(leg);
          straight = true;
        }
        from = leg;
      }
      if (straight && nav) log('No clear route found; walking straight.', true);
      walk.target = route[route.length - 1];
      walk.waypoints = route.slice(0, -1);
      showRoute([rig.position.clone(), ...route]);
      marker.position.set(walk.target.x, 0.003, walk.target.z);
      marker.visible = true;
    }
    if (isWalkLoop(motion.name)) {
      flow.target = null;
      if (motion.name !== loopName) ctx.play(loopName);
      return true;
    }
    flow.target = loopName;
    ctx.advance();
    return true;
  }

  function resetPosition() {
    if (ctx.pinned('reset')) return;
    const { home } = ctx;
    ctx.clearPlan();
    clearWalkPlan();
    ctx.view.closeup = false;
    const delta = new THREE.Vector3(home.x, 0, home.z).sub(rig.position);
    rig.position.set(home.x, 0, home.z);
    rig.rotation.y = THREE.MathUtils.degToRad(home.facing) + (walk.facingSign > 0 ? 0 : Math.PI);
    camera.position.add(delta);
    controls.target.add(delta);
  }

  let facingChecked = false;
  function checkFacing(axes) {
    if (facingChecked || !axes) return false;
    facingChecked = true;
    const local = axes.forward.clone().applyQuaternion(rig.getWorldQuaternion(new THREE.Quaternion()).invert());
    if (local.z >= 0) return false;
    walk.facingSign = -1;
    rig.rotation.y += Math.PI;
    rig.updateMatrixWorld(true);
    log('Model faced away from the camera; turned it 180°.');
    return true;
  }

  Object.assign(ctx, { stepAside, strideRange, turns, travelByClip, rootMotionByClip, turnState, yawDifference, lockFeet, clearWalkPlan, travelAt, isWalkLoop, leadsToWalk, isTurn, routeRemaining, fitStride, updateLocomotion, turnToward, glideToward, playTurn, beginWalk, resetPosition, checkFacing });
}
