# Plan: Saturday evening to the 9:00 AM deadline

Tracks: A10 Agent Harness (Prompt 2) and AWS. The whole team is undergrad, so the AWS track is open to us. **Still unconfirmed:** whether one project can be entered in both tracks. Gordon asks in #hack-questions (tag `hack-rules`) right away.

## Timeline

| Time (PT) | Gordon: backend | Teammate: frontend |
|---|---|---|
| now to 6:30 | AWS setup and smoke tests (`BACKEND.md` section 8). Ask about both tracks | Repo, Vite + React + TS, app shell and polling store against the mock |
| 6:30 to 7:00 | Dinner, Heafey Atrium | Dinner |
| 7:00 to 10:00 | Store, events and routes, gateway, SHL fetch and PDF text, extractor and data checker, pre-smoke | Sources module and approval, Traffic drawer, STOP, Revoke |
| 10:00 to 1:00 | Draft, judges, repair, certainty, rollup, brief, verify_summary | Agent report module, inspector, document viewer with highlights, diffs |
| 1:00 to 3:00 | Deploy to EC2 behind API Gateway. Full SHL run. Tag `demo-ok-1` | Brief with certainty highlighting. Switch `VITE_API_BASE` to the real API and fix integration bugs |
| 3:00 to 5:30 | CLI, planted injection test page, live demo segment, prep for judge questions | Agent swarm, vault drawer, charts, scorecard, print layout |
| 5:30 to 6:00 | Feature freeze. Tag `demo-ok-final` | Same |
| 6:00 to 7:30 | Record the video while the AWS account still works. Slides. Devpost text in Gordon's voice. Submit early, since it stays editable | Same |
| 7:30 to 8:30 | Two dry runs of the 4-minute pitch. Each person answers code questions about the other's half | Same |
| 8:30 | Devpost Q&A (Bergin 116 per the website, the Atrium per the guide) | |
| 9:00 | Deadline. Whole team in the Heafey Atrium | |
| 9:15 | Judging. Find your assigned judges and time slots. Missing one means missing that round | |

The doors lock around 8 PM. Stay in, or know who can let you back in.

## Demo script: 4 minutes, then 1 minute of questions

**0:00 to 0:20, the problem.** AI can write an investment memo in a minute, but nobody can trust it. Numbers get made up, and research agents read untrusted pages from the open web.

**0:20 to 1:40, live: A10's four steps, in their order.**
1. Launch: `axelrod run "Siemens Healthineers"` in the terminal. The run shows up in the UI and the plan streams in. Approve the priority sources, drag one up, reject one.
2. Traffic: every fetch and every model call, with tokens and cost.
3. Block: a link to an unapproved domain gets blocked by default deny. Revoke a source mid-run, and the next request to it turns red. Our planted test page with a prompt injection gets flagged and quarantined (label it as our test page).
4. Stop: hit STOP. The kill switch blocks the next model call.

**1:40 to 3:00, the finished SHL run** (run earlier in the night):
- The brief, highlighted by certainty, with the report gauge next to coverage.
- Click a sentence, and follow it through the conclusion and the data item to the highlighted line on the annual report page.
- A fixed item: an extractor's digit slip caught by code, shown as a diff.
- A low-certainty score: ROTIC scores +1, but with low certainty because SHL has fewer than ten years of reports since listing. "We tell you how much to trust each line, and why."
- The register of what we could not determine.

**3:00 to 3:30, architecture.** Bedrock (Sonnet plans and drafts, a swarm of Haiku agents extracts and judges), the Guardrails tripwire, S3 as the vault, DynamoDB for the event log, EC2 with an IAM role, API Gateway in front. In production the stages become a Step Functions workflow.

**3:30 to 4:00, business.** Who buys: PE, hedge fund and corp dev analysts screening public companies. Cost per screen comes from the gateway's meter, set against analyst hours. Price per seat. Limits: public companies only, BOFFIN screen depth, and certainty measures support, not truth.

## Fallbacks

- The live run breaks: open the finished run and walk the same four steps through its recorded traffic, and say it's recorded.
- Laptop or Wi-Fi dies: play the video from a phone.
- Bedrock throttles during the live part: the live segment only needs collect and the start of extract.

## Devpost checklist

- Public repo link, every teammate added, video, screenshots, a logo
- Both tracks selected, if the organizers allow it
- Sections: Inspiration, What it does, How we built it (list the AWS services and why), Challenges, What's next, plus who buys it, what a report costs and what we'd charge
- Label the simulated parts: fixture data, the planted injection page, the demo error-injection button
