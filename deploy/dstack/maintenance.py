#!/usr/bin/python3
"""Root-installed additive migration release helper; never execute incoming code.

Install as /opt/sixnine-release/migration_maintenance.py after independent review,
with the reviewed checker as migration_check.py. Only the CPU Compose services
are controlled here. Provider shutdown, rentals and business state are untouched.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile
import tempfile
import time

import release
try:
    import deployment_target
except ImportError:
    from deploy.platform import deployment_target

FILES = frozenset({"dispatch-image.tar.gz", "platform-overlay.yaml", "hatchet-entrypoint.sh",
    "source-set.tar.gz", "migration-check.py", "maintenance.py", "runtime-config.example.json",
    "server-config.example.yml", "broker.example.json"})
DSTACK_FILES = frozenset({"dstack-server-image.tar.gz", "dstack-native-provenance.json"})
# These are the two independently approved releases observed as current/previous
# before this helper upgrade. Only their original helper bytes may use the new
# installed checker for rollback; incoming artifact code is never executed.
LEGACY_HELPER_PAIRS = {commit: (
    "a8da20579bb9d776ad8337b2e5182dcb3dbaf6dfc0b533cd8958600a6a60b1b9",
    "eb6a7d8cab5cbd161b70643f6f0fa193d75f206862e6ac62c61905ce7353f3e3") for commit in (
        "4f093976bed7cd89696127981cf22835a82d6d23",
        "07da40322559ee5a12829be6884c541c401d852a")}
# Exact retained current/previous helper bytes before the staging upgrade.
RETAINED_DSTACK_HELPER_PAIRS = {
    # #684 SSM3dd45659: exact staging current/previous pair before Inky adoption.
    '0c1dfeceb015f34f2593783953ef00f5aea00468': ('135cbf73dfd467ddd870197d650ff66514e17b0808f403623e26ac01a7bffbef', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    'ff30d5aa9ceafc8693cf8a64fae92a8f8e35624a': ('135cbf73dfd467ddd870197d650ff66514e17b0808f403623e26ac01a7bffbef', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    # Current/rollback readback5be9e18b; no generic compatibility fallback.
    '6606164343b612d3c99acd6f688ad54dad0d49af': ('bb8fc2b756ed4b04f77197c3e53eb16360223ef5465e031f76cdbab68c600416', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    'e63e64f093bc2e343b8a4128e62de9cf68ae964f': ('bb8fc2b756ed4b04f77197c3e53eb16360223ef5465e031f76cdbab68c600416', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    # Exact main source pairs for a474 staging and queued backup-fix6b2; host CAS still required.
    'a47462f08257af8682a4b72e194028efa1597054': ('bb8fc2b756ed4b04f77197c3e53eb16360223ef5465e031f76cdbab68c600416', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    '6b2dae1d1ca7c9298c05e3e32bfb0799af953b85': ('bb8fc2b756ed4b04f77197c3e53eb16360223ef5465e031f76cdbab68c600416', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    # #524 host readback a8c7478c: exact staging/production current and rollback.
    # ce49754 is the queued main candidate, not a runtime acceptance claim.
    '7beb62ac0799c96b965f52dea941a720be79b6c5': ('bb8fc2b756ed4b04f77197c3e53eb16360223ef5465e031f76cdbab68c600416', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    '55cb0ff2c979ffec56b968eda0f99b23250f3ead': ('bb8fc2b756ed4b04f77197c3e53eb16360223ef5465e031f76cdbab68c600416', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    'fd2ae3af0922395639f26a4718b1b5f33994b652': ('bb8fc2b756ed4b04f77197c3e53eb16360223ef5465e031f76cdbab68c600416', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    'b78510e6991f5acccb0996981e8016be5434693f': ('bb8fc2b756ed4b04f77197c3e53eb16360223ef5465e031f76cdbab68c600416', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    'ce49754dbcacc162bcd83a46cd3cd29028b549f4': ('bb8fc2b756ed4b04f77197c3e53eb16360223ef5465e031f76cdbab68c600416', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    'feeb1c1e8c34921546908f7a9f6a9dd67d5c0f4d': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    '9f8736ba036108dd078c983c3301fc8c3996a41e': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    '1c659727d5490cba1480979f95f3be7d0227815d': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    '5d9780430ae4edaf72d3ddd1613d8e440648b051': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    # Exact original 7a candidate being reconciled through its normal deployer.
    '7a24b5a99df4546f775224b83a890aeb53897734': ('794921cab61dd3455aeb6cddf1ab1ec636d6b1251e5568946f934a275ce6dcf5', 'cfb43bbee56a159f9fe17cd8f48bdc6557e36ae0afa40d553e34a1cd8c56ab1b'),
    # Exact source of the queued #516 platform; retained runtime must still match.
    '20ab3dfd308800daad10a3ce8ac060da342b164a': ('794921cab61dd3455aeb6cddf1ab1ec636d6b1251e5568946f934a275ce6dcf5', 'cfb43bbee56a159f9fe17cd8f48bdc6557e36ae0afa40d553e34a1cd8c56ab1b'),
    # Exact B1 staging current/rollback sources before compact-cap validation changed.
    '5c01b159781729b6c34379b4f4383b88364426fb': ('794921cab61dd3455aeb6cddf1ab1ec636d6b1251e5568946f934a275ce6dcf5', 'cfb43bbee56a159f9fe17cd8f48bdc6557e36ae0afa40d553e34a1cd8c56ab1b'),
    '32d1ec4f7e40adca65b2563b5ea11810dd223039': ('794921cab61dd3455aeb6cddf1ab1ec636d6b1251e5568946f934a275ce6dcf5', 'cfb43bbee56a159f9fe17cd8f48bdc6557e36ae0afa40d553e34a1cd8c56ab1b'),
    # Observed staging A1 current/previous, SSM 74cb6214 (2026-10-24).
    'fcd3ce618dbbf2a4f0dad4ee09e0c74121c4f69c': ('49905c8b60ae0375ec02485028db8fa4f7cabd718a91a936649dbf301fb5d86d', 'ddd25ad1c878e5066245bc32bb660e97908a223bb41c6d2bb38de1e433ddeafe'),
    'a978e9ceb7e78c9a9b54f9365d1e6c368aea83ea': ('49905c8b60ae0375ec02485028db8fa4f7cabd718a91a936649dbf301fb5d86d', 'ddd25ad1c878e5066245bc32bb660e97908a223bb41c6d2bb38de1e433ddeafe'),
    # Later exact-source pair: aedd staged by original SSM 3b9b8e43;
    # 6cce is a planned retained source, not a claim of runtime acceptance.
    'aedd790ef843b5a2284c9885bfaaa97635e217e1': ('49905c8b60ae0375ec02485028db8fa4f7cabd718a91a936649dbf301fb5d86d', 'ddd25ad1c878e5066245bc32bb660e97908a223bb41c6d2bb38de1e433ddeafe'),
    '6cce36dc33176c9845c5b2095d9062374c654a47': ('49905c8b60ae0375ec02485028db8fa4f7cabd718a91a936649dbf301fb5d86d', 'ddd25ad1c878e5066245bc32bb660e97908a223bb41c6d2bb38de1e433ddeafe'),
    # Published platform candidate; runtime still validates its actual retained manifest/pair.
    '38dbd871b4bcb775af9b181c6d80bf8250247f7b': ('49905c8b60ae0375ec02485028db8fa4f7cabd718a91a936649dbf301fb5d86d', 'ddd25ad1c878e5066245bc32bb660e97908a223bb41c6d2bb38de1e433ddeafe'),
    # Exact retained staging reader/current pair before DBOS writer adoption.
    'baa0e351fcd0a3f7e4f3678dd1282a38ef0f6a96': ('3bf05a2128bd80b809c7ab0577a4ce49903890a5c58f7d0a49b6ea19ce0ebb69', '860d696caca2f6162f66ef194c915f05d4ff31f8293aedff35b039b441d317e0'),
    '6ccf461c178f9c3f9a4013c041844174687dbef4': ('3bf05a2128bd80b809c7ab0577a4ce49903890a5c58f7d0a49b6ea19ce0ebb69', '860d696caca2f6162f66ef194c915f05d4ff31f8293aedff35b039b441d317e0'),
    "46130d852459f12d81d09c7e23178a8544d1d4d4": ("2e52c10800c619de4b62ea4909e6865a34013886179bff30ea8af1ea79baf917", "03e056e4b56b471d2feb2709725c7974c1c301b929fa4b342f1a33e1a1e5ba9a"),
    "02681cacaf98822b5072a028fa9b313976d1c0ca": ("2e52c10800c619de4b62ea4909e6865a34013886179bff30ea8af1ea79baf917", "03e056e4b56b471d2feb2709725c7974c1c301b929fa4b342f1a33e1a1e5ba9a"),
    'b5902a1cf9bf2b32fe2de7ea35ca1cc7bbde3524': ('a4d0c782ab523b30c7f4db0b958b785480e17bb5ac0578edb93c47055d80c080', '0d8db5801395b3ecbf3ceea9109c187fb9e39f1de08473694c9ed45df0a23534'),
    'c848dde8bf1d49044743ae1f497a148e50b84409': ('9e324d663ec9fde10ee7fb9ad58bbca01203f64720de9e809b36a856fe8c1bb8', 'ff1efe4f384fd68c65067239fc8fdca96d1528f3c84aff495f3f18a2f9fa7c4a'),
    '6e6fe5ee14e82983161f7f2bf15cbfd6e8eb54f2': ('9e324d663ec9fde10ee7fb9ad58bbca01203f64720de9e809b36a856fe8c1bb8', '3b885cdbb50e6cd373daf3952f0844a0be71e3010646d699cff8bb578a972c03'),
    # Original staging publication and its observed current/previous rollback pair.
    '842aab11c9068281c39c17a999f25191d4a181e6': ('c6336387d0b17138ba5ea4e02ab200c87128cf054595bae50c85ad3b27763fc6', 'fba8d75a6845fe2fe7c78ea120747797b2f57ec80e0c6bc4861a0f6bfda54790'),
    '935bd72d03ed46e2599d75ab6ddbc7923a86c111': ('710f94d4311fd86f52e49b376b2c6c179d58860ae8ea3f7f72b95ceba25bae44', '82721d55e00ad3226b1a0c617ce4a143c48d7145427f7a45cd049f584c27c3aa'),
    '751378aad82e1a6d045e638090466b99d76c2015': ('710f94d4311fd86f52e49b376b2c6c179d58860ae8ea3f7f72b95ceba25bae44', '82721d55e00ad3226b1a0c617ce4a143c48d7145427f7a45cd049f584c27c3aa'),
    # Retained production rollback pair and the two accepted promotion platforms.
    '498e073113564881a621189c8bb312315d2f4d9b': ('710f94d4311fd86f52e49b376b2c6c179d58860ae8ea3f7f72b95ceba25bae44', '82721d55e00ad3226b1a0c617ce4a143c48d7145427f7a45cd049f584c27c3aa'),
    '5165403eb95966962f659168a50ad239d595c360': ('710f94d4311fd86f52e49b376b2c6c179d58860ae8ea3f7f72b95ceba25bae44', '82721d55e00ad3226b1a0c617ce4a143c48d7145427f7a45cd049f584c27c3aa'),
    'e3a852fe013300dcb468d6f5a9875e183e1109be': ('588fe9b1e68119428a8cc5aa81aa043efb8f40cae6917eb13f873d74d2f0e1a1', 'fba8d75a6845fe2fe7c78ea120747797b2f57ec80e0c6bc4861a0f6bfda54790'),
    '57043af14df1e1db3f1711a33ed172f762fb022d': ('588fe9b1e68119428a8cc5aa81aa043efb8f40cae6917eb13f873d74d2f0e1a1', 'fba8d75a6845fe2fe7c78ea120747797b2f57ec80e0c6bc4861a0f6bfda54790'),
    '3d6355caafdbb8dca6648cdd9b3a04fd9b6dafe8': ('588fe9b1e68119428a8cc5aa81aa043efb8f40cae6917eb13f873d74d2f0e1a1', 'fba8d75a6845fe2fe7c78ea120747797b2f57ec80e0c6bc4861a0f6bfda54790'),
    # #501 R/G and prior staging source pairs before optional Sentry adoption.
    # The approved host packet still verifies actual retained manifest/bytes.
    '5676bd46e8b31dac36fb5b9758de4763114b84ca': ('719d8d3f83fdcd687d06d343f4076db117c675de607f2e46e61d2b65da5db8b3', 'fba8d75a6845fe2fe7c78ea120747797b2f57ec80e0c6bc4861a0f6bfda54790'),
    '59d2ebefba0e98d677907c30b65144ac47681762': ('719d8d3f83fdcd687d06d343f4076db117c675de607f2e46e61d2b65da5db8b3', 'fba8d75a6845fe2fe7c78ea120747797b2f57ec80e0c6bc4861a0f6bfda54790'),
    'f7b1db096099d0b3ad09736a95afffa57f9626c7': ('719d8d3f83fdcd687d06d343f4076db117c675de607f2e46e61d2b65da5db8b3', 'fba8d75a6845fe2fe7c78ea120747797b2f57ec80e0c6bc4861a0f6bfda54790'),
    # #193 helper compatibility (Small): staging current 808e/rollback d67a and
    # production current c08e carry the pre-#540 pair; f6 is the held #540 candidate.
    '808e46744ac31a4e0cb57ae1307d4cb6bbd8b98b': ('d0d8eeff6a9d1d52d6357806382b17f7c3a6f0158776e7d4941ca1d30543c8eb', 'cfb43bbee56a159f9fe17cd8f48bdc6557e36ae0afa40d553e34a1cd8c56ab1b'),
    'd67a76b70777874e74623f480d7ffbea9e3d3b88': ('d0d8eeff6a9d1d52d6357806382b17f7c3a6f0158776e7d4941ca1d30543c8eb', 'cfb43bbee56a159f9fe17cd8f48bdc6557e36ae0afa40d553e34a1cd8c56ab1b'),
    'c08ea6d4595763195a3c9e15d428f93cf90e16c0': ('d0d8eeff6a9d1d52d6357806382b17f7c3a6f0158776e7d4941ca1d30543c8eb', 'cfb43bbee56a159f9fe17cd8f48bdc6557e36ae0afa40d553e34a1cd8c56ab1b'),
    'f6c1972b904d6e0a4b9acccc405efb0d706c239e': ('dde95072245c0d75714cfbc888e679d294d24cbd45952240e8382298fbee70c6', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    # C0: observed production/staging current, rollback and pending (SSM20664d1c);
    # plus exact queued main candidates. These entries do not claim acceptance.
    'd737c8a36d4ab5f892189972487a20e53f68fd2d': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    '72aa9e8c68dbb9c7a1e346a4f60d72053b1dfd02': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    'e58fa13a42e0fed4018cbd0c62c5561c0e52cab1': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    '8e504f2a4d63959bd260eecbd4c79c58f01054b1': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    '8b7ee574c0f87e26d15c351036452fb503ef94e2': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    '6e3b0fec2b74b300e45100574de935642b52a4fc': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    '6241f0c9f3a4e3c91e4338d3c0f00981a533ab95': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    # Exact main integrations through the R2 revert; no acceptance inferred.
    '448556894ccf44d9eb643900f9f6bc66626245ef': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    'e9ddebb0b4315948b9629b90d3ebed82559fafc5': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    '7f3312d53e49b5882a2157f36ed4c22995ce1c6f': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    '96a56a83c7ed779afdaa8ed59d599f94693af386': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    '7a76ebc22b2129bcca520dd127721d1a19a819fc': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    'e2980bad91ddf8c419f86f388eb976daaaa6de92': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    '06e38846ffd03b8729df1e3d64f27dea4228c401': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
    '8b7a746c88dfb90bbfd01841044cd97ce3b3df58': ('0519fa5db6b7a4ad822fbf34e021e9028cec05a4dbb9da490935ae4e900dd366', '6f869921a57697f3e337ba80ac3e7fec1e22f76b8b096561285830cee7d19b4a'),
}
LEGACY_HELPER_PAIRS.update(RETAINED_DSTACK_HELPER_PAIRS)
# Closed stock tuples: current build and the exact retained rollback only.
DSTACK_STOCK = {'0.22.3': ('009e2916e335d4c9e02d961bc68f704951f5db20', 'dstackai/dstack:0.22.3@sha256:ce0e567675360ba91a6867a0399dccc1682dc2169075c6210d2ec873146f1ad8', '731ee908299af3f4592b3bfd1542e8f00fa59bb3fc24506a9f32d73a5fc274ba'), '0.22.5': ('e59a6caa6d3d76f8b8c3430b7e8eeb7271138065', 'dstackai/dstack:0.22.5@sha256:8dc2c8a5ee348789b1ed1621bc75b31c156adf31d849fded29508cfc21b279e4', 'b12e1157da5a13102899132e41c4a7950b3a2dfad4a0a5b52960e1cd86078c5e')}
DSTACK_SHARED_SOURCE_HASHES = {'dstack/_internal/core/backends/vastai/api_client.py': '9a8a68d3bd00b995901af7a66b58511ff4451be8122135e6f7b1a534d4e3c685', 'dstack/_internal/core/backends/vastai/compute.py': '3d6466c87ad37ce3125d54feadff54287f82131425e80bbc57ccf0bec8acdea9', 'gpuhunt/providers/vastai.py': 'f3cefc3a20f6f05d2cce911cd8e61ff980c6af2a42ed16e4a8f290bc0e5f76db', 'dstack/_internal/server/background/pipeline_tasks/jobs_submitted.py': 'a68386172ebdf5fe28bd08f61e18ecdfd6a4a1310e9782c8147ce3a96e8eeaac', 'dstack/_internal/server/background/scheduled_tasks/offers_catalog.py': '158bc7bee1a5cadc4d7a1efa2512701edda39707ae5dda33b8cd71bcd342d092'}
DSTACK_RUNPOD_SOURCE_HASHES = {'dstack/_internal/core/backends/runpod/api_client.py': 'ba0d804987d70242ecedca00938215e7d23474d1874da1b828827f65d5058a6d', 'dstack/_internal/core/backends/runpod/compute.py': '8b0ac5dc44c052bbb83a20fe9d394b8e0b61a1634eb8352786cfb26eb4a130c4'}
DSTACK_BASE_IMAGE = DSTACK_STOCK["0.22.5"][1]
LEGACY_DSTACK_BASE_IMAGE = DSTACK_STOCK["0.22.3"][1]
DSTACK_SOURCE_PATHS = frozenset({*DSTACK_SHARED_SOURCE_HASHES, "dstack/_internal/server/services/backends/__init__.py"})
DSTACK_IMAGE_CHECK = r'''import hashlib,importlib.metadata,json
from pathlib import Path
root=Path('/root/.local/share/uv/tools/dstack/lib/python3.11/site-packages')
folder=Path('/opt/sixnine/native-vast')
stock={'0.22.3': ('009e2916e335d4c9e02d961bc68f704951f5db20', 'dstackai/dstack:0.22.3@sha256:ce0e567675360ba91a6867a0399dccc1682dc2169075c6210d2ec873146f1ad8', '731ee908299af3f4592b3bfd1542e8f00fa59bb3fc24506a9f32d73a5fc274ba'), '0.22.5': ('e59a6caa6d3d76f8b8c3430b7e8eeb7271138065', 'dstackai/dstack:0.22.5@sha256:8dc2c8a5ee348789b1ed1621bc75b31c156adf31d849fded29508cfc21b279e4', 'b12e1157da5a13102899132e41c4a7950b3a2dfad4a0a5b52960e1cd86078c5e')}
shared={'dstack/_internal/core/backends/vastai/api_client.py': '9a8a68d3bd00b995901af7a66b58511ff4451be8122135e6f7b1a534d4e3c685', 'dstack/_internal/core/backends/vastai/compute.py': '3d6466c87ad37ce3125d54feadff54287f82131425e80bbc57ccf0bec8acdea9', 'gpuhunt/providers/vastai.py': 'f3cefc3a20f6f05d2cce911cd8e61ff980c6af2a42ed16e4a8f290bc0e5f76db', 'dstack/_internal/server/background/pipeline_tasks/jobs_submitted.py': 'a68386172ebdf5fe28bd08f61e18ecdfd6a4a1310e9782c8147ce3a96e8eeaac', 'dstack/_internal/server/background/scheduled_tasks/offers_catalog.py': '158bc7bee1a5cadc4d7a1efa2512701edda39707ae5dda33b8cd71bcd342d092'}
value=json.loads((folder/'installed-provenance.json').read_text())
assert set(value)=={'patched_sources','dstack_version','upstream_commit','original_sources','helper_sha256','sources'}
original=json.loads((folder/'native-vast-provenance.json').read_text())
assert set(original)=={'version','dstack_version','dstack_upstream_commit','base_image','sources'} and type(original['version']) is int and original['version'] in {1,2}
selected=stock[original['dstack_version']]
assert value['dstack_version']==original['dstack_version']==importlib.metadata.version('dstack')
assert value['upstream_commit']==original['dstack_upstream_commit']==selected[0] and original['base_image']==selected[1]
expected={**shared,'dstack/_internal/server/services/backends/__init__.py':selected[2]}
if original['version']==2:
 assert original['dstack_version']=='0.22.5'
 expected.update({'dstack/_internal/core/backends/runpod/api_client.py': 'ba0d804987d70242ecedca00938215e7d23474d1874da1b828827f65d5058a6d', 'dstack/_internal/core/backends/runpod/compute.py': '8b0ac5dc44c052bbb83a20fe9d394b8e0b61a1634eb8352786cfb26eb4a130c4'})
assert value['patched_sources']==len(expected)
assert original['sources']=={name:{'sha256':digest} for name,digest in expected.items()}
assert value['original_sources']==expected and set(value['sources'])==set(expected)
assert all(hashlib.sha256((root/name).read_bytes()).hexdigest()==digest for name,digest in value['sources'].items())
helper=hashlib.sha256((root/'dstack/_internal/core/backends/vastai/sixnine_safety.py').read_bytes()).hexdigest()
assert helper==value['helper_sha256']==hashlib.sha256((folder/'native_vast_safety.py').read_bytes()).hexdigest()
print(json.dumps({'installed':value,'helper_sha256':helper,
 'installer_sha256':hashlib.sha256((folder/'patch_native_vast.py').read_bytes()).hexdigest(),
 'source_provenance_sha256':hashlib.sha256((folder/'native-vast-provenance.json').read_bytes()).hexdigest()},sort_keys=True))
'''
MANIFEST = "migration-manifest.json"
SHARED = ("dstack", "hatchet", "dispatch", "controller")
PATH_FIELDS = frozenset({"SIXNINE_EXECUTION_PROFILES_SECRET_FILE", "SIXNINE_GRAFANA_CONFIG_FILE",
    "SIXNINE_DSTACK_OPERATOR_CONFIG_FILE", "SIXNINE_DSTACK_RUNTIME_CONFIG_FILE",
    "SIXNINE_DSTACK_TOKEN_FILE", "SIXNINE_DSTACK_DSN_FILE", "SIXNINE_DSTACK_SSH_PUBLIC_KEY_FILE",
    "SIXNINE_GPU_SSH_KEY_FILE", "SIXNINE_HATCHET_BROKER_CONFIG_FILE", "SIXNINE_HATCHET_TOKEN_FILE",
    "SIXNINE_HATCHET_SECRET_DIR", "SIXNINE_DSTACK_STATE_DIR", "SIXNINE_DSTACK_SERVER_CONFIG_FILE",
    "SIXNINE_DSTACK_RUNTIME_DIR"})
OTHER_FIELDS = frozenset({"SIXNINE_OPERATOR_CAPACITY_OWNERS", "SIXNINE_HATCHET_POSTGRES_VOLUME",
    "SIXNINE_HATCHET_CONFIG_VOLUME", "SIXNINE_MIGRATION_GENERATION_ENABLED",
    "SIXNINE_MIGRATION_EXECUTION_BACKEND", "SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID"})
OPTIONAL_PATH_FIELDS = frozenset({"SIXNINE_STAGE_QUERY_CONFIG_FILE", "SIXNINE_STORAGE_CREDENTIAL_FILE", "INKSEQ_WORKOS_CONFIG_FILE", "INKSEQ_STRIPE_CONFIG_FILE", "INKSEQ_INKY_CONFIG_FILE", "INKSEQ_LANGFUSE_CONFIG_FILE"})
STORAGE_FIELDS = frozenset({"SIXNINE_STORAGE_PROVIDER", "SIXNINE_STORAGE_ENDPOINT", "SIXNINE_STORAGE_REGION",
    "SIXNINE_STORAGE_BUCKET", "SIXNINE_STORAGE_PROFILE", "SIXNINE_STORAGE_CREDENTIAL_FILE"})
OPTIONAL_FIELDS = frozenset({"SIXNINE_OPERATOR_ADMIN_OWNERS", "SIXNINE_STORYBOARD_OWNERS", 'INKSEQ_DEPLOYMENT_TARGET',
    'INKSEQ_RESOURCE_PROFILE', 'INKSEQ_CPU_CGROUP_PARENT', 'INKSEQ_GENERATION_QUEUE',
    'INKSEQ_CAPACITY_NO_PROGRESS_SECONDS', 'INKSEQ_CAPACITY_RUNTIME_FLOOR_SECONDS',
    'INKSEQ_CAPACITY_STARTUP_TIMEOUT_SECONDS',
    'INKSEQ_FILM_ENABLED', 'INKSEQ_STRIPE_MODE', 'INKSEQ_SENTRY_DSN'}) | OPTIONAL_PATH_FIELDS | STORAGE_FIELDS
LEGACY_MARKERS = ("operator-capacity/active.json", "gpu-acceptance/active.json", "gpu-scaler/active.json")
STORYBOARD_FLAG = "SIXNINE_STORYBOARD_OWNERS"
STORYBOARD_RECEIPT = "storyboard-maintenance.json"
STORYBOARD_SERVICES = ("app", *SHARED, "hatchet-db", "db")
DEFAULT_PROFILE_FLAG = "SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID"
DEFAULT_PROFILE_RECEIPT = "default-profile-maintenance.json"


def atomic(path, document):
    """Protected local release metadata only, never a task/lease authority."""
    path = Path(path)
    if path.exists() or path.is_symlink():
        release.regular(path, root_owned=True, maximum=262144)
    descriptor, temporary = tempfile.mkstemp(prefix=".migration-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(document, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        release.sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def protected_directory(path):
    info = Path(path).lstat()
    release.require(stat.S_ISDIR(info.st_mode) and (os.name == "nt" or
        info.st_uid == 0 and not info.st_mode & 0o022), "migration_directory_not_root_controlled")


def approved(root, directory, commit):
    approval = root / "approved-migrations" / (commit + ".sha256")
    protected_directory(approval.parent)
    release.regular(approval, root_owned=True, maximum=128)
    digest = approval.read_text(encoding="ascii").strip()
    release.regular(directory / MANIFEST, maximum=32768)
    release.require(release.DIGEST.fullmatch(digest) and digest == release.checksum(directory / MANIFEST),
                    "migration_independent_approval_missing")


def notify_layout(value):
    return value.get("version") == 2 and value.get("layout") == "postgres-notify-v1"


def artifact_files(value):
    files = FILES - {"hatchet-entrypoint.sh", "broker.example.json"} if notify_layout(value) else FILES
    return files | DSTACK_FILES if "dstack" in value else files


def shared_services(value):
    return ("dstack", "controller") if notify_layout(value) else SHARED


def directory_manifest(directory):
    return json.loads((Path(directory) / "artifact" / MANIFEST).read_text())


def file_limit(name):
    return 2 * 1024**3 if name in {"dispatch-image.tar.gz", "dstack-server-image.tar.gz"} else 64 * 1024**2 if name == "source-set.tar.gz" else 1024**2


def dstack_provenance(directory, value):
    proof = json.loads((Path(directory) / "dstack-native-provenance.json").read_text())
    fields = {"version", "commit", "base_image", "installed", "helper_sha256", "installer_sha256", "source_provenance_sha256"}
    release.require(type(proof) is dict and set(proof) == fields and proof["version"] == 1
        and proof["commit"] == value["commit"] and proof["base_image"] in {item[1] for item in DSTACK_STOCK.values()}
        and all(type(proof[k]) is str and release.DIGEST.fullmatch(proof[k])
            for k in ("helper_sha256", "installer_sha256", "source_provenance_sha256")),
        "migration_dstack_provenance_invalid")
    installed = proof["installed"]
    selected = next(item for item in DSTACK_STOCK.values() if item[1] == proof["base_image"])
    version = next(version for version, item in DSTACK_STOCK.items() if item == selected)
    expected_sources = {**DSTACK_SHARED_SOURCE_HASHES,
        "dstack/_internal/server/services/backends/__init__.py": selected[2]}
    if type(installed) is dict and installed.get("patched_sources") == 8 and version == "0.22.5":
        expected_sources.update(DSTACK_RUNPOD_SOURCE_HASHES)
    release.require(type(installed) is dict and set(installed) == {"patched_sources", "dstack_version", "upstream_commit", "original_sources", "helper_sha256", "sources"}
        and installed["patched_sources"] == len(expected_sources) and installed["helper_sha256"] == proof["helper_sha256"]
        and installed["dstack_version"] == version and installed["upstream_commit"] == selected[0]
        and installed["original_sources"] == expected_sources
        and type(installed["sources"]) is dict and set(installed["sources"]) == set(expected_sources)
        and all(type(digest) is str and release.DIGEST.fullmatch(digest) for digest in installed["sources"].values()),
        "migration_dstack_provenance_invalid")
    return {k: proof[k] for k in ("installed", "helper_sha256", "installer_sha256", "source_provenance_sha256")}


def manifest(directory, commit, *, platform_manifest_sha256=None):
    directory = Path(directory)
    release.require(release.SHA.fullmatch(commit) and directory.is_dir() and not directory.is_symlink(),
                    "migration_artifact_invalid")
    release.regular(directory / MANIFEST, maximum=32768)
    value = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    fields = {"version", "commit", "platform_manifest_sha256", "dispatch", "source_index_sha256", "files"}
    if type(value) is dict and value.get("version") == 2:
        fields |= {"layout", "dstack"}
        release.require(notify_layout(value), "migration_layout_invalid")
    release.require(type(value) is dict and fields <= set(value) <= fields | {"native_source_commits", "dstack"}
        and type(value["version"]) is int and value["version"] in (1, 2) and value["commit"] == commit
        and type(value.get("platform_manifest_sha256")) is str and release.DIGEST.fullmatch(value["platform_manifest_sha256"])
        and type(value.get("source_index_sha256")) is str and release.DIGEST.fullmatch(value["source_index_sha256"])
        and type(value["files"]) is dict and set(value["files"]) == artifact_files(value), "migration_manifest_invalid")
    release.require({p.name for p in directory.iterdir()} == artifact_files(value) | {MANIFEST}, "migration_artifact_files_invalid")
    if "native_source_commits" in value:
        source_commits = value["native_source_commits"]
        release.require(type(source_commits) is dict and 1 <= len(source_commits) <= 128
            and all(type(digest) is str and release.DIGEST.fullmatch(digest)
                and type(source) is str and release.SHA.fullmatch(source)
                for digest, source in source_commits.items()), "migration_source_commits_invalid")
    if platform_manifest_sha256 is not None:
        release.require(value["platform_manifest_sha256"] == platform_manifest_sha256,
                        "migration_platform_binding_changed")
    dispatch = value["dispatch"]
    release.require(type(dispatch) is dict and set(dispatch) == {"image", "image_id"}
        and dispatch["image"] == "sixnine-dispatch:" + commit and type(dispatch["image_id"]) is str
        and re.fullmatch(r"sha256:[0-9a-f]{64}", dispatch["image_id"]), "migration_dispatch_binding_invalid")
    if "dstack" in value:
        server = value["dstack"]
        release.require(type(server) is dict and set(server) == {"image", "image_id", "provenance_sha256"}
            and server["image"] == "sixnine-dstack:" + commit
            and type(server["image_id"]) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", server["image_id"])
            and server["provenance_sha256"] == value["files"]["dstack-native-provenance.json"],
            "migration_dstack_binding_invalid")
    for name, digest in value["files"].items():
        release.require(type(digest) is str and release.DIGEST.fullmatch(digest), "migration_checksum_invalid")
        maximum = file_limit(name)
        release.regular(directory / name, maximum=maximum)
        release.require(release.checksum(directory / name) == digest, "migration_checksum_mismatch")
    if "dstack" in value:
        dstack_provenance(directory, value)
    return value


def fetch(root, commit, *, api=None):
    """Fetch separately approved additive objects; never approve or start them."""
    release.require(release.SHA.fullmatch(commit), "invalid_commit")
    protected_directory(root / "incoming-migrations")
    if api is None:
        import boto3
        from botocore.config import Config
        api = boto3.client("s3", region_name="ap-southeast-1", endpoint_url="https://s3.ap-southeast-1.amazonaws.com",
            config=Config(connect_timeout=5, read_timeout=60, retries={"mode": "standard", "max_attempts": 3}))
    import fetch_release_s3
    staging = Path(tempfile.mkdtemp(prefix=".migration-download-", dir=root))
    target = root / "incoming-migrations" / commit
    try:
        prefix = "migration/"
        fetch_release_s3.download(api, commit, prefix + MANIFEST, staging / MANIFEST, 32768)
        approved(root, staging, commit)  # Before transferring the large image.
        document = json.loads((staging / MANIFEST).read_text())
        for name in sorted(artifact_files(document)):
            maximum = file_limit(name)
            fetch_release_s3.download(api, commit, prefix + name, staging / name, maximum)
        value = manifest(staging, commit)
        approved(root, staging, commit)
        if target.exists() or target.is_symlink():
            release.require(manifest(target, commit) == value, "migration_existing_download_differs")
            approved(root, target, commit)
            return target
        staging.rename(target)
        release.sync_directory(target.parent)
        return target
    finally:
        if staging.exists():
            for name in FILES | DSTACK_FILES | {MANIFEST}:
                (staging / name).unlink(missing_ok=True)
            staging.rmdir()


def extract_sources(archive_path, target, index_digest, *, source_commits=None):
    """Extract only the small public four-file source sets; no tar links/devices."""
    target = Path(target)
    release.require(not target.exists() and not target.is_symlink(), "migration_sources_already_exist")
    target.mkdir(mode=0o700)
    seen, total = set(), 0
    with tarfile.open(archive_path, "r|gz") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            release.require(name and name not in seen and not name.startswith("/") and "\\" not in name
                and all(p not in {"", ".", ".."} for p in name.split("/")) and member.isfile(),
                "migration_source_archive_unsafe")
            seen.add(name); total += member.size
            release.require(len(seen) <= 1024 and total <= 256 * 1024**2 and member.size <= 64 * 1024**2,
                            "migration_source_archive_limit")
            destination = target / name
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with destination.open("xb") as output:
                shutil.copyfileobj(archive.extractfile(member), output, 1024 * 1024)
                output.flush(); os.fsync(output.fileno())
            destination.chmod(0o600)
    verify_sources(target, index_digest, seen=seen, source_commits=source_commits)
    if os.name == "posix":
        for path in (target, *target.rglob("*")):
            os.chown(path, 10001, 10001)
            if path.is_dir():
                path.chmod(0o700)
    release.sync_directory(target)


def verify_sources(target, index_digest, *, seen=None, source_commits=None):
    target = Path(target)
    release.require(target.is_dir() and not target.is_symlink(), "migration_source_directory_invalid")
    actual = set()
    for path in target.rglob("*"):
        release.require(not path.is_symlink(), "migration_source_set_linked")
        if path.is_file():
            release.regular(path, maximum=64 * 1024**2)
            actual.add(path.relative_to(target).as_posix())
    if seen is not None:
        release.require(actual == seen, "migration_source_archive_changed")
    seen = actual
    release.require("index.json" in seen and release.checksum(target / "index.json") == index_digest,
                    "migration_source_index_changed")
    index = json.loads((target / "index.json").read_text())
    release.require(type(index) is dict and type(index.get("schema_version")) is int and index["schema_version"] == 1
        and index.get("purpose") == "local-unqualified-dstack-source-set"
        and index.get("production_adapter_verified") is False and type(index.get("sources")) is list
        and 0 < len(index["sources"]) <= 128, "migration_source_index_invalid")
    expected = {"index.json"}
    for item in index["sources"]:
        release.require(type(item) is dict and type(item.get("directory")) is str
            and item.get("gpu_count") == 1 and item.get("profile_slot_index") == 0
            and type(item.get("source_sha256")) is dict
            and set(item["source_sha256"]) == {"wangp-package.tar.gz", "wangp-bootstrap.py", "wangp-manifest.json", "wangp-runtime.json"},
            "migration_source_set_invalid")
        for name, digest in item["source_sha256"].items():
            relative = item["directory"] + "/" + name
            release.require(relative in seen and relative not in expected and type(digest) is str and release.DIGEST.fullmatch(digest)
                and release.checksum(target / relative) == digest, "migration_source_set_changed")
            expected.add(relative)
    release.require(seen == expected, "migration_source_set_extra_files")
    if source_commits is not None:
        packages = {item["source_sha256"]["wangp-package.tar.gz"] for item in index["sources"]}
        release.require(type(source_commits) is dict and set(source_commits) == packages
            and all(type(source) is str and release.SHA.fullmatch(source) for source in source_commits.values()),
            "migration_source_commits_changed")


def prepare(root, commit, platform_directory):
    incoming = root / "incoming-migrations" / commit
    value = manifest(incoming, commit, platform_manifest_sha256=release.checksum(platform_directory / "release-manifest.json"))
    approved(root, incoming, commit)
    parent = root / "migration" / "releases"
    protected_directory(parent)
    target = parent / commit
    if target.exists():
        protected_directory(target)
        release.require(manifest(target / "artifact", commit) == value, "migration_existing_release_differs")
        approved(root, target / "artifact", commit)
        verify_sources(target / "sources", value["source_index_sha256"], source_commits=value.get("native_source_commits"))
        expected_overlay = (target / "artifact" / "platform-overlay.yaml").read_text().replace(
            "source: ../hatchet/entrypoint.sh", "source: " + json.dumps(str(target / "artifact" / "hatchet-entrypoint.sh")))
        release.regular(target / "overlay.yaml", root_owned=True, maximum=1024**2)
        release.require((target / "overlay.yaml").read_text() == expected_overlay, "migration_derived_overlay_changed")
        return target, value
    staging = Path(tempfile.mkdtemp(prefix=".migration-release-", dir=parent))
    try:
        artifact = staging / "artifact"; artifact.mkdir(mode=0o700)
        for name in artifact_files(value) | {MANIFEST}:
            fd = os.open(incoming / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as source, (artifact / name).open("xb") as output:
                info = os.fstat(source.fileno())
                release.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "migration_copy_input_changed")
                maximum = file_limit(name)
                copied = 0
                while chunk := source.read(1024 * 1024):
                    copied += len(chunk)
                    release.require(copied <= maximum, "migration_copy_input_grew")
                    output.write(chunk)
                output.flush(); os.fsync(output.fileno())
            (artifact / name).chmod(0o600)
        release.require(manifest(artifact, commit) == value, "migration_copied_release_differs")
        approved(root, artifact, commit)
        extract_sources(artifact / "source-set.tar.gz", staging / "sources", value["source_index_sha256"],
            source_commits=value.get("native_source_commits"))
        overlay = (artifact / "platform-overlay.yaml").read_text()
        needle = "source: ../hatchet/entrypoint.sh"
        release.require(overlay.count(needle) == (0 if notify_layout(value) else 1), "migration_entrypoint_binding_invalid")
        # Resolve only this reviewed relative bind; never interpolate credentials.
        overlay = overlay.replace(needle, "source: " + json.dumps(str(target / "artifact" / "hatchet-entrypoint.sh")))
        (staging / "overlay.yaml").write_text(overlay)
        (staging / "overlay.yaml").chmod(0o600)
        release.sync_directory(artifact); release.sync_directory(staging)
        staging.rename(target); release.sync_directory(parent)
        return target, value
    finally:
        if staging.exists():
            # Fresh helper-owned staging only; no persistent/user data.
            shutil.rmtree(staging)


def settings(root):
    directory = root / "migration"; protected_directory(directory)
    path = directory / "paths.json"
    release.regular(path, root_owned=True, maximum=32768)
    value = json.loads(path.read_text())
    sealed = deployment_target.host(root, legacy=True)
    release.require(deployment_target.environment(value.get('environment', {}))['name']
        == sealed['name'], 'migration_target_host_mismatch')
    resource_env = deployment_target.resource_environment(sealed)
    release.require(deployment_target.resources(value.get('environment', {})) == deployment_target.resources(
        {'INKSEQ_DEPLOYMENT_TARGET': sealed['name'], **resource_env}), 'migration_resource_host_mismatch')
    if resource_env:
        operator = Path(value['environment']['SIXNINE_DSTACK_OPERATOR_CONFIG_FILE'])
        release.regular(operator, root_owned=True, maximum=1024**2)
        policy = json.loads(operator.read_text()).get('policy', {})
        # The compact profile seals CPU resources, not a separate single-GPU policy.
        # CR59 permits explicit protected limits up to ten; reads never adopt them.
        release.require(all(type(policy.get(key)) is int and 1 <= policy[key] <= 10
                            for key in ('max_instances', 'max_physical_gpus')),
                        'migration_compact_capacity_limit_invalid')
    return settings_document(value), release.checksum(directory / "paths.json")


def settings_document(value):
    """Validate a protected configuration or a flag-only candidate in memory."""
    release.require(type(value) is dict and set(value) == {"version", "environment", "host_memory_mib"}
        and type(value["version"]) is int and value["version"] == 1 and type(value["environment"]) is dict
        and PATH_FIELDS | OTHER_FIELDS <= set(value["environment"]) <= PATH_FIELDS | OTHER_FIELDS | OPTIONAL_FIELDS
        and type(value["host_memory_mib"]) is int and value["host_memory_mib"] >= 1024,
        "migration_paths_invalid")
    for key, text in value["environment"].items():
        if key in OPTIONAL_FIELDS and text == "":
            continue
        release.require(type(text) is str and text and text == text.strip() and not any(c in text for c in "\n\r\x00"),
                        "migration_path_reference_invalid")
        if key in PATH_FIELDS | OPTIONAL_PATH_FIELDS:
            path = Path(text)
            release.require(path.is_absolute() and ".." not in path.parts and not path.is_symlink(),
                            "migration_path_reference_invalid")
        elif key.endswith("_VOLUME"):
            release.require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", text), "migration_volume_reference_invalid")
    env = value["environment"]
    deployment_target.inky_environment(env)
    deployment_target.langfuse_environment(env)
    release.require(env.get('INKSEQ_SENTRY_DSN', '') in {'', release.SENTRY_DSN},
        'migration_api_error_monitoring_destination_invalid')
    release.require(not env.get('INKSEQ_SENTRY_DSN') or env.get('INKSEQ_DEPLOYMENT_TARGET') in {'staging', 'production'},
        'migration_api_error_monitoring_target_required')
    release.require(env.get('INKSEQ_WORKOS_CONFIG_FILE', '') in {'', '/run/sixnine-secrets/workos'},
        'migration_workos_configuration_path_invalid')
    stripe_file = env.get('INKSEQ_STRIPE_CONFIG_FILE', '')
    stripe_mode = env.get('INKSEQ_STRIPE_MODE', 'off')
    release.require(
        stripe_file in {'', '/run/sixnine-secrets/stripe'}
        and stripe_mode in {'off', 'test', 'live'}
        and (stripe_mode == 'off' or bool(stripe_file))
        and (
            stripe_mode != 'test'
            or deployment_target.environment(env)['name'] == 'staging'
        )
        and (
            stripe_mode != 'live'
            or deployment_target.environment(env)['name'] == 'production'
        )
        and (
            not stripe_file
            and stripe_mode == 'off'
            # Protected paths do not carry an origin override: the sealed
            # deployment target selects it. Validate the rendered app origin
            # separately in check_migration_deploy and runtime hydration.
            or env.get('INKSEQ_GENERATION_QUEUE', 'postgres') == 'dbos'
        ),
        'migration_stripe_configuration_invalid',
    )
    queue = env.get('INKSEQ_GENERATION_QUEUE', 'postgres')
    film = env.get('INKSEQ_FILM_ENABLED', '0')
    release.require(queue in {'postgres','dbos'} and film in {'0','1'}
        and (film == '0' or queue == 'dbos'), 'migration_workflow_settings_invalid')
    deployment_target.resources(env)
    storage_settings(env)
    release.require((env["SIXNINE_MIGRATION_GENERATION_ENABLED"], env["SIXNINE_MIGRATION_EXECUTION_BACKEND"])
        in {("0", "disabled"), ("1", "wangp-worker")}, "migration_admission_reference_invalid")
    release.require(re.fullmatch(r"[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*", env["SIXNINE_OPERATOR_CAPACITY_OWNERS"]),
                    "migration_owner_reference_invalid")
    admins = env.get("SIXNINE_OPERATOR_ADMIN_OWNERS", "")
    release.require(not admins or re.fullmatch(r"[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*", admins)
        and set(admins.split(",")) <= set(env["SIXNINE_OPERATOR_CAPACITY_OWNERS"].split(",")),
        "migration_admin_reference_invalid")
    storyboard = env.get("SIXNINE_STORYBOARD_OWNERS", "")
    release.require(not storyboard or len(storyboard.split(",")) == len(set(storyboard.split(",")))
        and set(storyboard.split(",")) <= {"superdan", "supervan"},
        "migration_storyboard_reference_invalid")
    release.require(re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", env["SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID"]),
                    "migration_default_profile_invalid")
    return value


def storage_settings(environment):
    """Closed nonsecret opt-in; existing local configurations need no changes."""
    selected = deployment_target.environment(environment)
    provider = environment.get("SIXNINE_STORAGE_PROVIDER", "local") or "local"
    if provider == "local":
        release.require(not any(environment.get(key) for key in STORAGE_FIELDS - {"SIXNINE_STORAGE_PROVIDER"}),
            "migration_storage_local_configuration_invalid")
        return
    release.require(provider == "r2" and STORAGE_FIELDS <= set(environment)
        and all(environment.get(key) for key in STORAGE_FIELDS)
        and environment["SIXNINE_STORAGE_PROFILE"] == selected['r2_profile']
        and (selected['name'] != 'staging' or environment['SIXNINE_STORAGE_BUCKET'] == selected['r2_bucket'])
        and environment["SIXNINE_STORAGE_REGION"] == "auto"
        and re.fullmatch(r"https://[0-9a-f]{32}(?:\.(?:eu|us|fedramp))?\.r2\.cloudflarestorage\.com",
            environment["SIXNINE_STORAGE_ENDPOINT"])
        and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", environment["SIXNINE_STORAGE_BUCKET"])
        and environment["SIXNINE_STORAGE_CREDENTIAL_FILE"] == "/run/sixnine-secrets/r2_storage",
        "migration_storage_r2_configuration_invalid")


def default_profile(directory, environment):
    """The explicit creator default must have sources in this approved artifact."""
    index = json.loads((directory / "sources" / "index.json").read_text())
    release.require(environment["SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID"] in
        {item["runtime_profile_id"] for item in index["sources"]}, "migration_default_profile_not_packaged")


def legacy_handoff(root, commit, *, active=None, now=None):
    """A separately approved CPU handoff does not settle or erase legacy pins.

    The fresh root receipt is required for initial activation. Later deployments
    bind the same receipt through active.json and recheck unchanged legacy pins;
    new dstack jobs/providers do not have to become empty for an app update.
    """
    directory = root / "migration"
    protected_directory(directory)
    receipt = directory / "legacy-cpu-handoff.json"
    approval = directory / "approved-legacy-cpu-handoff.sha256"
    release.regular(receipt, root_owned=True, maximum=32768)
    release.regular(approval, root_owned=True, maximum=128)
    digest = release.checksum(receipt)
    release.require(approval.read_text(encoding="ascii").strip() == digest,
                    "migration_legacy_handoff_independent_approval_missing")
    value = json.loads(receipt.read_text())
    release.require(type(value) is dict and set(value) == {"version", "purpose", "host_id", "target_commit",
        "checked_at", "markers_sha256", "providers", "business", "preservation", "evidence_refs"}
        and type(value["version"]) is int and value["version"] == 1
        and value["purpose"] == "legacy-cpu-authority-handoff-only"
        and type(value["target_commit"]) is str and release.SHA.fullmatch(value["target_commit"])
        and type(value["host_id"]) is str and re.fullmatch(r"i-[0-9a-f]{8,17}", value["host_id"])
        and type(value["checked_at"]) in (int, float) and math.isfinite(value["checked_at"])
        and type(value["markers_sha256"]) is dict and set(value["markers_sha256"]) == set(LEGACY_MARKERS)
        and type(value["providers"]) is dict and set(value["providers"]) == {"lium", "targon"}
        and type(value["business"]) is dict and set(value["business"]) == {"nonterminal_jobs", "nonterminal_attempts",
            "admission_closed_before_probe", "checked_at"}
        and all(type(value["business"][key]) is int and value["business"][key] == 0
            for key in ("nonterminal_jobs", "nonterminal_attempts"))
        and value["business"]["admission_closed_before_probe"] is True
        and type(value["business"]["checked_at"]) in (int, float) and math.isfinite(value["business"]["checked_at"])
        and value["preservation"] == {"original_markers": "unchanged", "ledger": "unchanged",
            "budgets_and_reservations": "unchanged", "provider_deletion": "not_asserted",
            "final_billing": "not_asserted", "legacy_retirement": "deferred"}
        and type(value["evidence_refs"]) is list and 0 < len(value["evidence_refs"]) <= 16
        and all(type(ref) is str and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,300}", ref) for ref in value["evidence_refs"]),
        "migration_legacy_handoff_invalid")
    host = Path("/var/lib/cloud/data/instance-id").read_text().strip()
    release.require(value["host_id"] == host, "migration_legacy_handoff_wrong_host")
    for name, expected in value["markers_sha256"].items():
        path = root / name
        if expected is None:
            release.require(not path.exists() and not path.is_symlink(), "migration_legacy_marker_changed")
        else:
            release.require(type(expected) is str and release.DIGEST.fullmatch(expected), "migration_legacy_handoff_invalid")
            protected_directory(path.parent)
            release.regular(path, root_owned=True, maximum=16384)
            release.require(release.checksum(path) == expected, "migration_legacy_marker_changed")
    fresh = not active or active.get("current") is None
    now = time.time() if now is None else now
    if fresh:
        release.require(value["target_commit"] == commit and 0 <= now-value["checked_at"] <= 300,
                        "migration_legacy_handoff_stale")
        release.require(0 <= now-value["business"]["checked_at"] <= 300, "migration_legacy_business_observation_stale")
    else:
        release.require(active.get("legacy_cpu_handoff_sha256") == digest,
                        "migration_legacy_handoff_binding_changed")
    for provider in value["providers"].values():
        release.require(type(provider) is dict and set(provider) == {"account_scope_verified", "complete",
            "listed_allocations", "checked_at"} and provider["account_scope_verified"] is True
            and provider["complete"] is True and type(provider["listed_allocations"]) is int
            and provider["listed_allocations"] == 0 and type(provider["checked_at"]) in (int, float)
            and math.isfinite(provider["checked_at"]), "migration_legacy_account_observation_invalid")
        if fresh:
            release.require(0 <= now-provider["checked_at"] <= 300, "migration_legacy_account_observation_stale")
    return digest


def installed_checker(artifact):
    """Execute only independent-review installed root code, never artifact code."""
    helper = Path(__file__).resolve()
    checker_path = helper.with_name("migration_check.py")
    release.regular(helper, root_owned=True, maximum=1024**2)
    release.regular(checker_path, root_owned=True, maximum=1024**2)
    installed = (release.checksum(helper), release.checksum(checker_path))
    incoming = (release.checksum(artifact / "maintenance.py"), release.checksum(artifact / "migration-check.py"))
    if installed != incoming:
        release.regular(artifact / MANIFEST, maximum=32768)
        value = json.loads((artifact / MANIFEST).read_text(encoding="utf-8"))
        release.require(type(value) is dict and type(value.get("commit")) is str
            and ("dstack" not in value or RETAINED_DSTACK_HELPER_PAIRS.get(value['commit']) == incoming)
            and LEGACY_HELPER_PAIRS.get(value.get("commit")) == incoming,
            "migration_root_helpers_need_reviewed_update")
    spec = importlib.util.spec_from_file_location("sixnine_trusted_migration_checker", checker_path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def pinned_loaded_image(expected, environment):
    value = json.loads(release.command(["image", "inspect", expected["image"]], environment=environment))[0]
    candidates = [expected["image"] + "@" + item.split("@", 1)[1] for item in value.get("RepoDigests", []) if "@" in item]
    candidates.append(expected["image"] + "@" + value["Id"])
    for reference in candidates:
        try:
            pinned = json.loads(release.command(["image", "inspect", reference], environment=environment))[0]
        except release.ReleaseError:
            continue
        if pinned.get("Id") == value["Id"] and pinned["Id"] in expected["archive_image_ids"]:
            return reference
    raise release.ReleaseError("migration_loaded_image_digest_not_resolvable")


def dstack_image(directory, migration, environment, *, load=False):
    """Optional v1 extension; old approved artifacts keep their exact upstream."""
    if "dstack" not in migration:
        return None
    expected = {"commit": migration["commit"], **migration["dstack"]}
    archive = Path(directory) / "artifact" / "dstack-server-image.tar.gz"
    expected["archive_image_ids"] = release.validate_image_archive(archive, expected)
    expected["installed_provenance"] = dstack_provenance(Path(directory) / "artifact", migration)
    if load:
        release.command(["load", "--input", str(archive)], environment=environment, timeout=300)
    return expected


def compose(base, migration_directory, environment, *arguments, timeout=180):
    return release.command(["compose", "--env-file", "/dev/null", "--project-directory", str(base),
        "-f", str(base / "compose.yaml"), "-f", str(migration_directory / "overlay.yaml"), *arguments],
        environment=environment, timeout=timeout)


def verify_services(base, directory, environment, app, dispatch):
    resources = deployment_target.resources(environment)
    compact = resources['name'] == deployment_target.COMPACT_RESOURCES
    migration = directory_manifest(directory)
    modern = notify_layout(migration)
    shared = shared_services(migration)
    expected = {"app": app, "controller": dispatch}
    if not modern:
        expected["dispatch"] = dispatch
    server = dispatch.get("_dstack_server")
    if server is not None:
        expected["dstack"] = server
    values = {}
    for name in ("app", *shared, *(("hatchet-db",) if not modern else ()), *(("db", "caddy") if compact else ())):
        identity = compose(base, directory, environment, "ps", "--all", "--quiet", name, timeout=20).decode().strip()
        release.require(re.fullmatch(r"[0-9a-f]{12,64}", identity), "migration_cpu_service_not_unique")
        inspected = json.loads(release.command(["inspect", identity], environment=environment, timeout=20))
        release.require(type(inspected) is list and len(inspected) == 1, "migration_cpu_service_not_unique")
        value = inspected[0]
        labels = value.get("Config", {}).get("Labels", {})
        release.require(labels.get("com.docker.compose.project") == "sixnine-platform"
            and labels.get("com.docker.compose.service") == name, "migration_cpu_service_identity_mismatch")
        values[name] = value
    app_id = values["app"]["Id"]
    for name, value in values.items():
        if compact:
            release.require(value.get('HostConfig', {}).get('CgroupParent') == resources['cgroup_parent'],
                            'migration_compact_runtime_parent_changed')
            pid = value.get('State', {}).get('Pid')
            release.require(type(pid) is int and pid > 0, 'migration_compact_runtime_pid_missing')
            membership = Path('/proc') / str(pid) / 'cgroup'
            prefix = '0::' + str(deployment_target.COMPACT_CGROUP.relative_to('/sys/fs/cgroup'))
            # Kernel records the hierarchy with a leading slash.
            prefix = prefix.replace('0::', '0::/') + '/'
            release.require(any(line.startswith(prefix) for line in membership.read_text().splitlines()),
                            'migration_compact_runtime_outside_parent')
        release.require(value.get("State", {}).get("Running") is True
            and value.get("State", {}).get("Restarting") is not True, "migration_cpu_service_not_running")
        if name in expected:
            release.require(value.get("Image") in expected[name]["archive_image_ids"], "migration_cpu_image_mismatch")
        if name in shared:
            release.require(value.get("HostConfig", {}).get("NetworkMode") in {
                "container:" + app_id, "container:" + values["app"].get("Name", "").lstrip("/")},
                "migration_shared_namespace_stale")
        if name in {"app", "dstack", "hatchet"}:
            release.require(value.get("State", {}).get("Health", {}).get("Status") == "healthy",
                            "migration_cpu_service_not_healthy")
    for service, volume, destination in (("hatchet", "SIXNINE_HATCHET_CONFIG_VOLUME", "/config"),
            ("hatchet-db", "SIXNINE_HATCHET_POSTGRES_VOLUME", "/var/lib/postgresql/data")):
        if modern:
            continue
        mounts = [item for item in values[service].get("Mounts", []) if item.get("Destination") == destination]
        release.require(len(mounts) == 1 and mounts[0].get("Type") == "volume"
            and mounts[0].get("Name") == environment[volume], "migration_broker_volume_changed")
    for service, image in (("dstack", LEGACY_DSTACK_BASE_IMAGE),
            ("hatchet", "ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.110.5@sha256:cc81f078a5285d7d2051ec4a4fa459d033e1face4ca5983d82612b00da6e3fa3"),
            ("hatchet-db", "postgres:15@sha256:c961aa287d8698297cb26cdfadfbe9fd2cbaf77e53cfffe9636e8d8a1e4d842c")):
        if service not in values or service == "dstack" and server is not None:
            continue
        pinned = json.loads(release.command(["image", "inspect", image], environment=environment, timeout=20))[0]
        release.require(values[service].get("Image") == pinned.get("Id"), "migration_infrastructure_image_mismatch")
    if server is not None:
        observed = json.loads(release.command(["exec", "--user", "0:0", values["dstack"]["Id"],
            "/root/.local/share/uv/tools/dstack/bin/python", "-B", "-c", DSTACK_IMAGE_CHECK],
            environment=environment, timeout=30))
        release.require(observed == server["installed_provenance"], "migration_dstack_installed_source_changed")


def guard_legacy(root, *, commit=None, active=None):
    receipt = root / "migration" / "legacy-cpu-handoff.json"
    digest = None
    if receipt.exists() or receipt.is_symlink():
        digest = legacy_handoff(root, commit, active=active)
    else:
        release.require(not active or not active.get("legacy_cpu_handoff_sha256"),
                        "migration_legacy_handoff_binding_changed")
        release.require(release.gpu_deployment_context(root) is None, "migration_requires_legacy_controller_handoff")
    environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
        "DOCKER_CONFIG": "/opt/sixnine-release/docker-config"}
    if digest and (not active or active.get("current") is None):
        # Initial handoff uses bounded downtime, so its final zero-job probe
        # cannot race a new request accepted by the old app after the receipt.
        app = release.command(["ps", "--quiet", "--filter", "label=com.docker.compose.project=sixnine-platform",
            "--filter", "label=com.docker.compose.service=app"], environment=environment, timeout=20)
        release.require(not app.strip(), "migration_legacy_admission_not_fenced")
    running = release.command(["ps", "--quiet", "--filter", "name=^sixnine-operator-"],
        environment=environment, timeout=20)
    release.require(not running.strip(), "migration_legacy_controller_still_running")
    for service in ("operator-controller", "gpu-controller", "gpu-worker"):
        running = release.command(["ps", "--quiet", "--filter", "label=com.docker.compose.project=sixnine-platform",
            "--filter", "label=com.docker.compose.service=" + service], environment=environment, timeout=20)
        release.require(not running.strip(), "migration_legacy_controller_still_running")
    running = release.command(["ps", "--quiet", "--filter", "name=^sixnine-(finite|acceptance)-"],
        environment=environment, timeout=20)
    release.require(not running.strip(), "migration_legacy_controller_still_running")
    return digest


def stop_superseded_cpu(environment, *, remove=False):
    """Stop exact old Compose CPU containers; retain containers, volumes and secrets.

    Collect and verify all identities before stopping any. Never use down,
    remove-orphans, broad names or a provider/GPU operation.
    """
    identities = []
    for service in ("dispatch", "hatchet", "hatchet-db"):
        found = release.command(["ps", "--all", "--quiet", "--filter",
            "label=com.docker.compose.project=sixnine-platform", "--filter",
            "label=com.docker.compose.service=" + service], environment=environment, timeout=20).decode().split()
        release.require(len(found) <= 1 and all(re.fullmatch(r"[0-9a-f]{12,64}", item) for item in found),
                        "migration_retired_cpu_identity_ambiguous")
        for identity in found:
            rows = json.loads(release.command(["inspect", identity], environment=environment, timeout=20))
            release.require(type(rows) is list and len(rows) == 1, "migration_retired_cpu_identity_invalid")
            value = rows[0]
            labels = value.get("Config", {}).get("Labels", {})
            release.require(labels.get("com.docker.compose.project") == "sixnine-platform"
                and labels.get("com.docker.compose.service") == service
                and not value.get("HostConfig", {}).get("DeviceRequests")
                and not value.get("HostConfig", {}).get("Devices")
                and not value.get("HostConfig", {}).get("Privileged"), "migration_retired_cpu_identity_invalid")
            identities.append(identity)
    if identities:
        release.command(["stop", "--time", "210", *identities], environment=environment, timeout=240)
        if remove:
            release.command(["rm", *identities], environment=environment, timeout=60)


def apply_locked(root, commit):
    """Caller holds existing release lock. Reconnect every shared CPU member."""
    storyboard_maintenance_fence(root)
    config, config_digest = settings(root)
    active_path = root / "migration" / "active.json"
    release.regular(active_path, root_owned=True, maximum=16384)
    active = json.loads(active_path.read_text())
    release.require(type(active) is dict and type(active.get("version")) is int and active["version"] == 1 and active.get("active") is True
        and active.get("paths_sha256") == config_digest, "migration_configuration_changed_requires_review")
    handoff_digest = guard_legacy(root, commit=commit, active=active)
    release.approved_manifest(root, root / "incoming" / commit, commit)
    base = release.prepare_bundle(root, commit)
    directory, migration = prepare(root, commit, base)
    checker = installed_checker(directory / "artifact")
    env = release.deployment_environment(root / "site.env", commit)
    state = json.loads((root / "release-state.json").read_text())
    release.require(state.get("pending") in (None, commit), "another_release_requires_reconciliation")
    release.require(state.get("dependencies") == release.pinned_dependencies(env),
                    "dependency_change_requires_separate_maintenance")
    app = release.load_approved_image(root, base, commit, env)
    dispatch = {"commit": commit, **migration["dispatch"]}
    dispatch["archive_image_ids"] = release.validate_image_archive(directory / "artifact" / "dispatch-image.tar.gz", dispatch)
    release.command(["load", "--input", str(directory / "artifact" / "dispatch-image.tar.gz")], environment=env, timeout=300)
    server = dstack_image(directory, migration, env, load=True)
    dispatch["_dstack_server"] = server
    env.update(config["environment"], SIXNINE_IMAGE=pinned_loaded_image(app, env),
        SIXNINE_DISPATCH_IMAGE=pinned_loaded_image(dispatch, env), SIXNINE_DSTACK_SOURCE_DIR=str(directory / "sources"),
        SIXNINE_DSTACK_SERVER_IMAGE=pinned_loaded_image(server, env) if server else LEGACY_DSTACK_BASE_IMAGE)
    default_profile(directory, env)
    checker.check(base / "compose.yaml", directory / "overlay.yaml", environment=env,
        host_memory_mib=config["host_memory_mib"], check_inputs=True)
    if state.get("current") == commit and state.get("status") == "app_ready" and active.get("current") == commit:
        verify_services(base, directory, env, app, dispatch)
        checker.check(base / "compose.yaml", directory / "overlay.yaml", environment=env,
            host_memory_mib=config["host_memory_mib"], check_inputs=True, probe=True)
        if notify_layout(migration):
            stop_superseded_cpu(env, remove=True)
        return
    previous = state.get("current")
    release.write_state(root, {**state, "pending": commit, "status": "deploying_migration", "updated_at": time.time()})
    atomic(active_path, {**active, "pending": commit})
    try:
        # Stop only the four CPU members before replacing their shared namespace.
        # Broker volumes and native provider runs/attempts remain untouched.
        if notify_layout(migration):
            stop_superseded_cpu(env)
        compose(base, directory, env, "stop", *shared_services(migration), timeout=240)
        compose(base, directory, env, "up", "-d", "db", *(("hatchet-db",) if not notify_layout(migration) else ()), timeout=240)
        compose(base, directory, env, "run", "--rm", "db-init", timeout=120)
        release.retire_frontend_pointer(root)
        compose(base, directory, env, "up", "-d", "--no-deps", "--force-recreate", "app", *shared_services(migration), timeout=300)
        release.wait_ready(base, env)
        deadline = time.monotonic() + 180
        while True:
            try:
                verify_services(base, directory, env, app, dispatch)
                checker.check(base / "compose.yaml", directory / "overlay.yaml", environment=env,
                    host_memory_mib=config["host_memory_mib"], check_inputs=True, probe=True)
                break
            except (release.ReleaseError, ValueError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(3)
        compose(base, directory, env, "up", "-d", "--no-deps", "caddy")
        release.wait_proxy_stable(base, env)
        if notify_layout(migration):
            stop_superseded_cpu(env, remove=True)
        atomic(active_path, {"version": 1, "active": True, "paths_sha256": config_digest,
            "current": commit, "pending": None,
            **({"legacy_cpu_handoff_sha256": handoff_digest} if handoff_digest else {})})
        release.write_state(root, {"current": commit, "previous": previous if previous != commit else state.get("previous"),
            "dependencies": state["dependencies"], "status": "app_ready", "updated_at": time.time()})
    except Exception:
        release.write_state(root, {**state, "pending": commit, "status": "migration_failed_needs_reconciliation",
            "updated_at": time.time()})
        raise release.ReleaseError("migration_release_failed_original_state_preserved") from None


def activate(root, commit):
    """Explicit independent root maintenance; future ordinary deploys stay guarded."""
    release.check_host(root)
    import fcntl
    with (root / "release.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        storyboard_maintenance_fence(root)
        _, digest = settings(root)
        path = root / "migration" / "active.json"
        if path.exists() or path.is_symlink():
            release.regular(path, root_owned=True, maximum=16384)
            active = json.loads(path.read_text())
            release.require(active.get("active") is True and active.get("paths_sha256") == digest,
                            "migration_activation_requires_reconciliation")
        handoff_digest = guard_legacy(root, commit=commit, active=active if path.exists() else None)
        import fetch_release_s3
        fetch_release_s3.fetch(commit, root=root)
        fetch(root, commit)
        if not path.exists():
            atomic(path, {"version": 1, "active": True, "paths_sha256": digest, "current": None, "pending": commit,
                **({"legacy_cpu_handoff_sha256": handoff_digest} if handoff_digest else {})})
        apply_locked(root, commit)


def storyboard_receipt(root, *, filename=STORYBOARD_RECEIPT, purpose="storyboard-owner-flag-only"):
    path = root / "migration" / filename
    if not path.exists() and not path.is_symlink():
        return None
    release.regular(path, root_owned=True, maximum=32768)
    value = json.loads(path.read_text())
    release.require(type(value) is dict and set(value) == {"version", "purpose", "commit", "operation_id",
        "status", "before", "after", "runtime_before"} | ({"profiles_sha256", "db_restarts"} if purpose == "default-profile-only" else set()) and type(value["version"]) is int and value["version"] == 1
        and value["purpose"] == purpose and type(value.get("commit")) is str
        and release.SHA.fullmatch(value["commit"]) and type(value.get("operation_id")) is str
        and release.DIGEST.fullmatch(value["operation_id"])
        and value["status"] in {"pending", "failed", "completed", "restored"}
        and all(type(value[key]) is dict and set(value[key]) == {"paths", "active"}
            and all(type(digest) is str and release.DIGEST.fullmatch(digest) for digest in value[key].values())
            for key in ("before", "after")) and type(value["runtime_before"]) is dict
        and set(value["runtime_before"]) in (set(STORYBOARD_SERVICES), {"app", "dstack", "controller", "db"}), "migration_storyboard_receipt_invalid")
    for item in value["runtime_before"].values():
        release.require(type(item) is dict and set(item) == {"image_id", "environment_sha256", "mounts_sha256"}
            and type(item["image_id"]) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", item["image_id"])
            and all(type(item[key]) is str and release.DIGEST.fullmatch(item[key])
                for key in ("environment_sha256", "mounts_sha256")), "migration_storyboard_receipt_invalid")
    if purpose == "default-profile-only":
        release.require(type(value['profiles_sha256']) is str and release.DIGEST.fullmatch(value['profiles_sha256']),
                        'migration_default_profile_receipt_invalid')
        restarts = value["db_restarts"]
        release.require(type(restarts) is list and len(restarts) <= 8, "migration_default_profile_receipt_invalid")
        for item in restarts:
            release.require(type(item) is dict and set(item) == {"stop_time_us", "terminated", "connections"}
                and type(item["stop_time_us"]) is str and re.fullmatch(r"[0-9]{15,20}", item["stop_time_us"])
                and (item["terminated"] is None or type(item["terminated"]) is int and 0 <= item["terminated"] <= 256)
                and (item["connections"] is None or type(item["connections"]) is dict),
                "migration_default_profile_receipt_invalid")
    return value


def storyboard_maintenance_fence(root):
    value = storyboard_receipt(root)
    release.require(not value or value["status"] not in {"pending", "failed"},
        "migration_storyboard_maintenance_pending_restore_required")
    value = storyboard_receipt(root, filename=DEFAULT_PROFILE_RECEIPT, purpose="default-profile-only")
    release.require(not value or value["status"] not in {"pending", "failed"},
        "migration_default_profile_pending_restore_required")


def _storyboard_context(root, commit, config):
    """Use the installed, independently approved current images without loading or fetching."""
    state = release._protected_json(root / "release-state.json", maximum=16384)
    release.require(state.get("current") == commit and state.get("pending") is None
        and state.get("status") == "app_ready", "migration_storyboard_current_release_required")
    base = root / "releases" / commit
    directory = root / "migration" / "releases" / commit
    for path in (base.parent, base, directory.parent, directory, directory / "artifact"):
        protected_directory(path)
    release.approved_manifest(root, base, commit)
    app = release.manifest(base, commit)
    migration = manifest(directory / "artifact", commit,
        platform_manifest_sha256=release.checksum(base / "release-manifest.json"))
    approved(root, directory / "artifact", commit)
    verify_sources(directory / "sources", migration["source_index_sha256"], source_commits=migration.get("native_source_commits"))
    overlay = directory / "overlay.yaml"
    release.regular(overlay, root_owned=True, maximum=1024**2)
    expected_overlay = (directory / "artifact" / "platform-overlay.yaml").read_text().replace(
        "source: ../hatchet/entrypoint.sh", "source: " + json.dumps(str(directory / "artifact" / "hatchet-entrypoint.sh")))
    release.require(overlay.read_text() == expected_overlay, "migration_derived_overlay_changed")
    checker = installed_checker(directory / "artifact")
    environment = release.deployment_environment(root / "site.env", commit)
    release.require(state.get("dependencies") == release.pinned_dependencies(environment),
        "dependency_change_requires_separate_maintenance")
    app = {**app, "archive_image_ids": release.validate_image_archive(base / "image.tar.gz", app)}
    dispatch = {"commit": commit, **migration["dispatch"]}
    dispatch["archive_image_ids"] = release.validate_image_archive(directory / "artifact" / "dispatch-image.tar.gz", dispatch)
    server = dstack_image(directory, migration, environment)
    dispatch["_dstack_server"] = server
    environment.update(config["environment"], SIXNINE_IMAGE=pinned_loaded_image(app, environment),
        SIXNINE_DISPATCH_IMAGE=pinned_loaded_image(dispatch, environment), SIXNINE_DSTACK_SOURCE_DIR=str(directory / "sources"),
        SIXNINE_DSTACK_SERVER_IMAGE=pinned_loaded_image(server, environment) if server else LEGACY_DSTACK_BASE_IMAGE)
    default_profile(directory, environment)
    checker.check(base / "compose.yaml", overlay, environment=environment,
        host_memory_mib=config["host_memory_mib"], check_inputs=True)
    return base, directory, environment, app, dispatch, checker


def _storyboard_runtime(base, directory, environment, *, flag=STORYBOARD_FLAG):
    """Nonsecret signatures; compare unchanged runtime settings and persistent mounts."""
    result = {}
    migration = directory_manifest(directory)
    for name in (("app", *shared_services(migration), "db") if notify_layout(migration) else STORYBOARD_SERVICES):
        identity = compose(base, directory, environment, "ps", "--all", "--quiet", name, timeout=20).decode().strip()
        release.require(re.fullmatch(r"[0-9a-f]{12,64}", identity), "migration_cpu_service_not_unique")
        values = json.loads(release.command(["inspect", identity], environment=environment, timeout=20))
        release.require(type(values) is list and len(values) == 1, "migration_cpu_service_not_unique")
        value = values[0]
        labels = value.get("Config", {}).get("Labels", {})
        release.require(labels.get("com.docker.compose.project") == "sixnine-platform"
            and labels.get("com.docker.compose.service") == name and value.get("State", {}).get("Running") is True
            and type(value.get("Image")) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", value["Image"]),
            "migration_storyboard_runtime_identity_invalid")
        entries = value.get("Config", {}).get("Env", [])
        release.require(type(entries) is list and all(type(item) is str and "=" in item for item in entries),
            "migration_storyboard_runtime_environment_invalid")
        actual = dict(item.split("=", 1) for item in entries)
        release.require(len(actual) == len(entries), "migration_storyboard_runtime_environment_invalid")
        flag_services = {"app", "dispatch", "controller"} if flag == DEFAULT_PROFILE_FLAG else {"app"}
        if name in flag_services:
            release.require(actual.pop(flag, "") == environment.get(flag, ""),
                "migration_storyboard_runtime_flag_mismatch")
        else:
            release.require(flag not in actual, "migration_storyboard_worker_environment")
        mounts = [{key: item.get(key) for key in ("Type", "Source", "Destination", "Name", "RW")}
            for item in value.get("Mounts", [])]
        result[name] = {"image_id": value.get("Image"),
            "environment_sha256": hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest(),
            "mounts_sha256": hashlib.sha256(json.dumps(sorted(mounts, key=lambda x: x["Destination"]), sort_keys=True).encode()).hexdigest()}
    return result


def _storyboard_refresh(context, config, runtime_before):
    base, directory, environment, app, dispatch, checker = context
    migration = directory_manifest(directory)
    # These members share app's container network namespace. App-only recreate
    # would orphan them; reconnect current approved CPU images, never provider runs.
    compose(base, directory, environment, "stop", *shared_services(migration), timeout=240)
    compose(base, directory, environment, "up", "-d", "--no-deps", "--force-recreate", "app", *shared_services(migration), timeout=300)
    release.wait_ready(base, environment)
    deadline = time.monotonic() + 180
    while True:
        try:
            verify_services(base, directory, environment, app, dispatch)
            release.require(_storyboard_runtime(base, directory, environment) == runtime_before,
                "migration_storyboard_runtime_or_mounts_changed")
            checker.check(base / "compose.yaml", directory / "overlay.yaml", environment=environment,
                host_memory_mib=config["host_memory_mib"], check_inputs=True, probe=True)
            release.wait_proxy_stable(base, environment)
            return
        except (release.ReleaseError, ValueError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(3)


def _storyboard_bytes(path, data):
    """Atomic protected metadata replacement with exact saved bytes."""
    release.regular(path, root_owned=True, maximum=32768)
    fd, temporary = tempfile.mkstemp(prefix=".storyboard-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path); release.sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _storyboard_backup(root, receipt, *, folder="storyboard-maintenance", flag=STORYBOARD_FLAG):
    directory = root / "migration" / folder / receipt["operation_id"]
    protected_directory(directory.parent); protected_directory(directory)
    saved = {}
    for phase in ("before", "after"):
        for name in ("paths", "active"):
            path = directory / (name + "." + phase + ".json")
            release.regular(path, root_owned=True, maximum=32768)
            raw = path.read_bytes()
            release.require(hashlib.sha256(raw).hexdigest() == receipt[phase][name], "migration_storyboard_backup_changed")
            saved[phase, name] = raw
    old = settings_document(json.loads(saved["before", "paths"]))
    new = settings_document(json.loads(saved["after", "paths"]))
    old_active, new_active = (json.loads(saved[phase, "active"]) for phase in ("before", "after"))
    expected = {**old, "environment": {**old["environment"], flag: new["environment"].get(flag, "")}}
    release.require(new == expected and old_active.get("active") is True and old_active.get("version") == 1
        and old_active.get("current") == receipt["commit"] and old_active.get("pending") is None
        and old_active.get("paths_sha256") == receipt["before"]["paths"]
        and new_active == {**old_active, "paths_sha256": receipt["after"]["paths"]},
        "migration_storyboard_backup_not_flag_only")
    return saved, old, new


def storyboard_owners_locked(root, commit, owners, paths_sha256, active_sha256):
    """Caller holds release.lock; explicit source/hash-bound, flag-only maintenance."""
    release.require(type(commit) is str and release.SHA.fullmatch(commit)
        and all(type(value) is str and release.DIGEST.fullmatch(value) for value in (paths_sha256, active_sha256))
        and type(owners) is str and (not owners or len(owners.split(",")) == len(set(owners.split(",")))
            and set(owners.split(",")) <= {"superdan", "supervan"}), "migration_storyboard_arguments_invalid")
    storyboard_maintenance_fence(root)
    config, digest = settings(root)
    active_path = root / "migration" / "active.json"
    active = release._protected_json(active_path, maximum=16384)
    release.require(digest == paths_sha256 and release.checksum(active_path) == active_sha256
        and active.get("active") is True and active.get("version") == 1 and active.get("current") == commit
        and active.get("pending") is None and active.get("paths_sha256") == digest,
        "migration_storyboard_original_binding_changed")
    guard_legacy(root, commit=commit, active=active)
    context = _storyboard_context(root, commit, config)
    verify_services(*context[:3], context[3], context[4])
    runtime = _storyboard_runtime(*context[:3])
    if config["environment"].get(STORYBOARD_FLAG, "") == owners:
        return {"state": "approved_storyboard_owners_unchanged", "commit": commit, "owners": owners.split(",") if owners else []}
    candidate = settings_document({**config, "environment": {**config["environment"], STORYBOARD_FLAG: owners}})
    candidate_environment = {**context[2], STORYBOARD_FLAG: owners}
    context[5].check(context[0] / "compose.yaml", context[1] / "overlay.yaml", environment=candidate_environment,
        host_memory_mib=candidate["host_memory_mib"], check_inputs=True)
    directory = root / "migration" / "storyboard-maintenance"
    if not directory.exists():
        directory.mkdir(mode=0o700); release.sync_directory(directory.parent)
    protected_directory(directory)
    operation = hashlib.sha256((commit + paths_sha256 + active_sha256 + owners + str(time.time_ns())).encode()).hexdigest()
    backup = directory / operation; backup.mkdir(mode=0o700)
    paths = root / "migration" / "paths.json"
    after_paths = json.dumps(candidate, sort_keys=True).encode()
    after_digest = hashlib.sha256(after_paths).hexdigest()
    saved = {("before", "paths"): paths.read_bytes(), ("before", "active"): active_path.read_bytes(),
        ("after", "paths"): after_paths,
        ("after", "active"): json.dumps({**active, "paths_sha256": after_digest}, sort_keys=True).encode()}
    receipt = {"version": 1, "purpose": "storyboard-owner-flag-only", "commit": commit, "operation_id": operation,
        "status": "pending", "runtime_before": runtime,
        **{phase: {name: hashlib.sha256(saved[phase, name]).hexdigest() for name in ("paths", "active")}
            for phase in ("before", "after")}}
    release.require(receipt["before"] == {"paths": paths_sha256, "active": active_sha256},
        "migration_storyboard_original_binding_changed")
    for (phase, name), data in saved.items():
        with (backup / (name + "." + phase + ".json")).open("xb") as output:
            os.fchmod(output.fileno(), 0o600); output.write(data); output.flush(); os.fsync(output.fileno())
    release.sync_directory(backup); release.sync_directory(directory)
    receipt_path = root / "migration" / STORYBOARD_RECEIPT
    atomic(receipt_path, receipt)  # Durable intent precedes every runtime/config mutation.
    try:
        _storyboard_refresh((*context[:2], candidate_environment, *context[3:]), candidate, runtime)
        release.require(release.checksum(paths) == paths_sha256 and release.checksum(active_path) == active_sha256,
            "migration_storyboard_original_binding_changed")
        _storyboard_bytes(paths, saved["after", "paths"])
        _storyboard_bytes(active_path, saved["after", "active"])
        release.require(settings(root)[1] == after_digest and release.checksum(active_path) == receipt["after"]["active"],
            "migration_storyboard_commit_readback_failed")
        atomic(receipt_path, {**receipt, "status": "completed"})
    except Exception:
        atomic(receipt_path, {**receipt, "status": "failed"})
        raise
    return {"state": "approved_storyboard_owners_updated", "commit": commit,
        "owners": owners.split(",") if owners else [], "paths_sha256": after_digest,
        "receipt_sha256": release.checksum(receipt_path)}


def restore_storyboard_owners_locked(root, commit, receipt_sha256):
    """Restore one interrupted intent; never replay activation or overwrite unrelated changes."""
    path = root / "migration" / STORYBOARD_RECEIPT
    release.require(type(commit) is str and release.SHA.fullmatch(commit) and type(receipt_sha256) is str
        and release.DIGEST.fullmatch(receipt_sha256), "migration_storyboard_arguments_invalid")
    receipt = storyboard_receipt(root)
    release.require(receipt and receipt["commit"] == commit and receipt["status"] in {"pending", "failed"}
        and release.checksum(path) == receipt_sha256, "migration_storyboard_restore_binding_changed")
    saved, old, _ = _storyboard_backup(root, receipt)
    current_hashes = {name: release.checksum(root / "migration" / (name + ".json")) for name in ("paths", "active")}
    release.require(all(current_hashes[name] in {receipt["before"][name], receipt["after"][name]}
        for name in current_hashes), "migration_storyboard_restore_unrelated_change")
    active = json.loads(saved["before", "active"])
    guard_legacy(root, commit=commit, active=active)
    context = _storyboard_context(root, commit, old)
    try:
        _storyboard_refresh(context, old, receipt["runtime_before"])
        release.require(all(release.checksum(root / "migration" / (name + ".json")) == current_hashes[name]
            for name in current_hashes), "migration_storyboard_restore_unrelated_change")
        for name in ("paths", "active"):
            _storyboard_bytes(root / "migration" / (name + ".json"), saved["before", name])
        release.require(settings(root)[1] == receipt["before"]["paths"] and
            release.checksum(root / "migration" / "active.json") == receipt["before"]["active"],
            "migration_storyboard_restore_readback_failed")
        atomic(path, {**receipt, "status": "restored"})
    except Exception:
        atomic(path, {**receipt, "status": "failed"})
        raise
    return {"state": "approved_storyboard_owners_restored", "commit": commit, "receipt_sha256": release.checksum(path)}


def storyboard_maintenance(root, commit, *, owners=None, paths_sha256=None, active_sha256=None, receipt_sha256=None):
    release.check_host(root)
    import fcntl
    with (root / "release.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if receipt_sha256 is not None:
            return restore_storyboard_owners_locked(root, commit, receipt_sha256)
        return storyboard_owners_locked(root, commit, owners, paths_sha256, active_sha256)



def _default_idle(context):
    # Counts every nonterminal state, including queued/creation_unknown work.
    output = compose(*context[:3], "exec", "-T", "--user", "postgres", "db",
        "psql", "-X", "-U", "postgres", "-d", "sixnine", "-At", "-v", "ON_ERROR_STOP=1",
        "-c", "BEGIN READ ONLY; SELECT count(*) FROM platform_jobs WHERE status NOT IN ('succeeded','failed','cancelled'); COMMIT;", timeout=30)
    release.require(output.decode().splitlines() == ["BEGIN", "0", "COMMIT"],
                    "migration_default_profile_jobs_not_idle")


def _default_profiles_digest(context):
    path = Path(context[2]["SIXNINE_EXECUTION_PROFILES_SECRET_FILE"])
    release.regular(path, root_owned=True, maximum=1048576)
    return release.checksum(path)


def _default_policy_probe(context, profile, expected_default):
    # The approved app validates its own packaged policies. Never import incoming
    # artifact code in the root helper or expose configuration/credentials.
    code = """import hashlib,json,os,pathlib,sys,time
