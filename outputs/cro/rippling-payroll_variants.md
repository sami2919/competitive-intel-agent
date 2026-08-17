# Landing page variants — https://www.rippling.com/payroll

Page snapshot: 2026-07-27 · generated 2026-07-27

## Test plan

2 arms (1 control + 1) · baseline 3.00% · MDE +20% rel · alpha 0.05 power 80% → 13,911/arm at 50,000 sessions/arm/week → runnable in 2d (<= 28d budget)

## Ship

### VAR-002 — 62/100

- **Headline:** Cut hours of manual payroll work with AI
- **Subhead:** Gusto's simplicity targets solopreneurs and S-corp owners — built for where you started, not where you're headed.
- **CTA:** Learn more
- **Changes:** subhead
- **Segment:** 30-200 employee migration
- **Cites:** CAN-007, CAN-002
- **Provenance:** HYP-001 → counters CAN-002 (100/100)
- **Score trace:** message_match 12/25 (shares payroll) · specificity 0/25 (only 1 concrete referent(s): payroll — asserts nothing checkable) · length 20/20 (all within layout limits) · readability 15/15 (grade 7.3, in band) · segment_fit 15/15 (3/3 segment terms) → 62/100

## Rejected

- **VAR-001** (below_gate) — "Built for teams past the solopreneur stage"
  - message_match 0/25 (headline shares no content term with source claim) · specificity 0/25 (only 0 concrete referent(s) — asserts nothing checkable) · length 20/20 (all within layout limits) · readability 15/15 (grade 7.6, in band) · segment_fit 15/15 (3/3 segment terms) → 50/100 below gate 60
