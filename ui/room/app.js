// File: ui/room/app.js

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const $ = (id) => document.getElementById(id);
const stage = $('stage');

function log(message, isError = false) {
  const box = $('log');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  const line = document.createElement('div');
  if (isError) line.className = 'err';
  line.textContent = message;
  box.append(line);
  if (atBottom) box.scrollTop = box.scrollHeight;
}
$('clearLog').onclick = () => { $('log').replaceChildren(); };

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFShadowMap;
stage.prepend(renderer.domElement);

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
  const width = stage.clientWidth;
  const height = stage.clientHeight;
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(stage);
resize();

const config = await (await fetch('avatar.json')).json();
const loader = new GLTFLoader();
const progress = new Map();

function showProgress() {
  let loaded = 0;
  let total = 0;
  for (const p of progress.values()) {
    loaded += p.loaded;
    total += p.total;
  }
  $('loadFill').style.width = total ? `${(100 * loaded) / total}%` : '0';
  $('loadLabel').textContent = `Loading ${(loaded / 1e6).toFixed(0)} / ${(total / 1e6).toFixed(0)} MB`;
}

function loadFile(url) {
  progress.set(url, { loaded: 0, total: 0 });
  return loader.loadAsync(encodeURI(url), (e) => {
    progress.set(url, { loaded: e.loaded, total: e.total || e.loaded });
    showProgress();
  });
}

const heelSuffix = config.heelsSuffix || '-HighHeels';
const heelsFiles = new Map();
await Promise.all([...new Set(Object.values(config.clips).map((c) => c.file))].map(async (file) => {
  const candidate = file.replace(/\.glb$/i, `${heelSuffix}.glb`);
  try {
    const res = await fetch(encodeURI(candidate), { method: 'HEAD', cache: 'no-store' });
    if (res.ok) heelsFiles.set(file, candidate);
  } catch {}
}));
const files = [...new Set([config.character, ...(config.room ? [config.room.file] : []), ...Object.values(config.clips).map((c) => c.file), ...heelsFiles.values(), ...(config.extraFiles || [])])];
const gltfs = new Map();
try {
  const loaded = await Promise.all(files.map(loadFile));
  files.forEach((f, i) => gltfs.set(f, loaded[i]));
} catch (err) {
  $('loadLabel').textContent = `Load failed: ${err.message || err}`;
  throw err;
}

const model = gltfs.get(config.character).scene;
let meshCount = 0;
model.traverse((o) => { if (o.isMesh) meshCount += 1; });
if (!meshCount) {
  $('loadLabel').textContent = `${config.character} has no meshes in it. Re-export it from Blender.`;
  throw new Error(`${config.character} is empty`);
}
const strays = model.children.filter((child) => {
  let skinned = false;
  child.traverse((o) => { if (o.isSkinnedMesh || o.isBone) skinned = true; });
  return !skinned;
});
for (const stray of strays) model.remove(stray);
const boneAliases = config.boneAliases || {};
const canonicalBone = (name) => boneAliases[name] || name;
model.traverse((o) => {
  if (o.isBone && boneAliases[o.name]) {
    log(`Bone "${o.name}" in ${config.character} is treated as ${boneAliases[o.name]}.`);
    o.name = boneAliases[o.name];
  }
});
const restPose = new Map();
const restPosition = new Map();
model.traverse((o) => {
  if (!o.isBone) return;
  restPose.set(o.name, o.quaternion.clone());
  restPosition.set(o.name, o.position.clone());
});

function attachOutfit(gltf, label) {
  const armature = findBone('CC_Base_BoneRoot')?.parent;
  const pieces = [];
  gltf.scene.traverse((o) => { if (o.isSkinnedMesh) pieces.push(o); });
  const attached = [];
  for (const mesh of pieces) {
    const skeletonBones = mesh.skeleton.bones.map((b) => findBone(canonicalBone(b.name)));
    const missing = mesh.skeleton.bones.filter((b, i) => !skeletonBones[i]).map((b) => b.name);
    if (missing.length) {
      log(`${label}: ${mesh.name} uses bones Iris does not have: ${missing.slice(0, 5).join(', ')}`, true);
      continue;
    }
    const bindMatrix = mesh.bindMatrix.clone();
    const inverses = mesh.skeleton.boneInverses.map((m) => m.clone());
    mesh.removeFromParent();
    mesh.position.set(0, 0, 0);
    mesh.quaternion.identity();
    mesh.scale.set(1, 1, 1);
    armature.add(mesh);
    mesh.bind(new THREE.Skeleton(skeletonBones, inverses), bindMatrix);
    mesh.visible = false;
    attached.push(mesh);
  }
  return attached;
}
const rig = new THREE.Group();
rig.add(model);
scene.add(rig);

const room = { scene: null, spots: new Map(), camera: null };
if (config.room) {
  const roomGltf = gltfs.get(config.room.file);
  room.scene = roomGltf.scene;
  room.scene.traverse((o) => {
    if (o.isMesh) {
      o.castShadow = true;
      o.receiveShadow = true;
    }
  });
  scene.add(room.scene);
  room.scene.updateMatrixWorld(true);
  const prefix = config.room.spotPrefix || 'Spot_';
  const suffixes = (config.room.spotSuffixes || []).map((sf) => sf.toLowerCase());
  room.scene.traverse((o) => {
    const lower = o.name.toLowerCase();
    if (o.name.startsWith(prefix)) room.spots.set(o.name.slice(prefix.length).toLowerCase(), o.getWorldPosition(new THREE.Vector3()));
    else if (suffixes.some((sf) => lower.endsWith(sf))) room.spots.set(lower, o.getWorldPosition(new THREE.Vector3()));
    if (o.isCamera && !room.camera) room.camera = o;
  });
  if (!room.camera && config.room.camera?.position) {
    room.camera = new THREE.PerspectiveCamera(config.room.camera.fov || 50);
    room.camera.position.fromArray(config.room.camera.position);
  }
  grid.visible = false;
  room.bounds = new THREE.Box3().setFromObject(room.scene);
  const noShadow = new RegExp(config.room.noShadowFrom || 'Wall|Ceiling|Door|Window|Trim|Sill|Floor|Plane|Shell', 'i');
  room.scene.traverse((o) => { if (o.isMesh && noShadow.test(o.name)) o.castShadow = false; });
  const hidden = config.room.hide || [];
  room.scene.traverse((o) => { if (hidden.some((h) => o.name.startsWith(h))) o.visible = false; });
}

const startsAny = (name, list = []) => list.some((p) => name.startsWith(p));
const rules = config.materials || {};
function applyMaterialRules(o) {
  if (!o.isMesh) return;
  o.castShadow = true;
  o.receiveShadow = true;
  o.frustumCulled = false;
  for (const m of [].concat(o.material)) {
    const name = m.name || '';
    if (startsAny(name, rules.opaque)) {
      m.transparent = false;
      m.alphaTest = 0;
      m.depthWrite = true;
    } else if (startsAny(name, rules.mask)) {
      m.transparent = false;
      m.alphaTest = 0.5;
      m.depthWrite = true;
    } else if (startsAny(name, rules.hair)) {
      m.transparent = false;
      m.alphaTest = 0.1;
      m.alphaToCoverage = true;
      m.depthWrite = true;
      m.side = THREE.DoubleSide;
    } else if (startsAny(name, rules.blend)) {
      m.transparent = true;
      m.alphaTest = 0;
      m.depthWrite = false;
      o.renderOrder = 1;
    } else if (startsAny(name, rules.glass)) {
      m.transparent = true;
      m.depthWrite = false;
      o.renderOrder = 2;
    }
    m.needsUpdate = true;
  }
}
model.traverse(applyMaterialRules);

function texturePixels(texture) {
  const img = texture.image;
  const canvas = document.createElement('canvas');
  canvas.width = img.width;
  canvas.height = img.height;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  const data = ctx.getImageData(0, 0, img.width, img.height).data;
  return (u, v) => {
    const x = Math.min(img.width - 1, Math.max(0, Math.floor((u - Math.floor(u)) * img.width)));
    const y = Math.min(img.height - 1, Math.max(0, Math.floor((v - Math.floor(v)) * img.height)));
    const i = (y * img.width + x) * 4;
    return [data[i], data[i + 1], data[i + 2]];
  };
}

function matchSkinSeams() {
  const meshes = new Map();
  model.traverse((o) => { if (o.isMesh) meshes.set([].concat(o.material)[0].name.split('.')[0], o); });
  for (const rule of config.seamMatch || []) {
    const part = meshes.get(rule.part);
    const reference = meshes.get(rule.to);
    if (!part || !reference || !part.material.map || !reference.material.map) {
      log(`Seam match: missing ${part ? rule.to : rule.part}`, true);
      continue;
    }
    const key = (x, y, z) => `${Math.round(x * 2000)},${Math.round(y * 2000)},${Math.round(z * 2000)}`;
    const refPos = reference.geometry.attributes.position;
    const refUv = reference.geometry.attributes.uv;
    const lookup = new Map();
    for (let i = 0; i < refPos.count; i++) lookup.set(key(refPos.getX(i), refPos.getY(i), refPos.getZ(i)), i);
    const partPos = part.geometry.attributes.position;
    const partUv = part.geometry.attributes.uv;
    const samplePart = texturePixels(part.material.map);
    const sampleRef = texturePixels(reference.material.map);
    const sumPart = [0, 0, 0];
    const sumRef = [0, 0, 0];
    let shared = 0;
    for (let i = 0; i < partPos.count; i++) {
      const j = lookup.get(key(partPos.getX(i), partPos.getY(i), partPos.getZ(i)));
      if (j === undefined) continue;
      const a = samplePart(partUv.getX(i), partUv.getY(i));
      const b = sampleRef(refUv.getX(j), refUv.getY(j));
      for (let c = 0; c < 3; c++) {
        sumPart[c] += a[c];
        sumRef[c] += b[c];
      }
      shared++;
    }
    if (shared < 10) {
      log(`Seam match: ${rule.part} and ${rule.to} share only ${shared} points`, true);
      continue;
    }
    const toLinear = (v) => Math.pow(v / 255, 2.2);
    const ratio = [0, 1, 2].map((c) => toLinear(sumRef[c] / shared) / toLinear(sumPart[c] / shared));
    part.material.color.setRGB(...ratio);
    log(`Seam match: ${rule.part} tinted (${ratio.map((v) => v.toFixed(2)).join(', ')}) to meet ${rule.to} at ${shared} seam points.`);
  }
}
matchSkinSeams();

const parts = new Map();
model.traverse((o) => {
  if (!o.isMesh) return;
  const grouped = o.parent && o.parent !== model && !o.parent.isBone && o.parent.children.every((c) => c.isMesh);
  const part = grouped ? o.parent : o;
  parts.set(part.name, part);
});
const partBoxes = new Map();
function addPartRow(name, part) {
  const label = document.createElement('label');
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.checked = part.visible;
  partBoxes.set(name, box);
  box.onchange = () => { part.visible = box.checked; };
  label.append(box, name);
  label.title = name;
  $('parts').append(label);
}
for (const [name, part] of [...parts].sort((a, b) => a[0].localeCompare(b[0]))) {
  part.visible = !startsAny(name, config.hiddenParts);
  addPartRow(name, part);
}

const outfitCatalog = new Map();
const titleCase = (key) => key.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
for (const [key, o] of Object.entries(config.outfits || {})) {
  outfitCatalog.set(key, { key, label: o.label || titleCase(key), file: o.file || null, parts: o.parts || null, meshes: null, loading: null });
}
if (config.outfitFolder) {
  try {
    const listing = await (await fetch(`${config.outfitFolder}/`, { cache: 'no-store' })).text();
    const names = [...listing.matchAll(/href="([^"]+\.glb)"/gi)].map((m) => decodeURIComponent(m[1]));
    for (const fileName of names.sort((a, b) => a.localeCompare(b))) {
      const label = fileName.replace(/\.glb$/i, '');
      const key = label.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
      if (!outfitCatalog.has(key)) outfitCatalog.set(key, { key, label, file: `${config.outfitFolder}/${fileName}`, parts: null, meshes: null, loading: null });
    }
  } catch (err) {
    log(`Could not list outfits in ${config.outfitFolder}: ${err.message || err}`, true);
  }
}

const slots = {
  feet: new RegExp(config.outfitSlots?.feet || 'shoe|boot|heel|sandal|sneaker', 'i'),
  bottoms: new RegExp(config.outfitSlots?.bottoms || 'underwear_bottom|panties|panty|brief', 'i'),
};
const ownClothing = config.clothing || [];
const outfitState = { current: null, requested: null };

function ensureOutfitLoaded(entry) {
  if (!entry.file || entry.meshes) return Promise.resolve(entry.meshes);
  entry.loading ||= loader.loadAsync(encodeURI(entry.file)).then((gltf) => {
    entry.meshes = attachOutfit(gltf, entry.label);
    for (const mesh of entry.meshes) {
      applyMaterialRules(mesh);
      const name = `${entry.label}: ${mesh.name}`;
      parts.set(name, mesh);
      addPartRow(name, mesh);
    }
    if (!entry.meshes.length) log(`${entry.label}: no clothing found in the file.`, true);
    return entry.meshes;
  });
  return entry.loading;
}

function findOutfit(text) {
  const want = text.toLowerCase().replace(/[^a-z0-9]+/g, '_');
  return outfitCatalog.get(want) || [...outfitCatalog.values()].find((e) => e.key.startsWith(want) || e.label.toLowerCase().startsWith(text.toLowerCase()));
}

const heel = { angle: 0, toe: 0, lift: 0, bones: [] };

function bindSpaceVertices(mesh) {
  const pos = mesh.geometry.attributes.position;
  const si = mesh.geometry.attributes.skinIndex;
  const sw = mesh.geometry.attributes.skinWeight;
  const out = [];
  const v = new THREE.Vector3();
  for (let i = 0; i < pos.count; i++) {
    v.fromBufferAttribute(pos, i).applyMatrix4(mesh.bindMatrix);
    let best = 0;
    for (let k = 1; k < 4; k++) if (sw.getComponent(i, k) > sw.getComponent(i, best)) best = k;
    out.push({ p: v.clone(), bone: mesh.skeleton.bones[si.getComponent(i, best)].name });
  }
  return out;
}

function bindPosition(mesh, name) {
  const i = mesh.skeleton.bones.findIndex((b) => b.name === name);
  return i < 0 ? null : new THREE.Vector3().setFromMatrixPosition(mesh.skeleton.boneInverses[i].clone().invert());
}

function heelFromSkeleton(shoe, body) {
  const at = (mesh, name) => bindPosition(mesh, name);
  const pitch = (a, b) => Math.atan2(a.y - b.y, b.z - a.z);
  const sides = [];
  for (const side of ['L', 'R']) {
    const names = [`CC_Base_${side}_Foot`, `CC_Base_${side}_ToeBase`, `CC_Base_${side}_MidToe1`];
    const s = names.map((n) => at(shoe, n));
    const b = names.map((n) => at(body, n));
    if (s.some((v) => !v) || b.some((v) => !v)) continue;
    const foot = pitch(s[0], s[1]) - pitch(b[0], b[1]);
    const toe = pitch(s[1], s[2]) - pitch(b[1], b[2]);
    sides.push({ foot, toe: toe - foot, lift: s[0].y - b[0].y });
  }
  if (!sides.length) return null;
  const avg = (k) => sides.reduce((a, v) => a + v[k], 0) / sides.length;
  if (avg('foot') < THREE.MathUtils.degToRad(config.minHeelAngle ?? 8) || avg('lift') < 0.01) return null;
  return { lift: avg('lift'), angle: avg('foot'), toe: avg('toe') };
}

