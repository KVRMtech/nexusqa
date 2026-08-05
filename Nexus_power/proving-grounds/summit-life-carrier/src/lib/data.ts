import { AdminUser, Application, VendorOrder, SuspenseItem, Policy, Claim, RateTableEntry, Customer, PaymentMethod, Payment } from './types';

export const adminUsers: AdminUser[] = [
  { id: 'usr-001', name: 'Margaret Chen', email: 'mchen@summitlife.com', role: 'chief_underwriter', department: 'New Business Underwriting' },
  { id: 'usr-002', name: 'David Kowalski', email: 'dkowalski@summitlife.com', role: 'senior_underwriter', department: 'New Business Underwriting' },
  { id: 'usr-003', name: 'Priya Sharma', email: 'psharma@summitlife.com', role: 'underwriter', department: 'New Business Underwriting' },
  { id: 'usr-004', name: 'James Whitfield', email: 'jwhitfield@summitlife.com', role: 'claims_manager', department: 'Claims Operations' },
  { id: 'usr-005', name: 'Elena Rodriguez', email: 'erodriguez@summitlife.com', role: 'claims_analyst', department: 'Claims Operations' },
  { id: 'usr-006', name: 'Thomas Park', email: 'tpark@summitlife.com', role: 'service_rep', department: 'Policy Services' },
  { id: 'usr-007', name: 'Rachel Goldstein', email: 'rgoldstein@summitlife.com', role: 'actuary', department: 'Actuarial & Pricing' },
];

