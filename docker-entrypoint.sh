#!/bin/sh
set -eu

# X11 expects this shared socket directory to be owned by root.
mkdir -p /tmp/.X11-unix
chown root:root /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix

for directory in /app/data /app/downloads /app/browser_data; do
    mkdir -p "$directory"
done

exec "$@"
