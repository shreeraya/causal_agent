# Ground Truth — Causal Effects & Risk Storylines Baked into the Snapshots

`add_weekly_snapshots.py` simulates **77 weeks** per stocked (DC, SKU) pair:
weeks 0–51 (2025-07-07 → 2026-06-29) are observed history; "today" is
**2026-07-06 = start of week 52**; weeks 52–76 (→ 2026-12-21) are a
**deterministic projection** (`projected: true`) from the inventory balance
equation. The network is DC-terminal (no stores): regional DCs face external
customer demand. Use this file to validate the agent's answers.
**Don't feed this file to the agent** — it's the answer key.

## Planning mechanics (why future stockouts happen at all)

- Every (DC, SKU, week) has a `forecast_demand` (base × seasonality × planned
  promo lift) for all 77 weeks.
- Replenishment is order-up-to **2.2× forecast** (regional) / **2.5×**
  (central), and weekly receipts are capped by the committed supply plan at
  **ALLOC_FLEX = 1.15× forecast** — so demand persistently above ~115% of
  forecast cannot be resupplied fast enough.
- Projected demand = forecast × trailing 8-week demand attainment
  (weeks 44–51), so past overshoot propagates into future risk.
- `supply_factor` on each snapshot = fraction of planned receipts deliverable
  (< 1 ⇒ disruption on the lane); projected weeks have no receipt noise.
- `units_sold = min(demand, on_hand_start + units_received)` — lost sales;
  `stockout = demand > available`.

## 1. Promotion → demand ↑, price ↓

- ~35% of RDC-SKU pairs get 1–4 planned promo spells of 2–3 weeks across all
  77 weeks (`on_promotion`); promos are in the forecast (they're planned).
- True effect: demand ×**1.4**, price −**20%**.
- Measured (historical RDC weeks): avg units_sold 707.7 (promo) vs 514.9 ≈ +37%.
- DoWhy `estimate_effect('on_promotion','units_sold')`: ATE +202.9 (+39.4%),
  placebo refuter ≈ 0 ✓. Promo spells are random ⇒ naive ≈ causal.

## 2. Category seasonality (demand driver, potential confounder)

- Beverages ±25% peaking early July; Snacks ±20% peaking late December;
  Dairy ±5%; Personal Care / Home Care flat.
- December snack spike is seasonality, NOT promotions (trap question).

## 3. Disruption events (supply ↓ downstream, ~1 week lag per echelon)

Severity multiplier on receipts: high **0.45**, medium **0.65** (softened to
0.75 if multi-sourced); plant shutdown **0.2**. Lags: supplier event → plant +2,
central DC +1, regional DC +1 (strike: RDC +1 only).

| Event | What | Event weeks | RDC impact weeks | Affected | Measured fingerprint (RDC level) |
|---|---|---|---|---|---|
| EVT-004 | Sugar shortage SUP-02 (high) | 41–44 | 45–48 | 52 SKUs | DoWhy ATE on stockout +0.22 (control 0.13) |
| EVT-001 | Port congestion SUP-04 (high) | 43–45 | 47–49 | 52 SKUs | received dip weeks 47–49 |
| EVT-002 | Snacks plant shutdown (0.2) | 47 | 49 | 24 SKUs | week-49 received collapse |
| EVT-003 | Labor strike CDC-02 (medium) | 49 | 50 | all SKUs via RDC-04/05/06 | western vs eastern received gap wks 50 |
| **EVT-005** | **ONGOING** resin fire SUP-07 (high), started 2026-06-22 | 50–54 | **54–58 (projected)** | 74 SKUs | supply_factor 0.82 vs 1.0; projected stockout rate **31.6% vs 6.7%** (wks 53–59) |
| **EVT-006** | **ANNOUNCED** PLANT-01 maintenance, 2026-09-14 | 62–63 | **64–65 (projected)** | 48 SKUs (Bev+Dairy) | supply_factor 0.73; projected stockout rate **25.0% vs 8.3%** (wks 63–68) |

Projected stockout pairs per week: baseline ~40, EVT-005 hump 84→228 over
weeks 54–58, EVT-006 spike 107/253 in weeks 64–65.

## 4. Demand-overshoot storyline (S1 — future risk WITHOUT any disruption)

SKUs **SKU-0070, SKU-0077, SKU-0079** run at ~**125%** demand attainment from
week 40 onward (true demand = 1.25× forecast; all 6 RDCs; untouched by
EVT-005/006 so the story is purely demand-driven).

Measured (RDC level): recent attainment 1.22–1.27; historical stockouts began
around weeks 45–51 (warning signs); projected stockout pair-weeks 51–98 of a
possible 150, starting ~week 52–54. `explain_stockout_risk` names
`demand_overshoot` (+ `low_starting_inventory`) as drivers, e.g. SKU-0070 @
RDC-02: attainment 134%, 0.3 weeks of cover, first risk week 53 (2026-07-13).

Note: a further ~20 unengineered pairs stock out persistently in the projection
because their sampled trailing attainment exceeds the 115% plan flex — same
mechanism, weaker signal.

## 5. Causal engine implementation notes

- SCM (dowhy.gcm, fitted on historical RDC rows): seasonal_base & on_promotion
  → demand; seasonal_base, on_promotion, supply_factor, on_hand_start →
  units_received; units_sold/stockout derived exactly (min/threshold).
- `what_if` / `counterfactual` hold cross-week inventory carryover at observed
  values (single-week effects — stated in tool output).
- Verified: `what_if(supply_factor=1, projected weeks 53–59)` → stockout rate
  22.5% → 6.7%, demand unchanged; single-week counterfactual reproduces the
  1.15× forecast receipt cap learned from data.

## Example validation questions for the agent

- "What is the causal effect of promotions on units sold?" → ≈ +39%, placebo ≈ 0.
- "Is SKU-0070 at risk of stocking out? Why?" → yes, most RDCs, from ~mid-July;
  demand attainment ~125–134% vs 115% plan flex; NOT disruption-related.
- "Which SKUs will stock out in September and why?" → EVT-006-driven Beverages/
  Dairy spike weeks 64–65 + persistent S1 SKUs.
- "Would the projected week-56 stockouts on resin SKUs happen if the SUP-07
  fire were resolved?" → mostly no (counterfactual flips stockout for most).
- "Did the sugar shortage cause stockouts?" → yes, ATE ≈ +0.22 weeks 45–48.
- "Is the December snack sales spike caused by promotions?" → no; seasonality.
