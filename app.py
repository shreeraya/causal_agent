"""
Supply Chain Causal Agent — chatbot backend (OpenAI).

FastAPI app serving a chat UI (static/index.html) and POST /api/chat.
Each chat turn runs an agentic loop: an OpenAI model grounded in the graph
ontology, with one tool — run_cypher — that executes read-only Cypher against
the local Neo4j supply chain graph. The agent classifies questions as factual
(direct graph lookups) or causal (diff-in-diff style comparisons over the
WeeklySnapshot time series) and answers from query results only.

Run:  uvicorn app:app --port 8000        (needs OPENAI_API_KEY in env or .env)
"""

import os
import re
import json
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from neo4j import GraphDatabase
from neo4j.time import Date, DateTime

ROOT = Path(__file__).parent

# --- minimal .env loader (no python-dotenv dependency)
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import openai  # noqa: E402  (import after .env load so the client sees the key)

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_AUTH = (os.environ.get("NEO4J_USERNAME") or os.environ.get("NEO4J_USER", "neo4j"),
              os.environ.get("NEO4J_PASSWORD", "supplychain123"))
MAX_TOOL_ROUNDS = 12
MAX_ROWS = 60

# --- demo hardening: shared access code + per-IP rate limit
DEMO_PASSCODE = os.environ.get("DEMO_PASSCODE", "")   # empty = gate disabled (local dev)
RATE_LIMIT_PER_MIN = 8         # chat requests per IP per minute
MAX_MESSAGE_CHARS = 2000
MAX_HISTORY_TURNS = 30
_request_log: dict[str, deque] = defaultdict(deque)


def client_ip(request: Request) -> str:
    # behind a Cloudflare tunnel the real client IP arrives in this header
    return request.headers.get("CF-Connecting-IP") or (request.client.host if request.client else "?")


