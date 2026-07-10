"""
DoWhy-backed causal engine for the supply chain agent.

Four capabilities, each exposed to the LLM as a tool (schemas in TOOL_SPECS,
dispatch via run_tool):

  estimate_effect        rung 2 — average causal effect of a promotion or a
                         DisruptionEvent on an outcome, via dowhy.CausalModel
                         (backdoor adjustment) + refutation tests.
  what_if                rung 2/3 — bulk intervention over matching snapshot
                         weeks ("what would sales have been if ...") via a
                         fitted dowhy.gcm structural causal model.
  counterfactual         rung 3 — single (DC, SKU, week) counterfactual:
                         abduction/action/prediction on the observed row.
  explain_stockout_risk  deterministic decomposition of PROJECTED stockout
                         risk for a SKU into demand-attainment, disruption,
                         and inventory-position drivers.

The SCM mirrors the true data-generating process (see GROUND_TRUTH.md):

    seasonal_base ─→ demand ←─ on_promotion
    seasonal_base ─→ units_received ←─ supply_factor, on_hand_start, on_promotion
    units_sold  = min(demand, on_hand_start + units_received)   (derived exactly)
    stockout    = demand > on_hand_start + units_received       (derived exactly)

demand and units_received are additive-noise mechanisms (invertible → true
counterfactuals); the min/threshold nodes are computed deterministically from
counterfactual parents, so they carry no ML approximation error.
"""

import os
import threading
import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd
import networkx as nx

# keep dowhy/statsmodels/joblib noise out of the server console
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="dowhy")
os.environ.setdefault("LOKY_MAX_CPU_COUNT", "4")

PROMO_LIFT = 1.4          # must match add_weekly_snapshots.py
ALLOC_FLEX = 1.15         # committed supply plan cap: receipts <= 1.15x forecast
WEEK0 = date(2025, 7, 7)
N_HIST = 52               # week 52 = today (2026-07-06); >= 52 is projected
ATTAIN_WEEKS = (44, 51)   # trailing window used for demand attainment

_lock = threading.RLock()  # re-entrant: _get_scm -> _load_panel both lock
_driver = None
_panel: pd.DataFrame | None = None
_events: list | None = None
_scm = None


def init(driver):
    """Give the engine the shared neo4j driver (call once at startup)."""
    global _driver
    _driver = driver


def warm():
    """Preload the snapshot panel and fit the SCM (call in a background thread)."""
    try:
        _load_panel()
        _load_events()
        _get_scm()
    except Exception as e:  # engine tools will retry and surface the error themselves
        print(f"[causal_engine] warm-up failed: {type(e).__name__}: {e}")


def week_date(w: int) -> str:
    return (WEEK0 + timedelta(weeks=int(w))).isoformat()


def _jsonable(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return round(float(v), 4)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, float):
        return round(v, 4)
    return v


# ---------------------------------------------------------------- data access

def _load_panel() -> pd.DataFrame:
    global _panel
    with _lock:
        if _panel is not None:
            return _panel
        return _load_panel_locked()


def _load_panel_locked() -> pd.DataFrame:
    global _panel
    with _driver.session() as s:
        rows = [r.data() for r in s.run("""
            MATCH (d:DistributionCenter)-[:HAS_SNAPSHOT]->(x:WeeklySnapshot)
                  -[:FOR_PRODUCT]->(p:Product)-[:IN_CATEGORY]->(c:Category)
            RETURN d.dc_id AS loc_id, d.tier AS tier, x.sku_id AS sku_id,
                   p.name AS product, c.name AS category,
                   x.week_index AS week_index, x.demand AS demand,
                   x.forecast_demand AS forecast_demand,
                   x.units_sold AS units_sold, x.units_received AS units_received,
                   x.on_hand_start AS on_hand_start, x.on_hand_end AS on_hand_end,
                   x.stockout AS stockout, x.on_promotion AS on_promotion,
                   x.supply_factor AS supply_factor, x.projected AS projected
        """)]
    df = pd.DataFrame(rows)
    df["on_promotion"] = df["on_promotion"].astype(int)
    df["stockout"] = df["stockout"].astype(int)
    df["projected"] = df["projected"].astype(bool)
    df["seasonal_base"] = df["forecast_demand"] / np.where(df["on_promotion"] == 1, PROMO_LIFT, 1.0)
    df["attainment"] = df["demand"] / df["forecast_demand"].clip(lower=1)
    _panel = df
    return df


