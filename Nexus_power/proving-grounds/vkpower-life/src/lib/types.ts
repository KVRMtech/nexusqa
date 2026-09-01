export interface Address {
  street: string;
  unit: string;
  city: string;
  state: string;
  zip: string;
}

export interface Employment {
  status: string;
  occupation: string;
  employer: string;
  annualIncome: string;
}

export interface MemberProfile {
  memberNumber: string;
  firstName: string;
  lastName: string;
  dateOfBirth: string;
  age: number;
  gender: string;
  ssn: string;
  email: string;
  phone: string;
  address: Address;
  employment: Employment;
  citizenship: string;
  residencyState: string;
  militaryStatus: string;
  militaryBranch: string;
  behaviorClass: string;
  policyNumber: string;
  coverageAmount: number;
  monthlyPremium: number;
  beneficiaries: BeneficiaryRecord[];
}

export interface BeneficiaryRecord {
  firstName: string;
  lastName: string;
  relationship: string;
  percentage: number;
  dateOfBirth: string;
  ssn: string;
  type: 'primary' | 'contingent';
}

export interface QuoteData {
  product: string;
  coverageAmount: string;
  termLength: string;
  militaryStatus: string;
  militaryBranch: string;
  firstName: string;
  lastName: string;
  dateOfBirth: string;
  gender: string;
  state: string;
  email: string;
  phone: string;
  tobaccoUse: string;
  heightFeet: string;
  heightInches: string;
  weight: string;
  healthEligible: boolean;
  estimatedPremium: number;
}

export interface ReplacementPolicy {
  type: string;
  company: string;
  policyNumber: string;
  faceAmount: string;
  reason: string;
}

export interface PaymentData {
  method: string;
  bankName: string;
  routingNumber: string;
  accountNumber: string;
  accountType: string;
  cardholderName: string;
  cardNumber: string;
  cardExpiry: string;
  cardCvv: string;
  billingFrequency: string;
}

export interface HealthQuestion {
  id: string;
  text: string;
  category: string;
  type: 'yesno' | 'text' | 'select' | 'number' | 'date' | 'textarea';
  options?: string[];
  dependsOn?: { questionId: string; answer: string };
  placeholder?: string;
}

export interface AppState {
  authenticated: boolean;
  memberNumber: string;
  profile: MemberProfile | null;
  quote: Partial<QuoteData>;
  personalInfo: Record<string, string>;
  replacements: ReplacementPolicy[];
  healthAnswers: Record<string, string>;
  lifestyleAnswers: Record<string, string>;
  decision: 'approved' | 'referred' | 'declined' | null;
  decisionReasons: string[];
  payment: Partial<PaymentData>;
  beneficiaries: BeneficiaryRecord[];
  signed: boolean;
  signedAt: string;
  applicationNumber: string;
  confirmationNumber: string;
}

export const EMPTY_STATE: AppState = {
  authenticated: false,
  memberNumber: '',
  profile: null,
  quote: {},
  personalInfo: {},
  replacements: [],
  healthAnswers: {},
  lifestyleAnswers: {},
  decision: null,
  decisionReasons: [],
  payment: {},
  beneficiaries: [],
  signed: false,
  signedAt: '',
  applicationNumber: '',
  confirmationNumber: '',
};
