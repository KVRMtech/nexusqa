// Helper: mint per-tenant JWTs for the canonical pipeline load tests.
//
// The platform-api JWT validator expects: sub, email, tenant_id, role.
// We sign HS256 with the shared NEXUS_JWT_SECRET. This is fine for load
// testing against pre-prod; do NOT use these tokens against production.
//
// Used by the k6_canonical_* scripts via `init()` to allocate one token
// per virtual user.

import encoding from 'k6/encoding';
import crypto from 'k6/crypto';

/** Mint a JWT for tenant N. Returns the compact serialization. */
export function mintTenantJWT(secret, tenantIndex, ttlSeconds = 3600) {
  const now = Math.floor(Date.now() / 1000);
  const header = { alg: 'HS256', typ: 'JWT' };
  const payload = {
    sub: `loadtest-user-${tenantIndex}`,
    email: `loadtest-${tenantIndex}@nexus.test`,
    tenant_id: `loadtest-tenant-${tenantIndex}`,
    role: 'admin',
    iat: now,
    exp: now + ttlSeconds,
  };

  const b64url = (obj) =>
    encoding
      .b64encode(JSON.stringify(obj), 'rawurl');

  const signingInput = `${b64url(header)}.${b64url(payload)}`;
  const sig = crypto.hmac('sha256', secret, signingInput, 'base64rawurl');
  return `${signingInput}.${sig}`;
}

/** Allocate N tokens — one per virtual user. */
export function mintBatch(secret, count, ttlSeconds = 3600) {
  const out = [];
  for (let i = 0; i < count; i++) {
    out.push({
      tenant_id: `loadtest-tenant-${i}`,
      token: mintTenantJWT(secret, i, ttlSeconds),
    });
  }
  return out;
}