def _load_events() -> list:
    """DisruptionEvents with affected SKUs/locations and their impact window
    at regional-DC level (echelon propagation lags from the generator)."""
    global _events
    with _lock:
        if _events is not None:
            return _events
        return _load_events_locked()


def _load_events_locked() -> list:
    global _events
    with _driver.session() as s:
        rows = [r.data() for r in s.run("""
            MATCH (e:DisruptionEvent)
            OPTIONAL MATCH (e)-[:AFFECTS]->(sup:Supplier)-[:SUPPLIES]->
                           (:RawMaterial)<-[:MADE_FROM]-(p1:Product)
            OPTIONAL MATCH (e)-[:AFFECTS]->(pl:Plant)-[:PRODUCES]->(p2:Product)
            OPTIONAL MATCH (e)-[:AFFECTS]->(:DistributionCenter {tier:'central'})
                           -[:SHIPS_TO]->(rdc:DistributionCenter)
            RETURN e.event_id AS id, e.type AS type, e.severity AS severity,
                   e.description AS description, toString(e.start_date) AS start,
                   e.duration_days AS days,
                   [x IN collect(DISTINCT p1.sku_id) WHERE x IS NOT NULL]
                   + [x IN collect(DISTINCT p2.sku_id) WHERE x IS NOT NULL] AS skus,
                   [x IN collect(DISTINCT rdc.dc_id) WHERE x IS NOT NULL] AS locs
        """)]
    events = []
    for r in rows:
        ws = (date.fromisoformat(r["start"]) - WEEK0).days // 7
        we = ws + max(1, round(r["days"] / 7)) - 1
        lag = {"raw_material_shortage": 4, "port_congestion": 4,
               "plant_shutdown": 2, "labor_strike": 1}[r["type"]]
        events.append({
            "id": r["id"], "type": r["type"], "severity": r["severity"],
            "description": r["description"], "start": r["start"],
            "event_weeks": (ws, we),
            "rdc_impact_weeks": (ws + lag, we + lag),
            "skus": set(r["skus"]),          # empty set => lane event (all SKUs)
            "locs": set(r["locs"]),          # empty set => all locations
        })
    _events = events
    return events


def _filter(df: pd.DataFrame, where: dict | None) -> pd.DataFrame:
    where = where or {}
    out = df
    if where.get("tier"):
        out = out[out["tier"] == where["tier"]]
    if where.get("loc_id"):
        out = out[out["loc_id"] == where["loc_id"]]
    if where.get("sku_id"):
        out = out[out["sku_id"] == where["sku_id"]]
    if where.get("category"):
        out = out[out["category"] == where["category"]]
    if where.get("week_from") is not None:
        out = out[out["week_index"] >= int(where["week_from"])]
    if where.get("week_to") is not None:
        out = out[out["week_index"] <= int(where["week_to"])]
    if where.get("projected") is not None:
        out = out[out["projected"] == bool(where["projected"])]
    return out


# ---------------------------------------------------------------- SCM (gcm)

_SCM_NODES = ["seasonal_base", "on_promotion", "supply_factor", "on_hand_start",
              "demand", "units_received"]


def _get_scm():
    global _scm
    with _lock:
        if _scm is not None:
            return _scm
        from dowhy import gcm
        from dowhy.gcm import config as gcm_config
        gcm_config.disable_progress_bars()
        df = _load_panel()
        fit_df = df[(~df["projected"]) & (df["tier"] == "regional")][_SCM_NODES]
        g = nx.DiGraph([
            ("seasonal_base", "demand"), ("on_promotion", "demand"),
            ("seasonal_base", "units_received"), ("on_promotion", "units_received"),
            ("supply_factor", "units_received"), ("on_hand_start", "units_received"),
        ])
        scm = gcm.InvertibleStructuralCausalModel(g)
        for root in ("seasonal_base", "on_promotion", "supply_factor", "on_hand_start"):
            scm.set_causal_mechanism(root, gcm.EmpiricalDistribution())
        from dowhy.gcm.ml import create_hist_gradient_boost_regressor
        scm.set_causal_mechanism("demand", gcm.AdditiveNoiseModel(
            create_hist_gradient_boost_regressor()))
        scm.set_causal_mechanism("units_received", gcm.AdditiveNoiseModel(
            create_hist_gradient_boost_regressor()))
        gcm.fit(scm, fit_df)
        _scm = scm
        return scm


