# QE-Central — Client Onboarding Input Contract

**Status:** v1 (2026-07-07). Product of a 6-lens + synthesis design sweep (`wf_efa25b94-e5d`). This is the "front door" checklist — what a client must give us before their app can be onboarded. Written in plain language; the *Acme Life Insurance* column is the model answer.

**The founder's starting proposal was 3 inputs:** (1) flow starting URL, (2) test data/credentials if required, (3) GitLab repo. **Verdict: the right core, but ~1/3 of the real contract is missing** — each of the three hides fine print, and two whole categories (**Answers** and **Safety**) were absent. Those two happen to carry the entire trust story.

---

## The 6 buckets (hold these in your head)

**ACCESS · CODE · DATA · ANSWERS · SAFETY · OPS**

The original three fold in: URL → lives inside *Safety & Scope*; credentials → *Access*; repo → *Code*. The missing half is *Answers* and *Safety*.

---

## MUST-HAVE — cannot onboard a single real client without these (ranked by how fast/badly things break)

| # | Input | Why | Acme example |
|---|---|---|---|
| 1 | **Which environment — written "this is NOT production"** | The bot *writes* (fills & submits). A URL doesn't say if it's a safe copy or the live site. | "Test `uat.acmelife.com`, refreshed nightly, wired to vendor sandboxes. Never touch `www.acmelife.com`." |
| 2 | **Written permission to test (signed rules of engagement)** | Automated traffic at a regulated app without sign-off is legally an attack. | Signed CISO letter authorizing UAT testing, 10pm–6am, excluding the payment gateway, valid 12 months. |
| 3 | **Network reachability (VPN / IP allow-list / not bot-blocked)** | Correct URL is useless if the explorer is refused at the door. Most common real-world blocker. | Allow-list our IPs in the firewall, grant VPN, pre-warn their SOC. |
| 4 | **A real login story — MFA + one account per role** | One password gets nothing: a second factor on every login, and each role sees a different product. | A programmatic way to read the texted 6-digit code, plus ~5 role accounts (applicant, policyholder, agent, underwriter, admin). |
| 5 | **The answer key (the oracle) — what "correct" actually is** | *The biggest gap and the heart of the pitch.* The repo shows *how* a premium is computed, never whether it's *right*. That truth lives outside code. | "35-yr non-smoker, $500k, 20-yr term, TX = $31.40/mo. BMI > 40 auto-declines. TX free-look = 20 days." |
| 6 | **Which flows are in scope, and where each starts** | A starting URL is one door; a real app has dozens of journeys. | "Test quote-to-bind (public quote page) and beneficiary-change (inside the portal). Ignore marketing." |
| 7 | **A "never click this" list — the point of no return** | An unfenced bot triggers irreversible real actions even in lower envs: bind, charge, e-sign, pull credit. | "Stop at payment review; never press Bind / Submit-to-Underwriting / Send-to-E-Sign; never open admin." |
| 8 | **Downstream systems stubbed + our traffic tagged** | A "staging" copy still wired to real payment/credit/e-sign/email vendors is dangerous; untagged synthetic activity poisons fraud models & dashboards. | UAT points payments/credit at vendor test modes; all email/SMS routes to a mailbox we control; records tagged synthetic. |
| 9 | **Reset-to-clean + pre-seeded state per flow** | Regression = same flow forever, but submitting flows change the world → tomorrow starts poisoned; valuable flows need a customer already deep in a state machine. | Nightly UAT rebuild + fixtures: an in-force policy to change, a policy lapsed <90 days to reinstate, a 200-item claim queue. |
| 10 | **Valid synthetic identities (format-valid AND compliance-approved)** | Insurance forms hard-validate SSNs/cards/routing/ZIP-state; random junk is rejected at field one; real PII is illegal. | SSNs from Acme's approved synthetic range that pass their validator; sandbox test cards (success + decline); fresh emails. |
| 11 | **Consumable data as a pool, not one value** | Some data is spent on use (one-time SSN, single-use e-sign token); one hardcoded value works once then fails forever. | A pool of never-before-seen synthetic SSNs + fresh e-sign tokens minted each run. |
| 12 | **Real code read-access + which version is deployed** | A link grants nothing on a private repo; the main branch is usually ahead of what the test site runs. | Read-only robot account scoped to portal projects (with rotation) + "UAT runs Release 14, not latest." |
| 13 | **All the codebases — and where the real rules live** | A real insurance app is never one repo; money/eligibility rules live in a back-end, rules service, or vendor platform (Guidewire/Pega) that isn't clonable. | "Portal = 4 codebases + a rules service; auto-approval limit lives there; auto-underwriting runs inside Guidewire." |
| 14 | **Where evidence may live + what to redact** | Dossiers screenshot SSNs & medical answers — themselves regulated data; regulated buyers require on-prem/own-tenant. Hard procurement gate. | "Blank SSN & health answers from every screenshot; all evidence stays in Acme's cloud; nothing transits third-party SaaS." |
| 15 | **Named humans: domain SME, escalation contact, kill switch** | An always-on autonomous system needs someone to adjudicate "is this correct?", someone to page on a real break, and a way to halt in minutes. | Jane (Chief Underwriter) confirms ambiguous rules; Sev-1 pages QE on-call; their 24/7 center can stop all activity in 5 min. |

