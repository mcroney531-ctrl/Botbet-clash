import { ArenaState, CompetitorId, CompetitorState, IDS, Ticket } from '../state/types';
export const MOCK_TICKETS: Record<CompetitorId, Ticket> = {
  gpt: {
    player: 'PLAYER X',
    prop_type: 'RECEIVING YARDS',
    side: 'OVER',
    line: 53.5,
    odds: -115,
    stake: 3,
    status: 'PENDING',
  },
  claude: {
    player: 'PLAYER Y',
    prop_type: 'RECEIVING YARDS',
    side: 'UNDER',
    line: 74.5,
    odds: -110,
    stake: 2,
    status: 'PENDING',
  },
  gemini: {
    player: 'PLAYER X',
    prop_type: 'RECEIVING YARDS',
    side: 'OVER',
    line: 53.5,
    odds: -115,
    stake: 4,
    status: 'PENDING',
  },
};
export function createMockState(): ArenaState {
  const competitors = Object.fromEntries(
    IDS.map((id) => [
      id,
      {
        competitor_id: id,
        display_name: id.toUpperCase(),
        status: 'IDLE',
        bankroll: 15,
        bankroll_delta: 0,
        confidence: null,
        bankroll_risk_pct: null,
        uncertainty: null,
        market_count: 0,
        live_value: null,
        target_value: null,
        unit: 'YDS',
        result: null,
        ticket: null,
      } satisfies CompetitorState,
    ]),
  ) as ArenaState['competitors'];
  return { week: 1, phase: 'REGULAR_SEASON', competitors };
}
