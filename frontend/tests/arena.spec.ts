import { test, expect } from '@playwright/test';
import { CAMERAS, STATUSES } from '../arena/state/types';

test('browser renders local geometry, plays a complete week, and accepts external state', async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  const outside: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('request', (request) => {
    const u = new URL(request.url());
    if (!['127.0.0.1', 'localhost'].includes(u.hostname) && u.protocol !== 'data:')
      outside.push(request.url());
  });
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Play demo week' })).toBeEnabled({
    timeout: 60_000,
  });
  await expect(page.locator('canvas')).toBeVisible();
  await expect(page.locator('.loading')).toHaveCount(0);
  await page.waitForTimeout(1500);
  await page.screenshot({ path: testInfo.outputPath('master.png') });
  await page.getByLabel('Playback speed').selectOption('4');
  await page.getByRole('button', { name: 'Play demo week' }).click();
  await expect
    .poll(() => page.evaluate(() => window.botbetArena!.getState().competitors.gemini.status), {
      timeout: 60_000,
    })
    .toBe('POUNCE');
  await page.getByRole('button', { name: 'Pause broadcast' }).click();
  const before = await page.evaluate(() => window.botbetArena!.getState());
  await page.waitForTimeout(600);
  expect(await page.evaluate(() => window.botbetArena!.getState())).toEqual(before);
  await page.screenshot({ path: testInfo.outputPath('pounce.png') });
  await page.getByRole('button', { name: 'Resume week' }).click();
  await expect(page.getByRole('button', { name: 'Replay week' })).toBeVisible({ timeout: 120_000 });
  const final = await page.evaluate(() => window.botbetArena!.getState());
  expect(final.competitors.gemini.bankroll).toBe(18.48);
  expect(final.competitors.claude.bankroll).toBe(13);
  expect(final.competitors.gpt.status).toBe('PASS');
  await page.getByRole('button', { name: 'State readout' }).click();
  await expect(page.getByRole('row').nth(1)).toContainText('GEMINI');
  await expect(page.getByRole('row').nth(1)).toContainText('$18.48');
  await page.evaluate(() =>
    window.botbetArena!.dispatch({
      type: 'UPDATE_COMPETITOR',
      competitor_id: 'claude',
      patch: { status: 'BUSTED', bankroll: 0 },
      announce: false,
    }),
  );
  expect(await page.evaluate(() => window.botbetArena!.getState().competitors.claude.status)).toBe(
    'BUSTED',
  );
  await page.screenshot({ path: testInfo.outputPath('final.png') });
  expect(errors).toEqual([]);
  expect(outside).toEqual([]);
});

test('all states and cameras are selectable; reset and mobile remain usable', async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Play demo week' })).toBeEnabled({
    timeout: 60_000,
  });
  await page.getByRole('button', { name: 'Scene lab' }).click();
  const panel = page.getByRole('complementary', { name: 'Developer controls' });
  await panel.getByLabel('Reduced motion').check();
  await panel.getByRole('combobox', { name: 'Competitor', exact: true }).selectOption('gemini');
  for (const status of STATUSES) {
    await panel.getByRole('combobox', { name: 'State', exact: true }).selectOption(status);
    expect(
      await page.evaluate(() => window.botbetArena!.getState().competitors.gemini.status),
    ).toBe(status);
  }
  for (const camera of CAMERAS) {
    await panel.getByRole('combobox', { name: 'Camera', exact: true }).selectOption(camera);
    await expect(page.locator('.shot-label')).toContainText(camera.replaceAll('_', ' '));
  }
  await panel.getByRole('button', { name: 'Reset arena' }).click();
  expect(await page.evaluate(() => window.botbetArena!.getState().competitors.gemini.status)).toBe(
    'IDLE',
  );
  await page.getByRole('button', { name: 'Scene lab' }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(1200);
  await page.screenshot({ path: testInfo.outputPath('mobile.png') });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await expect(page.getByRole('button', { name: 'Play demo week' })).toBeVisible();
  expect(errors).toEqual([]);
});
