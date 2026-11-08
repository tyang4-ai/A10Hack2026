#!/usr/bin/env node
// Launcher: runs the TypeScript entry with tsx, so there is no build step.
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const tsx = join(root, "node_modules", ".bin", "tsx");
const r = spawnSync(tsx, [join(root, "src", "index.tsx"), ...process.argv.slice(2)], { stdio: "inherit" });
process.exit(r.status ?? 1);
