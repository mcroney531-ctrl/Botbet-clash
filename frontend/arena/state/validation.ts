import { ArenaState, BroadcastEvent, BOARD_EVENTS, IDS, STATUSES, TICKET_STATES } from './types';
function requireValue(ok: unknown, message: string): asserts ok {
  if (!ok) throw new TypeError(message);
}
const finite = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n);
const text = (s: unknown) => typeof s === 'string' && s.length <= 160;
export function validateState(s: ArenaState): void {
  requireValue(s && typeof s === 'object', 'Expected ArenaState');
  requireValue(Number.isInteger(s.week) && s.week >= 0, 'week must be a nonnegative integer');
  requireValue(text(s.phase), 'Invalid phase');
  for (const id of IDS) {
    const c = s.competitors?.[id];
    requireValue(c && c.competitor_id === id, `Missing competitor ${id}`);
    requireValue(text(c.display_name) && c.display_name.length > 0, 'Invalid display_name');
    requireValue(STATUSES.includes(c.status), 'Unknown status');
    requireValue(
      finite(c.bankroll) && c.bankroll >= 0 && finite(c.bankroll_delta),
      'Invalid supplied bankroll',
    );
    requireValue(
      c.confidence === null || (finite(c.confidence) && c.confidence >= 0 && c.confidence <= 10),
      'confidence must be null or 0–10',
    );
    requireValue(
      c.bankroll_risk_pct === null ||
        (finite(c.bankroll_risk_pct) && c.bankroll_risk_pct >= 0 && c.bankroll_risk_pct <= 100),
      'risk must be null or 0–100',
    );
    requireValue(Number.isInteger(c.market_count) && c.market_count >= 0, 'Invalid market_count');
    requireValue(
      c.live_value === null || (finite(c.live_value) && c.live_value >= 0),
      'Invalid live_value',
    );
    requireValue(
      c.target_value === null || (finite(c.target_value) && c.target_value > 0),
      'Invalid target_value',
    );
    requireValue(
      text(c.unit) &&
        (c.uncertainty === null || text(c.uncertainty)) &&
        (c.result === null || text(c.result)),
      'Invalid text field',
    );
    if (c.ticket) {
      const t = c.ticket;
      requireValue(text(t.player) && text(t.prop_type), 'Invalid ticket text');
      requireValue(t.side === 'OVER' || t.side === 'UNDER', 'Unknown ticket side');
      requireValue(
        finite(t.line) && finite(t.odds) && finite(t.stake) && t.stake >= 0,
        'Invalid supplied ticket numbers',
      );
      requireValue(TICKET_STATES.includes(t.status), 'Unknown ticket status');
    }
  }
}
export function validateEvent(e: BroadcastEvent): void {
  requireValue(e && BOARD_EVENTS.includes(e.type), 'Unknown board event');
  requireValue(
    e.competitor_id === undefined || IDS.includes(e.competitor_id),
    'Unknown competitor',
  );
  requireValue(
    e.duration === undefined || (finite(e.duration) && e.duration > 0 && e.duration <= 30),
    'duration must be 0–30 seconds',
  );
  requireValue(e.title === undefined || text(e.title), 'Invalid event title');
  requireValue(
    e.lines === undefined || (Array.isArray(e.lines) && e.lines.length <= 5 && e.lines.every(text)),
    'Invalid event lines',
  );
  requireValue(
    e.participants === undefined ||
      (Array.isArray(e.participants) &&
        e.participants.length <= 3 &&
        e.participants.every((id) => IDS.includes(id))),
    'Invalid participants',
  );
}