function updateHeelPose(entry) {
  heel.angle = 0;
  heel.toe = 0;
  heel.lift = 0;
  model.position.y = 0;
  const shoes = (entry.meshes || []).filter((m) => m.visible && m.isSkinnedMesh && slots.feet.test(m.name));
  if (!shoes.length) return;
  let body = null;
  model.traverse((o) => { if (o.isSkinnedMesh && /Std_Skin_Leg/.test([].concat(o.material)[0].name)) body = o; });
  if (!body) return;
  const solved = heelFromSkeleton(shoes[0], body);
  if (!solved) {
    const relLow = (mesh, filter) => {
      const ankle = bindPosition(mesh, 'CC_Base_L_Foot');
      const ys = bindSpaceVertices(mesh).filter((v) => filter(v.bone)).map((v) => v.p.y);
      return ankle && ys.length ? Math.min(...ys) - ankle.y : null;
    };
    const footBones = (b) => /^CC_Base_[LR]_(Foot|ToeBase|.*Toe1)/.test(b);
    const shoeLow = Math.min(...shoes.map((m) => relLow(m, footBones)).filter((v) => v !== null));
    const bodyLow = relLow(body, footBones);
    const sole = bodyLow !== null && Number.isFinite(shoeLow) ? bodyLow - shoeLow : 0;
    if (sole > 0.005) {
      heel.lift = sole;
      model.position.y = sole;
      log(`${entry.label}: shoe soles ${(sole * 100).toFixed(1)} cm thick, lifted to stand on them.`);
    }
    return;
  }
  Object.assign(heel, solved);
  model.position.y = heel.lift;
  log(`${entry.label}: high heels, lifted ${(heel.lift * 100).toFixed(1)} cm, feet tipped ${THREE.MathUtils.radToDeg(heel.angle).toFixed(0)}°, toes ${THREE.MathUtils.radToDeg(heel.angle + heel.toe).toFixed(0)}°.`);
}

const actionStarted = new WeakMap();
let frameNumber = 0;
function blendWeight(action) {
  if (!action.enabled || !mixer._isActiveAction(action)) return 0;
  if (actionStarted.get(action) === frameNumber) return 0;
  return action.getEffectiveWeight();
}

const floorContact = { points: [], lift: 0 };
function updateFloorContacts() {
  floorContact.points = [];
  const shoes = [];
  for (const part of parts.values()) {
    if (!part.visible) continue;
    part.traverse?.((o) => { if (o.isSkinnedMesh && slots.feet.test(o.name)) shoes.push(o); });
    if (part.isSkinnedMesh && slots.feet.test(part.name) && !shoes.includes(part)) shoes.push(part);
  }
  const v = new THREE.Vector3();
  for (const mesh of shoes) {
    const pos = mesh.geometry.attributes.position;
    const ys = new Float32Array(pos.count);
    let minY = Infinity;
    for (let i = 0; i < pos.count; i++) {
      ys[i] = v.fromBufferAttribute(pos, i).applyMatrix4(mesh.bindMatrix).y;
      minY = Math.min(minY, ys[i]);
    }
    const band = [];
    for (let i = 0; i < pos.count; i++) if (ys[i] < minY + 0.03) band.push(i);
    const step = Math.max(1, Math.floor(band.length / 120));
    for (let k = 0; k < band.length; k += step) floorContact.points.push({ mesh, index: band[k] });
  }
}

function applyFloorContact(dt) {
  if (!floorContact.points.length || config.floorContact === false) {
    floorContact.lift = 0;
    return;
  }
  model.updateMatrixWorld(true);
  const v = new THREE.Vector3();
  let lowest = Infinity;
  for (const { mesh, index } of floorContact.points) {
    mesh.getVertexPosition(index, v);
    mesh.localToWorld(v);
    lowest = Math.min(lowest, v.y - rig.position.y);
  }
  const target = Math.max(0, -lowest + 0.002);
  if (target > floorContact.lift) floorContact.lift = target;
  else floorContact.lift += (target - floorContact.lift) * Math.min(1, dt * 4);
  model.position.y += floorContact.lift;
}

function heelsRecordingWeight() {
  let w = 0;
  let total = 0;
  for (const action of mixer._actions) {
    const weight = blendWeight(action);
    total += weight;
    if (heelsClips.has(action.getClip())) w += weight;
  }
  return total > 0 ? Math.min(1, w / total) : 0;
}

function applyHeelPose(axes) {
  const auto = 1 - heelsRecordingWeight();
  model.position.y = heel.lift * auto;
  if (!heel.angle || !axes || auto <= 0) return;
  if (!heel.bones.length) {
    heel.bones = ['L', 'R'].map((side) => ({ foot: findBone(`CC_Base_${side}_Foot`), toe: findBone(`CC_Base_${side}_ToeBase`) }));
    for (const { foot, toe } of heel.bones) for (const b of [foot, toe]) if (b && !baseTransforms.has(b)) baseTransforms.set(b, { q: b.quaternion.clone(), p: b.position.clone() });
  }
  for (const { foot, toe } of heel.bones) {
    rotateWorld(foot, axes.lateral, heel.angle * auto);
    rotateWorld(toe, axes.lateral, heel.toe * auto);
  }
}

async function setOutfit(key) {
  const entry = outfitCatalog.get(key) || findOutfit(key);
  if (!entry) return log(`No outfit "${key}". Outfits: ${[...outfitCatalog.values()].map((e) => e.label).join(', ')}`, true);
  outfitState.requested = entry.key;
  $('outfitSelect').value = entry.key;
  if (entry.file && !entry.meshes) {
    log(`Loading ${entry.label}...`);
    try {
      await ensureOutfitLoaded(entry);
    } catch (err) {
      return log(`Could not load ${entry.label}: ${err.message || err}`, true);
    }
    if (outfitState.requested !== entry.key) return;
  }
  const shown = new Set(entry.file ? entry.meshes : [...parts.entries()].filter(([n]) => startsAny(n, entry.parts || [])).map(([, p]) => p));
  const own = [...parts.entries()].filter(([n]) => startsAny(n, ownClothing)).map(([, p]) => p);
  const clothing = new Set(own);
  for (const e of outfitCatalog.values()) for (const m of e.meshes || []) clothing.add(m);
  const covers = (slot) => [...shown].some((p) => slot.test(p.name));
  for (const piece of clothing) {
    let on = shown.has(piece);
    if (!on && own.includes(piece)) {
      if (slots.feet.test(piece.name) && !covers(slots.feet)) on = true;
      if (slots.bottoms.test(piece.name) && !covers(slots.bottoms)) on = true;
    }
    piece.visible = on;
  }
  for (const [name, part] of parts) {
    const box = partBoxes.get(name);
    if (box) box.checked = part.visible;
  }
  outfitState.current = entry.key;
  updateHeelPose(entry);
  updateFloorContacts();
  try {
    localStorage.setItem('iris.outfit', entry.key);
  } catch {}
}

for (const entry of outfitCatalog.values()) {
  const option = document.createElement('option');
  option.value = entry.key;
  option.textContent = entry.label;
  $('outfitSelect').append(option);
}
$('outfitSelect').onchange = () => setOutfit($('outfitSelect').value);
let savedOutfit = null;
try {
  savedOutfit = localStorage.getItem('iris.outfit');
} catch {}
const startOutfit = savedOutfit && outfitCatalog.has(savedOutfit) ? savedOutfit : config.outfit;
if (startOutfit) await setOutfit(startOutfit);

const morphMeshes = [];
model.traverse((o) => { if (o.isMesh && o.morphTargetDictionary) morphMeshes.push(o); });
const morphNames = [...new Set(morphMeshes.flatMap((m) => Object.keys(m.morphTargetDictionary)))];

function solveLinear(a, b) {
  const n = a.length;
  const m = b[0].length;
  for (let col = 0; col < n; col++) {
    let pivot = col;
    for (let r = col + 1; r < n; r++) if (Math.abs(a[r][col]) > Math.abs(a[pivot][col])) pivot = r;
    [a[col], a[pivot]] = [a[pivot], a[col]];
    [b[col], b[pivot]] = [b[pivot], b[col]];
    for (let r = 0; r < n; r++) {
      if (r === col) continue;
      const f = a[r][col] / a[col][col];
      for (let c = col; c < n; c++) a[r][c] -= f * a[col][c];
      for (let c = 0; c < m; c++) b[r][c] -= f * b[col][c];
    }
  }
  return b.map((row, r) => row.map((v) => v / a[r][r]));
}

function regionWeights(mesh, boneNames) {
  const ids = boneNames.map((n) => mesh.skeleton.bones.findIndex((b) => b.name === n)).filter((i) => i >= 0);
  const index = mesh.geometry.attributes.skinIndex;
  const weight = mesh.geometry.attributes.skinWeight;
  const out = new Float32Array(index.count);
  for (let v = 0; v < index.count; v++) {
    for (let k = 0; k < 4; k++) {
      if (ids.includes(index.getComponent(v, k))) out[v] += weight.getComponent(v, k);
    }
  }
  return out;
}

function isolateMorph(mesh, i, rule, scale = 1) {
  const pos = mesh.geometry.attributes.position;
  const delta = mesh.geometry.morphAttributes.position[i].clone();
  const region = regionWeights(mesh, rule.bones);
  const center = new THREE.Vector3();
  let inside = 0;
  for (let v = 0; v < pos.count; v++) {
    if (region[v] > 0.5) {
      center.x += pos.getX(v);
      center.y += pos.getY(v);
      center.z += pos.getZ(v);
      inside += 1;
    }
  }
  if (!inside) return null;
  center.divideScalar(inside);
  const ata = Array.from({ length: 4 }, () => [0, 0, 0, 0]);
  const atb = Array.from({ length: 4 }, () => [0, 0, 0]);
  const p = new THREE.Vector3();
  for (let v = 0; v < pos.count; v++) {
    p.fromBufferAttribute(pos, v);
    if (region[v] > 0 || p.distanceTo(center) > rule.radius) continue;
    const row = [p.x, p.y, p.z, 1];
    const d = [delta.getX(v), delta.getY(v), delta.getZ(v)];
    for (let r = 0; r < 4; r++) {
      for (let c = 0; c < 4; c++) ata[r][c] += row[r] * row[c];
      for (let c = 0; c < 3; c++) atb[r][c] += row[r] * d[c];
    }
  }
  const fit = solveLinear(ata, atb);
  for (let v = 0; v < pos.count; v++) {
    p.fromBufferAttribute(pos, v);
    const row = [p.x, p.y, p.z, 1];
    const shift = [0, 1, 2].map((c) => row.reduce((sum, x, r) => sum + x * fit[r][c], 0));
    const w = region[v] * scale;
    delta.setXYZ(v, (delta.getX(v) - shift[0]) * w, (delta.getY(v) - shift[1]) * w, (delta.getZ(v) - shift[2]) * w);
  }
  return delta;
}

for (const [morph, rule] of Object.entries(config.morphCleanup || {})) {
  for (const m of morphMeshes) {
    const i = m.morphTargetDictionary[morph];
    if (i === undefined) continue;
    const material = [].concat(m.material)[0].name || '';
    const scale = Object.entries(rule.scale || {}).find(([prefix]) => material.startsWith(prefix))?.[1] ?? 1;
    const keep = m.isSkinnedMesh && startsAny(material, rule.meshes) ? isolateMorph(m, i, rule, scale) : null;
    for (const [key, attrs] of Object.entries(m.geometry.morphAttributes)) {
      if (!attrs[i]) continue;
      if (key === 'position' && keep) {
        attrs[i] = keep;
      } else if (key === 'normal' && keep) {
        const region = regionWeights(m, rule.bones);
        const n = attrs[i].clone();
        for (let v = 0; v < n.count; v++) n.setXYZ(v, n.getX(v) * region[v], n.getY(v) * region[v], n.getZ(v) * region[v]);
        attrs[i] = n;
      } else {
        attrs[i] = attrs[i].clone();
        attrs[i].array.fill(0);
      }
    }
  }
}

function setMorph(name, weight) {
  for (const m of morphMeshes) {
    const i = m.morphTargetDictionary[name];
    if (i !== undefined) m.morphTargetInfluences[i] = weight;
  }
}

const morphSet = new Set(morphNames);
const morphAliases = config.morphAliases || {};
function resolveMorph(name) {
  return morphSet.has(name) || !morphAliases[name] ? name : morphAliases[name];
}

let lastMorphs = new Map();
function resolveFrame(requested) {
  const frame = new Map();
  for (const [name, w] of requested) {
    const real = resolveMorph(name);
    frame.set(real, (frame.get(real) || 0) + w);
  }
  return frame;
}

function applyMorphs(frame) {
  for (const name of lastMorphs.keys()) if (!frame.has(name)) setMorph(name, 0);
  for (const [name, w] of frame) setMorph(name, Math.min(1, Math.max(0, w)));
  lastMorphs = frame;
}

function hipOrigin(clip) {
  const rootPos = clip.tracks.find((t) => t.name === 'CC_Base_BoneRoot.position');
  const rootRot = clip.tracks.find((t) => t.name === 'CC_Base_BoneRoot.quaternion');
  const hipPos = clip.tracks.find((t) => t.name === 'CC_Base_Hip.position');
  if (!rootPos || !rootRot || !hipPos) return null;
  const q = new THREE.Quaternion().fromArray(rootRot.values, 0);
  return new THREE.Vector3().fromArray(hipPos.values, 0).applyQuaternion(q).add(new THREE.Vector3().fromArray(rootPos.values, 0));
}

function normalizeClip(clip, origin = hipOrigin(clip)) {
  const c = clip.clone();
  const rootPos = c.tracks.find((t) => t.name === 'CC_Base_BoneRoot.position');
  if (!rootPos || !origin) return c;
  const hip = origin;
  const values = rootPos.values.slice();
  for (let i = 0; i < values.length; i += 3) {
    values[i] -= hip.x;
    values[i + 2] -= hip.z;
  }
  rootPos.values = values;
  return c;
}

function hipHeight(gltf) {
  const node = gltf.parser.json.nodes.find((n) => n.name === 'CC_Base_Hip');
  return node && node.translation ? Math.hypot(...node.translation) : null;
}

const characterHip = hipHeight(gltfs.get(config.character));
const keptPositions = new Set(['CC_Base_BoneRoot.position', 'CC_Base_Hip.position']);
function retarget(clip, gltf, sizeFrom = gltf) {
  const c = clip.clone();
  for (const t of c.tracks) {
    const dot = t.name.lastIndexOf('.');
    const bone = t.name.slice(0, dot);
    if (boneAliases[bone]) t.name = boneAliases[bone] + t.name.slice(dot);
  }
  if (config.retarget === false) return c;
  const sourceHip = hipHeight(sizeFrom);
  const ratio = characterHip && sourceHip ? characterHip / sourceHip : 1;
  c.tracks = c.tracks.filter((t) => !t.name.endsWith('.scale') && (!t.name.endsWith('.position') || keptPositions.has(t.name)));
  if (Math.abs(ratio - 1) > 1e-4) {
    for (const t of c.tracks) {
      if (!keptPositions.has(t.name)) continue;
      t.values = t.values.map((v) => v * ratio);
    }
  }
  return c;
}

const baseName = (path) => path.split('/').pop();
const rawClips = new Map();
const sourceClips = new Map();
for (const [file, gltf] of gltfs) {
  for (const clip of gltf.animations) {
    if (clip.name.startsWith('Key')) continue;
    const flatFile = [...heelsFiles].find(([, h]) => h === file)?.[0];
    const source = retarget(clip, gltf, flatFile ? gltfs.get(flatFile) : gltf);
    sourceClips.set(`${file}::${clip.name}`, source);
    rawClips.set(`${file}::${clip.name}`, normalizeClip(source));
  }
}

function holdFrame(clip, which) {
  const held = clip.clone();
  held.tracks = held.tracks.map((t) => {
    const size = t.getValueSize();
    const start = which === 'end' ? t.values.length - size : 0;
    const frame = Array.from(t.values.slice(start, start + size));
    return new t.constructor(t.name, [0, 1], frame.concat(frame));
  });
  held.resetDuration();
  return held;
}

