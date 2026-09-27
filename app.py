"""
QueryNative — NLP to SQL Flask Web App
- Multi-turn conversation memory + autocomplete
- Dual-engine: PostgreSQL cloud DB  ←→  Local file upload (CSV / XLSX / SQL / SQLite)
- Groq used ONLY for intent extraction; ingestion + SQL execution is 100% local.
"""
from dotenv import load_dotenv
load_dotenv()

import os
import sys
import re
import io
import csv
import base64
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, request, jsonify, render_template_string, Response

from nlp.groq_parser import parse_query, repair_sql
from nlp.text_parser import ENTITY_KEYWORDS, AGGREGATE_KEYWORDS
from schema.schema_linker import link_schema
from sql_engine.query_builder import build_query
from sql_engine.query_validator import validate_query
from sql_engine.sql_executor import execute_query
from db.db_connection import set_connection_config, test_connection, get_current_config
from db.schema_discovery import build_schema_string, get_schema_summary

# ── NEW: local upload engine ────────────────────────────────────────────────
from upload_handler import upload_manager

app = Flask(__name__)
# 64 MB upload cap (plenty for csv/xlsx/sql/sqlite while staying sane)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

conversation_memory = []
MAX_MEMORY = 20

# Cached schema string for Groq (refreshed on connect / upload activate)
_schema_string = ""


def _is_local_active() -> bool:
    return upload_manager.is_active()


def get_schema_str():
    """Schema string fed to Groq.  Switches based on the active source."""
    global _schema_string
    if _is_local_active():
        # always pull fresh so newly uploaded tables are visible
        return upload_manager.get_schema_string()
    if not _schema_string:
        try:
            _schema_string = build_schema_string()
        except Exception as e:
            print(f"Schema discovery error: {e}")
            _schema_string = ""
    return _schema_string


# ─────────────────────────────────────────────
# MULTI-TURN CONTEXT RESOLUTION  (unchanged)
# ─────────────────────────────────────────────
FOLLOWUP_PATTERNS = [
    r"^(show|filter|only|where|but|with|for|in|from|limit|top|sort|order)\b",
    r"^(now|also|and|what about|how about)\b",
    r"^(just|give me|can you|narrow|restrict)\b",
]
REFERENCE_WORDS = ["it", "them", "those", "that", "these", "this", "same", "above"]


def is_followup_question(question):
    q = question.lower().strip()
    words = q.split()

    all_entity_keywords = [kw for kws in ENTITY_KEYWORDS.values() for kw in kws]
    has_entity = any(re.search(r'\b' + re.escape(kw) + r'\b', q) for kw in all_entity_keywords)

    all_agg_keywords = [kw for kws in AGGREGATE_KEYWORDS.values() for kw in kws]
    has_aggregation = any(kw in q for kw in all_agg_keywords)
    has_limit = bool(re.search(r'\btop\s+\d+\b', q))
    has_year = bool(re.search(r'\b(19|20)\d{2}\b', q))

    if has_entity and (has_aggregation or has_limit):
        return False

    is_fragment = not has_entity and not has_aggregation and not has_limit
    if is_fragment:
        for pat in FOLLOWUP_PATTERNS:
            if re.match(pat, q):
                return True
        if any(word in words for word in REFERENCE_WORDS):
            return True
        if has_year:
            return True
    return False


def merge_parsed(base_parsed, new_parsed):
    merged = dict(base_parsed)
    for key in ("entity", "aggregation", "group_by", "time_dimension", "order_by"):
        if new_parsed.get(key) is not None:
            merged[key] = new_parsed[key]

    old_filters = [f for f in (base_parsed.get("filters") or []) if f]
    new_filters = [f for f in (new_parsed.get("filters") or []) if f]
    if new_filters:
        new_types = {f["type"] for f in new_filters}
        kept_old = [f for f in old_filters if f["type"] not in new_types]
        merged["filters"] = kept_old + new_filters
    else:
        merged["filters"] = old_filters or None

    if new_parsed.get("limit") is not None:
        merged["limit"] = new_parsed["limit"]

    merged["original_text"] = new_parsed.get("original_text", "")
    return merged


