'use client';
import { RoundedBox, Line } from '@react-three/drei';
import { ReactNode, useEffect, useState } from 'react';
import { CanvasTexture, SRGBColorSpace, LinearFilter } from 'three';
import type { ThreeElements } from '@react-three/fiber';
export function Box({
  size,
  color = '#30394a',
  radius = 0.12,
  metalness = 0.15,
  roughness = 0.5,
  glow = 0,
  ...props
}: {
  size: [number, number, number];
  color?: string;
  radius?: number;
  metalness?: number;
  roughness?: number;
  glow?: number;
} & Omit<ThreeElements['mesh'], 'args' | 'ref'>) {
  return (
    <RoundedBox
      args={size}
      radius={Math.min(radius, ...size.map((n) => n / 2 - 0.001))}
      smoothness={3}
      castShadow
      receiveShadow
      {...props}
    >
      <meshStandardMaterial
        color={color}
        metalness={metalness}
        roughness={roughness}
        emissive={color}
        emissiveIntensity={glow}
      />
    </RoundedBox>
  );
}
export function Stroke({
  points,
  color = '#ffffff',
  width = 2,
}: {
  points: [number, number, number][];
  color?: string;
  width?: number;
}) {
  return <Line points={points} color={color} lineWidth={width} />;
}
export type Painter = (ctx: CanvasRenderingContext2D, w: number, h: number) => void;
export function useSurface(key: string, paint: Painter, width = 1024, height = 512) {
  const [surface] = useState(() => {
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const texture = new CanvasTexture(canvas);
    texture.colorSpace = SRGBColorSpace;
    texture.minFilter = LinearFilter;
    texture.magFilter = LinearFilter;
    texture.generateMipmaps = false;
    return { canvas, texture };
  });
  useEffect(() => {
    const c = surface.canvas.getContext('2d')!;
    c.clearRect(0, 0, width, height);
    paint(c, width, height);
    surface.texture.needsUpdate = true;
  }, [key, surface, width, height]); // paint content is represented by key
  useEffect(() => () => surface.texture.dispose(), [surface]);
  return surface.texture;
}
export function Surface({
  contentKey,
  paint,
  width,
  height,
  pixels = [1024, 512],
  ...props
}: {
  contentKey: string;
  paint: Painter;
  width: number;
  height: number;
  pixels?: [number, number];
} & Omit<ThreeElements['mesh'], 'args' | 'ref'>) {
  const texture = useSurface(contentKey, paint, ...pixels);
  return (
    <mesh {...props}>
      <planeGeometry args={[width, height]} />
      <meshBasicMaterial map={texture} toneMapped={false} transparent />
    </mesh>
  );
}
export function label(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  size: number,
  color = '#edf5ff',
  align: CanvasTextAlign = 'center',
  weight = 600,
) {
  ctx.fillStyle = color;
  ctx.textAlign = align;
  ctx.textBaseline = 'middle';
  ctx.font = `${weight} ${size}px Arial, sans-serif`;
  ctx.fillText(text, x, y);
}
export function FittedLabel(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  size: number,
  maxWidth: number,
  color = '#edf5ff',
) {
  let s = size;
  ctx.font = `600 ${s}px Arial`;
  while (ctx.measureText(text).width > maxWidth && s > 12) {
    s -= 2;
    ctx.font = `600 ${s}px Arial`;
  }
  label(ctx, text, x, y, s, color);
}
export function Meter(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  value: number,
  color: string,
  segmented = false,
) {
  ctx.fillStyle = '#273442';
  ctx.fillRect(x, y, w, h);
  ctx.fillStyle = color;
  if (segmented) {
    for (let i = 0; i < 10; i++) {
      if (i < Math.round(value * 10)) ctx.fillRect(x + (i * w) / 10, y, w / 10 - 5, h);
    }
  } else ctx.fillRect(x, y, w * Math.max(0, Math.min(1, value)), h);
}
export function SmallScreen({
  position,
  children,
}: {
  position: [number, number, number];
  children?: ReactNode;
}) {
  return (
    <group position={position} rotation={[-0.45, 0, 0]}>
      <Box size={[0.64, 0.36, 0.07]} radius={0.04} color="#17212b" />
      {children}
    </group>
  );
}
