/**
 * PersonaMatrixPanel — the Persona × Environment management surface.
 *
 * RUN = Suite × Environment × Persona. This panel makes the whole matrix
 * operable from the portal instead of the raw API: define members (personas),
 * govern environments (posture / production default-deny / health), and
 * provision credential cards WRITE-ONLY (slot values are sent once and never
 * returned — the manifest shows slot NAMES only). It reads the same endpoints
 * the run dispatch enforces, so what you see here is what actually gates a run.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle, KeyRound, Loader2, Lock, Plus, RefreshCw, ShieldCheck, Users, Boxes,
} from 'lucide-react';
import { api } from './factoryApi';

type Persona = {
  persona_id: string; name: string; behavior_class?: string; traits?: string[];
  is_recording_baseline?: boolean; legacy?: boolean; status?: string;
};
type Environment = {
  environment_id: string; label?: string; posture?: string; effective_posture?: string;
  is_production?: boolean; write_authorized?: boolean; base_url?: string;
  data_epoch?: string; health_status?: string; health_detail?: string;
};
type Card = {
  persona_id: string; persona_name?: string; environment_id: string;
  slot_names?: string[]; verify_status?: string; verified_epoch?: string;
  last_verified_at?: string | null;
};

function Section({ icon, title, sub, children, right }: {
  icon: React.ReactNode; title: string; sub?: string;
  children: React.ReactNode; right?: React.ReactNode;
}) {
  return (
    <section className="rounded-xl bg-white ring-1 ring-nexus-100 p-4">
      <div className="flex items-center gap-2 mb-3">
        <span className="text-nexus-500">{icon}</span>
        <h3 className="text-[13px] font-bold text-nexus-900">{title}</h3>
        {sub && <span className="text-[11px] text-nexus-400 font-medium">— {sub}</span>}
        <div className="ml-auto">{right}</div>
      </div>
      {children}
    </section>
  );
}

function PostureBadge({ env }: { env: Environment }) {
  const p = env.effective_posture || env.posture || 'read_write';
  const tone = p === 'read_write' ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
    : p === 'read_only' ? 'bg-rose-50 text-rose-700 ring-rose-200'
      : 'bg-amber-50 text-amber-700 ring-amber-200';
  return <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ring-1 ${tone}`}>{p}</span>;
}

function HealthDot({ s }: { s?: string }) {
  const c = s === 'healthy' ? 'bg-emerald-500'
    : (s === 'unreachable' || s === 'login_failed' || s === 'recipe_drift') ? 'bg-rose-500'
      : 'bg-slate-300';
  return <span className={`inline-block h-2 w-2 rounded-full ${c}`} title={s || 'unknown'} />;
}

const INPUT = 'rounded-md border border-slate-200 px-2 py-1 text-[12px] text-slate-700 focus:outline-none focus:border-nexus-300';
const BTN = 'inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-[11px] font-semibold ring-1 disabled:opacity-50';

export default function PersonaMatrixPanel({ artifactId }: { artifactId: string }) {
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [envs, setEnvs] = useState<Environment[]>([]);
  const [cards, setCards] = useState<Card[]>([]);
  const [recipes, setRecipes] = useState<any[]>([]);
  const [ops, setOps] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState('');
  const [flash, setFlash] = useState('');

  const load = useCallback(async () => {
    setLoading(true); setErr('');
    try {
      const [p, e, m, r, o] = await Promise.all([
        api.listPersonas(artifactId), api.listEnvironments(artifactId),
        api.credentialsManifest(artifactId), api.listRecipes(artifactId),
        api.personaOpsSummary(artifactId),
      ]);
      setPersonas(p?.personas || []); setEnvs(e?.environments || []);
      setCards(m?.cards || []); setRecipes(r?.recipes || []); setOps(o || null);
    } catch (ex: any) {
      setErr(ex?.response?.data?.detail || ex?.message || 'failed to load the persona matrix');
    } finally { setLoading(false); }
  }, [artifactId]);

  useEffect(() => { load(); }, [load]);

  const say = (m: string) => { setFlash(m); window.setTimeout(() => setFlash(''), 3500); };
  const fail = (ex: any) => setErr(ex?.response?.data?.detail?.error || ex?.response?.data?.detail || ex?.message || 'action failed');

  // ── forms ──────────────────────────────────────────────────────────────────
  const [np, setNp] = useState({ name: '', behavior_class: '', traits: '' });
  const addPersona = async () => {
    if (!np.name.trim()) return;
    setBusy('persona'); setErr('');
    try {
      await api.createPersona(artifactId, {
        name: np.name.trim(), behavior_class: np.behavior_class.trim(),
        traits: np.traits.split(',').map((t) => t.trim()).filter(Boolean),
      });
      setNp({ name: '', behavior_class: '', traits: '' }); say('Persona saved.'); await load();
    } catch (ex) { fail(ex); } finally { setBusy(''); }
  };

  const [ne, setNe] = useState({ environment_id: '', posture: 'read_write', is_production: false, write_authorized: false, base_url: '', data_epoch: '' });
  const addEnv = async () => {
    if (!ne.environment_id.trim()) return;
    setBusy('env'); setErr('');
    try {
      await api.putEnvironment(artifactId, ne.environment_id.trim(), {
        posture: ne.posture, is_production: ne.is_production, write_authorized: ne.write_authorized,
        base_url: ne.base_url.trim(), data_epoch: ne.data_epoch.trim(),
      });
      setNe({ environment_id: '', posture: 'read_write', is_production: false, write_authorized: false, base_url: '', data_epoch: '' });
      say('Environment saved.'); await load();
    } catch (ex) { fail(ex); } finally { setBusy(''); }
  };

  const [nc, setNc] = useState({ persona_id: '', environment_id: '', slots: 'member_number, password' });
  const [slotVals, setSlotVals] = useState<Record<string, string>>({});
  const slotNames = useMemo(
    () => nc.slots.split(',').map((s) => s.trim()).filter(Boolean), [nc.slots]);
  const saveCard = async () => {
    if (!nc.persona_id || !nc.environment_id.trim() || !slotNames.length) return;
    const values: Record<string, string> = {};
    for (const s of slotNames) values[s] = slotVals[s] || '';
    if (Object.values(values).every((v) => !v)) { setErr('enter at least one slot value'); return; }
    setBusy('card'); setErr('');
    try {
      await api.putCredentialCard(artifactId, nc.persona_id, nc.environment_id.trim(), values);
      setSlotVals({}); say('Credential card stored (encrypted, write-only).'); await load();
    } catch (ex) { fail(ex); } finally { setBusy(''); }
  };

  const materializeRecipe = async () => {
    setBusy('recipe'); setErr('');
    try {
      const r = await api.ensureBaselineRecipe(artifactId);
      say(r?.materialized ? `Baseline recipe materialized (${(r.slots || []).join(', ')}).`
        : `No new recipe: ${r?.reason || 'nothing to derive'}.`);
      await load();
    } catch (ex) { fail(ex); } finally { setBusy(''); }
  };

  const cardKey = (c: Card) => `${c.persona_id}::${c.environment_id}`;
  const cardsByPersona = useMemo(() => {
    const m: Record<string, Card[]> = {};
    for (const c of cards) (m[c.persona_id] ||= []).push(c);
    return m;
  }, [cards]);

  const namedPersonas = personas.filter((p) => !p.legacy);

  if (loading) {
    return <div className="flex items-center gap-2 text-[12px] text-nexus-500 py-6">
      <Loader2 className="h-4 w-4 animate-spin" /> loading the persona × environment matrix…</div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-start gap-2">
        <div>
          <h2 className="text-[15px] font-bold tracking-tight text-nexus-900">Personas &amp; Environments</h2>
          <p className="text-[11px] text-nexus-400">
            RUN = Suite × Environment × Persona. Define members, govern environments,
            and provision credential cards (write-only). Everything here gates a run.
          </p>
        </div>
        <button onClick={load} className={`${BTN} ml-auto ring-slate-200 text-slate-600 hover:bg-slate-50`}>
          <RefreshCw className="h-3.5 w-3.5" /> Refresh
        </button>
      </div>

      {ops && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {[
            { k: 'Personas', v: ops.personas, icon: <Users className="h-3.5 w-3.5" /> },
            { k: 'Environments', v: `${ops.environments?.total ?? 0} · ${ops.environments?.production ?? 0} prod`, icon: <Boxes className="h-3.5 w-3.5" /> },
            { k: 'Cards verified', v: `${ops.credentials?.verified ?? 0}/${ops.credentials?.total ?? 0}`, icon: <KeyRound className="h-3.5 w-3.5" /> },
            { k: 'Recipes', v: ops.recipes, icon: <ShieldCheck className="h-3.5 w-3.5" /> },
          ].map((s) => (
            <div key={s.k} className="rounded-lg bg-white ring-1 ring-nexus-100 px-3 py-2">
              <div className="flex items-center gap-1.5 text-[10px] font-bold uppercase text-nexus-400">{s.icon}{s.k}</div>
              <div className="text-[16px] font-bold text-nexus-900 tabular-nums">{s.v}</div>
            </div>
          ))}
        </div>
      )}

      {err && <div className="flex items-center gap-2 rounded-md bg-rose-50 ring-1 ring-rose-200 px-3 py-2 text-[12px] text-rose-700">
        <AlertTriangle className="h-4 w-4 shrink-0" /> {String(err)}</div>}
      {flash && <div className="rounded-md bg-emerald-50 ring-1 ring-emerald-200 px-3 py-2 text-[12px] text-emerald-700">{flash}</div>}

      {/* ── Personas ─────────────────────────────────────────────────────── */}
      <Section icon={<Users className="h-4 w-4" />} title="Members (personas)"
        sub="identity + behavior class the same suite runs as">
        <div className="space-y-1.5 mb-3">
          {namedPersonas.length === 0 && <p className="text-[11px] text-nexus-400">No personas yet — add one below. Runs use the default identity until then.</p>}
          {namedPersonas.map((p) => (
            <div key={p.persona_id} className="flex items-center gap-2 text-[12px] rounded-md bg-nexus-50/60 px-2.5 py-1.5">
              <span className="font-semibold text-nexus-800">{p.name}</span>
              {p.is_recording_baseline && <span className="text-[9px] font-bold px-1 py-0.5 rounded bg-nexus-100 text-nexus-600">BASELINE</span>}
              {p.behavior_class && <span className="text-[10px] text-nexus-500">· {p.behavior_class}</span>}
              {!!(p.traits && p.traits.length) && <span className="text-[10px] text-nexus-400">· {p.traits.join(', ')}</span>}
              <span className="ml-auto text-[10px] text-nexus-400 tabular-nums">
                {(cardsByPersona[p.persona_id] || []).length} card(s)
              </span>
              <button onClick={async () => { setBusy(`retire:${p.persona_id}`); try { await api.retirePersona(artifactId, p.persona_id); say('Retired.'); await load(); } catch (ex) { fail(ex); } finally { setBusy(''); } }}
                disabled={busy === `retire:${p.persona_id}`}
                className="text-[10px] text-slate-400 hover:text-rose-500">retire</button>
            </div>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t border-nexus-100 pt-3">
          <input className={INPUT} placeholder="name (e.g. 50yo · 5 kids)" value={np.name} onChange={(e) => setNp({ ...np, name: e.target.value })} />
          <input className={INPUT} placeholder="behavior class (optional)" value={np.behavior_class} onChange={(e) => setNp({ ...np, behavior_class: e.target.value })} />
          <input className={`${INPUT} w-48`} placeholder="traits, comma-separated" value={np.traits} onChange={(e) => setNp({ ...np, traits: e.target.value })} />
          <button onClick={addPersona} disabled={busy === 'persona' || !np.name.trim()} className={`${BTN} ring-nexus-200 text-nexus-700 bg-nexus-50 hover:bg-nexus-100`}>
            {busy === 'persona' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />} Add member
          </button>
        </div>
      </Section>

      {/* ── Environments ─────────────────────────────────────────────────── */}
      <Section icon={<Boxes className="h-4 w-4" />} title="Environments"
        sub="posture · production default-deny · health">
        <div className="space-y-1.5 mb-3">
          {envs.length === 0 && <p className="text-[11px] text-nexus-400">No environments registered — a run against an unregistered environment is an ordinary read_write target.</p>}
          {envs.map((e) => (
            <div key={e.environment_id} className="flex items-center gap-2 text-[12px] rounded-md bg-nexus-50/60 px-2.5 py-1.5">
              <HealthDot s={e.health_status} />
              <span className="font-semibold text-nexus-800 font-mono">{e.environment_id}</span>
              <PostureBadge env={e} />
              {e.is_production && <span className="text-[9px] font-bold px-1 py-0.5 rounded bg-rose-100 text-rose-700">PROD{e.write_authorized ? ' · write-authorized' : ' · default-deny'}</span>}
              {e.data_epoch && <span className="text-[10px] text-nexus-400">epoch {e.data_epoch}</span>}
              {e.base_url && <span className="ml-auto text-[10px] text-nexus-400 font-mono truncate max-w-[220px]">{e.base_url}</span>}
            </div>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t border-nexus-100 pt-3">
          <input className={`${INPUT} w-28`} placeholder="env id (uat)" value={ne.environment_id} onChange={(e) => setNe({ ...ne, environment_id: e.target.value })} />
          <select className={INPUT} value={ne.posture} onChange={(e) => setNe({ ...ne, posture: e.target.value })}>
            <option value="read_write">read_write</option>
            <option value="read_only">read_only</option>
            <option value="no_submit">no_submit</option>
          </select>
          <label className="flex items-center gap-1 text-[11px] text-nexus-600"><input type="checkbox" checked={ne.is_production} onChange={(e) => setNe({ ...ne, is_production: e.target.checked })} /> production</label>
          <label className="flex items-center gap-1 text-[11px] text-nexus-600"><input type="checkbox" checked={ne.write_authorized} onChange={(e) => setNe({ ...ne, write_authorized: e.target.checked })} /> authorize writes</label>
          <input className={`${INPUT} w-56`} placeholder="base_url (optional)" value={ne.base_url} onChange={(e) => setNe({ ...ne, base_url: e.target.value })} />
          <input className={`${INPUT} w-24`} placeholder="data_epoch" value={ne.data_epoch} onChange={(e) => setNe({ ...ne, data_epoch: e.target.value })} />
          <button onClick={addEnv} disabled={busy === 'env' || !ne.environment_id.trim()} className={`${BTN} ring-nexus-200 text-nexus-700 bg-nexus-50 hover:bg-nexus-100`}>
            {busy === 'env' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />} Save environment
          </button>
        </div>
      </Section>

      {/* ── Credential cards (write-only) ────────────────────────────────── */}
      <Section icon={<KeyRound className="h-4 w-4" />} title="Credential cards"
        sub="envelope-encrypted · slot NAMES shown, values never returned"
        right={<span className="inline-flex items-center gap-1 text-[10px] text-nexus-400"><Lock className="h-3 w-3" /> write-only</span>}>
        <div className="overflow-x-auto mb-3">
          <table className="w-full text-[12px]">
            <thead><tr className="text-[10px] uppercase text-nexus-400 text-left">
              <th className="py-1 pr-3">Member</th><th className="py-1 pr-3">Environment</th>
              <th className="py-1 pr-3">Slots</th><th className="py-1 pr-3">Verified</th></tr></thead>
            <tbody>
              {cards.length === 0 && <tr><td colSpan={4} className="py-2 text-[11px] text-nexus-400">No cards yet. Add one below — a persona needs a card for the environment it runs in.</td></tr>}
              {cards.map((c) => (
                <tr key={cardKey(c)} className="border-t border-nexus-50">
                  <td className="py-1 pr-3 font-semibold text-nexus-800">{c.persona_name || c.persona_id.slice(0, 8)}</td>
                  <td className="py-1 pr-3 font-mono text-nexus-600">{c.environment_id}</td>
                  <td className="py-1 pr-3 text-nexus-500">{(c.slot_names || []).join(', ')}</td>
                  <td className="py-1 pr-3">
                    <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${c.verify_status === 'verified' ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>
                      {c.verify_status || 'unverified'}{c.verified_epoch ? ` · ${c.verified_epoch}` : ''}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="border-t border-nexus-100 pt-3 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <select className={INPUT} value={nc.persona_id} onChange={(e) => setNc({ ...nc, persona_id: e.target.value })}>
              <option value="">member…</option>
              {namedPersonas.map((p) => <option key={p.persona_id} value={p.persona_id}>{p.name}</option>)}
            </select>
            <input className={`${INPUT} w-28`} placeholder="env id (uat)" value={nc.environment_id} onChange={(e) => setNc({ ...nc, environment_id: e.target.value })}
              list="env-ids" />
            <datalist id="env-ids">{envs.map((e) => <option key={e.environment_id} value={e.environment_id} />)}</datalist>
            <input className={`${INPUT} w-64`} placeholder="slot names (member_number, password, pin)" value={nc.slots} onChange={(e) => setNc({ ...nc, slots: e.target.value })} />
          </div>
          {nc.persona_id && slotNames.length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              {slotNames.map((s) => (
                <input key={s} className={`${INPUT} w-40`} type="password" autoComplete="new-password"
                  placeholder={s} value={slotVals[s] || ''} onChange={(e) => setSlotVals({ ...slotVals, [s]: e.target.value })} />
              ))}
              <button onClick={saveCard} disabled={busy === 'card'} className={`${BTN} ring-nexus-200 text-nexus-700 bg-nexus-50 hover:bg-nexus-100`}>
                {busy === 'card' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Lock className="h-3.5 w-3.5" />} Store card
              </button>
            </div>
          )}
          <p className="text-[10px] text-nexus-400">Values are envelope-encrypted on save and never returned — this panel can set a card, never read one.</p>
        </div>
      </Section>

      {/* ── Recipes ──────────────────────────────────────────────────────── */}
      <Section icon={<ShieldCheck className="h-4 w-4" />} title="Login recipes"
        sub="the login choreography named members fill with their card"
        right={<button onClick={materializeRecipe} disabled={busy === 'recipe'} className={`${BTN} ring-nexus-200 text-nexus-700 bg-nexus-50 hover:bg-nexus-100`}>
          {busy === 'recipe' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />} Derive from form-login
        </button>}>
        {recipes.length === 0 && <p className="text-[11px] text-nexus-400">No recipe yet. Configure a form-login (auth) then “Derive from form-login”, or POST a multi-step recipe.</p>}
        <div className="space-y-1">
          {recipes.map((r) => (
            <div key={r.recipe_id} className="flex items-center gap-2 text-[12px] rounded-md bg-nexus-50/60 px-2.5 py-1.5">
              <span className="font-semibold text-nexus-800">v{r.version}</span>
              <span className={`text-[10px] px-1.5 py-0.5 rounded ${r.status === 'active' ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>{r.status}</span>
              <span className="text-[10px] text-nexus-400">{r.step_count} steps · slots {(r.slots || []).map((s: any) => s.name).join(', ')}</span>
              {r.verified_at && <span className="ml-auto text-[10px] text-emerald-600">verified</span>}
            </div>
          ))}
        </div>
      </Section>
    </div>
  );
}
