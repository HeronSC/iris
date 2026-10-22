// File: ui/room/events.js

export function createEvents(ctx) {
  const listeners = new Map();
  const on = (type, fn) => {
    if (!listeners.has(type)) listeners.set(type, new Set());
    listeners.get(type).add(fn);
    return () => listeners.get(type)?.delete(fn);
  };
  const emit = (type, value) => {
    for (const fn of listeners.get(type) || []) fn(value);
  };
  const log = (message, isError = false) => emit('log', { message, isError });
  Object.assign(ctx, { on, emit, log });
}
