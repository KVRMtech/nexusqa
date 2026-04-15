import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def main():
    e = create_async_engine("postgresql+asyncpg://nexus:nexus-dev@localhost:5432/nexus")
    async with e.begin() as c:
        # Clean up stale test cases from previous runs
        r = await c.execute(text("DELETE FROM test_cases WHERE tenant_id = 'nexus-platform'"))
        print(f"Cleaned up {r.rowcount} stale test cases")
    await e.dispose()

asyncio.run(main())
