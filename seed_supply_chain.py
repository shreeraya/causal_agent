"""
Seed a multi-echelon supply chain graph into Neo4j.

Topology (4 echelons):
    Suppliers -> Plants -> Central DCs -> Regional DCs -> Retail Stores

Ontology:
    (:Supplier)         raw material vendors
    (:RawMaterial)      inputs to manufacturing
    (:Plant)            factories producing finished SKUs
    (:DistributionCenter {tier: 'central'|'regional'})
    (:Store)            retail endpoints with demand
    (:Product)          finished SKUs (120 of them)
    (:Category)         product categories
    (:DisruptionEvent)  supply disruptions (for causal questions)

Relationships:
    (Supplier)-[:SUPPLIES {cost_per_unit, reliability}]->(RawMaterial)
    (Supplier)-[:SHIPS_TO {lead_time_days, transport_mode}]->(Plant)
    (Product)-[:MADE_FROM {qty_per_unit}]->(RawMaterial)
    (Plant)-[:PRODUCES {capacity_per_week, unit_cost}]->(Product)
    (Plant)-[:SHIPS_TO {lead_time_days}]->(DC central)
    (DC central)-[:SHIPS_TO {lead_time_days}]->(DC regional)
    (DC regional)-[:SHIPS_TO {lead_time_days}]->(Store)
    (DC|Store)-[:STOCKS {on_hand, safety_stock, reorder_point, fill_rate, stockout_events_90d}]->(Product)
    (Store)-[:SELLS {avg_weekly_demand, demand_std, price, on_promotion}]->(Product)
    (Product)-[:IN_CATEGORY]->(Category)
    (DisruptionEvent)-[:AFFECTS]->(Supplier|Plant|DC)

Run:  python seed_supply_chain.py
"""

import random
from neo4j import GraphDatabase

URI = "bolt://localhost:7687"
AUTH = ("neo4j", "supplychain123")

random.seed(42)

# ---------------------------------------------------------------- master data

CATEGORIES = ["Beverages", "Snacks", "Personal Care", "Home Care", "Dairy"]

RAW_MATERIALS = [
    "Sugar", "Corn Syrup", "Citric Acid", "Carbonated Water", "Tea Extract",
    "Wheat Flour", "Potato Flakes", "Vegetable Oil", "Salt", "Seasoning Mix",
    "Surfactant", "Fragrance Oil", "Glycerin", "Sodium Hydroxide", "Essential Oils",
    "Bleach Concentrate", "Enzymes", "Polymer Resin", "Milk Solids", "Cultures",
    "PET Preforms", "Aluminum Foil", "Carton Board", "Plastic Film", "Glass Bottles",
]

SUPPLIERS = [
    ("SUP-01", "AgriCore Commodities", "Iowa, US", 0.97),
    ("SUP-02", "GlobalSweet Ltd", "Sao Paulo, BR", 0.91),
    ("SUP-03", "ChemBase Industries", "Houston, US", 0.95),
    ("SUP-04", "PacificPack Materials", "Shenzhen, CN", 0.88),
    ("SUP-05", "EuroFlavor GmbH", "Hamburg, DE", 0.96),
    ("SUP-06", "DairyFirst Co-op", "Wisconsin, US", 0.98),
    ("SUP-07", "PetroPolymers Inc", "Rotterdam, NL", 0.93),
    ("SUP-08", "SpiceRoute Traders", "Mumbai, IN", 0.90),
]

PLANTS = [
    ("PLANT-01", "Midwest Beverage & Dairy Plant", "Chicago, IL", ["Beverages", "Dairy"]),
    ("PLANT-02", "Southern Snacks Plant", "Atlanta, GA", ["Snacks"]),
    ("PLANT-03", "Homecare & Personal Plant", "Dallas, TX", ["Personal Care", "Home Care"]),
]

CENTRAL_DCS = [
    ("CDC-01", "National DC East", "Columbus, OH"),
    ("CDC-02", "National DC West", "Reno, NV"),
]

REGIONAL_DCS = [
    ("RDC-01", "Northeast RDC", "Newark, NJ", "CDC-01"),
    ("RDC-02", "Southeast RDC", "Charlotte, NC", "CDC-01"),
    ("RDC-03", "Midwest RDC", "Indianapolis, IN", "CDC-01"),
    ("RDC-04", "Southwest RDC", "Phoenix, AZ", "CDC-02"),
    ("RDC-05", "West Coast RDC", "Sacramento, CA", "CDC-02"),
    ("RDC-06", "Northwest RDC", "Portland, OR", "CDC-02"),
]

