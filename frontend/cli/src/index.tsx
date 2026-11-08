// Entry point: `axelrod-tui` opens the projects list, `axelrod-tui <run_id>` opens a run.
// Runs full screen in the terminal's alternate buffer, and puts the terminal back on exit.
import { render } from "ink";
import { App } from "./app.tsx";

const arg = process.argv[2];
if (arg === "-h" || arg === "--help") {
  console.log(`Axelrod in the terminal

  axelrod-tui            the projects list
  axelrod-tui <run_id>   open one run

Talks to AXELROD_API (default http://localhost:8000/api/v1). AXELROD_API_KEY is sent if set.`);
  process.exit(0);
}

const fullscreen = process.stdout.isTTY;
if (fullscreen) process.stdout.write("\x1b[?1049h\x1b[H");
const app = render(<App initialRun={arg} />, { exitOnCtrlC: true });
await app.waitUntilExit();
if (fullscreen) process.stdout.write("\x1b[?1049l");
