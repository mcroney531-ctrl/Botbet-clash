import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests',
  testMatch: '*.spec.ts',
  fullyParallel: false,
  workers: 1,
  timeout: 180_000,
  use: {
    baseURL: 'http://127.0.0.1:3131',
    viewport: { width: 1600, height: 1000 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    launchOptions: {
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
      args: [
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--use-angle=swiftshader',
        '--enable-unsafe-swiftshader',
      ],
    },
  },
  webServer: {
    command: 'npm run start -- --port 3131 --hostname 127.0.0.1',
    url: 'http://127.0.0.1:3131',
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