STORE_CITIES = {
    "RDC-01": ["New York", "Boston", "Philadelphia", "Buffalo"],
    "RDC-02": ["Miami", "Orlando", "Nashville"],
    "RDC-03": ["Chicago", "Detroit", "Minneapolis", "St Louis"],
    "RDC-04": ["Phoenix", "Denver", "Albuquerque"],
    "RDC-05": ["Los Angeles", "San Francisco", "San Diego"],
    "RDC-06": ["Seattle", "Portland", "Boise"],
}

# which raw materials plausibly feed each category (indices into RAW_MATERIALS)
CATEGORY_MATERIALS = {
    "Beverages":     [0, 1, 2, 3, 4, 20, 24],
    "Snacks":        [5, 6, 7, 8, 9, 21, 23],
    "Personal Care": [10, 11, 12, 14, 17, 23],
    "Home Care":     [10, 13, 15, 16, 17, 23],
    "Dairy":         [18, 19, 0, 21, 22],
}

CATEGORY_NAME_PARTS = {
    "Beverages":     (["Fizzo", "AquaPure", "TeaLeaf", "Citrus", "ColaMax"], ["Cola", "Soda", "Iced Tea", "Sparkling Water", "Lemonade"]),
    "Snacks":        (["Crunchy", "Golden", "Spicy", "Sea Salt", "Smoky"], ["Chips", "Crackers", "Pretzels", "Popcorn", "Tortillas"]),
    "Personal Care": (["FreshDew", "SilkSoft", "PureGlow", "AquaMist", "Herbal"], ["Shampoo", "Body Wash", "Lotion", "Soap Bar", "Conditioner"]),
    "Home Care":     (["SparkleX", "CleanWave", "PowerScrub", "LemonFresh", "UltraShine"], ["Detergent", "Dish Soap", "Surface Cleaner", "Bleach", "Fabric Softener"]),
    "Dairy":         (["FarmFresh", "CreamyVale", "MorningStar", "AlpineHill", "DairyGold"], ["Yogurt", "Milk 1L", "Cheese Block", "Butter", "Cream"]),
}

SIZES = ["250ml", "500ml", "1L", "Small", "Medium", "Large", "Family Pack", "6-Pack"]

N_SKUS = 120


def generate_skus():
    """120 SKUs spread across the 5 categories, each with a BOM of 2-4 raw materials."""
    skus, used_names = [], set()
    per_cat = N_SKUS // len(CATEGORIES)  # 24 each
    sku_num = 0
    for cat in CATEGORIES:
        brands, forms = CATEGORY_NAME_PARTS[cat]
        for _ in range(per_cat):
            sku_num += 1
            while True:
                name = f"{random.choice(brands)} {random.choice(forms)} {random.choice(SIZES)}"
                if name not in used_names:
                    used_names.add(name)
                    break
            bom = random.sample(CATEGORY_MATERIALS[cat], k=random.randint(2, 4))
            skus.append({
                "sku_id": f"SKU-{sku_num:04d}",
                "name": name,
                "category": cat,
                "unit_cost": round(random.uniform(0.4, 6.0), 2),
                "list_price": 0.0,  # filled below
                "shelf_life_days": random.choice([30, 60, 90, 180, 365, 720]),
                "weight_kg": round(random.uniform(0.1, 2.5), 2),
                "bom": [{"material": RAW_MATERIALS[i], "qty_per_unit": round(random.uniform(0.05, 1.2), 3)} for i in bom],
            })
    for s in skus:
        s["list_price"] = round(s["unit_cost"] * random.uniform(1.6, 2.8), 2)
    return skus


# ---------------------------------------------------------------- cypher setup

