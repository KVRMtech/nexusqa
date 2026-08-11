/**
 * ONBOARDING WIZARD — the 6-bucket intake (Access · Code · Data · Answers ·
 * Safety · Ops) that registers a client app via POST /apps. Credentials are
 * envelope-encrypted server-side and never echoed; the Safety bucket carries the
 * non-prod attestation the crawl gate is fail-closed on.
 *
 * Scaffold note: complete + running (submits through the typed client). The
 * `onboarding` feature agent owns richer validation / preflight. Export
 * `OnboardingWizard`.
 */
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api as factoryApi } from '../../studio/factoryApi';
import { toast } from 'sonner';
import {
  ClipboardCheck,
  Database,
  GitBranch,
  KeyRound,
  Layers,
  Plus,
  Radio,
  Settings,
  ShieldCheck,
  Trash2,
  X,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { api, QecApiError } from '../../lib/api';
import { cn } from '../../lib/format';
import { Button, Panel, Seal } from '../../components';
import type { AppCreatePayload, EnvKind, EnvProfileCreatePayload } from '../../types/qec';

interface WizardForm {
  name: string;
  base_url: string;
  username: string;
  password: string;
  mfa_method: 'none' | 'totp' | 'otp' | 'hook';
  mfa_secret: string;
  mfa_delivery: string;
  repo_provider: string;
  repo_project: string;
  webhook_secret: string;
  seed_notes: string;
  seed_fields: string;
  answers: string;
  env_kind: EnvKind;
  attested_by: string;
  reset_procedure: string;
  roe_signed: boolean;
  attestation_days: string;
  preflight_passed: boolean;
  allowed_hosts: string;
  allow_submit: boolean;
  cadence: string;
  usd_per_cycle: string;
  /** Multi-env: Environment Profiles (crawl-once / run-many) — STRUCTURED drafts,
   *  each compiled to an EnvCreate payload on submit (see `envDraftToPayload`). */
  environments: EnvDraft[];
  /** Multi-env: which authored environment (by name) every cycle runs against.
   *  '' = the app base URL (single-env). Applied via a PATCH after the profiles
   *  are created, so server-side validation sees the existing profile. */
  run_environment: string;
  /** Crawl scope (Target mode, R3 Mode 2): path prefixes the crawl is CONFINED
   *  to (one per line / comma-separated, e.g. "/quote"). Compiled to
   *  schedule.scope_paths on submit. BLANK ⇒ Explore mode (whole-app crawl). */
  scope_paths: string;
  /** DATA dial: 'user' (default) = you supply the values and the crawl names what
   *  it could not fill; 'agent' = a coherent fictional person answers every field
   *  it honestly can. Compiled to schedule.data_mode; 'user' is written as ABSENT
   *  so an app nobody configured keeps the pre-agent behaviour. */
  data_mode: 'user' | 'agent';
  /** SCOPE dial. 'explore' / 'target' / 'e2e'. Only 'e2e' is stored on the app;
   *  the other two stay derivable from scope_paths, so an app configured before
   *  this key existed keeps behaving exactly the same way. */
  crawl_mode: 'explore' | 'target' | 'e2e';
}

/** The section a Base URL already points at — so Target confirms a scope instead
 *  of asking the operator to retype one they have already given us. */
function entryPathOf(baseUrl: string): string {
  try {
    const p = new URL(baseUrl).pathname.replace(/\/+$/, '');
    return p || '/';
  } catch {
    return '/';
  }
}

/** The server refuses a Target crawl whose Base URL enters outside the scope: it
 *  would start out-of-scope and capture nothing while reporting success. Catching
 *  it in the form means the operator fixes it before dispatch, not after a 422. */
function entryOutOfScope(baseUrl: string, scopeText: string): boolean {
  const entry = entryPathOf(baseUrl);
  const paths = scopeText
    .split(/[\s,]+/)
    .map((p) => p.trim())
    .filter(Boolean)
    .map((p) => (p.startsWith('/') ? p : `/${p}`));
  if (!paths.length) return false;
  return !paths.some((p) => entry === p || entry.startsWith(p.endsWith('/') ? p : `${p}/`));
}

function WizardRadio({
  checked, onSelect, disabled, title, note,
}: {
  checked: boolean; onSelect: () => void; disabled?: boolean; title: string; note: string;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={checked}
      disabled={disabled}
      onClick={onSelect}
      className={cn(
        'w-full text-left flex items-start gap-2.5 rounded-lg px-2.5 py-2 ring-1 transition-colors',
        checked ? 'bg-teal/10 ring-teal/50' : 'bg-panel ring-line hover:ring-ink/20',
        disabled && 'opacity-55 cursor-not-allowed hover:ring-line',
      )}
    >
      <span
        aria-hidden
        className={cn(
          'mt-0.5 h-3.5 w-3.5 shrink-0 rounded-full ring-2 transition-colors',
          checked ? 'ring-teal bg-teal/70' : 'ring-ink-faint bg-transparent',
        )}
      />
      <span className="min-w-0">
        <span className="block text-xs font-semibold text-ink">{title}</span>
        <span className="block text-2xs text-ink-faint leading-snug">{note}</span>
      </span>
    </button>
  );
}

const EMPTY: WizardForm = {
  name: '',
  base_url: '',
  username: '',
  password: '',
  mfa_method: 'none',
  mfa_secret: '',
  mfa_delivery: '',
  repo_provider: 'gitlab',
  repo_project: '',
  webhook_secret: '',
  seed_notes: '',
  seed_fields: '',
  answers: '',
  env_kind: 'disposable',
  attested_by: '',
  reset_procedure: '',
  roe_signed: false,
  attestation_days: '30',
  preflight_passed: false,
  allowed_hosts: '',
  allow_submit: false,
  cadence: 'on_push',
  usd_per_cycle: '',
  environments: [],
  run_environment: '',
  scope_paths: '',
  data_mode: 'user',
  crawl_mode: 'explore',
};

interface Bucket {
  key: string;
  label: string;
  icon: LucideIcon;
  hint: string;
}

const BUCKETS: Bucket[] = [
  { key: 'access', label: 'Access', icon: KeyRound, hint: 'Where the app lives and how to sign in.' },
  { key: 'code', label: 'Code', icon: GitBranch, hint: 'The repo whose pushes trigger regression.' },
  { key: 'data', label: 'Data', icon: Database, hint: 'Seed data the crawler feeds the app.' },
  { key: 'answers', label: 'Answers', icon: ClipboardCheck, hint: 'The ground-truth answer key (oracles).' },
  { key: 'safety', label: 'Safety', icon: ShieldCheck, hint: 'The non-prod attestation + egress fences.' },
  { key: 'envs', label: 'Environments', icon: Layers, hint: 'The same flow, run against dev/test/uat/prod.' },
  { key: 'ops', label: 'Ops', icon: Settings, hint: 'Cadence and per-cycle budget.' },
];

// ── field primitives ─────────────────────────────────────────────────────────

function Field({
  label,
  children,
  hint,
  required,
}: {
  label: string;
  children: React.ReactNode;
  hint?: string;
  required?: boolean;
}) {
  return (
    <label className="block">
      <span className="block text-2xs font-semibold text-ink-mid mb-1">
        {label}
        {required && <span className="text-crit ml-1">*</span>}
      </span>
      {children}
      {hint && <span className="block text-2xs text-ink-faint mt-1">{hint}</span>}
    </label>
  );
}

const INPUT_CLS =
  'w-full rounded-lg bg-inset text-ink text-sm ring-1 ring-line focus-visible:ring-teal/60 px-3 py-2 placeholder:text-ink-faint';

// ── Environment Profiles (multi-env: crawl-once / run-many) ───────────────────
// The SAME learned flow, rebound at RUN time to dev/test/uat/prod. Each profile
// carries the env SELECTOR (routing cookies / headers / basic-auth + optional
// base-URL override) and the HARD env-pin that PROVES the run actually landed on
// that env (mismatch → RED).
//
// When a run is BOUND to this profile (the app's run-environment, or a run
// targeting it directly) the runner applies every field: cookies → storageState,
// headers → use.extraHTTPHeaders, basic-auth → use.httpCredentials (via the
// self-detecting vkpower.env.json sidecar), base-URL → NEXUS_BASE_URL, env-pin →
// a compiled hard assertion (mismatch → RED). A profile that is never bound to a
// run does nothing — nothing here is silently applied behind the operator's back.
// Secret material (basic-auth) is sealed server-side (AAD=environment_id), never echoed.

interface CookieDraft {
  name: string;
  value: string;
  domain: string;
  path: string;
}

interface HeaderDraft {
  name: string;
  value: string;
}

/** Which HARD proof pins a run to this env. An un-pinned run cannot prove it hit
 *  the right environment — allowed only when the base URL alone is unambiguous. */
type EnvPinKind = 'banner' | 'url' | 'none';

interface EnvDraft {
  name: string;
  env_kind: EnvKind;
  base_url: string;
  cookies: CookieDraft[];
  headers: HeaderDraft[];
  /** HTTP basic-auth (sealed at rest). Both blank ⇒ no basic-auth. */
  basic_auth_user: string;
  basic_auth_pass: string;
  pin_kind: EnvPinKind;
  pin_selector: string;
  pin_expect_text: string;
  pin_url_pattern: string;
  allow_submit: boolean;
}

const BLANK_COOKIE: CookieDraft = { name: '', value: '', domain: '', path: '/' };
const BLANK_HEADER: HeaderDraft = { name: '', value: '' };

const blankEnv = (): EnvDraft => ({
  name: '',
  env_kind: 'staging',
  base_url: '',
  cookies: [],
  headers: [],
  basic_auth_user: '',
  basic_auth_pass: '',
  pin_kind: 'banner',
  pin_selector: '',
  pin_expect_text: '',
  pin_url_pattern: '',
  allow_submit: false,
});

/** Absolute http(s) URL? Kept in LOCKSTEP with the server's `_validated_base_url`
 *  (Python urlparse): require the `//` authority explicitly, because WHATWG
 *  `new URL()` alone accepts scheme-only strings (e.g. `https:example.com`) that
 *  urlparse rejects — a mismatch would let a bad override pass here and 422 there,
 *  orphaning the env after the app is already created. */
function isHttpUrl(url: string): boolean {
  const s = url.trim();
  if (!/^https?:\/\//i.test(s)) return false;
  try {
    const u = new URL(s);
    return (u.protocol === 'http:' || u.protocol === 'https:') && !!u.hostname;
  } catch {
    return false;
  }
}

/** The app's host — the sensible default `domain` for a new routing cookie. */
function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return '';
  }
}