def human_readable_merged(merged):
    parts = []
    if merged.get("entity"): parts.append(merged["entity"])
    if merged.get("aggregation"): parts.append(f"({merged['aggregation']})")
    if merged.get("group_by"): parts.append(f"by {merged['group_by']}")
    if merged.get("limit"): parts.append(f"top {merged['limit']}")
    for f in (merged.get("filters") or []):
        ft, fv = f.get("type"), f.get("value")
        if ft == "year": parts.append(f"year={fv}")
        elif ft == "country": parts.append(f"country={fv}")
        elif ft == "price_gt": parts.append(f"price>{fv}")
        elif ft == "price_lt": parts.append(f"price<{fv}")
        elif ft == "category": parts.append(f"category={fv}")
        elif ft == "discontinued": parts.append("discontinued")
    return " | ".join(parts) if parts else "?"


def resolve_followup(question, last_memory):
    if not last_memory: return None
    last_parsed = last_memory.get("parsed_intent")
    if not last_parsed: return None
    schema_summary = upload_manager.get_schema_summary() if _is_local_active() else get_schema_summary()
    new_parsed = parse_query(question, get_schema_str(), schema_summary)
    return merge_parsed(last_parsed, new_parsed)


# ─────────────────────────────────────────────
# QUERY VALIDATION
# ─────────────────────────────────────────────
def is_valid_query(parsed):
    question = parsed.get("original_text", "").strip().lower()
    INVALID = {
        "hi","hello","hey","thanks","thank you","ok","okay","bye","yes","no","cool",
        "what","why","how","who","lol","haha","nice","great","good","bad","sure",
        "maybe","idk","hmm","bruh","bro","omg","wow","damn","really"
    }
    if question in INVALID: return False
    DB_KEYWORDS = [
        "how many","count","total","average","avg","sum","max","min","top","show",
        "list","find","get","fetch","highest","lowest","most","least","best","worst",
        "revenue","sales","salary","income","price","rate","age","year","month",
        "by","from","where","group","order","filter","between","greater","less",
        "all","each","per","number","amount","value","data","record","table",
        "employee","customer","product","order","category","department","gender",
        "attrition","discontinued","country","city","region","date","time",
        "survey","question","answer","response"
    ]
    has_db_word = any(kw in question for kw in DB_KEYWORDS)
    if not has_db_word:
        try:
            schema = upload_manager.get_schema_summary() if _is_local_active() else get_schema_summary()
            schema_terms = []
            for table, cols in schema.items():
                schema_terms.append(str(table).lower().replace("_", " "))
                schema_terms.extend(str(c).lower().replace("_", " ") for c in cols)
            has_db_word = any(term and term in question for term in schema_terms)
        except Exception:
            has_db_word = False
    if not has_db_word: return False
    has_intent = any([
        parsed.get("aggregation"), parsed.get("entity"), parsed.get("group_by"),
        parsed.get("filters"), parsed.get("time_dimension"),
    ])
    return has_intent


# ─────────────────────────────────────────────
# CHART GENERATION  (unchanged — Sage / Charcoal)
# ─────────────────────────────────────────────
def detect_chart_type(columns, rows, parsed, question):
    if len(columns) != 2 or len(rows) == 0: return None
    try: [float(r[1]) for r in rows]
    except (ValueError, TypeError): return None

    q = question.lower()
    group_by = parsed.get("group_by", "")
    time_dim = parsed.get("time_dimension")
    aggregation = parsed.get("aggregation")
    n_rows = len(rows)

    if time_dim in ("year","month","quarter","day") or group_by in ("year","month","quarter","day"):
        return "line"
    if any(w in q for w in ["trend","over time","by year","by month","by quarter","monthly","yearly","annual"]):
        return "line"
    if 2 <= n_rows <= 6 and aggregation in ("sum","count"):
        if any(w in q for w in ["share","proportion","breakdown","distribution","percentage","pie"]):
            return "pie"
        if group_by in ("category","country") and n_rows <= 5:
            return "pie"
    labels = [str(r[0]) for r in rows]
    avg_label_len = sum(len(l) for l in labels) / len(labels)
    if avg_label_len > 12 or n_rows > 8:
        return "horizontal_bar"
    return "bar"


