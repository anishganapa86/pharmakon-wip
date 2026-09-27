// Runs after `vite build` (as the `postbuild` npm lifecycle script), once
// Vite's own copy of public/ into dist/ has finished -- see the comment in
// vite.config.ts for why this can't be done from inside a Vite plugin hook.
import { readFileSync, writeFileSync } from "node:fs";

const swPath = "dist/sw.js";
const buildId = new Date().toISOString().replace(/[:.]/g, "-");

const sw = readFileSync(swPath, "utf-8");
if (!sw.includes("__PHARMAKON_BUILD_ID__")) {
  throw new Error(
    `${swPath} has no __PHARMAKON_BUILD_ID__ placeholder to stamp -- did public/sw.js change?`,
  );
}
writeFileSync(swPath, sw.replaceAll("__PHARMAKON_BUILD_ID__", buildId));
console.log(`Stamped ${swPath} with build id ${buildId}`);
