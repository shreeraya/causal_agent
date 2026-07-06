"""
Add 52 weeks of periodic inventory/sales snapshots to the supply chain graph.

Creates (:WeeklySnapshot) nodes for every stocked (location, SKU) pair:

    (Store|DistributionCenter)-[:HAS_SNAPSHOT]->(s:WeeklySnapshot)-[:FOR_PRODUCT]->(Product)

    s: {loc_id, sku_id, week_start (date), week_index (0..51),
        demand, units_sold, units_received, on_hand_start, on_hand_end,
        stockout (bool), on_promotion (bool), price}

The weekly numbers come from a small multi-echelon simulation with REAL causal
structure baked in (documented in GROUND_TRUTH.md):

  1. Promotions lift demand ~x1.4 at a 20% discount (time-varying promo spells).
  2. Category seasonality (beverages peak in summer, snacks around holidays).
  3. The four DisruptionEvents reduce inbound supply along their affected lanes,
     propagating downstream with a ~1 week lag per echelon:
     plant -> central DC -> regional DC -> store.
  4. Stockouts cause lost sales: units_sold = min(demand, available).

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
AUTH = (os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", "supplychain123"))

random.seed(42)

N_WEEKS = 52
WEEK0 = date(2025, 7, 7)  # Monday; horizon ends 2026-06-29
WEEKS = [WEEK0 + timedelta(weeks=i) for i in range(N_WEEKS)]

PROMO_LIFT = 1.4
PROMO_DISCOUNT = 0.20
SEVERITY_MULT = {"high": 0.45, "medium": 0.65}
MULTI_SOURCE_MULT = 0.75  # softened impact when the material has other suppliers
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
            MATCH (st:Store)-[sl:SELLS]->(p:Product)-[:IN_CATEGORY]->(c:Category)
            RETURN st.store_id AS store, p.sku_id AS sku, p.list_price AS list_price,
                   sl.avg_weekly_demand AS base, sl.demand_std AS std, c.name AS category
        """).data()
        chain = s.run("""
            MATCH (cdc:DistributionCenter {tier:'central'})-[:SHIPS_TO]->
                  (rdc:DistributionCenter {tier:'regional'})-[:SHIPS_TO]->(st:Store)
            RETURN st.store_id AS store, rdc.dc_id AS rdc, cdc.dc_id AS cdc
        """).data()
        dc_stock = s.run("""
            MATCH (d:DistributionCenter)-[:STOCKS]->(p:Product)-[:IN_CATEGORY]->(c:Category)
            RETURN d.dc_id AS dc, d.tier AS tier, p.sku_id AS sku, c.name AS category
        """).data()
        rdc_parent = {r["rdc"]: r["cdc"] for r in chain}
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
    return sells, chain, dc_stock, rdc_parent, sku_plant, bom, events


# ------------------------------------------------- supply factors (causal core)

def build_supply_factors(sku_ids, sku_plant, bom, events, rdc_parent):
    """factor arrays in [0,1] per echelon: fraction of ordered qty that arrives."""
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
    store_lane_f = {(r, k): lag(v, 1) for (r, k), v in rdc_f.items()}
    return plant_f, cdc_f, rdc_f, store_lane_f, affected_summary


# ---------------------------------------------------------------- simulation

def promo_schedule():
    """Time-varying promo spells: ~35% of store-SKU pairs get 1-3 spells of 2-3 weeks."""
    if random.random() > 0.35:
        return set()
    weeks = set()
    for _ in range(random.randint(1, 3)):
        start = random.randint(0, N_WEEKS - 3)
        weeks.update(range(start, start + random.randint(2, 3)))
    return weeks


def simulate_pair(base, std, category, price, factor_arr, promo_weeks, target_mult):
    """Weekly order-up-to inventory sim for one (location, SKU). Returns row dicts."""
    rows, on_hand = [], int(base * 2)
    for w, wk in enumerate(WEEKS):
        promo = w in promo_weeks
        expected = base * seasonality(category, wk) * (PROMO_LIFT if promo else 1.0)
        demand = max(0, round(random.gauss(expected, std)))
        target = round(expected * target_mult)
        order = max(0, target - on_hand)
        received = round(order * factor_arr[w] * random.uniform(0.9, 1.0))
        available = on_hand + received
        sold = min(demand, available)
        rows.append({
            "wi": w, "week": wk.isoformat(), "demand": demand, "sold": sold,
            "recv": received, "oh0": on_hand, "oh1": available - sold,
            "stockout": demand > available, "promo": promo,
            "price": round(price * (1 - PROMO_DISCOUNT), 2) if promo else price,
        })
        on_hand = available - sold
    return rows