def generate_chart_base64(columns, rows, question, parsed):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        chart_type = detect_chart_type(columns, rows, parsed, question)
        if not chart_type: return None, None

        labels = [str(r[0]) for r in rows]
        values = [float(r[1]) for r in rows]

        BG, SAGE, SAGE2 = "#212121", "#769382", "#5A8A72"
        GRID, TEXT, TEXT2 = "#404040", "#AAAAAA", "#777777"
        COLORS = ["#769382","#5A8A72","#4A7A63","#7BA090","#8BB0A0",
                  "#3A6A53","#9BBFB0","#2A5A43","#ABBFC0","#1A4A33"]

        title = " ".join(w.capitalize() for w in question.split()[:7])

        if chart_type == "bar":
            fig, ax = plt.subplots(figsize=(10,5))
            fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
            bc = [SAGE2]*len(values)
            if values: bc[values.index(max(values))] = SAGE
            bars = ax.bar(labels, values, color=bc, width=0.55, edgecolor=BG, linewidth=1.2, zorder=3)
            ax.yaxis.grid(True, color=GRID, linewidth=0.7, zorder=0); ax.set_axisbelow(True)
            for s in ["top","right"]: ax.spines[s].set_visible(False)
            ax.spines["left"].set_color(GRID); ax.spines["bottom"].set_color(GRID)
            ax.tick_params(axis="x", rotation=25 if len(labels)>5 else 0, labelsize=9, colors=TEXT)
            ax.tick_params(axis="y", labelsize=9, colors=TEXT)
            ax.set_xlabel(columns[0], fontsize=10, color=TEXT2, labelpad=8)
            ax.set_ylabel(columns[1], fontsize=10, color=TEXT2, labelpad=8)
            for bar, val in zip(bars, values):
                ax.text(bar.get_x()+bar.get_width()/2, bar.get_height()+max(values)*0.012,
                        f"{val:,.0f}", ha="center", va="bottom", fontsize=8, color=SAGE)
            ax.set_title(title, fontsize=13, color="#E8E8E8", fontweight="bold", pad=16)

        elif chart_type == "horizontal_bar":
            fig, ax = plt.subplots(figsize=(10, max(4, len(rows)*0.45)))
            fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
            yp = range(len(labels))
            bc = [SAGE2]*len(values)
            if values: bc[values.index(max(values))] = SAGE
            bars = ax.barh(list(yp), values, color=bc, height=0.6, edgecolor=BG, linewidth=1.0)
            ax.set_yticks(list(yp)); ax.set_yticklabels(labels, fontsize=9, color=TEXT)
            ax.xaxis.grid(True, color=GRID, linewidth=0.7, zorder=0); ax.set_axisbelow(True)
            for s in ["top","right"]: ax.spines[s].set_visible(False)
            ax.spines["left"].set_color(GRID); ax.spines["bottom"].set_color(GRID)
            ax.tick_params(axis="x", labelsize=9, colors=TEXT)
            for bar, val in zip(bars, values):
                ax.text(bar.get_width()+max(values)*0.01, bar.get_y()+bar.get_height()/2,
                        f"{val:,.0f}", va="center", fontsize=8, color=SAGE)
            ax.set_xlabel(columns[1], fontsize=10, color=TEXT2, labelpad=8)
            ax.set_title(title, fontsize=13, color="#E8E8E8", fontweight="bold", pad=16)

        elif chart_type == "line":
            fig, ax = plt.subplots(figsize=(10,5))
            fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
            x = range(len(labels))
            ax.plot(list(x), values, color=SAGE, linewidth=2.5, marker="o", markersize=6,
                    markerfacecolor=SAGE, markeredgecolor=BG, markeredgewidth=2, zorder=3)
            ax.fill_between(list(x), values, alpha=0.12, color=SAGE)
            ax.yaxis.grid(True, color=GRID, linewidth=0.7, zorder=0); ax.set_axisbelow(True)
            for s in ["top","right"]: ax.spines[s].set_visible(False)
            ax.spines["left"].set_color(GRID); ax.spines["bottom"].set_color(GRID)
            ax.set_xticks(list(x))
            ax.set_xticklabels(labels, fontsize=9, color=TEXT, rotation=25 if len(labels)>6 else 0)
            ax.tick_params(axis="y", labelsize=9, colors=TEXT)
            ax.set_xlabel(columns[0], fontsize=10, color=TEXT2, labelpad=8)
            ax.set_ylabel(columns[1], fontsize=10, color=TEXT2, labelpad=8)
            for xi, val in zip(x, values):
                ax.text(xi, val+max(values)*0.025, f"{val:,.0f}", ha="center", fontsize=8, color=SAGE)
            ax.set_title(title, fontsize=13, color="#E8E8E8", fontweight="bold", pad=16)

        elif chart_type == "pie":
            fig, ax = plt.subplots(figsize=(8,6))
            fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
            wedges, texts, autotexts = ax.pie(
                values, labels=labels, colors=COLORS[:len(values)],
                autopct="%1.1f%%", startangle=140, pctdistance=0.82,
                wedgeprops={"edgecolor": BG, "linewidth": 2}
            )
            for t in texts: t.set_color(TEXT); t.set_fontsize(9)
            for t in autotexts: t.set_color("#E8E8E8"); t.set_fontsize(8); t.set_fontweight("bold")
            ax.set_title(title, fontsize=13, color="#E8E8E8", fontweight="bold", pad=16)

        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=BG)
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8"), chart_type

    except Exception as e:
        print(f"Chart error: {e}")
        traceback.print_exc()
        return None, None


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@app.route("/")
def index():
    with open(os.path.join(os.path.dirname(__file__), "templates", "index.html"),
              encoding="utf-8") as f:
        return render_template_string(f.read())


