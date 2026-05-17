#!/bin/bash
# Side-by-side comparison: old artifact (bbfbf73d) vs new artifact ($NEW)
# Run with: NEW=<new-artifact-id> bash scripts/compare_artifacts.sh
OLD="bbfbf73d-dc01-43c5-9350-045e74cad1ed"
NEW="${NEW:?NEW=<artifact-id> required}"

echo "════════════════════════════════════════════════════"
echo "  OLD: $OLD"
echo "  NEW: $NEW"
echo "════════════════════════════════════════════════════"

echo
echo "─── Counts ────────────────────────────────────────"
docker exec nexus-postgres psql -U nexus -d nexus -c "
SELECT 'frames' AS metric,
       (SELECT COUNT(*) FROM visual_frames WHERE artifact_id='$OLD') AS old_v,
       (SELECT COUNT(*) FROM visual_frames WHERE artifact_id='$NEW') AS new_v
UNION ALL SELECT 'scenes',
       (SELECT COUNT(*) FROM visual_scenes WHERE artifact_id='$OLD'),
       (SELECT COUNT(*) FROM visual_scenes WHERE artifact_id='$NEW')
UNION ALL SELECT 'flows',
       (SELECT COUNT(*) FROM visual_flows WHERE artifact_id='$OLD'),
       (SELECT COUNT(*) FROM visual_flows WHERE artifact_id='$NEW')
UNION ALL SELECT 'edges',
       (SELECT COUNT(*) FROM visual_flow_edges WHERE artifact_id='$OLD'),
       (SELECT COUNT(*) FROM visual_flow_edges WHERE artifact_id='$NEW')
UNION ALL SELECT 'controls',
       (SELECT COUNT(*) FROM evidence_controls WHERE artifact_id='$OLD'),
       (SELECT COUNT(*) FROM evidence_controls WHERE artifact_id='$NEW');
" -P pager=off

echo
echo "─── Distinct descriptions per scene (proof per-frame LLaVA worked) ───"
docker exec nexus-postgres psql -U nexus -d nexus -c "
SELECT (SELECT scene_index FROM visual_scenes vs WHERE vs.scene_id=vf.scene_id LIMIT 1) AS scene_idx,
       COUNT(*) AS frame_count,
       COUNT(DISTINCT description) AS distinct_descriptions
FROM visual_frames vf
WHERE vf.artifact_id='$NEW'
GROUP BY vf.scene_id
ORDER BY scene_idx;
" -P pager=off | head -40

echo
echo "─── frame_actions populated in scene_state_summary ───"
docker exec nexus-postgres psql -U nexus -d nexus -c "
SELECT scene_index,
       (scene_state_summary->>'step_label') AS label,
       (scene_state_summary->>'frame_action_count')::int AS actions,
       (scene_state_summary->>'visit_count')::int AS visits
FROM visual_scenes
WHERE artifact_id='$NEW'
ORDER BY scene_index;
" -P pager=off
