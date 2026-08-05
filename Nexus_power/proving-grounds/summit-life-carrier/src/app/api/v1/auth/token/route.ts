import { NextRequest, NextResponse } from 'next/server';
import { adminUsers } from '@/lib/data';

export async function POST(request: NextRequest) {
  const body = await request.json();
  const { email, password } = body;

  if (!email || !password) {
    return NextResponse.json({ error: 'invalid_request', message: 'Email and password are required' }, { status: 400 });
  }

  if (password.length < 8) {
    return NextResponse.json({ error: 'invalid_credentials', message: 'Authentication failed' }, { status: 401 });
  }

  const user = adminUsers.find(u => u.email === email) || adminUsers[0];

  const tokenPayload = { sub: user.id, email: user.email, role: user.role, iat: Math.floor(Date.now() / 1000), exp: Math.floor(Date.now() / 1000) + 3600 };
  const accessToken = `eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.${Buffer.from(JSON.stringify(tokenPayload)).toString('base64url')}.mock-rs256-signature`;

  return NextResponse.json({
    access_token: accessToken,
    token_type: 'Bearer',
    expires_in: 3600,
    scope: 'openid profile email',
    id_token: `eyJhbGciOiJSUzI1NiJ9.${Buffer.from(JSON.stringify({ sub: user.id, name: user.name, email: user.email, iss: 'https://auth.summitlife.com', aud: 'carrier-admin' })).toString('base64url')}.mock-id-signature`,
    user: { id: user.id, name: user.name, email: user.email, role: user.role, department: user.department },
  });
}
