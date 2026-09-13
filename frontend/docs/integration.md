# State and event integration

The canonical contract is `arena/state/types.ts`. `ArenaState` is authoritative supplied
competition data. `PresentationState` is local camera/timing/animation bookkeeping and
must not be mistaken for a ledger. `leaderboard` is a stable, descending visual sort of
supplied bankrolls (GPT/Claude/Gemini order breaks display ties); it is not an official
competition tiebreak rule. Board overlays and camera targets live in `PresentationState`.

## Schema

```ts
interface ArenaState {
  week: number;
  phase: string; // e.g. REGULAR_SEASON
  competitors: Record<
    'gpt' | 'claude' | 'gemini',
    {
      competitor_id: 'gpt' | 'claude' | 'gemini';
      display_name: string;
      status: Status;
      bankroll: number;
      bankroll_delta: number;
      confidence: number | null; // supplied 0–10
      bankroll_risk_pct: number | null; // supplied 0–100, never derived from stake
      uncertainty: string | null;
      market_count: number;
      live_value: number | null;
      target_value: number | null; // supplied positive tracker target, never rounded from line
      unit: string; // YDS, REC, etc.
      result: string | null; // supplied outcome, not inferred from live_value
      ticket: {
        player: string;
        prop_type: string;
        side: 'OVER' | 'UNDER';
        line: number;
        odds: number;
        stake: number;
        status: TicketStatus;
      } | null;
    }
  >;
}
```

`bankroll` and `bankroll_delta` are supplied display amounts in dollars. Convert backend
integer cents/decimal representations in the future adapter, not inside scene components.
Snapshots are full replacements and must contain all three competitors and all required
fields. Patches may omit unchanged fields. `ticket` in a patch is a whole replacement,
not a deep partial merge; send all ticket fields or `null` to clear it. Unknown/malformed
values throw `TypeError` before applying the data update. Validation checks presentation
shape/ranges only; it does not decide wager legality. Bankrolls must be finite/nonnegative.

Snapshot replacement is silent: it does not replay alerts or infer outcomes. Explicit
status changes via `UPDATE_COMPETITOR` announce Pounce, execution, pass, win, loss and
bust by default. Set `announce: false` when also supplying explicit broadcast events.
The automatic new-leader acknowledgment is a visual comparison of supplied bankrolls.
Event overlays do not change any competitor data; send the data update first.

## External application: React boundary

`ArenaExperience` creates one store per mounted arena, so multiple arenas are independent.
Supply initial state and receive the same imperative boundary used by the mock source:

```tsx
'use client';
import { useCallback, useEffect, useRef } from 'react';
import ArenaExperience from '@/components/ArenaExperience';
import type { ArenaBoundary } from '@/arena/state/boundary';
import type { ArenaState } from '@/arena/state/types';

export function EmbeddedArena({ state }: { state: ArenaState }) {
  const arena = useRef<ArenaBoundary | null>(null);
  const receive = useCallback((boundary: ArenaBoundary) => {
    arena.current = boundary;
  }, []);
  useEffect(() => {
    arena.current?.replaceState(state);
  }, [state]);
  return <ArenaExperience initialState={state} onBoundary={receive} />;
}
```

The host application owns transport and lifecycle. Subscribe to its backend/API/event
stream outside the scene, map that payload to `ArenaState` / `ArenaInput`, then call this
boundary. Unsubscribe the host transport when the embedding component unmounts.
No scene component imports a backend service or initiates a request.

## Exact console / plain JavaScript example

While the arena page is mounted, its trusted in-process boundary is also available as
`window.botbetArena`. This is a local integration/debug handle, not a public cross-origin
API. No `postMessage` listener or unauthenticated remote endpoint is installed.

```js
const arena = window.botbetArena;

// Full snapshot. Clone a complete current shape for this example.
const state = arena.getState();
state.week = 2;
state.competitors.gemini = {
  ...state.competitors.gemini,
  status: 'BET_EXECUTED',
  bankroll: 15,
  bankroll_delta: 0,
  confidence: 8.8,
  bankroll_risk_pct: 26.7,
  uncertainty: 'MEDIUM',
  ticket: {
    player: 'PLAYER X',
    prop_type: 'RECEIVING YARDS',
    side: 'OVER',
    line: 53.5,
    odds: -115,
    stake: 4,
    status: 'LOCKED',
  },
};
arena.replaceState(state);

// Separate broadcast instruction, after the data is in place.
arena.dispatch({
  type: 'BROADCAST',
  event: {
    type: 'BET_LOCKED',
    competitor_id: 'gemini',
    duration: 4,
  },
});

// Later, a supplied live update. No calculations or outcome inference.
arena.dispatch({
  type: 'UPDATE_COMPETITOR',
  competitor_id: 'gemini',
  announce: false,
  patch: {
    status: 'LIVE',
    live_value: 48,
    target_value: 54,
    unit: 'YDS',
    ticket: { ...state.competitors.gemini.ticket, status: 'LIVE' },
  },
});

// A final result and new balance arrive together from the authoritative producer.
arena.dispatch({
  type: 'UPDATE_COMPETITOR',
  competitor_id: 'gemini',
  patch: {
    status: 'WIN',
    result: 'WIN',
    bankroll: 18.48,
    bankroll_delta: 3.48,
    ticket: { ...state.competitors.gemini.ticket, status: 'WIN' },
  },
});

const unsubscribe = arena.subscribe((nextState) => console.log(nextState));
// When finished:
unsubscribe();
```