function anchorFeet(clip) {
  const feet = (config.feet || []).map((n) => model.getObjectByName(n)).filter(Boolean);
  const rootBone = model.getObjectByName('CC_Base_BoneRoot');
  const rootPos = clip.tracks.find((t) => t.name === 'CC_Base_BoneRoot.position');
  const hipPos = clip.tracks.find((t) => t.name === 'CC_Base_Hip.position');
  if (feet.length < 2 || !rootBone || !rootPos || !hipPos) {
    log(`Cannot anchor feet for ${clip.name}`, true);
    return clip;
  }
  const armature = rootBone.parent;
  const sampler = new THREE.AnimationMixer(model);
  const action = sampler.clipAction(clip).play();
  const rootAt = rootPos.createInterpolant();
  const times = hipPos.times.slice();
  const values = new Float32Array(times.length * 3);
  const mid = new THREE.Vector3();
  const foot = new THREE.Vector3();
  let start = null;
  times.forEach((t, i) => {
    action.time = t;
    sampler.update(0);
    model.updateMatrixWorld(true);
    mid.set(0, 0, 0);
    for (const f of feet) mid.add(f.getWorldPosition(foot));
    armature.worldToLocal(mid.divideScalar(feet.length));
    if (!start) start = mid.clone();
    const base = rootAt.evaluate(t);
    values[i * 3] = base[0] - (mid.x - start.x);
    values[i * 3 + 1] = base[1];
    values[i * 3 + 2] = base[2] - (mid.z - start.z);
  });
  sampler.stopAllAction();
  sampler.uncacheRoot(model);
  const anchored = clip.clone();
  anchored.tracks = anchored.tracks.map((t) => (t.name === rootPos.name ? new THREE.VectorKeyframeTrack(t.name, times, values) : t));
  return anchored;
}

const LOOP_BONES = ['CC_Base_Hip', 'CC_Base_L_Thigh', 'CC_Base_L_Calf', 'CC_Base_L_Foot', 'CC_Base_R_Thigh', 'CC_Base_R_Calf', 'CC_Base_R_Foot'];

function quatAngle(values, i, j) {
  const dot = Math.abs(values[i * 4] * values[j * 4] + values[i * 4 + 1] * values[j * 4 + 1] + values[i * 4 + 2] * values[j * 4 + 2] + values[i * 4 + 3] * values[j * 4 + 3]);
  return 2 * Math.acos(Math.min(1, dot));
}

function findLoopPoint(clip) {
  const legs = clip.tracks.filter((t) => t instanceof THREE.QuaternionKeyframeTrack && LOOP_BONES.some((b) => t.name === `${b}.quaternion`));
  const reference = legs.reduce((best, t) => (t.times.length > best.times.length ? t : best), legs[0]);
  if (!reference || reference.times.length < 8) return null;
  const n = reference.times.length;
  const dense = legs.filter((t) => t.times.length === n);
  const stepAt = (k) => Math.max(...dense.map((t) => quatAngle(t.values, k, k - 1)));
  const allSteps = Array.from({ length: n - 1 }, (_, i) => stepAt(i + 1)).sort((a, b) => a - b);
  const typical = allSteps[Math.floor(allSteps.length / 2)];
  const search = (filtered) => {
    let best = null;
    for (let k = Math.floor(n * 0.9); k < n; k++) {
      if (filtered && stepAt(k) < typical * 0.75) continue;
      let pose = 0;
      let speed = 0;
      for (const t of dense) {
        pose = Math.max(pose, quatAngle(t.values, k, 0));
        speed += Math.abs(quatAngle(t.values, k, k - 1) - quatAngle(t.values, 1, 0));
      }
      const score = pose + (2 * speed) / dense.length;
      if (!best || score < best.score) best = { score, time: reference.times[k], pose };
    }
    return best;
  };
  return search(true) || search(false);
}

function fixLoop(clip, name) {
  const point = findLoopPoint(clip);
  if (!point) return clip;
  const c = clip.clone();
  const start = Math.min(...clip.tracks.map((t) => t.times[0]));
  const end = point.time;
  const qa = new THREE.Quaternion();
  const qEnd = new THREE.Quaternion();
  const qCorr = new THREE.Quaternion();
  const ident = new THREE.Quaternion();
  c.tracks = c.tracks.map((track) => {
    if (track.times.length < 3) return new track.constructor(track.name, Array.from(track.times, (t) => Math.max(0, t - start)), track.values);
    const size = track.getValueSize();
    const t0 = track.times[0];
    const atEnd = Array.from(track.createInterpolant().evaluate(end));
    const first = Array.from(track.values.slice(0, size));
    const times = [];
    const values = [];
    const isQuat = track instanceof THREE.QuaternionKeyframeTrack;
    if (isQuat) {
      qEnd.fromArray(atEnd);
      qa.fromArray(first);
      if (qa.dot(qEnd) < 0) qEnd.set(-qEnd.x, -qEnd.y, -qEnd.z, -qEnd.w);
      qCorr.copy(qa).multiply(qEnd.clone().invert());
    }
    for (let i = 0; i < track.times.length && track.times[i] < end - 1e-6; i++) {
      const f = (track.times[i] - t0) / (end - t0);
      times.push(track.times[i] - start);
      if (isQuat) {
        const q = new THREE.Quaternion().fromArray(track.values, i * 4);
        new THREE.Quaternion().slerpQuaternions(ident, qCorr, f).multiply(q).toArray(values, values.length);
      } else {
        for (let k = 0; k < size; k++) values.push(track.values[i * size + k] + f * (first[k] - atEnd[k]));
      }
    }
    times.push(end - start);
    values.push(...first);
    return new track.constructor(track.name, times, values);
  });
  c.duration = end - start;
  log(`${name}: loop cut at ${end.toFixed(2)} s of ${clip.duration.toFixed(2)} s; ${THREE.MathUtils.radToDeg(point.pose).toFixed(1)} deg of mismatch spread across the stride.`);
  return c;
}

function computeTravel(clip, looped) {
  const feet = (config.feet || []).map((n) => findBone(n)).filter(Boolean);
  const hipPos = clip.tracks.find((t) => t.name === 'CC_Base_Hip.position');
  if (feet.length < 2 || !hipPos) return null;
  const sampler = new THREE.AnimationMixer(model);
  const action = sampler.clipAction(clip).play();
  const times = Array.from(hipPos.times);
  const samples = times.map((t) => {
    action.time = t;
    sampler.update(0);
    model.updateMatrixWorld(true);
    return feet.map((f) => f.getWorldPosition(new THREE.Vector3()));
  });
  sampler.stopAllAction();
  sampler.uncacheRoot(model);
  const values = new Float32Array(times.length * 3);
  const steps = [];
  let planted = samples[0][0].y <= samples[0][1].y ? 0 : 1;
  let x = 0;
  let z = 0;
  for (let i = 1; i < times.length; i++) {
    const other = 1 - planted;
    if (samples[i - 1][other].y < samples[i - 1][planted].y - 0.01) {
      planted = other;
      steps.push(times[i - 1]);
    }
    x -= samples[i][planted].x - samples[i - 1][planted].x;
    z -= samples[i][planted].z - samples[i - 1][planted].z;
    values[i * 3] = x;
    values[i * 3 + 2] = z;
  }
  if (looped && Math.hypot(x, z) > 0.01) {
    const heading = Math.atan2(x, z);
    const c = Math.cos(-heading);
    const sn = Math.sin(-heading);
    for (let i = 0; i < times.length; i++) {
      const vx = values[i * 3];
      const vz = values[i * 3 + 2];
      values[i * 3] = vx * c + vz * sn;
      values[i * 3 + 2] = -vx * sn + vz * c;
    }
  }
  const firstPlanted = samples[0][0].y <= samples[0][1].y ? 0 : 1;
  if (looped && planted !== firstPlanted) steps.push(times[times.length - 1]);
  const track = new THREE.VectorKeyframeTrack('travel', times, values);
  return { interp: track.createInterpolant(), start: times[0], end: times[times.length - 1], steps, distance: Math.hypot(x, z) };
}

function alignedSource(clipName) {
  const ref = config.clips[clipName];
  return ref ? sourceClips.get(`${ref.file}::${ref.clip}`) : null;
}

function sampleBones(clip, names, times, read) {
  const bonesToRead = names.map((n) => findBone(n)).filter(Boolean);
  const sampler = new THREE.AnimationMixer(model);
  const action = sampler.clipAction(clip).play();
  const out = times.map((t) => {
    action.time = t;
    sampler.update(0);
    model.updateMatrixWorld(true);
    return read(bonesToRead);
  });
  sampler.stopAllAction();
  sampler.uncacheRoot(model);
  return out;
}

function groundFeet(clip, fixedLift = null) {
  const rootPos = clip.tracks.find((t) => t.name === 'CC_Base_BoneRoot.position');
  if (!rootPos) return clip;
  const toes = config.toes || ['CC_Base_L_ToeBase', 'CC_Base_R_ToeBase'];
  const times = Array.from({ length: 41 }, (_, i) => (clip.duration * i) / 40);
  const lift = fixedLift ?? (config.toeHeight ?? 0.027) - Math.min(...sampleBones(clip, toes, times, (bs) => Math.min(...bs.map((b) => b.getWorldPosition(new THREE.Vector3()).y))));
  const scale = findBone('CC_Base_BoneRoot').parent.getWorldScale(new THREE.Vector3()).y;
  const c = clip.clone();
  const track = c.tracks.find((t) => t.name === 'CC_Base_BoneRoot.position');
  const values = track.values.slice();
  for (let i = 1; i < values.length; i += 3) values[i] += lift / scale;
  track.values = values;
  if (fixedLift === null) log(`${clip.name}: lifted ${(lift * 100).toFixed(1)} cm so her feet meet the floor.`);
  c.userData = { ...(c.userData || {}), groundLift: lift };
  return c;
}

function smoothSeries(values, radius) {
  return values.map((_, i) => {
    let sum = 0;
    let n = 0;
    for (let k = Math.max(0, i - radius); k <= Math.min(values.length - 1, i + radius); k++) {
      sum += values[k];
      n++;
    }
    return sum / n;
  });
}

function extractRootMotion(clip) {
  const find = (name) => clip.tracks.find((t) => t.name === name);
  const rootPos = find('CC_Base_BoneRoot.position');
  const rootRot = find('CC_Base_BoneRoot.quaternion');
  const hipPos = find('CC_Base_Hip.position');
  const hipRot = find('CC_Base_Hip.quaternion');
  if (!rootPos || !rootRot || !hipPos || !hipRot) return { clip, motion: null };
  const times = Array.from(hipPos.times);
  const at = (track) => track.createInterpolant();
  const iRootPos = at(rootPos);
  const iRootRot = at(rootRot);
  const iHipPos = at(hipPos);
  const iHipRot = at(hipRot);
  const rest = restPose.get('CC_Base_BoneRoot').clone().multiply(restPose.get('CC_Base_Hip')).invert();
  const xs = [];
  const zs = [];
  const yaws = [];
  const roots = [];
  for (const t of times) {
    const tr = new THREE.Vector3().fromArray(iRootPos.evaluate(t));
    const qr = new THREE.Quaternion().fromArray(iRootRot.evaluate(t));
    const th = new THREE.Vector3().fromArray(iHipPos.evaluate(t));
    const qh = new THREE.Quaternion().fromArray(iHipRot.evaluate(t));
    const hip = th.clone().applyQuaternion(qr).add(tr);
    const fwd = new THREE.Vector3(0, 0, 1).applyQuaternion(qr.clone().multiply(qh).multiply(rest));
    xs.push(hip.x);
    zs.push(hip.z);
    let yaw = Math.atan2(fwd.x, fwd.z);
    if (yaws.length) {
      const prev = yaws[yaws.length - 1];
      while (yaw - prev > Math.PI) yaw -= 2 * Math.PI;
      while (yaw - prev < -Math.PI) yaw += 2 * Math.PI;
    }
    yaws.push(yaw);
    roots.push({ tr, qr });
  }
  const radius = Math.max(1, Math.round(0.15 / ((times[times.length - 1] - times[0]) / (times.length - 1))));
  const sx = smoothSeries(xs, radius);
  const sz = smoothSeries(zs, radius);
  const sy = smoothSeries(yaws, radius);
  const n = times.length;
  const fix = (arr, raw) => {
    const d0 = raw[0] - arr[0];
    const d1 = raw[n - 1] - arr[n - 1];
    return arr.map((v, i) => v + d0 + ((d1 - d0) * i) / (n - 1));
  };
  const fx = fix(sx, xs);
  const fz = fix(sz, zs);
  const fy = fix(sy, yaws);
  const scale = findBone('CC_Base_BoneRoot').parent.getWorldScale(new THREE.Vector3()).x;
  const newPos = new Float32Array(n * 3);
  const newRot = new Float32Array(n * 4);
  const motionValues = new Float32Array(n * 3);
  const up = new THREE.Vector3(0, 1, 0);
  roots.forEach(({ tr, qr }, k) => {
    const dx = fx[k] - fx[0];
    const dz = fz[k] - fz[0];
    const dyaw = fy[k] - fy[0];
    const undo = new THREE.Quaternion().setFromAxisAngle(up, -dyaw);
    new THREE.Vector3(tr.x - dx, tr.y, tr.z - dz).applyQuaternion(undo).toArray(newPos, k * 3);
    undo.clone().multiply(qr).toArray(newRot, k * 4);
    motionValues[k * 3] = dx * scale;
    motionValues[k * 3 + 1] = dyaw;
    motionValues[k * 3 + 2] = dz * scale;
  });
  const c = clip.clone();
  c.tracks = c.tracks.map((t) => {
    if (t.name === rootPos.name) return new THREE.VectorKeyframeTrack(t.name, times, newPos);
    if (t.name === rootRot.name) return new THREE.QuaternionKeyframeTrack(t.name, times, newRot);
    return t;
  });
  const track = new THREE.VectorKeyframeTrack('rootMotion', times, motionValues);
  return { clip: c, motion: { interp: track.createInterpolant(), start: times[0], end: times[n - 1], yaw: fy[n - 1] - fy[0] } };
}

function longestClipKey(file) {
  let best = null;
  for (const [k, clip] of rawClips) {
    if (!k.startsWith(`${file}::`)) continue;
    if (!best || clip.duration > rawClips.get(best).duration) best = k;
  }
  return best;
}

const mirrorName = (name) => name.replace(/_L_/, '_TMP_').replace(/_R_/, '_L_').replace(/_TMP_/, '_R_');
const mirrorQuat = (q) => q.set(q.x, -q.y, -q.z, q.w);
let mirrorRig = null;

function setupMirror() {
  const armature = findBone('CC_Base_BoneRoot').parent;
  const list = [];
  armature.traverse((o) => { if (o.isBone) list.push(o); });
  const byName = new Map(list.map((b) => [b.name, b]));
  const saved = list.map((b) => [b, b.quaternion.clone(), b.position.clone()]);
  for (const b of list) {
    b.quaternion.copy(restPose.get(b.name));
    b.position.copy(restPosition.get(b.name));
  }
  armature.updateMatrixWorld(true);
  const armInv = armature.matrixWorld.clone().invert();
  const rest = new Map();
  const m = new THREE.Matrix4();
  for (const b of list) rest.set(b.name, new THREE.Quaternion().setFromRotationMatrix(m.multiplyMatrices(armInv, b.matrixWorld)));
  for (const [b, q, pos] of saved) {
    b.quaternion.copy(q);
    b.position.copy(pos);
  }
  mirrorRig = { armature, list, byName, rest };
}

