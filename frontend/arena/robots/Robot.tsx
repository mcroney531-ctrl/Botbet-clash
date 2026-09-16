'use client';
import { useRef } from 'react';
import { useFrame } from '@react-three/fiber';
import { Group, MathUtils } from 'three';
import { useArenaData, useArenaStore } from '../state/context';
import { CompetitorId, Status } from '../state/types';
import { Box, Surface } from '../scene/primitives';
import { COLORS, PLACEMENT } from '../scene/theme';
function Joint({
  position,
  size = 0.18,
  color = '#222d39',
}: {
  position: [number, number, number];
  size?: number;
  color?: string;
}) {
  return (
    <mesh position={position} castShadow>
      <sphereGeometry args={[size, 20, 16]} />
      <meshStandardMaterial color={color} metalness={0.5} roughness={0.35} />
    </mesh>
  );
}
function Face({ id, status }: { id: CompetitorId; status: Status }) {
  const color = COLORS[id],
    sad = status === 'LOSS' || status === 'BUSTED',
    excited = ['POUNCE', 'WIN'].includes(status),
    thinking = ['WATCHING', 'STRONG', 'RESEARCHING'].includes(status);
  return (
    <Surface
      contentKey={`${id}-${status}`}
      width={id === 'gemini' ? 1.34 : 1.17}
      height={id === 'gemini' ? 1.34 : 0.85}
      pixels={[512, 512]}
      position={[0, 0, 0.51]}
      paint={(c, w, h) => {
        if (id === 'claude') {
          c.fillStyle = '#0d1822';
          c.beginPath();
          c.roundRect(60, 125, 158, 206, 35);
          c.roundRect(294, 125, 158, 206, 35);
          c.fill();
        }
        c.fillStyle = color;
        c.strokeStyle = color;
        c.lineWidth = 13;
        c.lineCap = 'round';
        const eyes = [172, 340];
        for (let i = 0; i < 2; i++) {
          let x = eyes[i];
          const y = id === 'claude' ? 231 : 215;
          if (status === 'WIN') {
            c.beginPath();
            c.arc(x, y, 35, Math.PI, 0);
            c.stroke();
          } else if (id === 'gpt' && status === 'IDLE' && i === 0) {
            c.beginPath();
            c.moveTo(x - 27, y);
            c.lineTo(x + 24, y + 8);
            c.stroke();
          } else {
            c.beginPath();
            c.ellipse(x, y, excited ? 39 : 33, sad ? 23 : thinking ? 36 : 44, 0, 0, Math.PI * 2);
            c.fill();
            c.fillStyle = '#e9ffff';
            c.beginPath();
            c.arc(x + 6, y - 10, 8, 0, 7);
            c.fill();
            c.fillStyle = color;
          }
          c.beginPath();
          c.moveTo(x - 30, y - 66 + (sad ? 15 : 0));
          c.quadraticCurveTo(x, y - 86, x + 28, y - 63 + (i === 0 && id === 'gpt' ? -17 : 0));
          c.stroke();
        }
        c.strokeStyle = id === 'claude' ? '#453323' : color;
        c.lineWidth = 10;
        c.beginPath();
        c.moveTo(221, 350);
        c.quadraticCurveTo(260, sad ? 322 : 385, 297, 345);
        c.stroke();
        if (excited) {
          c.fillStyle = color;
          c.beginPath();
          c.ellipse(259, 360, 28, 18, 0, 0, Math.PI);
          c.fill();
        }
      }}
    />
  );
}
function Hand() {
  return (
    <group>
      <Box size={[0.26, 0.25, 0.17]} radius={0.07} color="#ddd9d0" />
      {[-0.085, -0.025, 0.035, 0.095].map((x) => (
        <Box
          key={x}
          size={[0.045, 0.14, 0.065]}
          position={[x, -0.1, 0.07]}
          color="#9ca4a6"
          radius={0.02}
        />
      ))}
    </group>
  );
}
export function Robot({ id }: { id: CompetitorId }) {
  const body = useRef<Group>(null),
    head = useRef<Group>(null),
    left = useRef<Group>(null),
    right = useRef<Group>(null);
  const arena = useArenaData();
  const status = arena.competitors[id].status;
  const store = useArenaStore();
  const shell = id === 'claude' ? '#e4cdb0' : '#e0e5e6',
    trim = id === 'claude' ? '#ad7445' : '#344454',
    color = COLORS[id];
  useFrame((_, dt) => {
    const { arena: a, presentation: p } = store.getSnapshot(),
      s = a.competitors[id].status,
      age = p.clock - p.changed_at[id];
    const calm = p.reduced_motion ? 0 : 1,
      energy = id === 'gemini' ? 1.3 : id === 'claude' ? 1 : 0.65;
    let tilt = id === 'claude' ? 0.1 : id === 'gemini' ? -0.09 : 0,
      turn = 0,
      lean = 0,
      armL = 0,
      armR = 0;
    if (['RESEARCHING', 'WATCHING', 'STRONG'].includes(s)) {
      turn = Math.sin(p.clock * 0.65) * 0.13 * calm;
      tilt += 0.045 * calm * Math.sin(p.clock);
      lean = 0.09;
      armR = 0.06 * Math.sin(p.clock * 2.6) * calm;
    }
    if (s === 'WATCHING' && id === 'claude') {
      armR = -0.7;
      tilt = 0.17;
    }
    if (s === 'POUNCE') {
      lean = 0.12;
      armR = -0.9 * energy * (age < 1 ? Math.sin((Math.min(1, age) * Math.PI) / 2) : 1);
      tilt = -0.06;
    }
    if (s === 'WIN' && age < 3) {
      armR = -1.5 * energy;
      armL = 0.4 * energy;
      tilt = -0.1;
    }
    if (s === 'LOSS') {
      tilt = 0.2;
      lean = 0.12;
      armR = 0.15;
      if (age < 2) turn = Math.sin(age * 8) * 0.14 * calm;
    }
    if (s === 'PASS' && age < 2) {
      tilt = 0.08 * Math.sin(age * 3) * calm;
    }
    if (s === 'BUSTED') {
      tilt = 0.2;
      lean = 0.1;
    }
    const event = p.active_event;
    if (
      event &&
      event.competitor_id &&
      event.competitor_id !== id &&
      ['POUNCE', 'WIN'].includes(event.type) &&
      p.clock - event.started_at < 2
    )
      turn =
        Math.sign(PLACEMENT[event.competitor_id].position[0] - PLACEMENT[id].position[0]) * 0.3;
    if (event?.type === 'UNANIMOUS') {
      tilt = -0.15;
      turn = id === 'claude' ? -0.2 : id === 'gemini' ? 0.2 : 0;
    }
    if (event?.type === 'HEAD_TO_HEAD' && event.participants?.includes(id)) {
      turn =
        id === 'claude'
          ? 0.3
          : id === 'gemini'
            ? -0.3
            : event.participants.includes('claude')
              ? -0.3
              : 0.3;
    }
    if (head.current) {
      head.current.rotation.x = MathUtils.damp(head.current.rotation.x, tilt, 6, dt);
      head.current.rotation.y = MathUtils.damp(head.current.rotation.y, turn, 5, dt);
    }
    if (body.current) {
      body.current.position.y = Math.sin(p.clock * 1.6) * 0.015 * calm;
      body.current.rotation.x = MathUtils.damp(body.current.rotation.x, lean, 5, dt);
    }
    if (right.current)
      right.current.rotation.x = MathUtils.damp(right.current.rotation.x, armR, 7, dt);
    if (left.current)
      left.current.rotation.x = MathUtils.damp(left.current.rotation.x, armL, 7, dt);
  });
  return (
    <group name={`robot-${id}`} position={[0, 0, -0.34]}>
      <Box
        name={`${id}-chair`}
        size={[1.3, 1.65, 0.4]}
        position={[0, 1.85, -0.62]}
        color="#1d293a"
        radius={0.25}
      />
      <group ref={body} name={`${id}-body-rig`}>
        <Box
          size={[1.12, 0.97, 0.65]}
          position={[0, 2.03, 0]}
          color={shell}
          radius={0.26}
          roughness={0.32}
        />
        <Box size={[0.6, 0.54, 0.07]} position={[0, 2, 0.335]} color={trim} radius={0.18} />
        <Box size={[0.12, 0.06, 0.025]} position={[0, 2.17, 0.38]} color={color} glow={0.6} />
        <Joint position={[0, 2.61, 0]} size={0.21} color={trim} />
        <group ref={head} name={`${id}-head-pivot`} position={[0, 3.2, 0]}>
          {id === 'gemini' ? (
            <group rotation={[0, 0, Math.PI / 4]}>
              <Box size={[1.15, 1.15, 0.86]} color={shell} radius={0.25} roughness={0.3} />
              <Box
                size={[1.02, 1.02, 0.08]}
                position={[0, 0, 0.44]}
                color="#0a1825"
                radius={0.23}
              />
            </group>
          ) : (
            <>
              <Box size={[1.47, 1.15, 0.92]} color={trim} radius={0.26} roughness={0.3} />
              <Box
                size={[1.38, 1.09, 0.22]}
                position={[0, 0, 0.36]}
                color={shell}
                radius={0.24}
                roughness={0.25}
              />
              {id !== 'claude' && (
                <Box
                  size={[1.2, 0.88, 0.05]}
                  position={[0, 0, 0.478]}
                  color="#091824"
                  radius={0.18}
                />
              )}
            </>
          )}
          <Face id={id} status={status} />
          {[-1, 1].map((side) => (
            <group key={side} position={[side * 0.76, 0, 0]} rotation={[0, 0, Math.PI / 2]}>
              <mesh castShadow>
                <cylinderGeometry args={[0.3, 0.3, 0.17, 32]} />
                <meshStandardMaterial color={trim} metalness={0.65} roughness={0.3} />
              </mesh>
              <mesh position={[0, side * -0.095, 0]} rotation={[Math.PI / 2, 0, 0]}>
                <torusGeometry args={[0.22, 0.026, 8, 32]} />
                <meshStandardMaterial color={color} emissive={color} emissiveIntensity={0.6} />
              </mesh>
            </group>
          ))}
        </group>
        {[-1, 1].map((side) => (
          <group
            key={side}
            ref={side === -1 ? left : right}
            name={`${id}-${side === -1 ? 'left' : 'right'}-arm-pivot`}
            position={[side * 0.7, 2.35, 0]}
            rotation={[0, 0, side * 0.2]}
          >
            <Joint position={[0, 0, 0]} size={0.27} color={trim} />
            <Box size={[0.4, 0.55, 0.42]} position={[0, -0.25, 0]} color={shell} radius={0.17} />
            <Joint position={[0, -0.53, 0.04]} size={0.18} color={trim} />
            <group
              position={[0, -0.52, 0.08]}
              rotation={[id === 'gpt' ? -1.45 : -1.05, 0, side * (id === 'gpt' ? -0.95 : -0.45)]}
            >
              <Box size={[0.31, 0.48, 0.34]} position={[0, -0.19, 0]} color={shell} radius={0.12} />
              <group position={[0, -0.46, 0]}>
                <Hand />
              </group>
            </group>
          </group>
        ))}
        {id === 'claude' && (
          <group name="claude-tablet" position={[0.45, 1.9, 0.55]} rotation={[-0.55, 0, -0.18]}>
            <Box size={[0.64, 0.83, 0.075]} color="#384556" radius={0.065} />
            <Box
              size={[0.52, 0.68, 0.012]}
              position={[0, 0, 0.047]}
              color="#142935"
              radius={0.02}
            />
            {[0, 1, 2].map((i) => (
              <Box
                key={i}
                size={[0.35, 0.02, 0.014]}
                position={[0, 0.15 - i * 0.11, 0.058]}
                color={color}
                glow={0.4}
                radius={0.005}
              />
            ))}
          </group>
        )}
      </group>
    </group>
  );
}
