#!/bin/bash
# First boot only. No API/account/DB secrets and no application release here.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
umask 027
test "$(id -u)" = 0
. /etc/os-release
test "$ID:$VERSION_ID" = ubuntu:24.04
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates curl python3-boto3
install -m 0755 -d /etc/apt/keyrings
curl --fail --silent --show-error --proto '=https' --tlsv1.2 https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod 0644 /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update -qq
apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
install -d -m 0755 /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"},"live-restore":true}
EOF
systemctl enable --now docker
systemctl restart docker
install -d -o root -g root -m 0755 /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/sixnine-retention.conf <<'EOF'
[Journal]
MaxRetentionSec=7day
MaxFileSec=1day
SystemMaxUse=128M
RuntimeMaxUse=64M
EOF
systemctl restart systemd-journald
getent group deploy >/dev/null || groupadd --system deploy
id deploy >/dev/null 2>&1 || useradd --system --gid deploy --no-create-home --shell /usr/sbin/nologin deploy
# Deliberately do not grant Docker group or arbitrary sudo to deployment user.
install -d -o root -g root -m 0755 /srv/sixnine /srv/sixnine/releases /srv/sixnine/approved-releases /opt/sixnine-release
install -d -o root -g deploy -m 2770 /srv/sixnine/incoming
install -d -o 10001 -g 10001 -m 0700 /srv/sixnine/platform-data /srv/sixnine/upload-spool
install -d -o root -g root -m 0755 /srv/sixnine/frontend /srv/sixnine/frontend/assets /srv/sixnine/frontend/releases
install -d -o root -g root -m 0700 /srv/sixnine/approved-frontends
install -d -o 70 -g 70 -m 0700 /srv/sixnine/postgres
install -d -o root -g root -m 0700 /opt/sixnine-release/docker-config /run/sixnine-secrets
cat > /etc/tmpfiles.d/sixnine.conf <<'EOF'
d /run/sixnine-secrets 0700 root root -
EOF
# SSM is present in the selected Canonical image; do not open SSH as fallback.
systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service
systemctl disable --now ssh.service ssh.socket || true
python3 - <<'PY'
import json, pathlib, subprocess, time
versions = subprocess.check_output(['dpkg-query', '-W', '-f=${Package}=${Version}\n',
    'docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-compose-plugin', 'python3-boto3'], text=True)
record = {'state': 'base_host_ready', 'created_at': time.time(), 'packages': versions.splitlines(),
          'application_started': False, 'ssh_enabled': False, 'secrets_created': False}
pathlib.Path('/opt/sixnine-release/host-bootstrap.json').write_text(json.dumps(record, indent=2))
print('sixnine_base_host_ready')
PY
