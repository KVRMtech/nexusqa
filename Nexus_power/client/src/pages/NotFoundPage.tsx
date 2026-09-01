// ═══════════════════════════════════════════════════════════════
//  VKPOWER AI ENGINE FACTORY — 404 Not Found Page
// ═══════════════════════════════════════════════════════════════
import { useNavigate } from 'react-router-dom';
import { Home, ArrowLeft, Search } from 'lucide-react';

export default function NotFoundPage() {
  const navigate = useNavigate();

  return (
    <div className="flex h-[calc(100vh-4rem)] items-center justify-center p-6">
      <div className="max-w-md w-full text-center space-y-6 animate-[fade-in_0.3s_ease-out]">
        {/* Glowing 404 */}
        <div className="relative">
          <span className="text-[120px] font-black text-transparent bg-clip-text bg-gradient-to-br from-nexus-500/30 to-purple-600/30 leading-none select-none">
            404
          </span>
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-nexus-500/15 ring-1 ring-nexus-500/25 shadow-lg shadow-nexus-500/10">
              <Search className="h-8 w-8 text-nexus-400" />
            </div>
          </div>
        </div>

        {/* Text */}
        <div>
          <h1 className="text-2xl font-bold text-[#0a2540]">Page not found</h1>
          <p className="mt-2 text-sm text-slate-600">
            The page you're looking for doesn't exist or has been moved.
          </p>
        </div>

        {/* Actions */}
        <div className="flex items-center justify-center gap-3">
          <button onClick={() => navigate('/')} className="btn-primary">
            <Home className="h-4 w-4" />
            Go to Dashboard
          </button>
          <button onClick={() => navigate(-1)} className="btn-secondary">
            <ArrowLeft className="h-4 w-4" />
            Go Back
          </button>
        </div>
      </div>
    </div>
  );
}
