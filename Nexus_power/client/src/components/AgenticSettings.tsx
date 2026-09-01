/**
 * AgenticSettings — turn each agentic-QE agent ON/OFF (governance + cost control).
 *
 * Self-contained popover. $0 deterministic agents (Sentinel / Triage / Verdict) default
 * ON; LLM agents (Context / Intent) default OFF. A toggle ONLY gates whether an agent
 * runs — never whether a step passes. Loads/saves via /api/v1/agentic/config; fail-open.
 */
import { useEffect, useState } from 'react';
import { api } from '../services/api';

const AGENT_META: Record<string, { name: string; desc: string; cost: 'free' | 'llm' }> = {
  sentinel: { name: 'Sentinel', desc: 'Auto-diagnose every failure — no click', cost: 'free' },
  triage:   { name: 'Triage',   desc: 'Is it our product, the script, or the environment?', cost: 'free' },
  verdict:  { name: 'Verdict',  desc: 'Fix it / build it / flag it decision', cost: 'free' },
  context:  { name: 'Context',  desc: 'Cross-field reasoner (catches e.g. an invalid value for the chosen country)', cost: 'llm' },
  intent:   { name: 'Intent',   desc: 'Cite the violated requirement in the defect', cost: 'llm' },
};
const ORDER = ['sentinel', 'context', 'triage', 'verdict', 'intent'];

export default function AgenticSettings() {
  const [open, setOpen] = useState(false);
  const [agents, setAgents] = useState<Record<string, boolean>>({});
  const [catalog, setCatalog] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!open || loaded) return;
    api.getAgenticConfig()
      .then((c) => { setAgents(c.agents || {}); setCatalog(c.catalog || []); setLoaded(true); })
      .catch(() => setLoaded(true));
  }, [open, loaded]);

  const toggle = async (k: string) => {
    const next = { ...agents, [k]: !agents[k] };
    setAgents(next);
    setSaving(true);
    try {
      const r = await api.setAgenticConfig(next);
      if (r?.agents) setAgents(r.agents);
    } catch {
      /* keep the optimistic value */
    } finally {
      setSaving(false);
    }
  };

  const keys = (catalog.length ? catalog : ORDER).slice().sort(
    (a, b) => ORDER.indexOf(a) - ORDER.indexOf(b));

  return (
    <div className="relative">
      <button onClick={() => setOpen((o) => !o)}
        title="Turn each agentic-QE agent on/off"
        className="flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-bold text-[#fff]"
        style={{ background: 'linear-gradient(135deg,#4f46e5,#7c3aed)' }}>
        🤖 Agents
      </button>
      {open && (
        <div className="absolute right-0 z-50 mt-1 w-72 rounded-lg border border-slate-200 bg-white shadow-xl p-2">
          <p className="text-[11px] font-black text-slate-800 mb-1.5">Agentic-QE agents</p>
          {keys.map((k) => {
            const m = AGENT_META[k] || { name: k, desc: '', cost: 'free' as const };
            return (
              <label key={k} className="flex items-start gap-2 py-1 cursor-pointer hover:bg-slate-50 rounded px-1">
                <input type="checkbox" checked={!!agents[k]} onChange={() => toggle(k)} className="mt-0.5" />
                <span className="flex-1">
                  <span className="text-[11px] font-bold text-slate-800">{m.name}</span>
                  <span className={`ml-1.5 rounded px-1 text-[8px] font-bold ${m.cost === 'llm' ? 'bg-amber-100 text-amber-700' : 'bg-emerald-100 text-emerald-700'}`}>
                    {m.cost === 'llm' ? 'LLM' : '$0'}
                  </span>
                  <span className="block text-[9.5px] text-slate-500 leading-snug">{m.desc}</span>
                </span>
              </label>
            );
          })}
          <p className="text-[8.5px] text-slate-400 mt-1.5 italic">
            A toggle only gates whether an agent runs — never whether a step passes.{saving ? ' Saving…' : ''}
          </p>
        </div>
      )}
    </div>
  );
}
