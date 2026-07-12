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
import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import {
  ClipboardCheck,
  Database,
  GitBranch,
  KeyRound,
  Settings,
  ShieldCheck,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { api, QecApiError } from '../../lib/api';
import { cn } from '../../lib/format';
import { Button, Panel, Seal } from '../../components';
import type { AppCreatePayload, EnvKind } from '../../types/qec';

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

// ── the wizard ───────────────────────────────────────────────────────────────

export function OnboardingWizard() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [form, setForm] = useState<WizardForm>(EMPTY);
  const [submitting, setSubmitting] = useState(false);

  const set = <K extends keyof WizardForm>(key: K, value: WizardForm[K]) => setForm((f) => ({ ...f, [key]: value }));

  const canProceedAccess = form.name.trim().length > 0 && form.base_url.trim().length > 0;
  const isLast = step === BUCKETS.length - 1;

  const payload = useMemo<AppCreatePayload>(() => {
    const hosts = form.allowed_hosts
      .split(/[\s,]+/)
      .map((h) => h.trim())
      .filter(Boolean);
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
    const credentials =
      form.username || form.password || authHook
        ? {
            username: form.username,
            password: form.password,
            ...(mfa ? { mfa } : {}),
            ...(authHook ? { auth_hook: authHook } : {}),
          }
        : null;
    return {
      name: form.name.trim(),
      base_url: form.base_url.trim(),
      credentials,
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
      fences: { allowed_hosts: hosts, allow_submit: form.allow_submit },
      schedule: { cadence: form.cadence },
      budgets: form.usd_per_cycle ? { usd_per_cycle: Number(form.usd_per_cycle) } : {},
    };
  }, [form]);

  const submit = async () => {
    if (!canProceedAccess) {
      toast.error('Name and base URL are required');
      setStep(0);
      return;
    }
    setSubmitting(true);
    try {
      const app = await api.createApp(payload);
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
      <ol className="grid grid-cols-6 gap-2" aria-label="Onboarding steps">
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
              <select className={INPUT_CLS} value={form.env_kind} onChange={(e) => set('env_kind', e.target.value as EnvKind)}>
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
                checked={form.allow_submit}
                onChange={(e) => set('allow_submit', e.target.checked)}
                className="accent-[rgb(var(--teal))]"
                disabled={form.env_kind !== 'disposable'}
              />
              Allow the mutating submit tier (disposable env only)
            </label>
          </div>
        )}

        {step === 5 && (
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
