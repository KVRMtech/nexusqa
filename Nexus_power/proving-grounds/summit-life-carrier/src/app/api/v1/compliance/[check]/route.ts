import { NextRequest } from 'next/server';
import { serviceResponse, errorResponse, requireAuth } from '@/lib/api-factory';

export async function POST(request: NextRequest, { params }: { params: Promise<{ check: string }> }) {
  const { check } = await params;
  const authErr = requireAuth(request, 'ComplianceCheck');
  if (authErr) return authErr;
  const body = await request.json();

  switch (check) {
    case 'ofac':
      return serviceResponse('ComplianceCheck', { applicantId: body.applicantId, screened: true, matchFound: false, listsChecked: ['SDN', 'Consolidated', 'Non-SDN'], searchDate: new Date().toISOString() });
    case 'state-regulatory':
      return serviceResponse('ComplianceCheck', { applicantId: body.applicantId, state: 'TX', productApproved: true, filingStatus: 'active', restrictions: [], maxFaceAmount: null });
    case 'suitability':
      return serviceResponse('ComplianceCheck', { applicantId: body.applicantId, suitable: true, score: 88, factors: [{ factor: 'income_to_coverage_ratio', value: 8.2, threshold: 25, pass: true }] });
    case 'replacement':
      return serviceResponse('ComplianceCheck', { applicantId: body.applicantId, is1035Exchange: false, existingCoverage: [{ carrier: 'MetLife', faceAmount: 500000, type: 'group_term' }], replacementFormRequired: false });
    default:
      return errorResponse('ComplianceCheck', 'Unknown compliance check: ' + check, 400);
  }
}
