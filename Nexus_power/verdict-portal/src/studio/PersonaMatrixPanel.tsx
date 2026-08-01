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
  AlertTriangle, KeyRound, Loader2, Lock, Plus, RefreshCw, ShieldCheck, Users, Boxes, Video,
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
/** One field of the recorded login — the app's OWN label, and the slot the card
 *  is keyed by. Never authored here; always derived from the recording. */
type SlotField = { name: string; label: string; type?: string };
type LoginContract = {
  has_recipe: boolean; fields: SlotField[]; reason?: string; note?: string;
  version?: number; login_domain?: string;
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

/** Why a recording produced no recipe — said plainly, never as a raw code. */
const RECIPE_REASONS: Record<string, string> = {
  no_observation_from_runner: 'the runner sent no recording (an older runner build?)',
  no_credential_fields_observed: 'no login fields were filled during the recording',
  no_submit_control_observed: 'no submit button was pressed',
  derivation_failed: 'the recording could not be read',
  unusable_observation: 'the recording did not contain a login',
};

export default function PersonaMatrixPanel({ artifactId }: { artifactId: string }) {
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [envs, setEnvs] = useState<Environment[]>([]);
  const [cards, setCards] = useState<Card[]>([]);
  const [recipes, setRecipes] = useState<any[]>([]);
  const [contract, setContract] = useState<LoginContract | null>(null);
  const [ops, setOps] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState('');
  const [flash, setFlash] = useState('');

  const load = useCallback(async () => {
    setLoading(true); setErr('');
    try {
      const [p, e, m, r, o, lc] = await Promise.all([
        api.listPersonas(artifactId), api.listEnvironments(artifactId),
        api.credentialsManifest(artifactId), api.listRecipes(artifactId),
        api.personaOpsSummary(artifactId), api.loginContract(artifactId),
      ]);
      setPersonas(p?.personas || []); setEnvs(e?.environments || []);
      setCards(m?.cards || []); setRecipes(r?.recipes || []); setOps(o || null);
      setContract(lc || null);
    } catch (ex: any) {
      setErr(ex?.response?.data?.detail || ex?.message || 'failed to load the member matrix');
    } finally { setLoading(false); }
  }, [artifactId]);

  useEffect(() => { load(); }, [load]);

  const say = (m: string) => { setFlash(m); window.setTimeout(() => setFlash(''), 3500); };
  const fail = (ex: any) => {
    const d = ex?.response?.data?.detail;
    // A card refused for not matching the login must say WHICH names are wrong —
    // a bare "does not match" just moves the guessing somewhere else.
    if (d && typeof d === 'object' && d.reason === 'slots_do_not_match_recipe') {
      const bits = [`the login fills ${(d.required || []).join(', ')}`];
      if (d.missing?.length) bits.push(`missing ${d.missing.join(', ')}`);
      if (d.unexpected?.length) bits.push(`not part of this login: ${d.unexpected.join(', ')}`);
      setErr(`${d.error || 'this card does not match the recorded login'} — ${bits.join(' · ')}`);
      return;
    }
    setErr(d?.error || (typeof d === 'string' ? d : '') || ex?.message || 'action failed');
  };

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
      setNp({ name: '', behavior_class: '', traits: '' }); say('Member saved.'); await load();
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

  const [nc, setNc] = useState({ persona_id: '', environment_id: '' });
  const [slotVals, setSlotVals] = useState<Record<string, string>>({});
  // The card's fields come from the RECORDED LOGIN, not from anything typed here.
  // A hand-typed slot name that does not match saves cleanly and then skips the
  // whole login at run time, so the suite runs logged out and the application gets
  // blamed. There is deliberately no way to author a slot name in this panel.
  const loginFields: SlotField[] = contract?.fields || [];
  const saveCard = async () => {
    if (!nc.persona_id || !nc.environment_id.trim() || !loginFields.length) return;
    const values: Record<string, string> = {};
    for (const f of loginFields) values[f.name] = slotVals[f.name] || '';
    const blank = loginFields.filter((f) => !values[f.name].trim());
    if (blank.length) {
      setErr(`the login needs every field — still empty: ${blank.map((f) => f.label).join(', ')}`);
      return;
    }
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

  // ── RECORD LOGIN ─────────────────────────────────────────────────────────────
  // Log in ONCE by hand in our browser; we record the choreography (which fields,
  // which buttons — identifiers only, never a value) and turn it into a recipe any
  // member can replay with their own card. The same pass also stores the session.
  const [liveUrl, setLiveUrl] = useState('');
  const [recorded, setRecorded] = useState<any>(null);

  const startRecording = async () => {
    setBusy('record'); setErr(''); setRecorded(null);
    try {
      const r = await api.startAuthCapture(artifactId);
      if (!r?.live_url) throw new Error('the runner did not return a live view');
      setLiveUrl(r.live_url);
    } catch (ex) { fail(ex); } finally { setBusy(''); }
  };

  const finishRecording = async () => {
    setBusy('record-save'); setErr('');
    try {
      const r = await api.saveAuthCapture(artifactId);
      setLiveUrl('');
      const rec = r?.recipe || null;
      setRecorded(rec);
      say(rec?.recorded
        ? `Login recorded — slots: ${(rec.slots || []).join(', ')}.`
        : `Session saved, but no recipe: ${RECIPE_REASONS[rec?.reason] || rec?.reason || 'unknown'}.`);
      await load();
    } catch (ex) { fail(ex); } finally { setBusy(''); }
  };

  const abortRecording = async () => {
    setBusy('record-cancel');
    try { await api.cancelAuthCapture(artifactId); } catch (ex) { /* closing anyway */ }
    setLiveUrl(''); setBusy('');
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
      <Loader2 className="h-4 w-4 animate-spin" /> loading the member × environment matrix…</div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-start gap-2">
        <div>
          <h2 className="text-[15px] font-bold tracking-tight text-nexus-900">Members &amp; Environments</h2>
          <p className="text-[11px] text-nexus-400">
            RUN = Suite × Environment × Member. Define members, govern environments,
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
            { k: 'Members', v: ops.personas, icon: <Users className="h-3.5 w-3.5" /> },
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
      <Section icon={<Users className="h-4 w-4" />} title="Members"
        sub="identity + behavior class the same suite runs as">
        <div className="space-y-1.5 mb-3">
          {namedPersonas.length === 0 && <p className="text-[11px] text-nexus-400">No members yet — add one below. Runs use the default identity until then.</p>}
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
              {cards.length === 0 && <tr><td colSpan={4} className="py-2 text-[11px] text-nexus-400">No cards yet. Add one below — a member needs a card for the environment it runs in.</td></tr>}
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
          {/* No recorded login means there is nothing a card could fill. Provisioning
              one now would be guessing at field names, and a guessed name skips the
              login instead of failing — so the form is not offered at all. */}
          {loginFields.length === 0 ? (
            <div className="rounded-lg ring-1 ring-amber-200 bg-amber-50 p-2.5 text-[11px] text-amber-800">
              <span className="font-bold">Record the login first.</span>{' '}
              {contract?.note || 'No login has been recorded for this application yet.'}
              {' '}A card supplies the values for the fields that login fills, so there
              is nothing to fill in until the recording exists. Use <span className="font-semibold">Record login</span> below.
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <select className={INPUT} value={nc.persona_id} onChange={(e) => setNc({ ...nc, persona_id: e.target.value })}>
                  <option value="">member…</option>
                  {namedPersonas.map((p) => <option key={p.persona_id} value={p.persona_id}>{p.name}</option>)}
                </select>
                <input className={`${INPUT} w-28`} placeholder="env id (uat)" value={nc.environment_id} onChange={(e) => setNc({ ...nc, environment_id: e.target.value })}
                  list="env-ids" />
                <datalist id="env-ids">{envs.map((e) => <option key={e.environment_id} value={e.environment_id} />)}</datalist>
                <span className="text-[10px] text-nexus-400">
                  fields from the recorded login{contract?.version ? ` (v${contract.version})` : ''}
                </span>
              </div>
              {nc.persona_id && (
                <div className="flex flex-wrap items-end gap-2">
                  {loginFields.map((f) => (
                    <label key={f.name} className="flex flex-col gap-0.5">
                      <span className="text-[10px] font-semibold text-nexus-600">{f.label}</span>
                      <input className={`${INPUT} w-44`} type="password" autoComplete="new-password"
                        placeholder={f.label} value={slotVals[f.name] || ''}
                        onChange={(e) => setSlotVals({ ...slotVals, [f.name]: e.target.value })} />
                    </label>
                  ))}
                  <button onClick={saveCard} disabled={busy === 'card'} className={`${BTN} ring-nexus-200 text-nexus-700 bg-nexus-50 hover:bg-nexus-100`}>
                    {busy === 'card' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Lock className="h-3.5 w-3.5" />} Store card
                  </button>
                </div>
              )}
            </>
          )}
          <p className="text-[10px] text-nexus-400">Values are envelope-encrypted on save and never returned — this panel can set a card, never read one.</p>
        </div>
      </Section>

      {/* ── Recipes ──────────────────────────────────────────────────────── */}
      <Section icon={<ShieldCheck className="h-4 w-4" />} title="Login recipes"
        sub="the login choreography named members fill with their card"
        right={<div className="flex items-center gap-1.5">
          <button onClick={startRecording} disabled={!!liveUrl || busy === 'record'}
            className={`${BTN} ring-nexus-600 text-white bg-nexus-600 hover:bg-nexus-700`}>
            {busy === 'record' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Video className="h-3.5 w-3.5" />} Record login
          </button>
          <button onClick={materializeRecipe} disabled={busy === 'recipe' || !!liveUrl} className={`${BTN} ring-nexus-200 text-nexus-700 bg-nexus-50 hover:bg-nexus-100`}>
            {busy === 'recipe' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />} Derive from form-login
          </button>
        </div>}>

        {/* live recorder — log in once, by hand, in our browser */}
        {liveUrl && (
          <div className="mb-3 rounded-lg ring-1 ring-nexus-300 bg-nexus-50/40 p-2.5">
            <div className="flex items-center gap-2 mb-2">
              <span className="inline-block h-2 w-2 rounded-full bg-rose-500 animate-pulse" />
              <span className="text-[12px] font-bold text-nexus-800">Recording — log in below as any member</span>
              <div className="ml-auto flex items-center gap-1.5">
                <button onClick={finishRecording} disabled={busy === 'record-save'}
                  className={`${BTN} ring-emerald-600 text-white bg-emerald-600 hover:bg-emerald-700`}>
                  {busy === 'record-save' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ShieldCheck className="h-3.5 w-3.5" />} I’ve logged in — save recipe
                </button>
                <button onClick={abortRecording} disabled={busy === 'record-cancel'}
                  className={`${BTN} ring-slate-200 text-slate-600 bg-white hover:bg-slate-50`}>Cancel</button>
              </div>
            </div>
            <iframe src={liveUrl} title="Log in to record the login"
              className="w-full h-[460px] rounded-md ring-1 ring-nexus-200 bg-white" />
            <p className="text-[10px] text-nexus-500 mt-1.5">
              We record WHICH fields you fill and WHICH controls you press — never the values you type.
              Your credentials are not stored by this recording; each member supplies their own in a credential card.
            </p>
            <p className="text-[10px] text-amber-700 mt-1">
              Save as soon as you reach the logged-in home page — whatever page you are on when you
              save becomes the “logged in” checkpoint we assert for every other member.
            </p>
          </div>
        )}

        {/* what the recording produced */}
        {recorded && !liveUrl && (
          <div className={`mb-3 rounded-lg p-2.5 ring-1 text-[11px] ${recorded.recorded
            ? 'ring-emerald-200 bg-emerald-50 text-emerald-800'
            : 'ring-amber-200 bg-amber-50 text-amber-800'}`}>
            {recorded.recorded ? (
              <>
                <span className="font-bold">Login recorded.</span>{' '}
                {recorded.step_count} steps · slots{' '}
                <span className="font-semibold">{(recorded.slots || []).join(', ')}</span>.
                {' '}Any member with a card for these slots can now run this suite.
                {(recorded.login_path || recorded.home_path) && (
                  <span className="block mt-1 font-mono text-[10px]">
                    login {recorded.login_path || '?'} → logged-in check {recorded.home_path || '(none — steps-completed only)'}
                  </span>
                )}
                {recorded.home_path === '' && (
                  <span className="block mt-1">
                    No landing page was seen, so success is judged on the steps completing rather than
                    on reaching a page. Re-record and save once you are on the home page to add that check.
                  </span>
                )}
                {recorded.truncated && <span className="block mt-1">Note: the session was long — some events were dropped; check the steps below.</span>}
              </>
            ) : (
              <>
                <span className="font-bold">Session saved, but no recipe.</span>{' '}
                {RECIPE_REASONS[recorded.reason] || recorded.reason || 'unknown reason'}.
                {' '}Runs will still start logged in as the member you used — but to run as OTHER members, record again and complete a full login.
              </>
            )}
          </div>
        )}

        {recipes.length === 0 && !liveUrl && <p className="text-[11px] text-nexus-400">No recipe yet. <span className="font-semibold text-nexus-600">Record login</span> — log in once by hand and we turn it into a recipe every member can replay. (Or derive one from a configured form-login.)</p>}
        <div className="space-y-1">
          {recipes.map((r) => (
            <div key={r.recipe_id} className="flex items-center gap-2 text-[12px] rounded-md bg-nexus-50/60 px-2.5 py-1.5">
              <span className="font-semibold text-nexus-800">v{r.version}</span>
              <span className={`text-[10px] px-1.5 py-0.5 rounded ${r.status === 'active' ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>{r.status}</span>
              {r.source === 'login_recording' && <span className="text-[10px] px-1.5 py-0.5 rounded bg-nexus-100 text-nexus-700 font-semibold">recorded</span>}
              <span className="text-[10px] text-nexus-400">{r.step_count} steps · slots {(r.slots || []).map((s: any) => s.name).join(', ')}</span>
              {r.verified_at && <span className="ml-auto text-[10px] text-emerald-600">verified</span>}
            </div>
          ))}
        </div>
      </Section>
    </div>
  );
}
