#!/bin/sh
# Create the ONE shared Samba user from the generated password, then run smbd in the foreground.
set -e
: "${FILESHARE_PASSWORD:?FILESHARE_PASSWORD missing (run: make secrets)}"
id fileshare >/dev/null 2>&1 || useradd -M -s /usr/sbin/nologin fileshare
printf '%s\n%s\n' "$FILESHARE_PASSWORD" "$FILESHARE_PASSWORD" | smbpasswd -s -a fileshare >/dev/null
# S2: the agent's read-only account, only if its password was provided (the legacy stack runs without it).
if [ -n "${FILESHARE_AGENT_PASSWORD:-}" ]; then
  id agent-ro >/dev/null 2>&1 || useradd -M -s /usr/sbin/nologin agent-ro
  printf '%s
%s
' "$FILESHARE_AGENT_PASSWORD" "$FILESHARE_AGENT_PASSWORD" | smbpasswd -s -a agent-ro >/dev/null
fi
mkdir -p /srv/share/Quotes
chmod 0777 /srv/share/Quotes
exec smbd --foreground --no-process-group --debug-stdout