export const applications: Application[] = [
  {
    id: 'app-7f3a9b2e', caseNumber: 'UW-2026-00142',
    applicant: { firstName: 'Michael', lastName: 'Thornberry', dateOfBirth: '1978-03-14', gender: 'male', ssn: '***-**-4521', email: 'mthornberry@email.com', phone: '(512) 555-0147', address: { street: '4821 Ridgeview Dr', city: 'Austin', state: 'TX', zip: '78731' }, occupation: 'Software Engineering Director', annualIncome: 245000, tobaccoUse: false, healthConditions: [] },
    product: 'term_life_20', faceAmount: 2000000, premiumMode: 'annual', status: 'underwriting', assignedUnderwriter: 'usr-002', submittedAt: '2026-07-18T09:22:00Z', lastUpdated: '2026-07-25T14:30:00Z',
    notes: [{ author: 'David Kowalski', text: 'All vendor reports received. Clean MIB, clean MVR. Paramedical results within normal limits. Proceeding to risk classification.', createdAt: '2026-07-25T14:30:00Z' }],
  },
  {
    id: 'app-2c8d4e1f', caseNumber: 'UW-2026-00143',
    applicant: { firstName: 'Sarah', lastName: 'Jennings', dateOfBirth: '1985-11-02', gender: 'female', ssn: '***-**-7832', email: 'sjennings@email.com', phone: '(404) 555-0293', address: { street: '1190 Peachtree Blvd NE', city: 'Atlanta', state: 'GA', zip: '30309' }, occupation: 'Cardiologist', annualIncome: 475000, tobaccoUse: false, healthConditions: ['controlled_hypertension'] },
    product: 'whole_life', faceAmount: 5000000, premiumMode: 'annual', status: 'requirements_pending', assignedUnderwriter: 'usr-001', submittedAt: '2026-07-20T11:45:00Z', lastUpdated: '2026-07-24T16:10:00Z',
    notes: [{ author: 'Margaret Chen', text: 'High face amount requires full APS from treating physician. Hypertension noted — need confirmation of current medication regimen and last 3 BP readings.', createdAt: '2026-07-24T16:10:00Z' }],
  },
  {
    id: 'app-5a1b9c3d', caseNumber: 'UW-2026-00144',
    applicant: { firstName: 'Robert', lastName: 'Vasquez', dateOfBirth: '1962-08-29', gender: 'male', ssn: '***-**-1094', email: 'rvasquez@email.com', phone: '(305) 555-0811', address: { street: '7742 Coral Way', city: 'Miami', state: 'FL', zip: '33155' }, occupation: 'Restaurant Owner', annualIncome: 185000, tobaccoUse: true, healthConditions: ['type_2_diabetes', 'elevated_bmi'] },
    product: 'universal_life', faceAmount: 1000000, premiumMode: 'monthly', status: 'in_review', assignedUnderwriter: 'usr-003', submittedAt: '2026-07-22T08:15:00Z', lastUpdated: '2026-07-26T10:00:00Z',
    notes: [{ author: 'Priya Sharma', text: 'Tobacco use with Type 2 diabetes and elevated BMI. Ordering full medical workup. May require table rating.', createdAt: '2026-07-26T10:00:00Z' }],
  },
  {
    id: 'app-8e2f6a4b', caseNumber: 'UW-2026-00145',
    applicant: { firstName: 'Jennifer', lastName: 'Walsh', dateOfBirth: '1990-05-17', gender: 'female', ssn: '***-**-5567', email: 'jwalsh@email.com', phone: '(312) 555-0456', address: { street: '225 N Michigan Ave Apt 3201', city: 'Chicago', state: 'IL', zip: '60601' }, occupation: 'Investment Banking VP', annualIncome: 380000, tobaccoUse: false, healthConditions: [] },
    product: 'term_life_30', faceAmount: 3000000, premiumMode: 'semi_annual', status: 'submitted', assignedUnderwriter: 'usr-002', submittedAt: '2026-07-26T07:30:00Z', lastUpdated: '2026-07-26T07:30:00Z',
    notes: [],
  },
  {
    id: 'app-3d7c5e9a', caseNumber: 'UW-2026-00146',
    applicant: { firstName: 'William', lastName: 'Okonkwo', dateOfBirth: '1972-12-03', gender: 'male', ssn: '***-**-8891', email: 'wokonkwo@email.com', phone: '(713) 555-0922', address: { street: '11402 Memorial Dr', city: 'Houston', state: 'TX', zip: '77024' }, occupation: 'Petroleum Engineer', annualIncome: 310000, tobaccoUse: false, healthConditions: ['sleep_apnea_treated'] },
    product: 'indexed_universal', faceAmount: 2500000, premiumMode: 'annual', status: 'approved', assignedUnderwriter: 'usr-001', submittedAt: '2026-07-10T13:00:00Z', lastUpdated: '2026-07-23T09:45:00Z', riskClass: 'standard_plus', premiumAmount: 4875,
    notes: [{ author: 'Margaret Chen', text: 'Treated sleep apnea with documented CPAP compliance >90%. Standard Plus classification approved. Reinsurance facultative not required under $3M threshold.', createdAt: '2026-07-23T09:45:00Z' }],
  },
];

export const vendorOrders: VendorOrder[] = [
  { id: 'vo-001', applicationId: 'app-7f3a9b2e', vendor: 'MVR', status: 'received', orderedAt: '2026-07-19T10:00:00Z', receivedAt: '2026-07-20T14:22:00Z', cost: 12.50 },
  { id: 'vo-002', applicationId: 'app-7f3a9b2e', vendor: 'MIB', status: 'received', orderedAt: '2026-07-19T10:00:00Z', receivedAt: '2026-07-19T16:45:00Z', cost: 8.00 },
  { id: 'vo-003', applicationId: 'app-7f3a9b2e', vendor: 'Paramedical', status: 'received', orderedAt: '2026-07-19T10:05:00Z', receivedAt: '2026-07-23T11:30:00Z', cost: 145.00 },
  { id: 'vo-004', applicationId: 'app-7f3a9b2e', vendor: 'Rx_PBM', status: 'received', orderedAt: '2026-07-19T10:00:00Z', receivedAt: '2026-07-20T09:15:00Z', cost: 15.00 },
  { id: 'vo-005', applicationId: 'app-2c8d4e1f', vendor: 'MIB', status: 'received', orderedAt: '2026-07-21T08:30:00Z', receivedAt: '2026-07-21T15:00:00Z', cost: 8.00 },
  { id: 'vo-006', applicationId: 'app-2c8d4e1f', vendor: 'APS', status: 'pending', orderedAt: '2026-07-24T16:15:00Z', cost: 35.00 },
  { id: 'vo-007', applicationId: 'app-5a1b9c3d', vendor: 'MVR', status: 'ordered', orderedAt: '2026-07-23T09:00:00Z', cost: 12.50 },
  { id: 'vo-008', applicationId: 'app-5a1b9c3d', vendor: 'MIB', status: 'ordered', orderedAt: '2026-07-23T09:00:00Z', cost: 8.00 },
  { id: 'vo-009', applicationId: 'app-5a1b9c3d', vendor: 'Paramedical', status: 'ordered', orderedAt: '2026-07-23T09:05:00Z', cost: 175.00 },
  { id: 'vo-010', applicationId: 'app-5a1b9c3d', vendor: 'ESI', status: 'ordered', orderedAt: '2026-07-23T09:05:00Z', cost: 22.00 },
];

