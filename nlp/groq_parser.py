"""
Groq-backed intent extraction with a deterministic local fallback.

The rest of the app consumes the same intent dictionary shape regardless of
whether Groq is available, returns malformed JSON, or raises an API error.
"""

import json
import os
import re

from nlp.text_parser import parse_query as fallback_parse_query

try:
    from groq import Groq
except Exception:
    Groq = None

_api_key = os.environ.get("GROQ_API_KEY", "")
_groq_client = Groq(api_key=_api_key) if Groq and _api_key else None
GROQ_AVAILABLE = _groq_client is not None

GROQ_MODEL = "llama-3.3-70b-versatile"

SYSTEM_PROMPT = """You are a database query intent extractor and schema linker.
Given a user question and a database schema, extract the query intent and link it to the exact tables and columns in the schema.

Return ONLY a valid JSON object with these exact keys:
{
  "entity": string or null,
  "aggregation": "sum" or "count" or "avg" or "max" or "min" or null,
  "group_by": string or null,
  "limit": number or null,
  "order_by": "ASC" or "DESC" or null,
  "time_dimension": "year" or "month" or "quarter" or "day" or null,
  "filters": array or null,

  "resolved_base_table": string or null,
  "resolved_group_table": string or null,
  "resolved_group_column": string or null,
  "resolved_metric_table": string or null,
  "resolved_metric_column": string or null,
  "resolved_filters": array or null
}

Allowed aggregation values: "sum", "count", "avg", "max", "min", null.
Allowed time_dimension values: "year", "month", "quarter", "day", null.

Rules for Schema Linking:
1. "resolved_base_table": The primary table to query.
   - For aggregate queries (like sum of sales, average price, max units), if the metric or transaction columns (e.g., unit_price, quantity) reside in a transactional/fact table (e.g., "order_details"), make that transactional table the "resolved_base_table".
2. "resolved_metric_column":
   - The exact column name to aggregate (e.g., "salary", "age").
   - If the query asks for "sales" or "revenue" and the schema contains unit price and quantity columns but no direct sales column, represent this as a SQL expression of the columns, such as "unit_price * quantity" (using the exact column names).
3. "resolved_metric_table": The table containing the "resolved_metric_column".
4. "resolved_group_table": The table containing the column we group by.
5. "resolved_group_column": The exact column name from the schema to group by. If it is a time dimension, specify the date/timestamp column.
6. "resolved_filters": An array of filters. Each filter object must have:
   {
     "table": string (the table name),
     "column": string (the column name),
     "operator": "=" or ">" or "<" or ">=" or "<=" or "LIKE" or "ILIKE" or "BETWEEN" or "IN",
     "value": any (use a list [min, max] for BETWEEN)
   }
   Map filter columns to the actual schema columns and tables.

Ensure all resolved table and column names exist EXACTLY as written in the provided schema. Do not invent any names.
Return ONLY the JSON. No explanation, no markdown, no backticks."""

REPAIR_PROMPT = """You repair PostgreSQL SELECT queries.
Given a database schema, user question, failed SQL, and database error, return ONLY one corrected PostgreSQL SELECT statement.
Rules:
- Return one read-only SELECT statement.
- Use only tables and columns present in the schema.
- Preserve the user's intent.
- Add LIMIT 200 unless the query is a single aggregate or already has a tighter limit.
- No markdown, no explanation."""


def _extract_json_object(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _fallback(question: str, schema_summary=None) -> dict:
    parsed = fallback_parse_query(question, schema_summary)
    parsed["used_groq"] = False
    parsed["parser_fallback"] = True
    return parsed


def parse_query(question, schema_string="", schema_summary=None):
    if not GROQ_AVAILABLE:
        return _fallback(question, schema_summary)

    user_content = (
        f"Schema:\n{schema_string}\n\nQuestion: {question}"
        if schema_string else f"Question: {question}"
    )

    try:
        response = _groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=400,
            temperature=0,
        )
        raw = response.choices[0].message.content.strip()
        intent = _extract_json_object(raw)
    except Exception:
        return _fallback(question)

    return {
        "original_text": question,
        "entity": intent.get("entity"),
        "aggregation": intent.get("aggregation"),
        "group_by": intent.get("group_by"),
        "limit": intent.get("limit"),
        "order_by": intent.get("order_by") or "DESC",
        "time_dimension": intent.get("time_dimension"),
        "filters": intent.get("filters"),
        "used_groq": True,
        "resolved_base_table": intent.get("resolved_base_table"),
        "resolved_group_table": intent.get("resolved_group_table"),
        "resolved_group_column": intent.get("resolved_group_column"),
        "resolved_metric_table": intent.get("resolved_metric_table"),
        "resolved_metric_column": intent.get("resolved_metric_column"),
        "resolved_filters": intent.get("resolved_filters"),
    }


def repair_sql(question: str, schema_string: str, sql: str, error: str) -> str | None:
    """Ask Groq for one bounded SQL repair. Returns None if unavailable."""
    if not GROQ_AVAILABLE:
        return None
    user_content = (
        f"Schema:\n{schema_string}\n\n"
        f"Question: {question}\n\n"
        f"Failed SQL:\n{sql}\n\n"
        f"Database error:\n{error}"
    )
    try:
        response = _groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": REPAIR_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=500,
            temperature=0,
        )
        repaired = response.choices[0].message.content.strip()
        repaired = re.sub(r"^```(?:sql)?\s*", "", repaired)
        repaired = re.sub(r"\s*```$", "", repaired).strip()
        return repaired or None
    except Exception:
        return None
