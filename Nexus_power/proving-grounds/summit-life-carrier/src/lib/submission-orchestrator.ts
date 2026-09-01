import { ApiCallStep, ApiCallResult, ApiCallPhase } from './types';

// Application submission flow — ~29 API calls in sequence/parallel groups
export function applicationSubmissionSteps(applicantData: Record<string, unknown>): ApiCallStep[] {
  const customerId = `cust-${crypto.randomUUID().slice(0, 8)}`;
  return [
    // Phase 1: Customer Verification (5 calls)
    { id: 'create-customer', name: 'Create Customer Record', endpoint: '/api/v1/customers', method: 'POST', phase: 'customer_verification', body: { ...applicantData } },
    { id: 'verify-identity', name: 'Identity Verification (LexisNexis)', endpoint: `/api/v1/customers/${customerId}/identity-verification`, method: 'POST', phase: 'customer_verification', body: { customerId, type: 'kba' } },
    { id: 'validate-address', name: 'USPS Address Standardization', endpoint: `/api/v1/customers/${customerId}/address-validation`, method: 'POST', phase: 'customer_verification', body: { customerId, address: applicantData.address } },
    { id: 'kyc-aml', name: 'KYC/AML Screening (World-Check)', endpoint: `/api/v1/customers/${customerId}/kyc-aml`, method: 'POST', phase: 'customer_verification', body: { customerId } },
    { id: 'verify-employment', name: 'Employment Verification (TWN)', endpoint: `/api/v1/customers/${customerId}/employment`, method: 'POST', phase: 'customer_verification', body: { customerId, employer: applicantData.employer } },

    // Phase 2: Vendor Checks (8 calls)
    { id: 'mib-check', name: 'MIB Code Check', endpoint: '/api/v1/vendors/mib', method: 'POST', phase: 'vendor_checks', body: { applicantId: customerId } },
    { id: 'mvr-check', name: 'Motor Vehicle Report', endpoint: '/api/v1/vendors/mvr', method: 'POST', phase: 'vendor_checks', body: { applicantId: customerId, state: 'TX' } },
    { id: 'rx-check', name: 'Prescription History (PBM)', endpoint: '/api/v1/vendors/rx-history', method: 'POST', phase: 'vendor_checks', body: { applicantId: customerId } },
    { id: 'credit-check', name: 'Credit & Financial Check', endpoint: '/api/v1/vendors/credit-check', method: 'POST', phase: 'vendor_checks', body: { applicantId: customerId } },
    { id: 'identity-proof', name: 'Identity Proofing', endpoint: '/api/v1/vendors/identity-proofing', method: 'POST', phase: 'vendor_checks', body: { applicantId: customerId } },
    { id: 'paramedical', name: 'Schedule Paramedical Exam', endpoint: '/api/v1/vendors/paramedical', method: 'POST', phase: 'vendor_checks', body: { applicantId: customerId, examType: 'full_blood_urine' } },
    { id: 'aps-request', name: 'Request APS Records', endpoint: '/api/v1/vendors/aps', method: 'POST', phase: 'vendor_checks', body: { applicantId: customerId, physician: 'Dr. Katherine Reeves' } },
    { id: 'inspection', name: 'Order Inspection Report', endpoint: '/api/v1/vendors/inspection', method: 'POST', phase: 'vendor_checks', body: { applicantId: customerId } },

    // Phase 3: Underwriting (4 calls)
    { id: 'risk-score', name: 'Risk Score Calculation', endpoint: '/api/v1/underwriting/risk-score', method: 'POST', phase: 'underwriting', body: { applicantId: customerId, faceAmount: applicantData.faceAmount } },
    { id: 'rules-engine', name: 'Automated Rules Engine', endpoint: '/api/v1/underwriting/rules-engine', method: 'POST', phase: 'underwriting', body: { applicantId: customerId } },
    { id: 'anti-fraud', name: 'Anti-Fraud Scoring', endpoint: '/api/v1/underwriting/anti-fraud', method: 'POST', phase: 'underwriting', body: { applicantId: customerId } },
    { id: 'reinsurance', name: 'Reinsurance Treaty Check', endpoint: '/api/v1/underwriting/reinsurance', method: 'POST', phase: 'underwriting', body: { applicantId: customerId, faceAmount: applicantData.faceAmount } },

    // Phase 4: Compliance (4 calls)
    { id: 'ofac-screen', name: 'OFAC Sanctions Screening', endpoint: '/api/v1/compliance/ofac', method: 'POST', phase: 'compliance', body: { applicantId: customerId } },
    { id: 'state-reg', name: 'State Regulatory Check', endpoint: '/api/v1/compliance/state-regulatory', method: 'POST', phase: 'compliance', body: { state: 'TX', product: applicantData.product } },
    { id: 'suitability', name: 'Suitability Assessment', endpoint: '/api/v1/compliance/suitability', method: 'POST', phase: 'compliance', body: { applicantId: customerId, faceAmount: applicantData.faceAmount, annualIncome: applicantData.annualIncome } },
    { id: 'replacement', name: '1035 Exchange / Replacement Check', endpoint: '/api/v1/compliance/replacement', method: 'POST', phase: 'compliance', body: { applicantId: customerId } },

    // Phase 5: Rating (3 calls)
    { id: 'product-avail', name: 'Product Availability Check', endpoint: '/api/v1/rating/product-availability', method: 'POST', phase: 'rating', body: { state: 'TX', product: applicantData.product } },
    { id: 'calc-premium', name: 'Premium Calculation', endpoint: '/api/v1/rating/premium', method: 'POST', phase: 'rating', body: { applicantId: customerId, product: applicantData.product, faceAmount: applicantData.faceAmount } },
    { id: 'gen-illustration', name: 'Generate Illustration', endpoint: '/api/v1/rating/illustration', method: 'POST', phase: 'rating', body: { applicantId: customerId, product: applicantData.product, faceAmount: applicantData.faceAmount } },

    // Phase 6: Documentation (2 calls)
    { id: 'gen-app-packet', name: 'Generate Application Packet', endpoint: '/api/v1/documents/generate', method: 'POST', phase: 'documentation', body: { type: 'application_packet', referenceId: customerId } },
    { id: 'gen-disclosure', name: 'Generate Disclosure Forms', endpoint: '/api/v1/documents/generate', method: 'POST', phase: 'documentation', body: { type: 'disclosure', referenceId: customerId } },

    // Phase 7: Finalization (5 calls)
    { id: 'agent-commission', name: 'Calculate Agent Commission', endpoint: '/api/v1/agents/commission', method: 'POST', phase: 'finalization', body: { applicantId: customerId, product: applicantData.product, premiumAmount: 4875 } },
    { id: 'create-tasks', name: 'Create Workflow Tasks', endpoint: '/api/v1/workflow/tasks', method: 'POST', phase: 'finalization', body: { type: 'underwriting_review', referenceId: customerId, assignedTo: 'usr-002' } },
    { id: 'send-confirmation', name: 'Dispatch Confirmation Notice', endpoint: '/api/v1/notifications', method: 'POST', phase: 'finalization', body: { type: 'application_received', recipient: applicantData.email, channel: 'email' } },
    { id: 'audit-log', name: 'Create Audit Trail Entry', endpoint: '/api/v1/audit', method: 'POST', phase: 'finalization', body: { action: 'application_submitted', entity: 'application', entityId: customerId, performedBy: 'usr-002' } },
    { id: 'submit-application', name: 'Submit Application Record', endpoint: '/api/v1/applications', method: 'POST', phase: 'finalization', body: { applicant: applicantData, product: applicantData.product, faceAmount: applicantData.faceAmount } },
  ];
}

