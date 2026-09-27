from db.db_connection import get_connection
from db.schema_catalog import load_schema_catalog

def get_tables(conn=None):
    close = conn is None
    if conn is None: conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' ORDER BY table_name
    """)
    tables = [t[0] for t in cur.fetchall()]
    cur.close()
    if close: conn.close()
    return tables

def get_columns(table_name, conn=None):
    close = conn is None
    if conn is None: conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = %s AND table_schema = 'public'
        ORDER BY ordinal_position
    """, (table_name,))
    columns = cur.fetchall()
    cur.close()
    if close: conn.close()
    return columns

def get_foreign_keys(conn=None):
    close = conn is None
    if conn is None: conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            tc.table_name,
            kcu.column_name,
            ccu.table_name  AS foreign_table,
            ccu.column_name AS foreign_column
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
          ON tc.constraint_name = kcu.constraint_name
        JOIN information_schema.constraint_column_usage AS ccu
          ON ccu.constraint_name = tc.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = 'public'
          AND ccu.table_schema = 'public'
    """)
    fks = cur.fetchall()
    cur.close()
    if close: conn.close()
    return fks

def build_schema_string():
    """
    Build a human-readable schema string from the connected database.
    Used as context for Groq when parsing questions.
    """
    conn = get_connection()
    try:
        tables  = get_tables(conn)
        fks     = get_foreign_keys(conn)

        # Build FK map: table -> [(col, ref_table, ref_col)]
        fk_map = {}
        for table, col, ref_table, ref_col in fks:
            fk_map.setdefault(table, []).append((col, ref_table, ref_col))

        lines = ["Database Tables:\n"]
        for table in tables:
            cols = get_columns(table, conn)
            col_strs = []
            for col_name, col_type in cols:
                # Simplify type names
                simple_type = (col_type
                    .replace("character varying", "varchar")
                    .replace("integer", "int")
                    .replace("numeric", "decimal")
                    .replace("timestamp without time zone", "timestamp")
                    .replace("double precision", "float"))
                col_strs.append(f"{col_name} ({simple_type})")
            lines.append(f"- {table}: {', '.join(col_strs)}")

            # Add FK relationships
            if table in fk_map:
                for col, ref_table, ref_col in fk_map[table]:
                    lines.append(f"    FK: {col} → {ref_table}.{ref_col}")

        lines.append("")
        return "\n".join(lines)
    finally:
        conn.close()

def get_schema_summary():
    """
    Return a dict summary of the schema for display in the UI.
    """
    return load_schema_catalog().summary()
