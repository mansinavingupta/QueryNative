"""
upload_handler.py
─────────────────
Local-file ingestion engine for QueryNative.

Responsibilities
1. Accept .csv / .xlsx / .sql / .sqlite uploads.
2. Smart-ingest into a *persistent* on-disk SQLite database
   (created once with tempfile.mkdtemp(); survives between requests).
3. Auto-infer column types, primary keys and foreign keys.
4. Expose schema (string + summary) so the rest of the pipeline
   (Groq intent extraction → SQL build → execution) can target
   the local file when an upload is "Active".
5. Build & execute SQL from a parsed intent dict — no LLM, no
   external API calls. 100% local Pandas + SQLAlchemy + sqlite3.

Constraints respected
- Flask only (no FastAPI).
- Groq is NOT called from this file; intent comes from groq_parser
  in app.py and is consumed by execute_intent() here.
"""

from __future__ import annotations

import os
import io
import re
import csv
import sqlite3
import tempfile
import shutil
from contextlib import closing
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine


# ─────────────────────────────────────────────
# helpers — naming, sanitising
# ─────────────────────────────────────────────
_SAFE_RE = re.compile(r"[^A-Za-z0-9_]")


def _safe_ident(name: str, fallback: str = "col") -> str:
    """Return a SQLite-safe identifier (no quotes needed)."""
    name = (name or "").strip()
    if not name:
        return fallback
    name = _SAFE_RE.sub("_", name)
    if name[0].isdigit():
        name = f"_{name}"
    return name


def _quote(ident: str) -> str:
    """Double-quote an identifier for safe SQL injection-free use."""
    return '"' + ident.replace('"', '""') + '"'


# ─────────────────────────────────────────────
# Type inference
# ─────────────────────────────────────────────
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


def _infer_series_type(series: pd.Series) -> str:
    """Return one of: INTEGER, REAL, NUMERIC, TEXT, DATETIME, BOOLEAN."""
    non_null = series.dropna()
    if non_null.empty:
        return "TEXT"

    # pandas already-typed columns
    if pd.api.types.is_bool_dtype(series):
        return "BOOLEAN"
    if pd.api.types.is_integer_dtype(series):
        return "INTEGER"
    if pd.api.types.is_float_dtype(series):
        return "REAL"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "DATETIME"

    # string heuristics
    sample = non_null.astype(str).str.strip()

    # all integers?
    if sample.head(200).map(lambda v: bool(_INT_RE.match(v))).all():
        return "INTEGER"
    # all floats / numeric?
    if sample.head(200).map(
        lambda v: bool(_INT_RE.match(v) or _FLOAT_RE.match(v))
    ).all():
        return "REAL"

    # datetime?
    def _looks_date(v: str) -> bool:
        for fmt in _DATE_FORMATS:
            try:
                datetime.strptime(v, fmt)
                return True
            except ValueError:
                continue
        return False

    if sample.head(50).map(_looks_date).all():
        return "DATETIME"

    return "TEXT"


# ─────────────────────────────────────────────
# Primary / Foreign key inference
# ─────────────────────────────────────────────
def _infer_primary_key(df: pd.DataFrame, table_name: str) -> Optional[str]:
    """Pick a likely PK column: matching name pattern + uniqueness + non-null."""
    table_lc = table_name.lower()
    candidates_priority = []
    for col in df.columns:
        c = col.lower()
        score = 0
        if c == "id":
            score = 100
        elif c == f"{table_lc}_id" or c == f"{table_lc}id":
            score = 95
        elif c.endswith("_id") or c.endswith("id"):
            score = 50
        elif c in ("pk", "key", "uid", "uuid"):
            score = 80
        if score:
            candidates_priority.append((score, col))

    candidates_priority.sort(reverse=True)

    for _, col in candidates_priority:
        s = df[col]
        if s.notna().all() and s.is_unique:
            return col

    # fallback: any unique non-null column
    for col in df.columns:
        s = df[col]
        if s.notna().all() and s.is_unique:
            return col
    return None


def _infer_foreign_keys(
    df: pd.DataFrame, table_name: str, all_tables: Dict[str, Dict[str, Any]]
) -> List[Tuple[str, str, str]]:
    """
    Returns list of (col, ref_table, ref_col).
    A column is treated as a FK if its name matches '<other>_id' or '<other>id'
    AND <other> exists as another table (singular or plural form) AND the other
    table has a PK.
    """
    fks: List[Tuple[str, str, str]] = []
    for col in df.columns:
        c = col.lower()
        m = re.match(r"^([a-z][a-z0-9_]*?)(_id|id)$", c)
        if not m:
            continue
        ref_root = m.group(1).rstrip("_")
        if not ref_root or ref_root == table_name.lower():
            continue
        # try direct, plural, singular matches against other tables
        candidates = {ref_root, ref_root + "s", ref_root.rstrip("s")}
        match = None
        for other_name in all_tables.keys():
            if other_name == table_name:
                continue
            if other_name.lower() in candidates:
                match = other_name
                break
        if match and all_tables[match].get("primary_key"):
            fks.append((col, match, all_tables[match]["primary_key"]))
    return fks


