// File: ui/room/sunlight.js

import * as THREE from 'three';

const LEVELS = ['exposure', 'environment', 'key', 'rim', 'fill', 'face'];

export function createSunlight(ctx) {
  const { scene, keyLight, fillLight, lighting, emit } = ctx;
  const sun = new THREE.DirectionalLight(0xffffff, 0);
  sun.visible = false;
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  sun.shadow.bias = -0.0004;
  sun.shadow.normalBias = 0.03;
  scene.add(sun);
  scene.add(sun.target);
  const bounce = new THREE.DirectionalLight(0xffffff, 0);
  bounce.position.set(0, -10, 0);
  bounce.target.position.set(0, 0, 0);
  bounce.visible = false;
  scene.add(bounce);
  scene.add(bounce.target);
  const defaults = { ...lighting };
  const baseSky = fillLight.color.clone();
  const baseGround = fillLight.groundColor.clone();
  const _up = new THREE.Vector3(0, 1, 0);

  function sunDirection(record, cfg) {
    const from = cfg.from && record.scene.getObjectByName(cfg.from);
    if (from?.isDirectionalLight) return new THREE.Vector3(0, 0, -1).applyQuaternion(from.getWorldQuaternion(new THREE.Quaternion())).normalize();
    if (cfg.from) ctx.log(`Sun "${cfg.from}" is not a sun light in the ${record.name} file; using elevation and azimuth.`, true);
    const elevation = THREE.MathUtils.degToRad(cfg.elevation ?? 40);
    const azimuth = THREE.MathUtils.degToRad(cfg.azimuth ?? 0);
    const dir = new THREE.Vector3(-Math.sin(azimuth) * Math.cos(elevation), -Math.sin(elevation), Math.cos(azimuth) * Math.cos(elevation));
    return dir.applyAxisAngle(_up, THREE.MathUtils.degToRad(record.config.rotation || 0)).normalize();
  }

  function applyLamps(record, sunUp) {
    const lamps = record.config.lamps || {};
    const level = Math.max(0, lamps.level ?? 1);
    const on = level > 0 && (!sunUp || lamps.alwaysOn);
    for (const light of record.lightObjects || []) {
      light.visible = on;
      light.intensity = (light.userData.baseIntensity ?? light.intensity) * level;
    }
    return on;
  }

  function applyRoomLighting(record) {
    const cfg = record?.config?.sun;
    const overrides = record?.config?.lights || {};
    for (const name of LEVELS) {
      const value = overrides[name] ?? defaults[name];
      if (lighting[name] !== value) ctx.setLighting(name, value);
    }
    fillLight.color.set(overrides.sky ?? baseSky);
    fillLight.groundColor.set(overrides.ground ?? baseGround);
    bounce.intensity = overrides.bounce ?? 0;
    bounce.color.set(overrides.bounceColor ?? overrides.ground ?? baseGround);
    bounce.visible = bounce.intensity > 0;
    if (!cfg || !record.bounds || record.bounds.isEmpty()) {
      sun.visible = false;
      keyLight.castShadow = true;
      ctx.sun = null;
      applyLamps(record, false);
      emit('sun', null);
      return;
    }
    const dir = sunDirection(record, cfg);
    const centre = record.bounds.getCenter(new THREE.Vector3());
    const radius = record.bounds.getSize(new THREE.Vector3()).length() / 2 + 1;
    sun.target.position.copy(centre);
    sun.position.copy(centre).addScaledVector(dir, -radius * 3);
    Object.assign(sun.shadow.camera, { left: -radius, right: radius, top: radius, bottom: -radius, near: 0.5, far: radius * 6 });
    sun.shadow.camera.updateProjectionMatrix();
    const size = cfg.shadowSize || 2048;
    if (sun.shadow.mapSize.x !== size) {
      sun.shadow.mapSize.set(size, size);
      sun.shadow.map?.dispose();
      sun.shadow.map = null;
    }
    sun.color.set(cfg.color || '#fff4e0');
    sun.intensity = cfg.intensity ?? 3;
    sun.visible = sun.intensity > 0;
    keyLight.castShadow = false;
    const lampsOn = applyLamps(record, sun.visible && sun.intensity >= (record.config.lamps?.onBelow ?? 1));
    ctx.sun = { light: sun, config: cfg, direction: dir, lampsOn };
    emit('sun', { room: record.name, lampsOn, ...cfg });
  }

  function setLamps(level) {
    const record = ctx.rooms.get(ctx.room.name);
    if (!record) return false;
    record.config.lamps = { ...(record.config.lamps || {}), level: Number(level) || 0 };
    applyRoomLighting(record);
    return true;
  }

  function setSun(changes) {
    const record = ctx.rooms.get(ctx.room.name);
    if (!record?.config.sun) return false;
    Object.assign(record.config.sun, changes);
    applyRoomLighting(record);
    return true;
  }

  Object.assign(ctx, { sunLight: sun, applyRoomLighting, setSun, setLamps });
  const current = ctx.rooms.get(ctx.room.name);
  if (current) applyRoomLighting(current);
}
