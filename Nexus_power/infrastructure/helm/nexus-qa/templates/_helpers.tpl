{{/*
=================================================================
Nexus QA — Helm Template Helpers
=================================================================
*/}}

{{/*
Expand the name of the chart.
*/}}
{{- define "nexus-qa.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name.
Truncated at 63 chars because some k8s name fields are limited.
*/}}
{{- define "nexus-qa.fullname" -}}
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
Create chart name + version for chart label.
*/}}
{{- define "nexus-qa.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "nexus-qa.labels" -}}
helm.sh/chart: {{ include "nexus-qa.chart" . }}
{{ include "nexus-qa.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: nexus-qa
{{- end }}

{{/*
Selector labels (stable across upgrades).
*/}}
{{- define "nexus-qa.selectorLabels" -}}
app.kubernetes.io/name: {{ include "nexus-qa.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Generate labels for a specific service component.
Usage: {{ include "nexus-qa.componentLabels" (dict "root" . "component" "shield-engine") }}
*/}}
{{- define "nexus-qa.componentLabels" -}}
{{ include "nexus-qa.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Generate selector labels for a specific service component.
Usage: {{ include "nexus-qa.componentSelectorLabels" (dict "root" . "component" "shield-engine") }}
*/}}
{{- define "nexus-qa.componentSelectorLabels" -}}
{{ include "nexus-qa.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Construct an image reference from global + service settings.
Usage: {{ include "nexus-qa.image" (dict "root" . "service" "shield-engine" "tag" .Values.engines.shield.image.tag) }}
*/}}
{{- define "nexus-qa.image" -}}
{{- $registry := .root.Values.global.image.registry -}}
{{- $prefix := .root.Values.global.image.prefix -}}
{{- $tag := default .root.Values.global.image.tag .tag -}}
{{- printf "%s/%s-%s:%s" $registry $prefix .service $tag -}}
{{- end }}

{{/*
Name of the Secret containing shared credentials.
*/}}
{{- define "nexus-qa.secretName" -}}
{{ include "nexus-qa.fullname" . }}-secrets
{{- end }}

{{/*
Name of the ConfigMap containing shared non-secret configuration.
*/}}
{{- define "nexus-qa.configName" -}}
{{ include "nexus-qa.fullname" . }}-config
{{- end }}

{{/*
Common environment variables injected into every service pod.
References the shared Secret and ConfigMap.
*/}}
{{- define "nexus-qa.commonEnv" -}}
- name: NEXUS_ENV
  valueFrom:
    configMapKeyRef:
      name: {{ include "nexus-qa.configName" .root }}
      key: NEXUS_ENV
- name: REDIS_URL
  valueFrom:
    configMapKeyRef:
      name: {{ include "nexus-qa.configName" .root }}
      key: REDIS_URL
- name: AUTH_SERVICE_URL
  valueFrom:
    configMapKeyRef:
      name: {{ include "nexus-qa.configName" .root }}
      key: AUTH_SERVICE_URL
- name: JWT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "nexus-qa.secretName" .root }}
      key: jwt-secret
{{- end }}

{{/*
Return the fully qualified internal service URL for a component.
Usage: {{ include "nexus-qa.serviceUrl" (dict "root" . "component" "shield-engine" "port" 8001) }}
*/}}
{{- define "nexus-qa.serviceUrl" -}}
{{- $fullname := include "nexus-qa.fullname" .root -}}
{{- printf "http://%s-%s:%d" $fullname .component (int .port) -}}
{{- end }}

{{/*
Pod annotations applied to every Deployment template. Includes:
  - config and secret checksums for rollout-on-change
  - service-mesh injection annotation when .Values.mesh.linkerd.enabled
  - Prometheus scrape opt-in is set per workload, not here (ServiceMonitor)
  - any operator-supplied additional annotations via .Values.podAnnotations
Usage: {{ include "nexus-qa.podAnnotations" . | nindent 8 }}
*/}}
{{- define "nexus-qa.podAnnotations" -}}
checksum/config: {{ include (print .Template.BasePath "/configmap.yaml") . | sha256sum }}
checksum/secret: {{ include (print .Template.BasePath "/secret.yaml") . | sha256sum }}
{{- if and .Values.mesh .Values.mesh.linkerd .Values.mesh.linkerd.enabled }}
linkerd.io/inject: enabled
config.linkerd.io/proxy-await: enabled
config.linkerd.io/proxy-log-level: {{ default "warn,linkerd=info" .Values.mesh.linkerd.proxyLogLevel | quote }}
{{- end }}
{{- if and .Values.mesh .Values.mesh.istio .Values.mesh.istio.enabled }}
sidecar.istio.io/inject: "true"
proxy.istio.io/config: '{ "concurrency": 2 }'
{{- end }}
{{- with .Values.podAnnotations }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Pod-level securityContext. Merges hard defaults (PSS restricted) with any
operator overrides in .Values.podSecurityContext. Hard defaults are
enforced regardless of values — operators may TIGHTEN, never loosen.
Usage: {{ include "nexus-qa.podSecurityContext" . | nindent 8 }}
*/}}
{{- define "nexus-qa.podSecurityContext" -}}
{{- $defaults := dict
  "runAsNonRoot" true
  "runAsUser" 1000
  "runAsGroup" 1000
  "fsGroup" 1000
  "fsGroupChangePolicy" "OnRootMismatch"
  "seccompProfile" (dict "type" "RuntimeDefault")
-}}
{{- $merged := mergeOverwrite $defaults (default dict .Values.podSecurityContext) -}}
{{- toYaml $merged -}}
{{- end }}

{{/*
Container-level securityContext. Merges hard defaults (PSS restricted)
with operator overrides. capabilities.drop is always [ALL] — additions
are appended via .Values.containerSecurityContext.capabilities.add.
Usage: {{ include "nexus-qa.containerSecurityContext" . | nindent 12 }}
*/}}
{{- define "nexus-qa.containerSecurityContext" -}}
{{- $defaults := dict
  "runAsNonRoot" true
  "runAsUser" 1000
  "runAsGroup" 1000
  "allowPrivilegeEscalation" false
  "readOnlyRootFilesystem" true
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
Object storage env vars injected into every engine pod. Reads the
top-level .Values.storage selector and emits backend-specific vars +
secretRefs. The engine SDK uses NEXUS_STORAGE_BACKEND to pick the
right backend at startup.
Usage: {{ include "nexus-qa.storageEnv" . | nindent 12 }}
*/}}
{{- define "nexus-qa.storageEnv" -}}
{{- $storage := default dict .Values.storage -}}
{{- $backend := default "s3" $storage.backend -}}
- name: NEXUS_STORAGE_BACKEND
  value: {{ $backend | quote }}
- name: S3_PRESIGN_EXPIRY
  value: {{ default 3600 $storage.presignExpirySeconds | quote }}
{{- if eq $backend "s3" }}
- name: S3_BUCKET
  value: {{ $storage.bucket | quote }}
- name: S3_REGION
  value: {{ default "us-east-1" (default dict $storage.s3).region | quote }}
- name: S3_ENDPOINT
  value: {{ default "" (default dict $storage.s3).endpoint | quote }}
- name: S3_USE_SSL
  value: {{ default true (default dict $storage.s3).useSsl | quote }}
- name: S3_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "nexus-qa.secretName" . }}
      key: s3-access-key
