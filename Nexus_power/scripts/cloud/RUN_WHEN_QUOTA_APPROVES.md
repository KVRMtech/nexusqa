# When GCP GPU quota approves — runbook

Total time: ~45-60 min (mostly waiting on Cloud SQL + Docker builds + Ollama model pull).

## Pre-reqs (already done in this session)

- ✅ GCP project created: `project-1506cdce-a0b5-42e7-bdc`
- ✅ Billing upgraded from free trial
- ✅ Code pushed to private GitHub repo (`nexus-power-snapshot`)
- ⏳ **GPU quota** — waiting on GCP support response

## Step 1 — Push your laptop's latest code to GitHub (if any changes since the snapshot)

```bash
cd /c/Users/harik/nexusqa
git push runpod runpod-snapshot:main --force
```

This is only needed if you edited code locally after the snapshot. Skip if not.

## Step 2 — Set up GitHub deploy key for the VM

The VM needs an SSH deploy key to clone your private repo. **Use a repo-scoped
deploy key, NOT your account-level key** — minimum-privilege.

In **Cloud Shell** (not your laptop):

```bash
# Generate a deploy key on Cloud Shell (or reuse if already done)
[ -f ~/.ssh/runpod_deploy ] || ssh-keygen -t ed25519 -f ~/.ssh/runpod_deploy -N "" -C "gcp-vm-deploy"

# Print the public half — copy this output
cat ~/.ssh/runpod_deploy.pub
```

Then in your browser:
1. Open https://github.com/YOUR_GH_USER/nexus-power-snapshot/settings/keys
2. Click **"Add deploy key"**
3. Title: `gcp-vm-deploy`
4. Key: paste the line you copied from `cat ~/.ssh/runpod_deploy.pub`
5. **Leave "Allow write access" UNCHECKED** (read-only is enough)
6. Save

## Step 3 — Upload provision_gcp.sh + deploy_on_vm.sh to Cloud Shell

Two ways:

**A. Upload via Cloud Shell's three-dot menu → Upload** — pick both files from
   `Nexus_power/scripts/cloud/`.

**B. Or git-clone them inside Cloud Shell:**

```bash
git clone git@github.com:YOUR_GH_USER/nexus-power-snapshot.git
cp nexus-power-snapshot/Nexus_power/scripts/cloud/*.sh ~/
```

## Step 4 — Provision

```bash
chmod +x ~/provision_gcp.sh ~/deploy_on_vm.sh
PROJECT_ID=project-1506cdce-a0b5-42e7-bdc ~/provision_gcp.sh
```

Takes ~10 min. Prints `VM_IP`, `DB_IP`, `REDIS_IP`, `DB_PASSWORD` at the end.

**If you get `Quota exceeded` again** — quota didn't actually approve; reply to
the GCP support thread and confirm. The script exits with code 2.

**If you get `out of stock` everywhere** — re-run after 30-60 min. T4 capacity
rotates fast. Or try forcing L4 with `GPU_TYPE=nvidia-l4 ~/provision_gcp.sh`
(it's the default but explicit is fine).

## Step 5 — Deploy (uses values printed by step 4)

```bash
GH_USER=YOUR_GH_USER ~/deploy_on_vm.sh \
  nexus-vm us-central1-a \  # ← use zone from step 4 output
  <DB_IP> <REDIS_IP> \
  project-1506cdce-a0b5-42e7-bdc-nexus-artifacts \
  <DB_PASSWORD>
```

This:
- scp's `.env` + deploy key to the VM
- SSHs into the VM, installs Docker + NVIDIA toolkit
- Clones the repo, brings up docker compose
- Pulls LLaVA into Ollama

30-60 min, output streams live.

## Step 6 — Smoke test

```bash
# Final summary will print the VM IP. From your laptop OR Cloud Shell:
curl http://<VM_IP>:8100/health   # should return 200

# Open the client UI:
echo "http://<VM_IP>:3000"
```

Then upload a real-audio video. Expected behavior on GPU:
- OCR per workflow: ~15-30s (was 380s on CPU)
- analyze_scenes: real LLaVA completion, no circuit-breaker trip
- Total wall time: ~2-3 min (was ~9 min on CPU)
- Quality_gate outcome: mostly `pass` (clean), not `pass_with_warnings`

## Cost reminders

| Action | Cost |
|---|---|
| VM + GPU running | ~$0.95/hr (always-on) |
| Stop VM | `gcloud compute instances stop nexus-vm --zone us-central1-a` — drops cost to $0 (only SSD ~$0.05/hr) |
| Cloud SQL + Redis idle | ~$2/day even with VM stopped |
| Delete everything | run `teardown_gcp.sh` (TODO: write when needed) |

Your $300 credit covers ~12 days always-on or ~75 days "few hours per day".
