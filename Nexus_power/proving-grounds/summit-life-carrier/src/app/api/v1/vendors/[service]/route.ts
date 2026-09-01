import { NextRequest } from 'next/server';
import { serviceResponse, errorResponse, requireAuth } from '@/lib/api-factory';

export async function POST(request: NextRequest, { params }: { params: Promise<{ service: string }> }) {
  const { service } = await params;
  const authErr = requireAuth(request, 'VendorGateway');
  if (authErr) return authErr;
  const body = await request.json();
  const { applicantId } = body;

  switch (service) {
    case 'mib':
      return serviceResponse('VendorGateway', { applicantId, codes: [{ code: '070', description: 'Build/Height-Weight' }], matchFound: false, reportId: 'MIB-2026-44821' });
    case 'mvr':
      return serviceResponse('VendorGateway', { applicantId, violations: [], accidentsLast5Years: 0, licenseStatus: 'valid', state: 'TX' });
    case 'rx-history':
      return serviceResponse('VendorGateway', { applicantId, medications: [{ name: 'Lisinopril', prescribedDate: '2024-01-15', prescriber: 'Dr. Smith' }], controlledSubstances: false });
    case 'credit-check':
      return serviceResponse('VendorGateway', { applicantId, score: 742, reportDate: '2026-07-28', derogatory: 0, bankruptcies: 0, collections: 0 });
    case 'identity-proofing':
      return serviceResponse('VendorGateway', { applicantId, verified: true, matchScore: 0.97, sources: ['LexisNexis', 'TransUnion'] });
    case 'paramedical':
      return serviceResponse('VendorGateway', { applicantId, scheduled: true, examDate: '2026-08-10', examiner: 'Allied Medical', location: 'Austin TX', examType: 'full_blood_urine' });
    case 'inspection':
      return serviceResponse('VendorGateway', { applicantId, orderId: 'INS-2026-8831', inspectorAssigned: 'Investigative Services Inc', estimatedCompletion: '2026-08-15' });
    case 'aps':
      return serviceResponse('VendorGateway', { applicantId, requestId: 'APS-2026-1192', physician: 'Dr. Katherine Reeves', facility: 'Piedmont Heart Institute', estimatedDays: 14 });
    default:
      return errorResponse('VendorGateway', 'Unknown vendor service: ' + service, 400);
  }
}
