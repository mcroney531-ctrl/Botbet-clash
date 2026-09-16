'use client';
import { useState } from 'react';
import { useArena, useArenaStore } from '@/arena/state/context';
import {
  BOARD_EVENTS,
  CAMERAS,
  CompetitorId,
  IDS,
  STATUSES,
  Status,
  TicketStatus,
} from '@/arena/state/types';
import { MOCK_TICKETS } from '@/arena/demo/mock-state';
export default function DeveloperPanel() {
  const store = useArenaStore(),
    { arena, presentation: p } = useArena();
  const [id, setId] = useState<CompetitorId>('gemini'),
    [error, setError] = useState('');
  const c = arena.competitors[id];
  function act(fn: () => void) {
    try {
      store.takeControl();
      fn();
      setError('');
    } catch (e) {
      setError((e as Error).message);
    }
  }
  function status(s: Status) {
    const map: Partial<Record<Status, TicketStatus>> = {
      WATCHING: 'WATCH',
      STRONG: 'PENDING',
      POUNCE: 'POUNCE',
      BET_EXECUTED: 'LOCKED',
      LIVE: 'LIVE',
      WIN: 'WIN',
      LOSS: 'LOSS',
    };
    const ticket = map[s] ? { ...(c.ticket ?? MOCK_TICKETS[id]), status: map[s]! } : null;
    act(() =>
      store.dispatch({
        type: 'UPDATE_COMPETITOR',
        competitor_id: id,
        patch: {
          status: s,
          ticket,
          ...(s === 'LIVE' ? { live_value: 0, target_value: id === 'claude' ? 74 : 54 } : {}),
        },
      }),
    );
  }
  return (
    <aside className="debug-panel" aria-label="Developer controls">
      <div className="panel-heading">
        <span>SCENE LAB</span>
        <span className="tag">LOCAL ONLY</span>
      </div>
      <p>Manual actions pause the demo. Financial values are supplied independently.</p>
      <label>
        Competitor
        <select value={id} onChange={(e) => setId(e.target.value as CompetitorId)}>
          {IDS.map((id) => (
            <option key={id}>{id}</option>
          ))}
        </select>
      </label>
      <label>
        State
        <select value={c.status} onChange={(e) => status(e.target.value as Status)}>
          {STATUSES.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
      </label>
      <div className="field-grid">
        {(
          [
            'bankroll',
            'bankroll_delta',
            'confidence',
            'bankroll_risk_pct',
            'live_value',
            'target_value',
          ] as const
        ).map((field) => (
          <label key={field}>
            {field.replaceAll('_', ' ')}
            <input
              aria-label={field}
              type="number"
              step="0.1"
              key={`${id}-${field}`}
              value={c[field] ?? ''}
              onChange={(e) => {
                const value = e.target.value === '' ? null : Number(e.target.value);
                act(() =>
                  store.dispatch({
                    type: 'UPDATE_COMPETITOR',
                    competitor_id: id,
                    patch: { [field]: value },
                    announce: false,
                  }),
                );
              }}
            />
          </label>
        ))}
      </div>
      <label>
        Stake
        <input
          aria-label="stake"
          type="number"
          step=".5"
          value={c.ticket?.stake ?? MOCK_TICKETS[id].stake}
          onChange={(e) =>
            act(() =>
              store.dispatch({
                type: 'UPDATE_COMPETITOR',
                competitor_id: id,
                patch: {
                  ticket: { ...(c.ticket ?? MOCK_TICKETS[id]), stake: Number(e.target.value) },
                },
                announce: false,
              }),
            )
          }
        />
      </label>
      <div className="debug-actions">
        {(['POUNCE', 'BET_EXECUTED', 'PASS', 'LIVE', 'WIN', 'LOSS', 'BUSTED'] as Status[]).map(
          (s) => (
            <button key={s} onClick={() => status(s)}>
              {s.replaceAll('_', ' ')}
            </button>
          ),
        )}
      </div>
      <label>
        Camera
        <select
          value={p.camera_target}
          onChange={(e) =>
            act(() => {
              store.setDirector(false);
              store.dispatch({
                type: 'CAMERA',
                camera: e.target.value as (typeof CAMERAS)[number],
              });
            })
          }
        >
          {CAMERAS.map((c) => (
            <option key={c}>{c}</option>
          ))}
        </select>
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={p.auto_director}
          onChange={(e) => store.setDirector(e.target.checked)}
        />{' '}
        Automatic camera director
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={p.reduced_motion}
          onChange={(e) => store.setReducedMotion(e.target.checked)}
        />{' '}
        Reduced motion
      </label>
      <div className="debug-actions">
        <button
          onClick={() =>
            act(() =>
              store.dispatch({
                type: 'BROADCAST',
                event: {
                  type: 'UNANIMOUS',
                  title: 'UNANIMOUS',
                  lines: ['ALL THREE MODELS', 'OVER 64.5'],
                  participants: [...IDS],
                },
              }),
            )
          }
        >
          Unanimous pick
        </button>
        <button
          onClick={() =>
            act(() =>
              store.dispatch({
                type: 'BROADCAST',
                event: {
                  type: 'HEAD_TO_HEAD',
                  title: 'HEAD TO HEAD',
                  lines: ['GPT · OVER 53.5', 'VS', 'CLAUDE · UNDER 53.5'],
                  participants: ['gpt', 'claude'],
                },
              }),
            )
          }
        >
          Head-to-head
        </button>
      </div>
      <label>
        Board event
        <select
          defaultValue=""
          onChange={(e) => {
            if (e.target.value)
              act(() =>
                store.dispatch({
                  type: 'BROADCAST',
                  event: {
                    type: e.target.value as (typeof BOARD_EVENTS)[number],
                    competitor_id: id,
                  },
                }),
              );
            e.target.value = '';
          }}
        >
          <option value="">Trigger event…</option>
          {BOARD_EVENTS.map((type) => (
            <option key={type}>{type}</option>
          ))}
        </select>
      </label>
      <button className="reset" onClick={() => act(() => store.dispatch({ type: 'RESET' }))}>
        Reset arena
      </button>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
    </aside>
  );
}
