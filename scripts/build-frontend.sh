#!/usr/bin/env bash
# Copy the static frontend into an output dir and point it at the backend.
set -euo pipefail
out="${1:-_site}"
: "${API_BASE_URL:?API_BASE_URL must be set (e.g. https://umd-api.onrender.com)}"
if [[ ! "$API_BASE_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?/?$ ]]; then
  echo "API_BASE_URL must look like https://host[:port]" >&2; exit 1
fi
rm -rf "$out" && mkdir -p "$out"
cp frontend/index.html frontend/style.css frontend/app.js "$out/"
printf 'window.UMD_CONFIG = { apiBase: "%s" };\n' "${API_BASE_URL%/}" > "$out/config.js"
