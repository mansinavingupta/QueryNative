# schema/schema_linker.py
# Dynamic schema linker — works with ANY database

def get_discovered_schema():
    try:
        from db.schema_discovery import get_schema_summary
        return get_schema_summary()
    except:
        return {}


def get_catalog():
    try:
        from db.schema_catalog import load_schema_catalog
        return load_schema_catalog()
    except Exception:
        return None


def find_table(entity, schema):
    """Find best matching table for entity string."""
    if not schema or not entity: return None
    entity_l = entity.lower()
    tables = list(schema.keys())
    
    # Exact match
    for t in tables:
        if t.lower() == entity_l: return t
    
    # Plural match: entity + 's' = table name (customers, products, etc)
    plural_entity = entity_l + "s"
    for t in tables:
        if t.lower() == plural_entity: return t
    
    # Exact substring match (prefer simpler names like 'customers' over 'customer_customer_demo')
    # Prioritize: shorter names, names that are just entity_l + suffix, avoid compound names
    candidates = []
    for t in tables:
        if entity_l in t.lower():
            underscores = t.count('_')
            length = len(t)
            candidates.append((underscores, length, t))
    
    if candidates:
        # Sort by: fewer underscores first, then shorter names first
        candidates.sort(key=lambda x: (x[0], x[1]))
        return candidates[0][2]
    
    # Entity contains table name
    for t in tables:
        if t.lower() in entity_l and len(t) > 2: return t
    
    return tables[0] if tables else None


def find_group_col(group_by, table, schema):
    """Find best column to group by in a table."""
    if not schema or not table or table not in schema: return None
    cols = schema[table]
    group_l = group_by.lower()
    for c in cols:
        if c.lower() == group_l: return c
    for c in cols:
        if group_l in c.lower(): return c
    for c in cols:
        if c.lower() in group_l and len(c) > 2: return c
    return None


# Synonym map — question words → column name hints
# Covers common domain-specific columns while staying generic
COLUMN_SYNONYMS = {
    # Business/Sales domain
    "salary":     ["monthlyincome","salary","income","wage","pay","compensation"],
    "income":     ["monthlyincome","income","salary","wage","pay","total"],
    "wage":       ["monthlyincome","salary","income","wage"],
    "pay":        ["monthlyincome","salary","income","pay"],
    "price":      ["unit_price","unitprice","price","cost","amount","rate"],
    "revenue":    ["unit_price","unitprice","price","amount","income","total"],
    "sales":      ["unit_price","unitprice","price","amount","income","total"],
    "cost":       ["cost","unitcost","price"],
    
    # Universal numeric concepts
    "total":      ["total","sum","amount","quantity","value"],
    "amount":     ["amount","total","sum","value","price"],
    "quantity":   ["quantity","qty","units","count","number"],
    "value":      ["value","amount","total","sum"],
    
    # HR domain
    "age":        ["age"],
    "distance":   ["distancefromhome","distance"],
    "experience": ["totalworkingyears","yearsatcompany","experience","years"],
    "rating":     ["performancerating","rating","score"],
    "satisfaction":["jobsatisfaction","satisfaction","score"],
    "rate":       ["hourlyrate","dailyrate","rate"],
    "hike":       ["percentsalaryhike","hike","percent"],
}


