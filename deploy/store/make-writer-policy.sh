#!/usr/bin/env bash
# Usage: make-writer-policy.sh <project>
# Prints the pipeline-writer policy for one project on stdout.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <project>" >&2
  exit 2
fi
project="$1"
if ! [[ "$project" =~ ^[a-z0-9][a-z0-9._-]{0,99}$ ]]; then
  echo "invalid project name: must match ^[a-z0-9][a-z0-9._-]{0,99}\$" >&2
  exit 2
fi
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The validated name contains no sed metacharacters other than '.', which is literal in the replacement.
sed "s/PROJECT/${project}/g" "$here/policies/pipeline-writer.template.json"
