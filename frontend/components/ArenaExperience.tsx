'use client';
import dynamic from 'next/dynamic';
import { Component, ReactNode, useCallback, useEffect, useState } from 'react';
import { ArenaStoreContext, useArena, useArenaStore } from '@/arena/state/context';
import { createArenaStore, standings } from '@/arena/state/store';
import { createArenaBoundary, ArenaBoundary } from '@/arena/state/boundary';
import { ArenaState, IDS } from '@/arena/state/types';
import { COLORS, money } from '@/arena/scene/theme';
import { DEMO_CUES, DEMO_DURATION } from '@/arena/demo/timeline';
import DeveloperPanel from './DeveloperPanel';
const Scene = dynamic(() => import('@/arena/scene/ArenaScene'), { ssr: false });
class SceneError extends Component<{ children: ReactNode }, { error: boolean }> {
  state = { error: false };
  static getDerivedStateFromError() {
    return { error: true };
  }
  render() {
    return this.state.error ? (
      <div className="scene-error">
        <h2>3D rendering is unavailable</h2>
        <p>
          Enable hardware acceleration or open this page in a WebGL-capable browser. The state
          readout and controls remain available.
        </p>
      </div>
    ) : (
      this.props.children
    );
  }
}
function Runtime({ onBoundary }: { onBoundary?: (boundary: ArenaBoundary) => void }) {
  const store = useArenaStore();
  useEffect(() => {
    const boundary = createArenaBoundary(store);
    window.botbetArena = boundary;
    onBoundary?.(boundary);
    const media = matchMedia('(prefers-reduced-motion: reduce)');
    store.setReducedMotion(media.matches);
    const change = () => store.setReducedMotion(media.matches);
    media.addEventListener('change', change);
    let last = performance.now(),
      raf = 0;
    const tick = (now: number) => {
      const dt = (now - last) / 1000;
      if (dt >= 1 / 30) {
        last = now;
        if (!document.hidden) store.advance(dt);
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    const visibility = () => {
      last = performance.now();
    };
    document.addEventListener('visibilitychange', visibility);
    return () => {
      cancelAnimationFrame(raf);
      media.removeEventListener('change', change);
      document.removeEventListener('visibilitychange', visibility);
      if (window.botbetArena === boundary) delete window.botbetArena;
    };
  }, [store, onBoundary]);
  return null;
}
function BroadcastUI() {
  const { arena, presentation: p } = useArena(),
    store = useArenaStore();
  const [debug, setDebug] = useState(false),
    [ready, setReady] = useState(false),
    [readout, setReadout] = useState(false);
  const onReady = useCallback(() => setReady(true), []),
    demo = p.demo,
    seconds = Math.floor(demo.elapsed),
    cue = [...DEMO_CUES].reverse().find((c) => c.at <= demo.elapsed);
  const currentEvent = p.active_event?.type;
  return (
    <main className="experience">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">
            B<span>C</span>
          </span>
          <div>
            <h1>BOTBET CLASH</h1>
            <p>THREE MINDS. ONE MARKET.</p>
          </div>
        </div>
        <div className="broadcast-meta">
          <span className="live-dot" /> MOCK BROADCAST <span className="divider" /> WEEK{' '}
          {String(arena.week).padStart(2, '0')}
        </div>
        <button
          className={debug ? 'utility active' : 'utility'}
          aria-pressed={debug}
          onClick={() => setDebug(!debug)}
        >
          Scene lab <span>⌘</span>
        </button>
      </header>
      <section className="stage" aria-label="Arena broadcast">
        <SceneError>
          <Scene onReady={onReady} />
        </SceneError>
        {!ready && (
          <div className="loading">
            <span className="loading-ring" />
            <p>Preparing the studio</p>
            <small>Building editable 3D scene</small>
          </div>
        )}
        <div className="shot-label">
          <span className="red-dot" />
          {p.camera_target.replaceAll('_', ' ')}
          <span className="shot-tag">
            CAM{' '}
            {String(
              [
                'MASTER',
                'GPT_CLOSE',
                'CLAUDE_CLOSE',
                'GEMINI_CLOSE',
                'HEAD_TO_HEAD_LEFT_CENTER',
                'HEAD_TO_HEAD_CENTER_RIGHT',
                'SCOREBOARD',
                'CENTER_FLOOR',
              ].indexOf(p.camera_target) + 1,
            ).padStart(2, '0')}
          </span>
        </div>
        <div className="stage-caption">
          <span>{demo.playing ? 'ON AIR' : demo.finished ? 'WEEK COMPLETE' : 'THE ARENA'}</span>
          <h2>{demo.cursor ? cue?.label : 'A little room. Real competition.'}</h2>
          <p>
            {demo.cursor
              ? 'Independent decisions. Every ticket tells a story.'
              : 'Watch three AI analysts navigate a simulated NFL prop week.'}
          </p>
        </div>
        {debug && <DeveloperPanel />}
      </section>
      <section className="broadcast-controls" aria-label="Playback controls">
        <button
          className="play-button"
          disabled={!ready}
          onClick={() => (demo.playing ? store.pause() : store.play())}
        >
          <span>{demo.playing ? 'Ⅱ' : '▶'}</span>
          {demo.playing
            ? 'Pause broadcast'
            : demo.finished
              ? 'Replay week'
              : demo.cursor
                ? 'Resume week'
                : 'Play demo week'}
        </button>
        <div className="timeline">
          <div className="timeline-label">
            <span>
              {String(Math.floor(seconds / 60)).padStart(2, '0')}:
              {String(seconds % 60).padStart(2, '0')}
            </span>
            <span>{demo.finished ? 'FINAL' : (cue?.label ?? 'Ready for kickoff')}</span>
            <span>01:50</span>
          </div>
          <div
            className="timeline-track"
            role="progressbar"
            aria-label="Demo progress"
            aria-valuemin={0}
            aria-valuemax={DEMO_DURATION}
            aria-valuenow={seconds}
          >
            <span style={{ width: `${(demo.elapsed / DEMO_DURATION) * 100}%` }} />
            {[28, 34, 60, 84, 92].map((at) => (
              <i key={at} style={{ left: `${(at / DEMO_DURATION) * 100}%` }} />
            ))}
          </div>
        </div>
        <select
          aria-label="Playback speed"
          value={demo.speed}
          onChange={(e) => store.setSpeed(Number(e.target.value))}
        >
          <option value={1}>1×</option>
          <option value={2}>2×</option>
          <option value={4}>4×</option>
        </select>
        <button
          className="utility"
          title="Hold master camera; disables automatic director"
          onClick={() => {
            store.setDirector(false);
            store.dispatch({ type: 'CAMERA', camera: 'MASTER' });
          }}
        >
          Master view
        </button>
      </section>
      <footer className="footer">
        <span>
          LOCAL SIMULATION <span className="footer-separator">/</span> NO LIVE WAGERS
        </span>
        <button onClick={() => setReadout(!readout)} aria-expanded={readout}>
          State readout {readout ? '−' : '+'}
        </button>
        <span className="footer-right">TEAL · AMBER · BLUE</span>
      </footer>
      <p className="sr-only" aria-live="polite">
        {currentEvent
          ? `${currentEvent.replaceAll('_', ' ')} ${p.active_event?.competitor_id ?? ''}`
          : ''}
      </p>
      {readout && (
        <section className="readout" aria-label="Accessible arena state">
          <table>
            <caption>Supplied competition state</caption>
            <thead>
              <tr>
                <th>Rank</th>
                <th>Competitor</th>
                <th>Status</th>
                <th>Bankroll</th>
                <th>Confidence</th>
                <th>Risk</th>
                <th>Ticket</th>
                <th>Live</th>
              </tr>
            </thead>
            <tbody>
              {standings(arena).map((c, i) => (
                <tr key={c.competitor_id}>
                  <td>{i + 1}</td>
                  <td style={{ color: COLORS[c.competitor_id] }}>{c.display_name}</td>
                  <td>{c.status}</td>
                  <td>{money(c.bankroll)}</td>
                  <td>{c.confidence ?? '—'}</td>
                  <td>{c.bankroll_risk_pct === null ? '—' : `${c.bankroll_risk_pct}%`}</td>
                  <td>
                    {c.ticket
                      ? `${c.ticket.status} · ${c.ticket.side} ${c.ticket.line} · ${money(c.ticket.stake)}`
                      : '—'}
                  </td>
                  <td>
                    {c.live_value ?? '—'} / {c.target_value ?? '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </main>
  );
}
export default function ArenaExperience({
  initialState,
  onBoundary,
}: {
  initialState?: ArenaState;
  onBoundary?: (boundary: ArenaBoundary) => void;
}) {
  const [store] = useState(() => createArenaStore(initialState));
  return (
    <ArenaStoreContext.Provider value={store}>
      <Runtime onBoundary={onBoundary} />
      <BroadcastUI />
    </ArenaStoreContext.Provider>
  );
}
