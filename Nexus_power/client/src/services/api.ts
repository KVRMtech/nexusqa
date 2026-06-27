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
      const token = sessionStorage.getItem('nexus_token');
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
          sessionStorage.removeItem('nexus_token');
          sessionStorage.removeItem('nexus_user');
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
    /**
     * RFC-style idempotency key. Pass a fresh UUID per logical
     * submission. If the network retries inside the HTTP client (or
     * the user double-clicks), the gateway returns the original
     * workflow_id instead of creating a duplicate workflow.
     */
    idempotencyKey?: string;
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

    const headers: Record<string, string> = {
      'Content-Type': 'multipart/form-data',
    };
    if (params.idempotencyKey) {
      headers['Idempotency-Key'] = params.idempotencyKey;
    }

    const { data } = await this.client.post('/v1/orchestrator/process', form, {
      headers,
      timeout: 300_000, // 5 min — files can be large
    });
    return data;
  }

  /**
   * Get the SSE stream path for a workflow's progress.
   * Use with the useSSE hook: useSSE({ path: api.getWorkflowStreamPath(id) })
   *
   * Defaults to the Phase 12 canonical-workflow plane stream — that's
   * where `/v1/orchestrator/process` now dispatches every upload.
   * Pass `kind: 'legacy'` only for workflows started through the
   * old `chain_engine.start` path (e.g. consumer chains).
   */
  getWorkflowStreamPath(workflowId: string, kind: 'legacy' | 'canonical' = 'canonical'): string {
    if (kind === 'legacy') {
      return `/v1/orchestrator/workflows/${workflowId}/stream`;
    }
    return `/v1/canonical-workflows/${workflowId}/stream`;
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

  /**
   * Phase D.1 — fetch triangulated user-action steps for an artifact.
   * Returns one row per detected user-action (click, type, select, etc.)
   * sourced from the evidence_steps table.  Each step carries
   * confidence + agreement_score + evidence_signals so the UI can render
   * a confidence-aware action timeline without fabricating labels.
   */
  async listArtifactEvidenceSteps(
    artifactId: string,
    options?: { sceneId?: string; minConfidence?: number },
  ): Promise<{
    artifact_id: string;
    total: number;
    by_action_kind: Record<string, number>;
    steps: import('../types/canonical').EvidenceStep[];
  }> {
    const params: Record<string, string | number> = {};
    if (options?.sceneId) params.scene_id = options.sceneId;
    if (options?.minConfidence != null) params.min_confidence = options.minConfidence;
    const { data } = await this.client.get(
      `/v1/artifacts/${artifactId}/evidence-steps`,
      { params },
    );
    return data;
  }

  /**
   * Fetch all visual_frames for an artifact (paginated).  Used to build
   * the frame_id → asset_path lookup the EvidenceStepsPanel needs to
   * render before/after thumbnails for every step.  Pages are fetched
   * automatically until the response is empty so the caller does not
   * have to manage offsets manually.
   */
  async listArtifactFramesAll(
    artifactId: string,
    pageSize: number = 200,
  ): Promise<Array<{
    frame_id: string;
    frame_index: number;
    frame_asset_path: string;
    timestamp_seconds: number;
    is_keyframe: boolean;
    // Phase 1.7 — extended fields the backend already returns via
    // row_to_dict.  Optional so older callers stay compatible.
    extracted_text?: string;
    url_or_path?: string;
    ui_elements_json?: Array<Record<string, unknown>>;
    scene_id?: string | null;
    ocr_confidence?: number;
  }>> {
    const out: Array<{
      frame_id: string;
      frame_index: number;
      frame_asset_path: string;
      timestamp_seconds: number;
      is_keyframe: boolean;
      extracted_text?: string;
      url_or_path?: string;
      ui_elements_json?: Array<Record<string, unknown>>;
      scene_id?: string | null;
      ocr_confidence?: number;
    }> = [];
    let page = 1;
    while (true) {
      const { data } = await this.client.get(
        `/v1/artifacts/${artifactId}/frames`,
        { params: { page, limit: pageSize } },
      );
      const frames = (data?.frames ?? []) as typeof out;
      if (frames.length === 0) break;
      out.push(...frames);
      if (frames.length < pageSize) break;
      page += 1;
      if (page > 50) break;  // hard cap — 50 × 200 = 10k frames
    }
    return out;
  }

  /**
   * Phase D.2 — fetch per-frame cursor positions and click annotations
   * for the step-replay overlay.
   */
  async listArtifactCursorEvents(
    artifactId: string,
    options?: { frameId?: string; isClick?: boolean },
  ): Promise<{
    artifact_id: string;
    total: number;
    click_count: number;
    events: import('../types/canonical').CursorEvent[];
  }> {
    const params: Record<string, string | boolean> = {};
    if (options?.frameId) params.frame_id = options.frameId;
    if (options?.isClick != null) params.is_click = options.isClick;
    const { data } = await this.client.get(
      `/v1/artifacts/${artifactId}/cursor-events`,
      { params },
    );
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
    // Bumped from default 30s to 60s. Defensive: under sustained load
    // (or a stuttering Docker Desktop bridge), the platform-api/postgres
    // chain can take >30s to ack a session INSERT, even though the
    // payload is small. A 30s ceiling here was making the upload flow
    // fail BEFORE the actual upload POST ran — confusing users with
    // "timeout of 30000ms exceeded" when the real cause was upstream
    // congestion. 60s gives the system room to breathe.
    const { data } = await this.client.post('/v1/sessions', {
      tenant_id: tenantId,
      title,
      session_type: sessionType,
    }, { timeout: 60_000 });
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
  async listSMEProfiles() {
    const { data } = await this.client.get('/v1/sme/profiles');
    return data;
  }

  async getSMEProfile(speakerId: string) {
    const { data } = await this.client.get(`/v1/sme/profiles/${speakerId}`);
    return data;
  }

  // ── Contradictions (Module 5) ───────────────────────────
  async listContradictions() {
    const { data } = await this.client.get('/v1/contradictions');
    return data;
  }

  async resolveContradiction(contradictionId: string, resolution: string) {
    const { data } = await this.client.post(`/v1/contradictions/${contradictionId}/resolve`, { resolution });
    return data;
  }

  // ── AI Confidence / Guardrails (Module 6) ───────────────
  async getGuardrailPipeline() {
    const { data } = await this.client.get('/v1/guardrails/pipeline');
    return data;
  }

  async getReviewQueue() {
    const { data } = await this.client.get('/v1/guardrails/review-queue');
    return data;
  }

  async getTrustTrend() {
    const { data } = await this.client.get('/v1/guardrails/trust-trend');
    return data;
  }

  // ── Traceability (Module 7) ─────────────────────────────
  async listTraces() {
    const { data } = await this.client.get('/v1/traceability');
    return data;
  }

  // ── Test Execution (Module 8) ───────────────────────────
  async listTestSuites() {
    const { data } = await this.client.get('/v1/tests/suites');
    return data;
  }

  async listTestRuns() {
    const { data } = await this.client.get('/v1/tests/runs');
    return data;
  }

  // ── Data Forge (Module 9) ──────────────────────────────
  async listDataForgeConfigs() {
    const { data } = await this.client.get('/v1/data-forge/configs');
    return data;
  }

  async listDataForgeResults() {
    const { data } = await this.client.get('/v1/data-forge/results');
    return data;
  }

  // ── Compliance (Module 10) ─────────────────────────────
  async listComplianceJurisdictions() {
    const { data } = await this.client.get('/v1/compliance/jurisdictions');
    return data;
  }

  // ── Executive Insights (Module 11) ─────────────────────
  async getExecutiveKPIs() {
    const { data } = await this.client.get('/v1/insights/kpis');
    return data;
  }

  async getExecutiveROI() {
    const { data } = await this.client.get('/v1/insights/roi');
    return data;
  }

  async getExecutiveRisks() {
    const { data } = await this.client.get('/v1/insights/risks');
    return data;
  }

  async getWeeklyTrend() {
    const { data } = await this.client.get('/v1/insights/weekly-trend');
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

  async getAdminIntegrations() {
    const { data } = await this.client.get('/v1/admin/integrations');
    return data;
  }

  async getAuditLog() {
    const { data } = await this.client.get('/v1/admin/audit');
    return data;
  }

  async getAdminUsers() {
    const { data } = await this.client.get('/v1/admin/users');
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
  async generateE2EArchitect(artifactId: string, sessionId?: string, forceRegenerate = false, evidenceMode?: string): Promise<import('../types/canonical').E2EArchitectResponse> {
    const { data } = await this.client.post('/v1/e2e-architect/generate', {
      artifact_id: artifactId,
      session_id: sessionId,
      force_regenerate: forceRegenerate,
      ...(evidenceMode ? { evidence_mode: evidenceMode } : {}),
    }, { timeout: 900_000 });
    return data;
  }

  // ── E2E Scenario Lifecycle (P3) ────────────────────────
  /** Fetch the map of scenario_id → state row for an artifact.
   *  Scenarios without a row are implicitly in 'draft' state. */
  async getScenarioStates(
    artifactId: string,
  ): Promise<{ artifact_id: string; states: Record<string, import('../types/canonical').ScenarioStateResponse> }> {
    const { data } = await this.client.get(
      `/v1/e2e-architect/${artifactId}/scenarios/state`,
    );
    return data;
  }

  /** Transition a scenario to a new lifecycle state.  Returns the updated row.
   *  Throws axios error with 422 detail if transition is illegal. */
  async transitionScenario(
    artifactId: string,
    scenarioId: string,
    newState: import('../types/canonical').ScenarioLifecycleState,
    note: string = '',
  ): Promise<{ success: boolean; state: import('../types/canonical').ScenarioStateResponse; allowed_next_states: string[] }> {
    const { data } = await this.client.post(
      `/v1/e2e-architect/${artifactId}/scenarios/${scenarioId}/transition`,
      { new_state: newState, note },
    );
    return data;
  }

  /** Append a comment to a scenario's thread. */
  async commentOnScenario(
    artifactId: string,
    scenarioId: string,
    body: string,
  ): Promise<{ success: boolean; state: import('../types/canonical').ScenarioStateResponse }> {
    const { data } = await this.client.post(
      `/v1/e2e-architect/${artifactId}/scenarios/${scenarioId}/comment`,
      { body },
    );
    return data;
  }

  // ── Co-Architect Chat (P4) ─────────────────────────────
  /** One turn of conversation with the Co-Architect agent.
   *  The full conversation history MUST be sent every turn — the
   *  backend is stateless on purpose. */
  async coArchitectChat(
    artifactId: string,
    messages: Array<import('../types/canonical').CoArchitectMessage>,
    options?: { proposeScenarios?: boolean; sessionId?: string },
  ): Promise<import('../types/canonical').CoArchitectChatResponse> {
    const { data } = await this.client.post(
      `/v1/e2e-architect/${artifactId}/co-architect/chat`,
      {
        messages,
        propose_scenarios: options?.proposeScenarios ?? false,
        session_id: options?.sessionId ?? '',
      },
      { timeout: 180_000 },
    );
    return data;
  }

  /** Commit agent-proposed scenarios into the artifact's e2e_architect_cache.
   *  The backend re-validates each step against the live visual graph and
   *  drops any that can't be re-grounded. */
  async commitCoArchitectProposals(
    artifactId: string,
    proposedScenarios: Array<import('../types/canonical').CoArchitectProposedScenario>,
  ): Promise<import('../types/canonical').CoArchitectCommitResponse> {
    const { data } = await this.client.post(
      `/v1/e2e-architect/${artifactId}/co-architect/commit`,
      { proposed_scenarios: proposedScenarios },
    );
    return data;
  }

  /** Inline-edit a stored test-case scenario's data fields (action /
   *  input value / expected result). Grounding is immutable server-side. */
  async editScenario(
    artifactId: string,
    scenarioId: string,
    patch: {
      title?: string;
      steps?: Array<{ step_index: number; action?: string; input_data?: string; expected_output?: string }>;
    },
  ): Promise<{ success: boolean; scenario_id: string; step_count: number; human_edited: boolean; regenerate_playwright: boolean }> {
    const { data } = await this.client.patch(
      `/v1/e2e-architect/${artifactId}/scenarios/${scenarioId}`,
      patch,
    );
    return data;
  }

  /** Inline-edit a Test Factory (Pages & Forms) case's step data. Stored as
   *  an override that survives regeneration; Playwright recompiles from it. */
  async editTestCase(
    artifactId: string,
    caseId: string,
    patch: { title?: string; steps?: Array<{ step_number: number; action?: string; expected_result?: string; verification?: string; control?: { label: string; kind?: string; anchor?: string; anchor_kind?: string } }> },
  ): Promise<{ success: boolean; test_case_id: string; overridden_steps: number; repointed_steps?: number; survives_regenerate: boolean }> {
    const { data } = await this.client.patch(
      `/v1/test-factory/${artifactId}/test-cases/${caseId}`,
      patch,
    );
    return data;
  }

  /** The grounded menu of controls captured in this recording — the ONLY controls
   *  a step may be re-pointed to (the backend rejects anything not captured). */
  async getStepControls(artifactId: string, caseId: string): Promise<{ controls: Array<{ label: string; kind: string; page?: string }> }> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/test-cases/${caseId}/controls`);
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

  // ── Visual Evidence (Phase 1) ────────────────────────────

  /**
   * Build the absolute HTTP URL for a frame image given its asset path.
   *
   * The Eyes engine serialises `frame_asset_path` as a storage-root-relative
   * path, e.g. `"<tenant_id>/<session_id>/<job_id>_frames/frame_00001.png"`.
   * The gateway forwards `/api/v1/eyes/frames/...` to the Eyes engine which
   * enforces JWT tenant isolation and path traversal guards before serving
   * the file.
   *
   * The auth token is appended as a query param so <img> tags load without
   * a custom fetch — the Eyes engine reads it via the standard Bearer
   * authorisation header forwarded by the gateway.
   *
   * @param frameAssetPath  relative asset path from FrameAnalysis.frame_asset_path
   * @returns               fully-qualified URL safe to use as <img src>
   */
  getFrameImageUrl(frameAssetPath: string): string {
    if (!frameAssetPath) return '';
    // Normalise separator: Windows paths from the engine use backslash
    const normalised = frameAssetPath.replace(/\\/g, '/');
    const token = sessionStorage.getItem('nexus_token') ?? '';
    const base = API_BASE.replace(/\/$/, '');
    return `${base}/v1/eyes/frames/${normalised}${token ? `?token=${encodeURIComponent(token)}` : ''}`;
  }

  /**
   * Resolve a run step's `screenshot_url` (the 'This run' actual) into a value
   * safe for an <img src>. The reporter stores a same-origin relative path
   * (/api/v1/test-runs/screenshot/{id}); we append the auth token as a query
   * param (the server's auth middleware accepts ?token=) so the <img> loads
   * without a custom fetch. An absolute http(s) URL (external CI-hosted shot)
   * is returned unchanged.
   */
  getRunScreenshotUrl(urlOrPath: string): string {
    if (!urlOrPath) return '';
    if (/^https?:\/\//i.test(urlOrPath)) return urlOrPath;
    const token = sessionStorage.getItem('nexus_token') ?? '';
    if (!token) return urlOrPath;
    const sep = urlOrPath.includes('?') ? '&' : '?';
    return `${urlOrPath}${sep}token=${encodeURIComponent(token)}`;
  }

  // ── Visual Evidence — Phase 3 (Scenes) ─────────────────

  /** Fetch all visual scenes for an artifact, optionally filtered by app instance. */
  async getArtifactVisualScenes(
    artifactId: string,
    instanceId?: string,
  ): Promise<import('../types/canonical').VisualScene[]> {
    return this.getVisualScenes(artifactId, instanceId);
  }

  async getVisualScenes(
    artifactId: string,
    instanceId?: string,
  ): Promise<import('../types/canonical').VisualScene[]> {
    const params = instanceId ? `?instance_id=${instanceId}` : '';
    const { data } = await this.client.get(`/v1/artifacts/${artifactId}/visual-scenes${params}`);
    return data.scenes ?? [];
  }

  // ── Visual Evidence — Phase 5 (App Instances) ──────────

  async getAppInstances(
    artifactId: string,
  ): Promise<import('../types/canonical').AppInstance[]> {
    const { data } = await this.client.get(`/v1/artifacts/${artifactId}/app-instances`);
    return data.instances ?? [];
  }

  // ── Visual Evidence — Phase 7 (Flow Graph) ─────────────

  async getVisualEvidenceGraph(
    artifactId: string,
    options?: { includeStoryboardOverlays?: boolean },
  ): Promise<import('../types/canonical').VisualEvidenceGraph> {
    const params: Record<string, string> = {};
    if (options?.includeStoryboardOverlays) {
      params.include_storyboard_overlays = 'true';
    }
    const { data } = await this.client.get(
      `/v1/artifacts/${artifactId}/visual-evidence-graph`,
      { params },
    );
    return data;
  }

  // ── Storyboard Phase 1 — picture-first visual evidence ──────────
  // Triggers lazy derivation on first call.  Returns the composed
  // storyboard (panels + apps + summary + derivation status).
  // See platform/api/app/routers/storyboard.py for the payload shape.

  async getStoryboard(
    artifactId: string,
  ): Promise<import('../types/canonical').StoryboardPayload> {
    // The first call for an artifact (or any call while a version is
    // stale) triggers the full composer derivation — scene grouping,
    // captions, action + page + form-snapshot extraction — which runs
    // the multimodal LLM and can take 1-3 minutes.  The default 30s
    // client timeout surfaced as "Storyboard not ready / timeout of
    // 30000ms exceeded" whenever the user opened the tab mid-derivation.
    // Give it a generous budget; subsequent cache-hit reads return in
    // a few seconds.
    const { data } = await this.client.get(
      `/v1/artifacts/${artifactId}/storyboard`,
      { timeout: 300_000 },
    );
    return data;
  }

  async regenerateStoryboard(
    artifactId: string,
  ): Promise<import('../types/canonical').StoryboardPayload> {
    const { data } = await this.client.post(
      `/v1/artifacts/${artifactId}/storyboard/regenerate`,
    );
    return data;
  }

  // ── Surface toggles (cost control: only derive surfaces you want to see) ──
  async getArtifactSurfaces(artifactId: string): Promise<{
    surfaces: { storyboard: boolean; pages_forms: boolean; three_d_journey: boolean };
    source: string; tenant_default: any; has_artifact_override: boolean;
  }> {
    const { data } = await this.client.get(`/v1/artifacts/${artifactId}/surfaces`);
    return data;
  }

  async setArtifactSurfaces(
    artifactId: string,
    surfaces: { storyboard: boolean; pages_forms: boolean; three_d_journey: boolean },
  ): Promise<{ surfaces: any; deriving: boolean; newly_enabled: string[] }> {
    const { data } = await this.client.put(`/v1/artifacts/${artifactId}/surfaces`, surfaces);
    return data;
  }

  async getTenantSurfaces(): Promise<{ surfaces: any; is_set: boolean }> {
    const { data } = await this.client.get(`/v1/tenant/surfaces`);
    return data;
  }

  async setTenantSurfaces(
    surfaces: { storyboard: boolean; pages_forms: boolean; three_d_journey: boolean },
  ): Promise<{ surfaces: any }> {
    const { data } = await this.client.put(`/v1/tenant/surfaces`, surfaces);
    return data;
  }

  /**
   * Build an annotated-frame URL the <img> tag can src= directly.
   * The endpoint is auth-gated (JWT in header), so this is only
   * usable inside the SPA where the axios interceptor attaches the
   * token; to use it from a plain <img>, append the token as a
   * query param (matching the eyes-engine pattern).
   */
  getAnnotatedFrameUrl(artifactId: string, frameId: string): string {
    const token = sessionStorage.getItem('nexus_token') || '';
    const base = (API_BASE || '/api').replace(/\/$/, '');
    const path = `${base}/v1/artifacts/${encodeURIComponent(artifactId)}/frames/${encodeURIComponent(frameId)}/annotated.png`;
    return token ? `${path}?token=${encodeURIComponent(token)}` : path;
  }

  // ── Visual Strict Test Generation — Phase 9 ────────────

  async generateTestsFromVisual(
    body: import('../types/canonical').GenerateTestsFromVisualRequest,
  ): Promise<import('../types/canonical').VisualTestGenerationResponse> {
    const { data } = await this.client.post('/v1/heart/generate-tests-from-visual', body);
    return data;
  }

  // ── Playwright Export — Phase 10 ────────────────────────

  async exportPlaywright(artifactId: string): Promise<Blob> {
    const resp = await this.client.post(
      `/v1/tests/export-playwright`,
      null,
      { params: { artifact_id: artifactId }, responseType: 'blob' },
    );
    return resp.data as Blob;
  }

  /** @deprecated Use exportPlaywright() instead */
  async exportPlaywrightScripts(artifactId: string): Promise<Blob> {
    return this.exportPlaywright(artifactId);
  }

  // ── Multi-format Test Export (P5) ──────────────────────
  /** List all export formats the platform supports. */
  async listExportFormats(): Promise<{
    formats: Array<{ id: string; label: string; file_extension: string }>;
  }> {
    const { data } = await this.client.get('/v1/tests/export-formats');
    return data;
  }

  /** Generic test export — returns a ZIP blob in the chosen format.
   *  Throws on lifecycle-gate / automation-gate failure (axios error
   *  with .response.data.detail describing the unmet gates). */
  async exportTests(
    artifactId: string,
    format: 'playwright' | 'cypress' | 'gherkin' | 'json',
    options?: { enforceGates?: boolean },
  ): Promise<Blob> {
    const resp = await this.client.post(
      `/v1/tests/export`,
      null,
      {
        params: {
          artifact_id: artifactId,
          format,
          enforce_gates: options?.enforceGates !== false,
        },
        responseType: 'blob',
      },
    );
    return resp.data as Blob;
  }

  // ── Execution Feedback Loop (P6) ───────────────────────
  /** Ingest a CI test run + per-step details.  Authenticated with the
   *  caller's JWT; CI uses a long-lived service token. */
  async ingestTestRun(payload: {
    artifact_id: string;
    ci_run_id?: string;
    ci_commit_sha?: string;
    ci_pipeline_url?: string;
    environment?: string;
    status?: string;
    started_at?: string;
    completed_at?: string;
    steps: Array<{
      test_name?: string;
      scenario_id?: string;
      step_number?: number;
      status?: string;
      duration_ms?: number;
      expected_selector?: string;
      resolved_selector?: string;
      resolved_bbox?: Record<string, number>;
      evidence_scene_id?: string;
      evidence_control_id?: string;
      evidence_edge_id?: string;
      error_message?: string;
      screenshot_url?: string;
      expected_output?: string;
      metadata?: Record<string, unknown>;
    }>;
    metadata?: Record<string, unknown>;
  }): Promise<{
    run_id: string;
    status: string;
    total_steps: number;
    passed_steps: number;
    failed_steps: number;
    unparsed_scenarios: number;
  }> {
    const { data } = await this.client.post('/v1/test-runs/ingest', payload);
    return data;
  }

  /** List recent E2E test runs for an artifact, newest first. (Distinct
   *  from the legacy ``listTestRuns()`` which targets ``/v1/tests/runs``
   *  for the old test suite system.) */
  async listE2ETestRuns(
    artifactId: string,
    limit: number = 20,
  ): Promise<{ artifact_id: string; runs: import('../types/canonical').TestRunDetail[] }> {
    const { data } = await this.client.get(
      `/v1/e2e-architect/${artifactId}/test-runs`,
      { params: { limit } },
    );
    return data;
  }

  // ── Test Factory (Pages & Forms → grounded test cases) ──────────────
  /** Generate demonstrated + combination test cases from Pages & Forms. */
  async generateTestFactory(artifactId: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/generate`, {},
    );
    return data;
  }

  /** Aggregate counts for the Test Cases summary. */
  async getTestFactorySummary(artifactId: string): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/summary`,
    );
    return data;
  }

  /** Paginated list of generated test cases (full ProductionTestCase each). */
  async listTestFactoryCases(
    artifactId: string, page = 1, limit = 50, status = 'active', type?: string,
  ): Promise<any> {
    const params: Record<string, any> = { page, limit, status };
    if (type) params.type = type;
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/test-cases`, { params },
    );
    return data;
  }

  /** Co-Architect (repointed) — grounded QA-copilot chat over Pages & Forms. */
  async assistantTestFactory(
    artifactId: string, message: string, history: { role: string; content: string }[] = [],
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/assistant`, { message, history },
      { timeout: 120_000 },
    );
    return data;
  }

  /** Draft a NEW grounded test case from a plain-English request (no write). */
  async proposeTestCase(
    artifactId: string, message: string, history: { role: string; content: string }[] = [],
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/propose-case`, { message, history },
      { timeout: 120_000 },
    );
    return data;
  }

  /** Persist a reviewed proposed case (re-validated + grounded server-side). */
  async addTestCase(
    artifactId: string, payload: { name: string; message: string; steps: any[] },
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/add-case`, payload,
      { timeout: 120_000 },
    );
    return data;
  }

  /** Transition a case through its sign-off lifecycle (draft -> in_review -> approved). */
  async reviewTestCase(
    artifactId: string, caseId: string,
    payload: { action: string; signature?: string; note?: string },
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/test-cases/${caseId}/review`, payload,
    );
    return data;
  }

  /** On-demand generate a non-demonstrated category (negative|boundary|error_state). */
  async generateTestFactoryCategory(artifactId: string, category: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/generate/${category}`, {},
      { timeout: 120_000 },
    );
    return data;
  }

  /** Capture available field options (vision) then regenerate combinations. */
  async captureTestFactoryOptions(artifactId: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/capture-options`, {},
      { timeout: 300_000 },
    );
    return data;
  }

  /** Capture per-action "where it sits" anchors (vision) + regenerate. */
  async captureTestFactoryAnchors(artifactId: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/capture-anchors`, {},
      { timeout: 300_000 },
    );
    return data;
  }

  /** Grounded triage board: per-failure verdict + baseline, + need-you tally. */
  async getTriage(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/triage`);
    return data;
  }

  /** Grounded Oracle scorecard: verified-vs-assumed grounding, confidence rollup, heal integrity. */
  async getOracleScorecard(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/oracle-scorecard`);
    return data;
  }

  /** Requirements Traceability Matrix: requirement(recorded)→step→assertion, grounded flags + unproven. */
  async getRtm(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/rtm`);
    return data;
  }

  /** ADVISORY perceptual diff (baseline-vs-actual screenshot) — a badge, never a verdict. */
  async getVisualDiff(artifactId: string, scenarioId: string, stepNumber = 0): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/scenarios/${encodeURIComponent(scenarioId)}/visual-diff`,
      { params: { step_number: stepNumber } });
    return data;
  }

  /** ADVISORY VLM semantic check — does the screen still satisfy the recorded outcome? Signal only. */
  async semanticCheck(artifactId: string, scenarioId: string, stepNumber = 0): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/scenarios/${encodeURIComponent(scenarioId)}/semantic-check`,
      {}, { params: { step_number: stepNumber } });
    return data;
  }

  /** Human CONFIRMS or OVERRIDES the engine's grounded verdict — the direct oracle
   *  label. De-identified server-side (verdict enums only); flywheel capture gated. */
  async triageFeedback(
    artifactId: string, scenarioId: string,
    body: { verdict?: string; agrees: boolean; corrected_verdict?: string },
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/triage/${encodeURIComponent(scenarioId)}/feedback`, body);
    return data;
  }

  /** Human resolves a value-conflict (typed vs committed vs other). De-identified
   *  server-side (choice enum only — never the value); flywheel capture gated. */
  async resolveValueConflict(
    artifactId: string, testId: string, choice: 'typed' | 'committed' | 'other',
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/value-conflict/resolve`, { test_id: testId, choice });
    return data;
  }

  /** THIS RUN: per-scenario, per-step pass/fail timeline for the single latest run. */
  async getLatestRunTimeline(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/runs/latest`);
    return data;
  }

  /** Auth profile status for an artifact (is a session stored + is a capture running). */
  async getAuthStatus(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/playwright/auth`);
    return data;
  }

  /** Start an interactive login capture; returns { live_url } to embed for the operator to log in. */
  async startAuthCapture(artifactId: string, baseUrl = ''): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/playwright/auth/capture`, { base_url: baseUrl },
    );
    return data;
  }

  /** Save the captured session (encrypted server-side). */
  async saveAuthCapture(artifactId: string): Promise<any> {
    const { data } = await this.client.post(`/v1/test-factory/${artifactId}/playwright/auth/save`, {});
    return data;
  }

  /** Cancel an in-progress capture. */
  async cancelAuthCapture(artifactId: string): Promise<any> {
    const { data } = await this.client.post(`/v1/test-factory/${artifactId}/playwright/auth/cancel`, {});
    return data;
  }

  /** Delete the stored auth profile (runs revert to unauthenticated). */
  async clearAuthProfile(artifactId: string): Promise<any> {
    const { data } = await this.client.delete(`/v1/test-factory/${artifactId}/playwright/auth`);
    return data;
  }

  /** Recent run headers (newest first) for the historical run picker. */
  async listRuns(artifactId: string, limit = 25): Promise<{ artifact_id: string; runs: any[] }> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/runs`, { params: { limit } });
    return data;
  }

  /** Per-scenario, per-step timeline for ONE historical run (same shape as runs/latest). */
  async getRunTimeline(artifactId: string, runId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/runs/${encodeURIComponent(runId)}`);
    return data;
  }

  /** Fidelity scorecard for ONE script (deep=1 adds the gpt-4o semantic review). */
  async getScriptFidelity(artifactId: string, testId: string, deep = false): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/scripts/${encodeURIComponent(testId)}/fidelity`,
      { params: deep ? { deep: 1 } : {} },
    );
    return data;
  }

  /** Agentic Playwright audit of ONE script: physical-possibility + grounding,
   *  5-dimension scorecard + per-step verdict. deep=1 adds the LLM reasoning;
   *  never green-wash (ungrounded "fix" → UNPROVEN). */
  async auditScriptAgentic(artifactId: string, testId: string, deep = true): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/scripts/${encodeURIComponent(testId)}/audit`,
      null, { params: deep ? { deep: 1 } : { deep: 0 } },
    );
    return data;
  }

  /** Apply the audit repair: re-derive from the recording with the grounded
   *  generator, recompile, save v+1. Returns before/after + the re-scored audit. */
  async repairScriptFromAudit(artifactId: string, testId: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/scripts/${encodeURIComponent(testId)}/audit/repair`,
    );
    return data;
  }

  /** Live Preflight — deterministic Playwright probe (no LLM): opens the live app
   *  and reports per-step locator resolution (1 good / 0 broken / >1 ambiguous). */
  async runScriptPreflight(artifactId: string, testId: string, baseUrl = ''): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/scripts/${encodeURIComponent(testId)}/preflight`,
      { base_url: baseUrl },
      { timeout: 180_000 },   // launches a real browser via the runner — well past the 30s default
    );
    return data;
  }

  /** Suite-level fidelity rollup (deterministic, all scripts). */
  async getSuiteFidelity(artifactId: string): Promise<any> {
    const { data } = await this.client.get(`/v1/test-factory/${artifactId}/fidelity`);
    return data;
  }

  /** Regenerate ONE script from its current test case → new immutable version. */
  async regenerateScript(artifactId: string, testId: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/scripts/${encodeURIComponent(testId)}/regenerate`, {});
    return data;
  }

  /** Make AI Review actionable: apply ONLY deterministic, grounded fixes
   *  (regenerate to resolve drift/stale oracles). Returns { fixed, escalations,
   *  version_no } — what was fixed vs. honestly left for a human. Never LLM code. */
  async fixScriptGaps(artifactId: string, testId: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/scripts/${encodeURIComponent(testId)}/fix-gaps`, {});
    return data;
  }

  /** Regenerate a group of scripts (test_ids/categories, or all) to new versions. */
  async regenerateAll(artifactId: string, body: { test_ids?: string[]; categories?: string[] }): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/scripts/regenerate-all`, body);
    return data;
  }

  /** Approve a PROPOSED (auto-healed / TrueFix) version → it becomes the active
   *  source for runs. The human gate on machine-written fixes. */
  async approveScriptVersion(artifactId: string, testId: string, versionNo: number): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/scripts/${encodeURIComponent(testId)}/versions/${versionNo}/approve`, {},
    );
    return data;
  }

  /** Nexus TrueFix — grounded root-cause diagnosis of one failed step. */
  async analyzeStep(artifactId: string, scenarioId: string, stepNumber: number): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/steps/${encodeURIComponent(scenarioId)}/${stepNumber}/analyze`,
    );
    return data;
  }

  /** TrueFix — apply the control-kind fix to one step + re-run it HEADED to prove
   *  it. Persists a new version only on a verified green. Returns { run_id, live_url };
   *  poll getNexusRunStatus for { healed, heal_version, heal_reason }. */
  async healStep(
    artifactId: string, scenarioId: string, stepNumber: number,
    body: { base_url?: string; data?: Record<string, string> },
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/steps/${encodeURIComponent(scenarioId)}/${stepNumber}/heal`,
      body,
    );
    return data;
  }

  /** Phase B — re-run the failing test with a11y capture ON so TrueFix can find a
   *  renamed control. Returns { captured, nodes, reanchored, reanchor }. */
  async captureFailureState(
    artifactId: string, scenarioId: string, stepNumber: number,
    body: { base_url?: string; data?: Record<string, string> } = {},
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/steps/${encodeURIComponent(scenarioId)}/${stepNumber}/capture-failure-state`,
      body,
    );
    return data;
  }

  /** Phase D — per-script run-history sparkline + flake + board Run Summary. */
  async getRunsSummary(artifactId: string, window = 10): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/runs/summary`, { params: { window } },
    );
    return data;
  }

  /** Structured listing of the compiled Playwright suite (each script's source +
   *  spec path + category + per-step stats, project files, run commands) for the
   *  Execution view. Same compilation as the zip, returned as JSON. */
  async getPlaywrightManifest(artifactId: string): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/playwright/manifest`,
    );
    return data;
  }

  /** Compile a CONFIGURED, env+data-driven Playwright run bundle (zip) for a
   *  local run — selected scripts + chosen base URL + data overrides baked in. */
  async getRunBundle(
    artifactId: string,
    body: { categories?: string[]; test_ids?: string[]; base_url?: string; data?: Record<string, string>; data_by_test?: Record<string, Record<string, string>>; browsers?: string[]; headed?: boolean; workers?: number; retries?: number },
  ): Promise<Blob> {
    const resp = await this.client.post(
      `/v1/test-factory/${artifactId}/playwright/run-config`,
      body,
      { responseType: 'blob' },
    );
    return resp.data as Blob;
  }

  /** CI/CD bundle: the configured run bundle PLUS GitHub Actions / GitLab CI /
   *  Jenkins pipelines that run the suite and report to the triage board. */
  async getCiBundle(
    artifactId: string,
    body: { categories?: string[]; test_ids?: string[]; base_url?: string; data?: Record<string, string>; data_by_test?: Record<string, Record<string, string>>; browsers?: string[]; headed?: boolean; workers?: number; retries?: number },
  ): Promise<Blob> {
    const resp = await this.client.post(
      `/v1/test-factory/${artifactId}/playwright/ci-bundle`,
      body,
      { responseType: 'blob' },
    );
    return resp.data as Blob;
  }

  /** Start a server-side run on the Nexus runner. Returns { run_id, status }. */
  async startNexusRun(
    artifactId: string,
    body: { categories?: string[]; test_ids?: string[]; base_url?: string; data?: Record<string, string>; data_by_test?: Record<string, Record<string, string>>; browsers?: string[]; headed?: boolean; workers?: number; retries?: number },
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/playwright/run`, body,
    );
    return data;
  }

  /** Start a HEADED, VNC-streamed run on the Nexus runner. Returns { run_id, live_url }. */
  async startNexusLiveRun(
    artifactId: string,
    body: { categories?: string[]; test_ids?: string[]; base_url?: string; data?: Record<string, string>; data_by_test?: Record<string, Record<string, string>>; browsers?: string[]; retries?: number },
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/playwright/run-live`, body,
    );
    return data;
  }

  /** Auto-Heal Run — run headed and auto diagnose+fix+re-run+continue until the
   *  whole suite is green, then save a "Clean Run - V1". Returns { run_id, live_url };
   *  poll getNexusRunStatus for { heal_trace, terminal_state, clean_run_version }. */
  async autoHealRun(
    artifactId: string,
    body: { categories?: string[]; test_ids?: string[]; base_url?: string; data?: Record<string, string>; enable_agentic_heal?: boolean; agentic_min_confidence?: number },
  ): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/auto-heal/run-config`, body,
    );
    return data;
  }

  /** Poll a Nexus runner job's live status (running -> passed/failed/...). */
  async getNexusRunStatus(artifactId: string, runId: string): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/playwright/run/${runId}`,
    );
    return data;
  }

  /** Phase C — per-test script version history (newest first). */
  async listScriptVersions(artifactId: string, testId: string): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/scripts/${testId}/versions`,
    );
    return data;
  }

  /** Per-script GROUNDED VERDICT HISTORY across recent runs: proven-green vs
   *  regression/drift/flake, duration, the final-frame success screenshot, and any
   *  heal event that landed on that run. Read-only, $0 LLM. */
  async getVerdictHistory(artifactId: string, scenarioId: string, limit = 20): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/scenarios/${encodeURIComponent(scenarioId)}/verdict-history`,
      { params: { limit } },
    );
    return data;
  }

  /** Agentic-QE per-agent ON/OFF config + catalog (for the toggles panel). $0 agents
   *  default ON; LLM agents default OFF. */
  async getAgenticConfig(): Promise<{ agents: Record<string, boolean>; defaults: Record<string, boolean>; catalog: string[] }> {
    const { data } = await this.client.get(`/v1/agentic/config`);
    return data;
  }

  /** Turn each agent on/off for this tenant (a toggle only gates whether an agent runs). */
  async setAgenticConfig(agents: Record<string, boolean>): Promise<{ agents: Record<string, boolean> }> {
    const { data } = await this.client.put(`/v1/agentic/config`, { agents });
    return data;
  }

  /** Phase 5 — Proven Control Ledger KB: controls whose heals are oracle-PROVEN green
   *  and reused across scenarios + recordings, with provenance (label/page/fix/confidence/
   *  app scope) + lifecycle (quarantined). scope = recording | app | tenant. Read-only. */
  async getProvenControls(
    artifactId: string,
    scope: 'recording' | 'app' | 'tenant' = 'recording',
    limit = 200,
  ): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/proven-controls`,
      { params: { scope, limit } },
    );
    return data;
  }

  /** Editor seed: the edited (active or a specific) source, else the
   *  deterministic parametrized compiled source. `edited` says which. */
  async getScriptSource(artifactId: string, testId: string, versionNo?: number): Promise<any> {
    const { data } = await this.client.get(
      `/v1/test-factory/${artifactId}/scripts/${testId}/source`,
      { params: versionNo ? { version_no: versionNo } : {} },
    );
    return data;
  }

  /** Save edits as a new immutable version (vN+1). */
  async saveScriptVersion(
    artifactId: string,
    body: { test_id: string; script_source: string; data?: Record<string, string>; note?: string },
  ): Promise<any> {
    const { data } = await this.client.post(`/v1/test-factory/${artifactId}/scripts/save`, body);
    return data;
  }

  /** Restore version N by appending it as a new highest version. */
  async restoreScriptVersion(
    artifactId: string,
    body: { test_id: string; version_no: number },
  ): Promise<any> {
    const { data } = await this.client.post(`/v1/test-factory/${artifactId}/scripts/restore`, body);
    return data;
  }

  /** Download the deterministic, grounded Playwright project (zip).
   *  Empty opts = whole active suite; {category} = one category;
   *  {testCaseId} = one specific test case (takes precedence). */
  async getPlaywrightBundle(
    artifactId: string,
    opts: { category?: string; testCaseId?: string } = {},
  ): Promise<Blob> {
    const params: Record<string, string> = {};
    if (opts.testCaseId) params.test_case_id = opts.testCaseId;
    else if (opts.category) params.category = opts.category;
    const resp = await this.client.get(
      `/v1/test-factory/${artifactId}/playwright`,
      { params, responseType: 'blob' },
    );
    return resp.data as Blob;
  }

  /** Capture per-action "what happened after" outcomes (vision) + regenerate. */
  async captureTestFactoryOutcomes(artifactId: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/capture-outcomes`, {},
      { timeout: 300_000 },
    );
    return data;
  }

  /** One-click enrichment: options + anchors + outcomes (vision) + regenerate. */
  async enrichTestFactory(artifactId: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/enrich`, {},
      { timeout: 600_000 },
    );
    return data;
  }

  /** Download the suite as Excel / CSV / JSON (blob). */
  async exportTestFactory(artifactId: string, format = 'excel', details = false, redact = false): Promise<Blob> {
    const resp = await this.client.get(
      `/v1/test-factory/${artifactId}/export`,
      { params: { format, details, redact }, responseType: 'blob' },
    );
    return resp.data as Blob;
  }

  /** Push the suite into a test-management tool (qtest|testrail|zephyr|xray). */
  async pushTestFactory(artifactId: string, tool: string): Promise<any> {
    const { data } = await this.client.post(
      `/v1/test-factory/${artifactId}/push/${tool}`, {},
    );
    return data;
  }

  /** Per-scenario last-run + flake summary. Already merged into the
   *  e2e_architect response; this endpoint is for refresh-friendly polling. */
  async getScenarioRunsSummary(
    artifactId: string,
  ): Promise<{
    artifact_id: string;
    scenarios: Record<string, import('../types/canonical').ScenarioLastRunSummary>;
  }> {
    const { data } = await this.client.get(
      `/v1/e2e-architect/${artifactId}/scenarios/runs/summary`,
    );
    return data;
  }

  /** Full detail of the most-recent run for one scenario: steps, drift,
   *  root-cause hints. */
  async getScenarioLastRunDetail(
    artifactId: string,
    scenarioId: string,
  ): Promise<import('../types/canonical').ScenarioLastRunDetailResponse> {
    const { data } = await this.client.get(
      `/v1/e2e-architect/${artifactId}/scenarios/${scenarioId}/last-run`,
    );
    return data;
  }

  // ── Demo Diff + Self-Healing (P7) ──────────────────────
  /** Structural diff between two visual evidence graphs +
   *  which scenarios in the current artifact are affected. */
  async diffArtifacts(
    baselineArtifactId: string,
    currentArtifactId: string,
  ): Promise<import('../types/canonical').ArtifactDiffResponse> {
    const { data } = await this.client.post(
      '/v1/artifacts/diff',
      {
        baseline_artifact_id: baselineArtifactId,
        current_artifact_id: currentArtifactId,
      },
    );
    return data;
  }

  /** Build a heal plan for stale evidence_control_ids in the artifact's
   *  scenarios. ``baselineArtifactId`` optionally provides original
   *  control labels for better matching. */
  async getHealSuggestions(
    artifactId: string,
    options?: { scenarioIds?: string[]; baselineArtifactId?: string },
  ): Promise<import('../types/canonical').HealSuggestionsResponse> {
    const { data } = await this.client.post(
      `/v1/e2e-architect/${artifactId}/scenarios/heal-suggestions`,
      {
        scenario_ids: options?.scenarioIds,
        baseline_artifact_id: options?.baselineArtifactId,
      },
    );
    return data;
  }

  /** Apply a chosen subset of heal suggestions. The backend re-validates
   *  each suggestion against the live visual graph before writing. */
  async applyHealSuggestions(
    artifactId: string,
    suggestions: Array<import('../types/canonical').HealSuggestion>,
  ): Promise<import('../types/canonical').HealApplyResponse> {
    const { data } = await this.client.post(
      `/v1/e2e-architect/${artifactId}/scenarios/apply-healing`,
      { suggestions },
    );
    return data;
  }

  // ── Phase 16: Tenant lifecycle admin (admin role only) ──────────
  async listTenants(state?: string) {
    const params = state && state !== 'all' ? `?state=${state}` : '';
    const { data } = await this.client.get(`/v1/admin/tenants${params}`);
    return data;
  }

  async getTenant(tenantId: string) {
    const { data } = await this.client.get(`/v1/admin/tenants/${tenantId}`);
    return data;
  }

  async createTenant(payload: {
    tenant_id?: string;
    display_name: string;
    tier?: string;
  }) {
    const { data } = await this.client.post('/v1/admin/tenants', payload);
    return data;
  }

  /** Apply one of: provision | suspend | resume | offboard | finalize-deletion */
  async transitionTenant(
    tenantId: string,
    action: 'provision' | 'suspend' | 'resume' | 'offboard' | 'finalize-deletion',
    body?: { reason?: string; retention_days?: number },
  ) {
    const { data } = await this.client.post(
      `/v1/admin/tenants/${tenantId}/${action}`,
      body && Object.keys(body).length > 0 ? body : undefined,
    );
    return data;
  }

  async exportTenantData(tenantId: string) {
    const { data } = await this.client.get(`/v1/admin/tenants/${tenantId}/export`);
    return data;
  }
}

export const api = new ApiClient();
export default api;
