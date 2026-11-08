# Baseline evaluation

**Experiment, run on 2026-11-07, not part of the demo run.**

Our checks caught 24 of 25 planted errors in the harness report. A plain Claude report gave 59 claims, and 0 of them failed our own quote check.

13 of the 59 plain claims and 39 of the 259 harness items failed at least one of the four checks.

This compares two ways of reading the same pages. It is one run with one prompt, not a benchmark. Run run_20261108_63kwb8, started 2026-11-08T04:43:07.884Z.

## Setup

- Pages: the same 36 pages the harness reads (`extract_pages` in `data/shl/plan.json`), fetched through the gateway into a throwaway local run. Stored pages were never edited.
  - S-001 (AR2025): Siemens Healthineers Annual Report FY2025, pages 7-8, 13, 15, 22, 25, 138, 140-141, 152, 156, 159-160, 162, 169, 177, 182, 184, 189
  - S-002 (Q4FY2026): Siemens Healthineers Q4 FY2026 earnings release and financial results, pages 1, 6-8, 13, 15, 17, 19
  - S-003 (AR2023): Siemens Healthineers Annual Report FY2023, pages 53, 55, 99
  - S-004 (AR2021): Siemens Healthineers Annual Report FY2021, pages 44, 46, 85
  - S-005 (AR2019): Siemens Healthineers Annual Report FY2019, pages 62, 64, 95
- Harness: one small-model extractor per page (us.anthropic.claude-haiku-4-5-20251001-v1:0). Code locates every quote, assigns the ids and runs the checks. A near-miss quote gets one re-cite by code from the closest real span.
- Plain Claude: one large-model call (us.anthropic.claude-sonnet-5) with all 36 pages and an analyst's prompt: write a BOFFIN screen of Siemens Healthineers AG from these pages, cite page numbers and quote the numbers you use. It was asked for structured claims (value, unit, period, page and quote) so our checks could run on them. No harness: no page-by-page agents, no re-cite, no repair. The call still went through the gateway, so it was metered like every other call.
- Our checks, the same code on both sides: quote_exact, value_in_quote, unit_context and period_match. For both sides, code took the unit and period header lines above each found quote, so a table value does not fail just for a missing header.

## Results

| | Harness | Plain Claude |
|---|---|---|
| Items or claims | 259 | 59 |
| Pass all four checks | 220 | 46 |
| Fixed by the code re-cite | 15 | not used |
| Failed quote_exact | 14 | 0 |
| Failed value_in_quote | 0 | 1 |
| Failed unit_context | 0 | 1 |
| Failed period_match | 25 | 12 |
| Failed any of the four | 39 | 13 |
| Could not be checked (blocked) | 0 | 0 |
| Quote found, but on another page than cited | n/a | 0 |
| Statement has a number its quote does not hold | 0 | 3 |
| Facts without a number | 19 | 3 |
| Model calls | 36 | 1 |
| Tokens in / out | 118,300 / 41,784 | 43,841 / 9,771 |
| Cost (gateway meter) | $0.3599 | $0.2039 |
| Wall time | 29 s | 70 s |
| Cost per item that passes all four | $0.0016 | $0.0044 |
| Passing numbers the other side also has (same period and row, or same value) | 39 of 201 | 28 of 43 |

Why period_match failed. The quote and its header do not name the period: harness 18, plain 12. The value sits under another year in the quote or table: harness 7, plain 0. The first kind is often a true value whose year is printed elsewhere on the page, so the check is strict there, the same way on both sides.

Fetching the pages took 1.5 s and is shared by both sides. The whole run spent $0.5639 on 37 model calls. The gateway allowed 42 requests and blocked 0.

## Planted errors

Code planted each kind of error into copies of up to 5 clean items per side (items that pass all four checks), spread evenly over the items. The same kinds as the B-verify planted set. Caught means at least one of the four checks fails on the planted copy.

| Error | Target check | Harness caught | Plain Claude caught |
|---|---|---|---|
| digit swap | quote_exact | 5 of 5 | 5 of 5 |
| dropped minus | value_in_quote | 4 of 5 | 3 of 3 |
| wrong year | period_match | 5 of 5 | 5 of 5 |
| wrong scale | unit_context | 5 of 5 | 5 of 5 |
| flipped not | quote_exact | 5 of 5 | 5 of 5 |
| Total | | 24 of 25 | 23 of 23 |

A plain Claude report has no checks of its own. Without ours it would catch none of these.

Missed:

- harness D-166, dropped minus ('– 658' to '658'): all four checks pass

## Plain Claude claims that did not pass

