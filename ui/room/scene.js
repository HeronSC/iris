// File: ui/room/scene.js

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

export function createScene(ctx, container) {
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  container.prepend(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x15171a);
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.6;

  const camera = new THREE.PerspectiveCamera(32, 1, 0.05, 100);
  camera.position.set(0, 1.2, 4);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0, 0.88, 0);
  controls.enableDamping = true;
  controls.update();

  const keyLight = new THREE.DirectionalLight(0xffffff, 2.2);
  keyLight.position.set(1.5, 3, 2.5);
  keyLight.castShadow = true;
  keyLight.shadow.mapSize.set(2048, 2048);
  Object.assign(keyLight.shadow.camera, { left: -1.5, right: 1.5, top: 2.2, bottom: -0.5, near: 0.5, far: 8 });
  keyLight.shadow.bias = -0.0005;
  keyLight.shadow.normalBias = 0.02;
  scene.add(keyLight);
  scene.add(keyLight.target);
  const keyOffset = keyLight.position.clone();
  const marker = new THREE.Mesh(new THREE.RingGeometry(0.18, 0.24, 48), new THREE.MeshBasicMaterial({ color: 0x7cb7ff }));
  marker.rotation.x = -Math.PI / 2;
  marker.position.y = 0.003;
  marker.visible = false;
  scene.add(marker);
  const rimLight = new THREE.DirectionalLight(0xbfd4ff, 1.2);
  rimLight.position.set(-2, 2.5, -2.5);
  scene.add(rimLight);
  const fillLight = new THREE.HemisphereLight(0xffffff, 0x2a2622, 0.4);
  scene.add(fillLight);

  const floor = new THREE.Mesh(new THREE.CircleGeometry(40, 96), new THREE.MeshStandardMaterial({ color: 0x24272b, roughness: 0.95 }));
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  scene.add(floor);
  const grid = new THREE.GridHelper(80, 80, 0x3a4048, 0x2c3137);
  grid.position.y = 0.001;
  scene.add(grid);

  function resize() {
    const width = container.clientWidth;
    const height = container.clientHeight;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(container);
  resize();

  const lighting = { exposure: 0.8, environment: 0.35, key: 1.4, rim: 0.6, fill: 0.2, ...(ctx.config.lighting || {}) };
  const lightTargets = {
    exposure: (v) => { renderer.toneMappingExposure = v; },
    environment: (v) => { scene.environmentIntensity = v; },
    key: (v) => { keyLight.intensity = v; },
    rim: (v) => { rimLight.intensity = v; },
    fill: (v) => { fillLight.intensity = v; },
  };
  function setLighting(name, value) {
    if (!lightTargets[name]) return;
    lighting[name] = value;
    lightTargets[name](value);
    ctx.emit('lighting', { name, value });
  }
  for (const [name, value] of Object.entries(lighting)) if (lightTargets[name]) lightTargets[name](value);

  Object.assign(ctx, { renderer, scene, camera, controls, keyLight, keyOffset, marker, rimLight, fillLight, floor, grid, lighting, setLighting });
}
