/**
 * LoginPage — the token-first sign-in. QE-Central validates a JWT minted by
 * platform-api (audience `vkpower-verdict`); the operator pastes that token here.
 * In mock mode a one-click demo principal is offered. No password is handled and
 * no secret is stored beyond the session token.
 */
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { KeyRound, PlayCircle } from 'lucide-react';

import { AuthError, useAuth } from '../lib/auth';
import { QEC_AUDIENCE, QEC_MOCK } from '../lib/config';
import { Button, Panel, VkMark } from '../components';

export function LoginPage() {
  const { loginWithToken, loginMock } = useAuth();
  const navigate = useNavigate();
  const [token, setToken] = useState('');
  const [error, setError] = useState<string | null>(null);

  const signIn = () => {
    setError(null);
    try {
      loginWithToken(token);
      navigate('/', { replace: true });
    } catch (err) {
      setError(err instanceof AuthError ? err.message : 'Could not sign in with that token.');
    }
  };

  const demo = () => {
    loginMock();
    navigate('/', { replace: true });
  };

  return (
    <div className="min-h-full flex items-center justify-center bg-bg px-4 py-12">
      <div className="w-full max-w-md">
        <div className="flex flex-col items-center text-center mb-6">
          <VkMark size={56} title="VKPower Verdict" />
          <h1 className="text-xl font-semibold text-ink mt-4 tracking-tight">
            VKPower <span className="text-teal">Verdict</span>
          </h1>
          <p className="text-xs text-ink-low mt-1 font-mono">the verdict on every release — certified, or refused</p>
        </div>

        <Panel tone="elevated" className="space-y-4">
          <div>
            <label htmlFor="token" className="block text-xs font-semibold text-ink-mid mb-1.5">
              Principal token (JWT)
            </label>
            <textarea
              id="token"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              rows={4}
              spellCheck={false}
              placeholder="Paste the JWT minted for you by platform-api…"
              className="w-full rounded-lg bg-inset text-ink text-xs font-mono ring-1 ring-line focus-visible:ring-teal/60 px-3 py-2.5 resize-none break-all"
            />
            <p className="text-2xs text-ink-faint mt-1.5">
              Must carry a <span className="font-mono text-ink-low">tenant_id</span> claim and audience{' '}
              <span className="font-mono text-ink-low">{QEC_AUDIENCE}</span>. The signature is verified server-side.
            </p>
          </div>

          {error && (
            <div role="alert" className="text-xs text-crit bg-crit/10 ring-1 ring-crit/25 rounded-lg px-3 py-2">
              {error}
            </div>
          )}

          <Button variant="primary" size="md" className="w-full" onClick={signIn} disabled={!token.trim()} icon={<KeyRound size={15} />}>
            Sign in
          </Button>

          {QEC_MOCK && (
            <>
              <div className="flex items-center gap-3 text-2xs text-ink-faint">
                <span className="h-px flex-1 bg-line" />
                MOCK MODE
                <span className="h-px flex-1 bg-line" />
              </div>
              <Button variant="secondary" size="md" className="w-full" onClick={demo} icon={<PlayCircle size={15} />}>
                Continue with demo data
              </Button>
            </>
          )}
        </Panel>
      </div>
    </div>
  );
}

export default LoginPage;
