import { ArenaStore } from './store';
import { ArenaInput, ArenaState } from './types';
/** Trusted in-process adapter. No HTTP, SSE, or cross-origin message listener. */
export function createArenaBoundary(store: ArenaStore) {
  return {
    getState: (): ArenaState => structuredClone(store.getSnapshot().arena),
    replaceState: (state: ArenaState) => {
      store.takeControl();
      store.dispatch({ type: 'REPLACE_STATE', state });
    },
    dispatch: (event: ArenaInput) => {
      store.takeControl();
      store.dispatch(event);
    },
    subscribe: (listener: (state: ArenaState) => void) => {
      let prior = store.getSnapshot().arena;
      return store.subscribe(() => {
        const next = store.getSnapshot().arena;
        if (next !== prior) {
          prior = next;
          listener(structuredClone(next));
        }
      });
    },
  };
}
export type ArenaBoundary = ReturnType<typeof createArenaBoundary>;
declare global {
  interface Window {
    botbetArena?: ArenaBoundary;
  }
}
