import { defineConfig } from "vitest/config";

/** Vitest configuration for the dashboard's unit tests. Uses a jsdom
 * environment so tests can exercise browser-only globals such as
 * localStorage and sessionStorage. */
export default defineConfig({
  test: {
    environment: "jsdom",
    globals: true,
  },
});
