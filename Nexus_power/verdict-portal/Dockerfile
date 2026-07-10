# VKPower Verdict — production image (multi-stage: Vite build → nginx static).
# Additive to the QE-Central plane; built only under the `portal` compose profile.
# The API base is env-driven at BUILD time (Vite inlines VITE_* at build). Default
# is empty ⇒ same-origin relative requests, which nginx proxies to qe-central.

# ── build ──────────────────────────────────────────────────────────────────
FROM node:18-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci 2>/dev/null || npm install
COPY . .
# Build-time config (Vite inlines these). Leave VITE_QEC_MOCK empty for the real
# API (nginx proxies /api + /health); set it to 1 for a self-contained demo.
ARG VITE_QEC_API_URL=
ARG VITE_QEC_MOCK=
ARG VITE_QEC_AUDIENCE=vkpower-verdict
ENV VITE_QEC_API_URL=${VITE_QEC_API_URL} \
    VITE_QEC_MOCK=${VITE_QEC_MOCK} \
    VITE_QEC_AUDIENCE=${VITE_QEC_AUDIENCE}
RUN npm run build

# ── serve ──────────────────────────────────────────────────────────────────
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=4s --retries=3 \
  CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1