| Claim | Statement | Check | Why |
|---|---|---|---|
| claim 16 | Non-derivative financial liabilities maturing in FY2026 totaled €6,243 million as of Sept 30, 2025. | period_match | The period_context quote does not mention FY2026 (it names 2028 and 2030) |
| claim 18 | Non-derivative financial liabilities maturing in fiscal 2031 and thereafter totaled €6,233 million. | period_match | The period_context quote does not mention FY2031 and thereafter (it names 2028 and 2030) |
| claim 19 | Siemens Healthineers has a multicurrency revolving credit facility of up to €2.5 billion with Siemens AG fo... | period_match | The evidence quote does not mention FY2025 (it names 2024) |
| claim 20 | Siemens Healthineers has a backup multicurrency revolving credit facility of up to €2.0 billion with Siemen... | period_match | The evidence quote does not mention FY2025 (it names 2024) |
| claim 21 | As of Sept 30, 2025, €451 million was drawn from the credit facilities with Siemens AG. | period_match | The evidence quote does not mention FY2025 (it names 2024) |
| claim 25 | FY2026 proposed dividend is €1.00 per share, same as prior year. | period_match | The evidence quote does not mention FY2026 |
| claim 26 | Imaging segment adjusted EBIT margin was 23.1% in FY2026, slightly below prior year. | period_match | The evidence quote does not mention FY2026 |
| claim 27 | Precision Therapy segment adjusted EBIT margin was 14.9% in FY2026, slightly above prior year. | period_match | The evidence quote does not mention FY2026 |
| claim 28 | Diagnostics segment adjusted EBIT margin was 3.2% in FY2026, clearly below prior-year level. | period_match | The evidence quote does not mention FY2026 |
| claim 34 | Global addressable market for Diagnostics segment was approximately €37 billion in FY2025. | period_match | The evidence quote does not mention FY2025 |
| claim 36 | Global addressable market for Advanced Therapies segment was over €7 billion in FY2025. | period_match | The evidence quote does not mention FY2025 |
| claim 43 | Total liabilities to the Siemens Group from loans were €12,756 million (current €1,919m + non-current €10,8... | value_in_quote | as_reported '1,919 / 10,837' reads as 1919, but value.number is 12756 |
| claim 57 | FY2026 income tax rate was 22.4%, below prior-year level. | period_match | The evidence quote does not mention FY2026 |

Harness items that did not pass: 39 (listed in results.json).

## Plain Claude numbers the harness has no item for

These plain claims pass all four checks, but no passing harness item has the same period on the same row or sentence, or the same value on any page. The extractors only take what the plan's data needs ask for, so some of these are left out by design. The rest are numbers the page-by-page extractors missed.

| Claim | Statement | Source, page | Quote |
|---|---|---|---|
| claim 2 | FY2026 adjusted EBIT was €3,823 million, down 0.8% nominally, with margin of 16.2%. | S-002 p. 6 | Adjusted EBIT2 3,823 3,855 −0.8% |
| claim 3 | FY2026 net income was €2,243 million, up 3.5% from FY2025. | S-002 p. 6 | Net income 2,243 2,168 3.5% |
| claim 4 | FY2026 free cash flow was €2,446 million, down 9.9% from FY2025. | S-002 p. 6 | Free cash flow4 2,446 2,714 −9.9% |
| claim 6 | FY2025 net income was €2,168 million. | S-001 p. 138 | Net income 2,168 1,959 |
| claim 40 | Equipment book-to-bill ratio for FY2025 was a very good 1.14, above prior-year 1.11. | S-001 p. 15 | The equipment book-to-bill ratio2 in fiscal year 2025 was a very good 1.14, above the prior-year figure of 1.11 which... |
| claim 41 | Equipment book-to-bill ratio for FY2026 was a very good 1.14, on par with prior-year 1.15. | S-002 p. 6 | The equipment book-to-bill ratio was a very good 1.14, on par with the prior-year level of 1.15*, which was also very... |
| claim 42 | Siemens AG announced on Nov 12, 2025 its intent to deconsolidate Siemens Healthineers and transfer 30% of s... | S-001 p. 189 | On November 12, 2025, Siemens AG announced that it intends to deconsolidate Siemens Healthineers and transfer 30% of ... |
| claim 45 | FY2025 revenue by region: Americas €10,283 million, up 9.1% nominally, driven by strong US growth. | S-001 p. 15 | Americas 10,283 9,428 9.1% 11.7% |
| claim 46 | FY2025 revenue in China was €2,482 million, down 2.7% nominally. | S-001 p. 15 | China 2,482 2,550 −2.7% −0.9% |
| claim 48 | FY2023 net income was €1,525 million. | S-003 p. 53 | Net income 1,525 2,054 |
| claim 50 | FY2021 net income was €1,746 million. | S-004 p. 44 | Net income 1,746 1,423 |
| claim 52 | FY2019 net income was €1,586 million. | S-005 p. 62 | Net income 1,586 1,284 |
| claim 54 | Total equity as of Sept 30, 2025 was €18,091 million. | S-001 p. 140 | Total equity 18,091 18,248 |
| claim 56 | Total equity as of Sept 30, 2026 was €19,917 million. | S-002 p. 15 | Total equity 19,917 18,091 |
| claim 58 | FY2026 adjusted basic EPS was €2.42, versus €2.39 prior year. | S-002 p. 6 | Adjusted basic earnings per share3 2.42 2.39 1.1% |

## The plain report's own scores (not checked by code)

Siemens Healthineers enters FY2027 on a solid operating footing: FY2026 revenue rose to €23,630m (+4.0% comparable) with adjusted EBIT of ~€3.8bn (16.2% margin) and free cash flow of ~€2.4bn, following a strong FY2025 (revenue €23,375m, +5.9% comparable, FCF €2,714m). Leverage is moderate and improving (net debt including pensions/EBITDA of 2.8x at FY2025-end, down from 3.3x), but the capital structure is dominated by intercompany loans and credit facilities from the Siemens Group (~€12.8bn of loans, €4.5bn of facilities) that face early-termination risk given Siemens AG's announced plan (Nov 12, 2025) to deconsolidate SHL and spin off 30% of shares. Liquidity looks adequate, with FCF and undrawn facilities covering near-term maturities and dividends more than 2x. Secular demand drivers (aging population, addressable imaging/diagnostics/radiotherapy markets of €31-37bn and €6-7bn resp...

| Criterion | Score | Reasoning |
|---|---|---|
| rotic_level | 0 | NOPAT proxy: FY2025 EBIT €3,154m less ~22-24% tax ≈ €2,400m against average total capital employed. Total assets €44,370m (AR2025 p.140) and total equity €18,091m give TICAP in the €30-35bn range once net debt and goodwill-heavy balance sheet are netted, implying ROTIC in the low-to-mid teens, no... |
| rotic_vs_peers | empty | No peer ROTIC figures were provided in these pages, so a direct comparison cannot be made; segment adjusted EBIT margins (Imaging 23.1%, Precision Therapy 14.9%, Diagnostics 3.2% in FY2026, Q4FY2026 p.7) show internal dispersion but no external peer benchmark is available. |
| secular_growth | +1 | Addressable markets are large and growing: Imaging >€31bn, Diagnostics ~€37bn, Varian >€6bn, Advanced Therapies >€7bn in FY2025 (AR2025 p.13), driven by aging population (over-60s to more than double by 2050, AR2025 p.8) and rising non-communicable disease burden. Comparable revenue growth of 5.9... |
| leverage | 0 | Net debt including pensions was €12,472m against EBITDA of €4,450m, a leverage ratio of 2.8x as of Sept 30, 2025 (AR2025 p.162), down from 3.3x a year earlier - a moderate, improving level. However, most debt (€10,837m non-current + €1,919m current of ~€12,756m total loans, AR2025 p.160) is inter... |
| liquidity | +1 | FY2025 free cash flow of €2,714m (AR2025 p.22) and FY2026 of €2,446m (Q4FY2026 p.6) comfortably exceed the FY2026 maturity of €6,243m non-derivative financial liabilities spread over one year, and even cover the 5-year maturities profile (FY2026 €6,243m + FY2027 €2,098m + FY2028-30 €5,158m = ~€13... |
| research_stage | +1 | The analyst has tracked Siemens Healthineers across multiple annual reports from FY2019 through FY2025 and the Q4 FY2026 release, a continuous coverage history spanning more than seven years, satisfying the >1 year closely-followed threshold. |

No code checked these scores or the arithmetic in their reasoning. In the harness, scores cite conclusions, and code checks computations and rubric thresholds.
The plain report scored research_stage. The rubric says that criterion is about the analyst's own coverage history, which no page can state. The harness leaves it as a declared gap.

## Key numbers to hand-check

20 numbers picked by code from harness items that pass all four checks, latest period first. Page is the 1-based page in the PDF file, not the printed page label. The last column says what the plain Claude report gave for the same row or sentence and period.

| # | What | Value | Period | Source, page | Quote | Plain report |
|---|---|---|---|---|---|---|
| 1 | Revenue was €23,630 million in fiscal year 2026. (D-028) | 23,630 EUR million | FY2026 | S-002 p. 6 | Revenue 23,630 23,375 | same (claim 1) |
| 2 | Revenue increased by 5.9% on a comparable basis in fiscal year 2025. (D-225) | 5.9% | FY2025 | S-001 p. 15 | Revenue increased by 5.9% on a comparable basis from the prior year. | not given |
| 3 | Income before income taxes was €2,890 million in fiscal year 2026. (D-174) | 2,890 EUR million | FY2026 | S-002 p. 13 | Income before income taxes 765 770 2,890 2,853 | not given |
| 4 | Income tax expenses were €646 million in fiscal year 2026. (D-208) | −646 EUR million | FY2026 | S-002 p. 13 | Income tax expenses −166 −174 −646 −686 | not given |
| 5 | Free cash flow was €2,714 million in fiscal year 2025. (D-007) | 2,714 EUR million | FY2025 | S-001 p. 22 | Free cash flow 2,714 2,130 | same (claim 9) |
| 6 | Cash flows from operating activities were €3,287 million in fiscal year 2026. (D-016) | 3,287 EUR million | FY2026 | S-002 p. 17 | Cash flows from operating activities 3,287 3,532 | not given |
| 7 | Research and development expenses were €1,969 million in fiscal year 2026. (D-088) | −1,969 EUR million | FY2026 | S-002 p. 13 | Research and development expenses −518 −499 −1,969 −1,958 | not given |
| 8 | Amortization, depreciation and other effects from IFRS 3 purchase price allocation adju... (D-071) | −357 EUR million | FY2025 | S-001 p. 184 | Amortization, depreciation and other effects from IFRS 3 purchase price allocation adjustments −357 −375 | not given |
| 9 | Total assets were €46,707 million at September 30, 2026. (D-021) | 46,707 EUR million | FY2026 | S-002 p. 15 | Total assets 46,707 44,370 | same (claim 55) |
| 10 | Cash and cash equivalents were €2,542 million at September 30, 2026. (D-047) | 2,542 EUR million | FY2026 | S-002 p. 15 | Cash and cash equivalents 2,542 2,175 | not given |
| 11 | Goodwill was €17,477 million at September 30, 2026. (D-073) | 17,477 EUR million | FY2026 | S-002 p. 15 | Goodwill 17,477 17,124 | not given |
| 12 | Long-term financial debt was €589 million at September 30, 2026. (D-122) | 589 EUR million | FY2026 | S-002 p. 15 | Long-term financial debt 589 487 | not given |
| 13 | Short-term financial debt and current maturities of long-term financial debt were €277 ... (D-098) | 277 EUR million | FY2026 | S-002 p. 15 | Short-term financial debt and current maturities of long-term financial debt 277 268 | not given |
| 14 | Net debt was €11,985 million as of September 30, 2025. (D-151) | 11,985 EUR million | FY2025 | S-001 p. 162 | Net debt 11,985 12,643 | not given |
| 15 | Provisions for pensions and similar obligations were €333 million at September 30, 2026. (D-143) | 333 EUR million | FY2026 | S-002 p. 15 | Provisions for pensions and similar obligations 333 488 | not given |
| 16 | Current lease liabilities were €176 million as of September 30, 2025. (D-089) | 176 EUR million | FY2025 | S-001 p. 159 | Lease liabilities 176 172 | not given |
| 17 | Total equity was €9,782 million on September 30, 2019. (D-246) | 9,782 EUR million | FY2019 | S-005 p. 64 | Total equity 9,782 8,675 | not given |
| 18 | Dividends paid to shareholders of Siemens Healthineers AG were €1,118 million in fiscal... (D-065) | −1,118 EUR million | FY2026 | S-002 p. 17 | Dividends paid to shareholders of Siemens Healthineers AG −1,118 −1,066 | same (claim 24) |
| 19 | Current liabilities to the Siemens Group were €2,932 million at September 30, 2026. (D-180) | 2,932 EUR million | FY2026 | S-002 p. 15 | Current liabilities to the Siemens Group 2,932 3,192 | not given |
| 20 | Non-derivative financial liabilities due in fiscal years 2031 and thereafter were €6,23... (D-189) | 6,233 EUR million | FY2031+ | S-001 p. 177 | Maturity analysis as of Sept 30, 2025 (in millions of €) Fiscal year 2026 Fiscal year 2027 Fiscal years 2028 to 2030 Fiscal years 2031 and thereafter Non-der... | same (claim 18) |

## Limits

- One run, one prompt, one model call. A different prompt or model could do better or worse.
- The harness side here is extract only, the data layer of the agent report. Draft, judges and repair are not part of this comparison.
- The planted set is five kinds of slip, planted by code into copies of items that passed. Kinds a side had no eligible item for are planted fewer times or not at all.
- Known checker limit: a dash followed by a space before a number can be a range dash, so a dropped minus there is accepted, not caught.
- An exact quote proves the page says it. It does not prove the number is the right one for the question.

## Rerun

From the repo root:

```
.venv/Scripts/python backend/scripts/baseline.py --keep
.venv/Scripts/python backend/scripts/baseline.py --reuse backend/.data/baseline
```

The first line fetches, extracts and makes the one baseline call (live Bedrock, about $1). The second recomputes this file from a kept run with no model calls.
