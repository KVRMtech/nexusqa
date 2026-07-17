#!/usr/bin/env bash
set -uo pipefail
docker exec -i nexus-postgres psql -U nexus -d qecentral -t -A -F'|' <<SQL
select tenant_id, left(name,30), onboarding_state from client_apps where app_id='bb03329f-6868-4ea0-8060-fd5d816aca69';
SQL
echo "--- all tenants with apps ---"
docker exec -i nexus-postgres psql -U nexus -d qecentral -t -A -F'|' <<SQL
select tenant_id, count(*) from client_apps group by tenant_id order by 2 desc;
SQL
