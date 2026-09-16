'use client';
import { RoundedBox } from '@react-three/drei';
import { useFrame } from '@react-three/fiber';
import { useRef } from 'react';
import { Mesh } from 'three';
import { CompetitorId } from '../state/types';
import { Box, SmallScreen, Surface } from '../scene/primitives';
import { COLORS, PLACEMENT } from '../scene/theme';
import { Robot } from '../robots/Robot';
import { StationScreen } from '../screens/StationScreen';
import { TicketDock } from '../tickets/TicketDock';
import { AccentMaterial } from '../lighting/Lighting';
import { useArenaStore } from '../state/context';
function AlertTrace({ id }: { id: CompetitorId }) {
  const ref = useRef<Mesh>(null),
    s = useArenaStore();
  useFrame(() => {
    if (!ref.current) return;
    const { arena, presentation: p } = s.getSnapshot();
    const age = p.clock - p.changed_at[id];
    ref.current.visible =
      !p.reduced_motion &&
      (arena.competitors[id].status === 'POUNCE' ||
        (arena.competitors[id].status === 'WIN' && age < 2));
    ref.current.position.x = -1.5 + ((age * 0.9) % 1) * 3;
  });
  return (
    <mesh ref={ref} position={[0, 1.58, 0.9]}>
      <boxGeometry args={[0.28, 0.035, 0.025]} />
      <meshBasicMaterial color={COLORS[id]} toneMapped={false} />
    </mesh>
  );
}
export function Station({ id }: { id: CompetitorId }) {
  const { position, rotation } = PLACEMENT[id];
  return (
    <group name={`station-${id}`} position={position} rotation={[0, rotation, 0]}>
      <Box
        name={`${id}-console-shell`}
        size={[3.65, 1.65, 1.8]}
        position={[0, 0.86, 0]}
        color="#434d61"
        radius={0.28}
        roughness={0.35}
        metalness={0.22}
      />
      <Box size={[3.2, 0.075, 1.52]} position={[0, 1.71, 0]} color="#17222e" radius={0.035} />
      <Box size={[2.95, 1.3, 0.09]} position={[0, 0.98, 0.87]} color="#17202c" radius={0.16} />
      <StationScreen id={id} />
      {[-1, 1].map((side) => (
        <RoundedBox
          key={side}
          args={[0.095, 1.15, 0.055]}
          radius={0.04}
          smoothness={3}
          position={[side * 1.57, 0.98, 0.945]}
        >
          <AccentMaterial id={id} />
        </RoundedBox>
      ))}
      <RoundedBox args={[2.8, 0.026, 0.05]} radius={0.012} position={[0, 0.11, 0.75]}>
        <AccentMaterial id={id} />
      </RoundedBox>
      <Robot id={id} />
      <TicketDock id={id} />
      <SmallScreen position={[id === 'claude' ? -0.95 : 0.95, 1.86, -0.15]}>
        <Surface
          width={0.53}
          height={0.25}
          position={[0, 0, 0.038]}
          contentKey={id}
          pixels={[320, 160]}
          paint={(c, w, h) => {
            c.fillStyle = '#0c1d2b';
            c.fillRect(0, 0, w, h);
            c.fillStyle = COLORS[id];
            [0, 1, 2, 3, 4, 5, 6].forEach((i) =>
              c.fillRect(30 + i * 37, 120 - ((i * 29) % 80), 19, 20 + ((i * 29) % 80)),
            );
          }}
        />
      </SmallScreen>
      <Box size={[1.08, 0.025, 0.4]} position={[0, 1.762, 0.4]} color="#314555" radius={0.01} />
      <AlertTrace id={id} />
    </group>
  );
}
