import re


FORBIDDEN_RE = re.compile(
    r"\b("
    r"alter|analyze|attach|call|comment|copy|create|delete|detach|drop|execute|"
    r"grant|insert|listen|load|lock|merge|notify|prepare|reindex|reset|revoke|"
    r"set|truncate|unlisten|update|vacuum"
    r")\b",
    re.IGNORECASE,
)


def _strip_comments_and_literals(sql: str) -> str:
    out = []
    i = 0
    in_string = False
    quote = ""
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if in_string:
            if ch == quote:
                if nxt == quote:
                    i += 2
                    continue
                in_string = False
            out.append(" ")
            i += 1
            continue
        if ch in ("'", '"'):
            in_string = True
            quote = ch
            out.append(" ")
            i += 1
            continue
        if ch == "-" and nxt == "-":
            while i < len(sql) and sql[i] != "\n":
                i += 1
            out.append(" ")
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < len(sql) and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _has_multiple_statements(sql_without_literals: str) -> bool:
    stripped = sql_without_literals.strip()
    if not stripped:
        return False
    if stripped.endswith(";"):
        stripped = stripped[:-1]
    return ";" in stripped


def _is_read_only_select(sql_without_literals: str) -> bool:
    s = sql_without_literals.strip().lstrip("(").strip()
    return s.lower().startswith(("select", "with"))


def validate_sql(sql, preflight=False):
    return validate_query(sql, preflight=preflight)


def validate_query(sql, preflight=False):
    if not sql or not isinstance(sql, str):
        return False, "SQL is empty"

    policy_sql = _strip_comments_and_literals(sql)
    if _has_multiple_statements(policy_sql):
        return False, "Only one SQL statement is allowed"
    if not _is_read_only_select(policy_sql):
        return False, "Only read-only SELECT queries are allowed"
    if FORBIDDEN_RE.search(policy_sql):
        return False, "Dangerous SQL keyword detected"

    if preflight:
        try:
            from db.db_connection import get_connection
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("BEGIN READ ONLY")
                    cur.execute(f"EXPLAIN {sql}")
                    cur.execute("ROLLBACK")
        except Exception as exc:
            return False, f"SQL preflight failed: {exc}"

    return True, "Valid"
