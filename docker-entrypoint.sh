#!/bin/sh
set -eu

# Xvfb runs as the unprivileged application user, but X11 requires this
# shared socket directory to be created and owned by root first.
mkdir -p /tmp/.X11-unix
chown root:root /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix

for directory in /app/data /app/downloads /app/browser_data; do
    mkdir -p "$directory"
    chown douyin:douyin "$directory"
done

# Older images wrote creator folders as root. Directory ownership is enough for
# creating/replacing completed media; partial files also need append permission.
find /app/downloads -type d -exec chown douyin:douyin {} +
find /app/downloads -type f \( -name '*.part' -o -name '*.part.url' \) -exec chown douyin:douyin {} +

# Browser profiles contain lock files that Chromium must be able to replace.
chown -R douyin:douyin /app/browser_data

exec gosu douyin "$@"
