import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { AlertCircle, ArrowRight, Hexagon, Zap } from 'lucide-react';

export default function LoginPage() {
  const { login, isAuthenticated } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  if (isAuthenticated) {
    navigate('/', { replace: true });
    return null;
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      await login(email, password);
      navigate('/');
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Login failed. Please check your credentials.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-gray-950 px-4">
      {/* Animated background */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute -top-32 right-1/4 h-[500px] w-[500px] rounded-full bg-nexus-600/[0.07] blur-[120px] animate-pulse" />
        <div className="absolute -bottom-32 left-1/4 h-[400px] w-[400px] rounded-full bg-purple-600/[0.06] blur-[100px]" />
        {/* Grid pattern */}
        <div
          className="absolute inset-0 opacity-[0.03]"
          style={{
            backgroundImage: 'linear-gradient(rgba(255,255,255,0.1) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.1) 1px, transparent 1px)',
            backgroundSize: '60px 60px',
          }}
        />
      </div>

      <div className="relative w-full max-w-md space-y-8">
        {/* Logo & brand */}
        <div className="text-center">
          <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-2xl bg-gradient-to-br from-nexus-500 to-purple-600 shadow-xl shadow-nexus-500/20">
            <Hexagon className="h-8 w-8 text-white" strokeWidth={1.5} />
          </div>
          <h1 className="mt-5 text-2xl font-bold text-white tracking-tight">
            Nexus <span className="text-transparent bg-clip-text bg-gradient-to-r from-nexus-400 to-purple-400">AI Engine Factory</span>
          </h1>
          <p className="mt-2 text-sm text-gray-500">
            Enterprise QA Platform — Sign in to continue
          </p>
        </div>

        {/* Card */}
        <div className="rounded-2xl bg-gray-900/80 backdrop-blur-sm p-8 ring-1 ring-white/[0.06] shadow-2xl">
          {error && (
            <div className="mb-5 flex items-center gap-2 rounded-xl bg-red-500/10 p-3 text-sm text-red-400 ring-1 ring-red-500/20">
              <AlertCircle className="h-4 w-4 shrink-0" />
              {error}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-5">
            <div>
              <label htmlFor="email" className="block text-xs font-medium text-gray-400 uppercase tracking-wider mb-1.5">
                Email address
              </label>
              <input
                id="email"
                type="email"
                required
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="input-field w-full"
                placeholder="admin@company.com"
              />
            </div>

            <div>
              <label htmlFor="password" className="block text-xs font-medium text-gray-400 uppercase tracking-wider mb-1.5">
                Password
              </label>
              <input
                id="password"
                type="password"
                required
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="input-field w-full"
                placeholder="••••••••"
              />
            </div>

            <button type="submit" disabled={loading} className="btn-primary w-full py-2.5 justify-center">
              {loading ? (
                <div className="h-5 w-5 animate-spin rounded-full border-2 border-white border-t-transparent" />
              ) : (
                <>
                  Sign in
                  <ArrowRight className="h-4 w-4" />
                </>
              )}
            </button>
          </form>
        </div>

        {/* Footer */}
        <p className="text-center text-sm text-gray-600">
          New organization?{' '}
          <Link to="/register" className="font-medium text-nexus-400 hover:text-nexus-300 transition-colors">
            Register here
          </Link>
        </p>

        {/* Engine status bar */}
        <div className="flex items-center justify-center gap-1.5">
          {Array.from({ length: 11 }).map((_, i) => (
            <div key={i} className="h-1.5 w-1.5 rounded-full bg-green-500/40" title="Engine online" />
          ))}
          <span className="ml-2 text-[10px] text-gray-600">11 Engines Ready</span>
        </div>
      </div>
    </div>
  );
}
