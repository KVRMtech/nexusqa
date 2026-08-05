import { NextRequest } from 'next/server';
import { serviceResponse, errorResponse, requireAuth } from '@/lib/api-factory';

export async function POST(request: NextRequest, { params }: { params: Promise<{ engine: string }> }) {
  const { engine } = await params;
  const authErr = requireAuth(request, 'UnderwritingEngine');
  if (authErr) return authErr;
  const body = await request.json();

  switch (engine) {
    case 'risk-score':
      return serviceResponse('UnderwritingEngine', { applicantId: body.applicantId, score: 82, riskClass: 'preferred', factors: [{ name: 'age', impact: -5 }, { name: 'bmi', impact: -3 }, { name: 'occupation', impact: 2 }], confidence: 0.94 });
    case 'rules-engine':
      return serviceResponse('UnderwritingEngine', { applicantId: body.applicantId, decision: 'proceed', rulesEvaluated: 47, rulesFired: 3, flags: [{ rule: 'TOBACCO_DISCLOSURE', action: 'review' }], automatedDecision: false });
    case 'reinsurance':
      return serviceResponse('UnderwritingEngine', { applicantId: body.applicantId, required: true, treaty: 'Automatic', retentionLimit: 5000000, cededAmount: 0, facultativeRequired: false });
    case 'anti-fraud':
      return serviceResponse('UnderwritingEngine', { applicantId: body.applicantId, fraudScore: 0.08, riskLevel: 'low', indicators: [], sipScore: 0.02, modelVersion: 'v3.2' });
    default:
      return errorResponse('UnderwritingEngine', 'Unknown underwriting engine: ' + engine, 400);
  }
}
