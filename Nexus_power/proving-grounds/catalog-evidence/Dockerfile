# Meridian Life — the M2.2 catalogue-evidence proving ground.
# Self-contained static application served by nginx, exactly as acme-life is.
# Every behaviour the catalogue must describe lives in the page JS, undeclared
# in the markup on purpose — see the comment block at the top of index.html.
FROM nginx:alpine
COPY index.html /usr/share/nginx/html/index.html
EXPOSE 80
