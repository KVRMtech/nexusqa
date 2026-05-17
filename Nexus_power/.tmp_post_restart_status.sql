with target_wf as (
  select wi.workflow_id,
         wi.session_id,
         wi.status,
         wi.started_at,
         wi.completed_at,
         wi.error,
         wi.input_data->>'processing_profile' as processing_profile
  from workflow_instances wi
  where wi.workflow_id = 'c8e34eac-bc1a-44d8-a795-0063092422be'
), target_stages as (
  select ms.workflow_id,
         ms.stage_number,
         ms.stage_type,
         ms.status,
         ms.started_at,
         ms.completed_at,
         ms.error_message
  from mission_stages ms
  where ms.workflow_id = 'c8e34eac-bc1a-44d8-a795-0063092422be'
    and ms.stage_type in ('audio_transcription','visual_extraction','artifact_persistence','canonical_quality_gate')
), newer_session_wf as (
  select wi.workflow_id,
         wi.session_id,
         wi.status,
         wi.started_at,
         wi.completed_at,
         wi.error,
         wi.input_data->>'processing_profile' as processing_profile
  from workflow_instances wi
  where wi.session_id = '68de23b2-9fcc-47bb-ab60-66f1044dce10'
    and wi.started_at > timestamp with time zone '2026-04-20 13:58:50+00'
  order by wi.started_at
)
select concat(
  'TARGET | workflow_id=', tw.workflow_id,
  ' | session_id=', coalesce(tw.session_id,''),
  ' | status=', coalesce(tw.status,''),
  ' | started_at=', coalesce(to_char(tw.started_at,'YYYY-MM-DD HH24:MI:SSOF'),''),
  ' | completed_at=', coalesce(to_char(tw.completed_at,'YYYY-MM-DD HH24:MI:SSOF'),''),
  ' | error=', coalesce(replace(tw.error,E'\n',' '),''),
  ' | processing_profile=', coalesce(tw.processing_profile,'')
) from target_wf tw
union all
select concat(
  'STAGE | workflow_id=', ts.workflow_id,
  ' | stage_number=', coalesce(ts.stage_number::text,''),
  ' | stage_type=', coalesce(ts.stage_type,''),
  ' | status=', coalesce(ts.status,''),
  ' | started_at=', coalesce(to_char(ts.started_at,'YYYY-MM-DD HH24:MI:SSOF'),''),
  ' | completed_at=', coalesce(to_char(ts.completed_at,'YYYY-MM-DD HH24:MI:SSOF'),''),
  ' | error=', coalesce(replace(ts.error_message,E'\n',' '),'')
) from target_stages ts
union all
select concat(
  'NEWER | workflow_id=', nw.workflow_id,
  ' | session_id=', coalesce(nw.session_id,''),
  ' | status=', coalesce(nw.status,''),
  ' | started_at=', coalesce(to_char(nw.started_at,'YYYY-MM-DD HH24:MI:SSOF'),''),
  ' | completed_at=', coalesce(to_char(nw.completed_at,'YYYY-MM-DD HH24:MI:SSOF'),''),
  ' | error=', coalesce(replace(nw.error,E'\n',' '),''),
  ' | processing_profile=', coalesce(nw.processing_profile,'')
) from newer_session_wf nw;