from studio_platform.execution_profiles import read_profiles
from studio_platform.execution_policy import authority_current
path=pathlib.Path(os.environ['SIXNINE_EXECUTION_PROFILES_FILE'])
before=hashlib.sha256(path.read_bytes()).hexdigest()
policies=read_profiles(path)
assert os.environ['SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID']==sys.argv[2]
for recipe in ('h3-base-fl2va-v1','h3-base-ref2va-v1'):
 p=policies.get((sys.argv[1],recipe))
 assert p and p['enabled'] and p['backend']=='wangp-worker'
 assert authority_current(p,time.time(),p['reservation']['expected_runtime_s'])
assert hashlib.sha256(path.read_bytes()).hexdigest()==before
print(json.dumps({'profiles_sha256':before,'modes':['FL','REF']}))
"""
    raw = compose(*context[:3], "exec", "-T", "app", "python", "-B", "-c", code,
                  profile, expected_default, timeout=30)
    value = json.loads(raw)
    digest = _default_profiles_digest(context)
    release.require(value == {"profiles_sha256": digest, "modes": ["FL", "REF"]},
                    "migration_default_profile_policies_unavailable")
    return digest



def _default_db_query(context, sql):
    raw = compose(*context[:3], "exec", "-T", "--user", "postgres", "db",
        "env", "PGOPTIONS=-c statement_timeout=20000 -c lock_timeout=3000",
        "psql", "-X", "-U", "postgres", "-d", "sixnine", "-At", "-v", "ON_ERROR_STOP=1",
        "-c", sql, timeout=30)
    return json.loads(raw)


def _default_record_stop(context, root, receipt):
    # Use the DB clock, not a host/DB clock assumption. Persist before stopping.
    stamp = _default_db_query(context,
        "SELECT (floor(extract(epoch FROM clock_timestamp()) * 1000000)::bigint)::text;")
    stamp = str(stamp)
    release.require(re.fullmatch(r"[0-9]{15,20}", stamp) and len(receipt["db_restarts"]) < 8,
                    "migration_default_profile_stop_time_invalid")
    receipt["db_restarts"].append({"stop_time_us": stamp, "terminated": None, "connections": None})
    atomic(root / "migration" / DEFAULT_PROFILE_RECEIPT, receipt)


def _default_db_cleanup(context, root, receipt):
    item = receipt["db_restarts"][-1]
    stamp = item["stop_time_us"]
    release.require(type(stamp) is str and re.fullmatch(r"[0-9]{15,20}", stamp),
                    "migration_default_profile_stop_time_invalid")
    # No query text, users or credentials are returned. Never terminate active or
    # idle-in-transaction sessions, other roles, or sessions from new processes.
    sql = f"""WITH candidates AS MATERIALIZED (
 SELECT pid, backend_start FROM pg_stat_activity
 WHERE usename IN ('sixnine_app','dstack_control') AND state='idle'
 AND backend_start < to_timestamp({stamp}::numeric / 1000000)
 AND pid <> pg_backend_pid() ORDER BY backend_start, pid LIMIT 257
), ended AS MATERIALIZED (
 SELECT pg_terminate_backend(a.pid) AS ok FROM candidates c
 JOIN pg_stat_activity a ON a.pid=c.pid AND a.backend_start=c.backend_start
 WHERE a.state='idle' AND a.usename IN ('sixnine_app','dstack_control')
 AND (SELECT count(*) FROM candidates) <= 256
) SELECT json_build_object('bounded',(SELECT count(*) FROM candidates)<=256,
 'terminated',(SELECT count(*) FROM ended WHERE ok));"""
    result = _default_db_query(context, sql)
    release.require(type(result) is dict and result.get("bounded") is True
        and type(result.get("terminated")) is int and 0 <= result["terminated"] <= 256,
        "migration_default_profile_db_cleanup_unproven")
    item["terminated"] = result["terminated"]
    atomic(root / "migration" / DEFAULT_PROFILE_RECEIPT, receipt)


def _default_db_connected(context, root, receipt):
    stamp = receipt["db_restarts"][-1]["stop_time_us"]
    sql = f"""SELECT json_object_agg(rolname, json_build_object(
 'total', (SELECT count(*) FROM pg_stat_activity WHERE usename=rolname),
 'fresh', (SELECT count(*) FROM pg_stat_activity WHERE usename=rolname
           AND backend_start >= to_timestamp({stamp}::numeric / 1000000)),
 'limit', CASE WHEN rolconnlimit > 0 THEN rolconnlimit
 ELSE current_setting('max_connections')::int - current_setting('superuser_reserved_connections')::int END))
 FROM pg_roles WHERE rolname IN ('sixnine_app','dstack_control');"""
    result = _default_db_query(context, sql)
    release.require(type(result) is dict and set(result) == {"sixnine_app", "dstack_control"},
                    "migration_default_profile_db_connections_unproven")
    for counts in result.values():
        release.require(type(counts) is dict and set(counts) == {"total", "fresh", "limit"}
            and all(type(v) is int for v in counts.values())
            and 0 < counts["fresh"] <= counts["total"] < counts["limit"],
            "migration_default_profile_db_connections_unproven")
    receipt["db_restarts"][-1]["connections"] = result
    atomic(root / "migration" / DEFAULT_PROFILE_RECEIPT, receipt)


def _default_start(context, config, runtime, root, receipt):
    base, directory, environment, app, dispatch, checker = context
    shared = shared_services(directory_manifest(directory))
    # The previous app namespace is already stopped. Recreate it and wait for
    # health before reconnecting dstack/controller; never mutate provider nodes.
    compose(base, directory, environment, "up", "-d", "--no-deps", "--force-recreate", "app", timeout=300)
    # The new app has started but may be waiting for a free role slot. Reap only
    # pre-stop idle sessions before waiting for readiness; then prove new clients.
    _default_db_cleanup(context, root, receipt)
    release.wait_ready(base, environment)
    compose(base, directory, environment, "up", "-d", "--no-deps", "--force-recreate", *shared, timeout=300)
    deadline = time.monotonic() + 180
    while True:
        try:
            verify_services(base, directory, environment, app, dispatch)
            release.require(_storyboard_runtime(base, directory, environment, flag=DEFAULT_PROFILE_FLAG) == runtime,
                            "migration_default_profile_runtime_changed")
            checker.check(base / "compose.yaml", directory / "overlay.yaml", environment=environment,
                          host_memory_mib=config["host_memory_mib"], check_inputs=True, probe=True)
            release.wait_proxy_stable(base, environment)
            _default_db_connected(context, root, receipt)
            return
        except (release.ReleaseError, ValueError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(3)


def _default_stop(context):
    _default_idle(context)
    compose(*context[:3], "stop", *shared_services(directory_manifest(context[1])), "app", timeout=240)


def default_profile_receipt(root):
    return storyboard_receipt(root, filename=DEFAULT_PROFILE_RECEIPT, purpose="default-profile-only")


def _default_restore(root, commit, receipt, *, stopped_unchanged=False):
    receipt["status"] = "pending"
    other = storyboard_receipt(root)
    release.require(not other or other['status'] not in {'pending','failed'},
                    'migration_storyboard_maintenance_pending_restore_required')
    saved, old, _ = _storyboard_backup(root, receipt, folder="default-profile-maintenance", flag=DEFAULT_PROFILE_FLAG)
    paths = {name: root / "migration" / (name + ".json") for name in ("paths", "active")}
    current = {name: release.checksum(path) for name, path in paths.items()}
    release.require(all(current[name] in {receipt[phase][name] for phase in ("before","after")}
                        for name in paths), "migration_default_profile_restore_unrelated_change")
    context = _storyboard_context(root, commit, old)
    guard_legacy(root, commit=commit, active=json.loads(saved["before", "active"]))
    release.require(_default_profiles_digest(context) == receipt["profiles_sha256"],
                    "migration_default_profile_policy_changed")
    if not stopped_unchanged:
        _default_record_stop(context, root, receipt)
        _default_stop(context)
        _default_idle(context)
    else:
        # A job arrived between precheck and admission stop. No configuration
        # changed; resume the identical old runtime rather than strand that job.
        release.require(current == receipt["before"], "migration_default_profile_restore_unrelated_change")
    for name, path in paths.items():
        release.require(release.checksum(path) == current[name], "migration_default_profile_restore_unrelated_change")
        _storyboard_bytes(path, saved["before", name])
    _default_start(context, old, receipt["runtime_before"], root, receipt)
    _default_policy_probe(context, old["environment"][DEFAULT_PROFILE_FLAG], old["environment"][DEFAULT_PROFILE_FLAG])
    release.require({name:release.checksum(path) for name,path in paths.items()} == receipt["before"],
                    "migration_default_profile_restore_readback_failed")
    atomic(root / "migration" / DEFAULT_PROFILE_RECEIPT, {**receipt, "status":"restored"})
    return {"state":"approved_default_profile_restored", "commit":commit,
            "receipt_sha256":release.checksum(root / "migration" / DEFAULT_PROFILE_RECEIPT)}


def default_profile_only_locked(root, commit, old_profile, new_profile, paths_sha256, active_sha256):
    release.require(type(commit) is str and release.SHA.fullmatch(commit)
        and all(type(v) is str and release.DIGEST.fullmatch(v) for v in (paths_sha256,active_sha256))
        and all(type(v) is str and re.fullmatch(r"[A-Za-z0-9_.-]{1,200}",v) for v in (old_profile,new_profile))
        and old_profile != new_profile, "migration_default_profile_arguments_invalid")
    storyboard_maintenance_fence(root)
    config, digest = settings(root)
    paths, active_path = (root / "migration" / (name + ".json") for name in ("paths","active"))
    active = release._protected_json(active_path, maximum=16384)
    release.require(digest == paths_sha256 and release.checksum(active_path) == active_sha256
        and active.get("active") is True and active.get("version") == 1 and active.get("current") == commit
        and active.get("pending") is None and active.get("paths_sha256") == digest
        and config["environment"][DEFAULT_PROFILE_FLAG] == old_profile,
        "migration_default_profile_original_binding_changed")
    guard_legacy(root, commit=commit, active=active)
    context = _storyboard_context(root, commit, config)
    verify_services(*context[:3], context[3], context[4])
    runtime = _storyboard_runtime(*context[:3], flag=DEFAULT_PROFILE_FLAG)
    _default_idle(context)
    policies_digest = _default_policy_probe(context, new_profile, old_profile)
    candidate = settings_document({**config,"environment":{**config["environment"],DEFAULT_PROFILE_FLAG:new_profile}})
    candidate_environment = {**context[2], DEFAULT_PROFILE_FLAG:new_profile}
    default_profile(context[1], candidate_environment)
    candidate_context = (*context[:2],candidate_environment,*context[3:])
    context[5].check(context[0]/"compose.yaml",context[1]/"overlay.yaml",environment=candidate_environment,
                     host_memory_mib=candidate["host_memory_mib"],check_inputs=True)
    directory = root / "migration" / "default-profile-maintenance"
    if not directory.exists():
        directory.mkdir(mode=0o700); release.sync_directory(directory.parent)
    protected_directory(directory)
    operation = hashlib.sha256((commit+paths_sha256+active_sha256+new_profile+str(time.time_ns())).encode()).hexdigest()
    backup = directory / operation; backup.mkdir(mode=0o700)
    after_paths = json.dumps(candidate,sort_keys=True).encode()
    after_digest = hashlib.sha256(after_paths).hexdigest()
    saved = {("before","paths"):paths.read_bytes(),("before","active"):active_path.read_bytes(),
             ("after","paths"):after_paths,
             ("after","active"):json.dumps({**active,"paths_sha256":after_digest},sort_keys=True).encode()}
    receipt = {"version":1,"purpose":"default-profile-only","commit":commit,"operation_id":operation,
        "status":"pending","runtime_before":runtime,"profiles_sha256":policies_digest,"db_restarts":[],
        **{phase:{name:hashlib.sha256(saved[phase,name]).hexdigest() for name in ("paths","active")}
           for phase in ("before","after")}}
    release.require(receipt["before"]=={"paths":paths_sha256,"active":active_sha256},
                    "migration_default_profile_original_binding_changed")
    for (phase,name),data in saved.items():
        with (backup/(name+"."+phase+".json")).open("xb") as output:
            os.fchmod(output.fileno(),0o600); output.write(data); output.flush(); os.fsync(output.fileno())
    release.sync_directory(backup); release.sync_directory(directory)
    receipt_path = root / "migration" / DEFAULT_PROFILE_RECEIPT
    atomic(receipt_path,receipt)
    changed = False
    stopped = False
    try:
        _default_record_stop(context, root, receipt)
        _default_stop(context)
        stopped = True
        _default_idle(context)  # admission is now stopped: close the TOCTOU gap
        release.require(_default_profiles_digest(context)==policies_digest,
                        "migration_default_profile_policy_changed")
        release.require(release.checksum(paths)==paths_sha256 and release.checksum(active_path)==active_sha256,
                        "migration_default_profile_original_binding_changed")
        changed = True
        _storyboard_bytes(paths,saved["after","paths"])
        _storyboard_bytes(active_path,saved["after","active"])
        _default_start(candidate_context,candidate,runtime,root,receipt)
        release.require(_default_policy_probe(candidate_context,new_profile,new_profile)==policies_digest,
                        "migration_default_profile_policy_changed")
        release.require(settings(root)[1]==after_digest and release.checksum(active_path)==receipt["after"]["active"],
                        "migration_default_profile_commit_readback_failed")
        atomic(receipt_path,{**receipt,"status":"completed"})
    except Exception:
        atomic(receipt_path,{**receipt,"status":"failed"})
        try:
            _default_restore(root,commit,receipt,stopped_unchanged=stopped and not changed)
        except Exception:
            atomic(receipt_path,{**receipt,"status":"failed"})
            raise release.ReleaseError("migration_default_profile_failed_reconciliation_required") from None
        raise release.ReleaseError("migration_default_profile_failed_restored") from None
    return {"state":"approved_default_profile_updated","commit":commit,"old_profile":old_profile,
            "new_profile":new_profile,"paths_sha256":after_digest,"active_sha256":receipt["after"]["active"],
            "receipt_sha256":release.checksum(receipt_path)}


def default_profile_maintenance(root, commit, *, old_profile=None, new_profile=None,
                                paths_sha256=None, active_sha256=None, receipt_sha256=None):
    release.check_host(root)
    import fcntl
    with (root/"release.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if receipt_sha256 is not None:
            release.require(type(commit) is str and release.SHA.fullmatch(commit)
                and type(receipt_sha256) is str and release.DIGEST.fullmatch(receipt_sha256),
                "migration_default_profile_arguments_invalid")
            receipt=default_profile_receipt(root)
            release.require(receipt and receipt["commit"]==commit and receipt["status"] in {"pending","failed","completed"}
                and release.checksum(root/"migration"/DEFAULT_PROFILE_RECEIPT)==receipt_sha256,
                "migration_default_profile_restore_binding_changed")
            path = root/"migration"/DEFAULT_PROFILE_RECEIPT
            atomic(path,{**receipt,"status":"pending"})
            try:
                return _default_restore(root,commit,receipt)
            except Exception:
                atomic(path,{**receipt,"status":"failed"})
                raise
        return default_profile_only_locked(root,commit,old_profile,new_profile,paths_sha256,active_sha256)


def main(argv=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) == 2 and argv[0] == "activate" and release.SHA.fullmatch(argv[1]):
            activate(release.ROOT, argv[1])
            result = {"state": "approved_migration_release_healthy", "commit": argv[1]}
        elif len(argv) == 5 and argv[0] == "storyboard-owners":
            result = storyboard_maintenance(release.ROOT, argv[1], owners="" if argv[2] == "-" else argv[2],
                paths_sha256=argv[3], active_sha256=argv[4])
        elif len(argv) == 3 and argv[0] == "restore-storyboard-owners":
            result = storyboard_maintenance(release.ROOT, argv[1], receipt_sha256=argv[2])
        elif len(argv) == 6 and argv[0] == "default-profile-only":
            result = default_profile_maintenance(release.ROOT, argv[1], old_profile=argv[2],
                new_profile=argv[3], paths_sha256=argv[4], active_sha256=argv[5])
        elif len(argv) == 3 and argv[0] == "restore-default-profile":
            result = default_profile_maintenance(release.ROOT, argv[1], receipt_sha256=argv[2])
        else:
            raise release.ReleaseError("migration_maintenance_arguments_invalid")
        print(json.dumps(result))
        return 0
    except Exception:
        print("Migration maintenance incomplete; preserve state and reconcile the same commit", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
