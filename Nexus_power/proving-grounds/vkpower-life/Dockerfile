# VKPower Life Insurance — Next.js static-export proving ground.
# Multi-stage: Node builds the static site, nginx:alpine serves it.

# ──── Stage 1: Build ─────────────────────────────────────────────
FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --ignore-scripts
COPY . .
RUN npm run build

# ──── Stage 2: Serve ─────────────────────────────────────────────
FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/out /usr/share/nginx/html
COPY answer_key.json /usr/share/nginx/html/answer_key.json
EXPOSE 80
