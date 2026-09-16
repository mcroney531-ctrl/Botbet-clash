import { ArenaInput, CompetitorId, CompetitorState } from '../state/types';
import { MOCK_TICKETS } from './mock-state';
const update = (
  id: CompetitorId,
  patch: Partial<CompetitorState>,
  announce = true,
): ArenaInput => ({ type: 'UPDATE_COMPETITOR', competitor_id: id, patch, announce });
export interface DemoCue {
  at: number;
  label: string;
  inputs: ArenaInput[];
}
// Supplied fixtures, not calculated settlements, eligibility, exposure, or forecasts.
export const DEMO_DURATION = 110;
export const DEMO_CUES: DemoCue[] = [
  {
    at: 0,
    label: 'Week open',
    inputs: [
      ...(['gpt', 'claude', 'gemini'] as const).map((id) =>
        update(id, { status: 'RESEARCHING' }, false),
      ),
      {
        type: 'BROADCAST',
        event: { type: 'WEEK_OPEN', title: 'WEEK 01', lines: ['THE WEEK IS OPEN'], duration: 3 },
      },
    ],
  },
  {
    at: 8,
    label: 'Claude watches three markets',
    inputs: [
      update('claude', {
        status: 'WATCHING',
        market_count: 3,
        ticket: { ...MOCK_TICKETS.claude, status: 'WATCH' },
        confidence: 7.8,
        bankroll_risk_pct: 13.3,
        uncertainty: 'MEDIUM',
      }),
    ],
  },
  {
    at: 15,
    label: 'Gemini finds a candidate',
    inputs: [
      update('gemini', {
        status: 'WATCHING',
        market_count: 2,
        ticket: { ...MOCK_TICKETS.gemini, status: 'WATCH' },
        confidence: 8.8,
        bankroll_risk_pct: 26.7,
        uncertainty: 'MEDIUM',
      }),
    ],
  },
  {
    at: 22,
    label: 'Gemini has a strong lean',
    inputs: [
      update('gemini', {
        status: 'STRONG',
        ticket: { ...MOCK_TICKETS.gemini, status: 'PENDING' },
        confidence: 8.8,
      }),
    ],
  },
  {
    at: 28,
    label: 'Gemini Pounces',
    inputs: [
      update('gemini', {
        status: 'POUNCE',
        confidence: 9.1,
        ticket: { ...MOCK_TICKETS.gemini, status: 'POUNCE' },
      }),
    ],
  },
  {
    at: 34,
    label: 'Gemini locks $4.00',
    inputs: [
      update('gemini', {
        status: 'BET_EXECUTED',
        ticket: { ...MOCK_TICKETS.gemini, status: 'LOCKED' },
      }),
    ],
  },
  { at: 42, label: 'GPT passes', inputs: [update('gpt', { status: 'PASS', ticket: null })] },
  {
    at: 50,
    label: 'Claude locks $2.00',
    inputs: [
      update('claude', {
        status: 'BET_EXECUTED',
        ticket: { ...MOCK_TICKETS.claude, status: 'LOCKED' },
      }),
    ],
  },
  {
    at: 60,
    label: 'Game live',
    inputs: [
      update('gemini', {
        status: 'LIVE',
        live_value: 0,
        target_value: 54,
        ticket: { ...MOCK_TICKETS.gemini, status: 'LIVE' },
      }),
      update('claude', {
        status: 'LIVE',
        live_value: 0,
        target_value: 74,
        ticket: { ...MOCK_TICKETS.claude, status: 'LIVE' },
      }),
    ],
  },
  {
    at: 72,
    label: 'Gemini • 32 / 54 yards',
    inputs: [
      update('gemini', { live_value: 32 }, false),
      update('claude', { live_value: 42 }, false),
    ],
  },
  {
    at: 78,
    label: 'Gemini • 48 / 54 yards',
    inputs: [
      update('gemini', { live_value: 48 }, false),
      update('claude', { live_value: 67 }, false),
    ],
  },
  {
    at: 84,
    label: 'Gemini wins',
    inputs: [
      update('gemini', {
        status: 'WIN',
        live_value: 61,
        bankroll: 18.48,
        bankroll_delta: 3.48,
        result: 'WIN',
        ticket: { ...MOCK_TICKETS.gemini, status: 'WIN' },
      }),
    ],
  },
  {
    at: 92,
    label: 'Claude loses',
    inputs: [
      update('claude', {
        status: 'LOSS',
        live_value: 82,
        bankroll: 13,
        bankroll_delta: -2,
        result: 'LOSS',
        ticket: { ...MOCK_TICKETS.claude, status: 'LOSS' },
      }),
    ],
  },
  {
    at: 100,
    label: 'Week standings',
    inputs: [
      {
        type: 'BROADCAST',
        event: {
          type: 'WEEK_RECAP',
          title: 'WEEK 01 COMPLETE',
          lines: ['GEMINI LEADS THE ARENA'],
          duration: 5,
        },
      },
      { type: 'CAMERA', camera: 'SCOREBOARD' },
    ],
  },
  { at: 110, label: 'Return to master', inputs: [{ type: 'CAMERA', camera: 'MASTER' }] },
];
