#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Does the DEPLOYED DATABASE carry the migrations this checkout ships?
#
# WHY THIS EXISTS. On 2026-09-04 the production qecentral database was found at
# qec_023 while the repository shipped qec_025. Two migrations had never run,
# so `journeys.criticality_band` and `catalog_questions.reveals` did not exist
# in production while the deployed CODE was written against them. Every crawl
# of a NEW application captured its questions correctly and then wrote no
# catalogue and no journeys at all:
#
#     OrangeHRM   57 questions captured -> 0 catalog_questions, 0 journeys
#     Summit Life 83 catalog_questions, 14 journeys   (written before the drift)
#
# The golden app looked healthy the whole time because its rows predate the
# divergence, which is why nothing went red.
#
# HOW IT WAS ABLE TO HAPPEN. `alembic upgrade head` appears in this repository
# exactly once — as a COMMENT in docker-compose.qec.yml describing a one-time
# bootstrap. No deploy has ever run it. So the schema advanced only when a human
# remembered, and the code advanced on every deploy.
#
# WHY test_schema_drift DID NOT CATCH IT. That test compares the MODELS to the
# MIGRATIONS. Both were correct and consistent at qec_025. Nobody compared the
# migrations to the database that is actually serving. Two green halves, one
# dead product.
#
# Exit codes:
#   0  IN SYNC     the deployed revision is the head this checkout ships
#   1  DRIFTED     the database is behind (or ahead of) this checkout
#   2  UNKNOWABLE  the revision could not be read - NOT a verdict about drift
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSIONS_DIR="${QEC_VERSIONS_DIR:-$HERE/../platform/qe-central/alembic_qec/versions}"
PG_CONTAINER="${QEC_PG_CONTAINER:-nexus-postgres}"
PG_USER="${QEC_PG_USER:-nexus}"
PG_DB="${QEC_PG_DB:-qecentral}"

say() { printf '%s\n' "$*"; }

# ── 1. What head does this checkout ship? ────────────────────────────────────
# The head is the revision that NO other migration names as its down_revision.
# Derived rather than hard-coded: a hard-coded head is a second thing to forget,
# and forgetting is the entire failure mode this gate exists for.
if [ ! -d "$VERSIONS_DIR" ]; then
  say "UNKNOWABLE - no migrations directory at $VERSIONS_DIR"
  say "SCHEMA_VERDICT=UNKNOWABLE"
  exit 2
fi

revs="$(grep -hoE '^revision(: str)? *= *"[^"]+"' "$VERSIONS_DIR"/*.py 2>/dev/null \
        | sed -E 's/.*"([^"]+)".*/\1/' | sort -u)"
downs="$(grep -hoE '^down_revision(: *Union\[str, *None\])? *= *"[^"]+"' "$VERSIONS_DIR"/*.py 2>/dev/null \
        | sed -E 's/.*"([^"]+)".*/\1/' | sort -u)"

if [ -z "$revs" ]; then
  say "UNKNOWABLE - no revisions parsed out of $VERSIONS_DIR"
  say "SCHEMA_VERDICT=UNKNOWABLE"
  exit 2
fi

repo_head="$(comm -23 <(printf '%s\n' "$revs") <(printf '%s\n' "$downs"))"
head_count="$(printf '%s\n' "$repo_head" | grep -c . || true)"
if [ "$head_count" != "1" ]; then
  # Two heads means an un-merged branch in the migration graph. That is a real
  # problem, but it is not THIS gate's verdict to give - and guessing which head
  # is "the" head would be worse than saying so.
  say "UNKNOWABLE - $head_count migration heads in this checkout:"
  printf '  %s\n' $repo_head
  say "SCHEMA_VERDICT=UNKNOWABLE"
  exit 2
fi

# ── 2. What revision is the DEPLOYED database actually at? ───────────────────
deployed="$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -t -A \
            -c 'select version_num from alembic_version' 2>/dev/null | tr -d '[:space:]')"

if [ -z "$deployed" ]; then
  # An unreachable database is an INFRASTRUCTURE fact. Reporting it as drift
  # would roll a deploy back for a container that was merely restarting.
  say "UNKNOWABLE - could not read alembic_version from $PG_CONTAINER/$PG_DB"
  say "SCHEMA_VERDICT=UNKNOWABLE"
  exit 2
fi

# ── 3. Adjudicate ────────────────────────────────────────────────────────────
say "  repo head : $repo_head"
say "  deployed  : $deployed"

if [ "$deployed" = "$repo_head" ]; then
  say "SCHEMA IN SYNC - the serving database carries every migration this build ships."
  say "SCHEMA_VERDICT=IN_SYNC"
  exit 0
fi

say ""
say "SCHEMA DRIFT - the deployed database is NOT at this build's head."
say "The code in this build was written against $repo_head; the database is at $deployed."
say "Columns this build reads may not exist, and the symptom is SILENT: rows are"
say "captured and then never written, exactly as the catalogue died on 2026-09-04."
say ""
say "Apply the missing migrations with:"
say "  docker compose --env-file <env> -f docker-compose.qec.yml run --rm --no-deps \\"
say "      qe-central 'alembic -c alembic_qec/alembic.ini upgrade head'"
say "SCHEMA_VERDICT=DRIFTED"
exit 1