def find_numeric_col(table, schema, question=""):
    """Find best numeric column for aggregation.
    Question context wins — if a column name is mentioned in the question, use it.
    Gracefully handles unknown domains by falling back to pattern matching.
    """
    if not schema or table not in schema: return None
    cols = schema[table]
    catalog = get_catalog()
    table_meta = catalog.get_table(table) if catalog else None
    numeric_names = {
        c.name.lower()
        for c in (table_meta.columns if table_meta else [])
        if c.is_numeric and not c.is_primary_key
    }

    # Expanded numeric keywords covering more domains
    ALL_NUMERIC = [
        # Business/Sales
        "income","salary","revenue","amount","price","rate","total","cost","value",
        "quantity","units","stock","order","invoice",
        # Music/Entertainment  
        "duration","length","milliseconds","bytes","bitrate",
        # General numeric
        "score","percent","hours","distance","years","level","age","count","number",
        "rating","satisfaction","involvement","hike","balance","id","fee",
        # Additional patterns
        "total_", "sum_", "avg_", "_amount", "_price", "_cost", "_rate"
    ]

    import re as _re
    q_lower = question.lower()

    # Step 1 — synonym map (highest priority)
    # e.g. "salary" → try monthlyincome, income, salary columns in order
    q_words_all = [w for w in q_lower.split() if len(w) > 3]
    for word in q_words_all:
        if word in COLUMN_SYNONYMS:
            for hint in COLUMN_SYNONYMS[word]:
                for col in cols:
                    if hint.lower() in col.lower() and (not numeric_names or col.lower() in numeric_names):
                        return col

    # Step 2 — exact column name as whole word in question
    for col in cols:
        col_l = col.lower()
        if _re.search(r'\b' + _re.escape(col_l) + r'\b', q_lower):
            if (not numeric_names or col_l in numeric_names) and any(h in col_l for h in ALL_NUMERIC):
                return col

    # Step 3 — question words match column fragments
    for word in q_words_all:
        for col in cols:
            col_l = col.lower()
            if word in col_l and (not numeric_names or col_l in numeric_names):
                return col

    # Step 4 — preferred fallback list (domain-agnostic)
    PREFERRED = ["total","amount","price","value","quantity","rate","cost","income","duration"]
    for pref in PREFERRED:
        for col in cols:
            if pref in col.lower() and (not numeric_names or col.lower() in numeric_names): return col

    # Step 5 — any numeric-looking column
    for hint in ALL_NUMERIC:
        for col in cols:
            if hint in col.lower() and (not numeric_names or col.lower() in numeric_names): return col

    # Step 6 — last resort: pick first non-id column
    for col in cols:
        col_l = col.lower()
        if "id" not in col_l and "_id" not in col_l:
            return col
    
    return cols[0] if cols else None