def _build_interventions(specs: list) -> dict:
    """[{variable, value|multiplier|add}] -> {var: lambda}. Lambdas close over
    the spec safely (default-arg binding)."""
    allowed = {"on_promotion", "supply_factor", "demand", "units_received", "on_hand_start"}
    out = {}
    for spec in specs:
        var = spec.get("variable")
        if var not in allowed:
            raise ValueError(f"unsupported intervention variable '{var}'; allowed: {sorted(allowed)}")
        if "value" in spec and spec["value"] is not None:
            out[var] = lambda x, v=float(spec["value"]): v
        elif "multiplier" in spec and spec["multiplier"] is not None:
            out[var] = lambda x, m=float(spec["multiplier"]): x * m
        elif "add" in spec and spec["add"] is not None:
            out[var] = lambda x, a=float(spec["add"]): x + a
        else:
            raise ValueError(f"intervention on '{var}' needs one of value/multiplier/add")
    return out


def _derive_outcomes(demand, on_hand_start, received):
    demand = np.maximum(0, np.round(demand))
    received = np.maximum(0, np.round(received))
    available = on_hand_start + received
    sold = np.minimum(demand, available)
    return demand, received, sold, (demand > available)


def _counterfactual_frame(rows: pd.DataFrame, interventions: dict) -> pd.DataFrame:
    """Row-wise counterfactuals for demand/units_received, with units_sold and
    stockout derived exactly from the structural equations."""
    from dowhy import gcm
    scm = _get_scm()
    cf = gcm.counterfactual_samples(scm, interventions,
                                    observed_data=rows[_SCM_NODES].reset_index(drop=True))
    d, r, s, so = _derive_outcomes(cf["demand"].to_numpy(),
                                   cf["on_hand_start"].to_numpy(),
                                   cf["units_received"].to_numpy())
    cf["demand"], cf["units_received"] = d, r
    cf["units_sold"], cf["stockout"] = s, so.astype(int)
    return cf


# ---------------------------------------------------------------- tools

def estimate_effect(treatment: str, outcome: str = "units_sold") -> dict:
    """ATE of on_promotion or a DisruptionEvent on an outcome, with refuters."""
    from dowhy import CausalModel
    valid_outcomes = {"units_sold", "stockout", "units_received", "demand", "on_hand_end"}
    if outcome not in valid_outcomes:
        return {"error": f"outcome must be one of {sorted(valid_outcomes)}"}
    df = _load_panel()
    rdc = df[df["tier"] == "regional"]
    note = None

    if treatment == "on_promotion":
        data = rdc[~rdc["projected"]].copy()
        data["treated"] = data["on_promotion"]
        common = ["seasonal_base"]
        note = ("Promo spells are planned independently of demand shocks, so "
                "backdoor adjustment on seasonal_base suffices.")
    elif treatment.upper().startswith("EVT-"):
        ev = next((e for e in _load_events() if e["id"] == treatment.upper()), None)
        if ev is None:
            return {"error": f"unknown event '{treatment}'"}
        lo, hi = ev["rdc_impact_weeks"]
        data = rdc[(rdc["week_index"] >= lo) & (rdc["week_index"] <= hi)].copy()
        if data.empty:
            return {"error": f"no snapshot weeks in impact window {lo}-{hi}"}
        treated = data["sku_id"].isin(ev["skus"]) if ev["skus"] else pd.Series(True, index=data.index)
        if ev["locs"]:
            treated &= data["loc_id"].isin(ev["locs"])
        data["treated"] = treated.astype(int)
        common = ["seasonal_base", "on_promotion"]
        if hi >= N_HIST:
            note = (f"Impact window (weeks {lo}-{hi}) reaches PROJECTED weeks — the "
                    "estimate partly reflects the deterministic planning projection, "
                    "not observed history.")
    else:
        return {"error": "treatment must be 'on_promotion' or an event id like 'EVT-005'"}

    if data["treated"].nunique() < 2:
        return {"error": "treatment has no variation in the selected window"}

    model = CausalModel(data=data, treatment="treated", outcome=outcome,
                        common_causes=common)
    estimand = model.identify_effect(proceed_when_unidentifiable=True)
    est = model.estimate_effect(estimand, method_name="backdoor.linear_regression")
    ate = float(est.value)
    ctrl_mean = float(data.loc[data["treated"] == 0, outcome].mean())

    refutations = {}
    for method, label in [("placebo_treatment_refuter", "placebo_treatment"),
                          ("random_common_cause", "random_common_cause")]:
        try:
            ref = model.refute_estimate(estimand, est, method_name=method,
                                        num_simulations=10, show_progress_bar=False)
            refutations[label] = {
                "new_effect": _jsonable(ref.new_effect),
                "passed": bool(abs(ref.new_effect) < abs(ate) * 0.3) if label == "placebo_treatment"
                          else bool(abs(ref.new_effect - ate) < max(abs(ate) * 0.2, 1e-9)),
            }
        except Exception as e:
            refutations[label] = {"error": f"{type(e).__name__}: {e}"}

    return _jsonable({
        "treatment": treatment, "outcome": outcome,
        "method": "dowhy backdoor.linear_regression, adjusted for " + ", ".join(common),
        "ate": ate,
        "control_mean": ctrl_mean,
        "relative_effect_pct": 100 * ate / ctrl_mean if ctrl_mean else None,
        "n_treated": int((data["treated"] == 1).sum()),
        "n_control": int((data["treated"] == 0).sum()),
        "refutations": refutations,
        "note": note,
    })


