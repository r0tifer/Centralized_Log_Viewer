#!/bin/sh
# Run the opt-in remote integration suite against a throwaway container.
#
#   tests/containers/run.sh alpine     # BusyBox — the profile that matters
#   tests/containers/run.sh gnu        # GNU coreutils — the control
#
# Why this exists: the default suite proves the transport against fake runners
# and the local shell, which is where Requirement 14 — the suite never touches a
# network — is kept. What that cannot catch is Requirement 5, because a fake
# runner returns whatever fixture it was given. A BusyBox `find` with no
# `-printf` and a `stat` with different format letters both pass a fixture and
# fail in the field. This is the only thing that finds that out.
#
# The keypair is generated per run into a scratch directory and goes away with
# the container. **One** line is added to ~/.ssh/known_hosts and removed again,
# and that needs explaining because it is the only thing here that touches
# anything of yours.
#
# OpenSSH does not honour $HOME — it reads known_hosts from the passwd entry —
# so a throwaway HOME cannot redirect it. The alternative is passing
# `-o UserKnownHostsFile=...`, which CLV refuses everywhere by design and which
# a test asserts never appears in an argv. Working around that here would mean
# testing a configuration the product does not support, so instead this does
# what an operator does by hand: trusts the key, then untrusts it.
#
# The entry is keyed to an ephemeral localhost port that will not exist again,
# the file is backed up first, and the cleanup restores it and verifies the
# restore. Nothing else in ~/.ssh is read or written.
set -eu

PROFILE="${1:-alpine}"
[ "$#" -gt 0 ] && shift   # anything after the profile goes through to pytest
ENGINE="${CLV_CONTAINER_ENGINE:-podman}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PYTHON="${CLV_TEST_PYTHON:-python3}"

case "$PROFILE" in
  alpine) EXPECT_PROFILE="busybox" ;;
  gnu)    EXPECT_PROFILE="gnu" ;;
  *) echo "usage: $0 [alpine|gnu]" >&2; exit 2 ;;
esac

IMAGE="clv-itest-$PROFILE"
NAME="clv-itest-$PROFILE-$$"
WORK="$(mktemp -d)"

KNOWN_HOSTS="$HOME/.ssh/known_hosts"
BACKUP="$WORK/known_hosts.backup"

cleanup() {
    # Restore before anything else: an interrupted run must not leave a trusted
    # key behind for a port that something else may later occupy.
    if [ -f "$BACKUP" ]; then
        cp "$BACKUP" "$KNOWN_HOSTS"
        if cmp -s "$BACKUP" "$KNOWN_HOSTS"; then
            echo "==> restored $KNOWN_HOSTS"
        else
            echo "!! could not restore $KNOWN_HOSTS; a copy is at $BACKUP" >&2
        fi
    fi
    "$ENGINE" rm -f "$NAME" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

echo "==> generating a throwaway keypair in $WORK"
ssh-keygen -q -t ed25519 -N '' -C "clv-itest" -f "$WORK/id"
cp "$WORK/id.pub" "$HERE/authorized_keys"

echo "==> building $IMAGE"
"$ENGINE" build -q -t "$IMAGE" -f "$HERE/Containerfile.$PROFILE" "$HERE" >/dev/null
rm -f "$HERE/authorized_keys"

echo "==> starting $NAME"
"$ENGINE" run -d --name "$NAME" -P "$IMAGE" >/dev/null
PORT="$("$ENGINE" port "$NAME" 22/tcp | head -1 | sed 's/.*://')"
[ -n "$PORT" ] || { echo "could not find the mapped port" >&2; exit 1; }

echo "==> waiting for sshd on 127.0.0.1:$PORT"
i=0
while [ "$i" -lt 60 ]; do
    if ssh-keyscan -p "$PORT" 127.0.0.1 2>/dev/null | grep -q .; then break; fi
    i=$((i + 1)); sleep 1
done
[ "$i" -lt 60 ] || { echo "sshd never came up" >&2; "$ENGINE" logs "$NAME" >&2; exit 1; }

# Pin the host key rather than disabling the check. See the header: this is the
# one thing here that touches a file of yours, and it is undone on the way out.
ssh-keyscan -p "$PORT" 127.0.0.1 > "$WORK/scanned" 2>/dev/null
grep -q . "$WORK/scanned" || { echo "ssh-keyscan found no host key" >&2; exit 1; }

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
touch "$KNOWN_HOSTS"
cp "$KNOWN_HOSTS" "$BACKUP"
echo "==> trusting [127.0.0.1]:$PORT (backup at $BACKUP, restored on exit)"
cat "$WORK/scanned" >> "$KNOWN_HOSTS"

echo "==> running the suite (expecting the '$EXPECT_PROFILE' profile)"
cd "$REPO"
CLV_TEST_SSH_HOST=127.0.0.1 \
CLV_TEST_SSH_PORT="$PORT" \
CLV_TEST_SSH_USER=clvtest \
CLV_TEST_SSH_DIR=/srv/logs \
CLV_TEST_SSH_PROFILE="$EXPECT_PROFILE" \
CLV_TEST_SSH_IDENTITY="$WORK/id" \
CLV_TEST_SSH_KNOWN_HOSTS="$WORK/known_hosts" \
    "$PYTHON" -m pytest -m remote_integration -q "$@"
