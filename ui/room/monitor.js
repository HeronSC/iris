// File: ui/room/monitor.js

import * as THREE from 'three';

export function createMonitor(ctx) {
  const { config, rooms, renderer, camera, log, emit } = ctx;
  const meshes = [];
  for (const record of rooms.values()) {
    const pattern = new RegExp(record.config.monitor || '^Monitor_Screen', 'i');
    record.scene.traverse((o) => {
      if (!o.isMesh) return;
      let node = o;
      while (node && node !== record.scene) {
        if (pattern.test(node.name)) { meshes.push(o); break; }
        node = node.parent;
      }
    });
  }
  const screenMaterial = new RegExp(config.monitorScreenMaterial || 'screen|glass|display', 'i');
  const named = meshes.filter((mesh) => screenMaterial.test(mesh.material?.name || ''));
  const screens = named.length ? named : meshes;
  const monitor = { meshes, screens, hover: false, glow: 0.12, picture: null, showPicture };
  if (!meshes.length) {
    if (rooms.size) log('No monitor in any room file; clicking it is off.');
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
    for (const mesh of screens) {
      const base = mesh.userData.baseEmissive;
      if (!base) continue;
      if (monitor.picture) {
        mesh.material.emissiveIntensity = on ? 1.25 : 1;
        continue;
      }
      mesh.material.emissive.copy(base);
      if (on) mesh.material.emissive.addScalar(monitor.glow);
    }
  }

  function planarUvs(geometry) {
    if (geometry.attributes.uv) return;
    geometry.computeBoundingBox();
    const { min, max } = geometry.boundingBox;
    const size = [max.x - min.x, max.y - min.y, max.z - min.z];
    const thin = size.indexOf(Math.min(...size));
    const [a, b] = [0, 1, 2].filter((axis) => axis !== thin);
    const position = geometry.attributes.position;
    const uv = new Float32Array(position.count * 2);
    const lo = [min.x, min.y, min.z];
    for (let i = 0; i < position.count; i += 1) {
      const point = [position.getX(i), position.getY(i), position.getZ(i)];
      uv[i * 2] = (point[a] - lo[a]) / (size[a] || 1);
      uv[i * 2 + 1] = (point[b] - lo[b]) / (size[b] || 1);
    }
    geometry.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
  }

  function showPicture(url) {
    new THREE.TextureLoader().load(url, (texture) => {
      texture.colorSpace = THREE.SRGBColorSpace;
      texture.flipY = false;
      if (monitor.picture) monitor.picture.dispose();
      monitor.picture = texture;
      for (const mesh of screens) {
        planarUvs(mesh.geometry);
        const material = mesh.material;
        material.map = texture;
        if (material.emissive) {
          material.emissiveMap = texture;
          material.emissive.set(0xffffff);
          material.emissiveIntensity = monitor.hover ? 1.25 : 1;
          material.color.set(0x000000);
        }
        material.needsUpdate = true;
      }
    }, undefined, () => log('The monitor picture could not be read.', true));
  }

  canvas.addEventListener('pointermove', (e) => setHover(ctx.roomsVisible && hits(e)));
  canvas.addEventListener('pointerleave', () => setHover(false));
  canvas.addEventListener('pointerdown', (e) => { press.x = e.clientX; press.y = e.clientY; press.down = e.button === 0; });
  canvas.addEventListener('pointerup', (e) => {
    if (!press.down) return;
    press.down = false;
    if (Math.hypot(e.clientX - press.x, e.clientY - press.y) > 6) return;
    if (!ctx.roomsVisible || !hits(e)) return;
    log('Monitor clicked.');
    emit('monitorClicked');
  });
  Object.assign(ctx, { monitor });
}
