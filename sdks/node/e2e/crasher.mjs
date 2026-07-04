// Crasher subprocess for the Node SDK end-to-end proof.
//
// Imports the BUILT bundle (dist/index.mjs, produced by `npm run build`), inits
// the real SDK with a DSN from the environment, then throws an UNCAUGHT error on
// a later tick. The SDK's process-level 'uncaughtException' handler (see
// src/instrument.ts) captures it at level fatal, flushes, and then mirrors
// Node's default by exiting with code 1 - so the event reaches the ingest
// endpoint even though the process crashes.
//
// Run by run_e2e.mjs, never directly. Reads CRASHLENS_DSN from the environment.

import { init } from "../dist/index.mjs";
import { MARKER } from "./marker.mjs";

init({
  dsn: process.env.CRASHLENS_DSN,
  release: "nodesvc@1.0.0",
  environment: "production",
});

// Uncaught on purpose, deferred one tick so the handler is installed first.
setImmediate(() => {
  throw new Error(MARKER);
});
