// File: ui/room/plan.js

export function createPlan(ctx) {
  const { config, log, emit, motion, walk, flow, activities, furniture, home, room, camera, controls } = ctx;
  const plan = { steps: [] };

  const where = (p) => `${p.x.toFixed(1)}, ${p.z.toFixed(1)}`;
  function describe(step) {
    if (step.type === 'activity') return step.name;
    if (step.type === 'walk') return step.target ? `walk ${step.via ? 'through the door ' : ''}to ${where(step.target)}` : step.paces ? `walk ${step.paces} paces` : 'walk';
    if (step.type === 'settle') return step.point ? `settle at ${where(step.point)}` : 'face';
    return step.label || 'then';
  }
  const describePlan = () => plan.steps.map(describe);
  const announce = () => emit('plan', describePlan());

  function setPlan(steps) {
    plan.steps = steps.filter(Boolean);
    flow.target = null;
    announce();
  }

  function clearPlan() {
    setPlan([]);
  }

  function defer(step) {
    flow.target = null;
    if (plan.steps[0]) plan.steps[0].started = false;
    plan.steps.unshift(step);
    announce();
  }

  function settledOutsideHome() {
    const { phase } = ctx.activityOf(motion.name);
    return motion.name !== 'home' && (phase === 'loop' || phase === 'other') && !ctx.isTurn(motion.name);
  }

  function startStep(step) {
    if (step.type === 'activity') {
      if (step.name === 'home') {
        walk.paces = null;
        walk.target = null;
        ctx.marker.visible = false;
      }
      flow.target = step.name;
      ctx.advance();
      return true;
    }
    if (step.type === 'walk') return ctx.beginWalk(step);
    if (step.type === 'then') step.run();
    return true;
  }

  function stepDone(step, dt) {
    if (step.type === 'activity') return flow.target === null;
    if (step.type === 'walk') {
      step.left ||= ctx.isWalkLoop(motion.name) || ctx.leadsToWalk(motion.name);
      return step.left && motion.name === 'home' && !walk.target && !walk.arrive;
    }
    if (step.type === 'settle') {
      const faced = step.yaw === undefined || ctx.turnToward(step.yaw, dt);
      const placed = !step.point || ctx.glideToward(step.point, dt);
      return faced && placed;
    }
    return true;
  }

  function updatePlan(dt) {
    const step = plan.steps[0];
    if (!step) return;
    const needsHome = step.type === 'settle' || step.type === 'then';
    if (needsHome && motion.name !== 'home') {
      if (settledOutsideHome() && !flow.target) defer({ type: 'activity', name: 'home' });
      return;
    }
    if (!step.started) {
      step.started = true;
      if (!startStep(step)) {
        log(`Could not ${describe(step)}; plan dropped.`, true);
        clearPlan();
        return;
      }
      if (plan.steps[0] !== step) return;
    }
    if (stepDone(step, dt)) {
      plan.steps.shift();
      announce();
      if (!plan.steps.length) emit('planDone', describe(step));
    }
  }

  function approachSteps(f) {
    const far = Math.hypot(ctx.rig.position.x - f.entry.x, ctx.rig.position.z - f.entry.z) > ctx.stepAside;
    if (far) return [{ type: 'walk', target: { x: f.entry.x, z: f.entry.z } }, { type: 'settle', yaw: f.facing }];
    return [{ type: 'settle', point: f.entry, yaw: f.facing }];
  }

  function request(target) {
    if (target === 'idle' && !activities.idle) {
      target = ctx.pickIdle();
      if (!target) return log('No idle motions found under the motion folder.', true);
    }
    if (target !== 'home' && !activities[target]) return log(`Unknown activity "${target}"`, true);
    if (target !== 'home' && !ctx.lifeConfig.pool.includes(target) && !ctx.isWalkLoop(activities[target].loop) && ctx.pinned(target)) return;
    motion.queue = [];
    const steps = [];
    for (const act of ctx.chainOf(target)) {
      const approach = act !== 'home' && activities[act]?.approach;
      if (approach && furniture.has(approach) && !ctx.atApproach(approach)) steps.push(...approachSteps(furniture.get(approach)));
    }
    steps.push({ type: 'activity', name: target });
    setPlan(steps);
  }

  function startWalk({ style = 'normal', paces = null, target = null, homeward = false } = {}) {
    if (!homeward && ctx.pinned('walk')) return;
    ctx.view.closeup = false;
    motion.queue = [];
    setPlan([{ type: 'walk', style, paces, target }]);
  }

  function goHome() {
    request('home');
  }

  function walkToSpot(spot, after = []) {
    const yaw = (spot.facing * Math.PI) / 180;
    ctx.view.closeup = false;
    motion.queue = [];
    const via = ctx.doorway(ctx.rig.position, spot);
    if (via || ctx.distanceTo(spot) > ctx.stepAside) setPlan([{ type: 'walk', target: { x: spot.x, z: spot.z }, via }, { type: 'settle', yaw }, ...after]);
    else setPlan([{ type: 'settle', point: { x: spot.x, z: spot.z }, yaw }, ...after]);
  }

  function returnHome() {
    if (ctx.pinned('home')) return;
    walkToSpot(home);
  }

  function closeUp() {
    if (ctx.pinned('close-up')) return;
    const spot = { x: 0, z: 4.4, facing: 0, ...config.closeup };
    if (room.camera) ctx.setCameraMode('room');
    walkToSpot(spot, [{ type: 'then', label: 'close-up', run: () => {
      ctx.view.closeup = true;
      if (room.camera) return;
      ctx.setCameraMode('orbit');
      ctx.aimPoint(controls.target);
      camera.position.set(spot.x, controls.target.y, spot.z + 1.4);
    } }]);
  }

  function goToSpot(name, style = 'normal') {
    if (ctx.pinned('goto')) return;
    const spot = room.spots.get(name.toLowerCase());
    if (!spot) return log(`No spot "${name}". Spots: ${[...room.spots.keys()].join(', ')}`, true);
    log(`Walking to ${name} (${spot.x.toFixed(2)}, ${spot.z.toFixed(2)})`);
    startWalk({ style, target: { x: spot.x, z: spot.z } });
  }

  const autoIdleState = { settled: 0 };
  function autoIdle(dt) {
    const ready = config.autoIdle !== false && (activities.idle || ctx.lifeConfig.pool.length) && motion.name === 'home' && !plan.steps.length && !walk.target && !walk.arrive && !motion.queue.length;
    autoIdleState.settled = ready ? autoIdleState.settled + dt : 0;
    if (autoIdleState.settled > (config.autoIdleDelay ?? 1.0)) {
      autoIdleState.settled = 0;
      request('idle');
    }
  }

  Object.assign(ctx, { plan, describePlan, setPlan, clearPlan, defer, updatePlan, request, startWalk, haltWalk: goHome, goHome, walkToSpot, returnHome, closeUp, goToSpot, autoIdle });
}
