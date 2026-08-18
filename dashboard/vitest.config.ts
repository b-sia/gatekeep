import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config";

/** Vitest configuration for the dashboard's unit tests. Merges with the
 * project's Vite config (so the React plugin and other build settings
 * apply under test) and adds a jsdom environment so tests can exercise
 * browser-only globals such as localStorage and sessionStorage. */
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: "jsdom",
    },
  }),
);
