// File: ui/room/host.js

export function connectHost(ctx, api, doc) {
  const params = new URLSearchParams(location.search);
  const inDev = () => doc.body.classList.contains('dev');
  const setDev = (on) => {
    doc.body.classList.toggle('dev', on);
    if (ctx.camera) ctx.camera.layers[on ? 'enable' : 'disable'](1);
  };
  if (params.has('dev')) setDev(true);
  ctx.on('ready', () => setDev(inDev()));
  doc.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.shiftKey && e.code === 'KeyD') {
      e.preventDefault();
      setDev(!inDev());
    }
  });

  const transport = window.qt?.webChannelTransport;
  if (!transport || typeof QWebChannel !== 'function') return null;

  let host = null;
  const pending = [];
  const send = (name, ...args) => {
    if (host) host[name](...args);
    else pending.push([name, args]);
  };
  ctx.on('progress', ({ loaded, total }) => send('progress', loaded, total));
  ctx.on('log', ({ message, isError }) => send('log', message, Boolean(isError)));
  ctx.on('loadError', (message) => send('loadError', message));
  ctx.on('ready', () => send('pageReady'));
  ctx.on('monitorClicked', () => send('openChat'));
  doc.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !inDev() && e.target === doc.body) send('openChat');
  });

  new QWebChannel(transport, (channel) => {
    host = channel.objects.host;
    host.stateChanged.connect((changes) => api.setState(changes));
    host.speechLevel.connect((level) => api.speechLevel(level));
    host.commandRequested.connect((text) => api.command(text));
    host.viewChanged.connect((view) => { ctx.hostView = view; });
    host.monitorImage.connect((url) => ctx.monitor?.showPicture(url));
    for (const [name, args] of pending.splice(0)) host[name](...args);
    ctx.host = host;
  });
  return send;
}
