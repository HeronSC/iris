// File: ui/room/idle_graph.js

import * as THREE from 'three';

export function createIdleGraph(ctx) {
  const { config, presets, mixer, model, findBone, footBones, feetMidNow, life, flow, walk, motion, between, log } = ctx;
  const graphConfig = { on: true, clips: (config.idleLife || {}).pool || ['idle'], fps: 15, dwell: [4, 14], fade: 0.6, speedDrift: 0.07, novelSeconds: 45, minGap: 3, swaySpineDegrees: 0.5, crossChance: 0.4, eventChance: 0.35, ...(config.idleGraph || {}) };
  const graphBones = [
    ['CC_Base_Hip', 1.5], ['CC_Base_Waist', 1], ['CC_Base_Spine01', 1], ['CC_Base_Spine02', 1],
    ['CC_Base_L_Thigh', 1.5], ['CC_Base_R_Thigh', 1.5], ['CC_Base_L_Calf', 1.2], ['CC_Base_R_Calf', 1.2], ['CC_Base_L_Foot', 0.8], ['CC_Base_R_Foot', 0.8],
    ['CC_Base_L_Upperarm', 0.8], ['CC_Base_R_Upperarm', 0.8], ['CC_Base_L_Forearm', 0.5], ['CC_Base_R_Forearm', 0.5],
    ['CC_Base_NeckTwist01', 1.2], ['CC_Base_Head', 1.2],
  ].map(([n, w]) => [findBone(n), w]).filter(([b]) => b);
  const idleGraph = { clips: new Map(), frames: [], targets: [], active: null, dwell: 0, clock: 0, jumps: 0, visited: new Map() };

  function buildIdleGraph() {
    if (!graphConfig.on) return;
    const hip = findBone('CC_Base_Hip');
    const saved = [];
    model.traverse((o) => { if (o.isBone) saved.push([o, o.quaternion.clone(), o.position.clone()]); });
    for (const name of graphConfig.clips) {
      const preset = presets.get(name);
      if (!preset) continue;
      const clone = preset.clip.clone();
      idleGraph.clips.set(name, { name, clip: preset.clip, actions: [mixer.clipAction(preset.clip), mixer.clipAction(clone)], first: idleGraph.frames.length });
      const sampler = new THREE.AnimationMixer(model);
      const action = sampler.clipAction(preset.clip).play();
      const step = 1 / graphConfig.fps;
      for (let t = 0; t <= preset.clip.duration - step; t += step) {
        action.time = t;
        sampler.update(0);
        const feet = feetMidNow();
        const hipRel = model.worldToLocal(hip.getWorldPosition(new THREE.Vector3())).sub(feet);
        const footRel = footBones.map((b) => model.worldToLocal(b.getWorldPosition(new THREE.Vector3())).sub(feet));
        idleGraph.frames.push({ clip: name, t, feet, hipRel, footRel, quats: graphBones.map(([b]) => b.quaternion.clone()) });
      }
      sampler.stopAllAction();
      sampler.uncacheRoot(model);
      idleGraph.clips.get(name).last = idleGraph.frames.length - 1;
    }
    for (const [b, q, pos] of saved) {
      b.quaternion.copy(q);
      b.position.copy(pos);
    }
    const frames = idleGraph.frames;
    const n = frames.length;
    for (let i = 0; i < n; i++) {
      const prev = frames[i - 1]?.clip === frames[i].clip ? frames[i - 1] : frames[i];
      const next = frames[i + 1]?.clip === frames[i].clip ? frames[i + 1] : frames[i];
      frames[i].vel = next.hipRel.clone().sub(prev.hipRel);
    }
    const cost = (a, b) => {
      let c = 0;
      for (let k = 0; k < graphBones.length; k++) c += graphBones[k][1] * a.quats[k].angleTo(b.quats[k]);
      c += 4 * a.hipRel.distanceTo(b.hipRel);
      c += 3 * (a.footRel[0].distanceTo(b.footRel[0]) + a.footRel[1].distanceTo(b.footRel[1]));
      c += 0.5 * graphConfig.fps * a.vel.distanceTo(b.vel);
      return c;
    };
    const lag = Math.max(1, Math.round(0.3 * graphConfig.fps));
    const natural = [];
    for (let i = 0; i + lag < n; i++) if (frames[i].clip === frames[i + lag].clip) natural.push(cost(frames[i], frames[i + lag]));
    natural.sort((a, b) => a - b);
    const limit = natural[Math.floor(natural.length / 2)] || 1;
    idleGraph.limit = limit;
    let edges = 0;
    idleGraph.fallback = [];
    for (let i = 0; i < n; i++) {
      const list = [];
      for (let j = 0; j < n; j++) {
        if (frames[i].clip === frames[j].clip && Math.abs(frames[i].t - frames[j].t) < graphConfig.minGap) continue;
        list.push([j, cost(frames[i], frames[j])]);
      }
      list.sort((a, b) => a[1] - b[1]);
      const good = list.filter(([, c]) => c < limit).slice(0, 24);
      idleGraph.targets.push(good);
      const perSecond = new Map();
      for (const [j, c] of list) {
        const key = `${frames[j].clip}:${Math.floor(frames[j].t)}`;
        if (!perSecond.has(key)) perSecond.set(key, [j, c]);
      }
      idleGraph.fallback.push([...perSecond.values()]);
      edges += good.length;
    }
    const cross = idleGraph.targets.reduce((a, list, i) => a + list.filter(([j]) => frames[j].clip !== frames[i].clip).length, 0);
    log(`Idle player: ${n} poses from ${[...idleGraph.clips.keys()].join(' + ')}, ${edges} seamless cut points (${cross} between different idles).`);
  }

  function graphFrameIndex(name, time) {
    const g = idleGraph.clips.get(name);
    return Math.min(g.last, g.first + Math.round(time * graphConfig.fps));
  }

  function idleGraphActive() {
    if (!graphConfig.on || !life.on || !idleGraph.frames.length) return false;
    if (flow.target || walk.target) return false;
    const g = idleGraph.clips.get(motion.name);
    return Boolean(g && g.actions.includes(motion.current));
  }

  function idleGraphJump(force) {
    const action = motion.current;
    const here = graphFrameIndex(motion.name, action.time);
    const options = [];
    const noveltyOf = (j) => {
      const last = idleGraph.visited.get(Math.floor(j / graphConfig.fps));
      return last === undefined ? 1 : Math.min(1, (idleGraph.clock - last) / graphConfig.novelSeconds);
    };
    if (force) {
      const goCross = Math.random() < graphConfig.crossChance && idleGraph.fallback[here].some(([j]) => idleGraph.frames[j].clip !== motion.name);
      for (const [j, c] of idleGraph.fallback[here]) {
        const cross = idleGraph.frames[j].clip !== motion.name;
        if (cross !== goCross) continue;
        const hub = Math.min(1, idleGraph.targets[j].length / 8);
        options.push({ to: j, cost: c, weight: Math.exp(-c / (idleGraph.limit * 2.5)) * (0.05 + noveltyOf(j)) * (0.25 + hub) });
      }
    } else {
      if (!idleGraph.targets[here].length) return false;
      for (const [j, c] of idleGraph.targets[here]) options.push({ to: j, cost: c, weight: Math.exp(-c / (idleGraph.limit * 0.35)) * (0.1 + noveltyOf(j)) });
    }
    if (!options.length) return false;
    let pick = Math.random() * options.reduce((a, o) => a + o.weight, 0);
    const choice = options.find((o) => (pick -= o.weight) <= 0) || options[options.length - 1];
    choice.from = here;
    const fade = THREE.MathUtils.clamp(graphConfig.fade * (0.6 + 0.4 * choice.cost / idleGraph.limit), graphConfig.fade, graphConfig.fade * 3);
    const target = idleGraph.frames[choice.to];
    const g = idleGraph.clips.get(target.clip);
    const next = g.actions.find((a) => a !== action && !(a.isRunning() && a.getEffectiveWeight() > 0.01)) || g.actions.find((a) => a !== action);
    next.reset();
    next.setLoop(THREE.LoopRepeat, Infinity);
    next.time = target.t;
    next.timeScale = action.timeScale;
    next.setEffectiveWeight(1);
    next.play();
    action.crossFadeTo(next, fade, false);
    const now = feetMidNow();
    ctx.footLock = now ? { action: next, dx: now.x - target.feet.x, dz: now.z - target.feet.z, applied: 0, fade, elapsed: 0, started: false } : null;
    motion.current = next;
    motion.name = target.clip;
    motion.next = null;
    idleGraph.jumps += 1;
    idleGraph.lastJump = { from: `${idleGraph.frames[choice.from].clip}@${idleGraph.frames[choice.from].t.toFixed(1)}`, to: `${target.clip}@${target.t.toFixed(1)}`, cost: +choice.cost.toFixed(3), fade: +fade.toFixed(2), forced: Boolean(force) };
    return true;
  }

  function updateIdleGraph(dt) {
    idleGraph.clock += dt;
    if (!idleGraphActive()) {
      idleGraph.active = null;
      return;
    }
    const action = motion.current;
    if (idleGraph.active !== action) {
      idleGraph.active = action;
      idleGraph.dwell = between(graphConfig.dwell);
    }
    const here = graphFrameIndex(motion.name, action.time);
    idleGraph.visited.set(Math.floor(here / graphConfig.fps), idleGraph.clock);
    const drift = 1 + graphConfig.speedDrift * 0.5 * (Math.sin(idleGraph.clock * 0.13) + Math.sin(idleGraph.clock * 0.071 + 1.7));
    action.timeScale = motion.speed * drift;
    idleGraph.dwell -= dt;
    const ahead = here + Math.round(0.6 * graphConfig.fps);
    const enteringEvent = idleGraph.frames[ahead]?.clip === motion.name && !idleGraph.targets[ahead].length && idleGraph.targets[here].length > 0;
    if (enteringEvent && idleGraph.committed !== action) {
      const last = idleGraph.visited.get(Math.floor(ahead / graphConfig.fps) + 2);
      const novelty = last === undefined ? 1 : Math.min(1, (idleGraph.clock - last) / graphConfig.novelSeconds);
      if (Math.random() < graphConfig.eventChance * (0.2 + novelty)) idleGraph.committed = action;
      else idleGraph.dwell = 0;
    }
    const nearEnd = action.time > action.getClip().duration - 1.2;
    const jumped = idleGraph.dwell <= 0 || nearEnd ? idleGraphJump(nearEnd) : false;
    if (jumped) {
      idleGraph.active = motion.current;
      idleGraph.dwell = between(graphConfig.dwell);
    } else if (idleGraph.dwell <= 0) {
      idleGraph.dwell = 0.15;
    }
  }

  Object.assign(ctx, { graphConfig, idleGraph, buildIdleGraph, idleGraphActive, updateIdleGraph });
}
