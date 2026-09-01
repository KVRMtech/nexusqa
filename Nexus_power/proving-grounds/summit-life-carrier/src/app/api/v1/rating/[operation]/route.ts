import { NextRequest } from 'next/server';
import { serviceResponse, errorResponse, requireAuth } from '@/lib/api-factory';

export async function POST(request: NextRequest, { params }: { params: Promise<{ operation: string }> }) {
  const { operation } = await params;
  const authErr = requireAuth(request, 'RatingEngine');
  if (authErr) return authErr;
  const body = await request.json();

  switch (operation) {
    case 'premium':
      return serviceResponse('RatingEngine', { applicantId: body.applicantId, annualPremium: 4875, monthlyEquivalent: 421.50, ratePerThousand: 1.95, riskClass: 'standard_plus', factors: { base: 3800, mortality: 450, expense: 375, profit: 250 }, guaranteedRate: 5200 });
    case 'illustration':
      return serviceResponse('RatingEngine', { applicantId: body.applicantId, illustrationId: 'ILL-2026-00891', format: 'pdf', generatedAt: new Date().toISOString(), projections: [{ year: 1, premium: 4875, cashValue: 0, deathBenefit: 2500000 }, { year: 5, premium: 4875, cashValue: 12400, deathBenefit: 2500000 }, { year: 10, premium: 4875, cashValue: 38200, deathBenefit: 2500000 }] });
    case 'product-availability':
      return serviceResponse('RatingEngine', { applicantId: body.applicantId, available: true, products: [{ code: 'TL20', name: 'Term Life 20-Year', available: true }, { code: 'WL', name: 'Whole Life', available: true }], state: 'TX', restrictions: [] });
    default:
      return errorResponse('RatingEngine', 'Unknown rating operation: ' + operation, 400);
  }
}
