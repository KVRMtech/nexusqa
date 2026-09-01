import { NextRequest, NextResponse } from 'next/server';
import { claims } from '@/lib/data';

export async function GET(request: NextRequest) {
  const { searchParams } = new URL(request.url);
  const status = searchParams.get('status');
  const page = parseInt(searchParams.get('page') || '1');
  const limit = parseInt(searchParams.get('limit') || '25');

  let filtered = [...claims];
  if (status) filtered = filtered.filter(c => c.status === status);

  const start = (page - 1) * limit;
  const paged = filtered.slice(start, start + limit);

  return NextResponse.json({
    data: paged,
    meta: { total: filtered.length, page, limit, totalPages: Math.ceil(filtered.length / limit) },
  });
}

export async function POST(request: NextRequest) {
  const body = await request.json();

  if (!body.policyNumber || !body.claimType || !body.dateOfLoss) {
    return NextResponse.json({ error: 'validation_error', message: 'Missing required fields: policyNumber, claimType, dateOfLoss' }, { status: 422 });
  }

  const newClaim = {
    id: `clm-${crypto.randomUUID().slice(0, 6)}`,
    claimNumber: `CL-2026-${String(claims.length + 302).padStart(5, '0')}`,
    policyId: body.policyId || 'unknown',
    policyNumber: body.policyNumber,
    type: body.claimType,
    status: 'reported',
    claimant: {
      firstName: body.claimantFirstName,
      lastName: body.claimantLastName,
      relationship: body.claimantRelationship,
      phone: body.claimantPhone,
      email: body.claimantEmail,
    },
    dateOfLoss: body.dateOfLoss,
    reportedAt: new Date().toISOString(),
    documents: [],
    investigationNotes: [],
    payments: [],
  };

  return NextResponse.json({ data: newClaim, message: 'Claim registered successfully' }, { status: 201 });
}
