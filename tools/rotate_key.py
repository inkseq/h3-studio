"""Rotate one provider key through the central AI Registry (#976).

Guided and step by step; the owner does every provider-console action:

    python tools/rotate_key.py --target NAME            # plan only, changes nothing
    python tools/rotate_key.py --target NAME --execute  # guided rotation

Steps for one target (one provider key):
  1. The owner creates a new key in the provider console and pastes it into a
     private drop file this tool prepares (`.local/secret-drop/<target>.env`).
  2. Import it into the central registry as a new dated profile
     (AI-Registry `tools/import_api_credentials.py`; the old profile is kept).
  3. Write it to the AWS parameter store entry the hosts read (SecureString,
     overwrite; the previous parameter version is reported for rollback).
  4. Install and restart on each configured host via SSM (a private host
     script), then run the private verify script.
  5. Delete the drop file, write a value-free registry inbox note, and remind
     the owner to revoke the old key in the provider console.

Which parameter, hosts and scripts belong to a key is private: it lives in a
local map file (default `.local/key-rotation.json`, never committed). See
`tools/key-rotation.example.json` for the shape. No key value is ever printed,
logged, passed as an argument or written outside the drop file and the
registry's encrypted vault. Production steps ask for the environment name.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAP = ROOT / ".local" / "key-rotation.json"
DROP = ROOT / ".local" / "secret-drop"
REGISTRY = Path(
    os.environ.get("AI_REGISTRY_ROOT", r"C:\Users\danmo\Desktop\AI-Registry")
)
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
FIELD = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
ENVIRONMENTS = ("staging", "production")


class RotationError(Exception):
    """A closed code; never carries a provider message or a key value."""


def load_map(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RotationError("rotation_map_unreadable") from None
    if type(value) is not dict or value.get("version") != 1:
        raise RotationError("rotation_map_invalid")
    return value


def target_spec(rotation_map, name):
    if not NAME.fullmatch(name or ""):
        raise RotationError("rotation_target_invalid")
    spec = rotation_map.get("targets", {}).get(name)
    parameter = (spec or {}).get("parameter") or {}
    hosts = (spec or {}).get("hosts") or {}
    if (
        type(spec) is not dict
        or not NAME.fullmatch(spec.get("service") or "")
        or not NAME.fullmatch(spec.get("profile_prefix") or "")
        or not FIELD.fullmatch(spec.get("env_field") or "")
        or not str(spec.get("console_url", "")).startswith("https://")
        or not isinstance(parameter.get("name"), str)
        or not parameter["name"].startswith("/")
        or not isinstance(parameter.get("region"), str)
        or parameter.get("json_field") is not None
        and not isinstance(parameter["json_field"], str)
        or type(hosts) is not dict
        or not set(hosts) <= set(ENVIRONMENTS)
        or any(
            type(h) is not dict
            or not re.fullmatch(r"i-[0-9a-f]{8,17}", str(h.get("instance_id")))
            or not isinstance(h.get("install"), str)
            or not isinstance(h.get("verify"), str)
            for h in hosts.values()
        )
    ):
        raise RotationError("rotation_target_invalid")
    return spec


def new_profile(spec, now):
    return spec["profile_prefix"] + "-" + now.strftime("%Y%m%d")


def plan(name, spec, now):
    environments = ", ".join(sorted(spec.get("hosts", {}))) or "none"
    return [
        f"Target {name}: service {spec['service']}, new profile {new_profile(spec, now)}",
        f"1. Owner: create a new key at {spec['console_url']} and paste it into "
        f"{DROP / (name + '.env')} as {spec['env_field']}=<value>",
        "2. Import into the central registry (old profile kept)",
        "3. Write the parameter store entry (previous version reported for rollback)",
        f"4. Install, restart and verify on: {environments}",
        "5. Delete the drop file, write a registry inbox note, revoke the old key",
    ]


def prepare_drop(name, spec, drop=DROP):
    drop.mkdir(parents=True, exist_ok=True)
    path = drop / (name + ".env")
    if not path.exists():
        path.write_text(spec["env_field"] + "=\n", encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
    return path


def drop_ready(path, field):
    """True when the owner has pasted a value; the value is not returned."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(
        line.startswith(field + "=") and len(line) > len(field) + 1 for line in lines
    )