def link_schema(parsed_query, schema_map=None):
    result = {}

    entity      = parsed_query.get("entity")
    group_by    = parsed_query.get("group_by")
    aggregation = parsed_query.get("aggregation")
    filters     = parsed_query.get("filters") or []
    time_dim    = parsed_query.get("time_dimension")
    question    = parsed_query.get("original_text", "")

    schema = get_discovered_schema()
    tables = list(schema.keys()) if schema else []

    if not tables:
        raise Exception("No schema discovered. Please connect to a database first.")

    # ── Check for Groq resolved metadata ──
    if parsed_query.get("resolved_base_table"):
        base_t = parsed_query["resolved_base_table"]
        # Ensure the table actually exists in discovery
        if base_t in schema:
            result["base_table"] = base_t
            result["metric_table"] = parsed_query.get("resolved_metric_table") or base_t
            result["metric_column"] = parsed_query.get("resolved_metric_column")
            result["group_table"] = parsed_query.get("resolved_group_table")
            
            resolved_group_col = parsed_query.get("resolved_group_column")
            if resolved_group_col:
                if time_dim in ("year", "month", "quarter"):
                    extract = time_dim.upper()
                    result["group_column"] = f"EXTRACT({extract} FROM {resolved_group_col}) AS {time_dim}"
                    result["group_table"] = parsed_query.get("resolved_group_table")
                    result["date_table"] = parsed_query.get("resolved_group_table") or base_t
                    result["date_col"] = resolved_group_col
                else:
                    result["group_column"] = resolved_group_col
                    # If group_table is missing, try to locate it in the schema
                    if not result.get("group_table"):
                        for t, cols in schema.items():
                            if resolved_group_col in cols:
                                result["group_table"] = t
                                break
            else:
                result["group_column"] = None

            result["joins"] = []
            # Transfer filters directly from parsed
            result["filters"] = parsed_query.get("resolved_filters") or filters
            return result

    # ── Base table (fallback) ──
    # For revenue/sales queries, prefer tables with numeric transaction columns
    REVENUE_ENTITIES = {"sales", "revenue", "income"}
    if entity and entity.lower() in REVENUE_ENTITIES:
        # Find the most transactional table (has price + quantity or amount)
        for t in tables:
            cols = [c.lower() for c in schema.get(t, [])]
            has_price = any("price" in c or "amount" in c or "rate" in c or "cost" in c for c in cols)
            has_qty   = any("quantity" in c or "qty" in c or "units" in c or "count" in c for c in cols)
            if has_price and has_qty:
                base_table = t
                break
        else:
            base_table = find_table(entity, schema) or tables[0]
    else:
        base_table = find_table(entity, schema) if entity else tables[0]
    result["base_table"] = base_table

    # ── Effective group (fallback) ──
    effective_group = time_dim or group_by

    # ── Group column resolution (fallback) ──
    if effective_group:
        if effective_group in ("year", "month", "quarter"):
            # Find a date column — search base table and all related tables
            date_col   = None
            date_table = base_table
            for t in [base_table] + [x for x in tables if x != base_table]:
                for col in schema.get(t, []):
                    if any(h in col.lower() for h in ["date", "time", "created", "ordered"]):
                        date_col   = col
                        date_table = t
                        break
                if date_col: break
            if date_col:
                extract = effective_group.upper()
                # Qualify with table alias if not in base
                tref = date_table if date_table != base_table else base_table
                result["group_column"] = f"EXTRACT({extract} FROM {date_col}) AS {effective_group}"
                result["group_table"]  = date_table if date_table != base_table else None
                result["date_table"]   = date_table
                result["date_col"]     = date_col
        else:
            # Try to find the group column in base table first
            group_col = find_group_col(effective_group, base_table, schema)
            if group_col:
                result["group_column"] = group_col
                result["group_table"]  = base_table
            else:
                # Find in a related table
                group_table = find_table(effective_group, schema)
                if group_table and group_table != base_table:
                    cols = schema.get(group_table, [])
                    # Prefer name/label columns
                    NAME_HINTS = ["name","title","label","description","type","role","status","department","category"]
                    best = None
                    for hint in NAME_HINTS:
                        for col in cols:
                            if hint in col.lower() and "id" not in col.lower():
                                best = col
                                break
                        if best: break
                    if not best:
                        for col in cols:
                            if "id" not in col.lower() and "number" not in col.lower():
                                list_c = col
                                break
                    if best:
                        result["group_column"] = best
                        result["group_table"]  = group_table

    # ── Metric column for aggregation (fallback) ──
    if aggregation in ("sum", "avg", "max", "min"):
        base_cols = [c.lower() for c in schema.get(base_table, [])]
        # Check if this is a transaction table with price * quantity pattern
        # Strict check — exclude "quantity_per_unit" style text columns
        has_price = any(("price" in c or "amount" in c) and "per" not in c and "list" not in c for c in base_cols)
        has_qty   = any((c == "quantity" or c == "qty" or
                        ("quantity" in c and "per" not in c and "unit" not in c)) for c in base_cols)
        if has_price and has_qty and aggregation == "sum":
            # Use price * quantity expression
            # Be strict: price col must be unit_price/price, qty col must be quantity/qty (not quantity_per_unit)
            price_col = next((c for c in schema.get(base_table,[])
                if ("unit_price" == c.lower() or c.lower() == "price" or
                    ("price" in c.lower() and "per" not in c.lower() and "list" not in c.lower())
                    or c.lower() in ("amount","cost"))), None)
            qty_col   = next((c for c in schema.get(base_table,[])
                if (c.lower() == "quantity" or c.lower() == "qty"
                    or ("qty" in c.lower() and "per" not in c.lower())
                    or ("quantity" in c.lower() and "per" not in c.lower() and "unit" not in c.lower())
                    )), None)
            if price_col and qty_col:
                result["metric_column"] = f"{price_col} * {qty_col}"
                result["metric_table"]  = base_table
        if not result.get("metric_column"):
            metric = find_numeric_col(base_table, schema, question)
            if metric:
                result["metric_column"] = metric
                result["metric_table"]  = base_table

    # ── Count aggregation (fallback) ──
    if aggregation == "count":
        result["metric_column"] = None   # COUNT(*) needs no specific column

    # ── Store date table info for year filters (fallback) ──
    for f in filters:
        if f.get("type") == "year" and not result.get("date_table"):
            for t in tables:
                for col in schema.get(t, []):
                    if any(h in col.lower() for h in ["date","time","ordered","created"]):
                        result["date_table"] = t
                        result["date_col"]   = col
                        break
                if result.get("date_table"): break

    result["joins"]   = []
    result["filters"] = filters
    return result
