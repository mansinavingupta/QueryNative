# sql_engine/query_builder.py
# Dynamic query builder — works with ANY database
# No hardcoded table/column names

import re

# ─────────────────────────────────────────────
# SCHEMA HELPERS
# ─────────────────────────────────────────────

# PostgreSQL reserved keywords that cannot be used as unquoted identifiers
RESERVED_KEYWORDS = {
    "select", "from", "where", "join", "left", "right", "inner", "outer",
    "on", "and", "or", "not", "in", "exists", "between", "like", "is",
    "null", "true", "false", "group", "by", "order", "having", "limit",
    "offset", "union", "intersect", "except", "case", "when", "then", "else",
    "end", "as", "with", "all", "any", "some", "distinct", "insert", "update",
    "delete", "create", "alter", "drop", "table", "database", "schema", "view",
    "index", "constraint", "primary", "key", "foreign", "unique", "check",
    "default", "values", "set", "action", "cascade", "restrict", "no",
    "column", "type", "cast", "extract", "interval", "date", "time", "timestamp"
}

def get_schema():
    try:
        from db.schema_discovery import get_schema_summary
        return get_schema_summary()
    except:
        return {}


def quote_ident(identifier):
    return '"' + str(identifier).replace('"', '""') + '"'


def quote_literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def get_alias(table):
    if not table: return "t"
    parts = table.split("_")
    if len(parts) >= 2:
        alias = "".join(p[0] for p in parts)
    else:
        alias = table[:2].lower()
    
    alias = re.sub(r"[^a-zA-Z0-9_]", "_", alias)
    if not alias or alias[0].isdigit() or alias.lower() in RESERVED_KEYWORDS:
        return f"t_{alias}"
    return alias


def find_col(table, *hints):
    """Find first column in table matching any hint (substring match)."""
    schema = get_schema()
    if not schema or table not in schema: return None
    cols = schema[table]
    for hint in hints:
        for col in cols:
            if hint.lower() == col.lower(): return col  # exact first
    for hint in hints:
        for col in cols:
            if hint.lower() in col.lower(): return col  # then partial
    return None


def find_numeric_col(table, *hints):
    """Find a numeric column — prefers hints, then common numeric patterns."""
    schema = get_schema()
    if not schema or table not in schema: return None
    cols = schema[table]
    # Try hints first
    result = find_col(table, *hints) if hints else None
    if result: return result
    # Common numeric column names
    NUMERIC = ["income","salary","revenue","amount","price","rate","total",
               "cost","value","score","percent","age","hours","distance",
               "years","level","count","number","quantity","units"]
    for hint in NUMERIC:
        for col in cols:
            if hint in col.lower(): return col
    return None


def find_text_col(table, *hints):
    """Find a text/label column — prefers hints, then common name patterns."""
    schema = get_schema()
    if not schema or table not in schema: return None
    cols = schema[table]
    result = find_col(table, *hints) if hints else None
    if result: return result
    TEXT = ["name","title","label","type","category","department","role",
            "status","description","group","class"]
    for hint in TEXT:
        for col in cols:
            if hint in col.lower() and "id" not in col.lower(): return col
    # Last resort: first non-id column
    for col in cols:
        if "id" not in col.lower() and "number" not in col.lower():
            return col
    return cols[0] if cols else None


def find_date_col(table):
    return find_col(table, "date", "time", "created", "updated", "ordered")


def find_join_col(table1, table2):
    """Find common column between two tables for JOIN."""
    schema = get_schema()
    if not schema: return None
    cols1 = {c.lower(): c for c in schema.get(table1, [])}
    cols2 = {c.lower(): c for c in schema.get(table2, [])}
    common = set(cols1.keys()) & set(cols2.keys())
    for c in common:
        if "id" in c: return cols1[c]
    return cols1[next(iter(common))] if common else None


def find_related_table(base_table, group_by):
    """
    Find a related table that contains the group_by concept.
    e.g. base=order_details, group_by=category → finds categories table
    """
    schema = get_schema()
    if not schema: return None
    for table in schema:
        if table == base_table: continue
        if group_by.lower() in table.lower(): return table
        if table.lower() in group_by.lower(): return table
    return None


