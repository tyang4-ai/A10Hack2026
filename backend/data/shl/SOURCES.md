# SHL sources

This plan is frozen for the demo. `plan.json` is the plan itself, and `backend/scripts/check_plan.py` checks it. Every URL below was opened on Sat Nov 7, 2026, and every page index was checked against the downloaded PDF (`check_plan.py --pages <folder outside the repo>`). Page numbers are 1-based indices in the PDF file, not the printed labels. In the FY2025 report they happen to match. In the FY2019 report they don't.

Base year: FY2026 (to Sep 30, 2026) from the Nov 5, 2026 results release, with full statements and notes from the FY2025 Annual Report, because the FY2026 Annual Report is only due Nov 26, 2026.

| Source | Tier, rank | Why it's in | How we found it | Pages that matter |
|---|---|---|---|---|
| S-001 Annual Report FY2025 | priority 1 | Latest audited statements and notes: the base for ROTIC, leverage, liquidity, R&D and market growth | Starting point in PLANS.md, also linked from the IR publications page | 7 R&D spend and patents. 8 market trends. 13 market development FY2025. 15 revenue by segment and region. 22 cash flows. 25 expected market development. 138 income statement. 140 balance sheet. 141 cash flow statement. 152 tax rate reconciliation (29.5%). 156 intangibles and their amortization. 159 financial debt with leases. 160 credit facilities and the loan table with maturities. 162 net debt and net debt/EBITDA. 169 dividend proposal. 177 maturity analysis. 182 segment table with free cash flow. 184 reconciliation with the purchase price allocation line (357). 189 subsequent events: Siemens AG deconsolidation and early termination of the Siemens Group loans |
| S-002 Q4 FY2026 earnings release and financial results | priority 2 | FY2026 figures, the base year | Linked as the full PDF from the press release page siemens-healthineers.com/press/releases/2026q4, which carries only the headline text | 1 headline figures, dividend, shareholder structure quote. 6 to 8 full-year development, new segments, tax rate, FY2027 outlook. 13 income statement. 15 balance sheet at Sep 30, 2026. 17 full-year cash flows. 19 EBITDA and purchase price allocation reconciliation |
| S-003 Annual Report FY2023 | priority 5 | ROTIC history: FY2023 and FY2022, balance sheets at Sep 30, 2023 and 2022 | IR publications page | 53 income statement. 55 balance sheet. 99 reconciliation with the purchase price allocation line |
| S-004 Annual Report FY2021 | priority 6 | ROTIC history: FY2021 and FY2020, with the Varian step in goodwill | IR publications page | 44 income statement. 46 balance sheet. 85 reconciliation with the purchase price allocation line |
| S-005 Annual Report FY2019 | priority 7 | ROTIC history back to listing: FY2019 and FY2018 | IR publications page | 62 income statement. 64 balance sheet. 95 reconciliation with amortization of intangibles acquired in business combinations |
| S-006 Reuters via Euronext, Nov 5, 2026 | tier_2 1 | Independent wire coverage of the FY2026 results (China, carve-out, slower FY2027). The demo revokes it, so it is fetched after every priority source | Web search for coverage of the FY2026 results. The same Reuters story is also on aol.com, a fallback if Euronext moves it | HTML, read whole |
| S-007 SEC company facts, GE HealthCare | priority 3 | Peer ROTIC inputs from tagged US GAAP 10-K data, read by code | SEC EDGAR, CIK 0001932393 | JSON, read by code |
| S-008 SEC company facts, Philips | priority 4 | Second peer, tagged IFRS 20-F data in euros, read by code | SEC EDGAR, CIK 0000313216 | JSON, read by code |
| S-009 Simply Wall St blog, Aug 5, 2026 | tier_3 1 | Low-trust commentary on the Q3 outlook cut and Diagnostics. Scanned before any agent reads it | Web search for blog or forum posts on SHL. Seeking Alpha answered 403 to our user agent and our search tool could not reach Reddit, so we took a blog that answers plain requests | HTML, read whole |
| S-010 Axelrod test page (simulated) | tier_3 2 | Our planted prompt injection, for the Guardrails tripwire and the blocked link in the demo. Not a real source | Written by us: `backend/data/test_pages/injection.html`, served from raw GitHub once the backend branch is pushed | HTML, quarantined |

36 extract pages in total: 19 from S-001, 8 from S-002 and 3 each from S-003 to S-005.

Notes for the run:
- The FY2019 and FY2021 PDFs carry soft hyphens, BEL control characters (U+0007) and thin spaces between a minus sign and its digits (`– 8,722`). Quotes and number parsing have to cope with them.
- data.sec.gov answered every GET with the default `HTTP_USER_AGENT`, but HEAD got an occasional 403. If the run gets a 403, Gordon sets `HTTP_USER_AGENT` with a contact.
- The research_stage criterion is about the analyst's own coverage history. No source can answer that, so N-07 brings R&D and outlook evidence and the coverage part stays a declared gap.
