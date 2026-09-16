'use client';
import { useEffect, useMemo } from 'react';
import { Box, Stroke } from './primitives';
import { InstancedMesh, Matrix4, Color, SphereGeometry, MeshStandardMaterial } from 'three';
// All crowd and stadium geometry is deterministic and local.
export function Stadium() {
  const crowd = useMemo(() => {
    const geometry = new SphereGeometry(0.082, 6, 5),
      material = new MeshStandardMaterial({ roughness: 1 });
    const mesh = new InstancedMesh(geometry, material, 1800);
    let i = 0;
    for (let row = 0; row < 15; row++)
      for (let col = 0; col < 120; col++) {
        const x = (col - 59.5) * 0.27,
          y = 0.4 + row * 0.25 + Math.sin(col * 19 + row * 13) * 0.065,
          z = -13 - row * 0.27 + Math.pow(x / 18, 2) * 1.5;
        mesh.setMatrixAt(i, new Matrix4().makeTranslation(x, y, z));
        mesh.setColorAt(
          i,
          new Color(
            ['#667794', '#b79c80', '#c7b9a0', '#6f7b8d', '#d4a67a', '#344863'][
              (row * 7 + col * 13) % 6
            ],
          ),
        );
        i++;
      }
    return mesh;
  }, []);
  useEffect(
    () => () => {
      crowd.geometry.dispose();
      (crowd.material as MeshStandardMaterial).dispose();
    },
    [crowd],
  );
  return (
    <group name="stadium-backdrop">
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.12, -17]}>
        <planeGeometry args={[42, 30]} />
        <meshStandardMaterial color="#31564b" roughness={1} />
      </mesh>
      {Array.from({ length: 8 }, (_, i) => (
        <Stroke
          key={i}
          points={[
            [-18, -0.1, -8 - i * 2.6],
            [18, -0.1, -8 - i * 2.6],
          ]}
          color="#97aca3"
          width={1}
        />
      ))}
      <Box size={[39, 8, 0.4]} position={[0, 3, -20]} color="#172b46" radius={0.1} />
      {Array.from({ length: 15 }, (_, row) => (
        <Box
          key={row}
          size={[36, 0.12, 0.3]}
          position={[0, 0.28 + row * 0.25, -13 - row * 0.27]}
          color={row % 3 === 0 ? '#475268' : '#26374e'}
          radius={0.015}
        />
      ))}
      <primitive object={crowd} />
      {[0, 1, 2].map((i) => (
        <Box
          key={i}
          size={[35, 0.04, 0.1]}
          position={[0, 0.5 + i * 1.25, -12.7 - i * 1.5]}
          color="#d7b482"
          glow={1}
          radius={0.018}
        />
      ))}
      <group position={[0, 0, -12]}>
        <Box size={[0.12, 2.2, 0.12]} position={[0, 1.1, 0]} color="#e2b650" />
        <Box size={[3.8, 0.11, 0.12]} position={[0, 2.2, 0]} color="#edc458" />
        {[-1.85, 1.85].map((x) => (
          <Box key={x} size={[0.1, 2.6, 0.1]} position={[x, 3.5, 0]} color="#edc458" />
        ))}
      </group>
      {[-7, 7].map((x) => (
        <group key={x} position={[x, 0, -15]}>
          <Box size={[0.08, 5.5, 0.08]} position={[0, 2.75, 0]} color="#62738b" />
          <Box size={[1.85, 0.65, 0.16]} position={[0, 5.45, 0]} color="#52687f" radius={0.06} />
          {Array.from({ length: 12 }, (_, i) => (
            <Box
              key={i}
              size={[0.22, 0.2, 0.04]}
              position={[-0.7 + (i % 6) * 0.28, 5.3 + Math.floor(i / 6) * 0.28, 0.1]}
              color="#eaf4ff"
              glow={2}
              radius={0.025}
            />
          ))}
        </group>
      ))}
    </group>
  );
}
