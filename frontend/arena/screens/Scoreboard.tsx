'use client';
import { useRef } from 'react';
import { Group, MathUtils } from 'three';
import { useFrame } from '@react-three/fiber';
import { useArena, useArenaStore } from '../state/context';
import { standings } from '../state/store';
import { CompetitorId } from '../state/types';
import { Box, FittedLabel, Surface, label } from '../scene/primitives';
import { COLORS, money, WARM } from '../scene/theme';
function Row({ id, rank }: { id: CompetitorId; rank: number }) {
  const { arena, presentation: p } = useArena(),
    c = arena.competitors[id],
    group = useRef<Group>(null),
    store = useArenaStore();
  const initialY = useRef(-0.02 - rank * 0.4);
  const mix = p.reduced_motion ? 1 : Math.min(1, (p.clock - p.changed_at[id]) / 1.2),
    cash = money(p.previous_bankroll[id] + (c.bankroll - p.previous_bankroll[id]) * mix);
  useFrame((_, dt) => {
    if (group.current)
      group.current.position.y = store.getSnapshot().presentation.reduced_motion
        ? -0.02 - rank * 0.4
        : MathUtils.damp(group.current.position.y, -0.02 - rank * 0.4, 5, dt);
  });
  return (
    <group ref={group} position={[0, initialY.current, 0.34]} name={`leaderboard-row-${id}`}>
      <Surface
        contentKey={`${c.display_name}-${cash}-${rank}`}
        width={3.75}
        height={0.34}
        pixels={[1024, 110]}
        paint={(ctx, w, h) => {
          ctx.fillStyle = '#152635';
          ctx.fillRect(0, 0, w, h);
          ctx.fillStyle = COLORS[id];
          ctx.fillRect(14, 13, 9, h - 26);
          label(ctx, `${rank + 1}`, 60, h / 2, 27, '#8097aa', 'left');
          label(ctx, c.display_name, 140, h / 2, 40, '#eff6fc', 'left');
          label(ctx, cash, w - 35, h / 2, 41, '#eff6fc', 'right');
        }}
      />
    </group>
  );
}
export function Scoreboard() {
  const { arena, presentation: p } = useArena(),
    event = p.active_event;
  const c = event?.competitor_id ? arena.competitors[event.competitor_id] : null;
  let title = event?.title ?? event?.type.replaceAll('_', ' ') ?? '',
    lines = event?.lines;
  if (event && !lines) {
    switch (event.type) {
      case 'POUNCE':
        lines = [
          c?.display_name ?? '',
          c?.ticket ? `${c.ticket.side} ${c.ticket.line}` : 'MARKET ALERT',
        ];
        break;
      case 'BET_LOCKED':
        lines = [c?.display_name ?? '', money(c?.ticket?.stake ?? 0)];
        break;
      case 'WIN':
      case 'LOSS':
        lines = [
          c?.display_name ?? '',
          `${(c?.bankroll_delta ?? 0) >= 0 ? '+' : '−'}${money(Math.abs(c?.bankroll_delta ?? 0))}`,
        ];
        break;
      case 'LEAD_CHANGE':
        title = 'NEW LEADER';
        lines = [c?.display_name ?? '', money(c?.bankroll ?? 0)];
        break;
      case 'PASS':
        lines = [c?.display_name ?? ''];
        break;
      case 'BANKRUPTCY':
        title = 'BUSTED';
        lines = [c?.display_name ?? '', 'REAL-MONEY PLAY ENDED'];
        break;
      default:
        lines = [];
    }
  }
  return (
    <group name="central-scoreboard" position={[0, 5.05, -3.1]}>
      {[-1.6, 1.6].map((x) => (
        <Box key={x} size={[0.035, 1.4, 0.035]} position={[x, 1.15, 0]} color="#7d8694" />
      ))}
      <Box size={[4.7, 2.35, 0.6]} color="#475062" radius={0.28} roughness={0.35} />
      <Box size={[4.38, 2.05, 0.08]} position={[0, 0, 0.285]} color="#101b28" radius={0.18} />
      <Box size={[3.95, 0.03, 0.09]} position={[0, -1.12, 0.18]} color={WARM} glow={1} />
      <Surface
        contentKey={`${arena.week}`}
        width={4.02}
        height={0.64}
        pixels={[1200, 200]}
        position={[0, 0.72, 0.337]}
        paint={(ctx, w, h) => {
          ctx.fillStyle = '#101b28';
          ctx.fillRect(0, 0, w, h);
          label(ctx, 'BOTBET CLASH', w / 2, 58, 63);
          label(ctx, `WEEK ${String(arena.week).padStart(2, '0')}`, w / 2, 151, 35, '#9ebacf');
        }}
      />
      <group visible={!event}>
        {standings(arena).map((entry, rank) => (
          <Row key={entry.competitor_id} id={entry.competitor_id} rank={rank} />
        ))}
      </group>
      {event && (
        <Surface
          contentKey={JSON.stringify([event, title, lines, c?.bankroll])}
          width={3.98}
          height={1.3}
          position={[0, -0.34, 0.34]}
          pixels={[1024, 370]}
          paint={(ctx, w, h) => {
            ctx.fillStyle = '#0c1c2c';
            ctx.fillRect(0, 0, w, h);
            FittedLabel(ctx, title, w / 2, 55, 48, w - 80, c ? COLORS[c.competitor_id] : '#ffd29b');
            (lines ?? []).forEach((line, i) =>
              FittedLabel(
                ctx,
                line,
                w / 2,
                132 + i * (210 / Math.max(lines!.length, 2)),
                lines!.length > 3 ? 30 : 46,
                w - 75,
              ),
            );
          }}
        />
      )}
    </group>
  );
}
