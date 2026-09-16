import type { Metadata } from 'next';
import './globals.css';
export const metadata: Metadata = {
  title: 'BotBet Clash — The Arena',
  description: 'A local 3D sports broadcast demo with three AI competitors. No live wagers.',
};
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
