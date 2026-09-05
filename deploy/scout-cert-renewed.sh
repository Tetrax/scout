#!/bin/sh
# Certbot deploy hook: reload only after renewal of Scout's lineage.
set -eu
[ "${RENEWED_LINEAGE:-}" = /etc/letsencrypt/live/scout.valdev.me ] || exit 0
exec /usr/bin/flock -x /home/tetrax/workspace/.locks/valdev-infra.lock /bin/sh -c '/usr/sbin/nginx -t && /usr/bin/systemctl reload nginx'
