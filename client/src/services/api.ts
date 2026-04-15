import axios, { AxiosInstance, AxiosError } from 'axios';

const API_BASE = import.meta.env.VITE_API_BASE || '/api';

class ApiClient {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      baseURL: API_BASE,
      timeout: 30_000,
      headers: { 'Content-Type': 'application/json' },
    });

    // Attach auth token
    this.client.interceptors.request.use((config) => {
      const token = localStorage.getItem('nexus_token');
      if (token) {
        config.headers.Authorization = `Bearer ${token}`;
      }
      return config;
    });

    // Handle 401 → redirect to login
    this.client.interceptors.response.use(
      (res) => res,
      (err: AxiosError) => {
        if (err.response?.status === 401) {
          localStorage.removeItem('nexus_token');
          localStorage.removeItem('nexus_user');
          window.location.href = '/login';
        }
        return Promise.reject(err);
      },
    );
  }

  // ── Auth ─────────────────────────────────────────────────
  async login(email: string, password: string) {
    const { data } = await this.client.post('/v1/auth/login', { email, password });
    return data;
  }

  async register(tenantName: string, email: string, password: string, name: string) {
    // Use the public self-signup endpoint (no auth required)
    const { data } = await this.client.post('/v1/auth/self-signup', {
      tenant_name: tenantName,
      email,
      password,
      name,
      plan: 'enterprise',
    });
    return {
      tenant: { tenant_id: data.tenant_id, name: tenantName },
      user: { user_id: data.user_id, email: data.email, name: data.name },
      access_token: data.access_token,
      refresh_token: data.refresh_token,
    };
  }

  // ── Dashboard / Health ───────────────────────────────────
  async getEngineHealth(engine: string) {
    const { data } = await this.client.get(`/v1/${engine}/health/ready`);
    return data;
  }

  async getAllEngineHealth() {
    const { ACTIVE_ENGINES } = await import('../productMode');
    const engines = ACTIVE_ENGINES;
    // Serialize health checks to avoid bursting the gateway rate-limiter
    const out: Array<{ engine: string; status: string; error?: string }> = [];
    for (const e of engines) {
      try {
        const d = await this.getEngineHealth(e);
        out.push({ engine: e, ...d });
      } catch (err: any) {
        out.push({ engine: e, status: 'unreachable', error: err?.message });
      }
    }
    return out;
  }

  /**
   * Check health of control-plane services (gateway, auth, orchestrator, platform-api).
   * These use different health endpoint patterns than engines.
   */
  async getControlPlaneHealth(): Promise<Array<{ service: string; status: string }>> {
    const { ACTIVE_CONTROL_PLANE } = await import('../productMode');
    // Serialize to avoid bursting the gateway rate-limiter
    const out: Array<{ service: string; status: string }> = [];
    for (const svc of ACTIVE_CONTROL_PLANE) {
      try {
        const resp = await this.client.get(svc.healthPath, { timeout: 5000 });
        out.push({ service: svc.name, status: resp.status < 500 ? 'healthy' : 'unreachable' });
      } catch {
        out.push({ service: svc.name, status: 'unreachable' });
      }
    }
    return out;
  }

  // ── Ears (Knowledge Transfer) ────────────────────────────
  // Direct engine uploads removed — all ingestion goes through startCanonicalProcessing().

  async getTranscriptionJob(jobId: string) {
    const { data } = await this.client.get(`/v1/ears/jobs/${jobId}`);
    return data;
  }

  // ── Eyes (Visual Analysis) ───────────────────────────────
  // Direct engine uploads removed — all ingestion goes through startCanonicalProcessing().

  async getVideoAnalysisJob(jobId: string) {
    const { data } = await this.client.get(`/v1/eyes/jobs/${jobId}`);
    return data;
  }

  // ── Shield (PII) ────────────────────────────────────────
  async redactText(text: string, tenantId: string) {
    const { data } = await this.client.post('/v1/shield/redact', {
      text,
      tenant_id: tenantId,
      trace_id: crypto.randomUUID(),
    });
    return data;
  }

  async restoreText(safeText: string, mappingId: string, tenantId: string) {
    const { data } = await this.client.post('/v1/shield/reveal', {
      safe_text: safeText,
      mapping_id: mappingId,
      tenant_id: tenantId,
      trace_id: crypto.randomUUID(),
    });
    return data;
  }

  // ── Heart (AI Analysis) ──────────────────────────────────
  async extractRules(transcript: string, tenantId: string, sessionId?: string) {
    const { data } = await this.client.post('/v1/heart/extract-rules', {
      transcript,
      tenant_id: tenantId,
      session_id: sessionId || crypto.randomUUID(),
      trace_id: crypto.randomUUID(),
    });
    return data;
  }

  async generateTests(rules: Record<string, unknown>[], tenantId: string) {
    const { data } = await this.client.post('/v1/heart/generate-tests', {
      rules,
      tenant_id: tenantId,
      trace_id: crypto.randomUUID(),
    });
    return data;
  }

  async analyzeDocument(content: string, tenantId: string) {
    const { data } = await this.client.post('/v1/heart/analyze', {
      content,
      tenant_id: tenantId,
      trace_id: crypto.randomUUID(),
    });
    return data;
  }

  // ── Backbone (Knowledge Graph) ───────────────────────────
  async storeNode(nodeType: string, properties: Record<string, any>, tenantId: string) {
    const { data } = await this.client.post('/v1/backbone/nodes', {
      node_type: nodeType,
      properties,
      tenant_id: tenantId,
      trace_id: crypto.randomUUID(),
    });
    return data;
  }

  async searchKnowledge(query: string, tenantId: string, nodeTypes?: string[]) {
    const { data } = await this.client.post('/v1/backbone/search', {
      query,
      tenant_id: tenantId,
      node_types: nodeTypes,
      trace_id: crypto.randomUUID(),
    });
    return data;
  }

  async getGraphStats(tenantId: string) {
    const { data } = await this.client.get('/v1/backbone/stats');
    return data;
  }

  async listNodesByType(nodeType: string, limit = 50) {
    const { data } = await this.client.get(`/v1/backbone/nodes/type/${nodeType}?limit=${limit}`);
    return data;
  }

  // ── Hands (Test Data) ───────────────────────────────────
  async generateTestData(profiles: number, tenantId: string, config?: Record<string, unknown>) {
    const { data } = await this.client.post('/v1/hands/generate-profiles', {
      count: profiles,
      tenant_id: tenantId,
      config,
      trace_id: crypto.randomUUID(),
    });
    return data;
  }

  // ── Legs (Test Execution) ───────────────────────────────
  async executeTest(testCase: Record<string, unknown>, tenantId: string) {
    const { data } = await this.client.post('/v1/legs/execute', {
      test_case: testCase,
      tenant_id: tenantId,
      trace_id: crypto.randomUUID(),
    });
    return data;
  }

  async getExecutionResult(jobId: string) {
    const { data } = await this.client.get(`/v1/legs/jobs/${jobId}`);
    return data;
  }

  // ── Mouth (Reports) ─────────────────────────────────────
  async generateReport(reportType: string, tenantId: string, data_payload: Record<string, unknown>) {
    const { data } = await this.client.post('/v1/mouth/generate', {
      report_type: reportType,
      tenant_id: tenantId,
      data: data_payload,
      trace_id: crypto.randomUUID(),
    });
    return data;
  }

  async listReports(tenantId: string) {
    const { data } = await this.client.get(`/v1/mouth/reports?tenant_id=${tenantId}`);
    return data;
  }

  // ── Nerves (Integrations) ───────────────────────────────
  async listConnectors() {
    const { data } = await this.client.get('/v1/nerves/connectors');
    return data;
  }

  async configureConnector(connector: string, credentials: Record<string, string>, tenantId: string) {
    const { data } = await this.client.post('/v1/nerves/connectors/configure', {
      connector,
      credentials,
      tenant_id: tenantId,
      trace_id: crypto.randomUUID(),
    });
    return data;
  }

  async executeConnectorAction(connector: string, action: string, parameters: Record<string, any>, tenantId: string) {
    const { data } = await this.client.post('/v1/nerves/execute', {
      connector,
      action,
      parameters,
      tenant_id: tenantId,
      trace_id: crypto.randomUUID(),
    });
    return data;
  }

  // ── Spine (Documents) ───────────────────────────────────
  async ingestDocument(file: File, tenantId: string) {
    const form = new FormData();
    form.append('file', file);
    form.append('tenant_id', tenantId);
    const { data } = await this.client.post('/v1/spine/ingest', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 60_000,
    });
    return data;
  }

  // ── Workflows ────────────────────────────────────────────
  async startWorkflow(chainName: string, tenantId: string, input: Record<string, any>) {
    const { data } = await this.client.post('/v1/orchestrator/workflows/start', {
      chain_name: chainName,
      tenant_id: tenantId,
      input,
      trace_id: crypto.randomUUID(),
    });
    return data;
  }

  async getWorkflowStatus(workflowId: string) {
    const { data } = await this.client.get(`/v1/workflows/${workflowId}`);
    return data;
  }

  async listWorkflows(tenantId: string) {
    const { data } = await this.client.get(`/v1/orchestrator/workflows?tenant_id=${tenantId}`);
    return data;
  }

  // ── Canonical Processing (Phase 3) ──────────────────────
  // Unified entry: upload media + start canonical chain in one call.
  // Replaces the legacy createQASession → upload → runPipeline flow.

  async startCanonicalProcessing(params: {
    sessionId: string;
    audioFile?: File;
    videoFile?: File;
    language?: string;
    numSpeakers?: number;
    processingProfile?: string;
    consumerChainId?: string;
  }): Promise<{
    workflow_id: string;
    chain_id: string;
    chain_name: string;
    status: string;
    session_id: string;
    artifact_id?: string;
  }> {
    const form = new FormData();
    form.append('session_id', params.sessionId);
    if (params.audioFile) form.append('audio', params.audioFile);
    if (params.videoFile) form.append('video', params.videoFile);
    if (params.language) form.append('language', params.language);
    if (params.numSpeakers != null) form.append('num_speakers', String(params.numSpeakers));
    if (params.processingProfile) form.append('processing_profile', params.processingProfile);
    if (params.consumerChainId) form.append('consumer_chain_id', params.consumerChainId);

    const { data } = await this.client.post('/v1/orchestrator/process', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300_000, // 5 min — files can be large
    });
    return data;
  }

  /**
   * Get the SSE stream path for a workflow's progress.
   * Use with the useSSE hook: useSSE({ path: api.getWorkflowStreamPath(id) })
   */
  getWorkflowStreamPath(workflowId: string): string {
    return `/v1/orchestrator/workflows/${workflowId}/stream`;
  }

  // ── Artifacts (Canonical) ─────────────────────────────────

  async getArtifact(artifactId: string): Promise<import('../types/canonical').CanonicalArtifact> {
    const { data } = await this.client.get(`/v1/artifacts/${artifactId}`);
    return data;
  }

  async getArtifactTranscript(artifactId: string): Promise<import('../types/canonical').ArtifactTranscript> {
    const { data } = await this.client.get(`/v1/artifacts/${artifactId}/transcript`);
    return data;
  }

  async listArtifacts(
    tenantId: string,
    filters?: { session_id?: string; status?: string; limit?: number; offset?: number },
  ): Promise<import('../types/canonical').CanonicalArtifact[]> {
    const params: Record<string, string | number> = { tenant_id: tenantId };
    if (filters?.session_id) params.session_id = filters.session_id;
    if (filters?.status) params.status = filters.status;
    if (filters?.limit != null) params.limit = filters.limit;
    if (filters?.offset != null) params.offset = filters.offset;
    const { data } = await this.client.get('/v1/artifacts', { params });
    return data;
  }

  async listSessionArtifacts(
    sessionId: string,
    tenantId: string,
  ): Promise<import('../types/canonical').CanonicalArtifact[]> {
    const { data } = await this.client.get(
      `/v1/sessions/${sessionId}/artifacts`,
      { params: { tenant_id: tenantId } },
    );
    return data;
  }

  async listSessionWorkflows(
    sessionId: string,
    tenantId: string,
  ) {
    const { data } = await this.client.get(
      `/v1/sessions/${sessionId}/workflows`,
      { params: { tenant_id: tenantId } },
    );
    return data;
  }

  async getWorkflowTimeline(workflowId: string): Promise<import('../types/canonical').WorkflowTimeline> {
    const { data } = await this.client.get(`/v1/workflows/${workflowId}/timeline`);
    return data;
  }

  // ── Artifact Status (Phase 1.4) ─────────────────────────
  // Artifact status is the official completion signal for canonical processing.

  async getArtifactStatus(artifactId: string): Promise<import('../types/canonical').ArtifactCompletionStatus> {
    const { data } = await this.client.get(`/v1/artifacts/${artifactId}/status`);
    return data;
  }

  // ── Sessions (Module 1 & 2) ─────────────────────────────
  async listSessions(tenantId: string) {
    const { data } = await this.client.get(`/v1/sessions?tenant_id=${tenantId}`);
    return data;
  }

  async createSession(tenantId: string, title: string, sessionType = 'knowledge_transfer') {
    const { data } = await this.client.post('/v1/sessions', {
      tenant_id: tenantId,
      title,
      session_type: sessionType,
    });
    return data;
  }

  async getSession(sessionId: string) {
    const { data } = await this.client.get(`/v1/sessions/${sessionId}`);
    return data;
  }

  async getSessionEvents(sessionId: string) {
    const { data } = await this.client.get(`/v1/sessions/${sessionId}/events`);
    return data;
  }

  async getSessionTranscript(sessionId: string) {
    const { data } = await this.client.get(`/v1/sessions/${sessionId}/transcript`);
    return data;
  }

  // ── SME Profiles (Module 3) ─────────────────────────────
  async listSMEProfiles(tenantId: string) {
    const { data } = await this.client.get(`/v1/sme/profiles?tenant_id=${tenantId}`);
    return data;
  }

  async getSMEProfile(speakerId: string) {
    const { data } = await this.client.get(`/v1/sme/profiles/${speakerId}`);
    return data;
  }

  // ── Contradictions (Module 5) ───────────────────────────
  async listContradictions(tenantId: string) {
    const { data } = await this.client.get(`/v1/contradictions?tenant_id=${tenantId}`);
    return data;
  }

  async resolveContradiction(contradictionId: string, resolution: string) {
    const { data } = await this.client.post(`/v1/contradictions/${contradictionId}/resolve`, { resolution });
    return data;
  }

  // ── AI Confidence / Guardrails (Module 6) ───────────────
  async getGuardrailPipeline(tenantId: string) {
    const { data } = await this.client.get(`/v1/guardrails/pipeline?tenant_id=${tenantId}`);
    return data;
  }

  async getReviewQueue(tenantId: string) {
    const { data } = await this.client.get(`/v1/guardrails/review-queue?tenant_id=${tenantId}`);
    return data;
  }

  async getTrustTrend(tenantId: string) {
    const { data } = await this.client.get(`/v1/guardrails/trust-trend?tenant_id=${tenantId}`);
    return data;
  }

  // ── Traceability (Module 7) ─────────────────────────────
  async listTraces(tenantId: string) {
    const { data } = await this.client.get(`/v1/traceability?tenant_id=${tenantId}`);
    return data;
  }

  // ── Test Execution (Module 8) ───────────────────────────
  async listTestSuites(tenantId: string) {
    const { data } = await this.client.get(`/v1/tests/suites?tenant_id=${tenantId}`);
    return data;
  }

  async listTestRuns(tenantId: string) {
    const { data } = await this.client.get(`/v1/tests/runs?tenant_id=${tenantId}`);
    return data;
  }

  // ── Data Forge (Module 9) ──────────────────────────────
  async listDataForgeConfigs(tenantId: string) {
    const { data } = await this.client.get(`/v1/data-forge/configs?tenant_id=${tenantId}`);
    return data;
  }

  async listDataForgeResults(tenantId: string) {
    const { data } = await this.client.get(`/v1/data-forge/results?tenant_id=${tenantId}`);
    return data;
  }

  // ── Compliance (Module 10) ─────────────────────────────
  async listComplianceJurisdictions(tenantId: string) {
    const { data } = await this.client.get(`/v1/compliance/jurisdictions?tenant_id=${tenantId}`);
    return data;
  }

  // ── Executive Insights (Module 11) ─────────────────────
  async getExecutiveKPIs(tenantId: string) {
    const { data } = await this.client.get(`/v1/insights/kpis?tenant_id=${tenantId}`);
    return data;
  }

  async getExecutiveROI(tenantId: string) {
    const { data } = await this.client.get(`/v1/insights/roi?tenant_id=${tenantId}`);
    return data;
  }

  async getExecutiveRisks(tenantId: string) {
    const { data } = await this.client.get(`/v1/insights/risks?tenant_id=${tenantId}`);
    return data;
  }

  async getWeeklyTrend(tenantId: string) {
    const { data } = await this.client.get(`/v1/insights/weekly-trend?tenant_id=${tenantId}`);
    return data;
  }

  async getEngineStatus() {
    const { data } = await this.client.get('/v1/insights/engines');
    return data;
  }

  // ── Admin (Module 12) ──────────────────────────────────
  async getAdminEngines() {
    const { data } = await this.client.get('/v1/admin/engines');
    return data;
  }

  async getAdminResources() {
    const { data } = await this.client.get('/v1/admin/resources');
    return data;
  }

  async getAdminIntegrations(tenantId: string) {
    const { data } = await this.client.get(`/v1/admin/integrations?tenant_id=${tenantId}`);
    return data;
  }

  async getAuditLog(tenantId: string) {
    const { data } = await this.client.get(`/v1/admin/audit?tenant_id=${tenantId}`);
    return data;
  }

  async getAdminUsers(tenantId: string) {
    const { data } = await this.client.get(`/v1/admin/users?tenant_id=${tenantId}`);
    return data;
  }

  // ── QI Portal: Personas ─────────────────────────────────
  async listPersonas(includeInactive = false) {
    const params = includeInactive ? '?include_inactive=true' : '';
    const { data } = await this.client.get(`/v1/personas${params}`);
    return data;
  }

  async getPersona(personaId: string) {
    const { data } = await this.client.get(`/v1/personas/${personaId}`);
    return data;
  }

  async createPersona(body: {
    name: string;
    slug: string;
    description?: string;
    avatar_icon?: string;
    system_prompt?: string;
    capabilities?: string[];
    stage_config?: Record<string, { engines: string[]; auto_advance: boolean }>;
    specialty_domains?: string[];
    metadata_json?: Record<string, unknown>;
  }) {
    const { data } = await this.client.post('/v1/personas', body);
    return data;
  }

  async updatePersona(
    personaId: string,
    body: {
      name?: string;
      description?: string;
      avatar_icon?: string;
      system_prompt?: string;
      capabilities?: string[];
      stage_config?: Record<string, { engines: string[]; auto_advance: boolean }>;
      specialty_domains?: string[];
      is_active?: boolean;
    },
  ) {
    const { data } = await this.client.put(`/v1/personas/${personaId}`, body);
    return data;
  }

  async deletePersona(personaId: string) {
    const { data } = await this.client.delete(`/v1/personas/${personaId}`);
    return data;
  }

  async generatePersonaDraft(artifactId: string, sessionId?: string, forceRegenerate = false): Promise<import('../types/canonical').PersonaDraftResponse> {
    const { data } = await this.client.post('/v1/personas/generate-draft', {
      artifact_id: artifactId,
      session_id: sessionId,
      force_regenerate: forceRegenerate,
    }, { timeout: 900_000 });
    return data;
  }

  // ── Test Architect: Test Strategy ───────────────────────
  async generateTestStrategy(artifactId: string, sessionId?: string, forceRegenerate = false): Promise<import('../types/canonical').TestStrategyResponse> {
    const { data } = await this.client.post('/v1/test-strategy/generate', {
      artifact_id: artifactId,
      session_id: sessionId,
      force_regenerate: forceRegenerate,
    }, { timeout: 900_000 });
    return data;
  }

  // ── E2E Test Architect ─────────────────────────────────
  async generateE2EArchitect(artifactId: string, sessionId?: string, forceRegenerate = false): Promise<import('../types/canonical').E2EArchitectResponse> {
    const { data } = await this.client.post('/v1/e2e-architect/generate', {
      artifact_id: artifactId,
      session_id: sessionId,
      force_regenerate: forceRegenerate,
    }, { timeout: 900_000 });
    return data;
  }

  // ── QI Portal: Missions ─────────────────────────────────
  async listMissions(params?: {
    status?: string;
    persona_id?: string;
    limit?: number;
    offset?: number;
  }) {
    const query = new URLSearchParams();
    if (params?.status) query.set('status', params.status);
    if (params?.persona_id) query.set('persona_id', params.persona_id);
    if (params?.limit) query.set('limit', String(params.limit));
    if (params?.offset) query.set('offset', String(params.offset));
    const qs = query.toString();
    const { data } = await this.client.get(`/v1/missions${qs ? `?${qs}` : ''}`);
    return data;
  }

  async getMission(missionId: string) {
    const { data } = await this.client.get(`/v1/missions/${missionId}`);
    return data;
  }

  async getMissionDashboard() {
    const { data } = await this.client.get('/v1/missions/dashboard');
    return data;
  }

  async createMission(body: {
    title: string;
    description?: string;
    objective?: string;
    persona_id: string;
    priority?: string;
    tags?: string[];
    artifact_id?: string;
    session_id?: string;
  }) {
    const { data } = await this.client.post('/v1/missions', body);
    return data;
  }

  async updateMission(
    missionId: string,
    body: {
      title?: string;
      description?: string;
      objective?: string;
      priority?: string;
      tags?: string[];
      status?: string;
    },
  ) {
    const { data } = await this.client.put(`/v1/missions/${missionId}`, body);
    return data;
  }

  async deleteMission(missionId: string) {
    const { data } = await this.client.delete(`/v1/missions/${missionId}`);
    return data;
  }

  // ── QI Portal: Stages ───────────────────────────────────
  async listMissionStages(missionId: string) {
    const { data } = await this.client.get(`/v1/missions/${missionId}/stages`);
    return data;
  }

  async getMissionStage(missionId: string, stageNumber: number) {
    const { data } = await this.client.get(`/v1/missions/${missionId}/stages/${stageNumber}`);
    return data;
  }

  async startStage(missionId: string, stageNumber: number, inputs?: Record<string, unknown>) {
    const { data } = await this.client.post(
      `/v1/missions/${missionId}/stages/${stageNumber}/start`,
      inputs ? { inputs } : {},
    );
    return data;
  }

  async completeStage(
    missionId: string,
    stageNumber: number,
    outputs?: Record<string, unknown>,
  ) {
    const { data } = await this.client.post(
      `/v1/missions/${missionId}/stages/${stageNumber}/complete`,
      outputs ? { outputs } : {},
    );
    return data;
  }

  async advanceMission(
    missionId: string,
    body?: { skip_current?: boolean; stage_inputs?: Record<string, unknown> },
  ) {
    const { data } = await this.client.post(`/v1/missions/${missionId}/advance`, body ?? {});
    return data;
  }

  // ── QI Portal: Artifacts ────────────────────────────────
  async listMissionArtifacts(missionId: string) {
    const { data } = await this.client.get(`/v1/missions/${missionId}/artifacts`);
    return data;
  }

  async getMissionArtifact(missionId: string, artifactId: string) {
    const { data } = await this.client.get(`/v1/missions/${missionId}/artifacts/${artifactId}`);
    return data;
  }

  async addMissionArtifact(
    missionId: string,
    body: {
      artifact_type: string;
      name: string;
      description?: string;
      content_json?: Record<string, unknown>;
      content_text?: string;
      item_count?: number;
    },
  ) {
    const { data } = await this.client.post(`/v1/missions/${missionId}/artifacts`, body);
    return data;
  }

  // ── QI Portal: Messages ─────────────────────────────────
  async listMissionMessages(missionId: string) {
    const { data } = await this.client.get(`/v1/missions/${missionId}/messages`);
    return data;
  }

  async sendMissionMessage(
    missionId: string,
    body: {
      content: string;
      content_type?: string;
      action_data?: Record<string, unknown>;
    },
  ) {
    const { data } = await this.client.post(`/v1/missions/${missionId}/messages`, body);
    return data;
  }

  // ── Brain Engine (Intelligent Coordinator) ──────────────
  /**
   * Ask the Brain to make an intelligent cross-engine decision.
   * Types: route | quality_gate | confidence | merge | summarize
   */
  async brainDecide(body: {
    tenant_id: string;
    session_id: string;
    decision_type: string;
    engine_results?: Record<string, unknown>;
    rules?: Record<string, unknown>[];
    test_cases?: Record<string, unknown>[];
    confidence_scores?: Record<string, number>;
    user_query?: string;
    constraints?: Record<string, unknown>;
  }) {
    const { data } = await this.client.post('/v1/brain/decide', body);
    return data;
  }

  /** Evaluate the quality of a QA session's outputs via Brain quality gate. */
  async brainQualityGate(body: {
    tenant_id: string;
    session_id: string;
    rules?: Record<string, unknown>[];
    test_cases?: Record<string, unknown>[];
    engine_results?: Record<string, unknown>;
    confidence_scores?: Record<string, number>;
    pii_result?: Record<string, unknown>;
  }) {
    const { data } = await this.client.post('/v1/brain/quality-gate', body);
    return data;
  }

  /** Update Brain session state with engine results. */
  async brainSessionUpdate(
    sessionId: string,
    body: { tenant_id: string; session_id: string; engine_name: string; result: Record<string, unknown> },
  ) {
    const { data } = await this.client.post(`/v1/brain/sessions/${sessionId}/update`, body);
    return data;
  }

  /** Get session gap analysis and recommended next engines. */
  async brainSessionAnalyze(sessionId: string) {
    const { data } = await this.client.get(`/v1/brain/sessions/${sessionId}/analyze`);
    return data;
  }

  /** List all tracked QA sessions. */
  async brainListSessions() {
    const { data } = await this.client.get('/v1/brain/sessions');
    return data;
  }

  /** Get multi-tier provider status across all 11 engines. */
  async brainGetTiers() {
    const { data } = await this.client.get('/v1/brain/tiers');
    return data;
  }

  /** Get tier configuration for a specific engine. */
  async brainGetEngineTiers(engineName: string) {
    const { data } = await this.client.get(`/v1/brain/tiers/${engineName}`);
    return data;
  }

  /** Ask the Brain a free-form question about QA processes. */
  async brainAsk(body: { tenant_id: string; question: string; session_id?: string; context?: string }) {
    const { data } = await this.client.post('/v1/brain/ask', body);
    return data;
  }

  /** Get Brain LLM provider health status. */
  async brainLLMHealth() {
    const { data } = await this.client.get('/v1/brain/llm-health');
    return data;
  }

  async cancelSession(sessionId: string) {
    // Cancel session record in Platform API
    const { data } = await this.client.post(`/v1/sessions/${sessionId}/cancel`);
    return data;
  }

  async cancelWorkflow(workflowId: string) {
    // Cancel the running workflow in the orchestrator
    const { data } = await this.client.post(`/v1/orchestrator/workflows/${workflowId}/cancel`);
    return data;
  }

  async deleteSession(sessionId: string) {
    const { data } = await this.client.delete(`/v1/sessions/${sessionId}`);
    return data;
  }
}

export const api = new ApiClient();
export default api;
