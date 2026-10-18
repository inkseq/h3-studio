"""Optional Google configuration cannot gate database hydration; synthetic only."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from studio_platform.google_title_config import GoogleTitleConfigError, validate_config

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('title_hydration_test',ROOT/'deploy/platform/runtime_secrets_aws.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
FAKE={'enabled':True,'service':'gemini','profile':'gemini--user-supplied',
      'base_url':'https://generativelanguage.googleapis.com','api_key':'synthetic-private-key-for-test-only'}


class TitleHydrationTests(unittest.TestCase):
    def test_host_validator_loads_with_no_site_packages_and_an_unrelated_cwd(self):
        # Host copies this shared stdlib-only validator beside its protected helper.
        source=ROOT/'studio_platform/google_title_config.py'
        with tempfile.TemporaryDirectory() as tmp:
            program="import runpy; ns=runpy.run_path("+repr(str(source))+"); assert ns['validate_config']({'enabled':False}) is None"
            result=subprocess.run([sys.executable,'-I','-S','-c',program],cwd=tmp,capture_output=True,check=False)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))

    @unittest.skipIf(os.name=='nt','Host atomic file ownership exercised on Linux CI')
    def test_unavailable_or_invalid_google_disables_only_title_and_preserves_bound_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);root.chmod(0o700)
            path=root/'google_titles'
            original=Path.lstat
            def root_stat(path):
                values=list(original(path));values[4]=0;values[5]=0 if path==root else 10001
                return os.stat_result(values)
            api=Mock()
            with patch.object(module,'ROOT',root),patch('pathlib.Path.lstat',root_stat),patch.object(module.os,'fchown'):
                api.get_secret_value.return_value={'SecretString':json.dumps(FAKE)}
                module.hydrate_titles(api)
                self.assertEqual(validate_config(json.loads(path.read_text())).api_key,FAKE['api_key'])
                with path.open() as bound:
                    api.get_secret_value.side_effect=RuntimeError('synthetic-private upstream text')
                    with patch('builtins.print') as output: module.hydrate_titles(api)
                    self.assertEqual(json.loads(path.read_text()),{'enabled':False})
                    self.assertEqual(json.loads(bound.read()),FAKE)
                    self.assertNotIn('synthetic-private',str(output.call_args))
                api.get_secret_value.side_effect=None
                for value in ('{}','{"enabled":false,"enabled":true}',json.dumps({**FAKE,'base_url':'https://wrong.example'})):
                    api.get_secret_value.return_value={'SecretString':value}
                    with patch('builtins.print'): module.hydrate_titles(api)
                    self.assertEqual(json.loads(path.read_text()),{'enabled':False})
                self.assertEqual(path.stat().st_mode & 0o777,0o440)
                self.assertFalse(list(root.glob('.google-titles-*')))


if __name__=='__main__':
    unittest.main()