- name: S3_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "nexus-qa.secretName" . }}
      key: s3-secret-key
{{- else if eq $backend "azure" }}
- name: AZURE_STORAGE_ACCOUNT
  value: {{ (default dict $storage.azure).account | quote }}
- name: AZURE_STORAGE_CONTAINER
  value: {{ default $storage.bucket (default dict $storage.azure).container | quote }}
{{- if (default dict $storage.azure).useDefaultCredential }}
- name: AZURE_USE_DEFAULT_CREDENTIAL
  value: "true"
{{- else }}
{{- if .Values.secrets.azureStorageConnString }}
- name: AZURE_STORAGE_CONNECTION_STRING
  valueFrom:
    secretKeyRef:
      name: {{ include "nexus-qa.secretName" . }}
      key: azure-storage-connection-string
{{- else if .Values.secrets.azureStorageKey }}
- name: AZURE_STORAGE_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "nexus-qa.secretName" . }}
      key: azure-storage-key
{{- end }}
{{- end }}
{{- else if eq $backend "gcs" }}
- name: GCS_BUCKET
  value: {{ default $storage.bucket (default dict $storage.gcs).bucket | quote }}
- name: GCS_PROJECT
  value: {{ default "" (default dict $storage.gcs).project | quote }}
{{- if not (default true (default dict $storage.gcs).workloadIdentity) }}
- name: GCS_SERVICE_ACCOUNT_FILE
  value: "/var/secrets/gcs/gcs-service-account.json"
{{- end }}
{{- else if eq $backend "local" }}
- name: NEXUS_STORAGE_PATH
  value: "/data/nexus"
{{- end }}
{{- end }}

