import re

AGGREGATE_KEYWORDS = {
    "sum":   ["total", "sum", "revenue", "sales"],
    "count": ["count", "number of", "how many", "total number"],
    "avg":   ["average", "mean", "avg"],
    "max":   ["highest", "maximum", "most expensive", "priciest", "expensive", "most"],
    "min":   ["lowest", "minimum", "cheapest", "cheap", "least expensive"],
}

# Domain-specific entity keywords (kept for backward compatibility with known DBs)
ENTITY_KEYWORDS = {
    "sales":    ["sale", "sales", "revenue", "income"],
    "product":  ["product", "products", "item", "items", "track", "tracks", "album", "albums"],
    "category": ["category", "categories", "genre", "genres", "type", "types"],
    "customer": ["customer", "customers", "client", "clients"],
    "employee": ["employee", "employees", "staff", "artist", "artists"],
    "order":    ["order", "orders", "purchase", "purchases", "invoice", "invoices"],
    "supplier": ["supplier", "suppliers"],
    "shipper":  ["shipper", "shippers"],
}

KNOWN_COUNTRIES = [
    "usa", "us", "united states", "germany", "france", "uk", "united kingdom",
    "brazil", "canada", "mexico", "italy", "spain", "sweden", "norway",
    "denmark", "finland", "austria", "switzerland", "portugal", "belgium",
    "netherlands", "poland", "argentina", "venezuela", "chile", "peru",
]


def detect_aggregation(text):
    t = text.lower()
    # "total customers/employees/orders/products/categories by X" = count, not sum
    # Only treat "total/sum" as SUM when paired with revenue entities
    NON_REVENUE = ["customer","customers","employee","employees","product","products",
                   "category","categories","order","orders","supplier","suppliers",
                   "track","tracks","artist","artists","album","albums"]
    if re.search(r"\btotal\b", t) and any(w in t for w in NON_REVENUE):
        if not any(w in t for w in ["sales","revenue","income","price","value","amount"]):
            return "count"
    for agg, words in AGGREGATE_KEYWORDS.items():
        for w in sorted(words, key=len, reverse=True):
            if w in t:
                return agg
    return None


def detect_entity(text, schema=None):
    t = text.lower()
    if schema:
        # Match table names from schema dynamically!
        best_table = None
        for table in schema.keys():
            table_l = table.lower()
            words_to_try = [table_l]
            if table_l.endswith("s"):
                words_to_try.append(table_l[:-1])
            else:
                words_to_try.append(table_l + "s")
            
            for w in words_to_try:
                if re.search(r'\b' + re.escape(w) + r'\b', t):
                    if not best_table or len(table_l) < len(best_table):
                        best_table = table
        if best_table:
            return best_table

    # Score each entity by how many of its keywords appear
    # More specific entities (product, customer) beat generic ones (sales)
    PRIORITY = ["product","category","customer","employee","order","supplier","shipper","sales"]
    scores = {}
    for entity, words in ENTITY_KEYWORDS.items():
        count = sum(1 for w in words if re.search(r'\b' + re.escape(w) + r'\b', t))
        if count > 0:
            scores[entity] = count
    if not scores:
        return None
    # Return highest-priority entity that scored > 0
    for entity in PRIORITY:
        if entity in scores:
            return entity
    return None


def detect_limit(text):
    t = text.lower()
    m = re.search(r'\btop\s+(\d+)\b', t)
    if m:
        return int(m.group(1))
    if re.search(r'\b(most expensive|cheapest|highest|lowest|best|worst)\b', t):
        return 1
    return None


def detect_group_by(text, schema=None):
    t = text.lower()
    if schema:
        # Check if there's "by <column_name>" in text
        for table, cols in schema.items():
            for col in cols:
                col_l = col.lower()
                if re.search(r'\bby\s+' + re.escape(col_l) + r'\b', t):
                    return col
                col_words = col_l.split("_")
                for w in col_words:
                    if len(w) > 2 and re.search(r'\bby\s+' + re.escape(w) + r'\b', t):
                        return col

    # First pass: explicit "by <keyword>"
    for keyword, group in [
        ("category", "category"), ("country", "country"),
        ("year", "year"), ("month", "month"), ("quarter", "quarter"),
        ("employee", "employee"), ("customer", "customer"),
        ("product", "product"), ("supplier", "supplier"), ("shipper", "shipper"),
    ]:
        if re.search(r'\bby\s+' + keyword + r'\b', t):
            return group
    # Second pass: implicit grouping keywords present in text (not after "by")
    for keyword, group in [
        ("category", "category"), ("employee", "employee"),
        ("customer", "customer"), ("product", "product"),
        ("supplier", "supplier"), ("shipper", "shipper"),
    ]:
        if re.search(r'\b' + keyword + r'\b', t):
            return group
    return None