def import_profile(spec, profile, drop_path, *, run=subprocess.run):
    command = [
        sys.executable,
        str(REGISTRY / "tools" / "import_api_credentials.py"),
        "--service",
        spec["service"],
        "--profile",
        profile,
        "--env-file",
        str(drop_path),
    ]
    if spec.get("base_url"):
        command += ["--base-url", spec["base_url"]]
    # The importer never prints values; its output is still not echoed here.
    result = run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RotationError("rotation_registry_import_failed")


def load_new_key(spec, profile, *, loader=None):
    if loader is None:
        sys.path.insert(0, str(REGISTRY))
        from api_registry import load_api as loader
    config = loader(spec["service"], profile=profile)
    value = (config.env or {}).get(spec["env_field"]) or config.api_key
    if not isinstance(value, str) or len(value) < 8:
        raise RotationError("rotation_registry_value_missing")
    return value


def write_parameter(ssm, spec, value):
    """Overwrite the SecureString; return the previous version for rollback."""
    parameter = spec["parameter"]
    previous, current = None, None
    try:
        found = ssm.get_parameter(Name=parameter["name"], WithDecryption=True)
        previous = found["Parameter"].get("Version")
        current = found["Parameter"].get("Value")
    except Exception as error:
        if type(error).__name__ != "ParameterNotFound":
            raise RotationError("rotation_parameter_read_failed") from None
    if parameter.get("json_field"):
        try:
            document = json.loads(current) if current else {}
        except ValueError:
            raise RotationError("rotation_parameter_not_json") from None
        if type(document) is not dict:
            raise RotationError("rotation_parameter_not_json")
        document[parameter["json_field"]] = value
        stored = json.dumps(document, separators=(",", ":"))
    else:
        stored = value
    try:
        ssm.put_parameter(
            Name=parameter["name"], Value=stored, Type="SecureString", Overwrite=True
        )
    except Exception:
        raise RotationError("rotation_parameter_write_failed") from None
    return previous


def host_script(script_path):
    """Read a real host step; empty SSM programs are not installation proof."""
    try:
        commands = Path(script_path).read_text(encoding="utf-8")
    except OSError:
        raise RotationError("rotation_host_script_unreadable") from None
    if not commands.strip():
        raise RotationError("rotation_host_script_empty")
    return commands


def run_host_script(ssm, host, script_path, *, sleep=time.sleep, polls=120):
    """Run one private host script through SSM; only its status is reported."""
    commands = host_script(script_path)
    try:
        sent = ssm.send_command(
            InstanceIds=[host["instance_id"]],
            DocumentName="AWS-RunShellScript",
            Comment="inkseq key rotation",
            Parameters={"commands": [commands], "executionTimeout": ["900"]},
        )
        command_id = sent["Command"]["CommandId"]
        for _ in range(polls):
            sleep(5)
            try:
                status = ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=host["instance_id"]
                )["Status"]
            except Exception as error:
                if type(error).__name__ == "InvocationDoesNotExist":
                    continue
                raise
            if status not in ("Pending", "InProgress", "Delayed"):
                return status
    except RotationError:
        raise
    except Exception:
        raise RotationError("rotation_host_command_failed") from None
    return "TimedOut"


