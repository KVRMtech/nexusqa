# VKPower Life Insurance — life-insurance proving ground for VKPower Verdict.
# Self-contained static SPA served by nginx (same pattern as Acme/Aegis/Skyward).
# No backend; the multi-step login ladder (member no. -> password -> PIN) and the
# two seeded members (young-small-family / senior-large-family) live in page JS.
FROM nginx:alpine
COPY index.html /usr/share/nginx/html/index.html
COPY answer_key.json /usr/share/nginx/html/answer_key.json
EXPOSE 80
