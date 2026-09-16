import { CompetitorId } from '../state/types';
export const COLORS = { gpt: '#56eed3', claude: '#ffc278', gemini: '#599fff' };
export const WARM = '#ffca8a';
export const PLACEMENT: Record<
  CompetitorId,
  { position: [number, number, number]; rotation: number }
> = {
  gpt: { position: [0, 0, -2.8], rotation: 0 },
  claude: { position: [-4.5, 0, 0.2], rotation: 0.27 },
  gemini: { position: [4.5, 0, 0.2], rotation: -0.27 },
};
export const money = (n: number) =>
  new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n);
export const odds = (n: number) => (n > 0 ? `+${n}` : `${n}`);
