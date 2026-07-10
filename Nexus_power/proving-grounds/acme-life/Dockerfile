# Acme Life — synthetic life-insurance proving ground for VKPower Verdict.
# Self-contained static SPA served by nginx (same pattern as the Aegis/Skyward
# grounds). No backend; the over-60 business rule + its break-mode live in the
# page JS (mirrors the Aegis "?break=" convention).
FROM nginx:alpine
COPY index.html /usr/share/nginx/html/index.html
COPY answer_key.json /usr/share/nginx/html/answer_key.json
EXPOSE 80