/** Compile a draft to the server `EnvCreate` payload, or null when it has no name
 *  (blank rows are dropped). Emits a COMPLETE env_assertion only (the server 422s
 *  a partial one), and scopes `allow_submit` to disposable envs — prod/staging
 *  never mutate. */
function envDraftToPayload(e: EnvDraft): EnvProfileCreatePayload | null {
  const name = e.name.trim();
  if (!name) return null;

  const cookies = e.cookies
    .filter((c) => c.name.trim() && c.value.trim())
    .map((c) => {
      const cookie: Record<string, string> = {
        name: c.name.trim(),
        value: c.value.trim(),
        path: c.path.trim() || '/',
      };
      const domain = c.domain.trim();
      if (domain) cookie.domain = domain;
      return cookie;
    });

  // Routing headers → applied as use.extraHTTPHeaders by the runner. A header row
  // needs a name (value may legitimately be empty).
  const headers: Record<string, string> = {};
  for (const h of e.headers) {
    const hn = h.name.trim();
    if (hn) headers[hn] = h.value.trim();
  }

  // HTTP basic-auth → sealed server-side (creds_blob), applied as use.httpCredentials.
  const basicUser = e.basic_auth_user.trim();
  const credentials = basicUser
    ? { basic_auth: { username: basicUser, password: e.basic_auth_pass } }
    : null;

  const env_assertion =
    e.pin_kind === 'banner' && e.pin_selector.trim() && e.pin_expect_text.trim()
      ? { selector: e.pin_selector.trim(), expect_text: e.pin_expect_text.trim() }
      : e.pin_kind === 'url' && e.pin_url_pattern.trim()
        ? { url_pattern: e.pin_url_pattern.trim() }
        : {};

  return {
    name,
    ...(e.base_url.trim() ? { base_url: e.base_url.trim() } : {}),
    ...(cookies.length ? { cookies } : {}),
    ...(Object.keys(headers).length ? { headers } : {}),
    ...(credentials ? { credentials } : {}),
    ...(Object.keys(env_assertion).length ? { env_assertion } : {}),
    env_attestation: { env_kind: e.env_kind },
    fences: { allow_submit: e.env_kind === 'disposable' && e.allow_submit },
  };
}

/** Has the operator authored ANYTHING on this draft (beyond the name)? Used so a
 *  fully-blank row is silently dropped, but a content-bearing one with no name is
 *  flagged — never silently discarded (that would be a green-wash). */
function envHasContent(e: EnvDraft): boolean {
  return (
    !!e.base_url.trim() ||
    e.cookies.some((c) => c.name.trim() || c.value.trim() || c.domain.trim()) ||
    e.headers.some((h) => h.name.trim() || h.value.trim()) ||
    !!e.basic_auth_user.trim() ||
    !!e.basic_auth_pass.trim() ||
    !!e.pin_selector.trim() ||
    !!e.pin_expect_text.trim() ||
    !!e.pin_url_pattern.trim() ||
    e.allow_submit
  );
}

/** Client-side validation that pre-empts EVERY server rejection (so a profile is
 *  never dropped, nor an app created with its envs rejected): a name for any
 *  authored draft, unique + length-bounded names, complete pins, complete routing
 *  cookies, and a valid base-URL override. First human-readable error, or null. */
function validateEnvs(envs: EnvDraft[]): string | null {
  const seen = new Set<string>();
  for (let i = 0; i < envs.length; i++) {
    const e = envs[i];
    const name = e.name.trim();
    if (!name) {
      // A content-bearing draft with no name would be silently discarded on
      // submit (envDraftToPayload returns null) — refuse instead of losing it.
      if (envHasContent(e)) return `Environment ${i + 1} has settings but no name — add a name (or clear its fields).`;
      continue; // a fully-blank draft is intentionally dropped
    }
    if (name.length > 200) return `Environment “${name.slice(0, 24)}…”: the name must be 200 characters or fewer.`;
    const key = name.toLowerCase();
    if (seen.has(key)) return `Duplicate environment name “${name}”.`;
    seen.add(key);
    if (e.pin_kind === 'banner' && !(e.pin_selector.trim() && e.pin_expect_text.trim()))
      return `Environment “${name}”: the DOM-banner pin needs both a selector and expected text.`;
    if (e.pin_kind === 'url' && !e.pin_url_pattern.trim())
      return `Environment “${name}”: the URL pin needs a URL pattern.`;
    if (e.base_url.trim() && !isHttpUrl(e.base_url.trim()))
      return `Environment “${name}”: the base-URL override must be an absolute http(s) URL.`;
    // A half-filled routing cookie is the env SELECTOR silently vanishing — with a
    // 'none' pin that becomes an undetected wrong-env pass. Block it.
    if (e.cookies.some((c) => !!c.name.trim() !== !!c.value.trim()))
      return `Environment “${name}”: a routing cookie needs both a name and a value (or clear the row).`;
    // A header with a value but no name is dropped on submit — flag it.
    if (e.headers.some((h) => !h.name.trim() && !!h.value.trim()))
      return `Environment “${name}”: a routing header needs a name (or clear the row).`;
    // Basic-auth needs a username; a lone password would be dropped.
    if (!e.basic_auth_user.trim() && !!e.basic_auth_pass.trim())
      return `Environment “${name}”: HTTP basic-auth needs a username (or clear the password).`;
  }
  return null;
}

