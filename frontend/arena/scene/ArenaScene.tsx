'use client';
import { Canvas } from '@react-three/fiber';
import { Suspense, useEffect, memo } from 'react';
import { Room } from './Room';
import { Lighting } from '../lighting/Lighting';
import { Station } from '../stations/Station';
import { Scoreboard } from '../screens/Scoreboard';
import { CameraDirector, CAMERA_PRESETS } from '../cameras/CameraDirector';
import { useArenaStore } from '../state/context';
function Ready({ onReady }: { onReady: () => void }) {
  useEffect(onReady, [onReady]);
  return null;
}
function ArenaScene({ onReady }: { onReady: () => void }) {
  const store = useArenaStore();
  return (
    <Canvas
      shadows
      dpr={[1, 1.5]}
      camera={{ position: CAMERA_PRESETS.MASTER.position, fov: 43, near: 0.1, far: 90 }}
      gl={{ antialias: true, powerPreference: 'high-performance' }}
      onCreated={({ gl }) => {
        gl.setClearColor('#101b2a');
        gl.domElement.setAttribute('aria-label', 'BotBet Clash interactive 3D arena');
        gl.domElement.addEventListener('webglcontextlost', () => store.pause());
      }}
    >
      <color attach="background" args={['#101b2a']} />
      <fog attach="fog" args={['#101b2a', 30, 70]} />
      <Suspense fallback={null}>
        <Lighting />
        <Room />
        <Station id="gpt" />
        <Station id="claude" />
        <Station id="gemini" />
        <Scoreboard />
        <CameraDirector />
        <Ready onReady={onReady} />
      </Suspense>
    </Canvas>
  );
}

export default memo(ArenaScene);