function mirrorClip(clip) {
  mirrorRig ||= setupMirror() || mirrorRig;
  const { armature, list, byName, rest } = mirrorRig;
  const reference = clip.tracks.filter((t) => t.name.endsWith('.quaternion')).reduce((a, t) => (t.times.length > a.times.length ? t : a));
  const times = Array.from(reference.times);
  const tracked = new Set(clip.tracks.map((t) => t.name.slice(0, t.name.lastIndexOf('.'))));
  const sampler = new THREE.AnimationMixer(model);
  const action = sampler.clipAction(clip).play();
  const out = new Map();
  const add = (name, size) => {
    if (!out.has(name)) out.set(name, { size, values: [] });
    return out.get(name).values;
  };
  const m = new THREE.Matrix4();
  const world = new Map();
  const mirrored = new Map();
  const pos = new Map();
  for (const t of times) {
    action.time = t;
    sampler.update(0);
    armature.updateMatrixWorld(true);
    const armInv = armature.matrixWorld.clone().invert();
    for (const b of list) {
      m.multiplyMatrices(armInv, b.matrixWorld);
      world.set(b.name, new THREE.Quaternion().setFromRotationMatrix(m));
      pos.set(b.name, new THREE.Vector3().setFromMatrixPosition(m));
    }
    for (const b of list) {
      const partner = byName.has(mirrorName(b.name)) ? mirrorName(b.name) : b.name;
      const delta = world.get(b.name).clone().multiply(rest.get(b.name).clone().invert());
      mirrored.set(partner, mirrorQuat(delta).multiply(rest.get(partner)));
    }
    for (const b of list) {
      if (!tracked.has(b.name)) continue;
      const parentWorld = b.parent.isBone ? mirrored.get(b.parent.name) : new THREE.Quaternion();
      const local = parentWorld.clone().invert().multiply(mirrored.get(b.name));
      add(`${b.name}.quaternion`, 4).push(local.x, local.y, local.z, local.w);
    }
    for (const name of ['CC_Base_BoneRoot', 'CC_Base_Hip']) {
      if (!clip.tracks.some((tr) => tr.name === `${name}.position`)) continue;
      const b = byName.get(name);
      const p = pos.get(name).clone();
      p.x = -p.x;
      let local = p;
      if (b.parent.isBone) {
        const pp = pos.get(b.parent.name).clone();
        pp.x = -pp.x;
        local = p.sub(pp).applyQuaternion(mirrored.get(b.parent.name).clone().invert());
      }
      add(`${name}.position`, 3).push(local.x, local.y, local.z);
    }
  }
  sampler.stopAllAction();
  sampler.uncacheRoot(model);
  const tracks = [...out].map(([name, { size, values }]) => (size === 4 ? new THREE.QuaternionKeyframeTrack(name, times, values) : new THREE.VectorKeyframeTrack(name, times, values)));
  return new THREE.AnimationClip(`${clip.name} (mirrored)`, clip.duration, tracks);
}

const presets = new Map();
const clipEntries = [];
for (const [name, entry] of Object.entries(config.clips)) {
  clipEntries.push([name, entry]);
  const heelsFile = heelsFiles.get(entry.file);
  if (heelsFile) {
    const named = entry.clip !== 'longest' && rawClips.has(`${heelsFile}::${entry.clip}`);
    clipEntries.push([`${name}${heelSuffix}`, { ...entry, file: heelsFile, clip: named ? entry.clip : 'longest', heelsOf: name, groundFeet: false, groundFeetFrom: entry.groundFeet ? name : null }]);
  }
}
for (const [name, entry] of clipEntries) {
  const key = entry.clip === 'longest' ? longestClipKey(entry.file) : `${entry.file}::${entry.clip}`;
  if (entry.clip === 'longest' && key && rawClips.get(key).duration < 1) log(`"${name}": the longest clip in ${entry.file} is only ${rawClips.get(key).duration.toFixed(2)} s. The export may be missing its animation.`, true);
  let raw = rawClips.get(key);
  const refSource = entry.alignTo ? alignedSource(entry.alignTo) : null;
  if (raw && refSource) raw = normalizeClip(sourceClips.get(key), hipOrigin(refSource));
  if (!raw) {
    log(`Missing clip for "${name}": ${entry.clip} in ${entry.file}`, true);
    continue;
  }
  if (entry.mirror) raw = mirrorClip(raw);
  let clip = entry.hold ? holdFrame(raw, entry.hold) : raw;
  if (entry.anchorFeet) clip = anchorFeet(clip);
  if (entry.loopFix) clip = fixLoop(clip, name);
  if (entry.groundFeet) clip = groundFeet(clip);
  if (entry.groundFeetFrom) clip = groundFeet(clip, presets.get(entry.groundFeetFrom)?.clip.userData?.groundLift ?? 0);
  let rootMotion = null;
  if (entry.rootMotion) ({ clip, motion: rootMotion } = extractRootMotion(clip));
  const travel = entry.travel ? computeTravel(clip, entry.loop) : null;
  presets.set(name, { ...entry, clip, travel, rootMotion });
}
const heelsClips = new Set([...presets.values()].filter((p) => p.heelsOf).map((p) => p.clip));
if (heelsClips.size) log(`High-heel versions: ${[...presets.values()].filter((p) => p.heelsOf).map((p) => p.heelsOf).join(', ')}.`);
const footBones = (config.feet || []).map((n) => findBone(n)).filter(Boolean);
function feetMidNow() {
  if (footBones.length < 2) return null;
  model.updateMatrixWorld(true);
  const mid = new THREE.Vector3();
  for (const b of footBones) mid.add(model.worldToLocal(b.getWorldPosition(new THREE.Vector3())));
  return mid.divideScalar(footBones.length);
}
const startFeet = new Map();
{
  const saved = [];
  model.traverse((o) => { if (o.isBone) saved.push([o, o.quaternion.clone(), o.position.clone()]); });
  for (const p of presets.values()) {
    const sampler = new THREE.AnimationMixer(model);
    const action = sampler.clipAction(p.clip).play();
    action.time = 0;
    sampler.update(0);
    startFeet.set(p.clip, feetMidNow());
    sampler.stopAllAction();
    sampler.uncacheRoot(model);
  }
  for (const [b, q, pos] of saved) {
    b.quaternion.copy(q);
    b.position.copy(pos);
  }
}
let footLock = null;

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
      options.push({ to: j, cost: c, weight: Math.exp(-c / (idleGraph.limit * 2.5)) * (0.05 + noveltyOf(j)) * (0.25 + hub)  });
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
  footLock = now ? { action: next, dx: now.x - target.feet.x, dz: now.z - target.feet.z, applied: 0, fade, elapsed: 0, started: false } : null;
  motion.current = next;
  motion.name = target.clip;
  motion.next = null;
  idleGraph.jumps += 1;
  idleGraph.lastJump = { from: `${idleGraph.frames[choice.from].clip}@${idleGraph.frames[choice.from].t.toFixed(1)}`, to: `${target.clip}@${target.t.toFixed(1)}`, cost: +choice.cost.toFixed(3), fade: +fade.toFixed(2), forced: Boolean(force) };
  return true;
}

const headSmooth = { seconds: config.headSmoothing ?? 0.45, bones: null, state: new Map() };
function smoothHead(dt) {
  if (!headSmooth.seconds) return;
  headSmooth.bones ||= ['CC_Base_NeckTwist01', 'CC_Base_NeckTwist02', 'CC_Base_Head'].map((n) => findBone(n)).filter(Boolean);
  const k = 1 - Math.exp(-dt / headSmooth.seconds);
  for (const bone of headSmooth.bones) {
    const prev = headSmooth.state.get(bone);
    if (!prev) {
      headSmooth.state.set(bone, bone.quaternion.clone());
      continue;
    }
    prev.slerp(bone.quaternion, k);
    bone.quaternion.copy(prev);
  }
}

const autoIdleState = { settled: 0 };
function autoIdle(dt) {
  const ready = config.autoIdle !== false && activities.idle && motion.name === 'home' && !flow.target && !flow.after.length && flow.faceYaw === null && !flow.glide && !walk.target && !walk.arrive && !motion.queue.length;
  autoIdleState.settled = ready ? autoIdleState.settled + dt : 0;
  if (autoIdleState.settled > (config.autoIdleDelay ?? 1.0)) {
    autoIdleState.settled = 0;
    request('idle');
  }
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
  const result = idleGraph.dwell <= 0 || nearEnd ? idleGraphJump(nearEnd) : false;
  if (result === true) {
    idleGraph.active = motion.current;
    idleGraph.dwell = between(graphConfig.dwell);
  } else if (result !== 'stay' && idleGraph.dwell <= 0) {
    idleGraph.dwell = 0.15;
  }
}
function footLockable(name) {
  const p = presets.get(name);
  return config.footLock !== false && p && !p.travel && !p.rootMotion && !p.alignTo;
}

const travelByClip = new Map([...presets.values()].filter((p) => p.travel).map((p) => [p.clip, p.travel]));
const rootMotionByClip = new Map([...presets.values()].filter((p) => p.rootMotion).map((p) => [p.clip, p.rootMotion]));
const turnState = new Map();

const nav = buildNavGrid();

function buildNavGrid() {
  if (!room.scene || config.navigation?.enabled === false) return null;
  const opts = { cell: 0.1, radius: 0.28, minHeight: 0.05, maxHeight: 1.7, margin: 2, passable: [], ...(config.navigation || {}) };
  const bounds = new THREE.Box3().setFromObject(room.scene);
  const minX = bounds.min.x - opts.margin;
  const minZ = bounds.min.z - opts.margin;
  const cols = Math.ceil((bounds.max.x + opts.margin - minX) / opts.cell);
  const rows = Math.ceil((bounds.max.z + opts.margin - minZ) / opts.cell);
  const hard = new Uint8Array(cols * rows);
  const mark = (x, z) => {
    const c = Math.floor((x - minX) / opts.cell);
    const r = Math.floor((z - minZ) / opts.cell);
    if (c >= 0 && r >= 0 && c < cols && r < rows) hard[r * cols + c] = 1;
  };
  const passable = (o) => {
    for (let n = o; n; n = n.parent) if (opts.passable.includes(n.name)) return true;
    return false;
  };
  const a = new THREE.Vector3();
  const b = new THREE.Vector3();
  const c = new THREE.Vector3();
  room.scene.updateMatrixWorld(true);
  room.scene.traverse((o) => {
    if (!o.isMesh || passable(o)) return;
    const pos = o.geometry.attributes.position;
    const index = o.geometry.index;
    const count = index ? index.count : pos.count;
    const at = (i, out) => out.fromBufferAttribute(pos, index ? index.getX(i) : i).applyMatrix4(o.matrixWorld);
    for (let i = 0; i < count; i += 3) {
      at(i, a);
      at(i + 1, b);
      at(i + 2, c);
      const low = Math.min(a.y, b.y, c.y);
      const high = Math.max(a.y, b.y, c.y);
      if (high < opts.minHeight || low > opts.maxHeight) continue;
      for (const [p, q] of [[a, b], [b, c], [c, a]]) {
        const steps = Math.max(1, Math.ceil(Math.hypot(q.x - p.x, q.z - p.z) / (opts.cell / 2)));
        for (let k = 0; k <= steps; k++) mark(p.x + ((q.x - p.x) * k) / steps, p.z + ((q.z - p.z) * k) / steps);
      }
      const c0 = Math.floor((Math.min(a.x, b.x, c.x) - minX) / opts.cell);
      const c1 = Math.floor((Math.max(a.x, b.x, c.x) - minX) / opts.cell);
      const r0 = Math.floor((Math.min(a.z, b.z, c.z) - minZ) / opts.cell);
      const r1 = Math.floor((Math.max(a.z, b.z, c.z) - minZ) / opts.cell);
      const area = (b.x - a.x) * (c.z - a.z) - (c.x - a.x) * (b.z - a.z);
      if (Math.abs(area) < 1e-8) continue;
      for (let r = r0; r <= r1; r++) {
        for (let cc = c0; cc <= c1; cc++) {
          const px = minX + (cc + 0.5) * opts.cell;
          const pz = minZ + (r + 0.5) * opts.cell;
          const w0 = ((b.x - px) * (c.z - pz) - (c.x - px) * (b.z - pz)) / area;
          const w1 = ((c.x - px) * (a.z - pz) - (a.x - px) * (c.z - pz)) / area;
          if (w0 >= 0 && w1 >= 0 && w0 + w1 <= 1) mark(px, pz);
        }
      }
    }
  });
  const reach = Math.ceil(opts.radius / opts.cell);
  const near = new Float32Array(cols * rows).fill(Infinity);
  for (let r = 0; r < rows; r++) {
    for (let cc = 0; cc < cols; cc++) {
      if (!hard[r * cols + cc]) continue;
      for (let dr = -reach; dr <= reach; dr++) {
        for (let dc = -reach; dc <= reach; dc++) {
          const rr = r + dr;
          const c2 = cc + dc;
          if (rr < 0 || c2 < 0 || rr >= rows || c2 >= cols) continue;
          const d = Math.hypot(dr, dc) * opts.cell;
          if (d < near[rr * cols + c2]) near[rr * cols + c2] = d;
        }
      }
    }
  }
  const soft = new Uint8Array(cols * rows);
  for (let i = 0; i < soft.length; i++) soft[i] = !hard[i] && near[i] < opts.radius ? 1 : 0;
  log(`Floor map: ${cols} x ${rows} cells, ${hard.reduce((n, v) => n + v, 0)} solid.`);
  return { opts, minX, minZ, cols, rows, hard, soft };
}

function navCell(x, z) {
  return [Math.floor((x - nav.minX) / nav.opts.cell), Math.floor((z - nav.minZ) / nav.opts.cell)];
}

function navCenter(cc, r) {
  return new THREE.Vector3(nav.minX + (cc + 0.5) * nav.opts.cell, 0, nav.minZ + (r + 0.5) * nav.opts.cell);
}

function navInside(cc, r) {
  return cc >= 0 && r >= 0 && cc < nav.cols && r < nav.rows;
}

function nearestOpen(cc, r) {
  for (let ring = 0; ring < 30; ring++) {
    let best = null;
    for (let dr = -ring; dr <= ring; dr++) {
      for (let dc = -ring; dc <= ring; dc++) {
        if (Math.max(Math.abs(dr), Math.abs(dc)) !== ring) continue;
        const c2 = cc + dc;
        const r2 = r + dr;
        if (!navInside(c2, r2) || nav.hard[r2 * nav.cols + c2]) continue;
        const d = Math.hypot(dr, dc);
        if (!best || d < best.d) best = { c: c2, r: r2, d };
      }
    }
    if (best) return [best.c, best.r];
  }
  return null;
}

function lineClear(p, q, allowSoft) {
  const steps = Math.max(1, Math.ceil(p.distanceTo(q) / (nav.opts.cell / 2)));
  for (let k = 0; k <= steps; k++) {
    const x = p.x + ((q.x - p.x) * k) / steps;
    const z = p.z + ((q.z - p.z) * k) / steps;
    const [cc, r] = navCell(x, z);
    if (!navInside(cc, r)) return false;
    const i = r * nav.cols + cc;
    if (nav.hard[i]) return false;
    if (nav.soft[i] && !allowSoft(x, z)) return false;
  }
  return true;
}

