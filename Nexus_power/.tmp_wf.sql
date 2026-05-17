with wf_rows as (
  select 'target_workflow' as scope, wi.workflow_id, wi.session_id, wi.status, wi.started_at, wi.completed_at, wi.error,
         coalesce(wi.input_data->>'processing_profile', wc.snapshot->>'processing_profile', s.metadata_json->>'processing_profile') as processing_profile,
         wi.stages as workflow_stages_json
  from workflow_instances wi
  left join workflow_contexts wc on wc.workflow_id = wi.workflow_id
  left join sessions s on s.session_id = wi.session_id
  where wi.workflow_id = 'c8e34eac-bc1a-44d8-a795-0063092422be'
  union all
  select 'recent_for_session' as scope, wi.workflow_id, wi.session_id, wi.status, wi.started_at, wi.completed_at, wi.error,
         coalesce(wi.input_data->>'processing_profile', wc.snapshot->>'processing_profile', s.metadata_json->>'processing_profile') as processing_profile,
         wi.stages as workflow_stages_json
  from workflow_instances wi
  left join workflow_contexts wc on wc.workflow_id = wi.workflow_id
  left join sessions s on s.session_id = wi.session_id
  where wi.session_id = '68de23b2-9fcc-47bb-ab60-66f1044dce10'
  order by wi.started_at desc nulls last
  limit 5
), stage_rows as (
  select ms.workflow_id,
         string_agg(concat(coalesce(ms.stage_number::text,'?'),':',coalesce(ms.stage_type,'?'),':',coalesce(ms.status,'?'),':start=',coalesce(to_char(ms.started_at,'YYYY-MM-DD HH24:MI:SSTZ'),'null'),':end=',coalesce(to_char(ms.completed_at,'YYYY-MM-DD HH24:MI:SSTZ'),'null'),':err=',coalesce(replace(ms.error_message,E'\n',' '),'') ), ' || ' order by ms.stage_number) as mission_stage_statuses
  from mission_stages ms
  where ms.workflow_id in (
    select workflow_id from workflow_instances where workflow_id = 'c8e34eac-bc1a-44d8-a795-0063092422be'
    union
    select workflow_id from workflow_instances where session_id = '68de23b2-9fcc-47bb-ab60-66f1044dce10' order by started_at desc nulls last limit 5
  )
  group by ms.workflow_id
)
select concat(
  'scope=',wr.scope,
  ' | workflow_id=',wr.workflow_id,
  ' | session_id=',coalesce(wr.session_id,''),
  ' | status=',coalesce(wr.status,''),
  ' | started_at=',coalesce(to_char(wr.started_at,'YYYY-MM-DD HH24:MI:SSTZ'),''),
  ' | completed_at=',coalesce(to_char(wr.completed_at,'YYYY-MM-DD HH24:MI:SSTZ'),''),
  ' | error=',coalesce(replace(wr.error,E'\n',' '),''),
  ' | processing_profile=',coalesce(wr.processing_profile,''),
  ' | mission_stage_statuses=',coalesce(sr.mission_stage_statuses,''),
  ' | workflow_stages_json=',coalesce(left(replace(wr.workflow_stages_json::text,E'\n',' '),500),'')
)
from wf_rows wr
left join stage_rows sr on sr.workflow_id = wr.workflow_id
order by case when wr.scope='target_workflow' then 0 else 1 end, wr.started_at desc nulls last;
