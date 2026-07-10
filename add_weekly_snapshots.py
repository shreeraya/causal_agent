"""
Add 77 weeks of inventory/sales snapshots to the DC-terminal supply chain graph:
52 historical weeks + 25 projected future weeks.

Creates (:WeeklySnapshot) nodes for every stocked (DC, SKU) pair:

    (DistributionCenter)-[:HAS_SNAPSHOT]->(s:WeeklySnapshot)-[:FOR_PRODUCT]->(Product)

    s: {loc_id, sku_id, week_start (date), week_index (0..76),
        demand, forecast_demand, units_sold, units_received,
        on_hand_start, on_hand_end, stockout (bool), on_promotion (bool),
        price, supply_factor, projected (bool)}

Timeline: week_index 0 = 2025-07-07 ... 51 = 2026-06-29 (history),
"today" = 2026-07-06 = start of week 52, weeks 52..76 are PROJECTED
(projected: true) via a deterministic inventory balance:

    on_hand_end = on_hand_start + units_received - units_sold
    units_sold  = min(demand, on_hand_start + units_received)
    stockout    = demand > on_hand_start + units_received

Causal structure baked in (answer key in GROUND_TRUTH.md):

  1. Promotions lift demand x1.4 at a 20% discount. Promo spells are PLANNED,
     so they appear in forecast_demand too (past and future weeks).
  2. Category seasonality (beverages peak in summer, snacks around holidays).
  3. DisruptionEvents cut inbound supply on affected lanes, propagating with
     ~1 week lag per echelon: plant -> central DC -> regional DC.
     EVT-005 is ONGOING (started 2 weeks ago) and EVT-006 is ANNOUNCED for
     September — both hit mostly the projected weeks.
  4. Replenishment plans to FORECAST: weekly receipts are capped at
     ALLOC_FLEX x forecast (the committed supply plan). A handful of
     "overshoot" SKUs run ~25% above forecast from week 40 on (demand
     attainment ~125%), eroding inventory -> projected stockouts even
     without any disruption.
  5. Projected demand = forecast x trailing 8-week demand attainment, so past
     demand overshoot propagates into future risk.

Idempotent: deletes existing WeeklySnapshot nodes, then rebuilds (seed=42).
Run:  python add_weekly_snapshots.py
"""

import os
import random
from datetime import date, timedelta
from pathlib import Path
from neo4j import GraphDatabase

