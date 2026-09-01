#!/usr/bin/env python3
"""Idempotently add the GCP-KMS encryption env vars to platform-api's compose
block, inserted right after the RUNNER_TOKEN line (literal insert — no YAML
reformatting). Run with sudo on the VM."""
import sys

P = "/home/harik/nexus/Nexus_power/docker-compose.yml"
KEY = ("projects/project-1506cdce-a0b5-42e7-bdc/locations/asia-southeast1/"
       "keyRings/nexus-keyring/cryptoKeys/nexus-kek")

text = open(P).read()
if "NEXUS_KEK_PROVIDER" in text:
    print("ALREADY_PRESENT")
    sys.exit(0)

lines = text.splitlines(keepends=True)
out = []
done = False
for ln in lines:
    out.append(ln)
    if (not done) and "RUNNER_TOKEN:" in ln:
        indent = ln[: len(ln) - len(ln.lstrip())]
        out.append(f'{indent}NEXUS_KNOWLEDGE_FOUNDATION_ENABLED: "true"\n')
        out.append(f"{indent}NEXUS_KEK_PROVIDER: gcp_kms\n")
        out.append(f"{indent}NEXUS_KEK_GCP_KEY: {KEY}\n")
        done = True

if not done:
    print("ANCHOR_NOT_FOUND")
    sys.exit(1)

open(P, "w").write("".join(out))
print("INSERTED")
