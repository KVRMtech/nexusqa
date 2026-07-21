{{/*
=================================================================
VKPower Verdict — Helm Template Helpers

Mirrors the nexus-qa chart helper conventions (infrastructure/helm/
nexus-qa/templates/_helpers.tpl) so the two charts read the same and
an operator carries one mental model across both. Every helper is
namespaced "verdict.*".
=================================================================
*/}}

{{/*
Expand the name of the chart.
*/}}
{{- define "verdict.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name (truncated at 63 chars — k8s name limit).
*/}}
{{- define "verdict.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart name + version for the chart label.
*/}}
{{- define "verdict.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "verdict.labels" -}}
helm.sh/chart: {{ include "verdict.chart" . }}
{{ include "verdict.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: vkpower-verdict
{{- end }}

{{/*
Selector labels (stable across upgrades).
*/}}
{{- define "verdict.selectorLabels" -}}
app.kubernetes.io/name: {{ include "verdict.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Component labels.
Usage: {{ include "verdict.componentLabels" (dict "root" . "component" "qe-central") }}
*/}}
{{- define "verdict.componentLabels" -}}
{{ include "verdict.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Component selector labels.
Usage: {{ include "verdict.componentSelectorLabels" (dict "root" . "component" "qe-central") }}
*/}}
{{- define "verdict.componentSelectorLabels" -}}
{{ include "verdict.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Construct an image reference from the global registry + repository and a
per-component image name/tag. For an air-gapped install, set
global.image.registry to the client's private registry (see
values-airgapped.yaml) and the same {registry}/{repository}/{name}:{tag}
coordinates are pulled locally.
Usage: {{ include "verdict.image" (dict "root" . "name" "qe-central" "tag" .Values.qeCentral.image.tag) }}
*/}}
{{- define "verdict.image" -}}
{{- $registry := .root.Values.global.image.registry -}}
{{- $repo := .root.Values.global.image.repository -}}
{{- $tag := default .root.Values.global.image.tag .tag -}}
{{- if $registry -}}
{{- printf "%s/%s/%s:%s" $registry $repo .name $tag -}}
{{- else -}}
{{- printf "%s/%s:%s" $repo .name $tag -}}
{{- end -}}
{{- end }}

{{/*
Name of the Secret holding Verdict credentials.
*/}}
{{- define "verdict.secretName" -}}
{{ include "verdict.fullname" . }}-secrets
{{- end }}

{{/*
Name of the ConfigMap holding shared non-secret configuration.
*/}}
{{- define "verdict.configName" -}}
{{ include "verdict.fullname" . }}-config
{{- end }}

{{/*
The name of a component's Service / Deployment.
Usage: {{ include "verdict.componentName" (dict "root" . "component" "qe-central") }}
*/}}
{{- define "verdict.componentName" -}}
{{- printf "%s-%s" (include "verdict.fullname" .root) .component -}}
{{- end }}

{{/*
In-cluster URL for a Verdict component.
Usage: {{ include "verdict.serviceUrl" (dict "root" . "component" "qe-central" "port" 8093) }}
*/}}
{{- define "verdict.serviceUrl" -}}
{{- printf "http://%s-%s:%d" (include "verdict.fullname" .root) .component (int .port) -}}
{{- end }}

{{/*
The in-cluster host:port of the qecentral database — either the chart-managed
Postgres (postgres.enabled) or the operator-supplied external endpoint.
Returns just the "host:port" string.
*/}}
{{- define "verdict.pgEndpoint" -}}
{{- if .Values.postgres.enabled -}}
{{- printf "%s-postgres:%d" (include "verdict.fullname" .) (int .Values.postgres.port) -}}
{{- else -}}
{{- printf "%s:%d" (required "postgres.external.host is required when postgres.enabled is false" .Values.postgres.external.host) (int (default .Values.postgres.port .Values.postgres.external.port)) -}}
{{- end -}}
{{- end }}

{{/*
Pod annotations — checksum config+secret for rollout-on-change plus any
operator-supplied annotations.
Usage: {{ include "verdict.podAnnotations" . | nindent 8 }}
*/}}
{{- define "verdict.podAnnotations" -}}
checksum/config: {{ include (print .Template.BasePath "/configmap.yaml") . | sha256sum }}
{{- if not .Values.externalSecrets.enabled }}
checksum/secret: {{ include (print .Template.BasePath "/secret.yaml") . | sha256sum }}
{{- end }}
{{- with .Values.podAnnotations }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Pod-level securityContext. Enforces a non-root, seccomp-confined posture. It
deliberately does NOT pin runAsUser/runAsGroup — the Verdict images ship their
own non-root USER (nexus / explorer, both system users), so the container's
declared user stands and the chowned in-image data dirs keep working. fsGroup
is added as a supplemental group so mounted PVCs/emptyDirs are group-writable by
that user regardless of its uid. Operators may TIGHTEN via values.
Usage: {{ include "verdict.podSecurityContext" . | nindent 8 }}
*/}}
{{- define "verdict.podSecurityContext" -}}
{{- $defaults := dict
  "runAsNonRoot" true
  "fsGroup" 999
  "fsGroupChangePolicy" "OnRootMismatch"
  "seccompProfile" (dict "type" "RuntimeDefault")
-}}
{{- $merged := mergeOverwrite $defaults (default dict .Values.podSecurityContext) -}}
{{- toYaml $merged -}}
{{- end }}

{{/*
Container-level securityContext. capabilities.drop is always [ALL]; additions
are appended via containerSecurityContext.capabilities.add. readOnlyRootFilesystem
defaults FALSE for the Verdict plane (the services write to their in-image data
dirs and the browser writes broadly) — operators may flip it on with matching
writable mounts.
Usage: {{ include "verdict.containerSecurityContext" . | nindent 12 }}
*/}}
{{- define "verdict.containerSecurityContext" -}}
{{- $defaults := dict
  "runAsNonRoot" true
  "allowPrivilegeEscalation" false
  "readOnlyRootFilesystem" false
  "privileged" false
  "seccompProfile" (dict "type" "RuntimeDefault")
-}}
{{- $caps := dict "drop" (list "ALL") -}}
{{- $userCaps := default dict (default dict .Values.containerSecurityContext).capabilities -}}
{{- if hasKey $userCaps "add" -}}
{{- $_ := set $caps "add" (index $userCaps "add") -}}
{{- end -}}
{{- $merged := mergeOverwrite $defaults (omit (default dict .Values.containerSecurityContext) "capabilities") -}}
{{- $_ := set $merged "capabilities" $caps -}}
{{- toYaml $merged -}}
{{- end }}

{{/*
Common pod spec fragment for every Deployment: service-account token control,
grace period, imagePullSecrets, priorityClassName.
Usage: {{ include "verdict.podSpecCommon" . | nindent 6 }}
*/}}
{{- define "verdict.podSpecCommon" -}}
automountServiceAccountToken: {{ default false .Values.serviceAccount.automount }}
terminationGracePeriodSeconds: {{ default 60 .Values.terminationGracePeriodSeconds }}
{{- if .Values.priorityClassName }}
priorityClassName: {{ .Values.priorityClassName }}
{{- end }}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{/*
The envelope-encryption (KEK) env block — shared by qe-central + repo-intel.
Wires NEXUS_KEK_PROVIDER and the provider-specific coordinates. `local` is the
dev KEK the fail-closed boot gate REFUSES in staging/production (see
app.security.boot_validator); on-prem sets gcp_kms / aws_kms, or the SDK-envelope
`local` provider backed by the client's OWN key on a mounted secret/PVC.
Usage: {{ include "verdict.kekEnv" . | nindent 12 }}
*/}}
{{- define "verdict.kekEnv" -}}
- name: NEXUS_KEK_PROVIDER
  value: {{ .Values.kek.provider | quote }}
- name: NEXUS_LOCAL_KEK_PATH
  value: {{ .Values.kek.localKekPath | quote }}
- name: NEXUS_KEK_GCP_KEY
  value: {{ .Values.kek.gcpKey | default "" | quote }}
- name: NEXUS_KEK_AWS_ARN
  value: {{ .Values.kek.awsArn | default "" | quote }}
- name: AWS_REGION
  value: {{ .Values.kek.awsRegion | default "" | quote }}
{{- end }}

{{/*
Render-time guard: a multi-replica qe-central is only CORRECT with the shared
redis admission mutex + a single fleet-scan leader. The `memory` admission
backend is PROCESS-LOCAL, so each replica keeps its own limiter — two replicas
would each admit a crawl on the SAME host, breaking the global politeness mutex
(design P2, "admission is a mutex") and double-crawling the customer's app.
`daemonLeaderElection=none` means every replica scans the fleet and schedules
duplicate cycles. This fails the render so an unsafe scale-out can NEVER be
installed — the safe default (replicas=1 / memory) renders unchanged.
Usage (once, before the Deployment): {{ include "verdict.validateScaling" . }}
*/}}
{{- define "verdict.validateScaling" -}}
{{- $qc := .Values.qeCentral -}}
{{- $env := .Values.env -}}
{{- $maxReplicas := int $qc.replicas -}}
{{- if $qc.autoscaling.enabled -}}{{- $maxReplicas = int $qc.autoscaling.maxReplicas -}}{{- end -}}
{{- if eq $env.admissionBackend "redis" -}}
  {{- if not $env.redisUrl -}}
    {{- fail "verdict: env.admissionBackend=redis but env.redisUrl is empty — the redis limiter FAIL-CLOSES every admission (nothing would ever crawl). Set env.redisUrl to the shared redis DSN." -}}
  {{- end -}}
{{- end -}}
{{- if gt $maxReplicas 1 -}}
  {{- if ne $env.admissionBackend "redis" -}}
    {{- fail (printf "verdict: qe-central can run up to %d replicas but env.admissionBackend=%q. Multi-replica REQUIRES admissionBackend=redis — memory admission is process-local, so replicas would each admit on the same host and break the global politeness mutex. Set env.admissionBackend=redis (+ env.redisUrl), or keep it single-replica (qeCentral.replicas=1 AND qeCentral.autoscaling.maxReplicas=1)." $maxReplicas $env.admissionBackend) -}}
  {{- end -}}
  {{- if ne $env.daemonLeaderElection "advisory_lock" -}}
    {{- fail (printf "verdict: qe-central can run up to %d replicas but env.daemonLeaderElection=%q. Multi-replica REQUIRES daemonLeaderElection=advisory_lock so exactly ONE replica scans the fleet; otherwise every replica schedules duplicate cycles. Set env.daemonLeaderElection=advisory_lock." $maxReplicas $env.daemonLeaderElection) -}}
  {{- end -}}
{{- end -}}
{{- end -}}
