// File: ui/room/main.js

import { createAvatar } from './avatar.js';
import { createApi } from './api.js';
import { createPanel } from './panel.js';

const ctx = createAvatar();
const api = createApi(ctx);
createPanel(ctx, document);
window.avatarDev = ctx;
await ctx.init(document.getElementById('stage'));
window.avatar = api;
