// SPDX-License-Identifier: AGPL-3.0-only
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    include: ['src/**/*.test.{ts,tsx}'],
    environment: 'jsdom',
    // Node 25's file-backed Web Storage must not replace jsdom's per-window
    // browser storage. Disable only the host implementation in test workers.
    execArgv: process.allowedNodeEnvironmentFlags.has('--no-experimental-webstorage')
      ? ['--no-experimental-webstorage'] : [],
    setupFiles: './src/setupTests.ts',
    css: true,
  },
});
