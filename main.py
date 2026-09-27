# main.py
from nlp.groq_parser import parse_query
from schema.schema_linker import link_schema
from sql_engine.query_builder import build_query
from sql_engine.query_validator import validate_query
from sql_engine.sql_executor import execute_query
from visualizer.visualize import visualize_result
from db.schema_map import build_schema_map


def print_section(title, content=""):
     """Print formatted section"""
     print("\n" + "=" * 60)
     print(f"  {title}")
     print("=" * 60)
     if content:
         print(content)


def main():
     print("\n" + "🔍 " * 20)
     print("  NATURAL LANGUAGE TO SQL QUERY SYSTEM")
     print("  PostgreSQL Database Edition")
     print("🔍 " * 20)
    
     print("\nExample queries you can ask:")
     print("  • Top 5 products by sales")
     print("  • How many customers from USA?")
     print("  • Average product price by category")
     print("  • Total revenue by employee")
    
    
     question = input("\n💬 Ask your question: ")
    
     try:
         # STEP 1 — Parse text
         print_section("STEP 1: TEXT PARSING")
         parsed = parse_query(question)
        
         print(f"Original Query: {parsed.get('original_text')}")
         print(f"Detected Entity: {parsed.get('entity')}")
         print(f"Aggregation: {parsed.get('aggregation')}")
         print(f"Group By: {parsed.get('group_by')}")
         print(f"Limit: {parsed.get('limit')}")
         print(f"Filters: {parsed.get('filters')}")
         print(f"Order: {parsed.get('order_by')}")
        
         # STEP 2 — Schema linking
         print_section("STEP 2: SCHEMA LINKING")
         linked = link_schema(parsed, build_schema_map())
        
         print(f"Base Table: {linked.get('base_table')}")
         print(f"Metric Table: {linked.get('metric_table')}")
         print(f"Metric Column: {linked.get('metric_column')}")
         print(f"Group Table: {linked.get('group_table')}")
         print(f"Group Column: {linked.get('group_column')}")
         print(f"Required Joins: {len(linked.get('joins', []))} join(s)")
        
         # STEP 3 — Build SQL
         print_section("STEP 3: SQL GENERATION")
         sql = build_query(parsed, linked)
         print(sql)
        
         # STEP 4 — Validate SQL
         print_section("STEP 4: SQL VALIDATION")
         valid, msg = validate_query(sql)
         print(f"Status: {msg}")
        
         if not valid:
             print("\n❌ Query validation failed. Aborting.")
             return
        
         # STEP 5 — Execute SQL
         print_section("STEP 5: QUERY EXECUTION")
         print("Executing query against the active PostgreSQL database...")
        
         columns, rows = execute_query(sql)
         print(f"✅ Query executed successfully!")
         print(f"   Retrieved {len(rows)} row(s)")
        
         # STEP 6 — Visualize
         print_section("STEP 6: RESULTS")
         visualize_result(columns, rows)
        
         print("\n" + "✨ " * 20)
         print("  Query completed successfully!")
         print("✨ " * 20 + "\n")
        
     except Exception as e:
         print("\n" + "❌ " * 20)
         print(f"  ERROR: {str(e)}")
         print("❌ " * 20)
         print("\nTip: Try rephrasing your question or check the database connection.")
         import traceback
         print("\nDetailed error:")
         traceback.print_exc()


if __name__ == "__main__":
     main()
