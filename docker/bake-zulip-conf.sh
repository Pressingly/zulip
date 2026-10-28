#!/bin/bash
# Applies the zulip.conf every bundle host runs at build time, so a boot
# whose configuration matches it skips Puppet (see docker/entrypoint.sh).
# Worker counts are pinned because Puppet would otherwise size them from
# the build host's memory.
set -euo pipefail

ZULIP_CONF=/etc/zulip/zulip.conf

crudini --set "$ZULIP_CONF" application_server http_only true
crudini --set "$ZULIP_CONF" application_server nginx_worker_processes 2
crudini --set "$ZULIP_CONF" application_server queue_workers_multiprocess false
crudini --set "$ZULIP_CONF" application_server uwsgi_processes 4
crudini --set "$ZULIP_CONF" application_server nameserver 127.0.0.11
crudini --set "$ZULIP_CONF" loadbalancer ips 172.16.0.0/12,192.168.0.0/16,10.0.0.0/8

/home/zulip/deployments/current/scripts/zulip-puppet-apply -f
cp "$ZULIP_CONF" /etc/zulip/zulip.conf.baked
cp "$ZULIP_CONF" /etc/zulip/zulip.conf.applied

# The migrations this image ships, so a boot can tell from django_migrations
# alone whether the shared database needs `migrate` (docker/entrypoint.sh).
mkdir -p /usr/local/share/zulip
su zulip -c "/home/zulip/deployments/current/manage.py shell -c '
from django.db.migrations.loader import MigrationLoader
nodes = MigrationLoader(None, ignore_no_migrations=True).graph.nodes
with open(\"/tmp/migration-manifest\", \"w\") as manifest:
    manifest.write(\"\".join(f\"{app}.{name}\\n\" for app, name in sorted(nodes)))
'"
LC_ALL=C sort /tmp/migration-manifest >/usr/local/share/zulip/migration-manifest
rm -f /tmp/migration-manifest
test -s /usr/local/share/zulip/migration-manifest
