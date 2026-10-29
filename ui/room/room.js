// File: ui/room/room.js

import * as THREE from 'three';

export function loadRoom(ctx) {
  const { config, gltfs, scene, floor: ground, grid, log, emit } = ctx;
  const rooms = new Map();
  const room = { name: null, config: null, scene: null, spots: new Map(), camera: null, bounds: null, place: new THREE.Matrix4() };
  let current = null;

  for (const [name, roomConfig] of Object.entries(config.rooms || {})) {
    const roomGltf = gltfs.get(roomConfig.file);
    if (!roomGltf) {
      log(`Room "${name}": ${roomConfig.file} did not load.`, true);
      continue;
    }
    const record = { name, config: roomConfig, scene: roomGltf.scene, spots: new Map(), camera: null, bounds: null, place: new THREE.Matrix4(), floor: null };
    record.scene.traverse((o) => {
      if (o.isMesh) {
        o.castShadow = true;
        o.receiveShadow = true;
      }
    });
    const origin = roomConfig.origin || [0, 0, 0];
    const rotation = THREE.MathUtils.degToRad(roomConfig.rotation || 0);
    record.place.makeRotationY(rotation).setPosition(origin[0], 0, origin[2]);
    record.scene.position.set(origin[0], 0, origin[2]);
    record.scene.rotation.y = rotation;
    scene.add(record.scene);
    record.scene.updateMatrixWorld(true);
    const floor = roomConfig.floor && record.scene.getObjectByName(roomConfig.floor);
    if (roomConfig.floor && !floor) log(`Room "${name}": floor "${roomConfig.floor}" is not in the file.`, true);
    record.floor = floor || null;
    const floorTop = floor ? new THREE.Box3().setFromObject(floor).max.y : 0;
    if (Math.abs(floorTop) > 0.01) {
      log(`Room "${name}": floor sits at ${floorTop.toFixed(2)} m; lowered it so she stands on it.`);
      record.scene.position.y -= floorTop;
      record.scene.updateMatrixWorld(true);
    }
    const prefix = roomConfig.spotPrefix || 'Spot_';
    const suffixes = (roomConfig.spotSuffixes || []).map((sf) => sf.toLowerCase());
    record.scene.traverse((o) => {
      const lower = o.name.toLowerCase();
      if (o.name.startsWith(prefix)) record.spots.set(o.name.slice(prefix.length).toLowerCase(), o.getWorldPosition(new THREE.Vector3()));
      else if (suffixes.some((sf) => lower.endsWith(sf))) record.spots.set(lower, o.getWorldPosition(new THREE.Vector3()));
      if (o.isCamera && !record.camera) record.camera = o;
    });
    if (!record.camera && roomConfig.camera?.position) {
      record.camera = new THREE.PerspectiveCamera(roomConfig.camera.fov || 50);
      record.camera.position.fromArray(roomConfig.camera.position).applyMatrix4(record.place);
    }
    const walls = new RegExp(roomConfig.walls || 'Wall', 'i');
    record.bounds = new THREE.Box3();
    record.scene.traverse((o) => { if (o.isMesh && walls.test(o.name)) record.bounds.expandByObject(o); });
    if (record.bounds.isEmpty()) {
      log(`Room "${name}": no walls matching "${walls.source}"; using everything for its bounds.`, true);
      record.bounds.setFromObject(record.scene);
    }
    const noShadow = new RegExp(roomConfig.noShadowFrom || 'Wall|Ceiling|Door|Window|Trim|Sill|Floor|Plane|Shell', 'i');
    record.scene.traverse((o) => { if (o.isMesh && noShadow.test(o.name)) o.castShadow = false; });
    const imported = [];
    record.lightObjects = [];
    record.scene.traverse((o) => {
      if (!o.isLight) return;
      o.intensity *= roomConfig.lightScale ?? 1;
      o.userData.baseIntensity = o.intensity;
      record.lightObjects.push(o);
      if ((roomConfig.shadowLights || []).includes(o.name) && (o.isSpotLight || o.isPointLight)) {
        o.castShadow = true;
        o.shadow.mapSize.set(1024, 1024);
        o.shadow.bias = -0.0005;
        o.shadow.normalBias = 0.02;
        o.shadow.camera.near = 0.2;
      }
      imported.push(`${o.name} (${o.type.replace('Light', '').toLowerCase()} ${o.intensity.toFixed(1)})`);
    });
    record.lights = imported;
    if (imported.length) log(`Room "${name}" lights: ${imported.join(', ')}.`);
    const hidden = roomConfig.hide || [];
    record.scene.traverse((o) => { if (hidden.some((h) => o.name.startsWith(h))) o.visible = false; });
    rooms.set(name, record);
  }
  if (rooms.size) {
    grid.visible = false;
    ground.visible = false;
  }

  const inBounds = (bounds, p, margin) => p.x > bounds.min.x + margin && p.x < bounds.max.x - margin && p.z > bounds.min.z + margin && p.z < bounds.max.z - margin;

  function roomAt(position) {
    for (const record of rooms.values()) if (inBounds(record.bounds, position, 0)) return record;
    return null;
  }

  function insideRoom(position, margin = 0) {
    if (!room.bounds) return true;
    return inBounds(room.bounds, position, margin);
  }

  function setRoom(name, { cut = true } = {}) {
    const record = rooms.get(name);
    if (!record || record === current) return false;
    current = record;
    Object.assign(room, { name, config: record.config, scene: record.scene, spots: record.spots, camera: record.camera, bounds: record.bounds, place: record.place });
    config.room = record.config;
    for (const other of rooms.values()) if (other.floor) other.floor.visible = other === record;
    if (ctx.applySpots) ctx.applySpots(record);
    if (ctx.applyRoomLighting) ctx.applyRoomLighting(record);
    if (cut && ctx.view?.mode === 'room' && record.camera) ctx.setCameraMode('room');
    emit('room', name);
    return true;
  }

  function updateRoomZone() {
    if (rooms.size < 2 || !ctx.rig) return;
    const here = roomAt(ctx.rig.position);
    if (here && here !== current) {
      log(`She is in the ${here.name}.`);
      setRoom(here.name);
    }
  }

  function setRoomVisible(on) {
    if (!rooms.size) return;
    for (const record of rooms.values()) record.scene.visible = on;
    ctx.roomsVisible = on;
    grid.visible = !on;
    ground.visible = !on;
    emit('roomVisible', on);
  }

  Object.assign(ctx, { rooms, room, roomAt, insideRoom, setRoom, updateRoomZone, setRoomVisible, roomsVisible: true });
  const startName = rooms.has(ctx.startRoomName) ? ctx.startRoomName : rooms.keys().next().value;
  if (startName) setRoom(startName, { cut: false });
}