export const suspenseItems: SuspenseItem[] = [
  { id: 'susp-001', applicationId: 'app-2c8d4e1f', reason: 'medical_records', description: 'Awaiting full APS from Dr. Katherine Reeves, Piedmont Heart Institute. Records requested for controlled hypertension treatment history and current medication regimen.', createdBy: 'Margaret Chen', createdAt: '2026-07-24T16:20:00Z', dueDate: '2026-08-07T23:59:00Z' },
  { id: 'susp-002', applicationId: 'app-2c8d4e1f', reason: 'financial_verification', description: 'Face amount of $5M exceeds 15x income threshold. Requesting financial justification documentation and net worth statement.', createdBy: 'Margaret Chen', createdAt: '2026-07-24T16:25:00Z', dueDate: '2026-08-14T23:59:00Z' },
];

export const policies: Policy[] = [
  {
    id: 'pol-a1b2c3', policyNumber: 'SL-TL-2026-50005', applicationId: 'app-3d7c5e9a', product: 'indexed_universal',
    insured: { firstName: 'William', lastName: 'Okonkwo', dateOfBirth: '1972-12-03', gender: 'male' },
    owner: { firstName: 'William', lastName: 'Okonkwo' },
    faceAmount: 2500000, premiumAmount: 4875, premiumMode: 'annual', status: 'active',
    effectiveDate: '2026-08-01', issueDate: '2026-07-28', cashValue: 0, loanBalance: 0,
    beneficiaries: [
      { name: 'Adaeze Okonkwo', relationship: 'Spouse', percentage: 60, type: 'primary', irrevocable: false },
      { name: 'Chukwuemeka Okonkwo', relationship: 'Son', percentage: 40, type: 'primary', irrevocable: false },
      { name: 'Okonkwo Family Trust', relationship: 'Trust', percentage: 100, type: 'contingent', irrevocable: false },
    ],
    riders: [
      { name: 'Waiver of Premium', premium: 285 },
      { name: 'Accidental Death Benefit', premium: 312, faceAmount: 2500000 },
    ],
    paymentHistory: [{ date: '2026-08-01', amount: 5472, type: 'Initial Premium' }],
    transactions: [],
  },
  {
    id: 'pol-d4e5f6', policyNumber: 'SL-WL-2024-99001', applicationId: 'app-legacy-001', product: 'whole_life',
    insured: { firstName: 'Patricia', lastName: 'Morales', dateOfBirth: '1968-06-22', gender: 'female' },
    owner: { firstName: 'Patricia', lastName: 'Morales' },
    faceAmount: 500000, premiumAmount: 8420, premiumMode: 'annual', status: 'active',
    effectiveDate: '2024-03-15', issueDate: '2024-03-10', cashValue: 14250.80, loanBalance: 0,
    beneficiaries: [
      { name: 'Carlos Morales', relationship: 'Husband', percentage: 100, type: 'primary', irrevocable: false },
      { name: 'Maria Morales-Lopez', relationship: 'Daughter', percentage: 50, type: 'contingent', irrevocable: false },
      { name: 'Diego Morales', relationship: 'Son', percentage: 50, type: 'contingent', irrevocable: false },
    ],
    riders: [
      { name: 'Guaranteed Insurability', premium: 165 },
      { name: 'Long-Term Care', premium: 480 },
    ],
    paymentHistory: [
      { date: '2024-03-15', amount: 9065, type: 'Initial Premium' },
      { date: '2025-03-15', amount: 9065, type: 'Annual Renewal' },
      { date: '2026-03-15', amount: 9065, type: 'Annual Renewal' },
    ],
    transactions: [
      { id: 'txn-001', policyId: 'pol-d4e5f6', type: 'beneficiary_change', status: 'completed', requestedAt: '2025-09-12T10:00:00Z', completedAt: '2025-09-14T15:30:00Z', requestedBy: 'Thomas Park', details: { previous: 'Sole beneficiary: Carlos Morales 100%', updated: 'Added contingent beneficiaries' } },
    ],
  },
];

