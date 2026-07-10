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
  repo_provider: string;
  repo_project: string;
  webhook_secret: string;
  seed_notes: string;
  answers: string;
  env_kind: EnvKind;
  attested_by: string;
  reset_procedure: string;
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
  repo_provider: 'gitlab',
  repo_project: '',
  webhook_secret: '',
  seed_notes: '',
  answers: '',
  env_kind: 'disposable',
  attested_by: '',
  reset_procedure: '',
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
    return {
      name: form.name.trim(),
      base_url: form.base_url.trim(),
      credentials: form.username || form.password ? { username: form.username, password: form.password } : null,
      repo_binding: { provider: form.repo_provider, project: form.repo_project, webhook_secret: form.webhook_secret },
      answer_key: { notes: form.seed_notes, answers },
      env_attestation: { env_kind: form.env_kind, attested_by: form.attested_by, reset_procedure: form.reset_procedure },
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
            <Field label="Base URL" required hint="Absolute http(s) URL.">
              <input className={INPUT_CLS} value={form.base_url} onChange={(e) => set('base_url', e.target.value)} placeholder="https://quote.acmelife.example" />
            </Field>
            <Field label="Login username" hint="Envelope-encrypted at rest, never echoed.">
              <input className={INPUT_CLS} value={form.username} onChange={(e) => set('username', e.target.value)} autoComplete="off" />
            </Field>
            <Field label="Login password">
              <input type="password" className={INPUT_CLS} value={form.password} onChange={(e) => set('password', e.target.value)} autoComplete="off" />
            </Field>
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
          <Field label="Seed data notes" hint="How the crawler should seed forms (test personas, sample inputs).">
            <textarea className={cn(INPUT_CLS, 'min-h-[8rem] resize-y')} value={form.seed_notes} onChange={(e) => set('seed_notes', e.target.value)} />
          </Field>
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
            <Field label="Environment kind" hint="Only 'disposable' may host the mutating submit tier.">
              <select className={INPUT_CLS} value={form.env_kind} onChange={(e) => set('env_kind', e.target.value as EnvKind)}>
                <option value="disposable">disposable</option>
                <option value="staging">staging</option>
                <option value="prod">prod</option>
              </select>
            </Field>
            <Field label="Attested by">
              <input className={INPUT_CLS} value={form.attested_by} onChange={(e) => set('attested_by', e.target.value)} placeholder="you@company.com" />
            </Field>
            <Field label="Reset procedure" hint="How the env is restored between runs.">
              <input className={INPUT_CLS} value={form.reset_procedure} onChange={(e) => set('reset_procedure', e.target.value)} placeholder="nightly snapshot restore" />
            </Field>
            <Field label="Allowed egress hosts" hint="Comma/space separated. The crawl is network-fenced to these.">
              <input className={INPUT_CLS} value={form.allowed_hosts} onChange={(e) => set('allowed_hosts', e.target.value)} placeholder=".acmelife.example" />
            </Field>
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
