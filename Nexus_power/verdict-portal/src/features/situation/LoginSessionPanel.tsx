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

  const health: Health = sessionExpired ? 'expired' : stepCount > 0 ? 'recorded' : 'none';

  const [liveUrl, setLiveUrl] = useState('');
  const [busy, setBusy] = useState('');

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

  const HEAD: Record<Health, { pill: string; tone: 'crit' | 'good' | 'neutral'; icon: JSX.Element }> = {
    expired: { pill: 'Session expired', tone: 'crit', icon: <ShieldAlert size={16} className="text-crit" /> },
    recorded: { pill: `${stepCount} step${stepCount === 1 ? '' : 's'} recorded`, tone: 'good', icon: <ShieldCheck size={16} className="text-good" /> },
    none: { pill: 'No login recorded', tone: 'neutral', icon: <KeyRound size={16} className="text-ink-mid" /> },
  };
  const head = HEAD[health];

  return (
    <Panel tone="elevated">
      <SectionHead
        title="Login session"
        subtitle="the recorded login that gets a crawl past the sign-in wall"
        icon={head.icon}
        right={<Pill tone={head.tone} size="sm">{head.pill}</Pill>}
      />

      {health === 'expired' && (
        <p className="mt-3 text-sm text-ink">
          <span className="font-semibold text-crit">
            The last crawl did not cover the authenticated app.
          </span>{' '}
          The stored session was replayed but the application still presented a
          sign-in wall, so the crawl explored the public pages only. Re-record the
          login to restore authenticated coverage.
        </p>
      )}
      {health === 'none' && (
        <p className="mt-3 text-sm text-ink-mid">
          Crawls of this app run signed out. Record a login once and every later
          crawl starts authenticated.
        </p>
      )}
      {health === 'recorded' && (
        <p className="mt-3 text-sm text-ink-mid">
          The last crawl reached the authenticated app. Re-record if the login
          itself changed, or after a credential rotation.
        </p>
      )}

      {liveUrl ? (
        <div className="mt-3 space-y-2">
          <iframe
            src={liveUrl}
            title="Log in to record the login"
            className="h-[460px] w-full rounded-lg ring-1 ring-line-strong"
          />
          <p className="text-xs text-ink-mid">
            Log in as you normally would, then press Save. We record which fields
            you fill and which controls you press — never the values you type.
          </p>
          <div className="flex gap-2">
            <Button variant="primary" loading={busy === 'save'} onClick={save}>
              Save recording
            </Button>
            <Button variant="ghost" onClick={abort}>Cancel</Button>
          </div>
        </div>
      ) : (
        <div className="mt-3">
          <Button
            variant={health === 'expired' ? 'primary' : 'secondary'}
            icon={<Video size={14} />}
            loading={busy === 'start'}
            disabled={crawlActive}
            onClick={start}
          >
            {health === 'none' ? 'Record login' : 'Re-record login'}
          </Button>
          {crawlActive && (
            <span className="ml-2 text-xs text-ink-mid">
              A crawl is running — wait for it to finish.
            </span>
          )}
        </div>
      )}
    </Panel>
  );
}
