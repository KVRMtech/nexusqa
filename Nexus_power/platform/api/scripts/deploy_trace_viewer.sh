#!/usr/bin/env bash
# Self-host the Playwright trace viewer in the verdict portal at /trace/.
#
# WHY SELF-HOST: replay-from-step is the single richest piece of evidence we
# have, and an on-prem/air-gapped customer must not be told to upload their
# failure trace to trace.playwright.dev. The viewer is ~1.4 MB of static files
# that already ship inside the runner image, so we copy them across at deploy
# time — no new dependency, no network egress, no CDN.
#
# Re-run after upgrading Playwright in the runner so the viewer matches the
# trace format it produced.
set -euo pipefail

RUNNER=${RUNNER:-nexus-runner}
PORTAL=${PORTAL:-nexus-verdict-portal}
SRC=/opt/runner/node_modules/playwright-core/lib/vite/traceViewer
TMP=$(mktemp -d)

echo "→ copying the trace viewer out of ${RUNNER}"
sudo docker cp "${RUNNER}:${SRC}" "${TMP}/traceViewer"

echo "→ publishing to ${PORTAL}:/usr/share/nginx/html/trace"
sudo docker exec "${PORTAL}" sh -c "rm -rf /usr/share/nginx/html/trace"
sudo docker cp "${TMP}/traceViewer" "${PORTAL}:/usr/share/nginx/html/trace"

echo "→ verifying"
sudo docker exec "${PORTAL}" sh -c "test -f /usr/share/nginx/html/trace/index.html \
  && test -f /usr/share/nginx/html/trace/sw.bundle.js \
  && echo 'trace viewer OK (index.html + service worker present)'"
rm -rf "${TMP}"
