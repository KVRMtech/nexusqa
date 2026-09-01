'use client';

import { createContext, useContext, useCallback, useEffect, useState, type ReactNode } from 'react';
import { AppState, EMPTY_STATE, type BeneficiaryRecord, type ReplacementPolicy, type QuoteData, type PaymentData } from './types';

const STORAGE_KEY = 'vkpl_app_state';

interface StoreContextValue {
  state: AppState;
  login: (memberNumber: string) => void;
  logout: () => void;
  setProfile: (profile: AppState['profile']) => void;
  updateQuote: (partial: Partial<QuoteData>) => void;
  updatePersonalInfo: (key: string, value: string) => void;
  setPersonalInfo: (info: Record<string, string>) => void;
  addReplacement: (r: ReplacementPolicy) => void;
  removeReplacement: (index: number) => void;
  setHealthAnswer: (questionId: string, answer: string) => void;
  setHealthAnswers: (answers: Record<string, string>) => void;
  setLifestyleAnswer: (questionId: string, answer: string) => void;
  setLifestyleAnswers: (answers: Record<string, string>) => void;
  setDecision: (decision: AppState['decision'], reasons: string[]) => void;
  updatePayment: (partial: Partial<PaymentData>) => void;
  setBeneficiaries: (b: BeneficiaryRecord[]) => void;
  addBeneficiary: (b: BeneficiaryRecord) => void;
  removeBeneficiary: (index: number) => void;
  sign: (date: string) => void;
  setConfirmation: (appNum: string, confNum: string) => void;
  resetApplication: () => void;
}

const StoreContext = createContext<StoreContextValue | null>(null);

function loadState(): AppState {
  if (typeof window === 'undefined') return EMPTY_STATE;
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (raw) return { ...EMPTY_STATE, ...JSON.parse(raw) };
  } catch { /* corrupted storage */ }
  return EMPTY_STATE;
}

function saveState(state: AppState) {
  try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch { /* quota */ }
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AppState>(EMPTY_STATE);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    setState(loadState());
    setHydrated(true);
  }, []);

  const update = useCallback((fn: (prev: AppState) => AppState) => {
    setState(prev => {
      const next = fn(prev);
      saveState(next);
      return next;
    });
  }, []);

  const login = useCallback((memberNumber: string) => {
    update(s => ({ ...s, authenticated: true, memberNumber }));
  }, [update]);

  const logout = useCallback(() => {
    sessionStorage.removeItem(STORAGE_KEY);
    setState(EMPTY_STATE);
  }, []);

  const setProfile = useCallback((profile: AppState['profile']) => {
    update(s => ({ ...s, profile }));
  }, [update]);

  const updateQuote = useCallback((partial: Partial<QuoteData>) => {
    update(s => ({ ...s, quote: { ...s.quote, ...partial } }));
  }, [update]);

  const updatePersonalInfo = useCallback((key: string, value: string) => {
    update(s => ({ ...s, personalInfo: { ...s.personalInfo, [key]: value } }));
  }, [update]);

  const setPersonalInfo = useCallback((info: Record<string, string>) => {
    update(s => ({ ...s, personalInfo: { ...s.personalInfo, ...info } }));
  }, [update]);

  const addReplacement = useCallback((r: ReplacementPolicy) => {
    update(s => ({ ...s, replacements: [...s.replacements, r] }));
  }, [update]);

  const removeReplacement = useCallback((index: number) => {
    update(s => ({ ...s, replacements: s.replacements.filter((_, i) => i !== index) }));
  }, [update]);

  const setHealthAnswer = useCallback((questionId: string, answer: string) => {
    update(s => {
      const next = { ...s.healthAnswers, [questionId]: answer };
      if (answer === 'No') {
        Object.keys(next).forEach(k => {
          if (k.startsWith(questionId + '_')) delete next[k];
        });
      }
      return { ...s, healthAnswers: next };
    });
  }, [update]);

  const setHealthAnswers = useCallback((answers: Record<string, string>) => {
    update(s => ({ ...s, healthAnswers: { ...s.healthAnswers, ...answers } }));
  }, [update]);

  const setLifestyleAnswer = useCallback((questionId: string, answer: string) => {
    update(s => {
      const next = { ...s.lifestyleAnswers, [questionId]: answer };
      if (answer === 'No') {
        Object.keys(next).forEach(k => {
          if (k.startsWith(questionId + '_')) delete next[k];
        });
      }
      return { ...s, lifestyleAnswers: next };
    });
  }, [update]);

  const setLifestyleAnswers = useCallback((answers: Record<string, string>) => {
    update(s => ({ ...s, lifestyleAnswers: { ...s.lifestyleAnswers, ...answers } }));
  }, [update]);

  const setDecision = useCallback((decision: AppState['decision'], reasons: string[]) => {
    update(s => ({ ...s, decision, decisionReasons: reasons }));
  }, [update]);

  const updatePayment = useCallback((partial: Partial<PaymentData>) => {
    update(s => ({ ...s, payment: { ...s.payment, ...partial } }));
  }, [update]);

  const setBeneficiaries = useCallback((b: BeneficiaryRecord[]) => {
    update(s => ({ ...s, beneficiaries: b }));
  }, [update]);

  const addBeneficiary = useCallback((b: BeneficiaryRecord) => {
    update(s => ({ ...s, beneficiaries: [...s.beneficiaries, b] }));
  }, [update]);

  const removeBeneficiary = useCallback((index: number) => {
    update(s => ({ ...s, beneficiaries: s.beneficiaries.filter((_, i) => i !== index) }));
  }, [update]);

  const sign = useCallback((date: string) => {
    update(s => ({ ...s, signed: true, signedAt: date }));
  }, [update]);

  const setConfirmation = useCallback((applicationNumber: string, confirmationNumber: string) => {
    update(s => ({ ...s, applicationNumber, confirmationNumber }));
  }, [update]);

  const resetApplication = useCallback(() => {
    update(s => ({
      ...EMPTY_STATE,
      authenticated: s.authenticated,
      memberNumber: s.memberNumber,
      profile: s.profile,
    }));
  }, [update]);

  const value: StoreContextValue = {
    state, login, logout, setProfile, updateQuote, updatePersonalInfo, setPersonalInfo,
    addReplacement, removeReplacement, setHealthAnswer, setHealthAnswers,
    setLifestyleAnswer, setLifestyleAnswers, setDecision, updatePayment,
    setBeneficiaries, addBeneficiary, removeBeneficiary, sign, setConfirmation, resetApplication,
  };

  if (!hydrated) return null;

  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>;
}

export function useApp(): StoreContextValue {
  const ctx = useContext(StoreContext);
  if (!ctx) throw new Error('useApp must be used within AppProvider');
  return ctx;
}
