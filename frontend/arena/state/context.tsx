'use client';
import { createContext, useContext, useSyncExternalStore } from 'react';
import { ArenaStore } from './store';
export const ArenaStoreContext = createContext<ArenaStore | null>(null);
export function useArenaStore() {
  const s = useContext(ArenaStoreContext);
  if (!s) throw new Error('Arena store missing');
  return s;
}
export function useArena() {
  const s = useArenaStore();
  return useSyncExternalStore(s.subscribe, s.getSnapshot, s.getSnapshot);
}
/** Subscribe to supplied data only; procedural geometry does not rerender on clock ticks. */
export function useArenaData() {
  const s = useArenaStore();
  return useSyncExternalStore(
    s.subscribe,
    () => s.getSnapshot().arena,
    () => s.getSnapshot().arena,
  );
}