def build_join_chain(base_table, target_table):
    """
    Build JOIN path from base_table to target_table using FK relationships.
    Falls back to shared column name matching if FKs not available.
    """
    if base_table == target_table:
        return [], []

    schema = get_schema()
    if not schema: return [], []

    # Try FK-based path first
    try:
        from db.schema_discovery import get_foreign_keys
        fks = get_foreign_keys()
        if fks:
            fk_map = {}
            for table, col, ref_table, ref_col in fks:
                fk_map.setdefault(table, []).append((col, ref_table, ref_col))
                fk_map.setdefault(ref_table, []).append((ref_col, table, col))

            from collections import deque
            queue   = deque([(base_table, [])])
            visited = {base_table}
            while queue:
                current, path = queue.popleft()
                if current == target_table:
                    return path, [p[1] for p in path]
                for col, next_table, next_col in fk_map.get(current, []):
                    if next_table not in visited:
                        visited.add(next_table)
                        queue.append((next_table, path + [(current, next_table, col, next_col)]))
    except Exception:
        pass

    # Fallback: find shared column name between tables
    path = find_join_path_by_columns(base_table, target_table, schema)
    return path, [p[1] for p in path]


def find_join_path_by_columns(base_table, target_table, schema):
    """BFS join path using shared column names as join conditions."""
    from collections import deque
    tables  = list(schema.keys())
    queue   = deque([(base_table, [])])
    visited = {base_table}

    while queue:
        current, path = queue.popleft()
        if current == target_table:
            return path
        current_cols = {c.lower() for c in schema.get(current, [])}
        for next_table in tables:
            if next_table in visited: continue
            next_cols = {c.lower() for c in schema.get(next_table, [])}
            common = current_cols & next_cols
            # Find a shared column (prefer id columns)
            join_col = None
            for c in common:
                if "id" in c:
                    join_col = c
                    break
            if not join_col and common:
                join_col = next(iter(common))
            if join_col:
                # Get original case column names
                orig_cur  = next(c for c in schema.get(current,[]) if c.lower()==join_col)
                orig_next = next(c for c in schema.get(next_table,[]) if c.lower()==join_col)
                visited.add(next_table)
                queue.append((next_table, path + [(current, next_table, orig_cur, orig_next)]))

    return []


# ─────────────────────────────────────────────
# MAIN BUILD FUNCTION
# ─────────────────────────────────────────────

