export type ApplicationStatus = 'submitted' | 'in_review' | 'requirements_pending' | 'underwriting' | 'approved' | 'approved_rated' | 'declined' | 'counter_offer' | 'postponed' | 'withdrawn';
export type ProductType = 'whole_life' | 'term_life_10' | 'term_life_20' | 'term_life_30' | 'universal_life' | 'variable_universal' | 'indexed_universal';
export type VendorName = 'MVR' | 'MIB' | 'ESI' | 'Rx_PBM' | 'APS' | 'Paramedical' | 'Inspection';
export type VendorOrderStatus = 'pending' | 'ordered' | 'received' | 'reviewed' | 'cancelled';
export type SuspenseReason = 'missing_document' | 'medical_records' | 'financial_verification' | 'identity_verification' | 'beneficiary_clarification' | 'agent_follow_up' | 'compliance_review' | 'reinsurance_review';
export type PolicyStatus = 'active' | 'lapsed' | 'surrendered' | 'paid_up' | 'extended_term' | 'reduced_paid_up' | 'death_claim' | 'matured';
export type ServiceTransactionType = 'address_change' | 'beneficiary_change' | 'name_change' | 'premium_mode_change' | 'policy_loan' | 'loan_repayment' | 'partial_surrender' | 'full_surrender' | 'reinstatement' | 'conversion' | 'face_increase' | 'face_decrease' | 'dividend_option' | 'automatic_premium_loan' | 'paid_up_additions';
export type ClaimType = 'death' | 'accelerated_death' | 'waiver_of_premium' | 'accidental_death' | 'dismemberment';
export type ClaimStatus = 'reported' | 'under_investigation' | 'pending_documents' | 'in_review' | 'approved' | 'denied' | 'closed';
export type RiskClass = 'preferred_plus' | 'preferred' | 'standard_plus' | 'standard' | 'substandard_table_a' | 'substandard_table_b' | 'substandard_table_c' | 'substandard_table_d' | 'decline';

export interface AdminUser {
  id: string;
  name: string;
  email: string;
  role: 'underwriter' | 'senior_underwriter' | 'chief_underwriter' | 'claims_analyst' | 'claims_manager' | 'service_rep' | 'actuary' | 'admin';
  department: string;
  avatar?: string;
}

export interface Applicant {
  firstName: string;
  lastName: string;
  dateOfBirth: string;
  gender: 'male' | 'female';
  ssn: string;
  email: string;
  phone: string;
  address: { street: string; city: string; state: string; zip: string };
  occupation: string;
  annualIncome: number;
  tobaccoUse: boolean;
  healthConditions: string[];
}

export interface Application {
  id: string;
  caseNumber: string;
  applicant: Applicant;
  product: ProductType;
  faceAmount: number;
  premiumMode: 'monthly' | 'quarterly' | 'semi_annual' | 'annual';
  status: ApplicationStatus;
  assignedUnderwriter: string;
  submittedAt: string;
  lastUpdated: string;
  riskClass?: RiskClass;
  tableRating?: string;
  premiumAmount?: number;
  notes: { author: string; text: string; createdAt: string }[];
}

export interface VendorOrder {
  id: string;
  applicationId: string;
  vendor: VendorName;
  status: VendorOrderStatus;
  orderedAt: string;
  receivedAt?: string;
  cost: number;
  results?: Record<string, unknown>;
}

export interface SuspenseItem {
  id: string;
  applicationId: string;
  reason: SuspenseReason;
  description: string;
  createdBy: string;
  createdAt: string;
  dueDate: string;
  resolvedAt?: string;
  resolvedBy?: string;
  resolution?: string;
}

export interface Beneficiary {
  name: string;
  relationship: string;
  percentage: number;
  type: 'primary' | 'contingent';
  irrevocable: boolean;
}

export interface Policy {
  id: string;
  policyNumber: string;
  applicationId: string;
  product: ProductType;
  insured: { firstName: string; lastName: string; dateOfBirth: string; gender: string };
  owner: { firstName: string; lastName: string };
  faceAmount: number;
  premiumAmount: number;
  premiumMode: 'monthly' | 'quarterly' | 'semi_annual' | 'annual';
  status: PolicyStatus;
  effectiveDate: string;
  issueDate: string;
  maturityDate?: string;
  cashValue: number;
  loanBalance: number;
  beneficiaries: Beneficiary[];
  riders: { name: string; premium: number; faceAmount?: number }[];
  paymentHistory: { date: string; amount: number; type: string }[];
  transactions: ServiceTransaction[];
}

export interface ServiceTransaction {
  id: string;
  policyId: string;
  type: ServiceTransactionType;
  status: 'pending' | 'processing' | 'completed' | 'rejected';
  requestedAt: string;
  completedAt?: string;
  requestedBy: string;
  details: Record<string, unknown>;
}

export interface Claim {
  id: string;
  claimNumber: string;
  policyId: string;
  policyNumber: string;
  type: ClaimType;
  status: ClaimStatus;
  claimant: { firstName: string; lastName: string; relationship: string; phone: string; email: string };
  insured: { firstName: string; lastName: string; dateOfBirth: string; dateOfDeath?: string };
  dateOfLoss: string;
  reportedAt: string;
  faceAmount: number;
  benefitAmount: number;
  assignedAnalyst: string;
  causeOfDeath?: string;
  contestabilityExpiry: string;
  isContestable: boolean;
  documents: { name: string; type: string; received: boolean; receivedAt?: string }[];
  investigationNotes: { author: string; text: string; createdAt: string }[];
  payments: { amount: number; payee: string; date: string; type: string }[];
}

export interface RateTableEntry {
  age: number;
  gender: 'male' | 'female';
  tobaccoUse: boolean;
  riskClass: RiskClass;
  product: ProductType;
  ratePerThousand: number;
  mortalityFactor: number;
  expenseFactor: number;
  profitMargin: number;
}

export interface Customer {
  id: string;
  firstName: string;
  lastName: string;
  dateOfBirth: string;
  ssn: string;
  email: string;
  phone: string;
  address: { street: string; city: string; state: string; zip: string };
  occupation: string;
  employer: string;
  annualIncome: number;
  identityVerified: boolean;
  kycStatus: 'pending' | 'cleared' | 'flagged' | 'review_required';
  createdAt: string;
  updatedAt: string;
}

export interface PaymentMethod {
  id: string;
  customerId: string;
  type: 'ach' | 'credit_card' | 'eft' | 'check';
  bankName: string;
  routingLast4: string;
  accountLast4: string;
  verified: boolean;
  createdAt: string;
}

export interface Payment {
  id: string;
  policyId: string;
  customerId: string;
  amount: number;
  type: 'initial_premium' | 'renewal' | 'reinstatement' | 'loan_repayment';
  status: 'pending' | 'processing' | 'completed' | 'failed' | 'refunded';
  paymentMethodId: string;
  processedAt: string;
  receiptId: string;
  referenceNumber: string;
}

export type ApiCallPhase = 'customer_verification' | 'vendor_checks' | 'underwriting' | 'compliance' | 'rating' | 'documentation' | 'finalization';

export interface ApiCallStep {
  id: string;
  name: string;
  endpoint: string;
  method: 'GET' | 'POST' | 'PUT';
  phase: ApiCallPhase;
  body?: Record<string, unknown>;
}

export interface ApiCallResult {
  step: ApiCallStep;
  status: 'pending' | 'running' | 'completed' | 'failed';
  response?: Record<string, unknown>;
  error?: string;
  startedAt?: number;
  completedAt?: number;
}