// Customer profile creation flow — ~10 API calls
export function customerProfileSteps(profileData: Record<string, unknown>): ApiCallStep[] {
  const customerId = `cust-${crypto.randomUUID().slice(0, 8)}`;
  return [
    { id: 'create-profile', name: 'Create Customer Record', endpoint: '/api/v1/customers', method: 'POST', phase: 'customer_verification', body: profileData },
    { id: 'verify-identity', name: 'Identity Verification', endpoint: `/api/v1/customers/${customerId}/identity-verification`, method: 'POST', phase: 'customer_verification', body: { customerId } },
    { id: 'validate-address', name: 'USPS Address Validation', endpoint: `/api/v1/customers/${customerId}/address-validation`, method: 'POST', phase: 'customer_verification', body: { customerId, address: profileData.address } },
    { id: 'kyc-screening', name: 'KYC/AML Screening', endpoint: `/api/v1/customers/${customerId}/kyc-aml`, method: 'POST', phase: 'customer_verification', body: { customerId } },
    { id: 'ofac-screen', name: 'OFAC Sanctions Check', endpoint: '/api/v1/compliance/ofac', method: 'POST', phase: 'compliance', body: { applicantId: customerId } },
    { id: 'employment-verify', name: 'Employment Verification', endpoint: `/api/v1/customers/${customerId}/employment`, method: 'POST', phase: 'customer_verification', body: { customerId, employer: profileData.employer } },
    { id: 'add-beneficiaries', name: 'Register Beneficiaries', endpoint: `/api/v1/customers/${customerId}/beneficiaries`, method: 'POST', phase: 'documentation', body: { customerId, beneficiaries: [{ name: 'Spouse', relationship: 'spouse', percentage: 100 }] } },
    { id: 'upload-documents', name: 'Upload Verification Docs', endpoint: `/api/v1/customers/${customerId}/documents`, method: 'POST', phase: 'documentation', body: { customerId, type: 'identification', documentName: 'Government ID' } },
    { id: 'send-welcome', name: 'Dispatch Welcome Notice', endpoint: '/api/v1/notifications', method: 'POST', phase: 'finalization', body: { type: 'profile_created', recipient: profileData.email, channel: 'email' } },
    { id: 'audit-log', name: 'Create Audit Trail', endpoint: '/api/v1/audit', method: 'POST', phase: 'finalization', body: { action: 'customer_created', entity: 'customer', entityId: customerId, performedBy: 'usr-006' } },
  ];
}

