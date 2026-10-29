// File: ui/room/api.js

export function createApi(ctx) {
  const titleCase = (key) => key.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
  let readyResolve;
  const ready = new Promise((resolve) => { readyResolve = resolve; });
  ctx.on('ready', () => readyResolve(api));

  function capabilities() {
    const { activities, lifeConfig, gestureDefs, expressions, outfitCatalog, room } = ctx;
    const stationary = Object.keys(activities).filter((key) => !ctx.isWalkLoop(activities[key].loop));
    return {
      activities: ['home', 'idle', ...stationary.filter((key) => !lifeConfig.pool.includes(key))].map((key) => ({ key, label: titleCase(key) })),
      idles: lifeConfig.pool.map((key) => ({ key, label: titleCase(key) })),
      gestures: Object.keys(gestureDefs).map((key) => ({ key, label: titleCase(key) })),
      expressions: [...expressions.keys()].map((key) => ({ key, label: titleCase(key) })),
      outfits: [...outfitCatalog.values()].map(({ key, label, everyday }) => ({ key, label, everyday })),
      spots: [...ctx.allSpots.keys()].map((key) => ({ key, label: titleCase(key) })),
      rooms: [...ctx.rooms.keys()].map((key) => ({ key, label: titleCase(key) })),
      turns: ['left', 'right', 'around'].map((key) => ({ key, label: titleCase(key) })),
    };
  }

  function describe() {
    const { motion, rig, outfitState, life, voice, view } = ctx;
    const { act, phase } = ctx.activityOf(motion.name);
    return {
      activity: act,
      phase,
      motion: motion.name,
      plan: ctx.describePlan(),
      busy: Boolean(ctx.plan.steps.length || ctx.walk.target),
      position: { x: Number(rig.position.x.toFixed(2)), z: Number(rig.position.z.toFixed(2)) },
      room: ctx.room.name,
      outfit: outfitState.current,
      listening: life.listening,
      speaking: ctx.speech.on,
      voice: voice.on,
      camera: view.mode,
    };
  }

  const state = { listening: false, speaking: false, thinking: false };
  function setState(changes) {
    for (const [key, value] of Object.entries(changes)) {
      if (!(key in state) || state[key] === Boolean(value)) continue;
      state[key] = Boolean(value);
      if (key === 'listening') ctx.setListening(state.listening);
      if (key === 'speaking') {
        ctx.setSpeaking(state.speaking);
        if (!ctx.speechLevelsSeen) ctx.setTalk(state.speaking);
      }
      if (key === 'thinking' && state.thinking && ctx.gestureDefs.small_tilt) ctx.playGesture('small_tilt');
    }
    ctx.emit('state', { ...state });
    return { ...state };
  }

  function goTo(where, style = 'normal') {
    if (typeof where === 'string') return ctx.goToSpot(where, style);
    return ctx.startWalk({ style, target: { x: where.x, z: where.z } });
  }

  function express(name, weight = 1) {
    if (!ctx.expressions.has(name)) return ctx.log(`No expression "${name}"`, true);
    for (const other of ctx.expressions.keys()) if (other !== name) ctx.setExpression(other, 0);
    ctx.setExpression(name, Math.min(1, Math.max(0, weight)));
  }

  function neutral() {
    for (const name of ctx.expressions.keys()) ctx.setExpression(name, 0);
  }

  const api = {
    ready,
    capabilities,
    describe,
    do: (activity) => ctx.request(activity),
    home: () => ctx.returnHome(),
    goTo,
    wander: (spot) => ctx.wander(spot),
    turn: (direction, degrees) => ctx.runCommand(`turn ${direction}${degrees ? ` ${degrees}` : ''}`),
    gesture: (name) => ctx.playGesture(name),
    express,
    neutral,
    outfit: (name) => ctx.setOutfit(name),
    setState,
    speechLevel: (level) => {
      ctx.speechLevelsSeen = true;
      ctx.setSpeechLevel(level);
    },
    voice: (on) => ctx.setVoice(on),
    camera: (mode) => ctx.setCameraMode(mode),
    command: (text) => ctx.runCommand(text),
    on: ctx.on,
  };
  ctx.api = api;
  return api;
}
