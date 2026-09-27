from db.schema_discovery import get_tables, get_columns, get_foreign_keys

def build_schema_map():
    schema = {}

    tables = get_tables()
    fks = get_foreign_keys()

    for table in tables:
        schema[table] = {
            "columns": [col[0] for col in get_columns(table)],
            "foreign_keys": []
        }

    for fk in fks:
        table, column, ref_table, ref_column = fk
        schema[table]["foreign_keys"].append(
            (column, ref_table, ref_column)
        )

    return schema