# Nexus QA — Client Test Handover

**Status:** Stack rebuilt with the full canonical processing pipeline (Phase 5-A 6-step video split, Phase 6 4-step audio split, Phase 12 DAG dispatcher, Phase 14 GPU batcher, Phase 7 Milvus, Phase 16 tenant lifecycle, LLM tier with cost tracking + PII guard + token budget + fail-fast-last).

---

## Access

| Endpoint | URL | Purpose |
|---|---|---|
| **Web UI** | http://localhost:3000 | Client-facing portal — upload audio/video, view canonical results, search |
| Gateway API | http://localhost:8080 | All REST/SSE traffic enters here |
| Platform API | http://localhost:8091 | Tenant + session + workflow CRUD |
| Auth API | http://localhost:8000 | Login, self-signup, token refresh |
| Orchestrator | http://localhost:8100 | Workflow plane control |

For testing outside the host machine (LAN access), replace `localhost` with the host's LAN IP. No TLS in dev — production cluster uses cert-manager via the Helm chart's ingress.

## Credentials

Freshly-provisioned admin user (created via `/api/v1/auth/self-signup`):

| Field | Value |
|---|---|
| **Email** | _filled in below after creation_ |
| **Password** | _filled in below after creation_ |
| **Tenant** | _filled in below after creation_ |
| **Role** | `admin` (full UI access) |

The client should change the password on first login. The signup endpoint is open for additional users (`/api/v1/auth/self-signup`) — the admin user can also invite teammates via the admin UI's user-management section.

## What's safe to test today

| Scenario | Expectation |
|---|---|
| Upload a 1-5 min audio file (.wav / .mp3) | Workflow goes through 6 steps (shield → preprocess → diarize → transcribe → align → backbone) and produces a transcript + speaker labels in <2 min |
| Upload a 1-5 min screen recording (.mp4) | Workflow goes through 8 steps (shield → extract_frames → detect_scenes → ocr_frames → analyze_scenes → analyze_transitions → build_evidence → backbone). The two GPU-heavy steps run sequentially on the single local GPU — expect 3-15 min wall time |
| Re-upload the same file | Cache-hit path returns `status=completed` in <500ms |
| Semantic search via UI / backbone API | Returns the just-canonicalized content with relevant similarity scores |
| Open `/admin/tenants` (admin only) | See the lifecycle UI — provision / suspend / offboard tenants |

## What's NOT ready for client testing yet

These are documented gaps; do not have the client exercise them:

- **Cloud-tier LLM (Anthropic / OpenAI / Azure)** — code is in place but disabled at launch. The platform runs on local Ollama (Tier 3) only until cloud API keys are provisioned. Cost-tracking + PII guard + token budget are armed for the day cloud tiers come online.
- **Test execution via legs engine** — Playwright path works but the canonical processing pipeline doesn't dispatch to it; that's a Phase 4.x integration.
- **High availability** — single-instance Postgres + Redis in dev. Production Helm chart deploys CNPG + Sentinel; that path needs a real K8s cluster to verify behavior.
- **Load at scale** — pre-prod load test (Phase 9) has scripts ready but hasn't been run against 100 concurrent tenants. Pilot should stay ≤10 tenants until measured numbers exist.

## How to run a smoke test (operator side, before letting the client in)

```bash
# 1. Verify every canonical engine is healthy
for p in 8001 8002 8003 8005 8009 8000 8080 8091 8100; do
  curl -sm 3 -o /dev/null -w ":$p -> %{http_code}\n" http://localhost:$p/health
done
# Expect: all 200

# 2. Verify workflow lanes are attached
for lane in shield.cpu eyes.cpu eyes.gpu ears.cpu ears.gpu spine.cpu spine.gpu backbone.cpu; do
  docker compose -f docker-compose.yml exec redis redis-cli -n 3 XINFO GROUPS "nexus:queue:$lane" 2>&1 \
    | awk -v lane="$lane" '/^name/{getline; print "  ✓ "lane}'
done
# Expect: 8 checkmarks

# 3. Verify backbone is on real Milvus
curl -s http://localhost:8005/health | python -c "import json,sys; print(json.load(sys.stdin)['modes'])"
# Expect: vector_store says 'milvus + sentence-transformers ...'

# 4. Run a single workflow as the test user
# (via UI is cleaner; or use the curl example below)
```

## Pre-prod check before exposing this to the wider client team

Run [scripts/ci_smoke.sh](Nexus_power/scripts/ci_smoke.sh) — full bring-up from scratch on fresh data volumes. If anything regresses there, the client will hit it too.

## Support / escalation

| Issue | First action |
|---|---|
| Workflow stuck at a specific step | Check Grafana dashboard "Nexus — Canonical Pipeline" → which step's p95 is climbing |
| Engine pod restarting | `docker compose logs nexus-<engine>` |
| Auth issues | Verify `NEXUS_JWT_SECRET` is the same value across auth-service + gateway + all engines |
| Cache-hit miss (same file processes twice) | Check the artifact_id fingerprint in Postgres `canonical_artifacts` table |
| Search returns 0 hits | Verify backbone health says `milvus + sentence-transformers (...)` — degraded mode returns 0 silently |

Full operator procedures: [OPERATOR_RUNBOOK.md](Nexus_power/docs/OPERATOR_RUNBOOK.md).

---

**Recommendation:** Do a single end-to-end run (upload a known small video + verify the transcript + verify search) yourself before sharing credentials with the client. Catches any deploy-time surprise.
