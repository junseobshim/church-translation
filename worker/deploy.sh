#!/bin/bash
# Deploy the waiting-page Worker to BOTH the live and testing hostnames.
#
# Both configs share src/index.js, so the only way the two environments can
# diverge is by deploying one without the other. Always use this script rather
# than a bare `wrangler deploy` — testing is only useful as a mirror of live.
#
#   ./deploy.sh            deploy both (default)
#   ./deploy.sh test       deploy only testing (for iterating on worker code)
#
# Deploying only live is deliberately not offered: that is the direction that
# silently leaves testing stale.

set -euo pipefail
cd "$(dirname "$0")"

case "${1:-both}" in
  test)
    echo "[worker] Deploying testing only → testing.rctranslation.org"
    wrangler deploy -c wrangler.test.toml
    ;;
  both)
    echo "[worker] Deploying testing → testing.rctranslation.org"
    wrangler deploy -c wrangler.test.toml
    echo "[worker] Deploying live → live.rctranslation.org"
    wrangler deploy -c wrangler.toml
    echo "[worker] Both environments deployed from the same src/index.js."
    ;;
  *)
    echo "Usage: $0 [both|test]" >&2
    exit 1
    ;;
esac