---

## SHOULD-HAVE — needed to scale and to close regulated buyers

- **Session limits** — login validity + whether accounts can be shared (so mid-test logouts aren't misread as bugs). *Acme: 10-min idle timeout, no two sessions per account.*
- **Secret handling + rotation** — a vault reference, not a text box, plus rotation cadence + an automated pickup channel. *Acme: password rotates every 30 days.*
- **Third-party sandbox "magic" values** — inputs that deterministically trigger approved/declined/fraud paths. *Acme: a LexisNexis test SSN returning "verified," another returning "fraud alert."*
- **Negative / guardrail scenarios** — what the app must *refuse* (the compliance surface a happy-path crawl never finds). *Acme: terminal-illness disclosure must be declined; a 16-year-old can't buy adult term.*
- **Business-criticality ranking** — what pages someone at 3am vs. what's cosmetic. *Acme: "premium posts correctly" = Tier-1; FAQ layout = Tier-3.*
- **Jurisdiction & product scope** — which states/product lines; the same flow is *correct differently* per state. *Acme: Term & Whole Life in TX/FL/CA; NY out of scope.*
- **A deploy/change signal** — a ping when a new build is live + a way to mark an *intended* change so a shipped rate update isn't flagged as a regression. *Acme: "Release 15 touched beneficiary rules"; "Q3 rate rise on Jul 15 is expected."*
- **Which folders / which code system** — which folders in a mono-repo are this app; and if it isn't GitLab (often it isn't), which system. *Acme: 60 services in one repo, portal is one folder; they're on self-hosted Bitbucket.*
- **Time-relative data rules** — insurance is date-driven; fixed-date fixtures rot. *Acme: "premium past due" needs due-date = today−45 days, regenerated each run.*
- **Report routing, cadence, retention, SLAs** — who gets what, how often, kept how long, which compliance standard. *Acme: nightly 2am, defects to Jira, dossier PDF to the CCO, tamper-evident, 7-year retention for the NAIC Model Audit Rule.*
- **Known-flaky zones + blackout windows** — areas to quarantine + periods when no testing is allowed. *Acme: quarantine the sandbox Plaid iframe; no traffic Nov 1–Dec 15.*

---

## Is defining the inputs the first step?

**Yes — for onboarding.** This contract is the front door; no client run can start until we know how to get in, what's safe to touch, and what "correct" means. As the thing a *customer* does first, the founder is right, and this checklist turns onboarding from a per-client scramble into a repeatable form.

**But the first thing to BUILD is the honesty harness, not the intake.** The whole pitch is "we refuse to green-wash" — a claim about the *engine*, provable on data we already have, before a single client secret changes hands. Build order: prove the engine tells the truth (given a correct answer it fails a wrong result; given no answer it refuses to certify rather than green-washing), *then* build the intake that feeds it real answers. The two even meet: an honest engine with no answer key *must* say "I can't certify this" — and that refusal is the strongest sales demo, and it's what proves bucket #4 (Answers) is non-negotiable.

**Genuine very-next action:** write this contract as a one-page onboarding checklist (done — this doc), *then* build the refuse-harness against existing data. That order keeps us honest and moving.
