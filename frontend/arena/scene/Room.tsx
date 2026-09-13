'use client';
import { useMemo } from 'react';
import { Shape } from 'three';
import { Box, Stroke, Surface, label } from './primitives';
import { Stadium } from './Stadium';
import { WARM } from './theme';
import { useArenaData } from '../state/context';
function Playbook({ x }: { x: number }) {
  return (
    <group position={[x, 3.55, -6.47]}>
      <Box size={[2.15, 3.35, 0.18]} color="#1d2e46" radius={0.2} />
      <Surface
        width={1.9}
        height={2.95}
        position={[0, 0, 0.101]}
        contentKey="playbook"
        paint={(c, w, h) => {
          c.strokeStyle = '#a9b4c5';
          c.lineWidth = 6;
          c.globalAlpha = 0.55;
          [
            [180, 100],
            [490, 290],
            [770, 160],
            [690, 390],
          ].forEach(([x, y]) => {
            c.beginPath();
            c.moveTo(x - 23, y - 23);
            c.lineTo(x + 23, y + 23);
            c.moveTo(x + 23, y - 23);
            c.lineTo(x - 23, y + 23);
            c.stroke();
          });
          [
            [240, 410],
            [400, 180],
            [860, 440],
          ].forEach(([x, y]) => {
            c.beginPath();
            c.ellipse(x, y, 25, 30, 0, 0, 7);
            c.stroke();
          });
          c.beginPath();
          c.moveTo(260, 360);
          c.bezierCurveTo(100, 180, 700, 280, 730, 65);
          c.lineTo(690, 92);
          c.moveTo(730, 65);
          c.lineTo(765, 99);
          c.stroke();
          c.globalAlpha = 1;
        }}
      />
    </group>
  );
}
export function Room() {
  const arena = useArenaData();
  const floorShape = useMemo(() => {
    const s = new Shape();
    s.moveTo(-3.6, 0);
    s.quadraticCurveTo(0, 2.1, 3.6, 0);
    s.quadraticCurveTo(0, -2.1, -3.6, 0);
    return s;
  }, []);
  const football = useMemo(() => {
    const pts: [number, number, number][] = [];
    for (let i = 0; i <= 100; i++) {
      const t = (i / 100) * Math.PI * 2;
      pts.push([3.6 * Math.cos(t), 0.025, 1.5 + 1.3 * Math.sin(t) * Math.abs(Math.sin(t)) ** 0.25]);
    }
    return pts;
  }, []);
  return (
    <group name="room-architecture">
      <Stadium />
      <mesh rotation={[-Math.PI / 2, 0, 0]} receiveShadow>
        <circleGeometry args={[13, 96]} />
        <meshStandardMaterial color="#414958" metalness={0.22} roughness={0.45} />
      </mesh>
      <mesh position={[0, 0.006, 1.5]} rotation={[-Math.PI / 2, 0, 0]}>
        <shapeGeometry args={[floorShape]} />
        <meshStandardMaterial color="#303947" metalness={0.2} roughness={0.5} />
      </mesh>
      <Stroke points={football} color={WARM} width={3} />
      <Box size={[2.1, 0.024, 0.035]} position={[0, 0.025, 1.5]} color="#b7a891" radius={0.01} />
      {[-0.8, -0.4, 0, 0.4, 0.8].map((x) => (
        <Box
          key={x}
          size={[0.12, 0.022, 0.42]}
          position={[x, 0.03, 1.5]}
          color="#d7bc98"
          radius={0.01}
        />
      ))}
      {Array.from({ length: 12 }, (_, i) => {
        const a = (i * Math.PI) / 6;
        return (
          <Stroke
            key={i}
            points={[
              [Math.sin(a) * 4, 0.012, 1.5 + Math.cos(a) * 2],
              [Math.sin(a) * 12, 0.012, 1.5 + Math.cos(a) * 10],
            ]}
            color="#262e3a"
            width={1}
          />
        );
      })}
      <Box
        name="rear-wall-base"
        size={[21, 0.8, 0.7]}
        position={[0, 0.4, -6.8]}
        color="#3a4050"
        radius={0.2}
      />
      <Box size={[21, 0.055, 0.13]} position={[0, 0.87, -6.35]} color={WARM} glow={1} />
      <Box size={[21, 1, 0.7]} position={[0, 6.2, -6.8]} color="#454653" radius={0.18} />
      <Box size={[21, 0.06, 0.16]} position={[0, 5.74, -6.35]} color={WARM} glow={1.3} />
      {[-10, -6.65, -2.4, 2.4, 6.65, 10].map((x) => (
        <group key={x} position={[x, 0, -6.5]}>
          <Box size={[0.55, 5.3, 0.6]} position={[0, 3.1, 0]} color="#585461" radius={0.15} />
          <Box
            size={[0.07, 1.0, 0.05]}
            position={[0, 3.6, 0.33]}
            color={WARM}
            glow={1.6}
            radius={0.03}
          />
        </group>
      ))}
      {[-4.5, 0, 4.5].map((x) => (
        <group key={x} name={`stadium-window-${x}`}>
          <Box size={[3.85, 0.12, 0.12]} position={[x, 1.25, -6.4]} color="#6a6262" />
          <Box size={[3.85, 0.12, 0.12]} position={[x, 5.48, -6.4]} color="#6a6262" />
        </group>
      ))}
      <Playbook x={-8.25} />
      <Playbook x={8.25} />
      <Surface
        contentKey={`${arena.week}-${arena.phase}`}
        width={17.5}
        height={0.47}
        pixels={[2048, 96]}
        position={[0, 6.18, -6.42]}
        paint={(c, w, h) => {
          label(
            c,
            `WEEK ${String(arena.week).padStart(2, '0')} · ${arena.phase.replaceAll('_', ' ')}                         WEEK ${String(arena.week).padStart(2, '0')} · ${arena.phase.replaceAll('_', ' ')}`,
            w / 2,
            h / 2,
            31,
            '#eed9bd',
          );
        }}
      />
      <Box size={[22, 0.18, 15]} position={[0, 7.1, -2]} color="#41424e" />
      {[-7, -3, 3, 7].map((x) => (
        <group key={x}>
          <Box size={[1.8, 0.09, 0.38]} position={[x, 6.95, -0.5]} color="#ffe3bd" glow={1.3} />
          <Box size={[1.1, 0.09, 0.38]} position={[x, 6.95, -4.8]} color="#ffe3bd" glow={1.3} />
        </group>
      ))}
      {[-1, 1].map((side) => (
        <group key={side} position={[side * 9, 0, 0]} rotation={[0, side * -0.12, 0]}>
          <Box size={[0.3, 5.9, 12]} position={[0, 3, -0.7]} color="#303b50" radius={0.13} />
          <Box
            size={[0.05, 0.055, 11]}
            position={[-side * 0.17, 5.7, -0.7]}
            color={WARM}
            glow={1}
          />
        </group>
      ))}
      {[-1, 1].map((side) => (
        <group key={side} position={[side * 7.1, 0, 5.2]} rotation={[0, side * -0.23, 0]}>
          <Box size={[4, 0.17, 0.18]} position={[0, 0.9, 0]} color="#303b4e" radius={0.07} />
          <Box size={[3.8, 0.025, 0.03]} position={[0, 0.88, 0.1]} color={WARM} glow={0.8} />
          {[-1.5, 1.5].map((x) => (
            <Box key={x} size={[0.1, 0.9, 0.1]} position={[x, 0.43, 0]} color="#424b5a" />
          ))}
        </group>
      ))}
    </group>
  );
}
