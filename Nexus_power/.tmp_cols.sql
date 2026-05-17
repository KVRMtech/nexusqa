select table_name||'.'||column_name||':'||data_type as c
from information_schema.columns
where table_schema='public'
  and table_name in ('workflow_instances','workflow_contexts','mission_stages','sessions','media_processing_jobs','review_queue','traces')
order by table_name, ordinal_position;