def what_if(interventions: list, where: dict | None = None) -> dict:
    """Bulk intervention over matching regional-DC snapshot weeks; reports
    baseline vs intervened aggregates."""
    df = _load_panel()
    where = dict(where or {})
    where.setdefault("tier", "regional")
    rows = _filter(df, where)
    if rows.empty:
        return {"error": "no snapshot rows match the filter", "where": where}
    if len(rows) > 20000:
        return {"error": f"filter matches {len(rows)} rows; narrow it (sku/loc/category/week range)"}
    try:
        iv = _build_interventions(interventions)
    except ValueError as e:
        return {"error": str(e)}
    cf = _counterfactual_frame(rows, iv)

    def agg(frame, demand, sold, stockout, received):
        return {"avg_demand": frame[demand].mean() if isinstance(demand, str) else demand.mean(),
                "avg_units_sold": frame[sold].mean() if isinstance(sold, str) else sold.mean(),
                "stockout_rate": frame[stockout].mean() if isinstance(stockout, str) else stockout.mean(),
                "avg_units_received": frame[received].mean() if isinstance(received, str) else received.mean()}

    base = agg(rows, "demand", "units_sold", "stockout", "units_received")
    new = agg(cf, "demand", "units_sold", "stockout", "units_received")
    return _jsonable({
        "interventions": interventions, "where": where,
        "n_dc_weeks": len(rows),
        "weeks": [int(rows["week_index"].min()), int(rows["week_index"].max())],
        "baseline": base,
        "intervened": new,
        "delta": {k: new[k] - base[k] for k in base},
        "total_units_sold_delta": float(cf["units_sold"].sum() - rows["units_sold"].sum()),
        "note": ("Row-level counterfactuals from the fitted SCM; inventory carryover "
                 "between weeks is held at observed values (single-week effects)."),
    })


def counterfactual(loc_id: str, sku_id: str, week_index: int, interventions: list) -> dict:
    """Counterfactual for one observed (DC, SKU, week): what WOULD this week
    have looked like under the intervention?"""
    df = _load_panel()
    row = df[(df["loc_id"] == loc_id) & (df["sku_id"] == sku_id)
             & (df["week_index"] == int(week_index))]
    if row.empty:
        return {"error": f"no snapshot for {loc_id}/{sku_id}/week {week_index}"}
    try:
        iv = _build_interventions(interventions)
    except ValueError as e:
        return {"error": str(e)}
    cf = _counterfactual_frame(row, iv).iloc[0]
    obs = row.iloc[0]
    fields = ["demand", "units_received", "units_sold", "stockout"]
    return _jsonable({
        "loc_id": loc_id, "sku_id": sku_id, "week_index": int(week_index),
        "week_start": week_date(week_index),
        "projected_week": bool(obs["projected"]),
        "interventions": interventions,
        "observed": {f: obs[f] for f in fields} | {"on_hand_start": obs["on_hand_start"],
                                                   "supply_factor": obs["supply_factor"],
                                                   "on_promotion": int(obs["on_promotion"])},
        "counterfactual": {f: cf[f] for f in fields},
        "delta": {f: float(cf[f]) - float(obs[f]) for f in fields},
        "method": "dowhy.gcm abduction-action-prediction on the fitted invertible SCM",
    })


