import { NextRequest } from 'next/server';
import { serviceResponse, errorResponse, requireAuth } from '@/lib/api-factory';

const TASKS: Record<string, Record<string, unknown>> = {
  'task-001': { id: 'task-001', type: 'underwriting_review', title: 'Review Thornberry Application', assignedTo: 'usr-002', applicationId: 'app-7f3a9b2e', status: 'open', priority: 'high', dueDate: '2026-08-05', createdAt: '2026-07-25T09:00:00Z' },
  'task-002': { id: 'task-002', type: 'medical_review', title: 'APS Review - Jennings', assignedTo: 'usr-001', applicationId: 'app-2c8d4e1f', status: 'pending', priority: 'medium', dueDate: '2026-08-10', createdAt: '2026-07-26T14:00:00Z' },
  'task-003': { id: 'task-003', type: 'compliance_check', title: 'OFAC Re-screen Vasquez', assignedTo: 'usr-003', applicationId: 'app-5a1b9c3d', status: 'open', priority: 'low', dueDate: '2026-08-12', createdAt: '2026-07-27T08:30:00Z' },
};

type Ctx = { params: Promise<{ taskId: string }> };

export async function GET(request: NextRequest, { params }: Ctx) {
  const authErr = requireAuth(request, 'WorkflowService');
  if (authErr) return authErr;

  const { taskId } = await params;
  const task = TASKS[taskId];
  if (!task) return errorResponse('WorkflowService', 'Task not found', 404);

  return serviceResponse('WorkflowService', task);
}

export async function PUT(request: NextRequest, { params }: Ctx) {
  const authErr = requireAuth(request, 'WorkflowService');
  if (authErr) return authErr;

  const { taskId } = await params;
  const task = TASKS[taskId];
  if (!task) return errorResponse('WorkflowService', 'Task not found', 404);

  const body = await request.json();
  const updated = { ...task, ...body, id: taskId, updatedAt: new Date().toISOString() };

  return serviceResponse('WorkflowService', updated);
}
