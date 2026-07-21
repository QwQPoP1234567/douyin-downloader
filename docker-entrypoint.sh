#!/bin/sh
set -eu

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