// Payment processing flow — ~10 API calls
export function paymentProcessingSteps(paymentData: Record<string, unknown>): ApiCallStep[] {
  const methodId = `pm-${crypto.randomUUID().slice(0, 8)}`;
  const paymentId = `pay-${crypto.randomUUID().slice(0, 8)}`;
  return [
    { id: 'register-method', name: 'Register Payment Method', endpoint: '/api/v1/payments/methods', method: 'POST', phase: 'customer_verification', body: { customerId: paymentData.customerId, type: 'ach', bankName: paymentData.bankName, routingNumber: paymentData.routingNumber, accountNumber: paymentData.accountNumber } },
    { id: 'validate-method', name: 'Validate Payment Method', endpoint: `/api/v1/payments/methods/${methodId}/validate`, method: 'POST', phase: 'customer_verification', body: { methodId } },
    { id: 'verify-bank', name: 'Bank Account Verification', endpoint: '/api/v1/payments/bank-account/verify', method: 'POST', phase: 'customer_verification', body: { methodId, routingNumber: paymentData.routingNumber } },
    { id: 'process-payment', name: 'Process Payment', endpoint: '/api/v1/payments/process', method: 'POST', phase: 'finalization', body: { policyId: paymentData.policyId, amount: paymentData.amount, paymentMethodId: methodId } },
    { id: 'allocate-premium', name: 'Premium Allocation', endpoint: '/api/v1/payments/premium-allocation', method: 'POST', phase: 'finalization', body: { paymentId, policyId: paymentData.policyId, amount: paymentData.amount } },
    { id: 'calc-commission', name: 'Commission Calculation', endpoint: '/api/v1/payments/commission', method: 'POST', phase: 'finalization', body: { paymentId, amount: paymentData.amount } },
    { id: 'calc-tax', name: 'Tax Withholding Check', endpoint: '/api/v1/payments/tax-withholding', method: 'POST', phase: 'finalization', body: { paymentId, amount: paymentData.amount, type: 'premium' } },
    { id: 'gen-receipt', name: 'Generate Receipt', endpoint: '/api/v1/payments/receipt', method: 'POST', phase: 'documentation', body: { paymentId, amount: paymentData.amount } },
    { id: 'send-confirmation', name: 'Payment Confirmation', endpoint: '/api/v1/notifications', method: 'POST', phase: 'finalization', body: { type: 'payment_processed', recipient: paymentData.email, channel: 'email' } },
    { id: 'audit-log', name: 'Payment Audit Trail', endpoint: '/api/v1/audit', method: 'POST', phase: 'finalization', body: { action: 'payment_processed', entity: 'payment', entityId: paymentId, performedBy: 'usr-006' } },
  ];
}

// Phase display metadata
export const PHASE_LABELS: Record<ApiCallPhase, { label: string; color: string }> = {
  customer_verification: { label: 'Customer Verification', color: 'text-blue-500' },
  vendor_checks: { label: 'Vendor & Third-Party Checks', color: 'text-purple-500' },
  underwriting: { label: 'Underwriting Engine', color: 'text-amber-500' },
  compliance: { label: 'Compliance & Regulatory', color: 'text-red-500' },
  rating: { label: 'Rating & Illustration', color: 'text-emerald-500' },
  documentation: { label: 'Document Generation', color: 'text-cyan-500' },
  finalization: { label: 'Finalization & Audit', color: 'text-gray-500' },
};

// Execute a single API call with auth token
async function executeCall(step: ApiCallStep, token: string): Promise<ApiCallResult> {
  const startedAt = Date.now();
  try {
    const res = await fetch(step.endpoint, {
      method: step.method,
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
      body: step.method !== 'GET' ? JSON.stringify(step.body ?? {}) : undefined,
    });
    const response = await res.json();
    return { step, status: 'completed', response, startedAt, completedAt: Date.now() };
  } catch (err) {
    return { step, status: 'failed', error: String(err), startedAt, completedAt: Date.now() };
  }
}

// Execute all steps sequentially, calling onProgress after each one
export async function executeFlow(
  steps: ApiCallStep[],
  token: string,
  onProgress: (results: ApiCallResult[]) => void,
): Promise<ApiCallResult[]> {
  const results: ApiCallResult[] = steps.map(step => ({ step, status: 'pending' as const }));
  onProgress([...results]);

  for (let i = 0; i < steps.length; i++) {
    results[i] = { ...results[i], status: 'running', startedAt: Date.now() };
    onProgress([...results]);

    const result = await executeCall(steps[i], token);
    results[i] = result;
    onProgress([...results]);
  }

  return results;
}
