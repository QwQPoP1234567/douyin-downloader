#!/bin/sh
set -eu

# X11 expects this shared socket directory to be owned by root.
mkdir -p /tmp/.X11-unix
chown root:root /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix

# `docker restart` reuses the same writable layer, so /tmp keeps the X lock file
# written by the previous run. The PID recorded inside it is almost always
# recycled by an unrelated process in the fresh PID namespace, which makes Xvfb
# abort with "Server is already active for display" and the container crash-loop
# forever. A container that just started cannot host a live X server, so any
# leftover lock is stale by definition.
# `rm -f` returns 0 for unmatched globs, so this stays safe under `set -e`.
rm -f /tmp/.X*-lock 2>/dev/null || true
rm -f /tmp/.X11-unix/X* 2>/dev/null || true

for directory in /app/data /app/downloads /app/browser_data; do
    mkdir -p "$directory"
done

exec "$@"
