// Build the ESM and IIFE bundles with esbuild (the only build dependency) and
// report the gzip size of the IIFE build. Type declarations are emitted
// separately by `tsc -p tsconfig.build.json` (see the build script).

import { build } from "esbuild";
import { gzipSync } from "node:zlib";
import { readFileSync } from "node:fs";

const shared = {
  entryPoints: ["src/index.ts"],
  bundle: true,
  minify: true,
  sourcemap: false,
  target: ["es2019"],
  legalComments: "none",
  logLevel: "warning",
};

await build({
  ...shared,
  format: "esm",
  outfile: "dist/crashlens.esm.js",
});

await build({
  ...shared,
  format: "iife",
  globalName: "Crashlens",
  outfile: "dist/crashlens.iife.js",
});

const iife = readFileSync("dist/crashlens.iife.js");
const raw = (iife.length / 1024).toFixed(2);
const gz = (gzipSync(iife).length / 1024).toFixed(2);
console.log(`IIFE bundle: ${raw} KB raw, ${gz} KB gzip (target < 6 KB gzip)`);