# connection from .env / environment, defaulting to the local instance
env_file = Path(__file__).parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
AUTH = (os.environ.get("NEO4J_USERNAME") or os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", ""))  # set NEO4J_PASSWORD in .env

random.seed(42)

N_HIST = 52                    # historical weeks: index 0..51
N_FUTURE = 25                  # projected weeks: index 52..76
N_WEEKS = N_HIST + N_FUTURE
WEEK0 = date(2025, 7, 7)       # Monday; week 52 starts 2026-07-06 = "today"
WEEKS = [WEEK0 + timedelta(weeks=i) for i in range(N_WEEKS)]

PROMO_LIFT = 1.4
PROMO_DISCOUNT = 0.20
SEVERITY_MULT = {"high": 0.45, "medium": 0.65}
MULTI_SOURCE_MULT = 0.75   # softened impact when the material has other suppliers
ALLOC_FLEX = 1.15          # supply plan committed to forecast: receipts <= 1.15x forecast
ATTAIN_WINDOW = 8          # trailing weeks used to extrapolate demand attainment
OVERSHOOT_START = 40       # overshoot SKUs run above forecast from this week on
OVERSHOOT_MULT = 1.25      # their true demand = 1.25x forecast (attainment ~125%)
N_OVERSHOOT = 3            # engineered "demand overshoot" SKUs (storyline S1)
BATCH = 5000


def week_of(d: date) -> int:
    return (d - WEEK0).days // 7


def seasonality(category: str, week_start: date) -> float:
    import math
    doy = week_start.timetuple().tm_yday
    if category == "Beverages":     # peaks early July
        return 1 + 0.25 * math.cos(2 * math.pi * (doy - 190) / 365)
    if category == "Snacks":        # peaks late December
        return 1 + 0.20 * math.cos(2 * math.pi * (doy - 355) / 365)
    if category == "Dairy":
        return 1 + 0.05 * math.cos(2 * math.pi * (doy - 190) / 365)
    return 1.0


# ---------------------------------------------------------------- graph reads

def fetch(driver):
    with driver.session() as s:
        sells = s.run("""
            MATCH (d:DistributionCenter {tier:'regional'})-[sl:SELLS]->(p:Product)
                  -[:IN_CATEGORY]->(c:Category)
            RETURN d.dc_id AS rdc, p.sku_id AS sku, p.list_price AS list_price,
                   sl.avg_weekly_demand AS base, sl.demand_std AS std, c.name AS category
        """).data()
        rdc_parent = {r["rdc"]: r["cdc"] for r in s.run("""
            MATCH (cdc:DistributionCenter {tier:'central'})-[:SHIPS_TO]->
                  (rdc:DistributionCenter {tier:'regional'})
            RETURN rdc.dc_id AS rdc, cdc.dc_id AS cdc
        """)}
        cdc_stock = s.run("""
            MATCH (d:DistributionCenter {tier:'central'})-[:STOCKS]->(p:Product)
                  -[:IN_CATEGORY]->(c:Category)
            RETURN d.dc_id AS dc, p.sku_id AS sku, c.name AS category
        """).data()
        sku_plant = {r["sku"]: r["plant"] for r in s.run(
            "MATCH (pl:Plant)-[:PRODUCES]->(p:Product) RETURN p.sku_id AS sku, pl.plant_id AS plant")}
        # per SKU: which suppliers feed each BOM material (to spot sole-sourcing)
        bom = s.run("""
            MATCH (p:Product)-[:MADE_FROM]->(m:RawMaterial)<-[:SUPPLIES]-(sup:Supplier)
            RETURN p.sku_id AS sku, m.name AS material, collect(sup.supplier_id) AS suppliers
        """).data()
        events = s.run("""
            MATCH (e:DisruptionEvent)-[:AFFECTS]->(n)
            RETURN e.event_id AS id, e.type AS type, e.severity AS severity,
                   toString(e.start_date) AS start, e.duration_days AS days,
                   coalesce(n.supplier_id, n.plant_id, n.dc_id) AS target
        """).data()
    return sells, rdc_parent, cdc_stock, sku_plant, bom, events


# ------------------------------------------------- supply factors (causal core)

def build_supply_factors(sku_ids, sku_plant, bom, events, rdc_parent):
    """factor arrays in [0,1] per echelon: fraction of planned qty that arrives."""
    plant_f = {k: [1.0] * N_WEEKS for k in sku_ids}
    affected_summary = []

    bom_by_sku = {}
    for r in bom:
        bom_by_sku.setdefault(r["sku"], []).append(r)

    for e in events:
        ws = week_of(date.fromisoformat(e["start"]))
        we = ws + max(1, round(e["days"] / 7)) - 1
        mult = SEVERITY_MULT[e["severity"]]

        if e["type"] in ("raw_material_shortage", "port_congestion"):
            # supplier event: hits plants ~2 weeks later (inbound material transit)
            hit = []
            for sku, rows in bom_by_sku.items():
                for r in rows:
                    if e["target"] in r["suppliers"]:
                        m = mult if len(r["suppliers"]) == 1 else max(mult, MULTI_SOURCE_MULT)
                        for w in range(max(0, ws + 2), min(N_WEEKS, we + 3)):
                            plant_f[sku][w] = min(plant_f[sku][w], m)
                        hit.append(sku)
                        break
            affected_summary.append((e["id"], e["type"], len(set(hit)), ws, we))
        elif e["type"] == "plant_shutdown":
            hit = [k for k in sku_ids if sku_plant[k] == e["target"]]
            for sku in hit:
                for w in range(max(0, ws), min(N_WEEKS, we + 1)):
                    plant_f[sku][w] = min(plant_f[sku][w], 0.2)
            affected_summary.append((e["id"], e["type"], len(hit), ws, we))

    def lag(arr, n):
        return [1.0] * n + arr[:-n] if n else arr[:]

    # strike at a central DC throttles its OUTBOUND to regional DCs
    strike_out = {c: [1.0] * N_WEEKS for c in set(rdc_parent.values())}
    for e in events:
        if e["type"] == "labor_strike":
            ws = week_of(date.fromisoformat(e["start"]))
            we = ws + max(1, round(e["days"] / 7)) - 1
            for w in range(max(0, ws), min(N_WEEKS, we + 1)):
                strike_out[e["target"]][w] = SEVERITY_MULT[e["severity"]]
            affected_summary.append((e["id"], e["type"], "all SKUs via " + e["target"], ws, we))

    cdc_f = {(c, k): lag(plant_f[k], 1) for c in strike_out for k in sku_ids}
    rdc_f = {(r, k): [a * b for a, b in zip(lag(cdc_f[(p, k)], 1), lag(strike_out[p], 1))]
             for r, p in rdc_parent.items() for k in sku_ids}
    return plant_f, cdc_f, rdc_f, affected_summary


def pick_overshoot_skus(sells, sku_plant, bom, events):
    """S1 storyline: SKUs stocked at all 6 RDCs and untouched by any disruption
    that reaches the PROJECTED weeks, so their future risk is purely
    demand-driven (clean answer key). Historical-only events are tolerated."""
    from collections import Counter
    rdc_count = Counter(r["sku"] for r in sells)
    exposed = set()
    future_events = [e for e in events
                     if week_of(date.fromisoformat(e["start"]))
                     + max(1, round(e["days"] / 7)) + 4 >= N_HIST]  # impact reaches week 52+
    supplier_targets = {e["target"] for e in future_events
                        if e["type"] in ("raw_material_shortage", "port_congestion")}
    plant_targets = {e["target"] for e in future_events if e["type"] == "plant_shutdown"}
    for r in bom:
        if supplier_targets & set(r["suppliers"]):
            exposed.add(r["sku"])
    exposed |= {k for k, pl in sku_plant.items() if pl in plant_targets}
    for min_rdcs in (6, 5, 4):
        clean = sorted(k for k, c in rdc_count.items() if c >= min_rdcs and k not in exposed)
        if len(clean) >= N_OVERSHOOT:
            return clean[:N_OVERSHOOT]
    return clean[:N_OVERSHOOT]


# ---------------------------------------------------------------- simulation

def promo_schedule():
    """Planned promo spells over ALL 77 weeks: ~35% of RDC-SKU pairs get 1-4
    spells of 2-3 weeks (future spells are already planned -> in the forecast)."""
    if random.random() > 0.35:
        return set()
    weeks = set()
    for _ in range(random.randint(1, 4)):
        start = random.randint(0, N_WEEKS - 3)
        weeks.update(range(start, start + random.randint(2, 3)))
    return weeks


def simulate_pair(base, std, category, price, factor_arr, promo_weeks,
                  target_mult, overshoot=False):
    """Weekly order-up-to inventory sim for one (DC, SKU) over 77 weeks.

    Weeks 0..51: stochastic history. Weeks 52..76: deterministic projection —
    demand = forecast x trailing attainment, no receipt noise.
    Replenishment always plans to FORECAST, and receipts are capped by the
    committed supply plan (ALLOC_FLEX x forecast).
    """
    rows, on_hand = [], int(base * 2)
    attain_ratios, attain_hat = [], 1.0
    for w, wk in enumerate(WEEKS):
        promo = w in promo_weeks
        forecast = max(1, round(base * seasonality(category, wk)
                                * (PROMO_LIFT if promo else 1.0)))
        if w < N_HIST:
            true_mult = OVERSHOOT_MULT if (overshoot and w >= OVERSHOOT_START) else 1.0
            demand = max(0, round(random.gauss(forecast * true_mult, std)))
            noise = random.uniform(0.9, 1.0)
            attain_ratios.append(demand / forecast)
        else:
            if w == N_HIST:  # extrapolate demand attainment from recent history
                recent = attain_ratios[-ATTAIN_WINDOW:]
                attain_hat = min(1.8, max(0.5, sum(recent) / len(recent)))
            demand = round(forecast * attain_hat)
            noise = 1.0

        target = round(forecast * target_mult)          # plans to forecast
        order = max(0, target - on_hand)
        plan_cap = round(forecast * ALLOC_FLEX)         # committed supply plan
        received = round(min(order, plan_cap) * factor_arr[w] * noise)
        available = on_hand + received
        sold = min(demand, available)
        rows.append({
            "wi": w, "week": wk.isoformat(), "demand": demand, "fc": forecast,
            "sold": sold, "recv": received, "oh0": on_hand, "oh1": available - sold,
            "stockout": demand > available, "promo": promo,
            "price": round(price * (1 - PROMO_DISCOUNT), 2) if promo else price,
            "factor": round(factor_arr[w], 3), "projected": w >= N_HIST,
        })
        on_hand = available - sold
    return rows


def run(driver):
    sells, rdc_parent, cdc_stock, sku_plant, bom, events = fetch(driver)
    sku_ids = sorted(sku_plant)
    plant_f, cdc_f, rdc_f, summary = build_supply_factors(
        sku_ids, sku_plant, bom, events, rdc_parent)
    overshoot_skus = set(pick_overshoot_skus(sells, sku_plant, bom, events))

    print("Disruption fingerprints baked in:")
    for eid, etype, n, ws, we in summary:
        pad = 1 if etype == "labor_strike" else 4   # echelon propagation reach
        tag = " [ONGOING]" if ws < N_HIST <= we + pad else (" [FUTURE]" if ws >= N_HIST else "")
        print(f"  {eid} ({etype}): affects {n} SKUs, event weeks {ws}-{we} (+downstream lags){tag}")
    print(f"Demand-overshoot SKUs (attainment ~{OVERSHOOT_MULT:.0%} from week "
          f"{OVERSHOOT_START}): {sorted(overshoot_skus)}")

    # regional DC snapshots: external customer demand, promos, prices
    rdc_rows, last_week_promo = [], []
    for r in sells:
        promo_wks = promo_schedule()
        for row in simulate_pair(r["base"], r["std"], r["category"], r["list_price"],
                                 rdc_f[(r["rdc"], r["sku"])], promo_wks,
                                 target_mult=2.2, overshoot=r["sku"] in overshoot_skus):
            rdc_rows.append({**row, "loc": r["rdc"], "sku": r["sku"]})
        last_week_promo.append({"dc": r["rdc"], "sku": r["sku"],
                                "promo": (N_HIST - 1) in promo_wks})

    # central DC snapshots: demand = downstream regional demand for that SKU
    downstream = {}
    for r in sells:
        cdc = rdc_parent[r["rdc"]]
        downstream[(cdc, r["sku"])] = downstream.get((cdc, r["sku"]), 0) + r["base"]

    cdc_rows = []
    for r in cdc_stock:
        base = max(50, downstream.get((r["dc"], r["sku"]), 50))
        for row in simulate_pair(base, base * 0.2, r["category"], 0.0,
                                 cdc_f[(r["dc"], r["sku"])], set(),
                                 target_mult=2.5, overshoot=r["sku"] in overshoot_skus):
            row["price"] = None
            cdc_rows.append({**row, "loc": r["dc"], "sku": r["sku"]})

    # ------------------------------------------------------------- write
    with driver.session() as s:
        s.run("CREATE INDEX snap_week IF NOT EXISTS FOR (x:WeeklySnapshot) ON (x.week_start)")
        s.run("CREATE INDEX snap_loc_sku IF NOT EXISTS FOR (x:WeeklySnapshot) ON (x.loc_id, x.sku_id)")
        # idempotent rebuild, batched delete
        while s.run("""
            MATCH (x:WeeklySnapshot) WITH x LIMIT 20000 DETACH DELETE x
            RETURN count(*) AS c""").single()["c"]:
            pass

        def write(rows, label):
            q = """
                UNWIND $rows AS r
                MATCH (loc:DistributionCenter {dc_id: r.loc}), (p:Product {sku_id: r.sku})
                CREATE (x:WeeklySnapshot {loc_id: r.loc, sku_id: r.sku,
                    week_start: date(r.week), week_index: r.wi,
                    demand: r.demand, forecast_demand: r.fc,
                    units_sold: r.sold, units_received: r.recv,
                    on_hand_start: r.oh0, on_hand_end: r.oh1,
                    stockout: r.stockout, on_promotion: r.promo, price: r.price,
                    supply_factor: r.factor, projected: r.projected})
                CREATE (loc)-[:HAS_SNAPSHOT]->(x)
                CREATE (x)-[:FOR_PRODUCT]->(p)
            """
            for i in range(0, len(rows), BATCH):
                s.run(q, rows=rows[i:i + BATCH])
            print(f"  wrote {len(rows):,} {label} snapshots")

        print("Writing snapshots...")
        write(rdc_rows, "regional DC")
        write(cdc_rows, "central DC")

        # keep the static SELLS promo flag consistent with the latest ACTUAL week
        s.run("""
            UNWIND $rows AS r
            MATCH (d:DistributionCenter {dc_id: r.dc})-[sl:SELLS]->(p:Product {sku_id: r.sku})
            SET sl.on_promotion = r.promo,
                sl.price = CASE WHEN r.promo THEN round(p.list_price * (1 - $d), 2)
                                ELSE p.list_price END
        """, rows=last_week_promo, d=PROMO_DISCOUNT)

        total = s.run("MATCH (x:WeeklySnapshot) RETURN count(*) AS c").single()["c"]
        nproj = s.run("MATCH (x:WeeklySnapshot {projected:true}) RETURN count(*) AS c").single()["c"]
    print(f"Total WeeklySnapshot nodes: {total:,} ({WEEKS[0]} .. {WEEKS[-1]}, "
          f"{N_HIST} historical + {N_FUTURE} projected weeks; {nproj:,} projected)")


if __name__ == "__main__":
    driver = GraphDatabase.driver(URI, auth=AUTH)
    driver.verify_connectivity()
    run(driver)
    driver.close()