function planRoute(from, to) {
  if (!nav) return null;
  let [sc, sr] = navCell(from.x, from.z);
  let [gc, gr] = navCell(to.x, to.z);
  if (!navInside(sc, sr) || !navInside(gc, gr)) return null;
  if (nav.hard[sr * nav.cols + sc]) [sc, sr] = nearestOpen(sc, sr) || [sc, sr];
  let goal = to.clone();
  if (nav.hard[gr * nav.cols + gc]) {
    const open = nearestOpen(gc, gr);
    if (!open) return null;
    [gc, gr] = open;
    goal = navCenter(gc, gr);
    log('That spot is inside something solid; walking to the nearest open floor.');
  }
  const { cols, rows, hard, soft } = nav;
  const total = cols * rows;
  const g = new Float32Array(total).fill(Infinity);
  const from_ = new Int32Array(total).fill(-1);
  const closed = new Uint8Array(total);
  const heap = [];
  const push = (i, f) => {
    heap.push([f, i]);
    let k = heap.length - 1;
    while (k > 0) {
      const parent = (k - 1) >> 1;
      if (heap[parent][0] <= heap[k][0]) break;
      [heap[parent], heap[k]] = [heap[k], heap[parent]];
      k = parent;
    }
  };
  const pop = () => {
    const top = heap[0];
    const end = heap.pop();
    if (heap.length) {
      heap[0] = end;
      let k = 0;
      for (;;) {
        const l = 2 * k + 1;
        const r = l + 1;
        let m = k;
        if (l < heap.length && heap[l][0] < heap[m][0]) m = l;
        if (r < heap.length && heap[r][0] < heap[m][0]) m = r;
        if (m === k) break;
        [heap[m], heap[k]] = [heap[k], heap[m]];
        k = m;
      }
    }
    return top;
  };
  const start = sr * cols + sc;
  const target = gr * cols + gc;
  const h = (i) => Math.hypot((i % cols) - gc, Math.floor(i / cols) - gr);
  g[start] = 0;
  push(start, h(start));
  const dirs = [[1, 0, 1], [-1, 0, 1], [0, 1, 1], [0, -1, 1], [1, 1, Math.SQRT2], [1, -1, Math.SQRT2], [-1, 1, Math.SQRT2], [-1, -1, Math.SQRT2]];
  while (heap.length) {
    const [, i] = pop();
    if (closed[i]) continue;
    closed[i] = 1;
    if (i === target) break;
    const cc = i % cols;
    const r = Math.floor(i / cols);
    for (const [dc, dr, step] of dirs) {
      const c2 = cc + dc;
      const r2 = r + dr;
      if (!navInside(c2, r2)) continue;
      const j = r2 * cols + c2;
      if (hard[j] || closed[j]) continue;
      if (dc && dr && (hard[r * cols + c2] || hard[r2 * cols + cc])) continue;
      const cost = g[i] + step * (soft[j] ? 6 : 1);
      if (cost < g[j]) {
        g[j] = cost;
        from_[j] = i;
        push(j, cost + h(j));
      }
    }
  }
  if (!closed[target]) return null;
  const cells = [];
  for (let i = target; i !== -1; i = from_[i]) cells.unshift(navCenter(i % cols, Math.floor(i / cols)));
  cells[0] = from.clone().setY(0);
  cells[cells.length - 1] = goal;
  const allowSoft = (x, z) => Math.hypot(x - from.x, z - from.z) < 0.6 || Math.hypot(x - goal.x, z - goal.z) < 0.6;
  const route = [cells[0]];
  let k = 0;
  while (k < cells.length - 1) {
    let j = cells.length - 1;
    while (j > k + 1 && !lineClear(cells[k], cells[j], allowSoft)) j--;
    route.push(cells[j]);
    k = j;
  }
  return route.slice(1);
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

const navView = { group: new THREE.Group(), route: null };
scene.add(navView.group);
navView.group.visible = false;
if (nav) {
  const data = new Uint8Array(nav.cols * nav.rows * 4);
  for (let i = 0; i < nav.cols * nav.rows; i++) {
    const color = nav.hard[i] ? [235, 80, 80, 170] : nav.soft[i] ? [240, 180, 70, 110] : [0, 0, 0, 0];
    data.set(color, i * 4);
  }
  const texture = new THREE.DataTexture(data, nav.cols, nav.rows);
  texture.magFilter = THREE.NearestFilter;
  texture.needsUpdate = true;
  const plane = new THREE.Mesh(
    new THREE.PlaneGeometry(nav.cols * nav.opts.cell, nav.rows * nav.opts.cell),
    new THREE.MeshBasicMaterial({ map: texture, transparent: true, depthWrite: false, side: THREE.DoubleSide }),
  );
  plane.rotation.x = Math.PI / 2;
  plane.position.set(nav.minX + (nav.cols * nav.opts.cell) / 2, 0.01, nav.minZ + (nav.rows * nav.opts.cell) / 2);
  navView.group.add(plane);
}

function showRoute(points) {
  if (navView.route) {
    navView.group.remove(navView.route);
    navView.route.geometry.dispose();
    navView.route = null;
  }
  if (!points || points.length < 2) return;
  const geometry = new THREE.BufferGeometry().setFromPoints(points.map((p) => new THREE.Vector3(p.x, 0.03, p.z)));
  navView.route = new THREE.Line(geometry, new THREE.LineBasicMaterial({ color: 0x7cb7ff }));
  navView.group.add(navView.route);
}

const furniture = new Map();
for (const [name, f] of Object.entries(config.furniture || {})) {
  const anchorObject = room.scene?.getObjectByName(f.anchor);
  const source = alignedSource(f.alignClip);
  const origin = source ? hipOrigin(source) : null;
  if (!anchorObject || !origin) {
    log(`Furniture "${name}": missing ${anchorObject ? 'clip ' + f.alignClip : 'object ' + f.anchor}`, true);
    continue;
  }
  const anchor = anchorObject.getWorldPosition(new THREE.Vector3());
  const armatureScale = findBone('CC_Base_BoneRoot').parent.getWorldScale(new THREE.Vector3());
  const entry = new THREE.Vector3(anchor.x + origin.x * armatureScale.x, 0, anchor.z + origin.z * armatureScale.z);
  furniture.set(name, { entry, facing: THREE.MathUtils.degToRad(f.facing || 0) });
}
function placeSpot(label, spot) {
  const placed = { x: 0, z: 0, facing: 0, ...spot };
  const anchor = placed.anchor && room.scene?.getObjectByName(placed.anchor);
  if (placed.anchor && !anchor) log(`${label} spot: missing object ${placed.anchor}`, true);
  if (anchor) {
    const at = anchor.getWorldPosition(new THREE.Vector3());
    placed.x += at.x;
    placed.z += at.z;
  }
  return placed;
}
const home = placeSpot('Home', config.homeSpot);
const work = placeSpot('Work', config.workSpot || config.homeSpot);
if (!furniture.has('home')) furniture.set('home', { entry: new THREE.Vector3(home.x, 0, home.z), facing: THREE.MathUtils.degToRad(home.facing) });
rig.position.set(home.x, 0, home.z);
rig.rotation.y = THREE.MathUtils.degToRad(home.facing);

const voice = { on: false, cameraBefore: null };
function pinned(what) {
  if (!voice.on) return false;
  log(`Voice is on, so she stays at her spot (${what} ignored). "voice off" frees her.`, true);
  return true;
}

const mixer = new THREE.AnimationMixer(model);
const motion = { current: null, name: '', next: null, queue: [], speed: 1, fade: 0.4, loopRaw: true };
const travelLast = new Map();
const walk = { halt: false, paces: null, target: null, waypoints: [], arrive: null, steps: 0, turnRate: Math.PI / 2, facingSign: 1, stride: 1 };
const activities = config.activities || {};
const flow = { target: null, faceYaw: null, after: [], glide: null };
const stepAside = config.walking?.stepAside ?? 0.4;
const strideRange = config.walking?.strideRange ?? [0.6, 1.3];

function startClip(name, clip, { loop, next = null }) {
  const action = mixer.clipAction(clip);
  const previous = motion.current;
  action.reset();
  action.setLoop(loop ? THREE.LoopRepeat : THREE.LoopOnce, Infinity);
  action.clampWhenFinished = true;
  action.timeScale = motion.speed;
  action.setEffectiveWeight(1);
  action.play();
  actionStarted.set(action, frameNumber);
  travelLast.set(action, action.time);
  if (previous && previous !== action) {
    if (motion.fade > 0) previous.crossFadeTo(action, motion.fade, false);
    else previous.stop();
  }
  footLock = null;
  if (previous && previous !== action && footLockable(name) && startFeet.get(clip)) {
    const now = feetMidNow();
    const start = startFeet.get(clip);
    if (now) footLock = { action, dx: now.x - start.x, dz: now.z - start.z, applied: 0, fade: motion.fade, elapsed: 0, started: false };
  }
  motion.current = action;
  motion.name = name;
  motion.next = next;
}

function play(name, { once = false } = {}) {
  const preset = presets.get(name);
  if (!preset) {
    log(`Unknown motion "${name}". Try: ${[...presets.keys()].join(', ')}`, true);
    return false;
  }
  const heelsVersion = heel.angle > 0 ? presets.get(`${name}${heelSuffix}`) : null;
  startClip(name, (heelsVersion || preset).clip, { loop: once ? false : preset.loop, next: preset.next });
  return true;
}

function runSequence(names) {
  const unknown = names.filter((n) => !presets.has(n));
  if (unknown.length) {
    log(`Unknown motion: ${unknown.join(', ')}`, true);
    return;
  }
  motion.queue = names.slice(1);
  play(names[0], { once: motion.queue.length > 0 });
}

function stopAll() {
  mixer.stopAllAction();
  motion.current = null;
  motion.name = '';
  motion.queue = [];
  clearWalkPlan();
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
  if (footLock) {
    const { action } = footLock;
    if (action !== motion.current || !action.enabled) footLock = null;
    else {
      if (footLock.started) footLock.elapsed += dt;
      footLock.started = true;
      const w = footLock.fade > 0 ? Math.min(1, footLock.elapsed / footLock.fade) : 1;
      const step = w - footLock.applied;
      footLock.applied = w;
      _travel.x += footLock.dx * step;
      _travel.z += footLock.dz * step;
      if (w >= 1) footLock = null;
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
      play(preset.stop);
      motion.fade = fade;
      if (walk.target) walk.arrive = { point: walk.target.clone(), clip: presets.get(preset.stop).clip };
      if (flow.target && isWalkLoop(activities[flow.target]?.loop)) flow.target = null;
      walk.halt = false;
      walk.paces = null;
      walk.target = null;
    }
  }
}

const turns = config.turns || {};
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
  startClip(name, preset.clip, { loop: false, next });
  motion.fade = fade;
  turnState.set(motion.current, { warp, startYaw: rig.rotation.y });
  return true;
}

function startWalk({ style = 'normal', paces = null, target = null, homeward = false } = {}) {
  if (!homeward && pinned('walk')) return;
  const loopName = style === 'relaxed' ? 'walk_relaxed' : 'walk';
  const startName = style === 'relaxed' ? 'home_to_walk_relaxed' : 'home_to_walk';
  if (!presets.has(loopName) || !presets.has(startName)) return log(`Walk clips for "${style}" are not set up`, true);
  clearWalkPlan();
  view.closeup = false;
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
    if (motion.name !== loopName) play(loopName);
    return;
  }
  request(style === 'relaxed' ? 'walk_relaxed' : 'walk');
}

function haltWalk() {
  request('home');
}

function activityOf(name) {
  for (const [act, a] of Object.entries(activities)) {
    if (name === a.loop) return { act, phase: 'loop' };
    if (name === a.enter) return { act, phase: 'enter' };
    if (name === a.exit) return { act, phase: 'exit' };
    if (name === a.once) return { act, phase: 'once' };
  }
  return { act: 'home', phase: name === 'home' ? 'loop' : 'other' };
}

function parentOf(act) {
  return act === 'home' ? null : activities[act]?.parent || 'home';
}

function chainOf(act) {
  const chain = [];
  for (let a = act; a; a = parentOf(a)) chain.unshift(a);
  return chain;
}

function nextStep(from, to) {
  const a = chainOf(from);
  const b = chainOf(to);
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i++;
  if (a.length > i) return { type: 'exit', act: a[a.length - 1] };
  if (b.length > i) return { type: 'enter', act: b[i] };
  return null;
}

function playSettled(act) {
  play(act === 'home' ? 'home' : activities[act].loop);
}

function request(target) {
  if (target !== 'home' && !activities[target]) return log(`Unknown activity "${target}"`, true);
  if (target !== 'home' && !lifeConfig.pool.includes(target) && !isWalkLoop(activities[target].loop) && pinned(target)) return;
  motion.queue = [];
  flow.faceYaw = null;
  flow.glide = null;
  flow.after = [];
  if (target === 'home') {
    walk.paces = null;
    walk.target = null;
    marker.visible = false;
  }
  flow.target = target;
  advance();
}

function advance() {
  if (!flow.target || isTurn(motion.name)) return;
  let { act, phase } = activityOf(motion.name);
  if (phase === 'enter' || phase === 'exit') return;
  if (phase === 'once') act = 'home';
  const step = nextStep(act, flow.target);
  if (!step) {
    flow.target = null;
    if (act === 'home' && motion.name !== 'home') play('home');
    return;
  }
  const a = activities[step.act];
  if (step.type === 'exit') {
    if (isWalkLoop(a.loop)) walk.halt = true;
    else play(a.exit);
    return;
  }
  if (isWalkLoop(a.loop) && walk.target && motion.name === 'home') {
    const aim = walk.waypoints[0] || walk.target;
    const want = Math.atan2(aim.x - rig.position.x, aim.z - rig.position.z);
    const yaw = facingYaw();
    const diff = Math.atan2(Math.sin(want - yaw), Math.cos(want - yaw));
    if (Math.abs(diff) > THREE.MathUtils.degToRad(turns.minAngle ?? 35) && playTurn(diff, a.enter, routeRemaining() > 1.2)) return;
    if (Math.abs(diff) > 0.6) {
      const desired = flow.target;
      flow.target = null;
      flow.faceYaw = want;
      flow.after.unshift(() => {
        flow.target = desired;
        advance();
      });
      return;
    }
  }
  if (a.approach && !atApproach(a.approach)) {
    goApproach(a.approach);
    return;
  }
  if (a.once) {
    flow.target = null;
    play(a.once);
    return;
  }
  play(a.enter);
}

function facingYaw() {
  _forward.set(0, 0, walk.facingSign).applyQuaternion(rig.quaternion);
  return Math.atan2(_forward.x, _forward.z);
}

function atApproach(name) {
  const f = furniture.get(name);
  if (!f) return true;
  const yaw = facingYaw();
  const turn = Math.atan2(Math.sin(f.facing - yaw), Math.cos(f.facing - yaw));
  return Math.hypot(rig.position.x - f.entry.x, rig.position.z - f.entry.z) < 0.08 && Math.abs(turn) < 0.05;
}

function goApproach(name) {
  const f = furniture.get(name);
  const desired = flow.target;
  const far = Math.hypot(rig.position.x - f.entry.x, rig.position.z - f.entry.z) > stepAside;
  const sitDown = () => {
    flow.target = desired;
    advance();
  };
  if (far) {
    startWalk({ style: 'normal', target: { x: f.entry.x, z: f.entry.z } });
    flow.after.push(() => {
      flow.faceYaw = f.facing;
      flow.after.push(sitDown);
    });
  } else {
    flow.target = null;
    if (motion.name !== 'home') play('home');
    settleAt(f.entry, f.facing);
    flow.after.push(sitDown);
  }
}

function resetPosition() {
  if (pinned('reset')) return;
  clearWalkPlan();
  view.closeup = false;
  const delta = new THREE.Vector3(home.x, 0, home.z).sub(rig.position);
  rig.position.set(home.x, 0, home.z);
  rig.rotation.y = THREE.MathUtils.degToRad(home.facing) + (walk.facingSign > 0 ? 0 : Math.PI);
  camera.position.add(delta);
  controls.target.add(delta);
}

mixer.addEventListener('finished', (e) => {
  if (e.action !== motion.current) return;
  if (motion.queue.length) {
    const name = motion.queue.shift();
    play(name, { once: motion.queue.length > 0 });
    return;
  }
  const { act, phase } = activityOf(motion.name);
  if (phase === 'exit') {
    playSettled(parentOf(act));
    advance();
    return;
  }
  if (phase === 'enter') {
    play(activities[act].loop);
    advance();
    return;
  }
  if (motion.next) {
    const abandoned = isTurn(motion.name) && leadsToWalk(motion.next) && !(walk.target && isWalkLoop(activities[flow.target]?.loop));
    play(abandoned ? 'home' : motion.next);
    advance();
  }
});

function goHome() {
  request('home');
}

