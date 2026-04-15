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
