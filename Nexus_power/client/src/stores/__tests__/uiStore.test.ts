import { describe, it, expect, beforeEach, vi } from 'vitest';
import { act } from '@testing-library/react';

vi.unmock('../../stores/uiStore');

let useUIStore: any;

beforeEach(async () => {
  vi.resetModules();
  const module = await import('../../stores/uiStore');
  useUIStore = module.useUIStore;
});

describe('uiStore', () => {
  it('starts with sidebar expanded', () => {
    const state = useUIStore.getState();
    expect(state.sidebarCollapsed).toBe(false);
  });

  it('toggles sidebar', () => {
    act(() => {
      useUIStore.getState().toggleSidebar();
    });
    expect(useUIStore.getState().sidebarCollapsed).toBe(true);
    act(() => {
      useUIStore.getState().toggleSidebar();
    });
    expect(useUIStore.getState().sidebarCollapsed).toBe(false);
  });

  it('opens and closes modal', () => {
    act(() => {
      useUIStore.getState().openModal('confirm-delete', { id: '123' });
    });
    const state = useUIStore.getState();
    expect(state.activeModal).toBe('confirm-delete');
    expect(state.modalData).toEqual({ id: '123' });

    act(() => {
      useUIStore.getState().closeModal();
    });
    expect(useUIStore.getState().activeModal).toBeNull();
    expect(useUIStore.getState().modalData).toBeNull();
  });

  it('toggles command palette', () => {
    act(() => {
      useUIStore.getState().setCommandPaletteOpen(true);
    });
    expect(useUIStore.getState().commandPaletteOpen).toBe(true);
    act(() => {
      useUIStore.getState().setCommandPaletteOpen(false);
    });
    expect(useUIStore.getState().commandPaletteOpen).toBe(false);
  });

  it('handles mobile sidebar', () => {
    act(() => {
      useUIStore.getState().setSidebarMobileOpen(true);
    });
    expect(useUIStore.getState().sidebarMobileOpen).toBe(true);
    act(() => {
      useUIStore.getState().setSidebarMobileOpen(false);
    });
    expect(useUIStore.getState().sidebarMobileOpen).toBe(false);
  });
});
