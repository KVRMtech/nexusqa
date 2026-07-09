"""Stack detection for repo-intel.

:mod:`app.detect.stack` fingerprints a cloned repository from its manifest
files (package.json, pom.xml, build.gradle, Gemfile, *.csproj) and vendor
markers (Guidewire/Pega/Salesforce), returning which extractors apply and
their PUBLISHED static-rule accuracy ceiling bands (honesty: rules never
claim recall above the band; opaque low-code stacks route to crawl+human).
"""
