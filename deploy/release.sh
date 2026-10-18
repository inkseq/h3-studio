#!/usr/bin/env bash
# CPU-only, explicit release; never provisions a GPU or transfers user media.
set -Eeuo pipefail
umask 077
release="${1:?Supply the full source commit SHA}"
[[ "$release" =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid release SHA'; exit 2; }
[[ "$EUID" == 0 ]] || { echo 'Run via sudo on the target Lightsail VM'; exit 2; }
base=/srv/h3-studio
source_dir="$base/incoming/$release"
[[ "$(realpath "$(dirname "${BASH_SOURCE[0]}")")" == "$source_dir" ]] || exit 2
test -f "$base/config/site.env"
test -d "$base/data" && test -d "$base/runtime"
exec 9>"$base/release.lock"
flock -n 9 || { echo 'Another deployment is running'; exit 2; }
cd "$source_dir"
sha256sum --check image.tar.gz.sha256
docker load -i image.tar.gz >/dev/null
image="h3-studio:$release"
[[ "$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" == "$release" ]] || exit 2
export H3_IMAGE="$image" H3_RELEASE="$release"
compose=(docker compose --env-file "$base/config/site.env" -f "$source_dir/compose.yaml")
"${compose[@]}" config --quiet
domain="$("${compose[@]}" config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["caddy"]["environment"]["H3_DOMAIN"])')"
[[ "$domain" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ ]] || { echo 'Invalid DNS hostname'; exit 2; }
# Fetch proxy before interrupting anything. No GPU image or provider credential.
"${compose[@]}" pull caddy
"${compose[@]}" run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
# Preflight is read-only: do not import server.py (its import migrates SQLite).
docker run --rm --network none --user 10001:10001 \
  --mount "type=bind,src=$base/data,dst=/data,readonly" "$image" python -c '
import pathlib,sqlite3,sys
p=pathlib.Path("/data/studio.sqlite3")
if not p.exists(): sys.exit("Provision both password accounts first; see docs/LIGHTSAIL.md")
c=sqlite3.connect("file:/data/studio.sqlite3?mode=ro",uri=True)
tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type=\"table\"")}
if "jobs" in tables and c.execute("SELECT COUNT(*) FROM jobs WHERE status IN (\"queued\",\"running\",\"cancel_requested\")").fetchone()[0]:
 sys.exit("Active jobs exist: drain and reconcile before deployment")
from password_auth import passwords_ready
if not passwords_ready(c): sys.exit("Provision both password accounts first; see docs/LIGHTSAIL.md")
'
previous=''
if [[ -L "$base/current" ]]; then previous="$(readlink -f "$base/current")"; fi
case "$previous" in ''|"$base/incoming/"*) ;; *) echo 'Unexpected current release target'; exit 2;; esac
# SQLite backup API is consistent even while the old CPU app is running.
# Back up before downtime so a full disk cannot stop a healthy site.
mkdir -p "$base/backups"
backup="$base/backups/before-$release-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
docker run --rm --network none --user 0:0 \
  --mount "type=bind,src=$base/data,dst=/data" \
  --mount "type=bind,src=$base/backups,dst=/backups" "$image" \
  python -c 'import sqlite3,sys; s=sqlite3.connect("/data/studio.sqlite3"); d=sqlite3.connect(sys.argv[1]); s.backup(d); assert d.execute("PRAGMA integrity_check").fetchone()[0]=="ok"; d.close(); s.close()' "/backups/$(basename "$backup")"
restore_previous() {
  trap - ERR
  "${compose[@]}" stop caddy app || true
  # Never restore a database over new data automatically. Only roll back code
  # when schema is unchanged; otherwise leave the proxy stopped for recovery.
  if [[ -n "$previous" ]] && docker run --rm --network none --user 0:0 \
      --mount "type=bind,src=$base/data,dst=/data,readonly" \
      --mount "type=bind,src=$base/backups,dst=/backups,readonly" "$image" \
      python -c 'import sqlite3,sys; q="SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"; a=sqlite3.connect("file:/data/studio.sqlite3?mode=ro",uri=True); b=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True); sys.exit(a.execute(q).fetchall()!=b.execute(q).fetchall())' "/backups/$(basename "$backup")"; then
    old_release="$(basename "$previous")"
    export H3_IMAGE="h3-studio:$old_release" H3_RELEASE="$old_release"
    docker compose --env-file "$base/config/site.env" -f "$previous/compose.yaml" up -d --wait --wait-timeout 90 app
    if python3 "$previous/check_release.py" "$old_release"; then
      docker compose --env-file "$base/config/site.env" -f "$previous/compose.yaml" up -d caddy
      echo 'Release failed; previous code restored with existing data'
    else echo 'Release failed; proxy remains stopped. Inspect app and backup.'; fi
  else echo 'Release failed; proxy remains stopped. Inspect schema and backup before manual recovery.'; fi
  exit 1
}
trap restore_previous ERR
# Stop the public proxy before the app; no external writes during the cutover.
"${compose[@]}" stop caddy app
"${compose[@]}" up -d --wait --wait-timeout 90 app
python3 "$source_dir/check_release.py" "$release"
"${compose[@]}" up -d caddy
python3 - "$domain" "$release" <<'PY'
import json,sys,time,urllib.request
for attempt in range(18):
    try:
        with urllib.request.urlopen('https://'+sys.argv[1]+'/healthz',timeout=5) as r:
            health=json.load(r)
        assert health['status']=='ok' and health['release']==sys.argv[2]
        assert health['authentication']=='password' and health['auth_ready']
        assert health['generation_enabled'] is False
        break
    except Exception:
        if attempt==17: raise SystemExit('Public HTTPS health check failed')
        time.sleep(5)
PY
ln -sfn "$source_dir" "$base/current"
trap - ERR
echo "Published CPU release $release; generation disabled"