Calling `replaceState` or `dispatch` transfers control away from a running/paused demo and
clears its remaining queued choreography. Subsequent external events can queue normally.
`replaceState` also clears existing temporary overlays and returns the master view, making
snapshot reconnects quiet. `getState()` and subscriptions return defensive copies. Internal
`store.getSnapshot()` is a read-only view by convention and should not be mutated by consumers.

The mock demo can always be restarted using Play; it deliberately restores mock fixtures.
`initialState` initializes the mounted instance once; prop changes must use `replaceState`.

## Supported inputs

| Input                                                 | Effect                                                             |
| ----------------------------------------------------- | ------------------------------------------------------------------ |
| `REPLACE_STATE {state}`                               | Full supplied snapshot, no status-alert replay                     |
| `UPDATE_COMPETITOR {competitor_id, patch, announce?}` | Atomic validated patch; optional status/lead acknowledgments       |
| `BROADCAST {event}`                                   | Temporary board overlay and optional automatic camera cue          |
| `CAMERA {camera}`                                     | Select named camera; auto director may subsequently select another |
| `RESET`                                               | Restore initial snapshot and clear timeline/events/camera state    |

Use the development panel to disable the automatic director when holding a manual camera.
The store also exposes `play`, `pause`, `takeControl`, `setSpeed`, `setDirector`, and
`setReducedMotion` for an alternate host playback UI. They are presentation operations.

## Competitor and ticket states

| Competitor status | Screen / robot / light treatment                                                      |
| ----------------- | ------------------------------------------------------------------------------------- |
| IDLE              | READY, bankroll, relaxed small motions, low accents                                   |
| RESEARCHING       | Identity/status/bankroll, screen glances, tapping, calm accents                       |
| WATCHING          | Market count, focused lean; Claude thinking pose; raised candidate                    |
| STRONG            | Primary prop, stake, distinct confidence and risk meters; stronger accent             |
| POUNCE            | Prop prominence, rising/enlarging candidate, arm reaction, perimeter trace, board cue |
| BET_EXECUTED      | Stable locked physical ticket, stake and risk, stable accent, board acknowledgment    |
| PASS              | Neutral decisive reaction, bankroll and no-qualifying-edge text, no failure styling   |
| LIVE              | Supplied value/target and progress bar, official ticket remains                       |
| WIN               | Supplied delta/new bankroll, short arm lift and accent sweep, animated rank movement  |
| LOSS              | Supplied delta/new bankroll, brief head shake, softened light then normal accent      |
| BUSTED            | Dark display, explicit elimination text, muted accents, ticket removed; robot stays   |

Ticket statuses: `WATCH`, `PENDING`, `POUNCE`, `LOCKED`, `LIVE`, `WIN`, `LOSS`, `EXPIRED`.
A station status controls the presentation intensity. The supplied ticket status controls
its printed label/lock metaphor. Keep these coherent in the producer; the renderer does
not enforce a competition state machine. Null ticket data shows an empty dock or awaiting
prop screen. PASS/BUSTED suppress the physical card without inventing a settlement.

There is one visible active ticket per competitor in this initial presentation contract.
Historical/multiple tickets require a future contract extension, not a new room.

## Broadcast events and cameras

Board types: `WEEK_OPEN`, `POUNCE`, `BET_LOCKED`, `PASS`, `WIN`, `LOSS`, `LEAD_CHANGE`,
`UNANIMOUS`, `HEAD_TO_HEAD`, `BANKRUPTCY`, `WEEK_RECAP`.

```ts
interface BroadcastEvent {
  type: BoardEventType;
  competitor_id?: CompetitorId;
  title?: string;
  lines?: string[]; // explicit small overlay copy, max 5 lines
  participants?: CompetitorId[];
  duration?: number; // default 4 seconds, >0 and <=30
}
```

Overlays queue in arrival order (bounded to the latest 12 pending events), then expire back
to the standings. No permanently animated board or constant camera orbit. Repeated explicit
broadcast events are treated as intentional; a future stream adapter should deduplicate by
its authoritative event ID and handle stale/out-of-order updates before calling this API.

Eight presets: `MASTER`, `GPT_CLOSE`, `CLAUDE_CLOSE`, `GEMINI_CLOSE`,
`HEAD_TO_HEAD_LEFT_CENTER`, `HEAD_TO_HEAD_CENTER_RIGHT`, `SCOREBOARD`, `CENTER_FLOOR`.
Pounce/execution/result cues select competitor close-ups; lead/unanimous/bust/recap select
scoreboard; head-to-head selects the relevant pair. A WIN followed by a supplied lead
change naturally plays the close-up then scoreboard. After queued events finish, the
automatic director returns master. Pair presets cover GPT–Claude and GPT–Gemini. A direct
Claude–Gemini pairing should use MASTER until a third pair preset is added.

Motion is damped in `useFrame`; the mock clock is elapsed-time based, not interval-count
based. Demo speed scales choreography as well as cue time. Hidden tabs and deliberate
pause do not skip the story. Reduced motion removes loops, pulses and numerical/rank
interpolation and uses immediate camera preset changes.