function closeUp() {
  if (pinned('close-up')) return;
  const spot = { x: 0, z: 4.4, facing: 0, ...config.closeup };
  if (room.camera) setCameraMode('room');
  walkToSpot(spot);
  flow.after.push(() => {
    view.closeup = true;
    if (room.camera) return;
    setCameraMode('orbit');
    aimPoint(controls.target);
    camera.position.set(spot.x, controls.target.y, spot.z + 1.4);
  });
}

const distanceTo = (spot) => Math.hypot(rig.position.x - spot.x, rig.position.z - spot.z);

function returnHome() {
  if (pinned('home')) return;
  walkToSpot(home);
}

function walkToSpot(spot) {
  const yaw = THREE.MathUtils.degToRad(spot.facing);
  if (distanceTo(spot) > stepAside) {
    startWalk({ style: 'normal', target: { x: spot.x, z: spot.z }, homeward: true });
    flow.after.push(() => { flow.faceYaw = yaw; });
  } else {
    request('home');
    settleAt(spot, yaw);
  }
}

function settleAt(spot, yaw) {
  flow.glide = new THREE.Vector3(spot.x, 0, spot.z);
  flow.faceYaw = yaw;
}

const face = config.face || {};
function findBone(name) {
  let found = null;
  model.traverse((o) => { if (!found && o.isBone && o.name === name) found = o; });
  return found;
}
const bones = {};
for (const key of ['jawBone', 'eyeLeft', 'eyeRight', 'neck', 'head']) {
  const bone = face[key] ? findBone(face[key]) : null;
  if (!bone) log(`Bone not found for ${key}: ${face[key]}`, true);
  bones[key] = bone;
}
const boneDrivers = (face.boneDrivers || []).map((d) => {
  const bone = findBone(d.bone);
  if (!bone) log(`Bone not found for driver: ${d.bone}`, true);
  return { ...d, object: bone };
}).filter((d) => d.object);
const baseTransforms = new Map([...Object.values(bones), ...boneDrivers.map((d) => d.object)].filter(Boolean).map((b) => [b, { q: b.quaternion.clone(), p: b.position.clone() }]));

const _q = new THREE.Quaternion();
const _pw = new THREE.Quaternion();
const _bw = new THREE.Quaternion();
function rotateWorld(bone, axis, angle) {
  if (!bone || !angle) return;
  bone.getWorldQuaternion(_bw);
  bone.parent.getWorldQuaternion(_pw);
  _q.setFromAxisAngle(axis, angle).multiply(_bw);
  bone.quaternion.copy(_pw.invert().multiply(_q));
  bone.updateMatrixWorld(true);
}

const _up = new THREE.Vector3();
const _eyeL = new THREE.Vector3();
const _eyeR = new THREE.Vector3();
const up = new THREE.Vector3(0, 1, 0);
function faceAxes() {
  if (!bones.eyeLeft || !bones.eyeRight) return null;
  bones.eyeLeft.getWorldPosition(_eyeL);
  bones.eyeRight.getWorldPosition(_eyeR);
  const lateral = _eyeL.clone().sub(_eyeR).normalize();
  const forward = lateral.clone().cross(up).normalize();
  const center = _eyeL.clone().add(_eyeR).multiplyScalar(0.5);
  return { lateral, forward, center };
}

const gestureDefs = config.gestures || {};
const activeGestures = [];
const breathing = { on: config.breathing?.on ?? true, t: 0, bones: null };

function curveAt(keys, t, column = 1) {
  if (!keys || !keys.length) return 0;
  if (t <= keys[0][0]) return keys[0][column];
  for (let i = 1; i < keys.length; i++) {
    if (t <= keys[i][0]) {
      const a = keys[i - 1];
      const b = keys[i];
      const f = (t - a[0]) / (b[0] - a[0]);
      const e = 0.5 - 0.5 * Math.cos(Math.PI * f);
      return a[column] + (b[column] - a[column]) * e;
    }
  }
  return keys[keys.length - 1][column];
}

function gestureLength(def) {
  const ends = [def.head, def.shoulders, ...Object.values(def.morphs || {})].filter(Boolean).map((k) => k[k.length - 1][0]);
  return Math.max(0, ...ends);
}

function playGesture(name) {
  const def = gestureDefs[name];
  if (!def) return log(`No gesture "${name}". Gestures: ${Object.keys(gestureDefs).join(', ')}`, true);
  for (let i = activeGestures.length - 1; i >= 0; i--) if (activeGestures[i].name === name) activeGestures.splice(i, 1);
  activeGestures.push({ name, def, t: 0, length: gestureLength(def) });
}

function ensureBones(names) {
  return names.map((n) => {
    const b = findBone(n);
    if (b && !baseTransforms.has(b)) baseTransforms.set(b, { q: b.quaternion.clone(), p: b.position.clone() });
    return b;
  });
}

function applyGestures(dt, axes, morphs) {
  const deg = THREE.MathUtils.degToRad;
  let pitch = 0;
  let yaw = 0;
  let roll = 0;
  let shrug = 0;
  for (let i = activeGestures.length - 1; i >= 0; i--) {
    const g = activeGestures[i];
    g.t += dt;
    if (g.t > g.length) {
      activeGestures.splice(i, 1);
      continue;
    }
    if (g.def.head) {
      pitch += curveAt(g.def.head, g.t, 1);
      yaw += curveAt(g.def.head, g.t, 2);
      roll += curveAt(g.def.head, g.t, 3);
    }
    if (g.def.shoulders) shrug += curveAt(g.def.shoulders, g.t, 1);
    for (const [name, keys] of Object.entries(g.def.morphs || {})) morphs.set(name, (morphs.get(name) || 0) + curveAt(keys, g.t, 1));
  }
  let chest = 0;
  if (breathing.on) {
    breathing.t += dt;
    const period = config.breathing?.period ?? 4.2;
    const phase = 0.5 - 0.5 * Math.cos((2 * Math.PI * breathing.t) / period);
    const rest = restingAct() ? config.breathing?.restMultiplier ?? 1.4 : 1;
    chest = (config.breathing?.chestDegrees ?? 1.2) * phase * rest;
    shrug += (config.breathing?.shoulderDegrees ?? 0.8) * phase * rest;
  }
  if (!axes) return;
  const faceUp = new THREE.Vector3().crossVectors(axes.forward, axes.lateral).normalize();
  if (pitch || yaw || roll) {
    for (const [bone, share] of [[bones.neck, 0.4], [bones.head, 0.6]]) {
      rotateWorld(bone, axes.lateral, deg(pitch * share));
      rotateWorld(bone, faceUp, deg(yaw * share));
      rotateWorld(bone, axes.forward, deg(roll * share));
    }
  }
  if (chest || shrug) {
    breathing.bones ||= ensureBones(['CC_Base_Spine02', 'CC_Base_L_Clavicle', 'CC_Base_R_Clavicle']);
    const [spine, left, right] = breathing.bones;
    if (spine && chest) rotateWorld(spine, axes.lateral, deg(-chest));
    if (left && shrug) rotateWorld(left, axes.forward, deg(shrug));
    if (right && shrug) rotateWorld(right, axes.forward, deg(-shrug));
  }
}

const lifeConfig = { on: true, pool: ['idle'], switchSeconds: [25, 50], glanceSeconds: [2.5, 6], holdSeconds: [1, 3], cameraChance: 0.45, fidgetSeconds: [8, 16], fidgets: [], headShare: 0.55, headSpeed: 2.6, eyeSpeed: 14, ...(config.idleLife || {}) };
const life = { on: lifeConfig.on, listening: false, settled: 0, switchIn: 0, glanceIn: 1.5, holdLeft: 0, fidgetIn: 0, target: { yaw: 0, pitch: 0, camera: false }, eye: { yaw: 0, pitch: 0 }, head: { yaw: 0, pitch: 0, roll: 0 } };
const between = ([a, b]) => a + Math.random() * (b - a);
life.switchIn = between(lifeConfig.switchSeconds);
life.fidgetIn = between(lifeConfig.fidgetSeconds);

function restingAct() {
  const { act, phase } = activityOf(motion.name);
  return phase === 'loop' && (lifeConfig.rest || ['sit', 'lie']).includes(act) ? act : null;
}

function lifeActive() {
  if (!life.on || look.on || flow.target || walk.target) return false;
  const { act, phase } = activityOf(motion.name);
  return phase === 'loop' && (act === 'home' || lifeConfig.pool.includes(act) || (lifeConfig.rest || ['sit', 'lie']).includes(act));
}

function pickGlance() {
  const cameraChance = life.listening ? 0.9 : lifeConfig.cameraChance;
  const roll = Math.random();
  if (roll < cameraChance) life.target = { yaw: 0, pitch: 0, camera: true };
  else if (roll < cameraChance + (1 - cameraChance) * 0.7) life.target = { yaw: (Math.random() < 0.5 ? -1 : 1) * (12 + Math.random() * 23), pitch: -4 + Math.random() * 14, camera: false };
  else life.target = { yaw: -10 + Math.random() * 20, pitch: 12 + Math.random() * 8, camera: false };
  life.holdLeft = between(lifeConfig.holdSeconds) * (life.listening ? 1.6 : 1);
}

function cameraAngles(axes) {
  const d = camera.position.clone().sub(axes.center);
  const side = d.dot(axes.lateral);
  const ahead = d.dot(axes.forward);
  const up = new THREE.Vector3().crossVectors(axes.forward, axes.lateral).normalize();
  const yaw = THREE.MathUtils.radToDeg(Math.atan2(side, Math.max(0.05, ahead)));
  const pitch = -THREE.MathUtils.radToDeg(Math.atan2(d.dot(up), Math.hypot(side, ahead)));
  return { yaw: THREE.MathUtils.clamp(yaw, -40, 40), pitch: THREE.MathUtils.clamp(pitch, -20, 20), visible: ahead > 0 };
}

function applyIdleLife(dt, axes, morphs) {
  const active = lifeActive();
  let want = { yaw: 0, pitch: 0 };
  let roll = 0;
  if (active && axes) {
    life.settled += dt;
    const { act } = activityOf(motion.name);
    if (lifeConfig.pool.includes(act) && lifeConfig.pool.length > 1 && !(graphConfig.on && idleGraph.frames.length)) {
      life.switchIn -= dt;
      if (life.switchIn <= 0) {
        life.switchIn = between(lifeConfig.switchSeconds);
        const others = lifeConfig.pool.filter((n) => n !== act && activities[n]);
        if (others.length) request(others[Math.floor(Math.random() * others.length)]);
      }
    }
    life.glanceIn -= dt;
    if (life.glanceIn <= 0) {
      pickGlance();
      life.glanceIn = life.holdLeft + between(lifeConfig.glanceSeconds);
    }
    life.holdLeft -= dt;
    if (life.holdLeft > 0) {
      if (life.target.camera) {
        const c = cameraAngles(axes);
        want = c.visible ? c : { yaw: 0, pitch: 0 };
      } else want = life.target;
      if (restingAct() === 'lie') want = { yaw: THREE.MathUtils.clamp(want.yaw, -20, 20) * 0.6, pitch: THREE.MathUtils.clamp(want.pitch, -10, 10) * 0.5 };
    }
    if (life.listening) roll = 3;
    if ((idleGraphActive() || restingAct()) && graphConfig.swaySpineDegrees) {
      breathing.bones ||= ensureBones(['CC_Base_Spine02', 'CC_Base_L_Clavicle', 'CC_Base_R_Clavicle']);
      const spine = breathing.bones[0];
      const t = idleGraph.clock;
      const amp = THREE.MathUtils.degToRad(graphConfig.swaySpineDegrees);
      if (spine) {
        rotateWorld(spine, axes.forward, amp * Math.sin(t * 0.37 + 0.6) * Math.sin(t * 0.11));
        rotateWorld(spine, axes.lateral, amp * 0.6 * Math.sin(t * 0.23 + 2.1));
      }
    }
    life.fidgetIn -= dt;
    if (life.fidgetIn <= 0) {
      life.fidgetIn = between(lifeConfig.fidgetSeconds) * (life.listening ? 2 : 1);
      const options = restingAct() === 'lie' ? [] : lifeConfig.fidgets.filter((n) => gestureDefs[n]);
      if (options.length && !activeGestures.length) playGesture(options[Math.floor(Math.random() * options.length)]);
    }
  } else {
    life.settled = 0;
  }
  const ke = Math.min(1, dt * lifeConfig.eyeSpeed);
  const kh = Math.min(1, dt * lifeConfig.headSpeed);
  life.eye.yaw += (want.yaw - life.eye.yaw) * ke;
  life.eye.pitch += (want.pitch - life.eye.pitch) * ke;
  life.head.yaw += (want.yaw * lifeConfig.headShare - life.head.yaw) * kh;
  life.head.pitch += (want.pitch * lifeConfig.headShare - life.head.pitch) * kh;
  life.head.roll += (roll - life.head.roll) * kh;
  const eyeYaw = life.eye.yaw - life.head.yaw;
  const eyePitch = life.eye.pitch - life.head.pitch;
  const addMorph = (name, w) => { if (w > 0.001) morphs.set(name, (morphs.get(name) || 0) + Math.min(1, w)); };
  addMorph('Eye_L_Look_L', eyeYaw / 30);
  addMorph('Eye_R_Look_L', eyeYaw / 30);
  addMorph('Eye_L_Look_R', -eyeYaw / 30);
  addMorph('Eye_R_Look_R', -eyeYaw / 30);
  addMorph('Eye_L_Look_Down', eyePitch / 25);
  addMorph('Eye_R_Look_Down', eyePitch / 25);
  addMorph('Eye_L_Look_Up', -eyePitch / 20);
  addMorph('Eye_R_Look_Up', -eyePitch / 20);
  if (!axes) return;
  const { yaw, pitch } = life.head;
  if (Math.abs(yaw) + Math.abs(pitch) + Math.abs(life.head.roll) < 0.01) return;
  const deg = THREE.MathUtils.degToRad;
  const faceUp = new THREE.Vector3().crossVectors(axes.forward, axes.lateral).normalize();
  for (const [bone, share] of [[bones.neck, 0.4], [bones.head, 0.6]]) {
    rotateWorld(bone, faceUp, deg(yaw * share));
    rotateWorld(bone, axes.lateral, deg(pitch * share));
    rotateWorld(bone, axes.forward, deg(life.head.roll * share));
  }
}

const blink = { on: true, t: 0, nextAt: 1.5, duration: 0.18, double: false };
function blinkWeight(dt) {
  if (!blink.on) return 0;
  blink.t += dt;
  if (blink.t < blink.nextAt) return 0;
  const p = (blink.t - blink.nextAt) / blink.duration;
  if (p >= 1) {
    blink.t = 0;
    blink.double = !blink.double && Math.random() < 0.15;
    blink.nextAt = blink.double ? 0.08 : 2 + Math.random() * 4;
    return 0;
  }
  return p < 0.4 ? p / 0.4 : 1 - (p - 0.4) / 0.6;
}

const eyelids = { shut: 0 };
function eyelidWeight(dt) {
  const want = presets.get(motion.name)?.eyesClosed ? 1 : 0;
  eyelids.shut += (want - eyelids.shut) * Math.min(1, dt * (want ? 2 : 8));
  return eyelids.shut;
}

const talk = { on: false, t: 0, target: 0, value: 0 };
function talkValue(dt) {
  if (!talk.on) {
    talk.value += (0 - talk.value) * Math.min(1, dt * 12);
    return talk.value;
  }
  talk.t -= dt;
  if (talk.t <= 0) {
    talk.t = 0.07 + Math.random() * 0.12;
    talk.target = Math.random() < 0.15 ? 0 : 0.25 + Math.random() * 0.75;
  }
  talk.value += (talk.target - talk.value) * Math.min(1, dt * 18);
  return talk.value;
}