# ── PostgreSQL connect (unchanged behaviour) ────────────────────────────────
@app.route("/api/connect", methods=["POST"])
def api_connect():
    global _schema_string
    data = request.get_json()
    host = data.get("host", "127.0.0.1")
    port = data.get("port", 5432)
    dbname = data.get("dbname", "")
    user = data.get("user", "")
    password = data.get("password", "")

    if not dbname or not user:
        return jsonify({"success": False, "error": "Database name and username are required."}), 400

    ok, err = test_connection(host, dbname, user, password, port)
    if not ok:
        return jsonify({"success": False, "error": f"Connection failed: {err}"}), 400

    set_connection_config(host, dbname, user, password, port)
    _schema_string = ""

    # Connecting to a real DB always switches off any active local upload.
    upload_manager.deactivate()

    try:
        schema_str = get_schema_str()
        schema_summary = get_schema_summary()
        conversation_memory.clear()
        return jsonify({
            "success": True,
            "dbname": dbname,
            "host": host,
            "tables": len(schema_summary),
            "schema": schema_summary,
        })
    except Exception as e:
        return jsonify({"success": False, "error": f"Schema discovery failed: {str(e)}"}), 500


@app.route("/api/connection", methods=["GET"])
def api_connection_status():
    """Return the *active* source: local upload OR PostgreSQL."""
    if _is_local_active():
        return jsonify({
            "connected": True,
            "source": "local",
            "config": {
                "dbname": upload_manager.source_name,
                "host": "local file",
                "port": "—",
            },
            "schema": upload_manager.get_schema_summary(),
            "row_counts": upload_manager.get_table_row_counts(),
            "upload": upload_manager.get_status(),
        })
    try:
        cfg = get_current_config()
        summary = get_schema_summary()
        return jsonify({
            "connected": True,
            "source": "postgres",
            "config": cfg,
            "schema": summary,
            "upload": upload_manager.get_status(),
        })
    except Exception as e:
        return jsonify({
            "connected": False,
            "source": None,
            "error": str(e),
            "upload": upload_manager.get_status(),
        })


# ── NEW: file upload endpoints ──────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Receive a file, ingest into a persistent SQLite, return preview."""
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file part."}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"success": False, "error": "No file selected."}), 400
    declared_kind = request.form.get("kind") or request.form.get("type") or None

    result = upload_manager.handle_upload(f, declared_kind)
    if not result.get("success"):
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/upload/activate", methods=["POST"])
def api_upload_activate():
    """Mark the currently uploaded file as the ACTIVE source for queries."""
    global _schema_string
    if not upload_manager.activate():
        return jsonify({"success": False, "error": "No upload to activate."}), 400
    # Switch the Groq schema string + clear chat memory so follow-ups don't
    # leak between sources.
    _schema_string = ""
    conversation_memory.clear()
    return jsonify({
        "success": True,
        "message": "Local file is now the active source.",
        "schema_id": upload_manager._schema_id(),
        "tables": len(upload_manager.tables),
    })


@app.route("/api/upload/deactivate", methods=["POST"])
def api_upload_deactivate():
    """Stop using the uploaded file; revert to the configured PostgreSQL DB."""
    global _schema_string
    upload_manager.deactivate()
    _schema_string = ""
    conversation_memory.clear()
    return jsonify({"success": True, "message": "Reverted to cloud database."})


@app.route("/api/upload/status", methods=["GET"])
def api_upload_status():
    return jsonify(upload_manager.get_status())


@app.route("/api/upload/reset", methods=["POST"])
def api_upload_reset():
    """Discard the current upload entirely (deletes the temp .sqlite file)."""
    upload_manager.reset()
    return jsonify({"success": True})


