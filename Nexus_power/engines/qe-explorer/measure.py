import sys, os, time, asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests"))
import test_fill_engine_e2e as T
from app.fill_engine.persona import derive_persona
from app.fill_engine.learning import identity_seed

# 1 · validated completion, first-pass rate, widget coverage, alert suppression
app, r, built = T.run()
answered = r.first_pass + r.repaired
print("validated completion   : %d/%d fields ACCEPTED (0 attempted-but-unverified)"
      % (answered, answered + r.intent_unmet + len(r.repair_failed)))
print("first-pass rate        : %.0f%%" % (100.0 * r.first_pass / max(1, answered)))
print("widget coverage        : %d/%d classes answered %s"
      % (len(r.widgets_answered), len(r.widgets_met), sorted(r.widgets_answered)))
print("alerts suppressed      : %d (each one used to fail a field)" % r.alerts_suppressed)
print("verdict reads (clean)  : %d" % r.verdict_reads)

# 2 · repair, against a rule the DOM never declared
app2, r2, _ = T.run(T.FakeApplication(reject={
    "age": (lambda v: v.isdigit() and int(v) >= 40, "Age must be at least 40 for this product")}))
att = [e for e in r2.field_ledger if e.get("name") == "Age"][0]["repair"]["attempt_count"]
print("repair success rate    : %d/%d rejected fields accepted" % (r2.repaired, r2.repaired))
print("avg repair attempts    : %.1f" % att)
print("verdict reads (1 error): %d  <- paid only on suspicion" % r2.verdict_reads)

# 3 · identity consistency + cross-crawl stability
seeds = [identity_seed("t%d" % (i % 9), "app-%d" % i) for i in range(500)]
coherent = sum(1 for s in seeds if derive_persona(s).is_coherent())
print("identity consistency   : %d/%d personas pass every cross-field rule" % (coherent, len(seeds)))
stable = sum(1 for s in seeds[:100]
             if derive_persona(s).as_dict() == derive_persona(s).as_dict())
print("cross-crawl stability  : %d/100 identical on re-derivation" % stable)
distinct = len({derive_persona(s).applicant.full_name for s in seeds})
print("application isolation  : %d distinct applicants over %d applications" % (distinct, len(seeds)))

# 4 · latency
t0 = time.perf_counter()
for _ in range(20):
    T.run()
print("latency                : %.1f ms per 20-field page (fill+verdict, fake port)"
      % ((time.perf_counter() - t0) / 20 * 1000))