const audio = { el: null, ctx: null, analyser: null, buffer: null, value: 0 };
function audioValue(dt) {
  if (!audio.analyser || audio.el.paused) {
    audio.value += (0 - audio.value) * Math.min(1, dt * 12);
    return audio.value;
  }
  audio.analyser.getFloatTimeDomainData(audio.buffer);
  let sum = 0;
  for (const x of audio.buffer) sum += x * x;
  const rms = Math.sqrt(sum / audio.buffer.length);
  const target = Math.min(1, Math.max(0, (rms - 0.01) * 6));
  const rate = target > audio.value ? 25 : 10;
  audio.value += (target - audio.value) * Math.min(1, dt * rate);
  return audio.value;
}

const look = { on: false, yaw: 0, pitch: 0, pointer: new THREE.Vector2() };
renderer.domElement.addEventListener('pointermove', (e) => {
  const r = renderer.domElement.getBoundingClientRect();
  look.pointer.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
});
const raycaster = new THREE.Raycaster();
function updateLook(dt, axes) {
  let yaw = 0;
  let pitch = 0;
  if (look.on && axes) {
    raycaster.setFromCamera(look.pointer, camera);
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(camera.getWorldDirection(new THREE.Vector3()).negate(), axes.center);
    const point = raycaster.ray.intersectPlane(plane, new THREE.Vector3());
    if (point) {
      const d = point.sub(axes.center);
      const side = d.dot(axes.lateral);
      const ahead = Math.max(0.05, d.dot(axes.forward));
      yaw = THREE.MathUtils.clamp(Math.atan2(side, ahead), -1, 1);
      pitch = THREE.MathUtils.clamp(-Math.atan2(d.y, Math.hypot(side, ahead)), -0.5, 0.5);
    }
  }
  const k = Math.min(1, dt * 6);
  look.yaw += (yaw - look.yaw) * k;
  look.pitch += (pitch - look.pitch) * k;
  if (!axes || (Math.abs(look.yaw) < 1e-4 && Math.abs(look.pitch) < 1e-4)) return;
  rotateWorld(bones.neck, up, look.yaw * 0.4);
  rotateWorld(bones.neck, axes.lateral, look.pitch * 0.4);
  rotateWorld(bones.head, up, look.yaw * 0.6);
  rotateWorld(bones.head, axes.lateral, look.pitch * 0.6);
}

const explorer = new Map();
const bodyShape = new Map(Object.entries(config.bodyMorphs || {}));
const expressions = new Map(Object.entries(config.expressions || {}).map(([name, shape]) => [name, { shape, target: 0, value: 0 }]));
const faceState = { manualMouth: 0 };

const timer = new THREE.Timer();
let fpsFrames = 0;
let fpsTime = 0;
let fps = 0;
let facingChecked = false;

function frame() {
  frameNumber += 1;
  timer.update();
  const dt = Math.min(timer.getDelta(), 0.1);
  for (const [bone, base] of baseTransforms) {
    bone.quaternion.copy(base.q);
    bone.position.copy(base.p);
  }
  const before = rig.position.clone();
  updateIdleGraph(dt);
  mixer.update(dt);
  for (const [bone, base] of baseTransforms) {
    base.q.copy(bone.quaternion);
    base.p.copy(bone.position);
  }
  smoothHead(dt);
  updateLocomotion(dt);
  autoIdle(dt);
  if (view.mode === 'follow') {
    const moved = rig.position.clone().sub(before);
    camera.position.add(moved);
    controls.target.add(moved);
  } else if (view.mode === 'room') {
    trackWithRoomCamera(dt);
  } else if (view.mode === 'voice') {
    trackVoiceCamera(dt);
  }
  keyLight.position.copy(rig.position).add(keyOffset);
  keyLight.target.position.copy(rig.position);
  rig.updateMatrixWorld(true);

  let axes = faceAxes();
  if (!facingChecked && axes) {
    facingChecked = true;
    if (axes.forward.z < 0) {
      walk.facingSign = -1;
      rig.rotation.y += Math.PI;
      rig.updateMatrixWorld(true);
      axes = faceAxes();
      log('Model faced away from the camera; turned it 180°.');
    }
  }
  applyHeelPose(axes);
  updateLook(dt, axes);
  axes = faceAxes();

  const mouth = Math.max(faceState.manualMouth, talkValue(dt), audioValue(dt));
  const morphs = new Map(bodyShape);
  for (const [name, w] of explorer) morphs.set(name, (morphs.get(name) || 0) + w);
  const b = Math.max(blinkWeight(dt), eyelidWeight(dt));
  for (const name of face.blink || []) morphs.set(name, (morphs.get(name) || 0) + b);
  for (const e of expressions.values()) {
    e.value += (e.target - e.value) * Math.min(1, dt * 6);
    if (e.value < 0.001) continue;
    for (const [name, w] of Object.entries(e.shape)) morphs.set(name, (morphs.get(name) || 0) + e.value * w);
  }
  for (const [name, scale] of Object.entries(face.mouthMorphs || {})) morphs.set(name, (morphs.get(name) || 0) + mouth * scale);
  applyGestures(dt, axes, morphs);
  axes = faceAxes();
  applyIdleLife(dt, axes, morphs);
  axes = faceAxes();

  const resolved = resolveFrame(morphs);
  if (axes) {
    const faceUp = _up.crossVectors(axes.forward, axes.lateral).normalize();
    for (const d of boneDrivers) {
      let degrees = 0;
      for (const [name, k] of Object.entries(d.morphs)) degrees += Math.min(1, Math.max(0, resolved.get(resolveMorph(name)) || 0)) * k;
      if (degrees) rotateWorld(d.object, d.axis === 'up' ? faceUp : axes.lateral, THREE.MathUtils.degToRad(degrees));
    }
  }
  applyMorphs(resolved);
  applyFloorContact(dt);

  controls.update();
  renderer.render(scene, camera);

  fpsFrames += 1;
  fpsTime += dt;
  if (fpsTime >= 0.5) {
    fps = fpsFrames / fpsTime;
    fpsFrames = 0;
    fpsTime = 0;
  }
  const a = motion.current;
  const clipInfo = a ? `${motion.name}  ${a.time.toFixed(2)} / ${a.getClip().duration.toFixed(2)}s${a.loop === THREE.LoopOnce ? '  once' : '  loop'}` : 'bind pose';
  const queued = motion.queue.length ? `\nqueue: ${motion.queue.join(' → ')}` : motion.next ? `\nthen: ${motion.next}` : '';
  const where = `pos ${rig.position.x.toFixed(2)}, ${rig.position.z.toFixed(2)} m   steps ${walk.steps}${walk.paces !== null ? ` / ${walk.paces}` : ''}${walk.target ? `   to ${walk.target.x.toFixed(1)}, ${walk.target.z.toFixed(1)}` : ''}${walk.halt ? '   halting' : ''}`;
  $('hud').textContent = `${clipInfo}${queued}\n${where}\nmouth ${mouth.toFixed(2)}   ${fps.toFixed(0)} fps`;
}

const motionButtons = [
  ['Home', returnHome],
  ['Work', () => setVoice(!voice.on)],
  ['Idle', () => request('idle')],
  ...(activities.idle_b ? [['Idle B', () => request('idle_b')]] : []),
  ['Halt', goHome],
  ['Close-up', closeUp],
  ['Sit', () => request('sit')],
  ['Lie down', () => request('lie')],
  ['Dance', () => request('dance')],
];
for (const [label, action] of motionButtons) {
  const button = document.createElement('button');
  button.textContent = label;
  button.id = `motion_${label.toLowerCase().replace(/\W+/g, '_')}`;
  button.onclick = action;
  $('presets').append(button);
}

for (const [key, clip] of rawClips) {
  const [file, clipName] = key.split('::');
  const option = document.createElement('option');
  option.value = key;
  option.textContent = `${baseName(file)} › ${clipName} (${clip.duration.toFixed(2)}s)`;
  $('rawClips').append(option);
}
$('playRaw').onclick = () => {
  const key = $('rawClips').value;
  motion.queue = [];
  const clip = $('mirrorRaw').classList.contains('on') ? mirrorClip(rawClips.get(key)) : rawClips.get(key);
  startClip(key.split('::')[1], clip, { loop: motion.loopRaw });
};
$('mirrorRaw').onclick = () => $('mirrorRaw').classList.toggle('on');
$('loopToggle').onclick = () => {
  motion.loopRaw = !motion.loopRaw;
  $('loopToggle').classList.toggle('on', motion.loopRaw);
};
$('stopAll').onclick = stopAll;
$('resetPos').onclick = resetPosition;
function goToSpot(name, style = 'normal') {
  if (pinned('goto')) return;
  const spot = room.spots.get(name.toLowerCase());
  if (!spot) return log(`No spot "${name}". Spots: ${[...room.spots.keys()].join(', ')}`, true);
  log(`Walking to ${name} (${spot.x.toFixed(2)}, ${spot.z.toFixed(2)})`);
  startWalk({ style, target: { x: spot.x, z: spot.z } });
}

const view = { away: false, neutralAim: null, neutralFov: null, mode: 'orbit', baseFov: camera.fov, aim: new THREE.Vector3(), roomPos: new THREE.Vector3(), frameHeight: config.room?.frameHeight ?? 2.3, voiceShot: config.room?.voiceShot || null };

function aimPoint(out) {
  if (view.closeup && bones.head) {
    bones.head.getWorldPosition(out);
    out.y += config.closeup?.aimAboveHead ?? 0.06;
    return out;
  }
  return out.set(rig.position.x, rig.position.y + (config.room?.aimHeight ?? 1.0), rig.position.z);
}

function readRoomCamera() {
  const shot = config.room?.camera || {};
  room.camera.updateMatrixWorld(true);
  if (shot.position) view.roomPos.fromArray(shot.position);
  else room.camera.getWorldPosition(view.roomPos);
  if (shot.lookAt) view.neutralAim = new THREE.Vector3().fromArray(shot.lookAt);
  else {
    const look = new THREE.Vector3(0, 0, -1).applyQuaternion(room.camera.getWorldQuaternion(new THREE.Quaternion()));
    view.neutralAim = view.roomPos.clone().add(look.multiplyScalar(5));
  }
  view.neutralFov = shot.fov || room.camera.fov || camera.fov;
}

function setCameraMode(mode) {
  if (mode === 'room' && !room.camera) return log('The room file has no camera.', true);
  const fixed = ['room', 'voice'];
  const leavingFixed = fixed.includes(view.mode) && !fixed.includes(mode);
  view.mode = mode;
  controls.enabled = !fixed.includes(mode);
  if (mode === 'voice') view.aim.copy(controls.target);
  if (mode === 'room') {
    readRoomCamera();
    camera.position.copy(view.roomPos);
    aimPoint(view.aim);
    controls.target.copy(view.aim);
    camera.lookAt(view.aim);
    trackWithRoomCamera(1);
  } else if (leavingFixed) {
    camera.fov = view.baseFov;
    camera.updateProjectionMatrix();
  }
  if (mode === 'follow') aimPoint(controls.target);
  for (const m of ['orbit', 'follow', 'room']) $(`cam_${m}`).classList.toggle('on', m === mode);
}

function insideRoom(position, margin) {
  if (!room.bounds) return true;
  return position.x > room.bounds.min.x + margin && position.x < room.bounds.max.x - margin
    && position.z > room.bounds.min.z + margin && position.z < room.bounds.max.z - margin;
}

const shotPosition = new THREE.Vector3();
const shotAim = new THREE.Vector3();
const shotFrame = { boxes: null, inside: null };
const _corner = new THREE.Vector3();
const _chase = new THREE.Vector3();

function shotBoxes(names) {
  if (!shotFrame.boxes) {
    shotFrame.boxes = names.map((n) => room.scene?.getObjectByName(n)).filter(Boolean).map((o) => new THREE.Box3().setFromObject(o));
    if (shotFrame.boxes.length < names.length) log(`Voice shot: some of ${names.join(', ')} are missing from the room`, true);
  }
  return shotFrame.boxes;
}

function roomInside() {
  if (!shotFrame.inside) {
    const box = new THREE.Box3();
    room.scene?.traverse((o) => { if (o.isMesh && /Wall/i.test(o.name)) box.expandByObject(o); });
    shotFrame.inside = box.isEmpty() ? null : box.expandByVector(new THREE.Vector3(-0.5, 0, -0.5));
  }
  return shotFrame.inside;
}

function framedShot(shot) {
  if (shot.position && shot.lookAt) {
    shotPosition.fromArray(shot.position);
    shotAim.fromArray(shot.lookAt);
    return true;
  }
  const boxes = shotBoxes(shot.frame || []);
  if (!boxes.length) return false;
  const all = boxes.reduce((sum, b) => sum.union(b), new THREE.Box3());
  const yaw = THREE.MathUtils.degToRad(shot.facing || 0);
  const dir = new THREE.Vector3(-Math.sin(yaw), 0, -Math.cos(yaw));
  const right = new THREE.Vector3(Math.cos(yaw), 0, -Math.sin(yaw));
  const center = all.getCenter(new THREE.Vector3());
  const middle = boxes[0].getCenter(new THREE.Vector3()).sub(center).dot(right);
  center.addScaledVector(right, middle);
  const tanV = Math.tan(THREE.MathUtils.degToRad(shot.fov ?? view.baseFov) / 2);
  const tanH = tanV * camera.aspect;
  const grow = 1 + (shot.margin ?? 0.05);
  let distance = 0;
  for (let i = 0; i < 8; i++) {
    _corner.set(i & 1 ? all.max.x : all.min.x, i & 2 ? all.max.y : all.min.y, i & 4 ? all.max.z : all.min.z).sub(center);
    const near = -_corner.dot(dir);
    distance = Math.max(distance, near + Math.abs(_corner.dot(right)) * grow / tanH, near + Math.abs(_corner.y) * grow / tanV);
  }
  shotPosition.copy(center).addScaledVector(dir, -distance);
  shotAim.copy(center);
  return true;
}

function trackVoiceCamera(dt) {
  const shot = view.voiceShot;
  const k = 1 - Math.exp(-dt * (shot?.speed ?? 2));
  const her = aimPoint(new THREE.Vector3());
  const framed = shot && framedShot(shot);
  if (framed) {
    _chase.set(rig.position.x - work.x, 0, rig.position.z - work.z).add(shotPosition);
    const inside = roomInside();
    if (inside) _chase.clamp(inside.min, inside.max);
  } else {
    _chase.copy(camera.position);
  }
  if (voice.on) {
    camera.position.lerp(_chase, k);
    view.aim.lerp(framed ? shotAim.lerp(her, THREE.MathUtils.clamp(distanceTo(work) / 2, 0, 1)) : her, k);
    aimCameraFov(shot?.fov ?? view.baseFov, k);
    return;
  }
  const going = walk.target || walk.arrive;
  const toRoom = voice.cameraBefore === 'room' && room.camera;
  const along = going ? THREE.MathUtils.clamp(distanceTo(home) / 3, 0, 1) : 0;
  if (toRoom) _chase.lerp(view.roomPos, 1 - along);
  camera.position.lerp(_chase, k);
  view.aim.lerp(her, k);
  aimCameraFov(toRoom ? THREE.MathUtils.lerp(view.neutralFov, shot?.fov ?? view.baseFov, along) : view.baseFov, k);
  if (!going && (!toRoom || camera.position.distanceTo(view.roomPos) < 0.05)) setCameraMode(voice.cameraBefore || 'follow');
}

function aimCameraFov(fov, k) {
  camera.fov += (fov - camera.fov) * k;
  camera.updateProjectionMatrix();
  controls.target.copy(view.aim);
}

function setVoice(on) {
  if (on === voice.on) return;
  voice.on = on;
  $('voiceToggle').classList.toggle('on', on);
  $('motion_work').classList.toggle('on', on);
  if (on) {
    if (view.mode !== 'voice') voice.cameraBefore = view.mode;
    const { act } = activityOf(motion.name);
    const settled = (act === 'home' || lifeConfig.pool.includes(act)) && !walk.target && distanceTo(work) < 0.3;
    if (!settled) walkToSpot(work);
    if (gestureDefs.nod) playGesture('nod');
    setCameraMode('voice');
    setListening(true);
    log('Voice on: she walks to her work spot and stays there.');
  } else {
    setListening(false);
    if (room.camera) readRoomCamera();
    walkToSpot(home);
    log('Voice off: she walks back home and the camera returns.');
  }
}

