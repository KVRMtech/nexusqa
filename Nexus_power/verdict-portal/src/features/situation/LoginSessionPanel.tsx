/**
 * LOGIN SESSION panel — the recorded login, its health, and the one-click repair.
 *
 * A login is recorded ONCE by hand and its captured session is replayed on every
 * later crawl. Sessions expire on the application's own schedule, so this is not
 * an edge case: every authenticated app eventually reaches a day when its crawl
 * silently walks the logged-OUT product. The crawler now says so
 * (`coverage.auth_incomplete` / `auth_reason='session_expired'`); this panel is
 * where the operator SEES it and fixes it, without re-registering the app.
 *
 * We record WHICH fields are filled and WHICH controls are pressed — never the
 * values typed. The session itself rides the ENCRYPTED credential blob, and is
 * merged in server-side so refreshing it cannot destroy a stored username or
 * password.
 */
import { useState } from 'react';
import { KeyRound, ShieldAlert, ShieldCheck, Video } from 'lucide-react';
import { toast } from 'sonner';

import { Button, LoginSlotFields, Panel, Pill, SectionHead } from '../../components';
import type { LoginSlot } from '../../components';
import { api } from '../../lib/api';
import factoryApi from '../../studio/factoryApi';
import { useAsync } from '../../lib/useAsync';
import type { ClientApp, ExplorationCoverage } from '../../types/qec';

/** `session_only` is the state that used to masquerade as `recorded`: a login was
 *  recorded, so a SESSION exists, but no username/password the crawl can replay. It
 *  works until the session dies — and for an app whose login lives in client-side
 *  state it never works at all. It is called out so the operator sees the real choice
 *  (recording = one crawl, credentials = every crawl) instead of "nothing to do". */
type Health = 'expired' | 'recorded' | 'session_only' | 'none';

/** Entry-path tokens that typically sit behind a sign-in. A cheap boost signal
 *  only — it merely ESCALATES the "no login attached" copy; the recommended action
 *  (sign in once) is harmless if the app turns out to be public. */
// Generic gated-area path tokens only — NEVER domain-specific ones (a login-detection
// heuristic must work on ANY app, not just insurance). It only softly HINTS before a
// crawl; coverage evidence always overrides it.
const LOGIN_PATH_TOKENS = [
  '/portal', '/account', '/member', '/dashboard',
  '/profile', '/settings', '/secure', '/my',
];
function pathLikelyBehindLogin(baseUrl?: string): boolean {
  try {
    const p = new URL(baseUrl || '').pathname.toLowerCase();
    return LOGIN_PATH_TOKENS.some((t) => p === t || p.startsWith(`${t}/`));
  } catch {
    return false;
  }
}

