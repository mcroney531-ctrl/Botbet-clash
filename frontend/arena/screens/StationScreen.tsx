'use client';
import { useArena } from '../state/context';
import { CompetitorId } from '../state/types';
import { COLORS, money, odds } from '../scene/theme';
import { FittedLabel, label, Meter, Surface } from '../scene/primitives';
export function StationScreen({ id }: { id: CompetitorId }) {
  const { arena, presentation: p } = useArena(),
    c = arena.competitors[id],
    t = c.ticket,
    color = COLORS[id];
  const elapsed = p.clock - p.changed_at[id],
    mix = p.reduced_motion ? 1 : Math.min(1, elapsed / 1.2);
  const cash = money(p.previous_bankroll[id] + (c.bankroll - p.previous_bankroll[id]) * mix);
  return (
    <Surface
      name={`${id}-dynamic-screen`}
      contentKey={JSON.stringify([c, cash])}
      width={2.66}
      height={1.12}
      pixels={[1064, 448]}
      position={[0, 0.98, 0.927]}
      paint={(ctx, w, h) => {
        ctx.fillStyle = c.status === 'BUSTED' ? '#080e15' : '#0c1824';
        ctx.fillRect(0, 0, w, h);
        const gradient = ctx.createLinearGradient(0, 0, w, h);
        gradient.addColorStop(0, '#ffffff0b');
        gradient.addColorStop(1, '#ffffff00');
        ctx.fillStyle = gradient;
        ctx.fillRect(0, 0, w, h);
        if (c.status === 'BUSTED') {
          label(ctx, c.display_name, w / 2, 55, 44, '#73828f');
          label(ctx, 'BUSTED', w / 2, 166, 80, '#b2bdc7');
          label(ctx, 'REAL-MONEY COMPETITION ENDED', w / 2, 269, 30, '#7e8c99');
          label(ctx, cash, w / 2, 359, 38, '#73828f');
          return;
        }
        const simple = ['IDLE', 'RESEARCHING', 'WATCHING', 'PASS'].includes(c.status);
        if (simple) {
          label(ctx, c.display_name, w / 2, 77, 98);
          label(ctx, c.status === 'IDLE' ? 'READY' : c.status, w / 2, 181, 72, color);
          if (c.status === 'WATCHING')
            label(ctx, `${c.market_count} MARKETS`, w / 2, 258, 28, '#acbecf');
          if (c.status === 'PASS') label(ctx, 'NO QUALIFYING EDGE', w / 2, 258, 30, '#acbecf');
          label(ctx, cash, w / 2, 353, 79);
          return;
        }
        label(ctx, c.display_name, 44, 49, 52, '#eaf3fc', 'left');
        label(
          ctx,
          c.status === 'STRONG' ? 'STRONG LEAN' : c.status,
          w - 44,
          49,
          48,
          color,
          'right',
        );
        ctx.fillStyle = '#304354';
        ctx.fillRect(44, 84, w - 88, 2);
        if (c.status === 'WIN' || c.status === 'LOSS') {
          label(
            ctx,
            `${c.bankroll_delta >= 0 ? '+' : '−'}${money(Math.abs(c.bankroll_delta))}`,
            w / 2,
            176,
            94,
            c.status === 'WIN' ? color : '#c6b3a9',
          );
          label(ctx, `BANKROLL   ${cash}`, w / 2, 302, 50);
          label(ctx, c.result ?? c.status, w / 2, 389, 26, '#99b1c5');
          return;
        }
        if (c.status === 'LIVE') {
          label(ctx, `${c.live_value ?? '—'} / ${c.target_value ?? '—'} ${c.unit}`, w / 2, 183, 78);
          Meter(
            ctx,
            70,
            261,
            w - 140,
            15,
            c.live_value !== null && c.target_value ? c.live_value / c.target_value : 0,
            color,
          );
          label(
            ctx,
            t ? `${t.side} ${t.line} · ${t.player}` : 'AWAITING LIVE VALUES',
            w / 2,
            324,
            31,
            '#b6c7d9',
          );
          label(ctx, `BANKROLL ${cash}`, w / 2, 392, 28, '#8298ac');
          return;
        }
        if (!t) {
          label(ctx, 'AWAITING PROP', w / 2, 200, 48);
          label(ctx, cash, w / 2, 340, 42);
          return;
        }
        FittedLabel(ctx, t.player, w / 2, 123, 29, w - 100, '#afc2d4');
        label(ctx, `${t.side} ${t.line}`, w / 2, 194, 70);
        label(
          ctx,
          c.status === 'BET_EXECUTED'
            ? `${odds(t.odds)}   ·   STAKE ${money(t.stake)}`
            : `${c.status === 'POUNCE' ? 'MAX STAKE' : 'STAKE'} ${money(t.stake)}`,
          w / 2,
          262,
          34,
          color,
        );
        label(ctx, 'CONFIDENCE', 255, 318, 23, '#b8cadb');
        label(ctx, 'BANKROLL RISK', w - 255, 318, 23, '#b8cadb');
        label(ctx, `${c.confidence ?? '—'} / 10`, 255, 357, 38);
        label(ctx, `${c.bankroll_risk_pct ?? '—'}%`, w - 255, 357, 38);
        Meter(ctx, 60, 392, 390, 10, (c.confidence ?? 0) / 10, color, true);
        Meter(ctx, w - 450, 392, 390, 10, (c.bankroll_risk_pct ?? 0) / 100, color);
        label(ctx, `BANKROLL ${cash}`, w / 2, 429, 20, '#8298ac');
      }}
    />
  );
}
