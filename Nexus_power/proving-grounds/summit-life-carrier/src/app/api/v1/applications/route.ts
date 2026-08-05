import { NextRequest, NextResponse } from 'next/server';
import { applications, vendorOrders, suspenseItems } from '@/lib/data';

export async function GET(request: NextRequest) {
  const { searchParams } = new URL(request.url);
  const status = searchParams.get('status');
  const page = parseInt(searchParams.get('page') || '1');
  const limit = parseInt(searchParams.get('limit') || '25');
  const sortBy = searchParams.get('sort_by') || 'submittedAt';
  const sortDir = searchParams.get('sort_dir') || 'desc';

  let filtered = [...applications];
  if (status) filtered = filtered.filter(a => a.status === status);

  filtered.sort((a, b) => {
    const aVal = String((a as unknown as Record<string, unknown>)[sortBy] || '');
    const bVal = String((b as unknown as Record<string, unknown>)[sortBy] || '');
    return sortDir === 'desc' ? bVal.localeCompare(aVal) : aVal.localeCompare(bVal);
  });

  const start = (page - 1) * limit;
  const paged = filtered.slice(start, start + limit);

  return NextResponse.json({
    data: paged,
    meta: { total: filtered.length, page, limit, totalPages: Math.ceil(filtered.length / limit) },
    _links: {
      self: `/api/v1/applications?page=${page}&limit=${limit}`,
      ...(start + limit < filtered.length ? { next: `/api/v1/applications?page=${page + 1}&limit=${limit}` } : {}),
      ...(page > 1 ? { prev: `/api/v1/applications?page=${page - 1}&limit=${limit}` } : {}),
    },
  });
}

export async function POST(request: NextRequest) {
  const body = await request.json();

  if (!body.applicant?.firstName || !body.applicant?.lastName || !body.product || !body.faceAmount) {
    return NextResponse.json({ error: 'validation_error', message: 'Missing required fields: applicant.firstName, applicant.lastName, product, faceAmount', details: [] }, { status: 422 });
  }

  const newApp = {
    id: `app-${crypto.randomUUID().slice(0, 8)}`,
    caseNumber: `UW-2026-${String(applications.length + 147).padStart(5, '0')}`,
    ...body,
    status: 'submitted',
    submittedAt: new Date().toISOString(),
    lastUpdated: new Date().toISOString(),
    notes: [],
  };

  return NextResponse.json({ data: newApp, message: 'Application submitted successfully' }, { status: 201 });
}
