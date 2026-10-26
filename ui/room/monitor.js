// File: ui/room/monitor.js

import * as THREE from 'three';

export function createMonitor(ctx) {
  const { config, room, renderer, camera, log, emit } = ctx;
  const pattern = new RegExp(config.room?.monitor || '^Monitor_Screen', 'i');
  const meshes = [];
  room.scene?.traverse((o) => {
    if (!o.isMesh) return;
    let node = o;
    while (node && node !== room.scene) {
      if (pattern.test(node.name)) { meshes.push(o); break; }
      node = node.parent;
    }
  });
  const monitor = { meshes, hover: false, glow: 0.12 };
  if (!meshes.length) {
    if (room.scene) log(`No monitor matching "${pattern.source}" in the room file; clicking it is off.`);
    return;
  }
  for (const mesh of meshes) {
    mesh.material = mesh.material.clone();
    mesh.userData.baseEmissive = mesh.material.emissive ? mesh.material.emissive.clone() : null;
  }

  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  const press = { x: 0, y: 0, down: false };
  const canvas = renderer.domElement;

  function hits(e) {
    const r = canvas.getBoundingClientRect();
    pointer.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    return raycaster.intersectObjects(meshes, false).length > 0;
  }

  function setHover(on) {
    if (on === monitor.hover) return;
    monitor.hover = on;
    canvas.style.cursor = on ? 'pointer' : '';
    for (const mesh of meshes) {
      const base = mesh.userData.baseEmissive;
      if (!base) continue;
      mesh.material.emissive.copy(base);
      if (on) mesh.material.emissive.addScalar(monitor.glow);
    }
  }

  canvas.addEventListener('pointermove', (e) => setHover(room.scene?.visible && hits(e)));
  canvas.addEventListener('pointerleave', () => setHover(false));
  canvas.addEventListener('pointerdown', (e) => { press.x = e.clientX; press.y = e.clientY; press.down = e.button === 0; });
  canvas.addEventListener('pointerup', (e) => {
    if (!press.down) return;
    press.down = false;
    if (Math.hypot(e.clientX - press.x, e.clientY - press.y) > 6) return;
    if (!room.scene?.visible || !hits(e)) return;
    log('Monitor clicked.');
    emit('monitorClicked');
  });
  Object.assign(ctx, { monitor });
}
