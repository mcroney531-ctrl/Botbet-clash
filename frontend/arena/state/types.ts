export const IDS = ['gpt', 'claude', 'gemini'] as const;
export type CompetitorId = (typeof IDS)[number];
export const STATUSES = [
  'IDLE',
  'RESEARCHING',
  'WATCHING',
  'STRONG',
  'POUNCE',
  'BET_EXECUTED',
  'PASS',
  'LIVE',
  'WIN',
  'LOSS',
  'BUSTED',
] as const;
export type Status = (typeof STATUSES)[number];
export const TICKET_STATES = [
  'WATCH',
  'PENDING',
  'POUNCE',
  'LOCKED',
  'LIVE',
  'WIN',
  'LOSS',
  'EXPIRED',
] as const;
export type TicketStatus = (typeof TICKET_STATES)[number];
export interface Ticket {
  player: string;
  prop_type: string;
  side: 'OVER' | 'UNDER';
  line: number;
  odds: number;
  stake: number;
  status: TicketStatus;
}
export interface CompetitorState {
  competitor_id: CompetitorId;
  display_name: string;
  status: Status;
  bankroll: number;
  bankroll_delta: number;
  confidence: number | null;
  bankroll_risk_pct: number | null;
  uncertainty: string | null;
  market_count: number;
  live_value: number | null;
  target_value: number | null;
  unit: string;
  result: string | null;
  ticket: Ticket | null;
}
export const CAMERAS = [
  'MASTER',
  'GPT_CLOSE',
  'CLAUDE_CLOSE',
  'GEMINI_CLOSE',
  'HEAD_TO_HEAD_LEFT_CENTER',
  'HEAD_TO_HEAD_CENTER_RIGHT',
  'SCOREBOARD',
  'CENTER_FLOOR',
] as const;
export type CameraPreset = (typeof CAMERAS)[number];
export const BOARD_EVENTS = [
  'WEEK_OPEN',
  'POUNCE',
  'BET_LOCKED',
  'PASS',
  'WIN',
  'LOSS',
  'LEAD_CHANGE',
  'UNANIMOUS',
  'HEAD_TO_HEAD',
  'BANKRUPTCY',
  'WEEK_RECAP',
] as const;
export type BoardEventType = (typeof BOARD_EVENTS)[number];
export interface BroadcastEvent {
  type: BoardEventType;
  competitor_id?: CompetitorId;
  title?: string;
  lines?: string[];
  participants?: CompetitorId[];
  duration?: number;
}
export interface ArenaState {
  week: number;
  phase: string;
  competitors: Record<CompetitorId, CompetitorState>;
}
export type ArenaInput =
  | { type: 'REPLACE_STATE'; state: ArenaState }
  | {
      type: 'UPDATE_COMPETITOR';
      competitor_id: CompetitorId;
      patch: Partial<Omit<CompetitorState, 'competitor_id'>>;
      announce?: boolean;
    }
  | { type: 'BROADCAST'; event: BroadcastEvent }
  | { type: 'CAMERA'; camera: CameraPreset }
  | { type: 'RESET' };
export interface PresentationState {
  clock: number;
  camera_target: CameraPreset;
  auto_director: boolean;
  reduced_motion: boolean;
  active_event:
    (BroadcastEvent & { started_at: number; expires_at: number; serial: number }) | null;
  event_queue: BroadcastEvent[];
  serial: number;
  changed_at: Record<CompetitorId, number>;
  previous_bankroll: Record<CompetitorId, number>;
  demo: { playing: boolean; elapsed: number; cursor: number; speed: number; finished: boolean };
}
export interface ArenaView {
  arena: ArenaState;
  presentation: PresentationState;
}