def inbox_note(name, spec, profile, environments, now, *, registry=REGISTRY):
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    path = registry / "inbox" / f"inkseq-key-rotation-{name}-{stamp}.md"
    path.write_text(
        "\n".join(
            [
                f"- 来源项目与工作目录：inkseq h3-studio tools/rotate_key.py --target {name}",
                f"- 提交时间：{now.isoformat()}",
                "- 检查范围：一次密钥轮换（不含值）",
                "",
                f"### R001 {spec['service']} key rotation",
                "",
                "- **变更类型**：更新",
                f"- **中央 API service/profile（无值）**：{spec['service']} / {profile}"
                "（旧 profile 保留，待中央整合后停用）",
                f"- **中央加载切换状态**：已导入；已在 {', '.join(environments) or '无主机'} 安装并通过验证脚本",
                "- **冲突与待核实**：旧密钥需由所有者在服务商控制台撤销",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def destroy(path):
    try:
        size = path.stat().st_size
        with path.open("r+b") as stream:
            stream.write(b"\0" * size)
            stream.flush()
            os.fsync(stream.fileno())
        path.unlink()
    except FileNotFoundError:
        pass


def execute(
    name,
    spec,
    *,
    ask=input,
    say=print,
    run=subprocess.run,
    loader=None,
    ssm_factory=None,
    now=None,
    drop=DROP,
    registry=REGISTRY,
    sleep=time.sleep,
):
    now = now or datetime.now(timezone.utc)
    # Refuse missing steps before importing a key or replacing the parameter.
    # run_host_script reads again just before sending, so a later edit cannot
    # turn an empty program's SSM Success into verification.
    for host in spec.get("hosts", {}).values():
        host_script(host["install"])
        host_script(host["verify"])
    profile = new_profile(spec, now)
    path = prepare_drop(name, spec, drop)
    say(f"Step 1: create a new key at {spec['console_url']}")
    say(f"        paste it into {path} as {spec['env_field']}=<value>, then save")
    while not drop_ready(path, spec["env_field"]):
        if ask("Type 'done' when saved, or 'stop': ").strip().lower() == "stop":
            raise RotationError("rotation_stopped_by_owner")
    say(f"Step 2: importing into the central registry as {profile}")
    import_profile(spec, profile, path, run=run)
    value = load_new_key(spec, profile, loader=loader)
    if ssm_factory is None:
        import boto3

        def ssm_factory(region):
            return boto3.client("ssm", region_name=region)

    ssm = ssm_factory(spec["parameter"]["region"])
    hosts = spec.get("hosts", {})
    if "production" in hosts:
        if ask("Production is included. Type 'production' to continue: ") != (
            "production"
        ):
            raise RotationError("rotation_production_not_confirmed")
    say("Step 3: writing the parameter store entry")
    previous = write_parameter(ssm, spec, value)
    del value
    say(f"        done; previous parameter version for rollback: {previous}")
    for environment in ENVIRONMENTS:
        if environment not in hosts:
            continue
        host = hosts[environment]
        say(f"Step 4: {environment}: install and restart")
        status = run_host_script(ssm, host, host["install"], sleep=sleep)
        if status != "Success":
            raise RotationError(f"rotation_install_{environment}_{status.lower()}")
        status = run_host_script(ssm, host, host["verify"], sleep=sleep)
        if status != "Success":
            raise RotationError(f"rotation_verify_{environment}_{status.lower()}")
        say(f"        {environment}: verified")
    destroy(path)
    note = inbox_note(name, spec, profile, sorted(hosts), now, registry=registry)
    say(f"Step 5: drop file deleted; registry note {note.name}")
    say(f"        Now revoke the OLD key at {spec['console_url']}.")
    return {"profile": profile, "previous_parameter_version": previous}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Rotate one provider key")
    parser.add_argument("--target", required=True)
    parser.add_argument("--map", type=Path, default=DEFAULT_MAP)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        spec = target_spec(load_map(args.map), args.target)
        now = datetime.now(timezone.utc)
        if not args.execute:
            print("\n".join(plan(args.target, spec, now)))
            print("Plan only; nothing changed. Add --execute to rotate.")
            return 0
        execute(args.target, spec, now=now)
        return 0
    except RotationError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
