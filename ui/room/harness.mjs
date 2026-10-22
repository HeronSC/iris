// File: ui/room/harness.mjs

import { spawn, execSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';

const [url, debugPort = '9222', script = ''] = process.argv.slice(2);
const deterministic = process.env.DETERMINISTIC === '1';
const chrome = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const profile = path.join(os.tmpdir(), `iris-room-chrome-profile-${debugPort}`);
const child = spawn(chrome, ['--headless=new', '--disable-gpu', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--no-first-run', '--disable-extensions', `--user-data-dir=${profile}`, `--remote-debugging-port=${debugPort}`, '--window-size=640,400', 'about:blank'], { stdio: 'ignore' });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = (o) => console.log(JSON.stringify(o));

async function target() {
  for (let i = 0; i < 60; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${debugPort}/json`)).json();
      const page = list.find((t) => t.type === 'page');
      if (page) return page.webSocketDebuggerUrl;
    } catch {}
    await sleep(500);
  }
  throw new Error('chrome did not start');
}

const ws = new WebSocket(await target());
await new Promise((r) => ws.addEventListener('open', r));
let id = 0;
const pending = new Map();
const consoleLines = [];
ws.addEventListener('message', (e) => {
  const msg = JSON.parse(e.data);
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg);
    pending.delete(msg.id);
  } else if (msg.method === 'Runtime.exceptionThrown') {
    consoleLines.push(`EXCEPTION ${msg.params.exceptionDetails.text} ${msg.params.exceptionDetails.exception?.description || ''}`);
  } else if (msg.method === 'Runtime.consoleAPICalled' && ['error', 'warning'].includes(msg.params.type)) {
    consoleLines.push(`${msg.params.type.toUpperCase()} ${msg.params.args.map((a) => a.value ?? a.description).join(' ')}`);
  }
});
const send = (method, params = {}) => new Promise((resolve) => {
  const n = ++id;
  pending.set(n, resolve);
  ws.send(JSON.stringify({ id: n, method, params }));
});
const evaluate = async (expression) => {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) return `EVAL ERROR: ${r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text}`;
  return r.result?.result?.value;
};

await send('Runtime.enable');
await send('Page.enable');
await send('Page.addScriptToEvaluateOnNewDocument', { source: `(() => {
  try { localStorage.clear(); } catch {}
  let s = 12345;
  Math.random = () => { s = (s * 1103515245 + 12345) & 0x7fffffff; return s / 0x80000000; };
  window.__raf = window.requestAnimationFrame.bind(window);
  if (!${JSON.stringify(deterministic)}) return;
  let t = 0;
  window.requestAnimationFrame = (cb) => window.__raf(() => { t += 1000 / 30; cb(t); });
  performance.now = () => t;
})();` });
const waitFrames = (n) => evaluate(`new Promise((r) => { let n = 0; const tick = () => (++n >= ${n} ? r(true) : window.__raf(tick)); window.__raf(tick); })`);
const started = Date.now();
await send('Page.navigate', { url });

let ready = false;
for (let i = 0; i < 600 && !ready; i++) {
  await sleep(2000);
  const state = await evaluate(`window.avatar ? 'ready' : (document.getElementById('loadLabel')?.textContent || 'no label')`);
  if (state === 'ready') ready = true;
  else if (i % 15 === 14) out({ waiting: state, seconds: Math.round((Date.now() - started) / 1000) });
}
out({ ready, seconds: Math.round((Date.now() - started) / 1000) });
const readLog = () => evaluate(`[...document.querySelectorAll('#log div')].map((d) => d.textContent)`);
const hud = () => evaluate(`document.getElementById('hud').textContent`);
out({ log: await readLog() });
await evaluate(`document.getElementById('clearLog').click()`);
for (const step of script.split(';').map((s) => s.trim()).filter(Boolean)) {
  const [cmd, wait] = step.split('@');
  if (cmd.startsWith('js:')) {
    out({ js: cmd.slice(3), result: await evaluate(cmd.slice(3)) });
    continue;
  }
  await evaluate(`window.avatar.command(${JSON.stringify(cmd)})`);
  if (deterministic) await waitFrames(Math.round(Number(wait || 4) * 30));
  else await sleep(Number(wait || 4) * 1000);
  out({ cmd, hud: await hud(), log: await readLog() });
  await evaluate(`document.getElementById('clearLog').click()`);
}
out({ console: consoleLines });
try { execSync(`taskkill /PID ${child.pid} /T /F`, { stdio: 'ignore' }); } catch {}
process.exit(0);
