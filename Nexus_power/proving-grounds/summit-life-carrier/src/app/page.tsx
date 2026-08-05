'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/providers';

export default function RootPage() {
  const router = useRouter();
  const { isAuthenticated } = useAuth();

  useEffect(() => {
    router.replace(isAuthenticated ? '/dashboard/overview' : '/portal/sign-in');
  }, [isAuthenticated, router]);

  return (
    <div className="flex h-screen items-center justify-center bg-navy">
      <div className="animate-pulse text-gold text-lg font-semibold">Loading Summit Life Carrier Platform...</div>
    </div>
  );
}
