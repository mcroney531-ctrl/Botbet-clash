import test from 'node:test';
import assert from 'node:assert/strict';
import { createArenaStore, standings } from '../arena/state/store';
import { createMockState, MOCK_TICKETS } from '../arena/demo/mock-state';
import { createArenaBoundary } from '../arena/state/boundary';
import { STATUSES } from '../arena/state/types';

test('one-button week delivers supplied results, retains official tickets, and returns master', () => {
  const s = createArenaStore();
  s.play();
  for (let i = 0; i < 1110; i++) s.advance(0.1);
  const { arena, presentation: p } = s.getSnapshot();
  assert.deepEqual(
    standings(arena).map((c) => [c.competitor_id, c.bankroll]),
    [
      ['gemini', 18.48],
      ['gpt', 15],
      ['claude', 13],
    ],
  );
  assert.equal(arena.competitors.gpt.status, 'PASS');
  assert.equal(arena.competitors.gemini.ticket?.status, 'WIN');
  assert.equal(arena.competitors.claude.ticket?.status, 'LOSS');
  assert.equal(p.demo.finished, true);
  assert.equal(p.camera_target, 'MASTER');
});
test('presentation states do not decide money or outcomes', () => {
  const s = createArenaStore();
  for (const status of STATUSES) {
    s.dispatch({ type: 'UPDATE_COMPETITOR', competitor_id: 'gpt', patch: { status } });
    assert.equal(s.getSnapshot().arena.competitors.gpt.bankroll, 15);
    assert.equal(s.getSnapshot().arena.competitors.gpt.result, null);
  }
});
test('confidence and risk are independent supplied metrics', () => {
  const s = createArenaStore();
  s.dispatch({
    type: 'UPDATE_COMPETITOR',
    competitor_id: 'gemini',
    patch: { confidence: 9.1, bankroll_risk_pct: 26.7, ticket: MOCK_TICKETS.gemini },
  });
  s.dispatch({ type: 'UPDATE_COMPETITOR', competitor_id: 'gemini', patch: { confidence: 2 } });
  assert.equal(s.getSnapshot().arena.competitors.gemini.bankroll_risk_pct, 26.7);
  assert.equal(s.getSnapshot().arena.competitors.gemini.ticket?.stake, 4);
});
test('external snapshots replace mock source without mutation or automatic replay', () => {
  const s = createArenaStore(),
    api = createArenaBoundary(s);
  s.play();
  s.advance(35);
  const state = createMockState();
  state.competitors.claude.bankroll = 19.4;
  api.replaceState(state);
  state.competitors.claude.bankroll = 0;
  assert.equal(api.getState().competitors.claude.bankroll, 19.4);
  assert.equal(s.getSnapshot().presentation.demo.playing, false);
  const copy = api.getState();
  copy.competitors.gpt.bankroll = 999;
  assert.equal(api.getState().competitors.gpt.bankroll, 15);
});
test('invalid inputs are rejected atomically', () => {
  const s = createArenaStore(),
    before = s.getSnapshot().arena;
  for (const patch of [
    { bankroll: NaN },
    { confidence: 12 },
    { target_value: 0 },
    { bankroll_risk_pct: 101 },
    { ticket: { ...MOCK_TICKETS.gpt, stake: Infinity } },
  ]) {
    assert.throws(() => s.dispatch({ type: 'UPDATE_COMPETITOR', competitor_id: 'gpt', patch }));
    assert.deepEqual(s.getSnapshot().arena, before);
  }
});
test('reset clears event queue, cameras and old scripted cues', () => {
  const s = createArenaStore();
  s.play();
  s.advance(30);
  s.dispatch({ type: 'BROADCAST', event: { type: 'BANKRUPTCY', competitor_id: 'claude' } });
  s.dispatch({ type: 'RESET' });
  s.advance(90);
  assert.equal(s.getSnapshot().arena.competitors.gemini.status, 'IDLE');
  assert.equal(s.getSnapshot().presentation.active_event, null);
  assert.equal(s.getSnapshot().presentation.event_queue.length, 0);
  assert.equal(s.getSnapshot().presentation.camera_target, 'MASTER');
});
test('camera override survives alerts with director disabled', () => {
  const s = createArenaStore();
  s.setDirector(false);
  s.dispatch({ type: 'CAMERA', camera: 'CENTER_FLOOR' });
  s.dispatch({ type: 'BROADCAST', event: { type: 'POUNCE', competitor_id: 'gemini' } });
  s.advance(6);
  assert.equal(s.getSnapshot().presentation.camera_target, 'CENTER_FLOOR');
});
test('temporary overlays queue and expire without replacing competitor state', () => {
  const s = createArenaStore();
  s.dispatch({ type: 'BROADCAST', event: { type: 'UNANIMOUS', duration: 2 } });
  s.dispatch({
    type: 'BROADCAST',
    event: { type: 'HEAD_TO_HEAD', participants: ['gpt', 'claude'], duration: 2 },
  });
  s.advance(2.1);
  assert.equal(s.getSnapshot().presentation.active_event?.type, 'HEAD_TO_HEAD');
  s.advance(2.1);
  assert.equal(s.getSnapshot().presentation.active_event, null);
  assert.equal(s.getSnapshot().arena.competitors.gpt.status, 'IDLE');
});
test('boundary subscribers observe data changes only and unsubscribe', () => {
  const s = createArenaStore(),
    api = createArenaBoundary(s);
  let calls = 0;
  const stop = api.subscribe(() => calls++);
  s.advance(1);
  assert.equal(calls, 0);
  api.dispatch({ type: 'UPDATE_COMPETITOR', competitor_id: 'gpt', patch: { bankroll: 17 } });
  assert.equal(calls, 1);
  stop();
  api.dispatch({ type: 'RESET' });
  assert.equal(calls, 1);
});
test('pause freezes the mock week and broadcast event clock', () => {
  const s = createArenaStore();
  s.play();
  for (let i = 0; i < 29; i++) s.advance(1);
  s.pause();
  const before = s.getSnapshot();
  s.advance(10);
  assert.equal(s.getSnapshot().presentation.clock, before.presentation.clock);
  assert.equal(s.getSnapshot().presentation.demo.elapsed, before.presentation.demo.elapsed);
});
test('external takeover clears pending demo choreography', () => {
  const s = createArenaStore(),
    api = createArenaBoundary(s);
  s.play();
  s.advance(35);
  api.dispatch({ type: 'UPDATE_COMPETITOR', competitor_id: 'gpt', patch: { status: 'BUSTED' } });
  assert.equal(s.getSnapshot().presentation.active_event?.type, 'BANKRUPTCY');
  assert.equal(s.getSnapshot().presentation.event_queue.length, 0);
  s.advance(5);
  assert.equal(s.getSnapshot().presentation.active_event, null);
});
test('4x playback scales choreography and returns to master without trailing alerts', () => {
  const s = createArenaStore();
  s.setSpeed(4);
  s.play();
  for (let i = 0; i < 300; i++) s.advance(0.1);
  const p = s.getSnapshot().presentation;
  assert.equal(p.demo.finished, true);
  assert.equal(p.active_event, null);
  assert.equal(p.camera_target, 'MASTER');
});
