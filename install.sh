#!/usr/bin/env bash
set -euo pipefail

# Centralized Log Viewer installer
# - Detects arch, downloads latest release artifact, verifies checksum, and installs to /usr/local/bin (or ~/.local/bin)
# - Places CSS next to the binary
# - Ensures a user config at $XDG_CONFIG_HOME/clv/settings.conf (or ~/.config/clv/settings.conf)

# Repository can be provided via --repo <owner/repo> or CLV_REPO env var.
REPO="${CLV_REPO:-r0tifer/Centralized_Log_Viewer}"
# Optional: expected GPG signer fingerprint to verify SHA256SUMS signature.
GPG_FPR="${CLV_GPG_FPR:-}"
BIN_NAME="clv"
ASSET_PREFIX="clv"

usage() {
  cat <<EOF
Usage: curl -fsSL https://raw.githubusercontent.com/${REPO}/main/install.sh | bash

Options:
  --repo <owner/repo> GitHub repo (default: ${REPO})
  --gpg-fpr <fingerprint> Require SHA256SUMS to be signed by this GPG fingerprint
  --prefix <dir>      Install directory (default: /usr/local/bin or ~/.local/bin)
  --version <tag>     Install a specific version tag (e.g., v1.0.0). Defaults to latest
  --from-local <dir> Install from local build directory (expects 'clv' and 'log_viewer.css')
EOF
}

PREFIX=""
VERSION=""
FROM_LOCAL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2;;
    --repo) REPO="$2"; shift 2;;
    --gpg-fpr) GPG_FPR="$2"; shift 2;;
    --prefix) PREFIX="$2"; shift 2;;
    --version) VERSION="$2"; shift 2;;
    --from-local) FROM_LOCAL="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1"; usage; exit 1;;
  esac
done

detect_arch() {
  local uname_m
  uname_m=$(uname -m)
  case "$uname_m" in
    x86_64|amd64) echo "x86_64" ;;
    aarch64|arm64) echo "aarch64" ;;
    *) echo "Unsupported arch: $uname_m" >&2; exit 1 ;;
  esac
}

ensure_prefix() {
  if [[ -n "$PREFIX" ]]; then
    echo "$PREFIX"
    return
  fi
  if [[ -w /usr/local/bin ]]; then
    echo "/usr/local/bin"
  else
    echo "${HOME}/.local/bin"
    mkdir -p "${HOME}/.local/bin"
  fi
}

ensure_config() {
  local cfg_home cfg_dir cfg_file
  cfg_home="${XDG_CONFIG_HOME:-${HOME}/.config}"
  cfg_dir="${cfg_home}/clv"
  cfg_file="${cfg_dir}/settings.conf"
  mkdir -p "$cfg_dir"
  if [[ ! -f "$cfg_file" ]]; then
    if [[ -f settings.conf ]]; then
      cp -f settings.conf "$cfg_file"
    else
      cat > "$cfg_file" <<CFG
[log_viewer]
# Comma-separated absolute paths to log directories or files
log_dirs = /var/log
max_buffer_lines = 500
default_show_lines = 40
refresh_hz = 4
default_tree_width = 30
min_show_lines = 10
show_step = 10
CFG
    fi
    echo "Created default config at $cfg_file"
  fi
}

