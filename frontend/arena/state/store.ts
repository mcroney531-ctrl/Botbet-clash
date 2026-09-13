import {
  ArenaInput,
  ArenaState,
  ArenaView,
  BroadcastEvent,
  CameraPreset,
  CAMERAS,
  CompetitorId,
  IDS,
  PresentationState,
} from './types';
import { createMockState } from '../demo/mock-state';
import { DEMO_CUES, DEMO_DURATION } from '../demo/timeline';
import { validateEvent, validateState } from './validation';
export function standings(state: ArenaState) {
  return IDS.map((id) => state.competitors[id]).sort(
    (a, b) =>
      b.bankroll - a.bankroll || IDS.indexOf(a.competitor_id) - IDS.indexOf(b.competitor_id),
  );
}
const closeCamera = (id: CompetitorId): CameraPreset => `${id.toUpperCase()}_CLOSE` as CameraPreset;
function presentation(): PresentationState {
  return {
    clock: 0,
    camera_target: 'MASTER',
    auto_director: true,
    reduced_motion: false,
    active_event: null,
    event_queue: [],
    serial: 0,
    changed_at: { gpt: 0, claude: 0, gemini: 0 },
    previous_bankroll: { gpt: 15, claude: 15, gemini: 15 },
    demo: { playing: false, elapsed: 0, cursor: 0, speed: 1, finished: false },
  };
}
export function createArenaStore(initial: ArenaState = createMockState()) {
  validateState(initial);
  initial = structuredClone(initial);
  const freshPresentation = () => ({
    ...presentation(),
    previous_bankroll: Object.fromEntries(
      IDS.map((id) => [id, initial.competitors[id].bankroll]),
    ) as Record<CompetitorId, number>,
  });
  let view: ArenaView = { arena: structuredClone(initial), presentation: freshPresentation() };
  let listeners = new Set<() => void>();
  const emit = () => listeners.forEach((fn) => fn());
  const updateP = (patch: Partial<PresentationState>) => {
    view = { ...view, presentation: { ...view.presentation, ...patch } };
  };
  function direct(e: BroadcastEvent): CameraPreset {
    if (
      e.type === 'HEAD_TO_HEAD' &&
      e.participants?.includes('claude') &&
      e.participants.includes('gemini') &&
      !e.participants.includes('gpt')
    )
      return 'MASTER';
    if (e.type === 'HEAD_TO_HEAD')
      return e.participants?.includes('claude')
        ? 'HEAD_TO_HEAD_LEFT_CENTER'
        : 'HEAD_TO_HEAD_CENTER_RIGHT';
    if (['UNANIMOUS', 'LEAD_CHANGE', 'BANKRUPTCY', 'WEEK_RECAP'].includes(e.type))
      return 'SCOREBOARD';
    return e.competitor_id ? closeCamera(e.competitor_id) : 'MASTER';
  }
  function activate(e: BroadcastEvent) {
    const p = view.presentation;
    updateP({
      active_event: {
        ...e,
        serial: p.serial + 1,
        started_at: p.clock,
        expires_at: p.clock + (e.duration ?? 4),
      },
      serial: p.serial + 1,
      ...(p.auto_director ? { camera_target: direct(e) } : {}),
    });
  }
  function enqueue(event: BroadcastEvent) {
    validateEvent(event);
    const e = structuredClone(event);
    if (!view.presentation.active_event) activate(e);
    else updateP({ event_queue: [...view.presentation.event_queue, e].slice(-12) });
  }
  function apply(input: ArenaInput) {
    if (input.type === 'RESET') {
      view = {
        arena: structuredClone(initial),
        presentation: { ...freshPresentation(), reduced_motion: view.presentation.reduced_motion },
      };
      return;
    }
    if (input.type === 'CAMERA') {
      if (!CAMERAS.includes(input.camera)) throw new TypeError('Unknown camera');
      updateP({ camera_target: input.camera });
      return;
    }
    if (input.type === 'BROADCAST') {
      enqueue(input.event);
      return;
    }
    if (input.type === 'REPLACE_STATE') {
      validateState(input.state);
      const next = structuredClone(input.state),
        p = view.presentation;
      const previous_bankroll = { ...p.previous_bankroll },
        changed_at = { ...p.changed_at };
      for (const id of IDS) {
        previous_bankroll[id] = view.arena.competitors[id].bankroll;
        if (
          view.arena.competitors[id].status !== next.competitors[id].status ||
          view.arena.competitors[id].bankroll !== next.competitors[id].bankroll
        )
          changed_at[id] = p.clock;
      }
      view = {
        arena: next,
        presentation: {
          ...p,
          previous_bankroll,
          changed_at,
          active_event: null,
          event_queue: [],
          camera_target: 'MASTER',
        },
      };
      return;
    }
    if (input.type !== 'UPDATE_COMPETITOR' || !IDS.includes(input.competitor_id))
      throw new TypeError('Unknown arena input');
    const id = input.competitor_id,
      old = view.arena.competitors[id],
      priorLeader = standings(view.arena)[0].competitor_id;
    const next = {
      ...view.arena,
      competitors: {
        ...view.arena.competitors,
        [id]: { ...old, ...structuredClone(input.patch), competitor_id: id },
      },
    };
    validateState(next);
    view = { ...view, arena: next };
    const c = next.competitors[id],
      p = view.presentation;
    if (c.status !== old.status || c.bankroll !== old.bankroll)
      updateP({
        changed_at: { ...p.changed_at, [id]: p.clock },
        previous_bankroll: { ...p.previous_bankroll, [id]: old.bankroll },
      });
    if (input.announce !== false && c.status !== old.status) {
      const map = {
        POUNCE: 'POUNCE',
        BET_EXECUTED: 'BET_LOCKED',
        PASS: 'PASS',
        WIN: 'WIN',
        LOSS: 'LOSS',
        BUSTED: 'BANKRUPTCY',
      } as const;
      const type = map[c.status as keyof typeof map];
      if (type) enqueue({ type, competitor_id: id, duration: c.status === 'POUNCE' ? 5 : 4 });
    }
    const leader = standings(next)[0];
    if (input.announce !== false && leader.competitor_id !== priorLeader)
      enqueue({ type: 'LEAD_CHANGE', competitor_id: leader.competitor_id, duration: 3 });
  }
  return {
    getSnapshot: () => view,
    subscribe: (fn: () => void) => {
      listeners.add(fn);
      return () => {
        listeners.delete(fn);
      };
    },
    dispatch: (input: ArenaInput) => {
      apply(input);
      emit();
    },
    // Presentation clock only. No money, prediction, eligibility or outcome calculation.
    advance: (seconds: number) => {
      if (!Number.isFinite(seconds) || seconds < 0) throw new TypeError('Invalid elapsed seconds');
      const p = view.presentation;
      if (!p.demo.playing && p.demo.cursor > 0 && !p.demo.finished) return;
      updateP({ clock: p.clock + seconds * (p.demo.playing ? p.demo.speed : 1) });
      if (p.demo.playing) {
        const elapsed = Math.min(DEMO_DURATION, p.demo.elapsed + seconds * p.demo.speed);
        let cursor = p.demo.cursor;
        while (cursor < DEMO_CUES.length && DEMO_CUES[cursor].at <= elapsed) {
          for (const input of DEMO_CUES[cursor].inputs) apply(input);
          cursor++;
        }
        updateP({
          demo: {
            ...p.demo,
            elapsed,
            cursor,
            playing: elapsed < DEMO_DURATION,
            finished: elapsed >= DEMO_DURATION,
          },
        });
      }
      const active = view.presentation.active_event;
      if (active && view.presentation.clock >= active.expires_at) {
        const [next, ...rest] = view.presentation.event_queue;
        updateP({ active_event: null, event_queue: rest });
        if (next) activate(next);
        else if (view.presentation.auto_director) updateP({ camera_target: 'MASTER' });
      }
      emit();
    },
    takeControl: () => {
      const p = view.presentation;
      if (p.demo.cursor > 0 || p.demo.playing)
        updateP({
          active_event: null,
          event_queue: [],
          camera_target: 'MASTER',
          demo: { ...p.demo, playing: false, cursor: 0, elapsed: 0, finished: false },
        });
      emit();
    },
    play: () => {
      if (view.presentation.demo.finished || view.presentation.demo.cursor === 0) {
        const speed = view.presentation.demo.speed;
        const auto_director = view.presentation.auto_director;
        apply({ type: 'RESET' });
        updateP({ auto_director, demo: { ...view.presentation.demo, speed } });
      }
      updateP({ demo: { ...view.presentation.demo, playing: true } });
      emit();
    },
    pause: () => {
      updateP({ demo: { ...view.presentation.demo, playing: false } });
      emit();
    },
    setSpeed: (speed: number) => {
      if (![1, 2, 4].includes(speed)) throw new TypeError('Invalid demo speed');
      updateP({ demo: { ...view.presentation.demo, speed } });
      emit();
    },
    setDirector: (enabled: boolean) => {
      updateP({ auto_director: enabled });
      emit();
    },
    setReducedMotion: (enabled: boolean) => {
      updateP({ reduced_motion: enabled });
      emit();
    },
  };
}
export type ArenaStore = ReturnType<typeof createArenaStore>;
