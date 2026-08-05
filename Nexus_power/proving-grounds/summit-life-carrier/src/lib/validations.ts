import { z } from 'zod';

export const loginSchema = z.object({
  email: z.string().email('Valid email address required'),
  password: z.string().min(8, 'Password must be at least 8 characters'),
  mfaCode: z.string().length(6, 'MFA code must be 6 digits').optional(),
});

export const vendorOrderSchema = z.object({
  applicationId: z.string().min(1),
  vendor: z.enum(['MVR', 'MIB', 'ESI', 'Rx_PBM', 'APS', 'Paramedical', 'Inspection']),
  priority: z.enum(['routine', 'rush', 'stat']).default('routine'),
  notes: z.string().max(500).optional(),
});

export const suspenseSchema = z.object({
  applicationId: z.string().min(1),
  reason: z.enum(['missing_document', 'medical_records', 'financial_verification', 'identity_verification', 'beneficiary_clarification', 'agent_follow_up', 'compliance_review', 'reinsurance_review']),
  description: z.string().min(10, 'Description must be at least 10 characters').max(1000),
  dueDate: z.string().min(1, 'Due date is required'),
});

export const underwritingDecisionSchema = z.object({
  applicationId: z.string().min(1),
  decision: z.enum(['approve', 'approve_rated', 'decline', 'counter_offer', 'postpone']),
  riskClass: z.enum(['preferred_plus', 'preferred', 'standard_plus', 'standard', 'substandard_table_a', 'substandard_table_b', 'substandard_table_c', 'substandard_table_d', 'decline']).optional(),
  tableRating: z.string().optional(),
  premiumAmount: z.number().positive().optional(),
  rationale: z.string().min(20, 'Rationale must be at least 20 characters'),
  conditions: z.string().optional(),
  requiresManagerApproval: z.boolean().default(false),
});

export const serviceTransactionSchema = z.object({
  policyId: z.string().min(1),
  type: z.enum(['address_change', 'beneficiary_change', 'name_change', 'premium_mode_change', 'policy_loan', 'loan_repayment', 'partial_surrender', 'full_surrender', 'reinstatement', 'conversion', 'face_increase', 'face_decrease', 'dividend_option', 'automatic_premium_loan', 'paid_up_additions']),
  details: z.record(z.unknown()),
});

export const addressChangeSchema = z.object({
  street: z.string().min(1, 'Street address is required'),
  city: z.string().min(1, 'City is required'),
  state: z.string().length(2, 'State must be 2-letter code'),
  zip: z.string().regex(/^\d{5}(-\d{4})?$/, 'Invalid ZIP code format'),
});

export const beneficiaryChangeSchema = z.object({
  beneficiaries: z.array(z.object({
    name: z.string().min(1),
    relationship: z.string().min(1),
    percentage: z.number().min(0).max(100),
    type: z.enum(['primary', 'contingent']),
    irrevocable: z.boolean(),
  })),
});

export const claimIntakeSchema = z.object({
  policyNumber: z.string().min(1, 'Policy number is required'),
  claimType: z.enum(['death', 'accelerated_death', 'waiver_of_premium', 'accidental_death', 'dismemberment']),
  dateOfLoss: z.string().min(1, 'Date of loss is required'),
  claimantFirstName: z.string().min(1, 'First name is required'),
  claimantLastName: z.string().min(1, 'Last name is required'),
  claimantRelationship: z.string().min(1, 'Relationship is required'),
  claimantPhone: z.string().min(10, 'Valid phone number required'),
  claimantEmail: z.string().email('Valid email required'),
  causeOfDeath: z.string().optional(),
  description: z.string().min(20, 'Please provide details about the claim'),
});

export const rateQuoteSchema = z.object({
  age: z.number().int().min(18).max(85),
  gender: z.enum(['male', 'female']),
  tobaccoUse: z.boolean(),
  product: z.enum(['whole_life', 'term_life_10', 'term_life_20', 'term_life_30', 'universal_life', 'variable_universal', 'indexed_universal']),
  faceAmount: z.number().min(25000).max(50000000),
  riskClass: z.enum(['preferred_plus', 'preferred', 'standard_plus', 'standard', 'substandard_table_a', 'substandard_table_b', 'substandard_table_c', 'substandard_table_d']),
});

export type LoginFormData = z.infer<typeof loginSchema>;
export type VendorOrderFormData = z.infer<typeof vendorOrderSchema>;
export type SuspenseFormData = z.infer<typeof suspenseSchema>;
export type UnderwritingDecisionFormData = z.infer<typeof underwritingDecisionSchema>;
export type ServiceTransactionFormData = z.infer<typeof serviceTransactionSchema>;
export type AddressChangeFormData = z.infer<typeof addressChangeSchema>;
export type BeneficiaryChangeFormData = z.infer<typeof beneficiaryChangeSchema>;
export type ClaimIntakeFormData = z.infer<typeof claimIntakeSchema>;
export type RateQuoteFormData = z.infer<typeof rateQuoteSchema>;