CONSTRAINTS = [
    "CREATE CONSTRAINT sku_id IF NOT EXISTS FOR (p:Product) REQUIRE p.sku_id IS UNIQUE",
    "CREATE CONSTRAINT supplier_id IF NOT EXISTS FOR (s:Supplier) REQUIRE s.supplier_id IS UNIQUE",
    "CREATE CONSTRAINT plant_id IF NOT EXISTS FOR (p:Plant) REQUIRE p.plant_id IS UNIQUE",
    "CREATE CONSTRAINT dc_id IF NOT EXISTS FOR (d:DistributionCenter) REQUIRE d.dc_id IS UNIQUE",
    "CREATE CONSTRAINT store_id IF NOT EXISTS FOR (s:Store) REQUIRE s.store_id IS UNIQUE",
    "CREATE CONSTRAINT material_name IF NOT EXISTS FOR (m:RawMaterial) REQUIRE m.name IS UNIQUE",
    "CREATE CONSTRAINT category_name IF NOT EXISTS FOR (c:Category) REQUIRE c.name IS UNIQUE",
    "CREATE CONSTRAINT event_id IF NOT EXISTS FOR (e:DisruptionEvent) REQUIRE e.event_id IS UNIQUE",
]


def seed(driver):
    skus = generate_skus()

    with driver.session() as s:
        for c in CONSTRAINTS:
            s.run(c)
        s.run("MATCH (n) DETACH DELETE n")  # idempotent re-seed

        # --- categories & raw materials
        s.run("UNWIND $cats AS c MERGE (:Category {name: c})", cats=CATEGORIES)
        s.run("UNWIND $mats AS m MERGE (:RawMaterial {name: m})", mats=RAW_MATERIALS)

        # --- suppliers, each supplying 3-6 raw materials
        supplier_rows = []
        for sid, name, loc, rel in SUPPLIERS:
            mats = random.sample(RAW_MATERIALS, k=random.randint(3, 6))
            supplier_rows.append({
                "supplier_id": sid, "name": name, "location": loc, "reliability": rel,
                "supplies": [{"material": m, "cost_per_unit": round(random.uniform(0.1, 2.0), 2)} for m in mats],
            })
        s.run("""
            UNWIND $rows AS r
            MERGE (sup:Supplier {supplier_id: r.supplier_id})
            SET sup.name = r.name, sup.location = r.location, sup.reliability = r.reliability
            WITH sup, r
            UNWIND r.supplies AS sp
            MATCH (m:RawMaterial {name: sp.material})
            MERGE (sup)-[rel:SUPPLIES]->(m)
            SET rel.cost_per_unit = sp.cost_per_unit
        """, rows=supplier_rows)

        # ensure every raw material has at least one supplier
        s.run("""
            MATCH (m:RawMaterial) WHERE NOT ( (:Supplier)-[:SUPPLIES]->(m) )
            WITH m, ['SUP-01','SUP-03','SUP-04','SUP-05'][toInteger(rand()*4)] AS sid
            MATCH (sup:Supplier {supplier_id: sid})
            MERGE (sup)-[rel:SUPPLIES]->(m)
            SET rel.cost_per_unit = round(rand()*2 + 0.1, 2)
        """)

        # --- plants
        s.run("""
            UNWIND $rows AS r
            MERGE (p:Plant {plant_id: r.plant_id})
            SET p.name = r.name, p.location = r.location, p.categories = r.categories
        """, rows=[{"plant_id": pid, "name": n, "location": loc, "categories": cats}
                   for pid, n, loc, cats in PLANTS])

        # supplier -> plant shipping lanes
        lanes = []
        for sid, _, loc, _ in SUPPLIERS:
            overseas = not loc.endswith("US")
            for pid, _, _, _ in PLANTS:
                lanes.append({
                    "supplier_id": sid, "plant_id": pid,
                    "lead_time_days": random.randint(21, 45) if overseas else random.randint(3, 10),
                    "transport_mode": "sea" if overseas else "truck",
                })
        s.run("""
            UNWIND $rows AS r
            MATCH (sup:Supplier {supplier_id: r.supplier_id}), (p:Plant {plant_id: r.plant_id})
            MERGE (sup)-[rel:SHIPS_TO]->(p)
            SET rel.lead_time_days = r.lead_time_days, rel.transport_mode = r.transport_mode
        """, rows=lanes)

        # --- products, BOM, category, producing plant
        plant_by_cat = {c: pid for pid, _, _, cats in PLANTS for c in cats}
        sku_rows = []
        for sku in skus:
            sku_rows.append({**sku, "plant_id": plant_by_cat[sku["category"]],
                             "capacity_per_week": random.randint(5000, 50000)})
        s.run("""
            UNWIND $rows AS r
            MERGE (p:Product {sku_id: r.sku_id})
            SET p.name = r.name, p.unit_cost = r.unit_cost, p.list_price = r.list_price,
                p.shelf_life_days = r.shelf_life_days, p.weight_kg = r.weight_kg
            WITH p, r
            MATCH (c:Category {name: r.category})
            MERGE (p)-[:IN_CATEGORY]->(c)
            WITH p, r
            MATCH (pl:Plant {plant_id: r.plant_id})
            MERGE (pl)-[pr:PRODUCES]->(p)
            SET pr.capacity_per_week = r.capacity_per_week, pr.unit_cost = r.unit_cost
            WITH p, r
            UNWIND r.bom AS b
            MATCH (m:RawMaterial {name: b.material})
            MERGE (p)-[mf:MADE_FROM]->(m)
            SET mf.qty_per_unit = b.qty_per_unit
        """, rows=sku_rows)

        # --- DCs and shipping lanes
        s.run("""
            UNWIND $rows AS r
            MERGE (d:DistributionCenter {dc_id: r.dc_id})
            SET d.name = r.name, d.location = r.location, d.tier = 'central'
        """, rows=[{"dc_id": d, "name": n, "location": l} for d, n, l in CENTRAL_DCS])
        s.run("""
            UNWIND $rows AS r
            MERGE (d:DistributionCenter {dc_id: r.dc_id})
            SET d.name = r.name, d.location = r.location, d.tier = 'regional'
            WITH d, r
            MATCH (c:DistributionCenter {dc_id: r.parent})
            MERGE (c)-[rel:SHIPS_TO]->(d)
            SET rel.lead_time_days = r.lt
        """, rows=[{"dc_id": d, "name": n, "location": l, "parent": p, "lt": random.randint(2, 5)}
                   for d, n, l, p in REGIONAL_DCS])

        # every plant ships to both central DCs
        s.run("""
            MATCH (p:Plant), (c:DistributionCenter {tier: 'central'})
            MERGE (p)-[rel:SHIPS_TO]->(c)
            SET rel.lead_time_days = toInteger(rand()*4) + 2
        """)

        # --- stores
        store_rows, snum = [], 0
        for rdc, cities in STORE_CITIES.items():
            for city in cities:
                snum += 1
                store_rows.append({
                    "store_id": f"ST-{snum:03d}", "name": f"{city} Store", "city": city,
                    "rdc": rdc, "lt": random.randint(1, 3),
                    "format": random.choice(["hypermarket", "supermarket", "convenience"]),
                })
        s.run("""
            UNWIND $rows AS r
            MERGE (st:Store {store_id: r.store_id})
            SET st.name = r.name, st.city = r.city, st.format = r.format
            WITH st, r
            MATCH (d:DistributionCenter {dc_id: r.rdc})
            MERGE (d)-[rel:SHIPS_TO]->(st)
            SET rel.lead_time_days = r.lt
        """, rows=store_rows)

        # --- inventory (STOCKS) at DCs and stores, demand (SELLS) at stores
        # central DCs stock everything; regional DCs stock ~80%; stores stock ~50%
        sku_ids = [x["sku_id"] for x in skus]

        def stock_row(loc_id, sku_id, scale):
            avg_wk = random.randint(50, 800)
            safety = int(avg_wk * scale * random.uniform(0.3, 0.8))
            on_hand = int(safety * random.uniform(0.2, 3.0))
            return {
                "loc": loc_id, "sku": sku_id, "on_hand": on_hand, "safety_stock": safety,
                "reorder_point": int(safety * 1.5),
                "fill_rate": round(min(0.999, random.gauss(0.95, 0.04)), 3),
                "stockout_events_90d": max(0, int(random.gauss(2, 2))),
            }

        dc_stock = []
        for cdc, _, _ in CENTRAL_DCS:
            dc_stock += [stock_row(cdc, k, 4.0) for k in sku_ids]
        for rdc, _, _, _ in REGIONAL_DCS:
            dc_stock += [stock_row(rdc, k, 2.0) for k in random.sample(sku_ids, int(len(sku_ids) * 0.8))]
        s.run("""
            UNWIND $rows AS r
            MATCH (d:DistributionCenter {dc_id: r.loc}), (p:Product {sku_id: r.sku})
            MERGE (d)-[st:STOCKS]->(p)
            SET st.on_hand = r.on_hand, st.safety_stock = r.safety_stock,
                st.reorder_point = r.reorder_point, st.fill_rate = r.fill_rate,
                st.stockout_events_90d = r.stockout_events_90d
        """, rows=dc_stock)

        store_stock, sells = [], []
        for row in store_rows:
            assort = random.sample(sku_ids, int(len(sku_ids) * random.uniform(0.4, 0.6)))
            for k in assort:
                store_stock.append(stock_row(row["store_id"], k, 0.5))
                promo = random.random() < 0.15
                base = random.randint(20, 300)
                sells.append({
                    "store": row["store_id"], "sku": k,
                    "avg_weekly_demand": int(base * (1.4 if promo else 1.0)),
                    "demand_std": round(base * random.uniform(0.15, 0.5), 1),
                    "price": None, "on_promotion": promo,
                    "discount_pct": round(random.uniform(0.1, 0.3), 2) if promo else 0.0,
                })
        s.run("""
            UNWIND $rows AS r
            MATCH (st:Store {store_id: r.loc}), (p:Product {sku_id: r.sku})
            MERGE (st)-[x:STOCKS]->(p)
            SET x.on_hand = r.on_hand, x.safety_stock = r.safety_stock,
                x.reorder_point = r.reorder_point, x.fill_rate = r.fill_rate,
                x.stockout_events_90d = r.stockout_events_90d
        """, rows=store_stock)
        s.run("""
            UNWIND $rows AS r
            MATCH (st:Store {store_id: r.store}), (p:Product {sku_id: r.sku})
            MERGE (st)-[x:SELLS]->(p)
            SET x.avg_weekly_demand = r.avg_weekly_demand, x.demand_std = r.demand_std,
                x.price = round(p.list_price * (1 - r.discount_pct), 2),
                x.on_promotion = r.on_promotion, x.discount_pct = r.discount_pct
        """, rows=sells)

        # --- disruption events (fuel for causal questions)
        events = [
            {"event_id": "EVT-001", "type": "port_congestion", "severity": "high",
             "start_date": "2026-05-10", "duration_days": 21,
             "description": "Port congestion in Shenzhen delayed packaging shipments",
             "targets": ["SUP-04"]},
            {"event_id": "EVT-002", "type": "plant_shutdown", "severity": "medium",
             "start_date": "2026-06-02", "duration_days": 7,
             "description": "Equipment failure halted line 2 at the Southern Snacks Plant",
             "targets": ["PLANT-02"]},
            {"event_id": "EVT-003", "type": "labor_strike", "severity": "medium",
             "start_date": "2026-06-15", "duration_days": 10,
             "description": "Warehouse labor strike slowed outbound at National DC West",
             "targets": ["CDC-02"]},
            {"event_id": "EVT-004", "type": "raw_material_shortage", "severity": "high",
             "start_date": "2026-04-20", "duration_days": 30,
             "description": "Poor harvest cut sugar supply from GlobalSweet",
             "targets": ["SUP-02"]},
        ]
        s.run("""
            UNWIND $rows AS r
            MERGE (e:DisruptionEvent {event_id: r.event_id})
            SET e.type = r.type, e.severity = r.severity, e.start_date = date(r.start_date),
                e.duration_days = r.duration_days, e.description = r.description
            WITH e, r
            UNWIND r.targets AS t
            MATCH (n) WHERE n.supplier_id = t OR n.plant_id = t OR n.dc_id = t
            MERGE (e)-[:AFFECTS]->(n)
        """, rows=events)

        # --- summary
        counts = s.run("""
            MATCH (n) WITH labels(n)[0] AS label, count(*) AS c
            RETURN label, c ORDER BY label
        """).data()
        rels = s.run("MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS c ORDER BY type").data()

    print("Node counts:")
    for row in counts:
        print(f"  {row['label']:<20} {row['c']}")
    print("Relationship counts:")
    for row in rels:
        print(f"  {row['type']:<20} {row['c']}")


if __name__ == "__main__":
    driver = GraphDatabase.driver(URI, auth=AUTH)
    driver.verify_connectivity()
    seed(driver)
    driver.close()
    print("\nDone. Open http://localhost:7474 to browse the graph.")