# ── /api/query — switches engine based on the active source ────────────────
@app.route("/api/query", methods=["POST"])
def api_query():
    data = request.get_json()
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "No question provided"}), 400

    result = {
        "question": question, "resolved_question": None, "is_followup": False,
        "used_groq": False, "steps": {}, "sql": None, "columns": [],
        "rows": [], "chart": None, "chart_type": None, "error": None, "invalid": False,
        "source": "local" if _is_local_active() else "postgres",
    }

    try:
        # Multi-turn resolution
        last = conversation_memory[-1] if conversation_memory else None
        force_followup = data.get("force_followup", False)
        is_followup = last and (force_followup or is_followup_question(question))

        if is_followup:
            merged = resolve_followup(question, last)
            if merged:
                parsed = merged
                result["is_followup"] = True
                result["resolved_question"] = human_readable_merged(merged)
            else:
                schema_summary = upload_manager.get_schema_summary() if _is_local_active() else get_schema_summary()
                parsed = parse_query(question, get_schema_str(), schema_summary)
        else:
            schema_summary = upload_manager.get_schema_summary() if _is_local_active() else get_schema_summary()
            parsed = parse_query(question, get_schema_str(), schema_summary)

        if not is_valid_query(parsed):
            result["invalid"] = True
            result["error"] = ('That doesn\'t look like a database query. Try asking '
                               'things like "How many records?", "Count by category", '
                               'or "Average value by group".')
            return jsonify(result)

        result["used_groq"] = parsed.get("used_groq", False)
        result["steps"]["parsed"] = {
            "entity": parsed.get("entity"),
            "aggregation": parsed.get("aggregation"),
            "group_by": parsed.get("group_by"),
            "limit": parsed.get("limit"),
            "order_by": parsed.get("order_by"),
            "filters": str(parsed.get("filters")) if parsed.get("filters") else None,
            "time_dimension": parsed.get("time_dimension"),
        }

        # ── Engine switch ─────────────────────────────────────────────
        if _is_local_active():
            sql, columns, rows = upload_manager.execute_intent(parsed)
            result["sql"] = sql.strip()
            result["columns"] = columns
            result["rows"] = [list(r) for r in rows]
            result["steps"]["linked"] = {
                "base_table": "(local)",
                "metric_table": "(local)",
                "metric_column": None,
                "group_table": "(local)",
                "joins": "0 join(s)",
            }
            result["steps"]["validation"] = "Valid (SQLite local)"
        else:
            linked = link_schema(parsed, None)
            result["steps"]["linked"] = {
                "base_table": linked.get("base_table"),
                "metric_table": linked.get("metric_table"),
                "metric_column": linked.get("metric_column"),
                "group_table": linked.get("group_table"),
                "joins": f"{len(linked.get('joins', []))} join(s)",
            }
            sql = build_query(parsed, linked)
            result["sql"] = sql.strip()
            valid, msg = validate_query(sql, preflight=True)
            result["steps"]["validation"] = msg
            if not valid:
                repaired = repair_sql(question, get_schema_str(), sql, msg)
                if repaired:
                    repaired_valid, repaired_msg = validate_query(repaired, preflight=True)
                    result["steps"]["repair"] = repaired_msg
                    if repaired_valid:
                        sql = repaired
                        valid = True
                        msg = "Valid after repair"
                        result["sql"] = sql.strip()
                        result["steps"]["validation"] = "Valid after repair"
                    else:
                        result["error"] = f"SQL Repair Failed: {repaired_msg}"
                        return jsonify(result)
                else:
                    result["error"] = f"SQL Validation Failed: {msg}"
                    return jsonify(result)
            
            if valid:
                try:
                    columns, rows = execute_query(sql)
                except Exception as exec_error:
                    repaired = repair_sql(question, get_schema_str(), sql, str(exec_error))
                    if not repaired:
                        raise
                    repaired_valid, repaired_msg = validate_query(repaired, preflight=True)
                    result["steps"]["repair"] = repaired_msg
                    if not repaired_valid:
                        result["error"] = f"SQL Repair Failed: {repaired_msg}"
                        return jsonify(result)
                    columns, rows = execute_query(repaired)
                    result["sql"] = repaired.strip()
                    result["steps"]["validation"] = "Valid after repair"
                result["columns"] = columns
                result["rows"] = [list(row) for row in rows]

        # Charting (works for either engine — it's just data)
        chart_b64, chart_type = generate_chart_base64(
            result["columns"], result["rows"], question, parsed
        )
        result["chart"] = chart_b64
        result["chart_type"] = chart_type

        conversation_memory.append({
            "question": question,
            "sql": (result["sql"] or "").strip(),
            "row_count": len(result["rows"]),
            "columns": result["columns"],
            "parsed_intent": {
                "entity": parsed.get("entity"),
                "aggregation": parsed.get("aggregation"),
                "group_by": parsed.get("group_by"),
                "limit": parsed.get("limit"),
                "order_by": parsed.get("order_by"),
                "filters": parsed.get("filters"),
                "time_dimension": parsed.get("time_dimension"),
                "original_text": parsed.get("original_text", question),
            }
        })
        if len(conversation_memory) > MAX_MEMORY:
            conversation_memory.pop(0)

    except Exception as e:
        result["error"] = str(e)
        print(traceback.format_exc())

    return jsonify(result)