export const claims: Claim[] = [
  {
    id: 'clm-x9y8z7', claimNumber: 'CL-2026-00301', policyId: 'pol-legacy-002', policyNumber: 'SL-TL-2022-31004',
    type: 'death', status: 'under_investigation',
    claimant: { firstName: 'Linda', lastName: 'Hartwell', relationship: 'Spouse', phone: '(617) 555-0384', email: 'lhartwell@email.com' },
    insured: { firstName: 'Gregory', lastName: 'Hartwell', dateOfBirth: '1965-04-11', dateOfDeath: '2026-07-15' },
    dateOfLoss: '2026-07-15', reportedAt: '2026-07-17T09:00:00Z',
    faceAmount: 1500000, benefitAmount: 1500000, assignedAnalyst: 'usr-005',
    causeOfDeath: 'Acute myocardial infarction', contestabilityExpiry: '2024-11-01', isContestable: false,
    documents: [
      { name: 'Death Certificate', type: 'official', received: true, receivedAt: '2026-07-17T09:00:00Z' },
      { name: 'Claimant Statement', type: 'form', received: true, receivedAt: '2026-07-17T09:00:00Z' },
      { name: 'Attending Physician Statement', type: 'medical', received: true, receivedAt: '2026-07-22T14:00:00Z' },
      { name: 'Hospital Records', type: 'medical', received: false },
      { name: 'Autopsy Report', type: 'medical', received: false },
    ],
    investigationNotes: [
      { author: 'Elena Rodriguez', text: 'Death certificate received. Cause: acute MI. Policy issued Nov 2022 — outside 2-year contestability window. No exclusions applicable. Awaiting hospital records for standard due diligence.', createdAt: '2026-07-18T11:00:00Z' },
      { author: 'James Whitfield', text: 'Reinsurer notified per treaty terms (claim >$1M). SIU referral NOT warranted based on initial review. Expedite processing.', createdAt: '2026-07-20T09:30:00Z' },
    ],
    payments: [],
  },
];