export function placeSpots(ctx) {
  const { config, rooms, room, log, rig } = ctx;
  const furniture = new Map();
  const allSpots = new Map();

  function placeSpot(record, label, spot) {
    const placed = { x: 0, z: 0, facing: 0, ...spot };
    const rotation = THREE.MathUtils.degToRad(record.config.rotation || 0);
    const local = new THREE.Vector3(placed.x, 0, placed.z);
    const anchor = placed.anchor && record.scene.getObjectByName(placed.anchor);
    if (placed.anchor && !anchor) log(`${record.name} ${label} spot: missing object ${placed.anchor}`, true);
    if (anchor) local.applyAxisAngle(new THREE.Vector3(0, 1, 0), rotation).add(anchor.getWorldPosition(new THREE.Vector3()));
    else local.applyMatrix4(record.place);
    placed.x = local.x;
    placed.z = local.z;
    placed.facing += THREE.MathUtils.radToDeg(rotation);
    placed.room = record.name;
    return placed;
  }

  for (const record of rooms.values()) {
    record.furniture = new Map();
    for (const [name, f] of Object.entries(record.config.furniture || {})) {
      const anchorObject = record.scene.getObjectByName(f.anchor);
      if (!anchorObject) {
        log(`${record.name} furniture "${name}": missing object ${f.anchor}`, true);
        continue;
      }
      const anchor = anchorObject.getWorldPosition(new THREE.Vector3());
      const entry = new THREE.Vector3(anchor.x, 0, anchor.z);
      const facing = THREE.MathUtils.degToRad((f.facing || 0) + (record.config.rotation || 0));
      record.furniture.set(name, { entry, facing, room: record.name });
    }
    const spots = record.config.spots || {};
    record.home = placeSpot(record, 'home', spots.home);
    record.door = spots.door ? placeSpot(record, 'door', spots.door) : null;
    record.away = spots.away ? placeSpot(record, 'away', spots.away) : null;
    record.closeupSpot = spots.closeup ? placeSpot(record, 'close-up', spots.closeup) : null;
    for (const [key, spot] of Object.entries(spots)) if (!['home', 'door', 'away', 'closeup'].includes(key)) record.spots.set(key.toLowerCase(), placeSpot(record, key, spot));
    for (const [key, value] of record.spots) allSpots.set(rooms.size > 1 ? `${record.name}.${key}` : key, value);
  }

  function applySpots(record) {
    furniture.clear();
    for (const other of rooms.values()) for (const [name, f] of other.furniture || []) if (!furniture.has(name) || other === record) furniture.set(name, f);
    furniture.set('home', { entry: new THREE.Vector3(record.home.x, 0, record.home.z), facing: THREE.MathUtils.degToRad(record.home.facing), room: record.name });
    Object.assign(ctx, { home: record.home, door: record.door, away: record.away, closeupSpot: record.closeupSpot });
  }

  function doorway(from, to) {
    const a = ctx.roomAt(from);
    const b = ctx.roomAt(to);
    if (a === b) return null;
    const via = [];
    if (a?.door) via.push({ x: a.door.x, z: a.door.z });
    if (a?.away && b) via.push({ x: a.away.x, z: a.away.z });
    if (b?.away && a) via.push({ x: b.away.x, z: b.away.z });
    if (b?.door) via.push({ x: b.door.x, z: b.door.z });
    return via.length ? via : null;
  }

  Object.assign(ctx, { furniture, allSpots, applySpots, doorway });
  const first = rooms.get(room.name);
  if (!first) {
    rig.position.set(0, 0, 0);
    return;
  }
  applySpots(first);
  const outside = first.config.startOutside ?? rooms.size === 1;
  const start = outside && first.away ? first.away : first.home;
  rig.position.set(start.x, 0, start.z);
  rig.rotation.y = THREE.MathUtils.degToRad(start.facing);
}
