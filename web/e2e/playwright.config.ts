import { defineConfig, devices } from "@playwright/test"

// End-to-end tests against a REAL server: `global-setup.ts` builds the app when needed and
// starts `aksha serve --web dist` in fake mode on a free loopback port, in a fresh
// AKSHA_HOME; the tests read its URL from the environment (fixtures.ts). One worker, in
// order: the specs share one server and build on each other's state.
export default defineConfig({
  testDir: ".",
  testMatch: "**/*.spec.ts",
  globalSetup: "./global-setup.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 30_000 },
  reporter: [["list"]],
  outputDir: "../test-results",
  use: {
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1.5,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium" }],
})
