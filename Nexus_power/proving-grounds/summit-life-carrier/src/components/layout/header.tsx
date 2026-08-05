'use client';

import { useRouter, usePathname } from 'next/navigation';
import { useAuth } from '@/lib/providers';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Button } from '@/components/ui/button';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { Badge } from '@/components/ui/badge';
import { Bell, LogOut, User } from 'lucide-react';

const breadcrumbMap: Record<string, string> = {
  'dashboard': 'Dashboard',
  'overview': 'Overview',
  'underwriting': 'Underwriting',
  'new-business': 'New Business',
  'policy-admin': 'Policy Administration',
  'in-force': 'In-Force Policies',
  'claims': 'Claims Operations',
  'reported': 'Reported Claims',
  'actuarial': 'Actuarial',
  'product-pricing': 'Product Pricing',
  'review': 'Case Review',
  'requirements': 'Requirements',
  'decision': 'Risk Decision',
  'transactions': 'Transactions',
  'new': 'New',
  'investigation': 'Investigation',
  'new-fnol': 'New FNOL',
  'settings': 'Settings',
  'preferences': 'Preferences',
};

export function AppHeader() {
  const { user, logout } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  const segments = pathname.split('/').filter(Boolean);
  const breadcrumbs = segments.map(s => breadcrumbMap[s] || s);

  const handleLogout = () => {
    logout();
    router.push('/portal/sign-in');
  };

  const initials = user ? user.name.split(' ').map(n => n[0]).join('') : 'SL';

  return (
    <header className="flex h-16 items-center justify-between border-b bg-card px-6">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        {breadcrumbs.map((crumb, i) => (
          <span key={i} className="flex items-center gap-2">
            {i > 0 && <span className="text-border">/</span>}
            <span className={i === breadcrumbs.length - 1 ? 'text-foreground font-medium' : ''}>{crumb}</span>
          </span>
        ))}
      </div>

      <div className="flex items-center gap-4">
        <Button variant="ghost" size="icon" className="relative">
          <Bell className="h-4 w-4" />
          <Badge variant="destructive" className="absolute -right-1 -top-1 h-4 w-4 p-0 flex items-center justify-center text-[10px]">3</Badge>
        </Button>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" className="relative h-8 gap-2 rounded-full pl-2 pr-3">
              <Avatar className="h-7 w-7">
                <AvatarFallback className="bg-gold text-navy-dark text-xs font-semibold">{initials}</AvatarFallback>
              </Avatar>
              <span className="text-sm font-medium hidden sm:inline-block">{user?.name || 'Admin'}</span>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent className="w-56" align="end" forceMount>
            <DropdownMenuLabel className="font-normal">
              <div className="flex flex-col space-y-1">
                <p className="text-sm font-medium">{user?.name}</p>
                <p className="text-xs text-muted-foreground">{user?.email}</p>
                <Badge variant="secondary" className="w-fit text-[10px] mt-1">{user?.role?.replace(/_/g, ' ')}</Badge>
              </div>
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem><User className="mr-2 h-4 w-4" />Profile</DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={handleLogout}><LogOut className="mr-2 h-4 w-4" />Sign Out</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  );
}
