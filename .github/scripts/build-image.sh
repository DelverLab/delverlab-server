#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

export DELVERLAB_CI_IMAGE="${1:?Pass the local image reference}"
version="${2:-0.0.1-SNAPSHOT}"
project="delverlab-ci-$$-$RANDOM"
compose=(docker compose -p "$project" -f .github/scripts/compose.ci.yaml)
cleanup() {
  status=$?
  if (( status != 0 )); then
    "${compose[@]}" logs || true
  fi
  "${compose[@]}" down -v || true
  exit "$status"
}
trap cleanup EXIT

"${compose[@]}" up -d --wait --wait-timeout 90 postgres
address=$("${compose[@]}" port postgres 5432)
SPRING_DATASOURCE_URL="jdbc:postgresql://${address}/delverlab" \
SPRING_DATASOURCE_USERNAME=delverlab SPRING_DATASOURCE_PASSWORD=delverlab \
  ./gradlew --no-daemon --rerun-tasks build -PreleaseVersion="$version"

docker build --platform linux/amd64 --tag "$DELVERLAB_CI_IMAGE" \
  --label io.github.delverlab.source=https://github.com/DelverLab/delverlab-server \
  --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" \
  --label "org.opencontainers.image.version=$version" .
"${compose[@]}" up -d --wait --wait-timeout 120 server