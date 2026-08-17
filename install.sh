#!/usr/bin/env bash
# Install the `skill` launcher (plus a `skillctl` compatibility alias) into
# ~/.local/bin so the CLI works from any directory.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${ROOT}/bin/skill" setup "$@"
