import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["test/**/*.test.ts"],
    // The uncaught/unhandled tests spawn child processes and wait on a local
    // HTTP server, which can take a moment on a cold machine.
    testTimeout: 20000,
  },
});
