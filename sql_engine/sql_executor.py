from db.db_connection import get_connection

def execute_query(sql):

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN READ ONLY")
        cursor.execute(sql)

        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        conn.rollback()
        return columns, rows
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