function trackWithRoomCamera(dt) {
  const k = 1 - Math.exp(-dt * 4);
  if (config.room?.neutralWhenAway !== false && view.neutralAim) {
    if (!view.away && !insideRoom(rig.position, 0)) {
      view.away = true;
      log('Iris left the room. Camera back to neutral.');
    } else if (view.away && insideRoom(rig.position, 0.25)) {
      view.away = false;
      log('Iris is back. Camera following.');
    }
  }
  view.aim.lerp(view.away ? view.neutralAim : aimPoint(new THREE.Vector3()), k);
  camera.position.copy(view.roomPos);
  controls.target.copy(view.aim);
  const distance = camera.position.distanceTo(view.aim);
  const frameHeight = view.closeup ? config.closeup?.frameHeight ?? 0.6 : view.frameHeight;
  const tracked = THREE.MathUtils.clamp(THREE.MathUtils.radToDeg(2 * Math.atan(frameHeight / 2 / distance)), 10, 60);
  const fov = view.away || !(config.room?.zoom || view.closeup) ? view.neutralFov : tracked;
  camera.fov += (fov - camera.fov) * k;
  camera.updateProjectionMatrix();
}

function setMapVisible(on) {
  navView.group.visible = on;
  $('mapToggle').classList.toggle('on', on);
}

function setRoomVisible(on) {
  if (!room.scene) return;
  room.scene.visible = on;
  grid.visible = !on;
  $('roomToggle').classList.toggle('on', on);
}

if (room.scene) {
  for (const name of room.spots.keys()) {
    const button = document.createElement('button');
    button.textContent = name.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
    button.onclick = () => goToSpot(name);
    $('spots').append(button);
  }
  $('roomToggle').onclick = () => setRoomVisible(!room.scene.visible);
  $('mapToggle').onclick = () => setMapVisible(!navView.group.visible);
  setRoomVisible(config.room.show !== false);
} else {
  $('roomSection').hidden = true;
}

for (const m of ['orbit', 'follow', 'room']) $(`cam_${m}`).onclick = () => setCameraMode(m);
if (!room.camera) $('cam_room').hidden = true;
setCameraMode(room.camera && config.room?.cameraMode !== 'orbit' ? config.room?.cameraMode || 'room' : 'follow');

function bindSlider(id, onChange, digits = 2) {
  const input = $(id);
  const out = $(`${id}Out`);
  const update = () => {
    out.textContent = Number(input.value).toFixed(digits);
    onChange(Number(input.value));
  };
  input.addEventListener('input', update);
  update();
  return (value) => { input.value = value; update(); };
}
const setSpeed = bindSlider('speed', (v) => {
  motion.speed = v;
  if (motion.current) motion.current.timeScale = v;
});
const setFade = bindSlider('fade', (v) => { motion.fade = v; });

const lighting = { exposure: 0.8, environment: 0.35, key: 1.4, rim: 0.6, fill: 0.2, ...(config.lighting || {}) };
function bindLight(id, value, apply) {
  $(id).value = value;
  bindSlider(id, apply);
}
bindLight('exposure', lighting.exposure, (v) => { renderer.toneMappingExposure = v; });
bindLight('environment', lighting.environment, (v) => { scene.environmentIntensity = v; });
bindLight('key', lighting.key, (v) => { keyLight.intensity = v; });
bindLight('rim', lighting.rim, (v) => { rimLight.intensity = v; });
bindLight('fill', lighting.fill, (v) => { fillLight.intensity = v; });
const setMouth = bindSlider('mouth', (v) => { faceState.manualMouth = v; });

function toggle(id, state, key) {
  const set = (on) => {
    state[key] = on;
    $(id).classList.toggle('on', on);
  };
  $(id).onclick = () => set(!state[key]);
  set(state[key]);
  return set;
}
const setBlink = (on) => { blink.on = on; };
const setLook = toggle('lookToggle', look, 'on');
const setTalk = toggle('talkToggle', talk, 'on');

$('audioFile').onchange = (e) => {
  const file = e.target.files[0];
  if (!file) return;
  if (!audio.ctx) {
    audio.ctx = new AudioContext();
    audio.el = new Audio();
    const source = audio.ctx.createMediaElementSource(audio.el);
    audio.analyser = audio.ctx.createAnalyser();
    audio.analyser.fftSize = 1024;
    audio.buffer = new Float32Array(audio.analyser.fftSize);
    source.connect(audio.analyser);
    audio.analyser.connect(audio.ctx.destination);
  }
  audio.el.src = URL.createObjectURL(file);
  audio.ctx.resume();
  audio.el.play();
  log(`Playing ${file.name}`);
};

for (const name of morphNames) {
  const option = document.createElement('option');
  option.value = name;
  option.textContent = name;
  $('morphName').append(option);
}
const setMorphSlider = bindSlider('morphWeight', (v) => {
  const name = $('morphName').value;
  if (!name) return;
  if (v > 0) explorer.set(name, v);
  else explorer.delete(name);
}, 2);
$('morphName').onchange = () => {
  const v = explorer.get($('morphName').value) || 0;
  $('morphWeight').value = v;
  $('morphWeightOut').textContent = v.toFixed(2);
};
$('morphReset').onclick = () => {
  explorer.clear();
  setMorphSlider(0);
};

function addSliderRow(container, name, value, onInput) {
  const row = document.createElement('div');
  row.className = 'row';
  const label = document.createElement('label');
  label.textContent = name;
  const input = document.createElement('input');
  input.type = 'range';
  input.min = '0';
  input.max = '1';
  input.step = '0.01';
  input.value = value;
  const out = document.createElement('output');
  out.textContent = Number(value).toFixed(2);
  const set = (v) => {
    input.value = v;
    out.textContent = Number(v).toFixed(2);
    onInput(Number(v));
  };
  input.addEventListener('input', () => set(input.value));
  row.append(label, input, out);
  container.append(row);
  return set;
}

for (const [name, value] of bodyShape) {
  if (!morphNames.includes(name)) {
    log(`Body morph "${name}" is not in ${config.character}`, true);
    continue;
  }
  addSliderRow($('bodyMorphs'), name, value, (v) => bodyShape.set(name, v));
}

for (const name of Object.keys(gestureDefs)) {
  const button = document.createElement('button');
  button.textContent = name.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
  button.onclick = () => playGesture(name);
  $('gestures').append(button);
}
const setBreathing = (on) => { breathing.on = on; };
const setLife = (on) => {
  life.on = on;
  $('lifeToggle').classList.toggle('on', on);
};
const setListening = (on) => {
  life.listening = on;
  life.glanceIn = 0;
  $('listenToggle').classList.toggle('on', on);
};
$('lifeToggle').onclick = () => setLife(!life.on);
$('listenToggle').onclick = () => setListening(!life.listening);
$('voiceToggle').onclick = () => setVoice(!voice.on);
setLife(life.on);

const expressionSetters = new Map();
for (const [name, e] of expressions) {
  const missing = Object.keys(e.shape).filter((n) => !morphSet.has(resolveMorph(n)));
  if (missing.length) log(`Expression "${name}" uses missing morphs: ${missing.join(', ')}`, true);
  expressionSetters.set(name, addSliderRow($('expressions'), name, 0, (v) => { e.target = v; }));
}

const HELP = [
  'home (walk back to the home spot) · work (same as voice on) · idle · halt / stop (home pose where she stands) · closeup',
  'walk [relaxed] [paces] · goto <spot> [relaxed] · sit · lie (halt / home stands her up)',
  `Gestures: ${Object.keys(gestureDefs).join(', ').replace(/_/g, ' ')} · breathing on|off · life on|off · listen on|off`,
  'turn left|right|around [degrees] · dance (walks to the center first) · outfit <name> · outfits',
  'walkto <x> <z> [relaxed] · reset (back to the home spot)',
  'goto <spot> [relaxed] · spots · room on|off · map on|off · camera orbit|follow|room',
  'voice on|off (she goes to her work spot and stays; camera moves to the voice shot)',
  'camera shot (use the current view as the voice shot and print it for avatar.json)',
  'speed 0.5 · fade 0.3 · mouth 0.4 · freeze (bind pose)',
  'blink on|off · look on|off · talk on|off',
  'morph <name> <0..1> · morphs <filter>',
  `Expressions: ${[...expressions.keys()].join(', ')} <0..1|off> · neutral`,
  '  Typing one switches the others off; the sliders can be mixed.',
].join('\n');

const onOff = (word) => word === 'on' || word === 'true' || word === '1';
function runCommand(text) {
  const line = text.trim();
  if (!line) return;
  log(`> ${line}`);
  const [head, ...rest] = line.split(/\s+/);
  const command = head.toLowerCase();
  if (command === 'help' || command === '?') return log(HELP);
  if (command === 'stop' || command === 'halt') return goHome();
  if (command === 'home') return returnHome();
  if (command === 'work') return setVoice(true);
  if (command === 'closeup') return closeUp();
  if (command === 'outfit') return setOutfit(rest.join(' '));
  if (command === 'outfits') return log([...outfitCatalog.values()].map((e) => e.label).join(', '));
  if (command === 'turn') {
    const dir = rest[0];
    const degrees = Number(rest[1]) || (dir === 'around' ? 180 : 90);
    const angle = THREE.MathUtils.degToRad(dir === 'right' ? -degrees : degrees);
    if (!['left', 'right', 'around'].includes(dir)) return log('Usage: turn left|right|around [degrees]', true);
    const standing = activityOf(motion.name);
    const idling = standing.phase === 'loop' && lifeConfig.pool.includes(standing.act);
    const settle = idling ? activities[standing.act].loop : config.autoIdle !== false && activities.idle ? activities.idle.loop : 'home';
    if (motion.name === 'home' || idling) return playTurn(angle, settle, true) || log('Turn clips are not set up', true);
    if (isWalkLoop(motion.name)) return playTurn(angle, motion.name) || log('Turn clips are not set up', true);
    return log('Turns work from home (Halt first) or while walking.', true);
  }
  if (command === 'freeze') return stopAll();
  if (command === 'speed') return setSpeed(Number(rest[0]) || 1);
  if (command === 'fade') return setFade(Number(rest[0]) || 0);
  if (command === 'mouth') return setMouth(Number(rest[0]) || 0);
  if (command === 'blink') return setBlink(onOff(rest[0]));
  if (command === 'look') return setLook(onOff(rest[0]));
  if (command === 'talk') return setTalk(onOff(rest[0]));
  if (command === 'morph') {
    if (!morphNames.includes(rest[0])) return log(`No morph "${rest[0]}"`, true);
    const w = Number(rest[1]) || 0;
    if (w > 0) explorer.set(rest[0], w);
    else explorer.delete(rest[0]);
    return;
  }
  if (command === 'walk') {
    const style = rest.includes('relaxed') ? 'relaxed' : 'normal';
    const n = rest.map(Number).find((v) => Number.isFinite(v) && v > 0);
    return startWalk({ style, paces: n ?? null });
  }
  if (command === 'walkto') {
    const style = rest.includes('relaxed') ? 'relaxed' : 'normal';
    const [x, z] = rest.map(Number).filter((v) => Number.isFinite(v));
    if (x === undefined || z === undefined) return log('Usage: walkto <x> <z> [relaxed]   (meters; she starts at 0 0 facing +z)', true);
    return startWalk({ style, target: { x, z } });
  }
  if (command === 'goto') {
    const style = rest.includes('relaxed') ? 'relaxed' : 'normal';
    const name = rest.filter((w) => w !== 'relaxed').join('_');
    return goToSpot(name, style);
  }
  if (command === 'spots') return log(room.spots.size ? [...room.spots].map(([n, v]) => `${n} (${v.x.toFixed(2)}, ${v.z.toFixed(2)})`).join('\n') : 'No room loaded.');
  if (command === 'room') return setRoomVisible(onOff(rest[0]));
  if (command === 'roomcam') return setCameraMode('room');
  if (command === 'map') return setMapVisible(onOff(rest[0]));
  if (command === 'voice') return setVoice(onOff(rest[0] ?? 'on'));
  if (command === 'camera' && rest[0] === 'shot') {
    const round = (v) => v.toArray().map((n) => Number(n.toFixed(2)));
    view.voiceShot = { ...view.voiceShot, position: round(camera.position), lookAt: round(controls.target), fov: Number(camera.fov.toFixed(1)) };
    return log(`"voiceShot": ${JSON.stringify(view.voiceShot)}`);
  }
  if (command === 'camera') {
    if (!['orbit', 'follow', 'room'].includes(rest[0])) return log('Usage: camera orbit|follow|room', true);
    return setCameraMode(rest[0]);
  }
  if (command === 'reset') return resetPosition();
  const gestureName = gestureDefs[`${command}_${rest[0]}`] ? `${command}_${rest[0]}` : command;
  if (gestureDefs[gestureName]) return playGesture(gestureName);
  if (command === 'breathing') return setBreathing(onOff(rest[0]));
  if (command === 'life') return setLife(onOff(rest[0]));
  if (command === 'listen' || command === 'listening') return setListening(onOff(rest[0] ?? 'on'));
  if (command === 'idle' && rest[0] === 'b' && activities.idle_b) return request('idle_b');
  if (command === 'neutral') {
    for (const set of expressionSetters.values()) set(0);
    return;
  }
  if (expressionSetters.has(command)) {
    const v = rest[0] === undefined ? 1 : rest[0] === 'off' ? 0 : Number(rest[0]) || 0;
    for (const [name, set] of expressionSetters) if (name !== command) set(0);
    return expressionSetters.get(command)(Math.min(1, Math.max(0, v)));
  }
  if (command === 'morphs') {
    const filter = (rest[0] || '').toLowerCase();
    return log(morphNames.filter((n) => n.toLowerCase().includes(filter)).join(', '));
  }
  if (activities[command]) return request(command);
  runSequence(line.split(/\s*,\s*|\s+then\s+|\s+/).filter(Boolean));
}

const history = [];
let historyIndex = 0;
$('cmd').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    const value = $('cmd').value;
    if (value.trim()) history.push(value);
    historyIndex = history.length;
    $('cmd').value = '';
    runCommand(value);
  } else if (e.key === 'ArrowUp' && history.length) {
    historyIndex = Math.max(0, historyIndex - 1);
    $('cmd').value = history[historyIndex];
    e.preventDefault();
  } else if (e.key === 'ArrowDown' && history.length) {
    historyIndex = Math.min(history.length, historyIndex + 1);
    $('cmd').value = history[historyIndex] || '';
    e.preventDefault();
  }
});

window.avatar = { floorContact, heelsRecordingWeight, graphConfig, idleGraph, life, mirrorClip, playGesture, activeGestures, breathing, heel, renderer, scene, keyLight, play, runSequence, stopAll, runCommand, setMorph: (n, w) => (w > 0 ? explorer.set(n, w) : explorer.delete(n)), motion, presets, rawClips, model, mixer, camera, controls, bodyShape, rig, walk, startWalk, haltWalk, resetPosition, setOutfit, outfitCatalog, parts, request, goHome, returnHome, closeUp, flow, furniture, nav, planRoute, room, goToSpot, walkToSpot, home, work, setCameraMode, view, voice, setVoice, setExpression: (n, v) => expressionSetters.get(n)?.(v) };

$('loading').remove();
log(`Loaded ${files.length} files, ${rawClips.size} clips, ${morphNames.length} morphs. Type "help".`);
if (config.start) {
  if (activities[config.start] || config.start === 'home') request(config.start);
  else runSequence([config.start]);
}
buildIdleGraph();
renderer.setAnimationLoop(frame);
