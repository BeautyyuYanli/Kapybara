import { defineConfig, devices } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e",
  retries: 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: { baseURL: "http://127.0.0.1:5173/app/", headless: true, trace: "retain-on-failure" },
  projects: [
    { name: "mobile-chromium", use: { ...devices["Pixel 5"] } },
    { name: "desktop-chromium", use: { ...devices["Desktop Chrome"] } },
  ],
});
