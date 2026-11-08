# Axelrod: design

A harness for AI-written financial market reports. Every number has to trace back to an exact quote in an approved source, and every conclusion and forecast is checked before a person sees the report.

Built for the A10 "Agent Harness" prompt at the SCU ACM AWS x A10 Hack (Nov 7-8, 2026). The required demo maps to the product like this:

| A10 demo step | In Axelrod |
|---|---|
| Launch a CLI agent | Start a run for a company |
| Display its traffic | Live gateway feed: every fetch, model call and check |
| Block a disallowed action | Fetching an unapproved source, or citing a low-trust source for a number |
| Stop the run | Stop button on the dashboard |

Who does each step: **H** = human, **A** = agent (model), **C** = code.

## Pipeline

### Stage 1: Sources
| # | Step | Who |
|---|---|---|
| 1 | Enter the company | H |
| 2 | Decide which data types are needed | A |
| 3 | Propose sources | A |
| 4 | Rank and approve sources | A + H |
| 5 | Fetch the sources | A + C |

### Stage 2: Initial report
| # | Step | Who |
|---|---|---|
| 6 | Build the initial report (data, conclusions, bull/base/bear) with citations | A |
| 7 | Verify data claims | C |
| 8 | Verify conclusions (one validator agent per source, in parallel) | A |
| 9 | Verify extrapolation (bull/bear) | A |
| 10 | Build the human-readable report | A |

### Stage 3: Output
| # | Step | Who |
|---|---|---|
| 11 | Verify the summary against the verified report | A + C |
| 12 | Display the report and everything behind it | C |

## Source tiers (step 4)
Code assigns tiers from a domain list. The agent never assigns or raises a tier; only a human can promote a source.

| Tier | Examples | Approval | Can support |
|---|---|---|---|
| 1. Primary | Annual reports, filings, investor relations, regulators, exchanges | Human: reorder, add, delete | Any claim |
| 2. Reputable secondary | Reuters, Bloomberg, FT, trade press | Auto, human can remove | Numbers only if they agree with tier 1 |
| 3. Low trust | Reddit, blogs, small news sites | Auto, human can remove | Sentiment and leads, never a number on its own |

- Tier 1 order decides which source wins when two disagree.
- Deleting a source marks every claim citing it as unsupported.
- Auto-approved sources are capped (about 20 per run).

## Data claim check (step 7, code only)
The agent returns a quote and an approximate position for each cited number:

```json
{ "claim": "Revenue was €9,999m", "value": "9,999", "source": "S3", "quote": "Revenue rose to €9,999 million", "start": 18234 }
```

Code then:
1. Looks for the exact quote near `start` in the stored text of that source.
2. If it isn't there, searches the whole source. Found elsewhere means `position_off` (pass with a warning). Not found means `not_found` (fabricated).
3. Checks the value appears in the quote, normalising formats (`9,999` / `9.999` / `€9.9bn`), units and currency.
4. Checks the source tier is allowed to support that claim (`tier_violation` otherwise).

Checks always run against the saved extracted text (S3 + hash), never a fresh download.

## Validators (step 8)
- One agent per source, run in parallel. Each sees one source plus the claims that cite it.
- Validators have no tools and return a fixed format: `{claim_id, verdict: supported | unsupported | misleading, reason}`.
- A cross-source step compares claims about the same metric and period to find contradictions.

## Evaluation
Generate the same report with a plain Claude skill and compare:
- Score both with the same step 7 checker.
- Plant the same errors (e.g. a fake `9,999`) in both and count what each catches.
- Hand-check about 20 key numbers as ground truth.
- Track time and token cost for each.

## AWS
- Step Functions for the pipeline (Map state for parallel validators, task-token wait for human approval).
- Bedrock for agent and validator models.
- S3 for stored source text.
- DynamoDB for runs, sources, claims and gateway events.
- Lambda for code checks; API Gateway in front.

## Not investment advice
Reports are research analysis. Bull/base/bear are scenarios, not buy or sell recommendations.