export default function LoginSessionPanel({ appId }: { appId: string }) {
  const appState = useAsync((signal) => api.getApp(appId, { signal }), [appId]);
  const app = appState.data;
  const explorationId = app?.crawl?.exploration_id;
  const crawlActive = app?.crawl?.active ?? false;

  // The last crawl's own verdict on whether it reached the authenticated app.
  // Skipped while a crawl is in flight — a half-written coverage record would
  // flash a scary banner that resolves itself seconds later.
  const exploration = useAsync(
    (signal) =>
      explorationId && !crawlActive
        ? api.getExploration(explorationId, { signal })
        : Promise.resolve(null),
    [explorationId, crawlActive],
  );

  const coverage = (exploration.data?.stats as { coverage?: ExplorationCoverage } | undefined)
    ?.coverage;
  const sessionExpired = coverage?.auth_incomplete === true
    && coverage?.auth_reason === 'session_expired';
  const stepCount = (app?.login_recording?.steps as unknown[] | undefined)?.length ?? 0;

  // Can the crawl sign ITSELF in (a username+password / auth hook it replays), or does
  // it only hold a recorded session? `has_credentials` cannot answer that — it is true
  // for a blob holding nothing but a session, which is why this panel used to promise
  // "this app can sign itself in, nothing to do" for an app that never could. Fall back
  // to the old flag only when the server is older than `can_sign_in`.
  const canSignIn = app?.can_sign_in ?? app?.has_credentials ?? false;
  const hasSession = app?.has_session ?? stepCount > 0;

  // `expired` outranks everything: a crawl PROVED it could not sign in.
  const health: Health = sessionExpired
    ? 'expired'
    : canSignIn
      ? 'recorded'
      : (hasSession || stepCount > 0) ? 'session_only' : 'none';

  // Distinguish EVIDENCE from a HINT. The crawl's own coverage is authoritative: it
  // reported it was blocked / could not cover the authenticated app (auth_blocked /
  // auth_incomplete). A SUCCESSFUL crawl that covered forms and reported no auth trouble
  // PROVES the entry is reachable public content — never claim it is gated then. The
  // entry PATH is only a HINT (a nudge before any crawl exists), never proof.
  const coverageSaysBlocked =
    coverage?.auth_blocked === true || coverage?.auth_incomplete === true;
  const coverageSaysPublic =
    !!coverage && !coverageSaysBlocked && (coverage.forms_found ?? 0) > 0;
  const pathHint = pathLikelyBehindLogin(app?.base_url) && !coverageSaysPublic;

  const [liveUrl, setLiveUrl] = useState('');
  const [busy, setBusy] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  // Set when a recording proved this app's session cannot be carried over,
  // so the credential fields stop being an optional aside and become THE
  // next step, open and explained.
  const [needsCredentials, setNeedsCredentials] = useState(false);

  // The fields this app's RECORDING watched the login ask for. A recording never
  // captures what you type, so filling these once is what turns it from "one session"
  // into a login every FUTURE crawl replays — and it is the only place a login that
  // asks for an MFA code can be completed.
  const recipe = (app?.login_recording ?? {}) as { slots?: unknown[]; slot_names?: unknown[] };
  const recipeSlots = (
    recipe.slots?.length ? recipe.slots : recipe.slot_names ?? []
  ) as LoginSlot[];
  const [slotValues, setSlotValues] = useState<Record<string, string>>({});
  const slotFilled = Object.values(slotValues).some((v) => String(v || '').trim());

  const saveSlotValues = async () => {
    if (!app) return;
    setBusy('slots');
    try {
      await api.replaceLoginRecording(app.app_id, { slot_values: slotValues });
      setSlotValues({});
      toast.success('Sign-in details saved', {
        description: 'Every future crawl now signs itself in — no session to expire.',
      });
      appState.reload();
    } catch (err: unknown) {
      const e = err as { response?: { data?: { detail?: string } }; message?: string };
      toast.error('Could not save sign-in details', {
        description: e?.response?.data?.detail || e?.message || 'Unknown error.',
      });
    } finally {
      setBusy('');
    }
  };

  /**
   * Credentials are what let a crawl re-authenticate BY ITSELF when it meets a
   * sign-in wall part-way through a journey (public quote → authenticated apply).
   * A recorded session cannot do that — it is a snapshot that expires. Until now
   * they could only be set at onboarding, so an app whose session died could not
   * be repaired without re-registering it and stranding its catalogue.
   */
  const saveCredentials = async () => {
    if (!app) return;
    setBusy('creds');
    try {
      await api.replaceLoginRecording(app.app_id, { username, password });
      setUsername(''); setPassword('');
      toast.success('Credentials saved', {
        description: 'Crawls can now sign in on their own when a journey crosses a login.',
      });
      appState.reload();
    } catch (err: unknown) {
      const e = err as { response?: { data?: { detail?: string } }; message?: string };
      toast.error('Could not save credentials', {
        description: e?.response?.data?.detail || e?.message || 'Unknown error.',
      });
    } finally {
      setBusy('');
    }
  };

  /**
   * Where to open the recorder.
   *
   * The app's Base URL is where the PRODUCT starts, which is almost never where
   * signing in happens — for a quote funnel it is the quote page. Opening there
   * made operators hunt for the sign-in themselves, and reasonably read the
   * recorder as broken when their quote page appeared.
   *
   * A login already recorded for this domain knows the answer: its first `goto`
   * step IS the sign-in path. Best-effort in every direction — no recipe, an
   * unparseable one, or a lookup failure all fall back to the Base URL, which is
   * exactly the old behaviour. Finding the sign-in must never block recording it.
   */
  const recorderStartUrl = async (): Promise<string> => {
    if (!app) return '';
    try {
      const origin = new URL(app.base_url).origin;
      const res = await factoryApi.reuseCheck({ domain: new URL(app.base_url).hostname });
      for (const recipe of (res?.recipes || res?.library || []) as Array<Record<string, unknown>>) {
        const direct = String(recipe?.login_path || '');
        const fromSteps = ((recipe?.steps || []) as Array<Record<string, unknown>>)
          .find((s) => String(s?.action || '') === 'goto');
        const path = direct || String(fromSteps?.path || '');
        if (path.startsWith('/')) return origin + path;
      }
    } catch {
      /* fall through to the Base URL */
    }
    return app.base_url;
  };

  const start = async () => {
    if (!app) return;
    setBusy('start');
    try {
      const startUrl = await recorderStartUrl();
      let r;
      try {
        r = await factoryApi.startRecording(startUrl);
      } catch (first: unknown) {
        // The recorder browser is single-occupancy. A 409 means an earlier
        // recording was left open — a double-click, or a tab closed mid-way.
        // Recoverable, and the operator should not have to know why.
        const e = first as { response?: { status?: number } };
        if (e?.response?.status !== 409) throw first;
        await factoryApi.cancelRecording().catch(() => {});
        r = await factoryApi.startRecording(startUrl);
      }
      if (!r?.live_url) throw new Error('the recorder did not return a live view');
      setLiveUrl(r.live_url);
    } catch (err: unknown) {
      const e = err as { response?: { status?: number; data?: { detail?: string } }; message?: string };
      toast.error('Could not start recording', {
        description:
          e?.response?.status === 403
            ? 'Recording needs an editor, manager or admin role on this tenant.'
            : e?.response?.status === 502
              ? 'The recorder browser is unreachable — it may be mid-restart. Try again shortly.'
              : e?.response?.data?.detail || e?.message || 'Unknown error.',
      });
    } finally {
      setBusy('');
    }
  };

  const save = async () => {
    if (!app) return;
    setBusy('save');
    try {
      const r = await factoryApi.saveRecording();
      setLiveUrl('');
      if (!r?.session && !r?.login) {
        toast.warning('Nothing to save', {
          description:
            r?.reason === 'no_credential_fields_observed'
              ? 'No login fields were filled inside the recorder.'
              : `No login captured: ${r?.reason || 'unknown'}.`,
        });
        return;
      }
      await api.replaceLoginRecording(app.app_id, {
        ...(r.usable && r.login ? { login_recording: r.login } : {}),
        ...(r.session ? { session: r.session } : {}),
      });
      // A session with no cookies and no origins is EMPTY. Some apps keep their
      // session in sessionStorage, which a storageState capture cannot see, so the
      // steps are learned but nothing can carry the sign-in into a crawl. Claiming
      // "the next crawl starts logged in" here sent the operator off to run a crawl
      // that was silently signed out — observed on a Next.js app that reported
      // cookies=0 origins=0 while the login itself was recorded fine.
      // Mirrors nexus_sdk.session.session_has_substance (the Python canonical).
      // Kept as a local because TypeScript cannot import the Python helper — a
      // grep for `session_has_substance` / `__nx_session_storage` surfaces every
      // copy. Counting only cookies-and-origins is the bug this check exists to
      // catch: an app that keeps its sign-in in sessionStorage has neither, and
      // its session rides `__nx_session_storage`.
      const st = r.session as {
        cookies?: unknown[]; origins?: unknown[]; __nx_session_storage?: unknown[];
      } | undefined;
      const carriesSession = !!(
        st?.cookies?.length || st?.origins?.length || st?.__nx_session_storage?.length
      );
      if (carriesSession) {
        toast.success('Sign-in saved', {
          description: 'The next crawl starts signed in.',
        });
      } else {
        setNeedsCredentials(true);
        toast.warning('Signed in, but this app’s session cannot be carried over', {
          description: 'We learned the steps. This app keeps its sign-in somewhere a '
            + 'saved session cannot reach, so add a username and password below and '
            + 'the crawl will sign itself in.',
          duration: 15000,
        });
      }
      appState.reload();
      exploration.reload();
    } catch (err: unknown) {
      const e = err as { response?: { data?: { detail?: string } }; message?: string };
      toast.error('Could not save the recording', {
        description: e?.response?.data?.detail || e?.message || 'Unknown error.',
      });
    } finally {
      setBusy('');
    }
  };

  const abort = async () => {
    setLiveUrl('');
    await factoryApi.cancelRecording().catch(() => {});
  };

  /**
   * The panel states an operator can be in, and for each one the SINGLE next
   * action. An earlier version offered "record a login" and "add credentials"
   * side by side with no guidance, which left the operator to work out which
   * mechanism their app needed — a choice they have no basis to make and which
   * we can make for them. Sign-in details are the default because they do not
   * expire; recording is the exception for logins nobody can type.
   */
  const STATE: Record<Health, {
    pill: string; tone: 'crit' | 'good' | 'warn' | 'neutral'; icon: JSX.Element;
    headline: string; detail: string;
  }> = {
    expired: {
      pill: 'Sign-in broken', tone: 'crit',
      icon: <ShieldAlert size={16} className="text-crit" />,
      headline: 'The last crawl could not sign in, so it only covered public pages.',
      // The remediation DEPENDS on what is stored. With no username/password, "record
      // again" is a loop that cannot end — the next recording captures another session,
      // and some apps can never restore one. Say the durable fix instead.
      detail: canSignIn
        ? 'The app has ended the session you signed in with. Sign in once more '
          + 'below and crawling resumes where it left off.'
        : 'This app has only a recorded session, and the app would not accept it. '
          + 'Add a username and password below so the crawl signs itself in — '
          + 'recording again just captures another session that can fail the same way.',
    },
    recorded: {
      pill: 'Signs in', tone: 'good',
      icon: <ShieldCheck size={16} className="text-good" />,
      headline: 'This app can sign itself in. Nothing to do.',
      detail: 'Come back here if the sign-in itself changes — a new password, or a '
        + 'different login screen.',
    },
    // A login WAS recorded, so a session exists — but nothing the crawl can replay.
    // Honest, and states the choice plainly rather than implying it is handled.
    session_only: {
      pill: 'Session only', tone: 'warn',
      icon: <ShieldAlert size={16} className="text-warn" />,
      headline: 'This app has a recorded session, but nothing it can sign in with on its own.',
      detail: 'A recording captures the steps and a session — never what you type — so '
        + 'the session is a snapshot: it expires, and some apps cannot restore one at '
        + 'all. Add a username and password below and every future crawl signs itself in.',
    },
    none: {
      pill: 'Not set up', tone: 'neutral',
      icon: <KeyRound size={16} className="text-ink-mid" />,
      headline: 'Crawls of this app run signed out, so anything behind the sign-in is not tested.',
      detail: 'Sign in once below and every crawl from then on starts signed in — '
        + 'including when a journey reaches the sign-in part-way through.',
    },
  };
  const st = STATE[health];
  const entryPath = (() => {
    try { return new URL(app?.base_url || '').pathname; } catch { return ''; }
  })();
  // Two honest escalations of the passive 'none' state — never a claim the code cannot
  // back. EVIDENCE (the crawl reported it was blocked at the sign-in) → a definitive,
  // crit-tone "Login required". HINT only (the entry PATH looks gated but no crawl has
  // proven it) → a tentative nudge in the neutral tone, never asserted as fact.
  let view = st;
  if (health === 'none' && coverageSaysBlocked) {
    view = {
      pill: 'Login required',
      tone: 'crit' as const,
      icon: <ShieldAlert size={16} className="text-crit" />,
      headline: entryPath
        ? `The last crawl was blocked at this app's sign-in (${entryPath}) — it never reached anything behind the login.`
        : `The last crawl was blocked at this app's sign-in — it never reached anything behind the login.`,
      detail: st.detail,
    };
  } else if (health === 'none' && pathHint) {
    view = {
      ...st,
      headline: entryPath
        ? `This entry path (${entryPath}) looks like it may sit behind a sign-in. If it does, sign in once below so crawls don't stop at the login page.`
        : `This entry may sit behind a sign-in. If it does, sign in once below so crawls don't stop at the login page.`,
    };
  }

  return (
    <Panel tone="elevated">
      <SectionHead
        title="Signing in"
        subtitle="how a crawl gets past this app's sign-in screen"
        icon={view.icon}
        right={<Pill tone={view.tone} size="sm">{view.pill}</Pill>}
      />

      <p className="mt-3 text-sm font-semibold text-ink">{view.headline}</p>
      <p className="mt-1 text-sm text-ink-mid">{view.detail}</p>

      {liveUrl ? (
        <div className="mt-4 space-y-2">
          <p className="text-sm font-semibold text-ink">Sign in below, then press Save.</p>
          <p className="text-xs text-ink-mid">
            If this is not the sign-in page, click through to it as a person
            would — that path is part of what we learn. If it opens already signed
            in, sign out first; there is no login to learn otherwise.
          </p>
          <iframe
            src={liveUrl}
            title="Sign in to record the login"
            className="h-[460px] w-full rounded-lg ring-1 ring-line-strong"
          />
          <p className="text-xs text-ink-mid">
            We record which boxes you fill and which buttons you press, never what
            you type.
          </p>
          <div className="flex gap-2">
            <Button variant="primary" loading={busy === 'save'} onClick={save}>
              Save
            </Button>
            <Button variant="ghost" onClick={abort}>Cancel</Button>
          </div>
        </div>
      ) : (
        <>
          {/* PRIMARY — signing in by hand. It asks the operator for nothing they
              do not already do, and it is the only path that works for a one-time
              code, a company sign-on page or a human check. */}
          <div className="mt-4 rounded-lg bg-panel-2 p-3 ring-1 ring-line-strong">
            <p className="text-sm font-semibold text-ink">Sign in once, here</p>
            <p className="mt-1 text-xs text-ink-mid">
              A browser opens on this app's sign-in page — or on its start page, if
              we have not seen this app's sign-in before. Sign in exactly as you
              normally would, including any code sent to you, then press Save. We
              watch which boxes you fill and which buttons you press, never what you
              type.
            </p>
            <div className="mt-2">
              <Button
                variant="primary"
                icon={<Video size={14} />}
                loading={busy === 'start'}
                disabled={crawlActive}
                onClick={start}
              >
                {health === 'none' ? 'Sign in now' : 'Sign in again'}
              </Button>
              {crawlActive && (
                <span className="ml-2 text-xs text-ink-mid">
                  A crawl is running — wait for it to finish.
                </span>
              )}
            </div>
          </div>

          {/* SECONDARY — the durability top-up. Honest about WHY it exists rather
              than presenting itself as an equal alternative: today a signed-in
              session eventually lapses, and only a username/password lets the
              crawl sign itself back in without anyone being asked again. */}
          {/* COMPLETE THE RECORDING — the fix for "record once" meaning one crawl. The
              recording already knows WHICH fields this login needs; supplying their
              values makes every future crawl sign itself in, and is the only way to give
              a login that asks for an MFA code what it needs. */}
          {recipeSlots.length > 0 && !canSignIn && (
            <div className="mt-3 rounded-lg ring-1 ring-teal/40 bg-teal/[0.06] p-3">
              <p className="text-sm font-semibold text-ink">Complete the recorded sign-in</p>
              <p className="mt-1 text-xs text-ink-mid">
                The recording watched this app ask for the fields below, but never what
                you typed. Enter them once and every future crawl signs itself in —
                nothing to expire, and no need to record again. Stored encrypted, never
                shown again, and never written into a test or a report.
              </p>
              <div className="mt-2">
                <LoginSlotFields
                  slots={recipeSlots}
                  values={slotValues}
                  onChange={setSlotValues}
                  inputClassName="w-full rounded-md bg-panel px-2 py-1.5 text-sm text-ink ring-1 ring-line-strong"
                />
              </div>
              <div className="mt-2.5">
                <Button
                  variant="primary"
                  loading={busy === 'slots'}
                  disabled={!slotFilled}
                  onClick={saveSlotValues}
                >
                  Save sign-in details
                </Button>
              </div>
            </div>
          )}

          <details className="mt-3 border-t border-line pt-3" open={needsCredentials}>
            <summary className="cursor-pointer text-sm text-ink-mid">
              <span className="font-semibold text-ink">
                {needsCredentials ? 'Needed for this app:' : 'Optional:'}
              </span>{' '}
              {needsCredentials
                ? 'a username and password, so crawls can sign in'
                : 'save a username and password so you are never asked again'}
            </summary>
            <p className="mt-2 text-xs text-ink-mid">
              Signing in above lasts until this app ends the session — after that we
              ask you once more. If this app signs in with a plain username and
              password, saving them here means the crawl signs itself back in and
              nobody is asked again. Skip this if sign-in needs a code or a company
              sign-on page. Stored encrypted, never shown again, and never written
              into a test or a report.
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <input
                className="w-56 rounded-md bg-panel px-2 py-1.5 text-sm text-ink ring-1 ring-line-strong"
                placeholder="Username or email"
                autoComplete="off"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
              <input
                className="w-56 rounded-md bg-panel px-2 py-1.5 text-sm text-ink ring-1 ring-line-strong"
                placeholder="Password"
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
              <Button
                variant="secondary"
                loading={busy === 'creds'}
                disabled={!username || !password}
                onClick={saveCredentials}
              >
                Save
              </Button>
            </div>
          </details>
        </>
      )}
    </Panel>
  );
}