export const rateTable: RateTableEntry[] = [
  { age: 25, gender: 'male', tobaccoUse: false, riskClass: 'preferred_plus', product: 'term_life_20', ratePerThousand: 0.52, mortalityFactor: 0.38, expenseFactor: 0.08, profitMargin: 0.06 },
  { age: 25, gender: 'female', tobaccoUse: false, riskClass: 'preferred_plus', product: 'term_life_20', ratePerThousand: 0.41, mortalityFactor: 0.30, expenseFactor: 0.06, profitMargin: 0.05 },
  { age: 25, gender: 'male', tobaccoUse: true, riskClass: 'standard', product: 'term_life_20', ratePerThousand: 1.28, mortalityFactor: 0.98, expenseFactor: 0.18, profitMargin: 0.12 },
  { age: 35, gender: 'male', tobaccoUse: false, riskClass: 'preferred_plus', product: 'term_life_20', ratePerThousand: 0.68, mortalityFactor: 0.50, expenseFactor: 0.10, profitMargin: 0.08 },
  { age: 35, gender: 'female', tobaccoUse: false, riskClass: 'preferred_plus', product: 'term_life_20', ratePerThousand: 0.55, mortalityFactor: 0.40, expenseFactor: 0.08, profitMargin: 0.07 },
  { age: 35, gender: 'male', tobaccoUse: false, riskClass: 'preferred', product: 'term_life_20', ratePerThousand: 0.82, mortalityFactor: 0.60, expenseFactor: 0.12, profitMargin: 0.10 },
  { age: 35, gender: 'male', tobaccoUse: false, riskClass: 'standard', product: 'term_life_20', ratePerThousand: 1.05, mortalityFactor: 0.78, expenseFactor: 0.15, profitMargin: 0.12 },
  { age: 35, gender: 'male', tobaccoUse: true, riskClass: 'standard', product: 'term_life_20', ratePerThousand: 2.15, mortalityFactor: 1.65, expenseFactor: 0.30, profitMargin: 0.20 },
  { age: 45, gender: 'male', tobaccoUse: false, riskClass: 'preferred_plus', product: 'term_life_20', ratePerThousand: 1.42, mortalityFactor: 1.05, expenseFactor: 0.22, profitMargin: 0.15 },
  { age: 45, gender: 'female', tobaccoUse: false, riskClass: 'preferred_plus', product: 'term_life_20', ratePerThousand: 1.15, mortalityFactor: 0.85, expenseFactor: 0.18, profitMargin: 0.12 },
  { age: 45, gender: 'male', tobaccoUse: false, riskClass: 'standard', product: 'term_life_20', ratePerThousand: 2.28, mortalityFactor: 1.72, expenseFactor: 0.32, profitMargin: 0.24 },
  { age: 45, gender: 'male', tobaccoUse: true, riskClass: 'standard', product: 'term_life_20', ratePerThousand: 5.85, mortalityFactor: 4.50, expenseFactor: 0.80, profitMargin: 0.55 },
  { age: 55, gender: 'male', tobaccoUse: false, riskClass: 'preferred_plus', product: 'term_life_20', ratePerThousand: 4.12, mortalityFactor: 3.10, expenseFactor: 0.60, profitMargin: 0.42 },
  { age: 55, gender: 'female', tobaccoUse: false, riskClass: 'preferred_plus', product: 'term_life_20', ratePerThousand: 3.15, mortalityFactor: 2.35, expenseFactor: 0.48, profitMargin: 0.32 },
  { age: 55, gender: 'male', tobaccoUse: false, riskClass: 'standard', product: 'term_life_20', ratePerThousand: 6.75, mortalityFactor: 5.15, expenseFactor: 0.95, profitMargin: 0.65 },
  { age: 55, gender: 'male', tobaccoUse: true, riskClass: 'standard', product: 'term_life_20', ratePerThousand: 14.20, mortalityFactor: 10.90, expenseFactor: 1.95, profitMargin: 1.35 },
  { age: 35, gender: 'male', tobaccoUse: false, riskClass: 'preferred', product: 'whole_life', ratePerThousand: 8.45, mortalityFactor: 3.20, expenseFactor: 2.80, profitMargin: 2.45 },
  { age: 35, gender: 'female', tobaccoUse: false, riskClass: 'preferred', product: 'whole_life', ratePerThousand: 7.20, mortalityFactor: 2.70, expenseFactor: 2.40, profitMargin: 2.10 },
  { age: 45, gender: 'male', tobaccoUse: false, riskClass: 'preferred', product: 'whole_life', ratePerThousand: 14.85, mortalityFactor: 6.50, expenseFactor: 4.60, profitMargin: 3.75 },
  { age: 55, gender: 'male', tobaccoUse: false, riskClass: 'preferred', product: 'whole_life', ratePerThousand: 28.40, mortalityFactor: 14.20, expenseFactor: 7.80, profitMargin: 6.40 },
  { age: 35, gender: 'male', tobaccoUse: false, riskClass: 'standard_plus', product: 'universal_life', ratePerThousand: 2.85, mortalityFactor: 1.90, expenseFactor: 0.55, profitMargin: 0.40 },
  { age: 45, gender: 'male', tobaccoUse: false, riskClass: 'standard_plus', product: 'universal_life', ratePerThousand: 5.40, mortalityFactor: 3.80, expenseFactor: 0.95, profitMargin: 0.65 },
  { age: 55, gender: 'male', tobaccoUse: false, riskClass: 'standard_plus', product: 'universal_life', ratePerThousand: 11.20, mortalityFactor: 8.10, expenseFactor: 1.80, profitMargin: 1.30 },
  { age: 65, gender: 'male', tobaccoUse: false, riskClass: 'standard', product: 'term_life_10', ratePerThousand: 22.50, mortalityFactor: 17.20, expenseFactor: 3.10, profitMargin: 2.20 },
  { age: 65, gender: 'female', tobaccoUse: false, riskClass: 'standard', product: 'term_life_10', ratePerThousand: 16.80, mortalityFactor: 12.70, expenseFactor: 2.40, profitMargin: 1.70 },
];