def run(driver):
    sells, chain, dc_stock, rdc_parent, sku_plant, bom, events = fetch(driver)
    sku_ids = sorted(sku_plant)
    plant_f, cdc_f, rdc_f, store_lane_f, summary = build_supply_factors(
        sku_ids, sku_plant, bom, events, rdc_parent)

    print("Disruption fingerprints baked in:")
    for eid, etype, n, ws, we in summary:
        print(f"  {eid} ({etype}): affects {n} SKUs, event weeks {ws}-{we} (+downstream lags)")

    store_rdc = {r["store"]: r["rdc"] for r in chain}

    # store-level snapshots
    store_rows, last_week_promo = [], []
    for r in sells:
        lane = store_lane_f[(store_rdc[r["store"]], r["sku"])]
        promo_wks = promo_schedule()
        for row in simulate_pair(r["base"], r["std"], r["category"], r["list_price"],
                                 lane, promo_wks, target_mult=2.2):
            store_rows.append({**row, "loc": r["store"], "sku": r["sku"]})
        last_week_promo.append({"store": r["store"], "sku": r["sku"],
                                "promo": (N_WEEKS - 1) in promo_wks})

    # DC-level snapshots: demand = sum of downstream store demand for that SKU
    downstream = {}
    for r in sells:
        rdc = store_rdc[r["store"]]
        cdc = rdc_parent[rdc]
        downstream[(rdc, r["sku"])] = downstream.get((rdc, r["sku"]), 0) + r["base"]
        downstream[(cdc, r["sku"])] = downstream.get((cdc, r["sku"]), 0) + r["base"]

    dc_rows = []
    for r in dc_stock:
        base = downstream.get((r["dc"], r["sku"]), 50)
        factors = cdc_f[(r["dc"], r["sku"])] if r["tier"] == "central" else rdc_f[(r["dc"], r["sku"])]
        for row in simulate_pair(base, base * 0.2, r["category"], 0.0,
                                 factors, set(), target_mult=2.5):
            row["price"] = None
            dc_rows.append({**row, "loc": r["dc"], "sku": r["sku"]})

    # ------------------------------------------------------------- write
    with driver.session() as s:
        s.run("CREATE INDEX snap_week IF NOT EXISTS FOR (x:WeeklySnapshot) ON (x.week_start)")
        s.run("CREATE INDEX snap_loc_sku IF NOT EXISTS FOR (x:WeeklySnapshot) ON (x.loc_id, x.sku_id)")
        # idempotent rebuild, batched delete
        while s.run("""
            MATCH (x:WeeklySnapshot) WITH x LIMIT 20000 DETACH DELETE x
            RETURN count(*) AS c""").single()["c"]:
            pass

        def write(rows, match_clause, label):
            q = f"""
                UNWIND $rows AS r
                MATCH {match_clause}, (p:Product {{sku_id: r.sku}})
                CREATE (x:WeeklySnapshot {{loc_id: r.loc, sku_id: r.sku,
                    week_start: date(r.week), week_index: r.wi,
                    demand: r.demand, units_sold: r.sold, units_received: r.recv,
                    on_hand_start: r.oh0, on_hand_end: r.oh1,
                    stockout: r.stockout, on_promotion: r.promo, price: r.price}})
                CREATE (loc)-[:HAS_SNAPSHOT]->(x)
                CREATE (x)-[:FOR_PRODUCT]->(p)
            """
            for i in range(0, len(rows), BATCH):
                s.run(q, rows=rows[i:i + BATCH])
            print(f"  wrote {len(rows):,} {label} snapshots")

        print("Writing snapshots...")
        write(store_rows, "(loc:Store {store_id: r.loc})", "store")
        write(dc_rows, "(loc:DistributionCenter {dc_id: r.loc})", "DC")

        # keep the static SELLS promo flag consistent with the latest week
        s.run("""
            UNWIND $rows AS r
            MATCH (st:Store {store_id: r.store})-[sl:SELLS]->(p:Product {sku_id: r.sku})
            SET sl.on_promotion = r.promo,
                sl.price = CASE WHEN r.promo THEN round(p.list_price * (1 - $d), 2)
                                ELSE p.list_price END
        """, rows=last_week_promo, d=PROMO_DISCOUNT)

        total = s.run("MATCH (x:WeeklySnapshot) RETURN count(*) AS c").single()["c"]
    print(f"Total WeeklySnapshot nodes: {total:,} "
          f"({WEEKS[0]} .. {WEEKS[-1]}, {N_WEEKS} weeks)")


if __name__ == "__main__":
    driver = GraphDatabase.driver(URI, auth=AUTH)
    driver.verify_connectivity()
    run(driver)
    driver.close()
