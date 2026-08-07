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

import { Button, Panel, Pill, SectionHead } from '../../components';
import { api } from '../../lib/api';
import factoryApi from '../../studio/factoryApi';
import { useAsync } from '../../lib/useAsync';
import type { ClientApp, ExplorationCoverage } from '../../types/qec';

type Health = 'expired' | 'recorded' | 'none';

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

  // `recorded` means this app can sign itself in — by stored credentials or by a
  // recorded login. `expired` outranks both: a crawl PROVED it could not sign in.
  const health: Health = sessionExpired
    ? 'expired'
    : (app?.has_credentials || stepCount > 0) ? 'recorded' : 'none';

  const [liveUrl, setLiveUrl] = useState('');
  const [busy, setBusy] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

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

  const start = async () => {
    if (!app) return;
    setBusy('start');
    try {
      let r;
      try {
        r = await factoryApi.startRecording(app.base_url);
      } catch (first: unknown) {
        // The recorder browser is single-occupancy. A 409 means an earlier
        // recording was left open — a double-click, or a tab closed mid-way.
        // Recoverable, and the operator should not have to know why.
        const e = first as { response?: { status?: number } };
        if (e?.response?.status !== 409) throw first;
        await factoryApi.cancelRecording().catch(() => {});
        r = await factoryApi.startRecording(app.base_url);
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
      toast.success('Login re-recorded', {
        description: 'The next crawl starts logged in. Crawl again to cover the authenticated app.',
      });
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
    pill: string; tone: 'crit' | 'good' | 'neutral'; icon: JSX.Element;
    headline: string; detail: string;
  }> = {
    expired: {
      pill: 'Sign-in broken', tone: 'crit',
      icon: <ShieldAlert size={16} className="text-crit" />,
      headline: 'The last crawl could not sign in, so it only covered public pages.',
      detail: 'This app signs in with a recorded session, and that session has expired. '
        + 'Add the sign-in details below and it will not happen again — unlike a recorded '
        + 'session, they do not expire.',
    },
    recorded: {
      pill: 'Signs in', tone: 'good',
      icon: <ShieldCheck size={16} className="text-good" />,
      headline: 'This app can sign itself in. Nothing to do.',
      detail: 'Come back here if the sign-in itself changes — a new password, or a '
        + 'different login screen.',
    },
    none: {
      pill: 'Not set up', tone: 'neutral',
      icon: <KeyRound size={16} className="text-ink-mid" />,
      headline: 'Crawls of this app run signed out, so anything behind the sign-in is not tested.',
      detail: 'Add the sign-in details a tester would use. The crawl then signs in by '
        + 'itself, including part-way through a journey.',
    },
  };
  const st = STATE[health];

  return (
    <Panel tone="elevated">
      <SectionHead
        title="Signing in"
        subtitle="how a crawl gets past this app's sign-in screen"
        icon={st.icon}
        right={<Pill tone={st.tone} size="sm">{st.pill}</Pill>}
      />

      <p className="mt-3 text-sm font-semibold text-ink">{st.headline}</p>
      <p className="mt-1 text-sm text-ink-mid">{st.detail}</p>

      {liveUrl ? (
        <div className="mt-4 space-y-2">
          <p className="text-sm font-semibold text-ink">Sign in below, then press Save.</p>
          <iframe
            src={liveUrl}
            title="Sign in to record the login"
            className="h-[460px] w-full rounded-lg ring-1 ring-line-strong"
          />
          <p className="text-xs text-ink-mid">
            If this opens already signed in, sign out first — there is no login to
            learn otherwise. We record which boxes you fill and which buttons you
            press, never what you type.
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
          {/* STEP 1 — what almost every app needs. */}
          <div className="mt-4 rounded-lg bg-panel-2 p-3 ring-1 ring-line-strong">
            <p className="text-sm font-semibold text-ink">
              {health === 'recorded' ? 'Update the sign-in details' : 'Enter the sign-in details'}
            </p>
            <p className="mt-1 text-xs text-ink-mid">
              The username and password a tester uses on this app. Stored encrypted,
              never shown again, and never written into a test or a report.
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
                variant="primary"
                loading={busy === 'creds'}
                disabled={!username || !password}
                onClick={saveCredentials}
              >
                Save and use this
              </Button>
            </div>
          </div>

          {/* STEP 2 — the exception, deliberately quieter. */}
          <div className="mt-3 border-t border-line pt-3">
            <p className="text-sm text-ink-mid">
              <span className="font-semibold text-ink">Can't sign in with just a username and password?</span>{' '}
              If this app sends a one-time code, redirects to a company sign-on page,
              or shows a "prove you're human" check, sign in once by hand and we will
              learn the steps.
            </p>
            <div className="mt-2">
              <Button
                variant="secondary"
                icon={<Video size={14} />}
                loading={busy === 'start'}
                disabled={crawlActive}
                onClick={start}
              >
                Sign in by hand instead
              </Button>
              {crawlActive && (
                <span className="ml-2 text-xs text-ink-mid">
                  A crawl is running — wait for it to finish.
                </span>
              )}
            </div>
          </div>
        </>
      )}
    </Panel>
  );
}
