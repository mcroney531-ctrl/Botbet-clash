'use client';
import { useFrame } from '@react-three/fiber';
import { useRef } from 'react';
import { MeshStandardMaterial, MathUtils } from 'three';
import { useArenaStore } from '../state/context';
import { CompetitorId, Status } from '../state/types';
import { COLORS } from '../scene/theme';
const levels: Record<Status, number> = {
  IDLE: 0.5,
  RESEARCHING: 0.8,
  WATCHING: 0.92,
  STRONG: 1.05,
  POUNCE: 1.6,
  BET_EXECUTED: 1,
  LIVE: 1,
  WIN: 1.4,
  LOSS: 0.45,
  PASS: 0.8,
  BUSTED: 0.04,
};
export function AccentMaterial({ id }: { id: CompetitorId }) {
  const material = useRef<MeshStandardMaterial>(null);
  const store = useArenaStore();
  useFrame((_, dt) => {
    const { arena, presentation: p } = store.getSnapshot();
    const c = arena.competitors[id];
    let target = levels[c.status];
    const age = p.clock - p.changed_at[id];
    if (!p.reduced_motion && c.status === 'POUNCE') target += 0.25 * Math.sin(age * 9);
    if (!p.reduced_motion && c.status === 'WIN' && age < 2) target += 0.5 * Math.sin(age * 8);
    if (c.status === 'LOSS' && age > 2) target = 0.75;
    if (material.current)
      material.current.emissiveIntensity = MathUtils.damp(
        material.current.emissiveIntensity,
        target,
        8,
        dt,
      );
  });
  return (
    <meshStandardMaterial
      ref={material}
      color={COLORS[id]}
      emissive={COLORS[id]}
      emissiveIntensity={0.8}
      toneMapped={false}
    />
  );
}
export function Lighting() {
  return (
    <>
      <ambientLight intensity={1.1} color="#b9cce9" />
      <hemisphereLight args={['#d5e4ff', '#9d9ca9', 1.7]} />
      <directionalLight
        position={[-5, 10, 8]}
        intensity={2.5}
        color="#ffe6ca"
        castShadow
        shadow-mapSize={[2048, 2048]}
        shadow-camera-left={-11}
        shadow-camera-right={11}
        shadow-camera-top={10}
        shadow-camera-bottom={-10}
        shadow-normalBias={0.05}
      />
      <directionalLight position={[7, 7, 1]} intensity={1.8} color="#bed8ff" />
      <pointLight position={[0, 6, -1]} intensity={35} distance={16} color="#ffd3a0" />
      <pointLight position={[-7, 4, 3]} intensity={22} distance={12} color="#ffca8a" />
      <pointLight position={[7, 4, 3]} intensity={22} distance={12} color="#ffca8a" />
    </>
  );
}