def detect_time_dimension(text):
    t = text.lower()
    if re.search(r'\bby\s+month\b|\bmonthly\b|\bper\s+month\b', t):
        return "month"
    if re.search(r'\bby\s+year\b|\byearly\b|\bannual\b|\bper\s+year\b', t):
        return "year"
    if re.search(r'\bby\s+quarter\b|\bquarterly\b', t):
        return "quarter"
    return None


def detect_filters(text, schema=None):
    t = text.lower()
    filters = []

    # Year filter
    m = re.search(r'\b(?:in|for|year|during)\s+(19\d{2}|20\d{2})\b', t)
    if m:
        filters.append({"type": "year", "value": int(m.group(1))})
    else:
        m = re.search(r'\b(19\d{2}|20\d{2})\b', t)
        if m:
            filters.append({"type": "year", "value": int(m.group(1))})

    # Dynamic column-based numeric filters if schema is provided
    if schema:
        for table, cols in schema.items():
            for col in cols:
                col_l = col.lower()
                if any(h in col_l for h in ["price", "rate", "cost", "salary", "amount"]):
                    m_gt = re.search(r'\b' + re.escape(col_l) + r'\s*(?:>|greater than|more than|above|over)\s*\$?(\d+(?:\.\d+)?)\b', t)
                    if m_gt:
                        filters.append({"type": "price_gt", "value": float(m_gt.group(1))})
                    m_lt = re.search(r'\b' + re.escape(col_l) + r'\s*(?:<|less than|under|below|cheaper than)\s*\$?(\d+(?:\.\d+)?)\b', t)
                    if m_lt:
                        filters.append({"type": "price_lt", "value": float(m_lt.group(1))})
                    m_btw = re.search(r'\b' + re.escape(col_l) + r'\s*between\s+(\d+(?:\.\d+)?)\s+and\s+(\d+(?:\.\d+)?)\b', t)
                    if m_btw:
                        filters.append({"type": "price_between", "value": [float(m_btw.group(1)), float(m_btw.group(2))]})

    # Country filter
    has_country = any(f.get("type") == "country" for f in filters)
    if not has_country:
        for country in KNOWN_COUNTRIES:
            if re.search(r'\b(?:from|in|of)\s+' + re.escape(country) + r'\b', t):
                display = {"usa":"USA","us":"USA","united states":"USA","uk":"UK","united kingdom":"UK"}.get(country, country.title())
                filters.append({"type": "country", "value": display})
                break

    # Price between (fallback)
    has_price = any(f.get("type") in ("price_between", "price_gt", "price_lt") for f in filters)
    if not has_price:
        m = re.search(r'\bprice\s+between\s+(\d+(?:\.\d+)?)\s+and\s+(\d+(?:\.\d+)?)\b', t)
        if m:
            filters.append({"type": "price_between", "value": [float(m.group(1)), float(m.group(2))]})
        else:
            m = re.search(r'\b(?:greater than|more than|above|over|price\s*>)\s*\$?(\d+(?:\.\d+)?)\b', t)
            if m:
                filters.append({"type": "price_gt", "value": float(m.group(1))})
            m = re.search(r'\b(?:less than|under|below|cheaper than|price\s*<)\s*\$?(\d+(?:\.\d+)?)\b', t)
            if m:
                filters.append({"type": "price_lt", "value": float(m.group(1))})

    # Category filter
    has_category = any(f.get("type") == "category" for f in filters)
    if not has_category:
        m = re.search(r'\b(?:in\s+)?category\s+([A-Za-z][A-Za-z\s]+?)(?:\s|$)', text, re.IGNORECASE)
        if m:
            cat = m.group(1).strip()
            if cat.lower() not in ("name", "by", "the", "a", "an", "of"):
                filters.append({"type": "category", "value": cat})

    # Discontinued filter
    has_discontinued = any(f.get("type") == "discontinued" for f in filters)
    if not has_discontinued:
        if re.search(r'\bdiscontinued\b', t):
            filters.append({"type": "discontinued", "value": True})
        elif re.search(r'\bactive\b|\bnot discontinued\b', t):
            filters.append({"type": "discontinued", "value": False})

    return filters if filters else None


def parse_query(text, schema=None):
    parsed = {}
    parsed["original_text"]  = text
    parsed["aggregation"]    = detect_aggregation(text)
    parsed["entity"]         = detect_entity(text, schema)
    parsed["limit"]          = detect_limit(text)
    parsed["group_by"]       = detect_group_by(text, schema)
    parsed["time_dimension"] = detect_time_dimension(text)
    parsed["filters"]        = detect_filters(text, schema)
    parsed["order_by"]       = "ASC" if re.search(r'\b(ascending|lowest first|asc)\b', text.lower()) else "DESC"
    return parsed