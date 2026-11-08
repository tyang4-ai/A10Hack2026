# Plan: Saturday evening to the 9:00 AM deadline

**Update Sat 11 PM.** The backend passed G5 at 10:15 PM, five hours early. Gordon approved Tong's four feature requests and a demo built on cached documents. Rest of the night: the orchestrator works through `docs/BACKEND_V2.md` and writes READY FOR INTEGRATION in `backend/status.md` (target 4:00 AM). Then a second Claude Code session follows `docs/INTEGRATE.md`: one integration branch, the webapp wired to the backend and deployed on the same EC2 behind API Gateway, two timed rehearsals, and the laptop kit (target 5:15 AM). Freeze and merge into `main` at 5:30 as before. The timeline below is the original plan.

Tracks: A10 Agent Harness (Prompt 2) and AWS. The whole team is undergrad, so the AWS track is open to us. **Still unconfirmed:** whether one project can be entered in both tracks. Gordon asks in #hack-questions (tag `hack-rules`) right away.

## Timeline

| Time (PT) | Gordon: backend | Teammate: frontend |
|---|---|---|
| now to 6:30 | AWS setup and smoke tests (`BACKEND.md` section 8). Ask about both tracks | Repo, Next.js + React + TS, app shell and polling store against the mock |
| 6:30 to 7:00 | Dinner, Heafey Atrium | Dinner |
| 7:00 to 10:00 | Store, events and routes, gateway, SHL fetch and PDF text, extractor and data checker, pre-smoke | Sources module and approval, Traffic drawer, STOP, Revoke |
| 10:00 to 1:00 | Draft, judges, repair, certainty, rollup, brief, verify_summary | Agent report module, inspector, document viewer with highlights, diffs |
| 1:00 to 3:00 | Deploy to EC2 behind API Gateway. Full SHL run. Tag `demo-ok-1` | Brief with certainty highlighting. Switch `NEXT_PUBLIC_API_BASE` to the real API and fix integration bugs |
| 3:00 to 5:30 | CLI, planted injection test page, live demo segment, baseline comparison against plain Claude (`BACKEND.md` section 11), prep for judge questions | Agent swarm, vault drawer, charts, scorecard, print layout |
| 5:30 to 6:00 | Feature freeze. Merge `backend` and `frontend` into `main`, check the demo from `main`, tag `demo-ok-final`, make `main` the default branch | Same |
| 6:00 to 7:30 | Record the video while the AWS account still works. Slides. Devpost text in Gordon's voice. Submit early, since it stays editable | Same |
| 7:30 to 8:30 | Two dry runs of the 3-minute pitch. Each person answers code questions about the other's half | Same |
| 8:30 | Devpost Q&A (Bergin 116 per the website, the Atrium per the guide) | |
| 9:00 | Deadline. Whole team in the Heafey Atrium | |
| 9:15 | Judging. Find your assigned judges and time slots. Missing one means missing that round | |

The doors lock around 8 PM. Stay in, or know who can let you back in.

## Demo script: 3 minutes, webapp first

The live part runs on the deployed webapp with `cache_mode: documents`: every fetch is decided by the gateway, then served from the S3 document cache (Gordon's pre-event archive plus copies from earlier runs), so collection takes seconds and doesn't depend on outside sites. Model calls stay live. If Bedrock is slow, switch to `documents_and_models` (recorded model answers) and say so. The CLI is on screen for about 10 seconds. No scripted demo: we click it ourselves, in person. Rehearse twice with a stopwatch. The clicks, the timings and the `DEMO_PACING` value that worked go in `docs/LAPTOP.md`.

**0:00 to 0:15, the problem.** AI can write an investment memo in a minute, but nobody can trust it. Numbers get made up, and research agents read untrusted pages from the open web. In Axelrod, Bobby, our orchestrator agent, makes the calls and his goons do the work. Every one of them goes through one gateway, and code checks every number.

**0:15 to 0:25, launch from the CLI** (A10 step 1). `axelrod run "Siemens Healthineers" --ticker SHL` prints the run id and a link. Switch to the webapp, where the run is already there.

**0:25 to 1:20, the live run in the webapp** (A10 steps 2 to 4).
1. Bobby spawns and the plan streams in (frozen for the demo, say so). Bobby's first call shows in the log. Approve the priority sources, drag one up, reject one. Never touch S-006, S-009 or S-010.
2. Traffic: every fetch and every model call, with tokens and cost. Fetches say "cache" and when the copy was collected.
3. Block: S-009's off-site links go red on their own (default deny). Click Revoke on S-006 right after the first fetches appear (it fetches 14th, with `DEMO_PACING=0.8`), and its next request turns red. S-010, our planted test page (simulated), comes back flagged by Bedrock Guardrails and is quarantined.
4. Stop one of Bobby's goons in the Agents window. Its next model call is blocked while the rest keeps going.
5. STOP. The kill switch discards in-flight results and blocks everything after.

**1:20 to 2:35, the finished SHL run**: R2, `run_20261108_rzpoj7` (demo-ok-2), run earlier tonight on the same sources. Backup: `run_20261108_pin2ve` (demo-ok-1).
- The brief, shaded by certainty, with the report certainty next to coverage.
- Click a sentence and follow it through the conclusion and the data item to the highlighted line on the annual report page.
- A repaired item: an extractor's slip caught by code, shown as a diff.
- ROTIC scores -1 and code flags its certainty at 54, because some years lack the inputs. "We tell you how much to trust each line, and why."
- Five of six criteria are scored, and research stage is marked not applicable. Every failed item is a declared gap in the register, not a guess. Bobby's calls are in the log: he proceeded at three gates and sent three extractors back to rework at the extract gate. Don't show C-026 as a caught error: its judges rejected a claim whose years match its data.
- The baseline (an experiment, one run): plain Claude on the same pages got every quote right, but 13 of its 59 claims failed our checks, mostly on the year. Our checks caught 24 of 25 planted errors.

**2:35 to 2:50, architecture.** Bedrock (Bobby and the drafting goons on Claude Sonnet 5, a swarm of goons on Claude Haiku 4.5 that extracts and judges), the Guardrails tripwire, S3 for the vault and the document cache, DynamoDB for the event log, EC2 with an IAM role, API Gateway in front. About $3 of model calls per full report ($2.84 for R2).

**2:50 to 3:00, close.** Who buys: PE, hedge fund and corp dev analysts screening public companies, priced per seat. Labeled on screen and on Devpost: the frozen plan, the cached documents (collected before the event), the planted test page.

## Fallbacks

- The live run breaks: open the finished run and walk the four steps through its recorded traffic, and say it's recorded.
- Venue Wi-Fi dies: on the laptop, run the backend locally (`STORE=local`, it serves the recorded runs in `backend/data/replay/`, labeled) and the frontend locally (`docs/LAPTOP.md`). Last resort: the video, from a phone.
- Bedrock throttles during the live part: switch the instance to `documents_and_models` and say the model answers are recorded.

## Devpost checklist

- Public repo link, every teammate added, video, screenshots, a logo
- Both tracks selected, if the organizers allow it
- Sections: Inspiration, What it does, How we built it (list the AWS services and why), Challenges, What's next, plus who buys it, what a report costs and what we'd charge
- Label the simulated parts: fixture data, the planted injection page, the demo error-injection button, the frozen plan, the cached documents (collected before the event) and any recorded model answers
- Name the models we really use: Claude Sonnet 5 and Claude Haiku 4.5 on Bedrock (the workshop blocks the 5.5 models)
- Before submitting: make the repo public, remove the workshop account id from the docs, make `main` the default branch
