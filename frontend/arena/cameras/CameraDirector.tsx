'use client';
import { useThree, useFrame } from '@react-three/fiber';
import { useRef } from 'react';
import { PerspectiveCamera, Vector3 } from 'three';
import { useArenaStore } from '../state/context';
import { CameraPreset } from '../state/types';
export const CAMERA_PRESETS: Record<
  CameraPreset,
  { position: [number, number, number]; target: [number, number, number]; fov: number }
> = {
  MASTER: { position: [0, 4.5, 10.8], target: [0, 2.55, -1.5], fov: 43 },
  GPT_CLOSE: { position: [2.7, 3.6, 4.4], target: [0, 2.45, -2.5], fov: 45 },
  CLAUDE_CLOSE: { position: [-2.7, 3.4, 7.3], target: [-4.5, 2.3, 0.2], fov: 43 },
  GEMINI_CLOSE: { position: [2.7, 3.4, 7.3], target: [4.5, 2.3, 0.2], fov: 43 },
  HEAD_TO_HEAD_LEFT_CENTER: { position: [-7.6, 4.8, 10], target: [-2.1, 2.1, -1.4], fov: 53 },
  HEAD_TO_HEAD_CENTER_RIGHT: { position: [7.6, 4.8, 10], target: [2.1, 2.1, -1.4], fov: 53 },
  SCOREBOARD: { position: [0, 5.15, 4.5], target: [0, 5.05, -3.1], fov: 33 },
  CENTER_FLOOR: { position: [0, 8.8, 9], target: [0, 0.1, 1.6], fov: 45 },
};
export function CameraDirector() {
  const { camera, size } = useThree(),
    store = useArenaStore();
  const look = useRef(new Vector3(...CAMERA_PRESETS.MASTER.target));
  const desired = useRef(new Vector3());
  useFrame((_, dt) => {
    const p = store.getSnapshot().presentation,
      preset = CAMERA_PRESETS[p.camera_target],
      cam = camera as PerspectiveCamera;
    const portrait = size.width / size.height < 1.25;
    const pos = [...preset.position] as [number, number, number];
    if (portrait && p.camera_target === 'MASTER') {
      const distance = 7.5 / (Math.tan((preset.fov * Math.PI) / 360) * (size.width / size.height));
      pos[2] = Math.max(pos[2], preset.target[2] + distance);
    }
    desired.current.set(...pos);
    const blend = p.reduced_motion ? 1 : 1 - Math.exp(-dt * 3.2);
    camera.position.lerp(desired.current, blend);
    desired.current.set(...preset.target);
    look.current.lerp(desired.current, blend);
    camera.lookAt(look.current);
    cam.fov += (preset.fov - cam.fov) * blend;
    cam.updateProjectionMatrix();
  });
  return null;
}
