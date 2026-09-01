"""Quick DB introspection script."""
import asyncio
import asyncpg

async def check():
    conn = await asyncpg.connect("postgresql://nexus:nexus-dev@localhost:5432/nexus")
    tables = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    print("Existing tables:")
    for t in tables:
        print(f"  {t['tablename']}")
    
    # Check alembic_version
    try:
        ver = await conn.fetch("SELECT version_num FROM alembic_version")
        print(f"\nAlembic version: {[v['version_num'] for v in ver]}")
    except Exception as e:
        print(f"\nNo alembic_version table: {e}")
    
    # Check if test_cases table exists
    tc = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'test_cases' ORDER BY ordinal_position"
    )
    if tc:
        print(f"\ntest_cases columns: {[c['column_name'] for c in tc]}")
    else:
        print("\ntest_cases table does NOT exist yet")
    
    await conn.close()

asyncio.run(check())