@app.route("/api/autocomplete", methods=["GET"])
def autocomplete():
    q = request.args.get("q", "").strip().lower()
    if not q or len(q) < 2:
        return jsonify({"suggestions": []})
    pool = [
        "Count — How many [table]?",
        "Count grouped — Count by [column]",
        "Count + filter — Count where [column] = value",
        "Count + year filter — Count in [year]",
        "Count + limit — Top 5 by count",
        "Average grouped — Average [column] by [group]",
        "Sum grouped — Total [column] by [group]",
        "Sum + time — Total by year",
        "Max / Min — Highest [column]",
        "Max / Min — Lowest [column]",
        "Single aggregate — Total [column]",
        "Filter — Records where [column] > value",
        "Ranked — Top 10 by [column]",
    ]
    matches = [s for s in pool if q in s.lower()]
    matches.sort(key=lambda s: (0 if s.lower().startswith(q) else 1, s))
    return jsonify({"suggestions": matches[:6]})


@app.route("/api/export/csv", methods=["POST"])
def export_csv():
    data = request.get_json()
    columns = data.get("columns", [])
    rows = data.get("rows", [])
    question = data.get("question", "result")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    writer.writerows(rows)
    buf.seek(0)

    filename = "_".join(question.lower().split()[:5]).replace("/","_") + ".csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/api/memory", methods=["GET"])
def api_memory():
    return jsonify({"memory": conversation_memory})


@app.route("/api/memory", methods=["DELETE"])
def api_clear_memory():
    conversation_memory.clear()
    return jsonify({"status": "cleared"})


@app.route("/api/schema", methods=["GET"])
def api_schema():
    """Active-source-aware schema: returns tables + row counts."""
    try:
        if _is_local_active():
            return jsonify({
                "source": "local",
                "source_name": upload_manager.source_name,
                "schema": upload_manager.get_schema_summary(),
                "row_counts": upload_manager.get_table_row_counts(),
            })
        summary = get_schema_summary()
        # Best-effort row counts for PG
        row_counts = {}
        try:
            from db.db_connection import get_connection
            with get_connection() as conn:
                cur = conn.cursor()
                for t in summary.keys():
                    try:
                        cur.execute(f'SELECT COUNT(*) FROM "{t}"')
                        row_counts[t] = cur.fetchone()[0]
                    except Exception:
                        row_counts[t] = None
                cur.close()
        except Exception:
            pass
        return jsonify({
            "source": "postgres",
            "schema": summary,
            "row_counts": row_counts,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/suggestions", methods=["GET"])
def api_suggestions():
    suggestions = [
        "Count — How many [table]?",
        "Count grouped — Count by [column]",
        "Count + filter — Count where [column] = value",
        "Count + year filter — Count in [year]",
        "Count + limit — Top 5 by count",
        "Average grouped — Average [column] by [group]",
        "Sum grouped — Total [column] by [group]",
        "Sum + time — Total by year",
        "Sum + time — Total by month",
        "Max — Highest [column]",
        "Min — Lowest [column]",
        "Single aggregate — Total [column]",
        "Filter — Records where [column] > value",
        "Filter — Records from [country/category]",
        "Ranked — Top 10 by [column]",
    ]
    return jsonify({"suggestions": suggestions})


if __name__ == "__main__":
    cfg = get_current_config() or {}
    db_status = f"{cfg.get('dbname', 'Not connected')} @ {cfg.get('host', 'N/A')}:{cfg.get('port', 'N/A')}"
    print("\n╔══════════════════════════════════════════════╗")
    print("║         QueryNative  ·  NLP → SQL            ║")
    print("╠══════════════════════════════════════════════╣")
    print(f"║  Status   : {db_status:<33}║")
    print("║  Open     : http://127.0.0.1:5000            ║")
    print("╚══════════════════════════════════════════════╝\n")
    app.run(debug=False, port=5000, use_reloader=False)
