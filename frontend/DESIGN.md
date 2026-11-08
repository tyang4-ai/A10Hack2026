# Axelrod dashboard: design system

The dashboard is an analyst's desk for checking AI research. It should feel like a trading terminal wrapped around a research document: dark, quiet chrome for live machinery (gateway, agents, inspector), and a white page for the things people read (sources, the agent report, the brief).

## 1. Theme
- **Two surfaces, two jobs.** Dark slate for live system state. White paper for reading and deciding.
- **One loud thing.** The only saturated colour that is not a status is amber, used for the brand mark, the "waiting for you" state and the selected tab. Everything else is neutral until it means something.
- **Evidence over decoration.** Rules, borders and numbering only appear where they encode structure: a table, a sequence (the pipeline), a chain of citations.

## 2. Colour
| Token | Hex | Use |
|---|---|---|
| `desk` | `#0F172A` | Dark chrome background (top bar, inspector, drawer) |
| `desk-raised` | `#16213A` | Hover and selected rows on dark |
| `desk-line` | `#1E293B` | Hairlines on dark (depth comes from lines, never shadows) |
| `desk-ink` | `#E2E8F0` | Text on dark |
| `desk-muted` | `#94A3B8` | Secondary text on dark |
| `paper` | `#FFFFFF` | Reading surface |
| `ink` | `#0F172A` | Text on paper |
| `muted` | `#475569` | Secondary text on paper (7:1) |
| `rule` | `#E2E8F0` | Hairlines on paper |
| `zebra` | `#F8FAFC` | Selected or hovered rows on paper |
| `amber` | `#F59E0B` | Brand, awaiting approval, active tab |
| `good` / `desk-good` | `#15803D` / `#4ADE80` | Pass, allowed, extracted |
| `bad` / `desk-bad` | `#B91C1C` / `#F87171` | Fail, blocked, revoked, stop |
| `warn` / `desk-warn` | `#B45309` / `#FBBF24` | Flagged, fixed-with-warning, low certainty |
| `link` | `#1D4ED8` | Citations and references |

Certainty bands (brief shading, light so text stays 7:1): high `#DCFCE7`, medium `#FEF3C7`, low `#FEE2E2`, none `#F1F5F9`.

Status is never colour alone: every coloured state also has a word (PASS, BLOCKED) or a mark (✓ ✕ !).

## 3. Typography
| Role | Family | Size / weight |
|---|---|---|
| UI (labels, buttons, chrome) | IBM Plex Sans | 13px / 400–600 |
| Numbers, IDs, code, traffic | IBM Plex Mono, `tabular-nums` | 12–13px |
| Documents (report items, brief) | KaTeX_Main (Computer Modern) | 15–16px, line-height 1.6 |
| Page title | IBM Plex Sans | 22px / 600 |
| Section title | IBM Plex Sans | 15px / 600 |

- Plex Sans and Plex Mono share a skeleton, so the UI reads as one voice; the serif marks "this is the document".
- Labels are sentence case. No tracked all-caps eyebrows, no middle-dot meta strings; put metadata in its own columns or chips.
- Reading text stays under 80 characters per line.

## 4. Components
- **Buttons:** 32px tall (24px minimum for inline row actions), 4px radius, sentence case. Primary = ink fill. Destructive actions (Stop, Revoke) need a second click to confirm.
- **Tables:** hairline rows, right-aligned numbers, `tabular-nums`, header in muted 12px sans.
- **Status:** a word in its status colour, 11px semibold mono.
- **Certainty:** the number in its band colour, with the band word available on hover and in the inspector.
- **Charts:** direct value labels, axis from zero (or a marked break), a title and an accessible text alternative listing every point.

## 5. Layout
- Fixed shell: top bar, module rail (sequence 1 → 3), workspace, inspector (360px), bottom drawer (traffic, agents, vault, log).
- Spacing on a 4px base: 4, 8, 12, 16, 24, 32.
- Numbered markers only where order is real: the pipeline stages and the three modules.

## 6. Depth
No drop shadows in the app. Separation comes from surface colour (paper vs desk) and 1px lines. The brief page gets a single 1px outline so it reads as a printable sheet.

## 7. Motion
- Only motion that reports something: a new traffic row slides in, working agents pulse, the stage stepper fills. Nothing animates on page or panel entry.
- Respect `prefers-reduced-motion`.

## 8. Accessibility
- Visible 2px focus ring (`#3B82F6`) on every control.
- Text contrast at least 4.5:1, body text 7:1 where possible.
- Every input has a label; errors use `role="alert"` and say what happened.
- Loading uses stable skeletons with `aria-busy`, not spinners.

## 9. Don'ts
- No gradients, glass, or soft card shadows.
- No hover-only actions; anything on hover is also reachable by click or focus.
- Never show JSON to the analyst.
- Never merge certainty and coverage into one number.