def build_query(parsed, linked):
    base_table   = linked.get("base_table")
    group_column = linked.get("group_column")
    group_table  = linked.get("group_table")
    metric_col   = linked.get("metric_column")
    aggregation  = parsed.get("aggregation")
    filters      = linked.get("filters") or []
    limit        = parsed.get("limit")
    order_by     = parsed.get("order_by") or "DESC"
    if str(order_by).upper() not in ("ASC", "DESC"):
        order_by = "DESC"
    entity       = parsed.get("entity", "")
    question     = parsed.get("original_text", "").lower()

    if not base_table:
        raise Exception("Could not determine base table.")

    schema   = get_schema()
    alias    = get_alias(base_table)
    joins    = []   # list of JOIN strings
    joined   = {base_table}

    # ── Helper: add join path from base to a target table ──
    def ensure_joined(target_table):
        if target_table in joined: return get_alias(target_table)
        path, _ = build_join_chain(base_table, target_table)
        for from_t, to_t, from_col, to_col in path:
            if to_t not in joined:
                fa = get_alias(from_t)
                ta = get_alias(to_t)
                joins.append(
                    f"JOIN {quote_ident(to_t)} {ta} "
                    f"ON {fa}.{quote_ident(from_col)} = {ta}.{quote_ident(to_col)}"
                )
                joined.add(to_t)
        return get_alias(target_table)

    # ── Resolve metric expression ──
    # Support multi-column expressions like unit_price * quantity
    def resolve_metric():
        if not metric_col: return None
        # Already has table alias
        if "." in metric_col: return metric_col
        # Expression like "unit_price * quantity" — prefix each token with alias
        if any(op in metric_col for op in ["*", "+", "-", "/"]):
            tokens = re.split(r"(\s*[\*\+\-\/]\s*)", metric_col)
            qualified = []
            for tok in tokens:
                tok = tok.strip()
                if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", tok):
                    qualified.append(f"{alias}.{quote_ident(tok)}")
                else:
                    qualified.append(tok)
            return " ".join(qualified)
        return f"{alias}.{quote_ident(metric_col)}"

    # ── Resolve group expression ──
    def resolve_group_expr():
        if not group_column: return None, None
        # Time extract — qualify the date column with proper table alias
        if "EXTRACT" in str(group_column):
            date_table = linked.get("date_table")
            date_col   = linked.get("date_col")
            if date_table and date_col:
                dt_alias = ensure_joined(date_table) if date_table != base_table else alias
                extract_part = group_column.split(" AS ")[0]
                # Replace bare date_col with aliased version
                extract_part = extract_part.replace(date_col, f"{dt_alias}.{quote_ident(date_col)}")
                label = f"{extract_part} AS {group_column.split(' AS ')[-1].strip()}"
                expr  = extract_part
                return label, expr
            expr  = group_column.split(" AS ")[0].strip()
            label = group_column
            return label, expr
        # Group in different table
        if group_table and group_table != base_table:
            g_alias = ensure_joined(group_table)
            col_expr = f"{g_alias}.{quote_ident(group_column)}"
            return col_expr, col_expr
        # Group in base table
        col_expr = f"{alias}.{quote_ident(group_column)}"
        return col_expr, col_expr

    # ── SELECT clause ──
    select_parts = []

    group_select, group_expr = resolve_group_expr()
    if group_select:
        select_parts.append(group_select)

    metric_expr = resolve_metric()

    if aggregation and metric_expr:
        if aggregation == "sum":
            select_parts.append(f"SUM({metric_expr}) AS total")
        elif aggregation == "avg":
            select_parts.append(f"ROUND(AVG({metric_expr})::numeric, 2) AS average")
        elif aggregation == "max":
            select_parts.append(f"MAX({metric_expr}) AS maximum")
        elif aggregation == "min":
            select_parts.append(f"MIN({metric_expr}) AS minimum")
        elif aggregation == "count":
            select_parts.append(f"COUNT(*) AS total_count")
    elif aggregation == "count":
        select_parts.append("COUNT(*) AS total_count")

    # Nothing selected yet → default columns
    if not select_parts:
        # Pick a few meaningful columns instead of *
        name_col = find_text_col(base_table)
        num_col  = find_numeric_col(base_table)
        if name_col: select_parts.append(f"{alias}.{quote_ident(name_col)}")
        if num_col and num_col != name_col: select_parts.append(f"{alias}.{quote_ident(num_col)}")
        if not select_parts: select_parts.append(f"{alias}.*")

    # ── FROM ──
    from_clause = f"FROM {quote_ident(base_table)} {alias}"

    # ── WHERE ──
    conditions = []
    # ── WHERE ──
    conditions = []
    for f in filters:
        if not isinstance(f, dict):
            continue
        if "column" in f and "table" in f:
            f_table = f["table"]
            f_column = f["column"]
            f_operator = f.get("operator") or "="
            value = f.get("value")
            
            # Ensure the table is joined
            t_alias = ensure_joined(f_table) if f_table != base_table else alias
            col_ref = f"{t_alias}.{quote_ident(f_column)}"
            op_upper = str(f_operator).upper()
            
            if op_upper == "BETWEEN" and isinstance(value, (list, tuple)) and len(value) == 2:
                conditions.append(f"{col_ref} BETWEEN {quote_literal(value[0])} AND {quote_literal(value[1])}")
            elif op_upper in ("LIKE", "ILIKE"):
                conditions.append(f"{col_ref} {op_upper} {quote_literal(value)}")
            elif op_upper == "IN" and isinstance(value, (list, tuple)):
                in_vals = ", ".join(quote_literal(v) for v in value)
                conditions.append(f"{col_ref} IN ({in_vals})")
            else:
                conditions.append(f"{col_ref} {f_operator} {quote_literal(value)}")
        else:
            ft    = f.get("type")
            value = f.get("value")

            if ft == "year":
                date_table = linked.get("date_table")
                date_col   = linked.get("date_col") or find_date_col(base_table)
                if date_col:
                    if date_table and date_table != base_table:
                        dt_alias = ensure_joined(date_table)
                        conditions.append(f"EXTRACT(YEAR FROM {dt_alias}.{quote_ident(date_col)}) = {int(value)}")
                    else:
                        conditions.append(f"EXTRACT(YEAR FROM {alias}.{quote_ident(date_col)}) = {int(value)}")

            elif ft == "country":
                col = find_col(base_table, "country", "region", "location")
                if col:
                    conditions.append(f"{alias}.{quote_ident(col)} = {quote_literal(value)}")
                else:
                    # Try customers/related table
                    for t in schema:
                        c = find_col(t, "country")
                        if c:
                            ta = ensure_joined(t)
                            conditions.append(f"{ta}.{quote_ident(c)} = {quote_literal(value)}")
                            break

            elif ft == "category":
                col = find_col(base_table, "category", "department", "type", "group", "class")
                if col:
                    conditions.append(f"{alias}.{quote_ident(col)} ILIKE {quote_literal('%' + str(value) + '%')}")
                else:
                    for t in schema:
                        c = find_col(t, "category", "department")
                        if c:
                            ta = ensure_joined(t)
                            conditions.append(f"{ta}.{quote_ident(c)} ILIKE {quote_literal('%' + str(value) + '%')}")
                            break

            elif ft == "price_gt":
                col = find_numeric_col(base_table, "price", "rate", "income", "salary", "amount")
                if col: conditions.append(f"{alias}.{quote_ident(col)} > {quote_literal(value)}")

            elif ft == "price_lt":
                col = find_numeric_col(base_table, "price", "rate", "income", "salary", "amount")
                if col: conditions.append(f"{alias}.{quote_ident(col)} < {quote_literal(value)}")

            elif ft == "price_between":
                col = find_numeric_col(base_table, "price", "rate", "income", "salary")
                if col: conditions.append(f"{alias}.{quote_ident(col)} BETWEEN {quote_literal(value[0])} AND {quote_literal(value[1])}")

            elif ft == "discontinued":
                col = find_col(base_table, "discontinued", "attrition", "active", "status", "left")
                if col:
                    if value:
                        conditions.append(f"({alias}.{quote_ident(col)} = 1 OR {alias}.{quote_ident(col)}::text ILIKE 'yes')")
                    else:
                        conditions.append(f"({alias}.{quote_ident(col)} = 0 OR {alias}.{quote_ident(col)}::text ILIKE 'no')")

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # ── GROUP BY ──
    group_by_clause = ""
    if group_expr and aggregation:
        group_by_clause = f"GROUP BY {group_expr}"

    # ── ORDER BY ──
    order_by_clause = ""
    if aggregation and group_expr:
        agg_label = {
            "sum":   "total",
            "count": "total_count",
            "avg":   "average",
            "max":   "maximum",
            "min":   "minimum",
        }.get(aggregation, "total")
        # min queries should order ASC to get the lowest first
        if aggregation == "min" and order_by == "DESC":
            order_by = "ASC"
        order_by_clause = f"ORDER BY {agg_label} {order_by}"
    elif aggregation and not group_expr:
        pass  # single row aggregate — no ORDER BY

    # ── Auto LIMIT 1 for min/max superlative queries ──
    # e.g. "lowest paid job role", "most expensive product"
    question_l = question.lower()
    SUPERLATIVES = ["lowest","least","cheapest","minimum","worst","bottom",
                    "highest","most expensive","maximum","best","top performing"]
    if not limit and aggregation in ("min","max") and any(s in question_l for s in SUPERLATIVES):
        limit = 1
    if not limit and aggregation == "min" and group_column:
        limit = 1   # min with group always means "the one with lowest value"

    # ── LIMIT ──
    limit_clause = f"LIMIT {limit}" if limit else ""

    # ── Assemble ──
    parts = [f"SELECT {', '.join(select_parts)}", from_clause]
    for j in joins:
        parts.append(j)
    if where_clause:     parts.append(where_clause)
    if group_by_clause:  parts.append(group_by_clause)
    if order_by_clause:  parts.append(order_by_clause)
    if limit_clause:     parts.append(limit_clause)

    return "\n".join(parts)
