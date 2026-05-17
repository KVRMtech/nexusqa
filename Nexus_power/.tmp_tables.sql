select table_schema||'.'||table_name as t
from information_schema.tables
where table_schema not in ('pg_catalog','information_schema')
order by 1;
