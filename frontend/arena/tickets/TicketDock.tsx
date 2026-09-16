'use client';
import { useRef } from 'react';
import { useFrame } from '@react-three/fiber';
import { Group, MathUtils } from 'three';
import { useArenaData, useArenaStore } from '../state/context';
import { CompetitorId } from '../state/types';
import { Box, label, Surface } from '../scene/primitives';
import { COLORS, money, odds } from '../scene/theme';
export function TicketDock({ id }: { id: CompetitorId }) {
  const arena = useArenaData(),
    c = arena.competitors[id],
    ticket = c.ticket;
  const card = useRef<Group>(null),
    store = useArenaStore();
  const show = !!ticket && !['BUSTED', 'PASS', 'IDLE', 'RESEARCHING'].includes(c.status);
  useFrame((_, dt) => {
    if (!card.current) return;
    const { arena: a, presentation: p } = store.getSnapshot(),
      s = a.competitors[id].status;
    const y = s === 'POUNCE' ? 0.42 : s === 'WATCHING' || s === 'STRONG' ? 0.18 : 0;
    const scale = s === 'POUNCE' ? 1.17 : 1;
    card.current.position.y = p.reduced_motion
      ? y
      : MathUtils.damp(card.current.position.y, y, 7, dt);
    card.current.scale.setScalar(
      p.reduced_motion ? scale : MathUtils.damp(card.current.scale.x, scale, 7, dt),
    );
  });
  return (
    <group name={`${id}-ticket-dock`} position={[id === 'claude' ? 1.3 : -1.3, 1.78, 0.32]}>
      <Box size={[0.65, 0.09, 0.35]} color="#949ca7" metalness={0.7} radius={0.03} />
      <Box
        size={[0.54, 0.025, 0.22]}
        position={[0, 0.058, 0]}
        color={c.status === 'BUSTED' ? '#344451' : COLORS[id]}
        glow={0.45}
      />
      <mesh position={[0, 0.42, -0.035]}>
        <boxGeometry args={[0.57, 0.8, 0.035]} />
        <meshPhysicalMaterial
          color="#d6ecff"
          transparent
          opacity={0.16}
          roughness={0.18}
          depthWrite={false}
        />
      </mesh>
      {show && (
        <group ref={card} name={`${id}-ticket-card`}>
          <Box
            size={[0.5, 0.76, 0.026]}
            position={[0, 0.43, 0.008]}
            color="#dce6ef"
            radius={0.025}
          />
          <Surface
            contentKey={JSON.stringify(ticket)}
            width={0.46}
            height={0.69}
            pixels={[384, 576]}
            position={[0, 0.44, 0.025]}
            paint={(ctx, w, h) => {
              ctx.fillStyle = '#e2eaf1';
              ctx.fillRect(0, 0, w, h);
              label(ctx, ticket!.status, w / 2, 55, 34, '#1c3a56');
              ctx.fillStyle = COLORS[id];
              ctx.fillRect(35, 92, w - 70, 5);
              label(ctx, ticket!.player, w / 2, 144, 25, '#344a5d');
              label(ctx, `${ticket!.side} ${ticket!.line}`, w / 2, 221, 37, '#12283a');
              label(ctx, odds(ticket!.odds), w / 2, 281, 29, '#354e63');
              label(ctx, money(ticket!.stake), w / 2, 354, 49, '#163047');
              if (['LOCKED', 'LIVE', 'WIN', 'LOSS'].includes(ticket!.status)) {
                ctx.strokeStyle = '#254663';
                ctx.lineWidth = 9;
                ctx.beginPath();
                ctx.arc(w / 2, 451, 20, Math.PI, 0);
                ctx.stroke();
                ctx.fillStyle = '#254663';
                ctx.fillRect(w / 2 - 28, 450, 56, 43);
                ctx.fillStyle = '#e2eaf1';
                ctx.fillRect(w / 2 - 4, 462, 8, 15);
              } else label(ctx, 'CANDIDATE', w / 2, 469, 23, '#536a7a');
            }}
          />
        </group>
      )}
    </group>
  );
}
