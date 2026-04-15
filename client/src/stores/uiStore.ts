// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — UI Store (Zustand)
// ═══════════════════════════════════════════════════════════════
import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';

interface UIState {
  sidebarCollapsed: boolean;
  sidebarMobileOpen: boolean;
  commandPaletteOpen: boolean;
  activeModal: string | null;
  modalData: Record<string, unknown> | null;
}

interface UIActions {
  toggleSidebar: () => void;
  setSidebarMobileOpen: (open: boolean) => void;
  setCommandPaletteOpen: (open: boolean) => void;
  openModal: (id: string, data?: Record<string, unknown>) => void;
  closeModal: () => void;
}

export type UIStore = UIState & UIActions;

export const useUIStore = create<UIStore>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      sidebarMobileOpen: false,
      commandPaletteOpen: false,
      activeModal: null,
      modalData: null,

      toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setSidebarMobileOpen: (open) => set({ sidebarMobileOpen: open }),
      setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }),
      openModal: (id, data) => set({ activeModal: id, modalData: data ?? null }),
      closeModal: () => set({ activeModal: null, modalData: null }),
    }),
    {
      name: 'nexus-ui',
      storage: createJSONStorage(() => localStorage),
      partialize: (s) => ({ sidebarCollapsed: s.sidebarCollapsed }),
    },
  ),
);
