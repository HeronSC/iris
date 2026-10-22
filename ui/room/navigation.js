// File: ui/room/navigation.js

import * as THREE from 'three';

export function createNavigation(ctx) {
  const { config, room, log, scene, emit } = ctx;

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

  const nav = buildNavGrid();

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

  function setMapVisible(on) {
    navView.group.visible = on;
    emit('mapVisible', on);
  }

  Object.assign(ctx, { nav, planRoute, navView, showRoute, setMapVisible });
}
