"""Create a finite public Modal image context; no SDK, cloud or image build.

Reuse the existing native-profile image recipe rather than resolving a second
environment at container startup. A receipt is not image qualification or spend
permission. Explicit reviewed adoption must build/pin the resulting image ID.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy.wangp.package_tool import PRIVATE_FILES
from studio_platform.runtime_hosts.wangp_environment import regular_file

EXTRA = (
    'studio_platform/modal_entry.py',
    'studio_platform/modal_runtime.py',
    'deploy/dstack/repair_decord_metadata.py',
    'deploy/wangp/requirements.ocr.txt',
    'deploy/wangp/bootstrap.py',
)


def build(output):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError('modal_sources_output_invalid')
    if any(
        parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction())
        for parent in output.parents
    ):
        raise ValueError('modal_sources_linked_parent')
    output.mkdir(mode=0o700)
    hashes = {}
    for name in sorted(set(PRIVATE_FILES) | set(EXTRA)):
        source = regular_file(ROOT, name)
        data = source.read_bytes()
        if len(data) > 4 * 1024 * 1024:
            raise ValueError('modal_sources_file_limit')
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    recipe = regular_file(ROOT, 'deploy/dstack/Dockerfile.wangp').read_text()
    # Preserve the exact native dependency/source checks, including OCR isolation.
    recipe += '\nCOPY studio_platform /opt/inkseq/studio_platform\nCOPY comfy_workflow.py /opt/inkseq/comfy_workflow.py\nCOPY deploy/wangp/profiles /opt/inkseq/deploy/wangp/profiles\nENV PYTHONPATH=/opt/inkseq\nWORKDIR /opt/inkseq\nENTRYPOINT []\nCMD ["/venv/main/bin/python", "-m", "studio_platform.modal_entry"]\n'
    (output / 'Dockerfile').write_bytes(recipe.encode())
    hashes['Dockerfile'] = hashlib.sha256(recipe.encode()).hexdigest()
    receipt = {
        'purpose': 'unqualified-modal-native-image-context',
        'source_sha256': hashes,
    }
    (output / 'receipt.json').write_text(
        json.dumps(receipt, sort_keys=True, indent=2), encoding='utf8'
    )
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    print(json.dumps(build(parser.parse_args().output)))