export const customers: Customer[] = [
  {
    id: 'cust-7f3a9b2e',
    firstName: 'Michael', lastName: 'Thornberry',
    dateOfBirth: '1978-03-14', ssn: '***-**-4521',
    email: 'mthornberry@email.com', phone: '(512) 555-0147',
    address: { street: '4821 Ridgeview Dr', city: 'Austin', state: 'TX', zip: '78731' },
    occupation: 'Software Engineering Director', employer: 'TechCorp Inc',
    annualIncome: 245000, identityVerified: true, kycStatus: 'cleared',
    createdAt: '2026-07-18T09:00:00Z', updatedAt: '2026-07-18T09:22:00Z',
  },
  {
    id: 'cust-2c8d4e1f',
    firstName: 'Sarah', lastName: 'Jennings',
    dateOfBirth: '1985-11-02', ssn: '***-**-7832',
    email: 'sjennings@email.com', phone: '(404) 555-0293',
    address: { street: '1190 Peachtree Blvd NE', city: 'Atlanta', state: 'GA', zip: '30309' },
    occupation: 'Cardiologist', employer: 'Piedmont Healthcare',
    annualIncome: 475000, identityVerified: true, kycStatus: 'cleared',
    createdAt: '2026-07-20T11:00:00Z', updatedAt: '2026-07-20T11:45:00Z',
  },
  {
    id: 'cust-5a1b9c3d',
    firstName: 'Robert', lastName: 'Vasquez',
    dateOfBirth: '1962-08-29', ssn: '***-**-1094',
    email: 'rvasquez@email.com', phone: '(305) 555-0811',
    address: { street: '7742 Coral Way', city: 'Miami', state: 'FL', zip: '33155' },
    occupation: 'Restaurant Owner', employer: 'Vasquez Hospitality Group',
    annualIncome: 185000, identityVerified: true, kycStatus: 'cleared',
    createdAt: '2026-07-22T08:00:00Z', updatedAt: '2026-07-22T08:15:00Z',
  },
];

export const paymentMethods: PaymentMethod[] = [
  {
    id: 'pm-001', customerId: 'cust-7f3a9b2e', type: 'ach',
    bankName: 'Chase Bank', routingLast4: '4892', accountLast4: '7751',
    verified: true, createdAt: '2026-07-18T09:25:00Z',
  },
  {
    id: 'pm-002', customerId: 'cust-2c8d4e1f', type: 'ach',
    bankName: 'Wells Fargo', routingLast4: '1203', accountLast4: '3344',
    verified: true, createdAt: '2026-07-20T12:00:00Z',
  },
];

export const payments: Payment[] = [
  {
    id: 'pay-001', policyId: 'pol-a1b2c3', customerId: 'cust-7f3a9b2e',
    amount: 5472, type: 'initial_premium', status: 'completed',
    paymentMethodId: 'pm-001', processedAt: '2026-08-01T10:00:00Z',
    receiptId: 'rcp-001', referenceNumber: 'REF-2026-08-00001',
  },
];