# ─────────────────────────────────────────────
# SQL dump executor — naive but safe-ish
# ─────────────────────────────────────────────
_DROP_KEYWORDS = re.compile(
    r"\b(DROP\s+DATABASE|ATTACH\s+DATABASE|DETACH\s+DATABASE|PRAGMA)\b",
    re.IGNORECASE,
)


def _split_sql_statements(sql_text: str) -> List[str]:
    """Naive splitter that respects single-quoted strings and comments."""
    out, buf, in_str, str_ch, i = [], [], False, "", 0
    n = len(sql_text)
    while i < n:
        ch = sql_text[i]
        # line comment
        if not in_str and ch == "-" and i + 1 < n and sql_text[i + 1] == "-":
            while i < n and sql_text[i] != "\n":
                i += 1
            continue
        # block comment
        if not in_str and ch == "/" and i + 1 < n and sql_text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (sql_text[i] == "*" and sql_text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if in_str:
            buf.append(ch)
            if ch == str_ch:
                # handle '' escape
                if i + 1 < n and sql_text[i + 1] == str_ch:
                    buf.append(sql_text[i + 1])
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch in ("'", '"', "`"):
            in_str = True
            str_ch = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


# ─────────────────────────────────────────────
# THE MANAGER  (singleton)
# ─────────────────────────────────────────────
class UploadManager:
    """Holds the persistent SQLite file + metadata + active flag."""

    def __init__(self) -> None:
        self._dir: Optional[str] = None
        self.sqlite_path: Optional[str] = None
        self.engine: Optional[Engine] = None
        self.source_name: Optional[str] = None
        self.source_type: Optional[str] = None  # csv / xlsx / sql / sqlite
        self.tables: Dict[str, Dict[str, Any]] = {}  # name → meta
        self.active: bool = False
        self.last_message: str = ""

    # ── lifecycle ────────────────────────────────────────────────
    def _ensure_dir(self) -> str:
        if self._dir is None or not os.path.isdir(self._dir):
            self._dir = tempfile.mkdtemp(prefix="querynative_upload_")
        return self._dir

    def _new_sqlite_file(self, source_name: str) -> str:
        """Create a fresh persistent .sqlite file on disk."""
        self.reset()  # wipe previous upload
        d = self._ensure_dir()
        # short hash-ish suffix for traceability
        safe = _safe_ident(os.path.splitext(os.path.basename(source_name))[0]) or "upload"
        suffix = os.urandom(4).hex()
        path = os.path.join(d, f"upload_{safe}_{suffix}.sqlite")
        # touch the file so SQLAlchemy/sqlite create it
        with open(path, "wb"):
            pass
        self.sqlite_path = path
        self.engine = create_engine(f"sqlite:///{path}", future=True)
        return path

    def reset(self) -> None:
        """Tear down current upload state (keeps the temp dir for reuse)."""
        try:
            if self.engine is not None:
                self.engine.dispose()
        except Exception:
            pass
        self.engine = None
        if self.sqlite_path and os.path.exists(self.sqlite_path):
            try:
                os.remove(self.sqlite_path)
            except OSError:
                pass
        self.sqlite_path = None
        self.source_name = None
        self.source_type = None
        self.tables = {}
        self.active = False
        self.last_message = ""

    def shutdown(self) -> None:
        """Full cleanup including the temp directory."""
        self.reset()
        if self._dir and os.path.isdir(self._dir):
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    # ── high-level entry point ───────────────────────────────────
    def handle_upload(self, file_storage, declared_kind: Optional[str] = None) -> Dict[str, Any]:
        """
        file_storage : werkzeug FileStorage (request.files['file'])
        declared_kind: 'csv' | 'xlsx' | 'sql' | 'sqlite' (UI hint, optional)

        Returns dict ready to ship as JSON:
        { success, source_name, source_type, schema_id, message,
          tables: [{name, columns, row_count, primary_key, foreign_keys}], }
        """
        filename = file_storage.filename or "upload"
        kind = (declared_kind or "").lower().strip() or self._sniff_kind(filename)

        if kind not in ("csv", "xlsx", "xls", "sql", "sqlite", "db"):
            return {
                "success": False,
                "error": f"Unsupported file type: {kind or filename}. "
                         f"Use .csv, .xlsx, .sql or .sqlite.",
            }

        self._new_sqlite_file(filename)
        self.source_name = filename
        self.source_type = "xlsx" if kind in ("xlsx", "xls") else (
            "sqlite" if kind in ("sqlite", "db") else kind
        )

        try:
            if self.source_type == "csv":
                self._ingest_csv(file_storage, filename)
            elif self.source_type == "xlsx":
                self._ingest_excel(file_storage)
            elif self.source_type == "sql":
                self._ingest_sql_dump(file_storage)
            elif self.source_type == "sqlite":
                self._ingest_sqlite_file(file_storage)
        except Exception as exc:  # any parser/engine error → reset & report
            err = f"Failed to ingest {filename}: {exc}"
            self.reset()
            return {"success": False, "error": err}

        # Second-pass FK inference now that all tables are known
        self._post_pass_foreign_keys()

        schema_id = self._schema_id()
        self.last_message = (
            f"Created schema \"{schema_id}\" with {len(self.tables)} table(s)."
        )

        return {
            "success": True,
            "source_name": filename,
            "source_type": self.source_type,
            "schema_id": schema_id,
            "message": self.last_message,
            "active": self.active,
            "tables": self._tables_payload(),
        }

    @staticmethod
    def _sniff_kind(name: str) -> str:
        ext = os.path.splitext(name)[1].lower().lstrip(".")
        return ext

    def _schema_id(self) -> str:
        base = _safe_ident(
            os.path.splitext(os.path.basename(self.source_name or "upload"))[0]
        )
        suffix = os.path.splitext(os.path.basename(self.sqlite_path or ""))[0].split("_")[-1]
        return f"upload_{base}_{suffix}"

    # ── ingestion: CSV ───────────────────────────────────────────
    def _ingest_csv(self, file_storage, filename: str) -> None:
        raw = file_storage.read()
        if not raw:
            raise ValueError("Empty file.")
        # Try common encodings
        text_data = None
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                text_data = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text_data is None:
            raise ValueError("Could not decode CSV text.")

        # Sniff delimiter
        try:
            dialect = csv.Sniffer().sniff(text_data[:4096], delimiters=",;\t|")
            sep = dialect.delimiter
        except csv.Error:
            sep = ","

        df = pd.read_csv(io.StringIO(text_data), sep=sep)
        df.columns = [self._dedupe_col(c, df.columns, i) for i, c in enumerate(df.columns)]
        table = _safe_ident(os.path.splitext(os.path.basename(filename))[0]) or "data"
        self._write_dataframe(df, table)

    def _ingest_excel(self, file_storage) -> None:
        raw = file_storage.read()
        if not raw:
            raise ValueError("Empty Excel file.")
        xls = pd.ExcelFile(io.BytesIO(raw))
        if not xls.sheet_names:
            raise ValueError("Excel file has no sheets.")
        for sheet in xls.sheet_names:
            df = xls.parse(sheet)
            if df.empty and not list(df.columns):
                continue
            df.columns = [self._dedupe_col(c, df.columns, i) for i, c in enumerate(df.columns)]
            tbl = _safe_ident(str(sheet)) or f"sheet_{xls.sheet_names.index(sheet)+1}"
            self._write_dataframe(df, tbl)

    def _ingest_sql_dump(self, file_storage) -> None:
        raw = file_storage.read()
        if not raw:
            raise ValueError("Empty SQL file.")
        # decode tolerantly
        for enc in ("utf-8", "latin-1"):
            try:
                sql_text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                sql_text = None
        if sql_text is None:
            raise ValueError("Could not decode SQL text.")

        statements = _split_sql_statements(sql_text)
        if not statements:
            raise ValueError("No SQL statements found.")

        # Execute on raw sqlite3 (more permissive than SQLAlchemy)
        with closing(sqlite3.connect(self.sqlite_path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON;")
            for stmt in statements:
                # skip dangerous / unsupported
                if _DROP_KEYWORDS.search(stmt):
                    continue
                # rewrite MySQL-isms minimally so common dumps still load
                rewritten = self._sanitise_sql_dialect(stmt)
                if not rewritten:
                    continue
                try:
                    conn.execute(rewritten)
                except sqlite3.Error:
                    # tolerate stray statements; keep going
                    continue
            conn.commit()

        # Catalogue what landed in the DB
        self._catalogue_existing_tables()

    @staticmethod
    def _sanitise_sql_dialect(stmt: str) -> str:
        s = stmt.strip()
        if not s:
            return ""
        # Strip ENGINE=..., CHARSET=..., COLLATE=... (MySQL)
        s = re.sub(r"\)\s*ENGINE\s*=\s*\w+.*?(?=;|$)", ")", s, flags=re.IGNORECASE | re.DOTALL)
        s = re.sub(r"DEFAULT\s+CHARSET\s*=\s*\w+", "", s, flags=re.IGNORECASE)
        s = re.sub(r"COLLATE\s+\w+", "", s, flags=re.IGNORECASE)
        # Strip backticks
        s = s.replace("`", '"')
        # USE/SET ignored
        if re.match(r"^\s*(USE|SET|LOCK|UNLOCK|DELIMITER|START\s+TRANSACTION|BEGIN)\b", s, re.IGNORECASE):
            return ""
        # Type rewrites that SQLite tolerates anyway
        s = re.sub(r"\bTINYINT\(1\)\b", "BOOLEAN", s, flags=re.IGNORECASE)
        s = re.sub(r"\bAUTO_INCREMENT\b", "AUTOINCREMENT", s, flags=re.IGNORECASE)
        return s

    def _ingest_sqlite_file(self, file_storage) -> None:
        """
        Migrate every table from the uploaded .sqlite into our persistent file,
        preserving column types and FK constraints where possible.
        """
        raw = file_storage.read()
        if not raw:
            raise ValueError("Empty SQLite file.")
        # write source temp
        src_path = os.path.join(self._ensure_dir(), f"src_{os.urandom(4).hex()}.sqlite")
        with open(src_path, "wb") as fh:
            fh.write(raw)

        try:
            with closing(sqlite3.connect(src_path)) as src, \
                 closing(sqlite3.connect(self.sqlite_path)) as dst:
                src.row_factory = sqlite3.Row
                # gather user tables
                rows = src.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if not rows:
                    raise ValueError("Source SQLite contains no tables.")
                for r in rows:
                    create_sql = r["sql"]
                    if create_sql:
                        try:
                            dst.execute(create_sql)
                        except sqlite3.Error:
                            # best-effort: rebuild from PRAGMA
                            self._rebuild_table_from_pragma(src, dst, r["name"])
                    else:
                        self._rebuild_table_from_pragma(src, dst, r["name"])
                    # copy data
                    cols = [c[1] for c in src.execute(f'PRAGMA table_info("{r["name"]}")')]
                    if not cols:
                        continue
                    placeholders = ",".join("?" * len(cols))
                    col_list = ",".join(_quote(c) for c in cols)
                    cur = src.execute(f'SELECT {col_list} FROM "{r["name"]}"')
                    batch = []
                    for row in cur:
                        batch.append([row[c] for c in cols])
                        if len(batch) >= 1000:
                            dst.executemany(
                                f'INSERT INTO "{r["name"]}" ({col_list}) VALUES ({placeholders})',
                                batch,
                            )
                            batch = []
                    if batch:
                        dst.executemany(
                            f'INSERT INTO "{r["name"]}" ({col_list}) VALUES ({placeholders})',
                            batch,
                        )
                dst.commit()
        finally:
            try:
                os.remove(src_path)
            except OSError:
                pass

        self._catalogue_existing_tables()

    @staticmethod
    def _rebuild_table_from_pragma(src, dst, table_name: str) -> None:
        cols = src.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        defs = []
        pks = []
        for c in cols:
            # cid, name, type, notnull, dflt, pk
            line = f'{_quote(c[1])} {c[2] or "TEXT"}'
            if c[3]:
                line += " NOT NULL"
            if c[5]:
                pks.append(c[1])
            defs.append(line)
        if pks:
            defs.append(f'PRIMARY KEY ({", ".join(_quote(p) for p in pks)})')
        dst.execute(f'CREATE TABLE IF NOT EXISTS "{table_name}" ({", ".join(defs)})')

    # ── DataFrame writer + cataloguer ───────────────────────────
    def _write_dataframe(self, df: pd.DataFrame, table: str) -> None:
        if df is None or df.shape[1] == 0:
            return
        # Sanitise column names
        new_cols, seen = [], {}
        for c in df.columns:
            base = _safe_ident(str(c))
            if base in seen:
                seen[base] += 1
                base = f"{base}_{seen[base]}"
            else:
                seen[base] = 0
            new_cols.append(base)
        df = df.copy()
        df.columns = new_cols

        # Infer types per column to build CREATE TABLE manually
        col_types = {c: _infer_series_type(df[c]) for c in df.columns}
        pk = _infer_primary_key(df, table)

        col_defs = []
        for c in df.columns:
            t = col_types[c]
            sql_t = "INTEGER" if t == "BOOLEAN" else (
                "INTEGER" if t == "INTEGER" else
                "REAL" if t == "REAL" else
                "TEXT" if t in ("TEXT", "DATETIME") else "TEXT"
            )
            line = f"{_quote(c)} {sql_t}"
            if c == pk:
                line += " PRIMARY KEY"
            col_defs.append(line)

        with closing(sqlite3.connect(self.sqlite_path)) as conn:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            conn.execute(f'CREATE TABLE "{table}" ({", ".join(col_defs)})')
            # Bulk insert via pandas (uses SQLAlchemy under the hood for typing)
            df.to_sql(table, conn, if_exists="append", index=False)
            conn.commit()

        self.tables[table] = {
            "columns": [(c, col_types[c]) for c in df.columns],
            "row_count": int(len(df)),
            "primary_key": pk,
            "foreign_keys": [],   # filled in second pass
        }

    def _catalogue_existing_tables(self) -> None:
        """Inspect the SQLite file and rebuild self.tables from scratch."""
        self.tables = {}
        if not self.sqlite_path or not os.path.exists(self.sqlite_path):
            return
        with closing(sqlite3.connect(self.sqlite_path)) as conn:
            tnames = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]
            for t in tnames:
                cols_info = conn.execute(f'PRAGMA table_info("{t}")').fetchall()
                cols = [(r[1], (r[2] or "TEXT").upper()) for r in cols_info]
                pk = next((r[1] for r in cols_info if r[5]), None)
                row_count = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                # native FKs from pragma
                fks_native = conn.execute(f'PRAGMA foreign_key_list("{t}")').fetchall()
                fks: List[Tuple[str, str, str]] = [
                    (r[3], r[2], r[4]) for r in fks_native if r[2] and r[3] and r[4]
                ]
                self.tables[t] = {
                    "columns": cols,
                    "row_count": int(row_count),
                    "primary_key": pk,
                    "foreign_keys": fks,
                }

    def _post_pass_foreign_keys(self) -> None:
        """Run heuristic FK detection across all known tables (for csv/xlsx/sql)."""
        if not self.tables:
            return
        # only fill in if the table currently has no native FKs
        for tname, meta in self.tables.items():
            if meta["foreign_keys"]:
                continue
            # Build a virtual DataFrame view over column names only (PK heuristic uses df values)
            # For FK detection we just need column names & other tables' PKs.
            # So build a stub df with empty values — the inference function only inspects names.
            stub_df = pd.DataFrame({c: [] for c, _ in meta["columns"]})
            fks = _infer_foreign_keys(stub_df, tname, self.tables)
            meta["foreign_keys"] = fks

    # ── data dedup helper ────────────────────────────────────────
    @staticmethod
    def _dedupe_col(col: Any, all_cols, idx: int) -> str:
        name = str(col).strip() or f"col_{idx+1}"
        # strip pandas "Unnamed: N" → col_N
        if re.match(r"^Unnamed:?\s*\d+$", name, re.IGNORECASE):
            return f"col_{idx+1}"
        return name

    # ── public schema accessors ─────────────────────────────────
    def is_active(self) -> bool:
        return bool(self.active and self.sqlite_path and os.path.exists(self.sqlite_path))

    def activate(self) -> bool:
        if not self.sqlite_path or not self.tables:
            return False
        self.active = True
        return True

    def deactivate(self) -> None:
        self.active = False

    def get_status(self) -> Dict[str, Any]:
        return {
            "uploaded": self.sqlite_path is not None,
            "active": self.is_active(),
            "source_name": self.source_name,
            "source_type": self.source_type,
            "schema_id": self._schema_id() if self.sqlite_path else None,
            "message": self.last_message,
            "tables": self._tables_payload() if self.sqlite_path else [],
        }

    def _tables_payload(self) -> List[Dict[str, Any]]:
        out = []
        for name, meta in self.tables.items():
            out.append({
                "name": name,
                "columns": [{"name": c, "type": t} for c, t in meta["columns"]],
                "row_count": meta["row_count"],
                "primary_key": meta["primary_key"],
                "foreign_keys": [
                    {"column": col, "references_table": rt, "references_column": rc}
                    for col, rt, rc in meta["foreign_keys"]
                ],
            })
        return out

    def get_schema_summary(self) -> Dict[str, List[str]]:
        """{ table_name: [col_name, ...] } — same shape as the PG version."""
        return {
            name: [c for c, _ in meta["columns"]]
            for name, meta in self.tables.items()
        }

    def get_schema_string(self) -> str:
        """Human-readable schema string for Groq context."""
        if not self.tables:
            return ""
        lines = ["Database Tables (Local Upload):\n"]
        for name, meta in self.tables.items():
            cols_str = ", ".join(f"{c} ({t.lower()})" for c, t in meta["columns"])
            lines.append(f"- {name}: {cols_str}")
            for col, rt, rc in meta["foreign_keys"]:
                lines.append(f"    FK: {col} → {rt}.{rc}")
            if meta["primary_key"]:
                lines.append(f"    PK: {meta['primary_key']}")
        lines.append("")
        return "\n".join(lines)

    def get_table_row_counts(self) -> Dict[str, int]:
        return {name: meta["row_count"] for name, meta in self.tables.items()}

    # ─────────────────────────────────────────────────────────────
    # INTENT → SQL  (local builder)
    # ─────────────────────────────────────────────────────────────
    def execute_intent(self, parsed: Dict[str, Any]) -> Tuple[str, List[str], List[List[Any]]]:
        """
        Build & run a SQL statement on the active SQLite from a parsed intent dict.
        Returns (sql, columns, rows).
        Raises ValueError if no usable mapping is possible.
        """
        if not self.is_active():
            raise ValueError("No active local upload.")
        sql, params = self._build_sql(parsed)
        with closing(sqlite3.connect(self.sqlite_path)) as conn:
            cur = conn.execute(sql, params)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [list(r) for r in cur.fetchall()]
        return sql, cols, rows

    # ── builder helpers ─────────────────────────────────────────
    def _build_sql(self, parsed: Dict[str, Any]) -> Tuple[str, List[Any]]:
        entity = (parsed.get("entity") or "").lower().strip()
        agg = (parsed.get("aggregation") or "").lower().strip() or None
        group_by = (parsed.get("group_by") or "").lower().strip() or None
        time_dim = (parsed.get("time_dimension") or "").lower().strip() or None
        filters = parsed.get("filters") or []
        limit = parsed.get("limit")
        order = (parsed.get("order_by") or "DESC").upper()
        order = "ASC" if order == "ASC" else "DESC"

        # Check for Groq metadata-driven mappings
        resolved_base = parsed.get("resolved_base_table")
        if resolved_base and resolved_base in self.tables:
            base_table = resolved_base
        else:
            base_table = self._match_table(entity) or self._fallback_table(parsed)
            
        if not base_table:
            raise ValueError("Could not match any table for this question.")

        cols = [c for c, _ in self.tables[base_table]["columns"]]
        col_lc = {c.lower(): c for c in cols}

        # Resolve metric column for sum/avg/min/max
        resolved_metric = parsed.get("resolved_metric_column")
        if resolved_metric:
            metric_col = resolved_metric
        else:
            metric_col = self._pick_metric_column(base_table, parsed)

        # Resolve group-by expression
        resolved_group_col = parsed.get("resolved_group_column")
        if resolved_group_col and not time_dim:
            if resolved_group_col.lower() in col_lc:
                group_expr = _quote(col_lc[resolved_group_col.lower()])
                group_label = col_lc[resolved_group_col.lower()]
            else:
                group_expr = _quote(resolved_group_col)
                group_label = resolved_group_col
        else:
            group_expr, group_label = self._resolve_group_expr(
                base_table, group_by, time_dim, col_lc
            )

        # Build SELECT
        params: List[Any] = []
        select_parts: List[str] = []
        if group_expr:
            select_parts.append(f"{group_expr} AS {_quote(group_label)}")

        def quote_expression(expr: str) -> str:
            if not expr:
                return ""
            if any(op in expr for op in ("*", "+", "-", "/")):
                tokens = re.split(r"(\s*[\*\+\-\/]\s*)", expr)
                qualified = []
                for tok in tokens:
                    tok = tok.strip()
                    if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", tok):
                        if tok.lower() in col_lc:
                            qualified.append(_quote(col_lc[tok.lower()]))
                        else:
                            qualified.append(_quote(tok))
                    else:
                        qualified.append(tok)
                return " ".join(qualified)
            if expr.lower() in col_lc:
                return _quote(col_lc[expr.lower()])
            return _quote(expr)

        agg_sql = None
        if agg == "count":
            agg_sql = "COUNT(*)"
        elif agg == "sum" and metric_col:
            agg_sql = f"SUM({quote_expression(metric_col)})"
        elif agg == "avg" and metric_col:
            agg_sql = f"AVG({quote_expression(metric_col)})"
        elif agg == "max" and metric_col:
            agg_sql = f"MAX({quote_expression(metric_col)})"
        elif agg == "min" and metric_col:
            agg_sql = f"MIN({quote_expression(metric_col)})"

        if agg_sql:
            select_parts.append(f"{agg_sql} AS {_quote(self._agg_alias(agg, metric_col))}")
        elif group_expr:
            select_parts.append('COUNT(*) AS "count"')

        if not select_parts:
            select_parts.append("*")

        sql = f"SELECT {', '.join(select_parts)} FROM {_quote(base_table)}"

        # WHERE
        resolved_filters = parsed.get("resolved_filters")
        if resolved_filters and isinstance(resolved_filters, list):
            where_parts = []
            for f in resolved_filters:
                if not isinstance(f, dict):
                    continue
                f_col = f.get("column")
                f_op = f.get("operator") or "="
                val = f.get("value")
                
                if f_col and f_col.lower() in col_lc:
                    real_col = col_lc[f_col.lower()]
                    op_upper = str(f_op).upper()
                    if op_upper == "BETWEEN" and isinstance(val, (list, tuple)) and len(val) == 2:
                        where_parts.append(f"{_quote(real_col)} BETWEEN ? AND ?")
                        params.extend(val)
                    elif op_upper in ("LIKE", "ILIKE"):
                        where_parts.append(f"{_quote(real_col)} LIKE ?")
                        params.append(val)
                    elif op_upper == "IN" and isinstance(val, (list, tuple)):
                        placeholders = ", ".join(["?"] * len(val))
                        where_parts.append(f"{_quote(real_col)} IN ({placeholders})")
                        params.extend(val)
                    else:
                        where_parts.append(f"{_quote(real_col)} {f_op} ?")
                        params.append(val)
            
            if not where_parts and filters:
                where_parts = self._build_filters(base_table, filters, col_lc, params)
        else:
            where_parts = self._build_filters(base_table, filters, col_lc, params)
            
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)

        # GROUP BY
        if group_expr:
            sql += f" GROUP BY {group_expr}"

        # ORDER BY
        if group_expr:
            sql += f" ORDER BY 2 {order}"

        # LIMIT
        if isinstance(limit, int) and limit > 0:
            sql += f" LIMIT {int(limit)}"
        elif group_expr or agg_sql:
            sql += " LIMIT 100"
        else:
            sql += " LIMIT 200"

        return sql, params

    def _agg_alias(self, agg: str, col: Optional[str]) -> str:
        if agg == "count":
            return "count"
        if not col:
            return agg
        return f"{agg}_{col}"

    def _match_table(self, name: str) -> Optional[str]:
        if not name:
            return None
        n = name.lower().strip()
        # exact, then plural/singular, then substring
        for t in self.tables:
            if t.lower() == n:
                return t
        for t in self.tables:
            tl = t.lower()
            if tl == n + "s" or tl + "s" == n or tl == n.rstrip("s") or tl.rstrip("s") == n:
                return t
        for t in self.tables:
            if n in t.lower() or t.lower() in n:
                return t
        return None

    def _fallback_table(self, parsed: Dict[str, Any]) -> Optional[str]:
        # Largest table is the safest fallback for "how many records?" style asks
        if not self.tables:
            return None
        return max(self.tables.items(), key=lambda kv: kv[1]["row_count"])[0]

    def _pick_metric_column(self, table: str, parsed: Dict[str, Any]) -> Optional[str]:
        meta = self.tables[table]
        # Prefer numeric (INTEGER/REAL) and exclude PK
        pk = (meta.get("primary_key") or "").lower()
        numeric_cols = [c for c, t in meta["columns"]
                        if t in ("INTEGER", "REAL") and c.lower() != pk]
        if not numeric_cols:
            return None

        # Try to honour terms in question
        q = (parsed.get("original_text") or "").lower()
        priority_terms = ["revenue", "sales", "amount", "price", "total",
                          "salary", "score", "value", "cost", "qty", "quantity"]
        for term in priority_terms:
            if term in q:
                for c in numeric_cols:
                    if term in c.lower():
                        return c
        # First numeric column otherwise
        return numeric_cols[0]

    def _resolve_group_expr(
        self,
        table: str,
        group_by: Optional[str],
        time_dim: Optional[str],
        col_lc: Dict[str, str],
    ) -> Tuple[Optional[str], Optional[str]]:
        # time dim wins
        if time_dim in ("year", "month", "quarter", "day"):
            date_col, is_dt = self._pick_datetime_column(table, col_lc)
            if date_col:
                qcol = _quote(date_col)
                if not is_dt:
                    # already an integer/text year column — use as-is
                    return qcol, date_col
                if time_dim == "year":
                    return f"strftime('%Y', {qcol})", "year"
                if time_dim == "month":
                    return f"strftime('%Y-%m', {qcol})", "month"
                if time_dim == "quarter":
                    return (
                        f"strftime('%Y', {qcol}) || '-Q' || "
                        f"((CAST(strftime('%m', {qcol}) AS INTEGER)-1)/3 + 1)",
                        "quarter",
                    )
                if time_dim == "day":
                    return f"strftime('%Y-%m-%d', {qcol})", "day"
        if not group_by:
            return None, None
        gb = group_by.lower()
        # exact column match
        if gb in col_lc:
            real = col_lc[gb]
            return _quote(real), real
        # match by suffix (e.g. group_by="category" → "category_id" / "category_name")
        for lc_key, real in col_lc.items():
            if gb in lc_key:
                return _quote(real), real
        # year/month tokens slipped into group_by
        if gb in ("year", "month", "quarter", "day"):
            return self._resolve_group_expr(table, None, gb, col_lc)
        # fallback — first text column
        for c, t in self.tables[table]["columns"]:
            if t == "TEXT":
                return _quote(c), c
        return None, None

    def _pick_datetime_column(
        self, table: str, col_lc: Dict[str, str]
    ) -> Tuple[Optional[str], bool]:
        """Returns (column_name, is_real_datetime). is_real_datetime=False means
        an integer/text year-like column we should use as-is."""
        meta = self.tables[table]
        for c, t in meta["columns"]:
            if t == "DATETIME":
                return c, True
        for hint in ("date", "created_at", "timestamp", "time"):
            if hint in col_lc:
                return col_lc[hint], True
        # fall back to a year-named integer/text column
        for c, t in meta["columns"]:
            if c.lower() in ("year", "yr", "fiscal_year", "fy"):
                return c, False
        return None, False

    def _build_filters(
        self,
        table: str,
        filters: List[Dict[str, Any]],
        col_lc: Dict[str, str],
        params: List[Any],
    ) -> List[str]:
        if not filters:
            return []
        out: List[str] = []
        meta = self.tables[table]
        col_names = [c for c, _ in meta["columns"]]

        def find(*hints: str) -> Optional[str]:
            for h in hints:
                if h in col_lc:
                    return col_lc[h]
            for c in col_names:
                lc = c.lower()
                for h in hints:
                    if h in lc:
                        return c
            return None

        for f in filters:
            if not isinstance(f, dict):
                continue
            ftype = (f.get("type") or "").lower()
            val = f.get("value")

            if ftype == "year":
                date_col, is_dt = self._pick_datetime_column(table, col_lc)
                if date_col:
                    if is_dt:
                        out.append(f"strftime('%Y', {_quote(date_col)}) = ?")
                        params.append(str(val))
                    else:
                        out.append(f"{_quote(date_col)} = ?")
                        params.append(val)
                else:
                    yc = find("year")
                    if yc:
                        out.append(f"{_quote(yc)} = ?"); params.append(val)
            elif ftype == "country":
                col = find("country", "nation")
                if col:
                    out.append(f"LOWER({_quote(col)}) = LOWER(?)"); params.append(str(val))
            elif ftype == "category":
                col = find("category", "type", "kind", "genre")
                if col:
                    out.append(f"LOWER({_quote(col)}) = LOWER(?)"); params.append(str(val))
            elif ftype == "genre":
                col = find("genre", "category")
                if col:
                    out.append(f"LOWER({_quote(col)}) = LOWER(?)"); params.append(str(val))
            elif ftype == "price_gt":
                col = find("price", "amount", "cost")
                if col:
                    out.append(f"{_quote(col)} > ?"); params.append(val)
            elif ftype == "price_lt":
                col = find("price", "amount", "cost")
                if col:
                    out.append(f"{_quote(col)} < ?"); params.append(val)
            elif ftype == "price_between":
                col = find("price", "amount", "cost")
                if col and isinstance(val, (list, tuple)) and len(val) == 2:
                    out.append(f"{_quote(col)} BETWEEN ? AND ?")
                    params.extend(val)
            elif ftype == "discontinued":
                col = find("discontinued", "active", "is_active")
                if col:
                    out.append(f"{_quote(col)} = 1")
        return out


# ─────────────────────────────────────────────
# module-level singleton
# ─────────────────────────────────────────────
upload_manager = UploadManager()
