import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def main():
    e = create_async_engine("postgresql+asyncpg://nexus:nexus-dev@localhost:5432/nexus")
    async with e.begin() as c:
        r = await c.execute(text("SELECT tenant_id FROM tenants LIMIT 10"))
        rows = r.fetchall()
        print("Tenants:", [row[0] for row in rows])
        if not rows:
            print("No tenants found! Inserting a default tenant...")
            await c.execute(text(
                "INSERT INTO tenants (tenant_id, name, created_at) "
                "VALUES ('t-1', 'Default Tenant', NOW()) ON CONFLICT DO NOTHING"
            ))
            print("Inserted t-1")
    await e.dispose()

asyncio.run(main())
