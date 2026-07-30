import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/dashboard/",
  server: {
    proxy: {
      "/dashboard/api": "http://localhost:8100",
    },
  },
  build: {
    outDir: "dist",
  },
});