def rate_limited(ip: str) -> bool:
    now = time.time()
    q = _request_log[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MIN:
        return True
    q.append(now)
    return False

driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
client = openai.OpenAI() if os.environ.get("OPENAI_API_KEY") else None

SYSTEM_PROMPT = """\
You are a supply chain analyst agent answering questions over a Neo4j knowledge \
graph of a consumer-goods supply chain. Today's date is 2026-07-06.

# Graph ontology

4-echelon network: Supplier -> Plant -> central DC -> regional DC -> Store.

Nodes (key properties):
- (:Supplier {supplier_id, name, location, reliability})            8 suppliers
- (:RawMaterial {name})                                             25 materials
- (:Plant {plant_id, name, location, categories})                   3 plants
- (:DistributionCenter {dc_id, name, location, tier})               tier: 'central' (2) | 'regional' (6)
- (:Store {store_id, name, city, format})                           20 stores
- (:Product {sku_id, name, unit_cost, list_price, shelf_life_days, weight_kg})  120 SKUs
- (:Category {name})   Beverages, Snacks, Personal Care, Home Care, Dairy
- (:DisruptionEvent {event_id, type, severity, start_date (date), duration_days, description})
- (:WeeklySnapshot {loc_id, sku_id, week_start (date), week_index, demand, units_sold,
   units_received, on_hand_start, on_hand_end, stockout (bool), on_promotion (bool), price})
   52 weeks: week_index 0 = 2025-07-07 ... week_index 51 = 2026-06-29, at every stocked
   (location, SKU) pair, for stores AND distribution centers.

Relationships:
- (Supplier)-[:SUPPLIES {cost_per_unit}]->(RawMaterial)
- (Supplier)-[:SHIPS_TO {lead_time_days, transport_mode}]->(Plant)
- (Plant)-[:SHIPS_TO {lead_time_days}]->(DistributionCenter {tier:'central'})
- (central DC)-[:SHIPS_TO {lead_time_days}]->(regional DC)-[:SHIPS_TO {lead_time_days}]->(Store)
- (Product)-[:MADE_FROM {qty_per_unit}]->(RawMaterial)          (bill of materials)
- (Plant)-[:PRODUCES {capacity_per_week, unit_cost}]->(Product)
- (Store|DC)-[:STOCKS {on_hand, safety_stock, reorder_point, fill_rate, stockout_events_90d}]->(Product)
- (Store)-[:SELLS {avg_weekly_demand, demand_std, price, on_promotion, discount_pct}]->(Product)
- (Product)-[:IN_CATEGORY]->(Category)
- (DisruptionEvent)-[:AFFECTS]->(Supplier|Plant|DistributionCenter)
- (Store|DC)-[:HAS_SNAPSHOT]->(WeeklySnapshot)-[:FOR_PRODUCT]->(Product)

# How to answer

First classify the question:

FACTUAL (what/where/how much/when): answer with direct Cypher lookups.
Examples: inventory positions, lead times along SHIPS_TO paths, which supplier
provides a material, assortments, top sellers.

CAUSAL (why/what caused/what is the effect of/what would happen if): use the
WeeklySnapshot time series and the graph structure together. Methodology:
1. Identify the treatment (a DisruptionEvent, a promotion, etc.) and trace its
   causal pathway through the graph (e.g. event -> supplier -> material -> BOM
   -> affected SKUs -> downstream locations).
2. Build treated vs control cohorts (affected vs unaffected SKUs or stores) and
   before vs during/after windows from week_index. Supply disruptions propagate
   with roughly a 1-week lag per echelon, so look for effects a few weeks after
   an event's start_date.
3. Compute diff-in-diff style aggregates in Cypher (avg units_received, stockout
   rate = avg(CASE WHEN x.stockout THEN 1.0 ELSE 0.0 END), units_sold) across
   the four cells: treated/control x before/during.
4. Watch for confounders: seasonality (compare against unaffected SKUs in the
   same weeks, not just the same SKUs earlier) and promotions (on_promotion
   changes demand and price). Say explicitly when an association could be
   confounded and how you controlled for it.
5. Report effect sizes (differences, ratios) not just point estimates, and be
   honest about correlation vs causation.

# Canonical query patterns (adapt these — do not pull raw rows and eyeball them)

Promotion effect (treatment varies per snapshot week — ALWAYS use
WeeklySnapshot.on_promotion for causal analysis; SELLS.on_promotion is only the
current week's static flag):

    MATCH (:Store)-[:HAS_SNAPSHOT]->(x:WeeklySnapshot)
    RETURN x.on_promotion AS promo,
           avg(x.units_sold) AS avg_units_sold,
           avg(x.price) AS avg_price,
           count(*) AS n_store_weeks

Disruption diff-in-diff (4 cells: affected/unaffected x before/during; shift the
impact window a few weeks after the event start for propagation lag):

    MATCH (e:DisruptionEvent {event_id: 'EVT-004'})-[:AFFECTS]->(:Supplier)
          -[:SUPPLIES]->(:RawMaterial)<-[:MADE_FROM]-(p:Product)
    WITH collect(DISTINCT p.sku_id) AS hit
    MATCH (:Store)-[:HAS_SNAPSHOT]->(x:WeeklySnapshot)
    RETURN x.sku_id IN hit AS affected,
           x.week_index >= 44 AND x.week_index <= 48 AS during_impact,
           avg(CASE WHEN x.stockout THEN 1.0 ELSE 0.0 END) AS stockout_rate,
           avg(x.units_received) AS avg_received,
           count(*) AS n
    ORDER BY affected, during_impact

Lead-time path (lead_time_days lives on the SHIPS_TO relationships):

    MATCH path = (sup:Supplier {name: 'GlobalSweet Ltd'})-[:SHIPS_TO*..5]->(st:Store {city: 'Boston'})
    RETURN [n IN nodes(path) | coalesce(n.name, n.dc_id)] AS route,
           reduce(t = 0, r IN relationships(path) | t + r.lead_time_days) AS total_days
    ORDER BY total_days LIMIT 1

Rules:
- String properties are case-sensitive ('Sugar', not 'sugar'). Use the exact
  entity names from the vocabulary below. If a name lookup returns 0 rows,
  retry with WHERE toLower(x.name) CONTAINS toLower('...') before concluding
  the entity doesn't exist.
- Ground every claim in query results. Never invent numbers. If a query returns
  nothing, say so and try a different angle.
- Prefer several small queries over one giant query. Aggregate in Cypher rather
  than pulling raw rows.
- Results are capped at 60 rows; always use aggregation/ORDER BY/LIMIT.
- Cypher only reads: CREATE/MERGE/SET/DELETE are blocked.
- Answer concisely in markdown. Lead with the answer, then the supporting
  evidence (key numbers). Mention caveats briefly. Use tables for small
  comparisons. Don't dump raw query output.
"""

def build_vocabulary() -> str:
    """Pull exact entity names from the graph so the model never guesses casing."""
    queries = {
        "RawMaterial names": "MATCH (m:RawMaterial) RETURN m.name AS v ORDER BY v",
        "Suppliers": "MATCH (s:Supplier) RETURN s.supplier_id + ' = ' + s.name AS v ORDER BY v",
        "Plants": "MATCH (p:Plant) RETURN p.plant_id + ' = ' + p.name AS v ORDER BY v",
        "Distribution centers": "MATCH (d:DistributionCenter) RETURN d.dc_id + ' = ' + d.name + ' (' + d.tier + ')' AS v ORDER BY v",
        "Store cities": "MATCH (st:Store) RETURN st.store_id + ' = ' + st.city AS v ORDER BY v",
        "Disruption events": ("MATCH (e:DisruptionEvent) RETURN e.event_id + ' (' + e.type + ', ' "
                              "+ toString(e.start_date) + ', ' + toString(e.duration_days) + 'd): ' "
                              "+ e.description AS v ORDER BY v"),
    }
    sections = []
    try:
        with driver.session() as s:
            for title, q in queries.items():
                values = [r["v"] for r in s.run(q)]
                sections.append(f"{title}: " + "; ".join(values))
    except Exception:
        return ""
    return "\n\n# Entity vocabulary (exact names — copy these verbatim into queries)\n\n" + "\n".join(sections)


SYSTEM_PROMPT += build_vocabulary()

CYPHER_TOOL = {
    "type": "function",
    "function": {
        "name": "run_cypher",
        "description": (
            "Execute a read-only Cypher query against the supply chain Neo4j graph "
            "and return rows as JSON. Write operations are rejected. Results are "
            "capped at 60 rows, so aggregate in the query. Use the ontology from "
            "the system prompt for labels, relationship types, and properties."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The Cypher query to run."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

WRITE_PATTERN = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|FOREACH|CALL\s+\{)\b",
    re.IGNORECASE,
)


def to_jsonable(v):
    if isinstance(v, (Date, DateTime)):
        return str(v)
    if isinstance(v, list):
        return [to_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: to_jsonable(x) for k, x in v.items()}
    if hasattr(v, "items"):  # neo4j Node/Relationship
        return {k: to_jsonable(x) for k, x in dict(v).items()}
    return v


def run_cypher(query: str) -> dict:
    if WRITE_PATTERN.search(query):
        return {"error": "Rejected: only read queries are allowed."}
    try:
        with driver.session() as s:
            result = s.execute_read(
                lambda tx: [r.data() for r in tx.run(query)])
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    truncated = len(result) > MAX_ROWS
    rows = [to_jsonable(r) for r in result[:MAX_ROWS]]
    out = {"row_count": len(result), "rows": rows}
    if truncated:
        out["note"] = f"Truncated to first {MAX_ROWS} of {len(result)} rows. Aggregate instead."
    return out


# ---------------------------------------------------------------- API

app = FastAPI(title="Supply Chain Causal Agent")


class ChatTurn(BaseModel):
    role: str      # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatTurn]  # full history, last item is the new user message


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request):
    if DEMO_PASSCODE and request.headers.get("X-Passcode", "") != DEMO_PASSCODE:
        return JSONResponse(status_code=401,
                            content={"reply": "**Invalid or missing access code.**", "trace": []})
    if rate_limited(client_ip(request)):
        return JSONResponse(status_code=429,
                            content={"reply": "**Slow down** — limit is a few questions per minute.", "trace": []})
    if len(req.messages) > MAX_HISTORY_TURNS or any(len(t.content) > MAX_MESSAGE_CHARS for t in req.messages):
        return JSONResponse(status_code=413,
                            content={"reply": "**Message or conversation too long.** Refresh to start over.", "trace": []})
    if client is None:
        return {
            "reply": ("**No API key configured.** Create a file named `.env` next to "
                      "`app.py` containing:\n\n```\nOPENAI_API_KEY=sk-...\n```\n\n"
                      "then restart the server."),
            "trace": [],
        }

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += [{"role": t.role, "content": t.content} for t in req.messages]
    trace = []
    reply = ""

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=[CYPHER_TOOL],
            )
            msg = response.choices[0].message

            if not msg.tool_calls:
                reply = msg.content or ""
                break

            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
            })
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                    result = run_cypher(args.get("query", ""))
                except json.JSONDecodeError:
                    result = {"error": "Invalid JSON in tool arguments."}
                trace.append({
                    "query": args.get("query", "") if isinstance(args, dict) else "",
                    "row_count": result.get("row_count"),
                    "error": result.get("error"),
                    "preview": result.get("rows", [])[:5],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str),
                })

        if not reply:
            reply = "_The agent hit its tool-call limit without producing a final answer. Try rephrasing._"
        return {"reply": reply, "trace": trace}

    except openai.AuthenticationError:
        return {"reply": "**Invalid API key.** Check `OPENAI_API_KEY` in your `.env` file.",
                "trace": trace}
    except openai.APIStatusError as e:
        return {"reply": f"**API error ({e.status_code}):** {e.message}", "trace": trace}
    except Exception as e:
        return {"reply": f"**Error:** {type(e).__name__}: {e}", "trace": trace}


@app.get("/api/health")
def health():
    try:
        with driver.session() as s:
            s.run("RETURN 1").single()
        neo4j_ok = True
    except Exception:
        neo4j_ok = False
    return {"neo4j": neo4j_ok, "api_key": client is not None, "model": MODEL}


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