def explain_stockout_risk(sku_id: str, loc_id: str | None = None) -> dict:
    """Decompose projected stockout risk for a SKU into named drivers:
    demand attainment vs plan, active/announced disruptions, inventory position."""
    df = _load_panel()
    events = _load_events()
    rdc = df[(df["tier"] == "regional") & (df["sku_id"] == sku_id)]
    if loc_id:
        rdc = rdc[rdc["loc_id"] == loc_id]
    if rdc.empty:
        return {"error": f"no regional-DC snapshots for {sku_id}"
                         + (f" at {loc_id}" if loc_id else "")}

    locations = []
    for loc, g in rdc.groupby("loc_id"):
        g = g.sort_values("week_index")
        hist, proj = g[~g["projected"]], g[g["projected"]]
        risk_weeks = proj.loc[proj["stockout"] == 1, "week_index"].tolist()
        a_lo, a_hi = ATTAIN_WEEKS
        recent = hist[(hist["week_index"] >= a_lo) & (hist["week_index"] <= a_hi)]
        attain = float(recent["attainment"].mean()) if len(recent) else None
        oh_now = float(hist.iloc[-1]["on_hand_end"]) if len(hist) else 0.0
        next4_fc = float(proj.head(4)["forecast_demand"].mean()) if len(proj) else 1.0
        cover = oh_now / max(next4_fc, 1.0)
        hist_stockouts = hist.loc[hist["stockout"] == 1, "week_index"].tolist()

        drivers = []
        if attain and attain >= 1.10:
            drivers.append({
                "type": "demand_overshoot",
                "detail": (f"Demand attainment {attain:.0%} of forecast over weeks {a_lo}-{a_hi} "
                           f"(historical), while the committed supply plan only flexes to "
                           f"{ALLOC_FLEX:.0%} of forecast — projected demand is extrapolated at "
                           f"this attainment, so supply structurally trails demand."),
            })
        if len(proj):
            hit = proj[(proj["supply_factor"] < 0.999) | (proj["stockout"] == 1)]
            factor_weeks = proj.loc[proj["supply_factor"] < 0.999, "week_index"].tolist()
            for ev in events:
                lo, hi = ev["rdc_impact_weeks"]
                if hi < N_HIST:
                    continue  # historical event, cannot drive future risk
                sku_hit = (not ev["skus"]) or (sku_id in ev["skus"])
                loc_hit = (not ev["locs"]) or (loc in ev["locs"])
                overlap = [w for w in factor_weeks if lo <= w <= hi]
                if sku_hit and loc_hit and overlap:
                    drivers.append({
                        "type": "disruption",
                        "event_id": ev["id"],
                        "detail": (f"{ev['id']} ({ev['type']}, started {ev['start']}): "
                                   f"{ev['description']}. Cuts planned receipts in projected weeks "
                                   f"{min(overlap)}-{max(overlap)} "
                                   f"(supply factor {float(proj.loc[proj['week_index'].isin(overlap), 'supply_factor'].min()):.2f})."),
                    })
        if cover < 1.5:
            drivers.append({
                "type": "low_starting_inventory",
                "detail": (f"Entering the horizon with {cover:.1f} weeks of cover "
                           f"(on-hand {oh_now:.0f} vs ~{next4_fc:.0f}/week forecast)"
                           + (f"; already stocked out in historical weeks {hist_stockouts[-3:]}"
                              if hist_stockouts else "") + "."),
            })
        promo_risk = proj[(proj["on_promotion"] == 1) & (proj["stockout"] == 1)]
        if len(promo_risk):
            drivers.append({
                "type": "planned_promotion",
                "detail": (f"Planned promotions lift forecast x{PROMO_LIFT} in projected weeks "
                           f"{promo_risk['week_index'].tolist()}, coinciding with stockouts."),
            })

        locations.append({
            "loc_id": loc,
            "at_risk": bool(risk_weeks),
            "projected_stockout_weeks": risk_weeks,
            "first_risk_week": risk_weeks[0] if risk_weeks else None,
            "first_risk_date": week_date(risk_weeks[0]) if risk_weeks else None,
            "recent_demand_attainment": attain,
            "weeks_of_cover_now": cover,
            "historical_stockout_weeks": hist_stockouts,
            "drivers": drivers if risk_weeks else [],
        })

    at_risk = [l for l in locations if l["at_risk"]]
    return _jsonable({
        "sku_id": sku_id,
        "product": rdc.iloc[0]["product"],
        "category": rdc.iloc[0]["category"],
        "today": "2026-07-06 (start of week 52); weeks 52-76 are projected",
        "summary": (f"{len(at_risk)} of {len(locations)} regional DCs show projected stockouts"
                    if at_risk else "No projected stockouts at any regional DC — healthy"),
        "locations": locations,
    })


