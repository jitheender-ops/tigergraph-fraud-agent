import { defineConfig } from '@playwright/test';

// Starts the real API (DuckDB mirror, writes into a throwaway state dir) and the console,
// then drives the console in Chromium. Needs build/fraud.db, so it runs locally, not in CI.
//   npx playwright test
const STATE = '../build/e2e-state';
export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  use: { baseURL: 'http://localhost:5173' },
  webServer: [
    {
      command: `rm -rf ${STATE} && mkdir -p ${STATE} && cp ../build/triggers.json ${STATE}/ && ` +
               `cd .. && .venv/bin/uvicorn server:app --port 8000`,
      url: 'http://localhost:8000/api/health',
      env: { CONSOLE_BACKEND: 'duckdb', CONSOLE_STATE_DIR: 'build/e2e-state',
             ANALYST_TOKEN: 'e2e-analyst' },
      timeout: 180_000,
      reuseExistingServer: false,
    },
    { command: 'npx vite --port 5173 --strictPort', url: 'http://localhost:5173',
      reuseExistingServer: false },
  ],
});