/** One Environment Profile card — a structured sub-form (no hand-written JSON). */
function EnvCard({
  env,
  index,
  appHost,
  onChange,
  onRemove,
}: {
  env: EnvDraft;
  index: number;
  appHost: string;
  onChange: (patch: Partial<EnvDraft>) => void;
  onRemove: () => void;
}) {
  const submitDisabled = env.env_kind !== 'disposable';
  const setCookie = (j: number, patch: Partial<CookieDraft>) =>
    onChange({ cookies: env.cookies.map((c, k) => (k === j ? { ...c, ...patch } : c)) });
  const addCookie = () => onChange({ cookies: [...env.cookies, { ...BLANK_COOKIE, domain: appHost }] });
  const removeCookie = (j: number) => onChange({ cookies: env.cookies.filter((_, k) => k !== j) });
  const setHeader = (j: number, patch: Partial<HeaderDraft>) =>
    onChange({ headers: env.headers.map((h, k) => (k === j ? { ...h, ...patch } : h)) });
  const addHeader = () => onChange({ headers: [...env.headers, { ...BLANK_HEADER }] });
  const removeHeader = (j: number) => onChange({ headers: env.headers.filter((_, k) => k !== j) });

  return (
    <div className="rounded-lg bg-panel-2 ring-1 ring-line-strong px-3 py-3 space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-2xs font-semibold text-ink-mid font-mono">environment {index + 1}</span>
        <button
          type="button"
          onClick={onRemove}
          className="text-2xs text-ink-low hover:text-crit inline-flex items-center gap-1"
        >
          <Trash2 size={12} aria-hidden /> Remove
        </button>
      </div>

      <div className="grid sm:grid-cols-2 gap-3">
        <Field label="Name" required hint="dev · test · uat · prod — the profile label.">
          <input className={INPUT_CLS} value={env.name} maxLength={200} onChange={(e) => onChange({ name: e.target.value })} placeholder="uat" />
        </Field>
        <Field label="Environment kind" hint="Prod is always forced read-only; only disposable may submit.">
          <select
            className={INPUT_CLS}
            value={env.env_kind}
            onChange={(e) =>
              onChange({
                env_kind: e.target.value as EnvKind,
                allow_submit: e.target.value === 'disposable' ? env.allow_submit : false,
              })
            }
          >
            <option value="disposable">disposable</option>
            <option value="staging">staging</option>
            <option value="prod">prod</option>
          </select>
        </Field>
        <div className="sm:col-span-2">
          <Field
            label="Base URL override (optional)"
            hint="Leave blank to reuse the app's base URL. Set only when this env lives on a different host."
          >
            <input
              className={INPUT_CLS}
              value={env.base_url}
              onChange={(e) => onChange({ base_url: e.target.value })}
              placeholder="https://uat.acmelife.example"
            />
          </Field>
        </div>
      </div>

      {/* Routing cookies — the env selector (e.g. a Gloo routing cookie). */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-2xs font-semibold text-ink-mid">
            Routing cookies
            <span className="text-ink-faint font-normal ml-1">the env selector (e.g. a Gloo routing cookie)</span>
          </span>
          <button
            type="button"
            onClick={addCookie}
            className="text-2xs text-teal hover:text-teal/80 inline-flex items-center gap-1"
          >
            <Plus size={12} aria-hidden /> Cookie
          </button>
        </div>
        {env.cookies.length === 0 && (
          <p className="text-2xs text-ink-faint">No routing cookie — this env is selected by its base URL alone.</p>
        )}
        {env.cookies.map((c, j) => (
          <div key={j} className="grid grid-cols-[1fr_1fr_1.3fr_0.6fr_auto] gap-1.5 items-center">
            <input className={INPUT_CLS} value={c.name} onChange={(e) => setCookie(j, { name: e.target.value })} placeholder="gloo" aria-label="cookie name" />
            <input className={INPUT_CLS} value={c.value} onChange={(e) => setCookie(j, { value: e.target.value })} placeholder="uat" aria-label="cookie value" />
            <input className={INPUT_CLS} value={c.domain} onChange={(e) => setCookie(j, { domain: e.target.value })} placeholder=".acmelife.example" aria-label="cookie domain" />
            <input className={INPUT_CLS} value={c.path} onChange={(e) => setCookie(j, { path: e.target.value })} placeholder="/" aria-label="cookie path" />
            <button type="button" onClick={() => removeCookie(j)} className="text-ink-low hover:text-crit p-1" aria-label="remove cookie">
              <X size={14} aria-hidden />
            </button>
          </div>
        ))}
      </div>

      {/* Routing headers — applied as use.extraHTTPHeaders (e.g. gateway env header). */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-2xs font-semibold text-ink-mid">
            Routing headers
            <span className="text-ink-faint font-normal ml-1">sent on every request (e.g. an API-gateway env header)</span>
          </span>
          <button
            type="button"
            onClick={addHeader}
            className="text-2xs text-teal hover:text-teal/80 inline-flex items-center gap-1"
          >
            <Plus size={12} aria-hidden /> Header
          </button>
        </div>
        {env.headers.map((h, j) => (
          <div key={j} className="grid grid-cols-[1fr_1.6fr_auto] gap-1.5 items-center">
            <input className={INPUT_CLS} value={h.name} onChange={(e) => setHeader(j, { name: e.target.value })} placeholder="X-Env" aria-label="header name" />
            <input className={INPUT_CLS} value={h.value} onChange={(e) => setHeader(j, { value: e.target.value })} placeholder="uat" aria-label="header value" />
            <button type="button" onClick={() => removeHeader(j)} className="text-ink-low hover:text-crit p-1" aria-label="remove header">
              <X size={14} aria-hidden />
            </button>
          </div>
        ))}
      </div>

      {/* HTTP basic-auth — sealed at rest (AAD=environment_id); use.httpCredentials. */}
      <div className="grid sm:grid-cols-2 gap-3">
        <Field label="HTTP basic-auth user (optional)" hint="For envs behind HTTP basic-auth. Sealed at rest, never echoed.">
          <input className={INPUT_CLS} value={env.basic_auth_user} autoComplete="off" onChange={(e) => onChange({ basic_auth_user: e.target.value })} placeholder="qa" />
        </Field>
        <Field label="HTTP basic-auth password">
          <input type="password" className={INPUT_CLS} value={env.basic_auth_pass} autoComplete="off" onChange={(e) => onChange({ basic_auth_pass: e.target.value })} />
        </Field>
      </div>

      {/* Env pin — the HARD proof that the routing actually worked. */}
      <div className="space-y-2">
        <span className="block text-2xs font-semibold text-ink-mid">
          Environment pin
          <span className="text-ink-faint font-normal ml-1">proves the run landed on this env (mismatch → RED)</span>
        </span>
        <div className="flex gap-4 text-2xs text-ink-mid">
          {(['banner', 'url', 'none'] as EnvPinKind[]).map((k) => (
            <label key={k} className="inline-flex items-center gap-1.5">
              <input
                type="radio"
                name={`pin-${index}`}
                checked={env.pin_kind === k}
                onChange={() => onChange({ pin_kind: k })}
                className="accent-[rgb(var(--teal))]"
              />
              {k === 'banner' ? 'DOM banner' : k === 'url' ? 'URL pattern' : 'None'}
            </label>
          ))}
        </div>
        {env.pin_kind === 'banner' && (
          <div className="grid sm:grid-cols-2 gap-3">
            <Field label="Banner selector" hint="A DOM element that names the env.">
              <input className={INPUT_CLS} value={env.pin_selector} onChange={(e) => onChange({ pin_selector: e.target.value })} placeholder="#env-banner" />
            </Field>
            <Field label="Expected text" hint="Text that element must contain on this env.">
              <input className={INPUT_CLS} value={env.pin_expect_text} onChange={(e) => onChange({ pin_expect_text: e.target.value })} placeholder="Environment: UAT" />
            </Field>
          </div>
        )}
        {env.pin_kind === 'url' && (
          <Field label="URL pattern" hint="A substring the landed URL must contain.">
            <input className={INPUT_CLS} value={env.pin_url_pattern} onChange={(e) => onChange({ pin_url_pattern: e.target.value })} placeholder="uat.acmelife.example" />
          </Field>
        )}
        {env.pin_kind === 'none' && (
          <p className="text-2xs text-warn leading-snug">
            No pin — the run cannot prove it reached this environment. Only safe when the base URL alone is unambiguous
            (never for cookie-routed envs that share one URL).
          </p>
        )}
      </div>

      <label className={cn('flex items-center gap-2 text-xs', submitDisabled ? 'text-ink-faint' : 'text-ink-mid')}>
        <input
          type="checkbox"
          checked={env.allow_submit && !submitDisabled}
          onChange={(e) => onChange({ allow_submit: e.target.checked })}
          disabled={submitDisabled}
          className="accent-[rgb(var(--teal))]"
        />
        Allow the mutating submit tier (disposable env only)
      </label>
    </div>
  );
}

// ── the wizard ───────────────────────────────────────────────────────────────

/** Registrable domain of a base URL, mirroring the server's reduction so a host
 *  and its sub-hosts (uat./prod./a numbered box) all propose the same recipe. */
function reuseDomain(raw: string): string {
  try {
    const host = new URL(raw.trim()).hostname.toLowerCase();
    if (/^[\d.]+$/.test(host) || !host.includes('.')) return host;
    const parts = host.split('.');
    if (parts.length <= 2) return host;
    const lastTwo = parts.slice(-2).join('.');
    return MULTI_PART_SUFFIXES.has(lastTwo) ? parts.slice(-3).join('.') : lastTwo;
  } catch { return ''; }
}

const MULTI_PART_SUFFIXES = new Set([
  'co.uk', 'org.uk', 'gov.uk', 'ac.uk', 'com.au', 'net.au', 'org.au', 'co.nz',
  'co.jp', 'co.kr', 'co.in', 'co.za', 'com.br', 'com.mx', 'com.sg', 'com.hk',
  'com.cn', 'com.tw', 'com.my', 'com.tr', 'co.th', 'co.id', 'co.il',
]);

export function OnboardingWizard() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [form, setForm] = useState<WizardForm>(EMPTY);
  // Don't make the next tester record a login somebody already recorded.
  const [reuse, setReuse] = useState<any>(null);
  // RECORD AT ONBOARDING. The crawl cannot get past a login without one, so the
  // recording happens here — before the app exists — and rides along when it does.
  const [recLive, setRecLive] = useState('');
  const [recBusy, setRecBusy] = useState('');
  const [recorded, setRecorded] = useState<any>(null);
  const [recErr, setRecErr] = useState('');

  const startRecording = async () => {
    const url = form.base_url.trim();
    if (!url) { setRecErr('Enter the Base URL first — that is where the recording starts.'); return; }
    setRecBusy('start'); setRecErr(''); setRecorded(null);
    try {
      let r;
      try {
        r = await factoryApi.startRecording(url);
      } catch (first: any) {
        // The recorder browser is single-occupancy: one recording at a time. A
        // 409 means an earlier one was left open — a double-click, or a tab closed
        // mid-recording. That is recoverable and the operator should not have to
        // understand it, so close the stale one and take the slot.
        const conflict = first?.response?.status === 409 ||
          /409|already|busy|in progress/i.test(String(first?.response?.data?.detail || first?.message || ''));
        if (!conflict) throw first;
        await factoryApi.cancelRecording().catch(() => {});
        r = await factoryApi.startRecording(url);
      }
      if (!r?.live_url) throw new Error('the runner did not return a live view');
      setRecLive(r.live_url);
    } catch (e: any) {
      const status = e?.response?.status;
      const detail = e?.response?.data?.detail || e?.message || '';
      setRecErr(
        status === 409
          ? 'A recording is already open and could not be closed automatically. Wait a moment and press Record again.'
          : status === 502
            ? 'The recorder browser is unreachable. It may be mid-restart — try again shortly.'
            : status === 403
              ? 'Recording needs an editor, manager or admin role on this tenant.'
              : String(detail) || 'Could not start recording.');
    } finally { setRecBusy(''); }
  };

  const finishRecording = async () => {
    setRecBusy('save'); setRecErr('');
    try {
      const r = await factoryApi.saveRecording();
      setRecLive('');
      setRecorded(r);
      if (!r?.usable) {
        setRecErr(r?.reason === 'no_credential_fields_observed'
          ? 'No login fields were filled, so only the environment was captured.'
          : `No login recipe: ${r?.reason || 'unknown'}.`);
      }
      // The recording knows the real entry point better than a typed guess.
      if (r?.environment?.base_url) set('base_url', r.environment.base_url);
    } catch (e: any) {
      setRecErr(e?.response?.data?.detail || e?.message || 'could not save the recording');
    } finally { setRecBusy(''); }
  };

  const abortRecording = async () => {
    setRecBusy('cancel');
    try { await factoryApi.cancelRecording(); } catch { /* closing anyway */ }
    setRecLive(''); setRecBusy('');
  };

  const [submitting, setSubmitting] = useState(false);

  const set = <K extends keyof WizardForm>(key: K, value: WizardForm[K]) => setForm((f) => ({ ...f, [key]: value }));

  // Multi-env: structured Environment Profile handlers (add / patch / remove).
  const addEnv = () => setForm((f) => ({ ...f, environments: [...f.environments, blankEnv()] }));
  const updateEnv = (i: number, patch: Partial<EnvDraft>) =>
    setForm((f) => ({ ...f, environments: f.environments.map((e, k) => (k === i ? { ...e, ...patch } : e)) }));
  const removeEnv = (i: number) =>
    setForm((f) => ({ ...f, environments: f.environments.filter((_, k) => k !== i) }));

  useEffect(() => {
    const domain = reuseDomain(form.base_url);
    if (!domain) { setReuse(null); return; }
    let cancelled = false;
    const t = window.setTimeout(async () => {
      try {
        const r = await factoryApi.reuseCheck({ domain });
        if (!cancelled) setReuse(r && r.action && r.action !== 'record' ? r : null);
      } catch {
        // A proposal is a convenience. If the lookup fails the tester simply
        // records as they would have anyway — it must never block onboarding.
        if (!cancelled) setReuse(null);
      }
    }, 600);
    return () => { cancelled = true; window.clearTimeout(t); };
  }, [form.base_url]);

  const canProceedAccess = form.name.trim().length > 0 && form.base_url.trim().length > 0;
  const isLast = step === BUCKETS.length - 1;

  const payload = useMemo<AppCreatePayload>(() => {
    const hosts = form.allowed_hosts
      .split(/[\s,]+/)
      .map((h) => h.trim())
      .filter(Boolean);
    // Target mode (R3 Mode 2): parse the operator's crawl-scope prefixes into
    // schedule.scope_paths — normalize each to a leading '/', drop blanks. Empty
    // ⇒ the key is omitted ⇒ Explore mode (the backend confines the crawl only
    // when scope_paths is non-empty; see explorations.py::_dispatch_explorer).
    const scopePaths = form.scope_paths
      .split(/[\s,]+/)
      .map((p) => p.trim())
      .filter(Boolean)
      .map((p) => (p.startsWith('/') ? p : `/${p}`));
    let answers: Record<string, unknown> = {};
    try {
      answers = form.answers.trim() ? (JSON.parse(form.answers) as Record<string, unknown>) : {};
    } catch {
      answers = { _raw: form.answers };
    }
    // Data-tab seed field values (field/keyword → value). Projected server-side
    // onto the crawler's fill contract so real forms actually get filled.
    let fill: Record<string, unknown> = {};
    try {
      fill = form.seed_fields.trim() ? (JSON.parse(form.seed_fields) as Record<string, unknown>) : {};
    } catch {
      fill = {};
    }
    // The crawl gate (security/prod_guard.py) is fail-closed on THREE things:
    // a signed rules-of-engagement, an attested + unexpired non-prod envelope,
    // and a passed preflight. Build all three here so a completed wizard yields
    // an app that is actually crawlable ('live'), not a stuck 'draft'.
    const attDays = Math.max(1, Math.floor(Number(form.attestation_days) || 30));
    const expiresAt = new Date(Date.now() + attDays * 86_400_000).toISOString();
    const signer = form.attested_by.trim() || form.name.trim();
    // Optional MFA second factor, folded into the (envelope-encrypted)
    // credentials blob the crawler reads: TOTP computes the code from the shared
    // seed; 'otp' is a fixed/deterministic test code. `delivery` (optional) names
    // the channel to pick on a "how do you want your code?" screen.
    const mfa =
      form.mfa_method === 'totp' && form.mfa_secret.trim()
        ? { kind: 'totp', seed: form.mfa_secret.trim(), delivery: form.mfa_delivery.trim() }
        : form.mfa_method === 'otp' && form.mfa_secret.trim()
          ? { kind: 'otp', otp: form.mfa_secret.trim(), delivery: form.mfa_delivery.trim() }
          : null;
    // Tier-4 auth-hook (login the crawler can't script): an https URL that
    // returns a fresh Playwright storageState, fetched per crawl.
    const authHook = form.mfa_method === 'hook' && form.mfa_secret.trim() ? form.mfa_secret.trim() : null;
    // A RECORDED session is what actually gets the crawl past the login. The
    // crawler already prefers one over trying to script the form itself
    // (explorations._resolve_session), so recording removes the guesswork rather
    // than adding a new mechanism. Typed credentials still ride along so the login
    // can be re-driven when the session expires.
    const recordedSession = recorded?.usable ? recorded.session : null;
    const credentials =
      form.username || form.password || authHook || recordedSession
        ? {
            username: form.username,
            password: form.password,
            ...(mfa ? { mfa } : {}),
            ...(authHook ? { auth_hook: authHook } : {}),
            ...(recordedSession ? { session: recordedSession } : {}),
          }
        : null;
    return {
      name: form.name.trim(),
      base_url: form.base_url.trim(),
      credentials,
      // The recorded choreography travels with the app. It becomes a real recipe
      // when the first crawl mints an artifact — recipes are artifact-scoped, and
      // no artifact exists yet at this point.
      ...(recorded?.usable && recorded.login ? { login_recording: recorded.login } : {}),
      repo_binding: { provider: form.repo_provider, project: form.repo_project, webhook_secret: form.webhook_secret },
      answer_key: { fill, notes: form.seed_notes, outcomes: answers },
      env_attestation: {
        env_kind: form.env_kind,
        attested_by: form.attested_by.trim(),
        reset_procedure: form.reset_procedure,
        expires_at: expiresAt,
        rules_of_engagement: { signed: form.roe_signed, signed_by: form.roe_signed ? signer : '' },
        preflight: { passed: form.preflight_passed },
      },
      fences: { allowed_hosts: hosts, allow_submit: form.env_kind === 'disposable' && form.allow_submit },
      schedule: {
        cadence: form.cadence,
        ...(scopePaths.length ? { scope_paths: scopePaths } : {}),
        // 'user' is written as ABSENT, not as a value: the conservative default
        // must be what an app gets when nobody decided, on every read path.
        ...(form.data_mode === 'agent' ? { data_mode: 'agent' } : {}),
        ...(form.crawl_mode === 'e2e' ? { crawl_mode: 'e2e' } : {}),
      },
      budgets: form.usd_per_cycle ? { usd_per_cycle: Number(form.usd_per_cycle) } : {},
    };
  }, [form, recorded]);   // `recorded` matters: a saved recording changes the payload

  const submit = async () => {
    if (!canProceedAccess) {
      toast.error('Name and base URL are required');
      setStep(0);
      return;
    }
    // Validate Environment Profiles BEFORE creating the app, so an operator never
    // ends up with an app created but its profiles rejected server-side.
    const envError = validateEnvs(form.environments);
    if (envError) {
      toast.error(envError);
      setStep(5);
      return;
    }
    setSubmitting(true);
    try {
      const app = await api.createApp(payload);
      // Multi-env: create the Environment Profiles the operator authored as a
      // SEPARATE additive loop — the app + its one flow/baseline are created
      // exactly as before; environments are run-time rebinds of that same flow.
      const envPayloads = form.environments
        .map(envDraftToPayload)
        .filter((p): p is EnvProfileCreatePayload => p !== null);
      let created = 0;
      for (const env of envPayloads) {
        try {
          await api.createEnvironment(app.app_id, env);
          created++;
        } catch (e) {
          toast.error(`Environment '${env.name}' failed`, { description: (e as QecApiError).message });
        }
      }
      if (created) toast.success(`${created} environment profile(s) created`);
      // Multi-env: designate the app's run environment (the daemon runs EVERY cycle
      // against it). Applied AFTER the profiles exist so the server can validate the
      // name against a real Environment Profile.
      const runEnv = form.run_environment.trim();
      if (runEnv) {
        try {
          await api.updateApp(app.app_id, { run_environment: runEnv });
          toast.success(`Cycles will run against “${runEnv}”`);
        } catch (e) {
          toast.error(`Could not bind run environment “${runEnv}”`, { description: (e as QecApiError).message });
        }
      }
      toast.success('App onboarded', { description: app.name });
      navigate(`/apps/${app.app_id}`);
    } catch (err) {
      toast.error('Could not onboard app', { description: (err as QecApiError).message });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div className="flex items-center gap-3">
        <Seal size={30} tone="certified" title="" />
        <div>
          <h1 className="text-lg font-semibold text-ink tracking-tight">Onboard an application</h1>
          <p className="text-2xs text-ink-low font-mono">six buckets — then the fleet certifies every release</p>
        </div>
      </div>

      {/* stepper */}
      <ol className="grid grid-cols-4 sm:grid-cols-7 gap-2" aria-label="Onboarding steps">
        {BUCKETS.map((b, i) => {
          const Icon = b.icon;
          const active = i === step;
          const done = i < step;
          return (
            <li key={b.key}>
              <button
                type="button"
                onClick={() => setStep(i)}
                aria-current={active ? 'step' : undefined}
                className={cn(
                  'w-full rounded-lg px-2 py-2 text-center ring-1 transition-colors',
                  active
                    ? 'bg-teal/[0.1] ring-teal/40 text-ink'
                    : done
                      ? 'bg-panel-2 ring-line-strong text-ink-mid'
                      : 'bg-panel ring-line text-ink-low hover:text-ink',
                )}
              >
                <Icon size={16} className={cn('mx-auto mb-1', active ? 'text-teal' : done ? 'text-good' : 'text-ink-low')} aria-hidden />
                <span className="block text-2xs font-semibold">{b.label}</span>
              </button>
            </li>
          );
        })}
      </ol>

      <Panel tone="elevated" className="space-y-4">
        <div>
          <h2 className="text-sm font-semibold text-ink">{BUCKETS[step].label}</h2>
          <p className="text-2xs text-ink-low mt-0.5">{BUCKETS[step].hint}</p>
        </div>

        {step === 0 && (
          <div className="grid sm:grid-cols-2 gap-4">
            <Field label="App name" required>
              <input className={INPUT_CLS} value={form.name} onChange={(e) => set('name', e.target.value)} placeholder="ACME Life · Term Quote" />
            </Field>
            <Field label="Base URL" required hint="Absolute http(s) URL. Point at the SPECIFIC flow's entry, not a hub page.">
              <input className={INPUT_CLS} value={form.base_url} onChange={(e) => set('base_url', e.target.value)} placeholder="https://quote.acmelife.example" />
            </Field>
            {reuse && (
              <div className="sm:col-span-2 rounded-lg ring-1 ring-emerald-200 bg-emerald-50 px-3 py-2 text-2xs text-emerald-900">
                {reuse.action === 'reuse' ? (
                  <>
                    <span className="font-semibold">A login recipe already exists for{' '}
                    <span className="font-mono">{reuse.domain}</span>.</span>{' '}
                    A teammate recorded the <span className="font-semibold">steps</span> to sign in
                    {(reuse.recipes?.[0]?.slots?.length ?? 0) > 0 && (
                      <> — it fills <span className="font-semibold">
                        {reuse.recipes[0].slots.map((sl: any) => sl.name).join(', ')}
                      </span></>
                    )}. That saves re-recording the choreography, but a recipe holds{' '}
                    <span className="font-semibold">no credentials</span> — recording captures field
                    names, never the values you type. To actually sign in you still need to supply
                    them: fill <span className="font-semibold">Login username / password</span> below
                    now, or use <span className="font-semibold">Record the login &amp; environment</span>{' '}
                    below to re-record it here and capture a session. You can also attach a login to this
                    app later from its <span className="font-semibold">Signing in</span> panel — but the
                    crawl stays logged out until credentials exist.
                  </>
                ) : (
                  <>
                    <span className="font-semibold">
                      {reuse.recipes?.length ?? reuse.options?.length ?? 0} login recipes already exist for{' '}
                      <span className="font-mono">{reuse.domain}</span>.
                    </span>{' '}
                    These are recorded <span className="font-semibold">steps</span>, not credentials — a
                    public login and a member portal sign in differently, so more than one shape was
                    captured. Reusing one saves re-recording the steps, but{' '}
                    <span className="font-semibold">every recipe still needs a member&apos;s credentials
                    to sign in</span>. Supply them now: fill{' '}
                    <span className="font-semibold">Login username / password</span> below, or use{' '}
                    <span className="font-semibold">Record the login &amp; environment</span> below to
                    record the right flow here and capture a session. You can also attach a login to this
                    app later from its <span className="font-semibold">Signing in</span> panel — but the
                    crawl stays logged out until credentials exist, so don&apos;t skip this expecting
                    it&apos;s already done.
                  </>
                )}
              </div>
            )}

            <Field label="Login username" hint="Envelope-encrypted at rest, never echoed. Leave blank for a public app.">
              <input className={INPUT_CLS} value={form.username} onChange={(e) => set('username', e.target.value)} autoComplete="off" />
            </Field>
            <Field label="Login password">
              <input type="password" className={INPUT_CLS} value={form.password} onChange={(e) => set('password', e.target.value)} autoComplete="off" />
            </Field>

            {/* MFA second factor — folded into the encrypted credentials blob.
                The crawler logs itself in on every crawl AND every cycle, so the
                factor must be one it can compute: a TOTP seed or a fixed/test OTP. */}
            <Field
              label="Authentication method"
              hint="How the crawler gets past login. It re-authenticates every crawl and every run, so it must be a method it can complete itself."
            >
              <select
                className={INPUT_CLS}
                value={form.mfa_method}
                onChange={(e) => set('mfa_method', e.target.value as WizardForm['mfa_method'])}
              >
                <option value="none">Username + password only (single-step)</option>
                <option value="totp">+ MFA · Authenticator app (TOTP seed)</option>
                <option value="otp">+ MFA · Fixed / test code (deterministic OTP)</option>
                <option value="hook">Login hook — URL returns a session (un-scriptable logins)</option>
              </select>
            </Field>
            {form.mfa_method !== 'none' && (
              <Field
                label={
                  form.mfa_method === 'totp' ? 'TOTP secret (base32)'
                  : form.mfa_method === 'hook' ? 'Auth hook URL'
                  : 'Fixed one-time code'
                }
                required
                hint={
                  form.mfa_method === 'totp'
                    ? 'The shared authenticator seed; codes are computed per RFC 6238. Encrypted at rest.'
                    : form.mfa_method === 'hook'
                      ? 'An https endpoint that returns a fresh Playwright storageState; fetched per crawl for logins the crawler can’t script (captcha/SSO/hardware token).'
                      : 'A deterministic test code (e.g. a QA env that always accepts 123456). Encrypted at rest.'
                }
              >
                <input
                  className={INPUT_CLS}
                  value={form.mfa_secret}
                  onChange={(e) => set('mfa_secret', e.target.value)}
                  autoComplete="off"
                  placeholder={
                    form.mfa_method === 'totp' ? 'JBSWY3DPEHPK3PXP'
                    : form.mfa_method === 'hook' ? 'https://qa.acme.example/vkpower/session'
                    : '123456'
                  }
                />
              </Field>
            )}
            {/* RECORD — at the foot of Access, because the crawl cannot get past a
                login without one. One pass captures the environment landed on, the
                login choreography, and the session that gets THIS crawl in.
                Styling uses the portal's own tokens + Button: a hand-rolled
                `bg-brand` is not a defined colour here and rendered an INVISIBLE
                button (white text on a pale panel). */}
            <div className="sm:col-span-2 rounded-lg ring-1 ring-line bg-inset p-3">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs font-semibold text-ink">Record the login &amp; environment</span>
                <span className="text-2xs text-ink-low">
                  optional — log in once by hand instead of hoping we work your form out
                </span>
                {!recLive && (
                  <span className="ml-auto">
                    <Button variant="primary" size="sm" onClick={startRecording}
                      loading={recBusy === 'start'} icon={<Radio size={14} />}>
                      {recorded ? 'Record again' : 'Record'}
                    </Button>
                  </span>
                )}
              </div>

              {recLive && (
                <div className="mt-3">
                  <div className="flex items-center gap-2 mb-2 flex-wrap">
                    <span className="inline-block h-2 w-2 rounded-full bg-crit animate-pulse" />
                    <span className="text-xs font-semibold text-ink">
                      Recording — go to the right environment, then log in
                    </span>
                    <span className="ml-auto flex items-center gap-2">
                      <Button variant="primary" size="sm" onClick={finishRecording}
                        loading={recBusy === 'save'} icon={<ShieldCheck size={14} />}>
                        I&apos;m logged in — save
                      </Button>
                      <Button variant="secondary" size="sm" onClick={abortRecording}
                        loading={recBusy === 'cancel'}>
                        Cancel
                      </Button>
                    </span>
                  </div>
                  <iframe src={recLive} title="Log in to record the login"
                    className="w-full h-[420px] rounded-md ring-1 ring-line bg-panel" />
                  <p className="text-2xs text-ink-low mt-2">
                    We record WHICH fields you fill and WHICH controls you press — never the values you type.
                    Save as soon as you reach the logged-in page.
                  </p>
                </div>
              )}

              {recorded?.usable && !recLive && (
                <div className="mt-3 rounded-md ring-1 ring-teal/40 bg-teal/10 px-3 py-2 text-2xs text-ink">
                  <span className="font-semibold text-ink">Login recorded.</span>{' '}
                  slots <span className="font-semibold">{(recorded.login?.slot_names || []).join(', ')}</span>
                  {' · '}<span className="font-mono">{recorded.login?.login_path}</span>
                  {recorded.login?.home_path && <> → <span className="font-mono">{recorded.login.home_path}</span></>}
                  <div className="mt-1 text-ink-mid">
                    {recorded.session
                      ? 'A session was captured, so the FIRST crawl starts logged in. A session is a snapshot — it expires, and some apps cannot restore one at all.'
                      : 'No session was captured, so the crawl will start logged out.'}
                  </div>
                  {/* A recording captures the STEPS and a session, never the values
                      typed — so it alone cannot sign a later crawl in. Saying so here
                      (instead of after a crawl fails) is the difference between one
                      clear choice and a loop of re-recordings that never works. */}
                  {!form.username && (
                    <div className="mt-1.5 text-ink">
                      <span className="font-semibold">To keep every future crawl signed in,
                      also fill Login username / password above.</span>{' '}
                      A recording never captures what you type, so without them the crawl
                      can only replay this one session.
                    </div>
                  )}
                  {(recorded.environment?.cookies || []).length > 0 && (
                    <div className="mt-1 text-ink-mid">
                      routing cookies: <span className="font-mono">
                        {recorded.environment.cookies.map((c: any) => c.name).join(', ')}
                      </span>
                    </div>
                  )}
                </div>
              )}

              {recErr && !recLive && (
                <p className="mt-2 text-2xs text-warn">{recErr}</p>
              )}
            </div>

            {(form.mfa_method === 'totp' || form.mfa_method === 'otp') && (
              <Field label="Delivery channel (optional)" hint="If the app asks 'email or mobile?', which to pick. Blank = let the app default.">
                <input
                  className={INPUT_CLS}
                  value={form.mfa_delivery}
                  onChange={(e) => set('mfa_delivery', e.target.value)}
                  placeholder="email"
                />
              </Field>
            )}
          </div>
        )}

        {step === 1 && (
          <div className="grid sm:grid-cols-2 gap-4">
            <Field label="Repo provider">
              <select className={INPUT_CLS} value={form.repo_provider} onChange={(e) => set('repo_provider', e.target.value)}>
                <option value="gitlab">GitLab</option>
                <option value="github">GitHub</option>
              </select>
            </Field>
            <Field label="Project path" hint="e.g. acme/term-quote">
              <input className={INPUT_CLS} value={form.repo_project} onChange={(e) => set('repo_project', e.target.value)} />
            </Field>
            <Field label="Webhook secret" hint="Verifies X-Gitlab-Token on every push.">
              <input className={INPUT_CLS} value={form.webhook_secret} onChange={(e) => set('webhook_secret', e.target.value)} autoComplete="off" />
            </Field>
          </div>
        )}

        {step === 2 && (
          <div className="space-y-4">
            <Field
              label="Seed field values (JSON)"
              hint="field name or keyword → value. The crawler fills matching form fields with these so it can pass validation and reach the deep flows."
            >
              <textarea
                className={cn(INPUT_CLS, 'min-h-[9rem] resize-y font-mono text-xs')}
                value={form.seed_fields}
                onChange={(e) => set('seed_fields', e.target.value)}
                placeholder={'{\n  "age": 35,\n  "coverage": 500000,\n  "zip": "12345",\n  "tobacco": "no"\n}'}
              />
            </Field>
            <Field label="Seed data notes (optional)" hint="Free-text guidance (test personas, edge cases). Reference only today; compiled to seed values in a later release.">
              <textarea className={cn(INPUT_CLS, 'min-h-[6rem] resize-y')} value={form.seed_notes} onChange={(e) => set('seed_notes', e.target.value)} />
            </Field>
          </div>
        )}

        {step === 3 && (
          <Field label="Answer key (JSON)" hint="Ground-truth expected outcomes / oracles. JSON object, or free text.">
            <textarea
              className={cn(INPUT_CLS, 'min-h-[9rem] resize-y font-mono text-xs')}
              value={form.answers}
              onChange={(e) => set('answers', e.target.value)}
              placeholder={'{\n  "quote_min_age": 18,\n  "declined_reason_codes": ["UW-17", "UW-22"]\n}'}
            />
          </Field>
        )}

        {step === 4 && (
          <div className="grid sm:grid-cols-2 gap-4">
            <Field label="Environment kind" hint="Only 'disposable' may host the mutating submit tier. Never 'prod'.">
              <select
                className={INPUT_CLS}
                value={form.env_kind}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    env_kind: e.target.value as EnvKind,
                    // Mirror the EnvCard: a non-disposable kind can never keep the
                    // mutating submit tier enabled (else the checkbox stays checked
                    // while disabled, and the payload persists a contradiction).
                    allow_submit: e.target.value === 'disposable' ? f.allow_submit : false,
                  }))
                }
              >
                <option value="disposable">disposable</option>
                <option value="staging">staging</option>
                <option value="prod">prod</option>
              </select>
            </Field>
            <Field label="Attested by" required hint="Who affirms this attestation (your email or name).">
              <input className={INPUT_CLS} value={form.attested_by} onChange={(e) => set('attested_by', e.target.value)} placeholder="you@company.com" />
            </Field>
            <Field label="Attestation valid for (days)" hint="After this it expires and the app must be re-attested.">
              <input type="number" min="1" className={INPUT_CLS} value={form.attestation_days} onChange={(e) => set('attestation_days', e.target.value)} placeholder="30" />
            </Field>
            <Field label="Reset procedure" hint="How the env is restored between runs.">
              <input className={INPUT_CLS} value={form.reset_procedure} onChange={(e) => set('reset_procedure', e.target.value)} placeholder="nightly snapshot restore" />
            </Field>
            <Field label="Allowed egress hosts" hint="Comma/space separated; the crawl is network-fenced to these (defaults to the base-URL host).">
              <input className={INPUT_CLS} value={form.allowed_hosts} onChange={(e) => set('allowed_hosts', e.target.value)} placeholder=".acmelife.example" />
            </Field>
            {/* The fail-closed crawl gate (security/prod_guard.py): an app is
                crawlable ('live') ONLY with a signed RoE + attested non-prod env
                + a passed preflight. Collect the two that were missing. */}
            <div className="sm:col-span-2 rounded-lg bg-inset ring-1 ring-line px-3 py-2.5 space-y-2.5">
              <p className="text-2xs text-ink-low leading-snug">
                These make the app <span className="text-ink-mid font-semibold">crawlable (live)</span>. Without a signed
                rules-of-engagement and a passed preflight the app is saved as a{' '}
                <span className="text-ink-mid font-semibold">draft</span> and cannot crawl.
              </p>
              <label className="flex items-start gap-2 text-xs text-ink-mid">
                <input
                  type="checkbox"
                  checked={form.roe_signed}
                  onChange={(e) => set('roe_signed', e.target.checked)}
                  className="accent-[rgb(var(--teal))] mt-0.5"
                />
                <span>I am authorized to test this target — sign the rules of engagement.</span>
              </label>
              <label className="flex items-start gap-2 text-xs text-ink-mid">
                <input
                  type="checkbox"
                  checked={form.preflight_passed}
                  onChange={(e) => set('preflight_passed', e.target.checked)}
                  className="accent-[rgb(var(--teal))] mt-0.5"
                />
                <span>Safety preflight passed — target is reachable, non-prod, and this crawl is read-only.</span>
              </label>
            </div>

            <label className="flex items-center gap-2 text-xs text-ink-mid sm:col-span-2 mt-1">
              <input
                type="checkbox"
                checked={form.allow_submit && form.env_kind === 'disposable'}
                onChange={(e) => set('allow_submit', e.target.checked)}
                className="accent-[rgb(var(--teal))]"
                disabled={form.env_kind !== 'disposable'}
              />
              Allow the mutating submit tier (disposable env only)
            </label>
          </div>
        )}

        {step === 5 && (
          <div className="space-y-4">
            <div className="rounded-lg bg-inset ring-1 ring-line px-3 py-2.5">
              <p className="text-2xs text-ink-low leading-snug">
                The <span className="text-ink-mid font-semibold">same crawled flow</span>, run against many
                environments — crawl once, run many. Each profile rebinds the flow's entry (base URL) and
                routing (cookies) at run time, and proves it landed on the right env.{' '}
                <span className="text-ink-mid">Optional</span> — add one profile per target env.{' '}
                <span className="text-ink-mid">Prod is always forced read-only.</span>
              </p>
            </div>

            {form.environments.length === 0 && (
              <p className="text-2xs text-ink-faint">
                No environment profiles yet — the app will run against its base URL only. Add one to run the
                same flow against dev/test/uat.
              </p>
            )}

            <div className="space-y-3">
              {form.environments.map((env, i) => (
                <EnvCard
                  key={i}
                  env={env}
                  index={i}
                  appHost={hostOf(form.base_url)}
                  onChange={(patch) => updateEnv(i, patch)}
                  onRemove={() => removeEnv(i)}
                />
              ))}
            </div>

            <Button variant="ghost" onClick={addEnv} icon={<Plus size={15} />}>
              Add environment
            </Button>
          </div>
        )}

        {step === 6 && (
          <div className="grid sm:grid-cols-2 gap-4">
            <Field label="Cadence">
              <select className={INPUT_CLS} value={form.cadence} onChange={(e) => set('cadence', e.target.value)}>
                <option value="on_push">On every push</option>
                <option value="nightly">Nightly</option>
                <option value="manual">Manual only</option>
              </select>
            </Field>
            <Field label="Budget (USD / cycle)" hint="A cycle that would exceed this is budget_stopped — never a partial green.">
              <input type="number" min="0" className={INPUT_CLS} value={form.usd_per_cycle} onChange={(e) => set('usd_per_cycle', e.target.value)} placeholder="12" />
            </Field>
            <div className="sm:col-span-2">
              <Field
                label="Run environment"
                hint="Multi-env: every cycle runs against this Environment Profile (crawl once, run many). Default = the app base URL. Add profiles in the Environments tab first."
              >
                <select
                  className={INPUT_CLS}
                  value={form.run_environment}
                  onChange={(e) => set('run_environment', e.target.value)}
                >
                  <option value="">Default — the app base URL (single-env)</option>
                  {Array.from(new Set(form.environments.map((en) => en.name.trim()).filter(Boolean))).map((nm) => (
                    <option key={nm} value={nm}>
                      {nm}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <div className="sm:col-span-2 space-y-4">
              {/* SCOPE — where the crawl walks. Radios rather than a free-text
                  scope box: the client's complaint was being asked to identify a
                  deep URL and then repeat its path, which the Base URL already
                  says. Target prefills from it. */}
              <div className="space-y-1.5" role="radiogroup" aria-label="Crawl scope">
                <p className="text-2xs font-semibold uppercase tracking-wide text-ink-faint">
                  Scope — where the crawl walks
                </p>
                <WizardRadio
                  checked={form.crawl_mode === 'explore' && !form.scope_paths.trim()}
                  onSelect={() => {
                    set('crawl_mode', 'explore');
                    set('scope_paths', '');
                  }}
                  title="Explore — the whole application"
                  note="Every page reachable from the Base URL. Use this to discover what exists."
                />
                <WizardRadio
                  checked={form.crawl_mode !== 'e2e' && !!form.scope_paths.trim()}
                  onSelect={() => {
                    set('crawl_mode', 'target');
                    set('scope_paths', entryPathOf(form.base_url));
                  }}
                  title="Target — one section, thoroughly"
                  note="Confined to the path below. Nothing else on the host is crawled."
                />
                {form.crawl_mode !== 'e2e' && !!form.scope_paths.trim() && (
                  <div className="pl-6 space-y-1">
                    <input
                      className={cn(INPUT_CLS, 'font-mono text-xs')}
                      value={form.scope_paths}
                      onChange={(e) => set('scope_paths', e.target.value)}
                      placeholder={entryPathOf(form.base_url)}
                      aria-label="Target path prefix"
                    />
                    {entryOutOfScope(form.base_url, form.scope_paths) ? (
                      <p className="text-2xs text-amber-600 leading-snug">
                        Your Base URL enters at{' '}
                        <span className="font-mono">{entryPathOf(form.base_url)}</span>, outside
                        this scope — the crawl would start out of bounds and capture nothing.
                      </p>
                    ) : (
                      <p className="text-2xs text-ink-faint">
                        Taken from your Base URL. Edit if you meant a different section.
                      </p>
                    )}
                  </div>
                )}
                <WizardRadio
                  checked={form.crawl_mode === 'e2e'}
                  onSelect={() => {
                    set('crawl_mode', 'e2e');
                    set('scope_paths', '');
                  }}
                  title="End-to-end flow — walk each journey to its end"
                  note="Follows a funnel all the way to its final step instead of sampling the first few, and reports which journeys actually finished."
                />
                {form.crawl_mode === 'e2e' && (
                  <p className="pl-6 text-2xs text-amber-600 leading-snug">
                    One path per journey. At each decision point a single option is taken,
                    so the business paths behind the other options are not visited. Branch
                    coverage is the next phase.
                  </p>
                )}
              </div>

              {/* DATA — who supplies the values. */}
              <div className="space-y-1.5" role="radiogroup" aria-label="Test data">
                <p className="text-2xs font-semibold uppercase tracking-wide text-ink-faint">
                  Data — who supplies the values
                </p>
                <WizardRadio
                  checked={form.data_mode === 'user'}
                  onSelect={() => set('data_mode', 'user')}
                  title="You provide the data"
                  note="The crawl fills what it can from your test data and names anything it could not, so you supply only what is actually missing."
                />
                <WizardRadio
                  checked={form.data_mode === 'agent'}
                  onSelect={() => set('data_mode', 'agent')}
                  title="Let the agent fill what it can"
                  note="A coherent fictional person answers every field it honestly can — including the choices that decide which path a funnel takes. Every choice is recorded. One-time codes and document uploads are still asked for."
                />
              </div>
            </div>
          </div>
        )}

        {/* nav */}
        <div className="flex items-center justify-between pt-2 border-t border-line">
          <Button variant="ghost" onClick={() => setStep((s) => Math.max(0, s - 1))} disabled={step === 0}>
            Back
          </Button>
          {isLast ? (
            <Button variant="primary" loading={submitting} onClick={submit} icon={<ShieldCheck size={15} />}>
              Onboard app
            </Button>
          ) : (
            <Button
              variant="primary"
              onClick={() => setStep((s) => Math.min(BUCKETS.length - 1, s + 1))}
              disabled={step === 0 && !canProceedAccess}
            >
              Next
            </Button>
          )}
        </div>
      </Panel>
    </div>
  );
}

export default OnboardingWizard;