# ------------------------------------------------ OpenAI tool specs + dispatch

_INTERVENTION_SCHEMA = {
    "type": "array",
    "description": ("Interventions to apply. Each item: {variable, and exactly one of "
                    "value | multiplier | add}. Variables: on_promotion (0/1), "
                    "supply_factor (0-1; 1 = no disruption), demand, units_received, "
                    "on_hand_start."),
    "items": {
        "type": "object",
        "properties": {
            "variable": {"type": "string"},
            "value": {"type": ["number", "null"]},
            "multiplier": {"type": ["number", "null"]},
            "add": {"type": ["number", "null"]},
        },
        "required": ["variable"],
    },
}

_WHERE_SCHEMA = {
    "type": "object",
    "description": "Row filter. Omit fields you don't need.",
    "properties": {
        "sku_id": {"type": ["string", "null"]},
        "loc_id": {"type": ["string", "null"], "description": "e.g. RDC-03"},
        "category": {"type": ["string", "null"]},
        "week_from": {"type": ["integer", "null"], "description": "week_index 0-76"},
        "week_to": {"type": ["integer", "null"]},
        "projected": {"type": ["boolean", "null"],
                      "description": "true = future weeks only, false = history only"},
    },
}

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "estimate_effect",
        "description": ("Rigorous causal effect estimate (DoWhy backdoor adjustment + "
                        "refutation tests) of a treatment on an outcome across regional-DC "
                        "weekly snapshots. Use AFTER identifying the treatment via the graph. "
                        "Treatments: 'on_promotion' or a disruption event id ('EVT-001'..'EVT-006')."),
        "parameters": {"type": "object", "properties": {
            "treatment": {"type": "string"},
            "outcome": {"type": "string",
                        "enum": ["units_sold", "stockout", "units_received", "demand", "on_hand_end"]},
        }, "required": ["treatment", "outcome"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "what_if",
        "description": ("What-if simulation over many DC-weeks via the fitted structural "
                        "causal model: apply interventions (e.g. promo on, disruption removed, "
                        "demand +20%) to the snapshot weeks matching `where` and compare "
                        "baseline vs intervened aggregates. Works on historical AND projected weeks."),
        "parameters": {"type": "object", "properties": {
            "interventions": _INTERVENTION_SCHEMA,
            "where": _WHERE_SCHEMA,
        }, "required": ["interventions"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "counterfactual",
        "description": ("Counterfactual for ONE specific (DC, SKU, week): 'would this week's "
                        "stockout have happened if X had been different?' Abduction-action-"
                        "prediction on the observed row (Pearl rung 3)."),
        "parameters": {"type": "object", "properties": {
            "loc_id": {"type": "string"}, "sku_id": {"type": "string"},
            "week_index": {"type": "integer"},
            "interventions": _INTERVENTION_SCHEMA,
        }, "required": ["loc_id", "sku_id", "week_index", "interventions"],
            "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "explain_stockout_risk",
        "description": ("Future stockout risk report for a SKU (optionally one DC): which "
                        "projected weeks (52-76) stock out and WHY — demand attainment vs "
                        "forecast, active/announced disruptions, low starting inventory, "
                        "planned promotions. Use for any 'will X stock out / is X at risk / "
                        "why is X at risk' question."),
        "parameters": {"type": "object", "properties": {
            "sku_id": {"type": "string"},
            "loc_id": {"type": ["string", "null"]},
        }, "required": ["sku_id"], "additionalProperties": False},
    }},
]

_HANDLERS = {
    "estimate_effect": lambda a: estimate_effect(a.get("treatment", ""), a.get("outcome", "units_sold")),
    "what_if": lambda a: what_if(a.get("interventions", []), a.get("where")),
    "counterfactual": lambda a: counterfactual(a.get("loc_id", ""), a.get("sku_id", ""),
                                               a.get("week_index", -1), a.get("interventions", [])),
    "explain_stockout_risk": lambda a: explain_stockout_risk(a.get("sku_id", ""), a.get("loc_id")),
}


def run_tool(name: str, args: dict) -> dict:
    try:
        return _HANDLERS[name](args or {})
    except KeyError:
        return {"error": f"unknown causal tool '{name}'"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
