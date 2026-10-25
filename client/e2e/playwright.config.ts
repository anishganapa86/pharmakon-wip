import path from 'node:path'
import { defineConfig, devices } from '@playwright/test'
import { AUTH_FILE } from './auth-state.ts'

const runDir =
  process.env.DEMO_RUN_DIR ??
  path.resolve(import.meta.dirname, '../../reports/runs/local')
const gateOnly = process.env.DEMO_GATE_ONLY === '1'

export default defineConfig({
  testDir: import.meta.dirname,
  testMatch: /.*\.spec\.ts/,
  globalSetup: path.join(import.meta.dirname, 'global-setup.ts'),
  outputDir: path.join(import.meta.dirname, 'test-results'),
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 20_000 },
  grep: gateOnly ? /@gate/ : undefined,
  grepInvert: gateOnly ? undefined : /@gate/,
  reporter: [
    ['list'],
    [path.join(import.meta.dirname, 'reporter.ts'), { runDir }],
  ],
  use: {
    baseURL: process.env.DEMO_BASE_URL ?? 'http://localhost:5173',
    serviceWorkers: 'block',
    storageState: AUTH_FILE,
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
  projects: [
    {
      name: 'desktop',
      testMatch: /(flow|gate|auth|survey|survey-mapping)\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } },
    },
    {
      name: 'phone',
      testMatch: /phone\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 390, height: 844 },
        isMobile: true,
        hasTouch: true,
        deviceScaleFactor: 2,
      },
    },
  ],
})
