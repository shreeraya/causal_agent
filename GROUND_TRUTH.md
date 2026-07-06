# Ground Truth — Causal Effects Baked into the Weekly Snapshots

`add_weekly_snapshots.py` simulates 52 weeks (2025-07-07 → 2026-06-29) of weekly
snapshots per stocked (location, SKU) pair. The generator plants these causal
mechanisms — use them to validate whether the agent's causal answers are correct.
**Don't feed this file to the agent**; it's the answer key.

## 1. Promotion → demand ↑, price ↓

- ~35% of store-SKU pairs get 1–3 promo spells of 2–3 weeks (`on_promotion` on the snapshot).
- True effect: demand multiplied by **1.4**; price discounted **20%**.
- Verified in data: avg units_sold 239.7 (promo) vs 172.0 (non-promo) ≈ +39%.
- Confounder-free by construction (promo weeks assigned at random), so naive
  comparison ≈ causal effect. A correct agent should find ~+40% lift.

## 2. Category seasonality (demand driver, potential confounder)

- Beverages: ±25% sinusoid peaking early July.
- Snacks: ±20% peaking late December.
- Dairy: ±5% peaking July. Personal Care / Home Care: flat.

## 3. Disruption events → supply ↓ downstream, with ~1 week lag per echelon

Supply mechanics: each location orders up-to-target weekly; a disruption multiplies
the *received* quantity on affected lanes. Propagation lags (weeks after event start):
plant +2 for supplier events (material transit), then central DC +1, regional DC +1, store +1.
Multiplier: severity high → **0.45**, medium → **0.65**; softened to 0.75 if the
raw material is multi-sourced. Stockouts = demand > available → lost sales.

| Event | What | Event weeks (index) | Affected | Store-level impact window |
|---|---|---|---|---|
| EVT-004 | Sugar shortage (SUP-02, high) | 41–44 | 39 SKUs via BOM | ~weeks 46–49: stockout rate doubles (0.009 → 0.018) |
| EVT-001 | Port congestion (SUP-04, high) | 43–45 | 53 SKUs via packaging materials | ~weeks 48–50 |
| EVT-002 | Plant shutdown (PLANT-02, med → mult 0.2) | 47 | 24 snack SKUs | ~week 50 |
| EVT-003 | Labor strike (CDC-02, medium) | 49 | all SKUs in western network (RDC-04/05/06 stores) | weeks 50–51: stockout 0.085 vs 0.064 in eastern network; received 159 vs 199 |

`week_index` 0 = 2025-07-07; the event `start_date` on the DisruptionEvent node is the source of truth.

## 4. Inventory dynamics (non-causal mechanics)

- Order-up-to policy: target = 2.2× expected weekly demand (stores), 2.5× (DCs).
- `units_sold = min(demand, on_hand_start + units_received)` — lost sales, no backorders.
- Received qty noise: ×U(0.9, 1.0) even without disruptions.

## Example validation questions for the agent

- "Did the sugar shortage cause stockouts in stores? How large was the effect and with what lag?"
  → Yes; affected-SKU store stockout rate roughly doubles ~5 weeks after event start; unaffected SKUs flat (clean diff-in-diff).
- "What is the causal effect of promotions on units sold?" → ≈ +40%.
- "Why did western stores receive less inventory in late June 2026?" → CDC-02 labor strike (EVT-003), not demand.
- "Is the December snack sales spike caused by promotions?" → No; seasonality (trap question).
