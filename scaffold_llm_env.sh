#!/usr/bin/env bash
# Scaffold the LLM tier config into Nexus_power/.env WITHOUT the secret.
# Idempotent: preserves any existing vars (e.g. QEC_REAPER_TICK_SECONDS) and leaves
# the API_KEY line EMPTY for the founder to fill by hand (key never transits the agent).
set -uo pipefail
cd /home/srika/nexus-src/Nexus_power || { echo "NO_REPO"; exit 1; }
touch .env && chmod 600 .env

# Default to Anthropic; change PROVIDER/BASE_URL/MODEL below if using OpenAI-compatible.
declare -A KV=(
  [LLM_TIERS]=tier_fast
  [LLM_DEFAULT_TIER]=tier_fast
  [LLM_TIER_TIER_FAST_PROVIDER]=anthropic
  [LLM_TIER_TIER_FAST_BASE_URL]=https://api.anthropic.com
  [LLM_TIER_TIER_FAST_MODEL]=claude-opus-4-8
)
for k in "${!KV[@]}"; do
  v="${KV[$k]}"
  if grep -qE "^${k}=" .env; then
    sed -i "s|^${k}=.*|${k}=${v}|" .env
  else
    echo "${k}=${v}" >> .env
  fi
done
# The KEY line: only ADD it empty if absent — never overwrite a key the founder pasted.
if ! grep -qE '^LLM_TIER_TIER_FAST_API_KEY=' .env; then
  echo 'LLM_TIER_TIER_FAST_API_KEY=' >> .env
fi

echo "=== .env LLM lines (key value hidden) ==="
grep -E '^(LLM_TIERS|LLM_DEFAULT_TIER|LLM_TIER_TIER_FAST_(PROVIDER|BASE_URL|MODEL))=' .env
if grep -qE '^LLM_TIER_TIER_FAST_API_KEY=.+' .env; then
  echo "LLM_TIER_TIER_FAST_API_KEY=PRESENT(non-empty)"
else
  echo "LLM_TIER_TIER_FAST_API_KEY=EMPTY  <-- paste your key here:"
  echo "    sed -i 's#^LLM_TIER_TIER_FAST_API_KEY=.*#LLM_TIER_TIER_FAST_API_KEY=YOUR_KEY#' ~/nexus-src/Nexus_power/.env"
fi
echo "SCAFFOLD_DONE"
