"""One-shot: split multi-statement DROP+CREATE POLICY DDL in migrations
so asyncpg's single-statement-prepare driver accepts each op.execute()
call. Idempotent — pattern won't match if file is already split."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "alembic" / "versions"
FILES = [
    "020_knowledge_substrate.py",
    "021_echo_mvp.py",
    "022_e2e_scenario_state.py",
    "022_knowledge_cards.py",
    "023_product_atlas.py",
    "023_test_runs.py",
    "024_org_awareness.py",
    "025_action_layer.py",
    "026_marketplace_and_sovereign.py",
]

PAT = re.compile(
    r'op\.execute\(\s*\n'
    r'\s+f"""\s*\n'
    r'\s+DROP POLICY IF EXISTS tenant_isolation ON \{table\};\s*\n'
    r'\s+CREATE POLICY tenant_isolation ON \{table\}\s*\n'
    r"\s+USING \(tenant_id = current_setting\('nexus\.current_tenant_id', true\)\)\s*\n"
    r"\s+WITH CHECK \(tenant_id = current_setting\('nexus\.current_tenant_id', true\)\);\s*\n"
    r'\s+"""\s*\n'
    r'\s+\)',
    re.MULTILINE,
)

REPL = (
    'op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")\n'
    '        op.execute(\n'
    '            f"""\n'
    '            CREATE POLICY tenant_isolation ON {table}\n'
    "                USING (tenant_id = current_setting('nexus.current_tenant_id', true))\n"
    "                WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));\n"
    '            """\n'
    '        )'
)

for fn in FILES:
    p = ROOT / fn
    src = p.read_text(encoding="utf-8")
    new, n = PAT.subn(REPL, src)
    if n == 0:
        print(f"SKIP  {fn}: pattern not matched")
    else:
        p.write_text(new, encoding="utf-8")
        print(f"OK    {fn}: {n} block(s) split")
