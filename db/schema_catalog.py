from dataclasses import dataclass, field
from typing import Dict, List, Optional

from db.db_connection import get_connection


@dataclass(frozen=True)
class ColumnMeta:
    name: str
    data_type: str
    is_nullable: bool = True
    is_primary_key: bool = False

    @property
    def normalized(self) -> str:
        return self.name.lower()

    @property
    def is_numeric(self) -> bool:
        t = self.data_type.lower()
        return any(x in t for x in (
            "integer", "numeric", "decimal", "double", "real", "money",
            "smallint", "bigint", "serial",
        ))

    @property
    def is_temporal(self) -> bool:
        t = self.data_type.lower()
        return "date" in t or "time" in t

    @property
    def is_text(self) -> bool:
        t = self.data_type.lower()
        return any(x in t for x in ("char", "text", "uuid", "json"))


@dataclass(frozen=True)
class ForeignKeyMeta:
    table: str
    column: str
    ref_table: str
    ref_column: str


@dataclass
class TableMeta:
    name: str
    columns: List[ColumnMeta] = field(default_factory=list)
    foreign_keys: List[ForeignKeyMeta] = field(default_factory=list)

    def column_names(self) -> List[str]:
        return [c.name for c in self.columns]

    def get_column(self, name: str) -> Optional[ColumnMeta]:
        n = name.lower()
        return next((c for c in self.columns if c.name.lower() == n), None)


@dataclass
class SchemaCatalog:
    tables: Dict[str, TableMeta] = field(default_factory=dict)

    def table_names(self) -> List[str]:
        return list(self.tables.keys())

    def get_table(self, name: str) -> Optional[TableMeta]:
        return self.tables.get(name)

    def summary(self) -> Dict[str, List[str]]:
        return {name: table.column_names() for name, table in self.tables.items()}


def load_schema_catalog(schema_name: str = "public") -> SchemaCatalog:
    """Load PostgreSQL schema metadata once, including column types, PKs and FKs."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            (schema_name,),
        )
        table_names = [row[0] for row in cur.fetchall()]

        cur.execute(
            """
            SELECT tc.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = %s
            """,
            (schema_name,),
        )
        primary_keys = {(table, col) for table, col in cur.fetchall()}

        tables: Dict[str, TableMeta] = {}
        for table in table_names:
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """,
                (schema_name, table),
            )
            columns = [
                ColumnMeta(
                    name=col,
                    data_type=data_type,
                    is_nullable=(nullable == "YES"),
                    is_primary_key=(table, col) in primary_keys,
                )
                for col, data_type, nullable in cur.fetchall()
            ]
            tables[table] = TableMeta(name=table, columns=columns)

        cur.execute(
            """
            SELECT
                tc.table_name,
                kcu.column_name,
                ccu.table_name AS foreign_table,
                ccu.column_name AS foreign_column
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.constraint_schema = tc.constraint_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = %s
              AND ccu.table_schema = %s
            """,
            (schema_name, schema_name),
        )
        for table, column, ref_table, ref_column in cur.fetchall():
            if table in tables:
                tables[table].foreign_keys.append(
                    ForeignKeyMeta(table, column, ref_table, ref_column)
                )
        cur.close()
        return SchemaCatalog(tables=tables)
    finally:
        conn.close()