safe_extract_validate() {
  local tarfile="$1"
  local list
  list=$(tar -tzf "$tarfile")
  while IFS= read -r f; do
    if [[ "$f" = /* || "$f" == *".."* ]]; then
      echo "Refusing to extract unsafe path in archive: $f" >&2
      exit 1
    fi
    case "$f" in
      clv|log_viewer.css) ;;
      *) echo "Unexpected file in archive: $f" >&2; exit 1 ;;
    esac
  done <<< "$list"
}

verify_sha256sums_gpg() {
  local owner sums sums_sig tmpgnupg
  owner="${REPO%%/*}"
  sums="$1"
  sums_sig="$2"
  if ! command -v gpg >/dev/null 2>&1; then
    return 1
  fi
  tmpgnupg=$(mktemp -d)
  trap 'rm -rf "$tmpgnupg"' RETURN
  export GNUPGHOME="$tmpgnupg"
  curl -fsSL "https://api.github.com/users/${owner}/gpg_keys" \
    | grep -o '-----BEGIN PGP PUBLIC KEY BLOCK-----.*-----END PGP PUBLIC KEY BLOCK-----' \
    | sed 's/\\n/\n/g' \
    | gpg --batch --quiet --import 2>/dev/null || true
  if [[ -n "$GPG_FPR" ]]; then
    if ! gpg --batch --list-keys | grep -qi "$GPG_FPR"; then
      echo "Expected GPG fingerprint not found in imported keys" >&2
      return 1
    fi
  fi
  if gpg --batch --verify "$sums_sig" "$sums" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

download_and_install() {
  local arch prefix tmpdir tag api_url rel_api rel_json rel_asset_url rel_sha_url asset_name sha_name
  arch=$(detect_arch)
  prefix=$(ensure_prefix)
  tmpdir=$(mktemp -d)

  if [[ -z "$REPO" ]]; then
    echo "Missing --repo <owner/repo>. You can also set CLV_REPO env var." >&2
    exit 1
  fi

  if [[ -n "$VERSION" ]]; then
    tag="$VERSION"
  else
    # Resolve latest tag via GitHub API
    api_url="https://api.github.com/repos/${REPO}/releases/latest"
    tag=$(curl -fsSL "$api_url" | sed -n 's/.*"tag_name": "\([^"]\+\)".*/\1/p' | head -n1)
    if [[ -z "$tag" ]]; then
      echo "Failed to determine latest release tag" >&2
      exit 1
    fi
  fi

  echo "Resolving release assets for $REPO@$tag ..."
  rel_api="https://api.github.com/repos/${REPO}/releases/tags/${tag}"
  rel_json=$(curl -fsSL "$rel_api")
  # Find first matching asset URL for linux-<arch>.tar.gz
  rel_asset_url=$(echo "$rel_json" \
    | tr '\n' ' ' \
    | sed 's/\r//g' \
    | grep -o '"browser_download_url":"[^"]*"' \
    | cut -d '"' -f4 \
    | grep -E "/download/${tag}/clv-.*-linux-${arch}\.tar\.gz$" \
    | head -n1)
  # Matching checksum file (same basename plus .sha256)
  rel_sha_url=$(echo "$rel_json" \
    | tr '\n' ' ' \
    | sed 's/\r//g' \
    | grep -o '"browser_download_url":"[^"]*"' \
    | cut -d '"' -f4 \
    | grep -E "/download/${tag}/clv-.*-linux-${arch}\.tar\.gz\.sha256$" \
    | head -n1)

  if [[ -z "$rel_asset_url" ]]; then
    # Fallback to conventional name if not found
    asset_name="clv-${tag}-linux-${arch}.tar.gz"
    rel_asset_url="https://github.com/${REPO}/releases/download/${tag}/${asset_name}"
  else
    asset_name=$(basename "$rel_asset_url")
  fi

  echo "Downloading ${asset_name} ..."
  curl -fL "$rel_asset_url" -o "$tmpdir/$asset_name"

  # Download checksum if available and verify
  sha_name="${asset_name}.sha256"
  if [[ -z "$rel_sha_url" ]]; then
    # Try conventional name in release assets if not found via API
    rel_sha_url="https://github.com/${REPO}/releases/download/${tag}/${sha_name}"
  fi

  # Prefer aggregated SHA256SUMS + signature
  local sums_url sig_url
  sums_url="https://github.com/${REPO}/releases/download/${tag}/SHA256SUMS"
  sig_url="https://github.com/${REPO}/releases/download/${tag}/SHA256SUMS.asc"
  echo "Fetching SHA256SUMS ..."
  if curl -fL "$sums_url" -o "$tmpdir/SHA256SUMS" 2>/dev/null; then
    if curl -fL "$sig_url" -o "$tmpdir/SHA256SUMS.asc" 2>/dev/null; then
      if verify_sha256sums_gpg "$tmpdir/SHA256SUMS" "$tmpdir/SHA256SUMS.asc"; then
        echo "SHA256SUMS signature verified"
      else
        echo "Warning: failed to verify GPG signature; falling back to raw checksum check" >&2
      fi
    else
      echo "No signature found; using raw checksum check" >&2
    fi
    if command -v sha256sum >/dev/null 2>&1; then
      expected=$(grep -E "[ ]${asset_name}$" "$tmpdir/SHA256SUMS" | awk '{print $1}' | head -n1)
      if [[ -z "$expected" ]]; then
        echo "Checksum entry for ${asset_name} not found in SHA256SUMS" >&2
        exit 1
      fi
      actual=$(sha256sum "$tmpdir/$asset_name" | awk '{print $1}')
      if [[ "$expected" != "$actual" ]]; then
        echo "Checksum mismatch for ${asset_name}" >&2
        exit 1
      fi
      echo "Checksum OK"
    elif command -v shasum >/dev/null 2>&1; then
      expected=$(grep -E "[ ]${asset_name}$" "$tmpdir/SHA256SUMS" | awk '{print $1}' | head -n1)
      if [[ -z "$expected" ]]; then
        echo "Checksum entry for ${asset_name} not found in SHA256SUMS" >&2
        exit 1
      fi
      actual=$(shasum -a 256 "$tmpdir/$asset_name" | awk '{print $1}')
      if [[ "$expected" != "$actual" ]]; then
        echo "Checksum mismatch for ${asset_name}" >&2
        exit 1
      fi
      echo "Checksum OK"
    else
      echo "Warning: no sha256 tool found; skipping verification" >&2
    fi
  else
    echo "SHA256SUMS not found; trying per-asset checksum"
    sha_name="${asset_name}.sha256"
    if [[ -z "$rel_sha_url" ]]; then
      rel_sha_url="https://github.com/${REPO}/releases/download/${tag}/${sha_name}"
    fi
    if curl -fL "$rel_sha_url" -o "$tmpdir/$sha_name" 2>/dev/null; then
      if command -v sha256sum >/dev/null 2>&1; then
        (cd "$tmpdir" && sha256sum -c "$sha_name")
        echo "Checksum OK"
      elif command -v shasum >/dev/null 2>&1; then
        expected=$(cut -d ' ' -f1 "$tmpdir/$sha_name")
        actual=$(shasum -a 256 "$tmpdir/$asset_name" | awk '{print $1}')
        if [[ "$expected" != "$actual" ]]; then
          echo "Checksum mismatch" >&2
          exit 1
        fi
        echo "Checksum OK"
      else
        echo "Warning: no sha256 tool found; skipping verification" >&2
      fi
    else
      echo "Warning: checksum file not found; skipping verification" >&2
    fi
  fi

  echo "Validating and extracting ..."
  safe_extract_validate "$tmpdir/$asset_name"
  tar --no-same-owner -C "$tmpdir" -xzf "$tmpdir/$asset_name"

  # Expecting extracted files: clv, log_viewer.css
  install -m 0755 "$tmpdir/${BIN_NAME}" "$prefix/${BIN_NAME}"
  if [[ -f "$tmpdir/log_viewer.css" ]]; then
    install -m 0644 "$tmpdir/log_viewer.css" "$prefix/log_viewer.css"
  fi

  echo "Installed ${BIN_NAME} to $prefix"
}

install_from_local() {
  local arch prefix src
  arch=$(detect_arch) # not used, but kept for parity
  prefix=$(ensure_prefix)
  src="$FROM_LOCAL"
  if [[ ! -x "$src/clv" ]]; then
    echo "Local source missing 'clv' executable: $src" >&2
    exit 1
  fi
  install -m 0755 "$src/clv" "$prefix/clv"
  if [[ -f "$src/log_viewer.css" ]]; then
    install -m 0644 "$src/log_viewer.css" "$prefix/log_viewer.css"
  fi
  echo "Installed clv from local: $src -> $prefix"
}

main() {
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required" >&2; exit 1
  fi

  ensure_config

  if [[ -n "$FROM_LOCAL" ]]; then
    install_from_local
  else
    download_and_install
  fi

  # PATH hint
  if ! command -v clv >/dev/null 2>&1; then
    echo "Note: add \"$HOME/.local/bin\" to your PATH if not already present." >&2
  fi

  echo "Done. Run: clv"
}

main "$@"
