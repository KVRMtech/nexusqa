import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { AlertCircle, ArrowRight, Building2, Hexagon } from 'lucide-react';

export default function RegisterPage() {
  const { register } = useAuth();
  const navigate = useNavigate();
  const [form, setForm] = useState({ tenantName: '', name: '', email: '', password: '', confirmPassword: '' });
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const set = (field: string) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [field]: e.target.value }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    if (form.password !== form.confirmPassword) {
      setError('Passwords do not match');
      return;
    }
    if (form.password.length < 8) {
      setError('Password must be at least 8 characters');
      return;
    }
    setLoading(true);
    try {
      await register(form.tenantName, form.email, form.password, form.name);
      navigate('/');
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Registration failed.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-[#f5f7fa] px-4">
      {/* Animated background */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute -top-32 left-1/3 h-[500px] w-[500px] rounded-full bg-purple-600/[0.07] blur-[120px] animate-pulse" />
        <div className="absolute -bottom-32 right-1/4 h-[400px] w-[400px] rounded-full bg-nexus-600/[0.06] blur-[100px]" />
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
            <Hexagon className="h-8 w-8 text-[#0a2540]" strokeWidth={1.5} />
          </div>
          <h1 className="mt-5 text-2xl font-bold text-[#0a2540] tracking-tight">
            Create your <span className="text-transparent bg-clip-text bg-gradient-to-r from-nexus-400 to-purple-400">Organization</span>
          </h1>
          <p className="mt-2 text-sm text-slate-400">
            Set up a new Nexus AI Engine Factory tenant
          </p>
        </div>

        {/* Card */}
        <div className="rounded-2xl bg-white backdrop-blur-sm p-8 ring-1 ring-white/[0.06] shadow-2xl">
          {error && (
            <div className="mb-5 flex items-center gap-2 rounded-xl bg-red-500/10 p-3 text-sm text-red-400 ring-1 ring-red-500/20">
              <AlertCircle className="h-4 w-4 shrink-0" />
              {error}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs font-medium text-slate-8000 uppercase tracking-wider mb-1.5">
                <span className="flex items-center gap-1.5"><Building2 className="h-3 w-3" /> Organization name</span>
              </label>
              <input type="text" required value={form.tenantName} onChange={set('tenantName')} className="input-field w-full" placeholder="Acme Corp" />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-8000 uppercase tracking-wider mb-1.5">Your name</label>
              <input type="text" required value={form.name} onChange={set('name')} className="input-field w-full" placeholder="Jane Smith" />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-8000 uppercase tracking-wider mb-1.5">Email</label>
              <input type="email" required value={form.email} onChange={set('email')} className="input-field w-full" placeholder="jane@acme.com" />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs font-medium text-slate-8000 uppercase tracking-wider mb-1.5">Password</label>
                <input type="password" required value={form.password} onChange={set('password')} className="input-field w-full" placeholder="Min 8 chars" />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-8000 uppercase tracking-wider mb-1.5">Confirm</label>
                <input type="password" required value={form.confirmPassword} onChange={set('confirmPassword')} className="input-field w-full" placeholder="••••••••" />
              </div>
            </div>

            <button type="submit" disabled={loading} className="btn-primary w-full py-2.5 justify-center mt-2">
              {loading ? (
                <div className="h-5 w-5 animate-spin rounded-full border-2 border-white border-t-transparent" />
              ) : (
                <>
                  Create organization
                  <ArrowRight className="h-4 w-4" />
                </>
              )}
            </button>
          </form>
        </div>

        {/* Footer */}
        <p className="text-center text-sm text-slate-8000">
          Already have an account?{' '}
          <Link to="/login" className="font-medium text-nexus-400 hover:text-nexus-300 transition-colors">
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
