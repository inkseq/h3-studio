"""Prepare reviewed additive policy documents offline; never adopt or rent."""

import argparse
import copy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINDINGS = ROOT / 'deploy/dstack/taomate-v7.production-bindings.json'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def prepare(execution_raw, operator_raw, bindings):
    sources = bindings['source_receipts']
    if (
        digest(execution_raw) != sources['production_execution_sha256']
        or digest(operator_raw) != sources['production_operator_sha256']
    ):
        raise ValueError('production_original_binding_changed')
    execution, operator = json.loads(execution_raw), json.loads(operator_raw)
    before = copy.deepcopy((execution, operator))
    if execution.get('schema_version') != 1 or operator.get('version') != 1:
        raise ValueError('production_configuration_version_invalid')
    keys = {
        (p['deployment_profile_id'], tuple(p['recipe_ids']))
        for p in execution['policies']
    }
    ids = {p['id'] for p in operator['profiles']}
    for policy in bindings['execution_policies']:
        if (policy['deployment_profile_id'], tuple(policy['recipe_ids'])) in keys:
            raise ValueError('production_binding_already_exists')
        execution['policies'].append(copy.deepcopy(policy))
    for profile in bindings['operator_profiles']:
        if profile['id'] in ids:
            raise ValueError('production_binding_already_exists')
        operator['profiles'].append(copy.deepcopy(profile))
    demand = operator['policy']['demand_start']['profile_ids']
    for identity in bindings['demand_profile_ids']:
        if identity in demand:
            raise ValueError('production_binding_already_exists')
        demand.append(identity)
    # Retain every old profile and every global limit. This preparer has no
    # database, credentials, provider client or runtime enablement switch.
    if (
        execution['policies'][: len(before[0]['policies'])] != before[0]['policies']
        or operator['profiles'][: len(before[1]['profiles'])] != before[1]['profiles']
    ):
        raise ValueError('production_original_rows_changed')
    return execution, operator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execution-file', required=True, type=Path)
    parser.add_argument('--operator-file', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        raise ValueError('fresh_output_directory_required')
    documents = prepare(
        args.execution_file.read_bytes(),
        args.operator_file.read_bytes(),
        json.loads(BINDINGS.read_text()),
    )
    args.output.mkdir(mode=0o700)
    hashes = {}
    for name, document in zip(
        ('execution-profiles.json', 'dstack-operator.json'), documents, strict=True
    ):
        raw = json.dumps(document, sort_keys=True, indent=2).encode() + b'\n'
        with (args.output / name).open('xb') as f:
            f.write(raw)
        hashes[name] = digest(raw)
    print(json.dumps({'state': 'prepared_not_adopted', 'sha256': hashes}))


if __name__ == '__main__':
    main()