{{/*
Render the volume + mount for an engine's working dir. Selects emptyDir
vs PVC based on engine.persistence.<vol>.mode (default emptyDir).
Usage:
  {{ include "nexus-qa.persistVolumeMount" (dict
        "name" "audio-storage" "mountPath" "/app/audio_files"
        "spec" .Values.engines.ears.persistence.audio) | nindent 12 }}
  {{ include "nexus-qa.persistVolume"      (dict
        "name" "audio-storage" "claimName" "nexus-audio"
        "spec" .Values.engines.ears.persistence.audio) | nindent 8 }}
*/}}
{{- define "nexus-qa.persistVolumeMount" -}}
- name: {{ .name }}
  mountPath: {{ .mountPath }}
{{- end }}

{{- define "nexus-qa.persistVolume" -}}
{{- $spec := default dict .spec -}}
{{- $mode := default "emptyDir" $spec.mode -}}
- name: {{ .name }}
{{- if eq $mode "pvc" }}
  persistentVolumeClaim:
    claimName: {{ .claimName }}
{{- else }}
  emptyDir:
    sizeLimit: {{ default "5Gi" $spec.sizeLimit | quote }}
{{- end }}
{{- end }}

{{/*
Common pod spec fragment that ALL Deployments should include for
production hardening: automountServiceAccountToken control, topology
spread, priorityClassName, dnsPolicy, terminationGracePeriodSeconds.
Usage: {{ include "nexus-qa.podSpecCommon" (dict "root" . "component" "shield-engine") | nindent 6 }}
*/}}
{{- define "nexus-qa.podSpecCommon" -}}
{{- $root := .root -}}
{{- $component := .component -}}
automountServiceAccountToken: {{ default true $root.Values.serviceAccount.automount }}
terminationGracePeriodSeconds: {{ default 60 $root.Values.terminationGracePeriodSeconds }}
{{- if $root.Values.priorityClassName }}
priorityClassName: {{ $root.Values.priorityClassName }}
{{- end }}
{{- with $root.Values.topologySpread }}
{{- if .enabled }}
topologySpreadConstraints:
  - maxSkew: {{ .maxSkew | default 1 }}
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: {{ .whenUnsatisfiable | default "ScheduleAnyway" }}
    labelSelector:
      matchLabels:
        {{- include "nexus-qa.componentSelectorLabels" (dict "root" $root "component" $component) | nindent 8 }}
  - maxSkew: {{ .maxSkew | default 1 }}
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: {{ .whenUnsatisfiable | default "ScheduleAnyway" }}
    labelSelector:
      matchLabels:
        {{- include "nexus-qa.componentSelectorLabels" (dict "root" $root "component" $component) | nindent 8 }}
{{- end }}
{{- end }}
{{- end }}
