#!/usr/bin/env bash
set -euo pipefail

# Centralized Log Viewer installer
#
# Downloads the release tarball for this machine's architecture, verifies it
# against SHA256SUMS (and its GPG signature when available), and installs the
# PyInstaller onedir tree plus a launcher on PATH.
#
# The release tarball is:
#   centralized-log-viewer-linux-<arch>.tar.gz
# and contains a single top-level directory:
#   clv/
#     clv           <- executable
#     _internal/    <- runtime, including the bundled settings.conf template
#
# CLV creates its own ~/.config/clv/settings.conf on first run from that
# bundled template, so this script deliberately does not write one. Duplicating
# the template here is how the installed defaults previously drifted out of
# sync with the application's.

REPO="${CLV_REPO:-r0tifer/Centralized_Log_Viewer}"
APP_NAME="centralized-log-viewer"
BIN_NAME="clv"
# Optional: require SHA256SUMS to be signed by this GPG fingerprint.
GPG_FPR="${CLV_GPG_FPR:-}"

PREFIX=""
LIBDIR=""
VERSION=""
FROM_LOCAL=""

usage() {
  cat <<EOF
Usage: curl -fsSL https://raw.githubusercontent.com/${REPO}/main/install.sh | bash

Options:
  --repo <owner/repo>   GitHub repo (default: ${REPO})
  --version <tag>       Install a specific tag (e.g. v2.1.0). Defaults to latest
  --prefix <dir>        Directory for the launcher (default: /usr/local/bin or ~/.local/bin)
  --libdir <dir>        Directory for the program tree
                        (default: /opt/${APP_NAME} or ~/.local/share/${APP_NAME})
  --gpg-fpr <fpr>       Require SHA256SUMS to be signed by this GPG fingerprint
  --from-local <dir>    Install from a local build (the 'dist/clv' directory
                        produced by PyInstaller)
  -h, --help            Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2;;
    --gpg-fpr) GPG_FPR="$2"; shift 2;;
    --prefix) PREFIX="$2"; shift 2;;
    --libdir) LIBDIR="$2"; shift 2;;
    --version) VERSION="$2"; shift 2;;
    --from-local) FROM_LOCAL="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1" >&2; usage; exit 1;;
  esac
done

log()  { printf '%s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
die()  { printf 'Error: %s\n' "$*" >&2; exit 1; }

# Scratch directory, cleaned up on exit.
#
# This is deliberately script-scope rather than a function local: the EXIT trap
# runs after the function that created it has returned, so a local would be out
# of scope by then and `set -u` would abort the trap. That left the temp tree
# behind and made a successful install exit non-zero.
SCRATCH_DIR=""

cleanup() {
  [[ -n "${SCRATCH_DIR:-}" && -d "${SCRATCH_DIR}" ]] && rm -rf "${SCRATCH_DIR}"
  return 0
}
trap cleanup EXIT

make_scratch() {
  SCRATCH_DIR="$(mktemp -d)"
}

detect_arch() {
  local machine
  machine="$(uname -m)"
  case "$machine" in
    x86_64|amd64) echo "x86_64" ;;
    aarch64|arm64) echo "aarch64" ;;
    *)
      die "Unsupported architecture '${machine}'. Prebuilt packages exist for x86_64 and aarch64; install from source instead: pip install git+https://github.com/${REPO}.git"
      ;;
  esac
}

resolve_bindir() {
  if [[ -n "$PREFIX" ]]; then
    mkdir -p "$PREFIX"
    echo "$PREFIX"
  elif [[ -w /usr/local/bin ]]; then
    echo "/usr/local/bin"
  else
    mkdir -p "${HOME}/.local/bin"
    echo "${HOME}/.local/bin"
  fi
}

resolve_libdir() {
  if [[ -n "$LIBDIR" ]]; then
    echo "$LIBDIR"
  elif [[ -w /opt ]]; then
    echo "/opt/${APP_NAME}"
  else
    echo "${HOME}/.local/share/${APP_NAME}"
  fi
}

