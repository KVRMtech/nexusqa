#!/bin/bash
# Smoke test for the orchestrator's auto audio-extract path.
#
# Usage: bash scripts/verify_audio_extraction.sh
# Requires: orchestrator container rebuilt + restarted, ffmpeg in $PATH inside container.
set -e

echo "═══ Step 1: ffmpeg present in orchestrator ═══"
docker exec nexus-orchestrator ffmpeg -version 2>&1 | head -1

echo
echo "═══ Step 2: extract function importable ═══"
docker exec nexus-orchestrator python -c "
from app.main import _extract_audio_from_video
print('symbol:', _extract_audio_from_video.__module__, '.', _extract_audio_from_video.__name__)
print('docstring (first line):', (_extract_audio_from_video.__doc__ or '').split(chr(10))[0])
"

echo
echo "═══ Step 3: end-to-end via /process upload ═══"
echo "(skipped automatically — run a UI upload to verify the full path)"

echo
echo "═══ Step 4: check workflow input_data for new uploads ═══"
docker exec nexus-postgres psql -U nexus -d nexus -c "
SELECT
  workflow_id,
  input_data->>'video_file_id' AS video_id,
  input_data->>'audio_file_id' AS audio_id,
  CASE
    WHEN input_data->>'audio_file_id' IS NOT NULL
     AND input_data->>'video_file_id' IS NOT NULL THEN 'BOTH (good)'
    WHEN input_data->>'video_file_id' IS NOT NULL THEN 'VIDEO ONLY (audio-extract did NOT fire)'
    ELSE 'OTHER'
  END AS state
FROM workflow_instances
WHERE chain_id = 'nexus.canonical-processing'
ORDER BY started_at DESC
LIMIT 5;
" -P pager=off
