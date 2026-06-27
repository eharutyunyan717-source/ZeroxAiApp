import { cp, mkdir, rm } from "node:fs/promises";
import { join } from "node:path";

const dist = "dist";
const files = [
  "index.html",
  "manifest.webmanifest",
  "service-worker.js",
  "src",
  "assets"
];

await import("./check-assets.js");

await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });

for (const file of files) {
  await cp(file, join(dist, file), { recursive: true });
}

console.log("ZeroxAI build completed.");
console.log(`Output: ${dist}`);
