#!/usr/bin/env bash
# Install a verified GitHub CLI binary into this run's temporary directory.
# Called only from trusted org-actions/main by the deployment reusable workflow.
set -euo pipefail

version=2.105.0
case "$(uname -m)" in
  x86_64)
    arch=amd64
    expected_sha256=79e7ab5db738c48777b696e374f8b428f0647debfb424f44d2f426370407fe2f
    ;;
  aarch64|arm64)
    arch=arm64
    expected_sha256=b1278c75107423d818f1937fd92cafc9e5e9b606631568f68d7fb816f5885a02
    ;;
  *) echo "Unsupported GitHub CLI architecture: $(uname -m)" >&2; exit 1 ;;
esac

file="gh_${version}_linux_${arch}.tar.gz"
base="https://github.com/cli/cli/releases/download/v${version}"
dir="${RUNNER_TEMP:?RUNNER_TEMP is required}/gh-cli-v${version}"
mkdir -p "$dir"
curl --fail --location --retry 3 --retry-delay 2 --silent --show-error \
  "$base/$file" -o "$dir/$file"
printf '%s  %s\n' "$expected_sha256" "$dir/$file" | sha256sum --check --status
tar -xzf "$dir/$file" -C "$dir"
cli="$dir/gh_${version}_linux_${arch}/bin/gh"
test -x "$cli"
"$cli" --version
printf '%s\n' "$(dirname "$cli")" >> "${GITHUB_PATH:?GITHUB_PATH is required}"