# Reject archives that would write outside the extraction directory, or that
# are not the expected single-top-level-directory shape.
validate_archive() {
  local tarfile="$1" entry
  while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    case "$entry" in
      /*|*..*) die "Refusing to extract unsafe path from archive: ${entry}" ;;
      "${BIN_NAME}"|"${BIN_NAME}"/*) ;;
      *) die "Unexpected entry in archive (expected everything under '${BIN_NAME}/'): ${entry}" ;;
    esac
  done < <(tar -tzf "$tarfile")
}

verify_signature() {
  local sums="$1" sig="$2" owner keyring keys
  owner="${REPO%%/*}"

  command -v gpg >/dev/null 2>&1 || return 1

  # A keyring under the scratch dir, so it is removed with everything else and
  # the caller's GNUPGHOME is never touched. Passing --homedir per invocation
  # avoids exporting an environment variable that would outlive the directory
  # it points at.
  [[ -n "${SCRATCH_DIR:-}" ]] || return 1
  keyring="${SCRATCH_DIR}/gnupg"
  rm -rf "$keyring"
  mkdir -p "$keyring"
  chmod 0700 "$keyring"

  # Import the owner's public keys as published by GitHub.
  if command -v python3 >/dev/null 2>&1; then
    keys="$(curl -fsSL "https://api.github.com/users/${owner}/gpg_keys" \
      | python3 -c 'import json,sys; print("\n".join(k.get("raw_key") or "" for k in json.load(sys.stdin)))' 2>/dev/null || true)"
  else
    # Fall back to a text scrape when python3 is unavailable.
    keys="$(curl -fsSL "https://api.github.com/users/${owner}/gpg_keys" \
      | grep -o '"raw_key": *"[^"]*"' | cut -d'"' -f4 | sed 's/\\n/\n/g' || true)"
  fi
  [[ -n "$keys" ]] || return 1
  printf '%s\n' "$keys" | gpg --homedir "$keyring" --batch --quiet --import 2>/dev/null || return 1

  if [[ -n "$GPG_FPR" ]]; then
    gpg --homedir "$keyring" --batch --list-keys --with-colons \
      | grep -qi "$(printf '%s' "$GPG_FPR" | tr -d ' ')" \
      || { warn "Required GPG fingerprint not found among ${owner}'s keys"; return 1; }
  fi

  gpg --homedir "$keyring" --batch --verify "$sig" "$sums" >/dev/null 2>&1
}

verify_checksum() {
  local dir="$1" asset="$2" expected actual sha
  if command -v sha256sum >/dev/null 2>&1; then
    sha="sha256sum"
  elif command -v shasum >/dev/null 2>&1; then
    sha="shasum -a 256"
  else
    warn "No sha256 tool available; skipping checksum verification"
    return 0
  fi

  expected="$(awk -v f="$asset" '$2 == f {print $1}' "${dir}/SHA256SUMS" | head -n1)"
  [[ -n "$expected" ]] || die "No checksum entry for ${asset} in SHA256SUMS"
  actual="$(cd "$dir" && $sha "$asset" | awk '{print $1}')"
  [[ "$expected" == "$actual" ]] || die "Checksum mismatch for ${asset}"
  log "Checksum OK"
}

resolve_tag() {
  local tag
  if [[ -n "$VERSION" ]]; then
    printf '%s' "$VERSION"
    return
  fi
  tag="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | grep -o '"tag_name"[[:space:]]*:[[:space:]]*"[^"]*"' \
    | cut -d'"' -f4 | head -n1)"
  [[ -n "$tag" ]] || die "Could not determine the latest release tag for ${REPO}"
  printf '%s' "$tag"
}

install_tree() {
  local src="$1" libdir="$2" bindir="$3"

  [[ -x "${src}/${BIN_NAME}" ]] || die "No '${BIN_NAME}' executable found in ${src}"

  log "Installing program tree to ${libdir}"
  if [[ -d "$libdir" ]]; then
    rm -rf "${libdir:?}/"*
  else
    mkdir -p "$libdir"
  fi
  cp -a "${src}/." "${libdir}/"
  chmod 0755 "${libdir}/${BIN_NAME}"

  log "Installing launcher to ${bindir}/${BIN_NAME}"
  cat > "${bindir}/${BIN_NAME}" <<LAUNCHER
#!/usr/bin/env bash
exec "${libdir}/${BIN_NAME}" "\$@"
LAUNCHER
  chmod 0755 "${bindir}/${BIN_NAME}"
}

download_and_install() {
  local arch bindir libdir tag tmpdir asset base_url
  arch="$(detect_arch)"
  bindir="$(resolve_bindir)"
  libdir="$(resolve_libdir)"
  tag="$(resolve_tag)"
  asset="${APP_NAME}-linux-${arch}.tar.gz"
  base_url="https://github.com/${REPO}/releases/download/${tag}"

  make_scratch
  tmpdir="$SCRATCH_DIR"

  log "Installing ${APP_NAME} ${tag} (${arch}) from ${REPO}"

  log "Downloading ${asset} ..."
  curl -fL --retry 3 "${base_url}/${asset}" -o "${tmpdir}/${asset}" \
    || die "Could not download ${asset} from release ${tag}. Does that release include an ${arch} build?"

  log "Fetching SHA256SUMS ..."
  if curl -fsSL "${base_url}/SHA256SUMS" -o "${tmpdir}/SHA256SUMS"; then
    if curl -fsSL "${base_url}/SHA256SUMS.asc" -o "${tmpdir}/SHA256SUMS.asc" 2>/dev/null; then
      if verify_signature "${tmpdir}/SHA256SUMS" "${tmpdir}/SHA256SUMS.asc"; then
        log "SHA256SUMS signature verified"
      elif [[ -n "$GPG_FPR" ]]; then
        die "SHA256SUMS signature verification failed and --gpg-fpr was required"
      else
        warn "Could not verify the GPG signature; falling back to the raw checksum"
      fi
    elif [[ -n "$GPG_FPR" ]]; then
      die "No SHA256SUMS.asc in release ${tag} but --gpg-fpr was required"
    else
      warn "No signature published for this release; using the raw checksum"
    fi
    verify_checksum "$tmpdir" "$asset"
  elif [[ -n "$GPG_FPR" ]]; then
    die "No SHA256SUMS in release ${tag} but --gpg-fpr was required"
  else
    warn "No SHA256SUMS published for this release; skipping verification"
  fi

  log "Validating and extracting ..."
  validate_archive "${tmpdir}/${asset}"
  tar --no-same-owner -C "$tmpdir" -xzf "${tmpdir}/${asset}"

  install_tree "${tmpdir}/${BIN_NAME}" "$libdir" "$bindir"
}

install_from_local() {
  local bindir libdir
  bindir="$(resolve_bindir)"
  libdir="$(resolve_libdir)"
  [[ -d "$FROM_LOCAL" ]] || die "Local source directory not found: ${FROM_LOCAL}"
  install_tree "$FROM_LOCAL" "$libdir" "$bindir"
}

main() {
  command -v curl >/dev/null 2>&1 || die "curl is required"
  command -v tar  >/dev/null 2>&1 || die "tar is required"

  if [[ -n "$FROM_LOCAL" ]]; then
    install_from_local
  else
    download_and_install
  fi

  local bindir
  bindir="$(resolve_bindir)"
  if ! command -v "$BIN_NAME" >/dev/null 2>&1; then
    warn "${bindir} is not on your PATH. Add it with:"
    # shellcheck disable=SC2016  # $PATH is literal here: it is a line to copy
    printf '  export PATH="%s:$PATH"\n' "$bindir" >&2
  fi

  log "Done. Run: ${BIN_NAME}"
  log "CLV will create ~/.config/clv/settings.conf on first run."
}

main "$@"
