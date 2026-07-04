// Build the ESM and CJS bundles with esbuild (the only build dependency).
// Node builtins stay external automatically under platform "node". Type
// declarations are emitted separately by `tsc -p tsconfig.build.json`
// (see the build script in package.json).

import { build } from "esbuild";

const shared = {
  entryPoints: ["src/index.ts"],
  bundle: true,
  platform: "node",
  target: ["node18"],
  sourcemap: false,
  minify: false,
  legalComments: "none",
  logLevel: "warning",
};

await build({
  ...shared,
  format: "esm",
  outfile: "dist/index.mjs",
});

await build({
  ...shared,
  format: "cjs",
  outfile: "dist/index.cjs",
});

console.log("Built dist/index.mjs and dist/index.cjs");
