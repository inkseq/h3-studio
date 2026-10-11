"""Offline VM portability and candidate hardware boundaries; no provider calls."""
from contextlib import contextmanager, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from studio_platform import lium_bootstrap as boot
from studio_platform.lium_provider import LiumError
from studio_platform import wangp_bootstrap
from studio_platform.inference.wangp_contract import EngineManifest
from studio_platform.inference.wangp_profile_compiler import compile_settings
from studio_platform.runtime_catalog import PROFILE_IDS, engine_manifest, get_profile, timing_hint, validate_manifest
from studio_platform.runtime_hosts import wangp_session as session
from test_platform_wangp_profiles import example
from comfy_workflow import native_output_spec

PROFILE = 'h3-pruned-rank8-int8-pro6000-quanto-int8-vae-int8-sdpa-p4-lowram-v1'
GIB = 1024**3


class CandidateProfileTests(unittest.TestCase):
    def test_partial_native_evidence_keeps_weights_runtime_and_pending_envelopes(self):
        source, candidate = get_profile(PROFILE_IDS[0]), get_profile(PROFILE)
        for key in ('components', 'runtime', 'model_id', 'models', 'minimum_ram_bytes', 'minimum_free_vram_bytes'):
            self.assertEqual(candidate[key], source[key])
        self.assertEqual({case['id'] for case in candidate['verified_cases']},
                         {'pro6000-candidate-2', 'pro6000-candidate-8'})
        self.assertEqual(candidate['tested_topology']['gpus_per_host'], 1)
        self.assertEqual(candidate['tested_topology']['active_jobs_per_gpu'], 1)
        self.assertEqual(len(candidate['qualification_cases']), 6)
        self.assertEqual(candidate['validation']['scope'], 'pending_hardware_qualification')
        self.assertIs(candidate['validation']['production_adapter_verified'], False)
        for case in candidate['qualification_cases']:
            self.assertNotIn('measurements', case)
            request, metadata = example(PROFILE, case)
            settings = compile_settings(request, metadata, native_output_spec(request),
                {key: 'handle-'+key for key in metadata}, PROFILE)
            self.assertEqual(settings['config'], source['runtime']['task_config'])
            self.assertEqual(settings['num_inference_steps'], case['steps'])
            self.assertIsNone(timing_hint(PROFILE, case['mode'], case['width'], case['height'],
                case['frames'], case['fps'], case['steps'], case['input_roles']))

    def test_hardware_has_a_new_manifest_and_cannot_widen_the_existing_profile(self):
        old, new = engine_manifest(PROFILE_IDS[0], 'fl'), engine_manifest(PROFILE, 'fl')
        self.assertNotEqual(old.digest, new.digest)
        self.assertNotIn('hardware_admission', old.document)
        self.assertEqual(new.document['hardware_admission'], get_profile(PROFILE)['hardware_admission'])
        changed = new.document
        changed['hardware_admission']['minimum_total_vram_bytes'] = 30*GIB
        with self.assertRaisesRegex(ValueError, 'profile_manifest_mismatch'):
            validate_manifest(EngineManifest.from_dict(changed))

    def test_explicit_gpu_family_and_total_memory_are_enforced_before_lock_creation(self):
        class LockReached(Exception): pass
        def device(profile, name, gib, capability=(12, 0)):
            cuda = NS(is_available=lambda:True, device_count=lambda:1,
                get_device_properties=lambda _:NS(total_memory=gib*GIB, uuid='GPU-11111111-1111-4111-8111-111111111111'),
                get_device_name=lambda _:name, get_device_capability=lambda _:capability)
            with patch.dict(sys.modules, {'fcntl':NS()}), patch.object(session.os, 'O_NOFOLLOW', 0, create=True), \
                    patch.object(session.os, 'open', side_effect=LockReached):
                session._lock_profile_device(NS(cuda=cuda), get_profile(profile))
        with self.assertRaises(LockReached):
            device(PROFILE, 'NVIDIA RTX PRO 6000 Blackwell Server Edition', 96)
        with self.assertRaises(LockReached):
            device(PROFILE_IDS[0], 'NVIDIA GeForce RTX 5090', 32)
        for profile,name,size in ((PROFILE,'NVIDIA GeForce RTX 5090',32),
                (PROFILE_IDS[0],'NVIDIA RTX PRO 6000 Blackwell Server Edition',96),
                (PROFILE,'NVIDIA RTX PRO 6000 Blackwell Server Edition',48)):
            with self.subTest(profile=profile,name=name,size=size), self.assertRaisesRegex(ValueError,'gpu_mismatch'):
                device(profile,name,size)


class VMMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proc, self.groups = self.root/'proc', self.root/'cgroups'
        (self.proc/'self').mkdir(parents=True); self.groups.mkdir()
        (self.proc/'meminfo').write_text('MemTotal: '+str(512*GIB//1024)+' kB\n'
            'MemAvailable: '+str(120*GIB//1024)+' kB\n')
        (self.proc/'self/cgroup').write_text('0::/\n')
        (self.groups/'cgroup.controllers').write_text('memory cpu\n')

    def group(self, path, *, limit, used, cache=0, dirty=0):
        path.mkdir(parents=True, exist_ok=True)
        (path/'memory.max').write_text(str(limit))
        (path/'memory.current').write_text(str(used))
        (path/'memory.stat').write_text(f'file {cache}\nactive_file {cache}\nfile_dirty {dirty}\n')

    def test_vm_root_without_memory_max_uses_observed_available_ram(self):
        self.assertEqual(session._available_profile_ram(self.proc,self.groups), 120*GIB)
        self.assertEqual(session._effective_total_profile_ram(self.proc,self.groups), 512*GIB)

    def test_allocated_capacity_is_distinct_from_used_or_available_ram(self):
        self.group(self.groups,limit=29*GIB,used=5*GIB,cache=2*GIB,dirty=GIB)
        self.assertEqual(session._effective_total_profile_ram(self.proc,self.groups),29*GIB)
        self.assertEqual(session._available_profile_ram(self.proc,self.groups),25*GIB)

    def test_total_capacity_respects_host_total_and_tighter_ancestor(self):
        (self.proc/'self/cgroup').write_text('0::/system.slice/wangp.service\n')
        self.group(self.groups/'system.slice',limit=29*GIB,used=5*GIB)
        self.group(self.groups/'system.slice/wangp.service',limit=40*GIB,used=GIB)
        self.assertEqual(session._effective_total_profile_ram(self.proc,self.groups),29*GIB)
        (self.proc/'meminfo').write_text('MemTotal: '+str(24*GIB//1024)+' kB\n'
            'MemAvailable: '+str(20*GIB//1024)+' kB\n')
        self.assertEqual(session._effective_total_profile_ram(self.proc,self.groups),24*GIB)

    def test_total_capacity_does_not_hide_a_child_below_a_constrained_mount_root(self):
        self.group(self.groups,limit=40*GIB,used=GIB)
        self.group(self.groups/'runtime',limit=28*GIB,used=GIB)
        (self.proc/'self/cgroup').write_text('0::/runtime\n')
        self.assertEqual(session._effective_total_profile_ram(self.proc,self.groups),28*GIB)
        # Preserve the previous profiles' container-root available contract.
        self.assertEqual(session._available_profile_ram(self.proc,self.groups),39*GIB)

    def test_unknown_total_or_invalid_limits_cannot_become_capacity_proof(self):
        original=(self.proc/'meminfo').read_text()
        for value in ('MemAvailable: 1024 kB\n','MemTotal: 0 kB\nMemAvailable: 1024 kB\n'):
            (self.proc/'meminfo').write_text(value)
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'total_ram_unconfirmed'):
                session._effective_total_profile_ram(self.proc,self.groups)
        (self.proc/'meminfo').write_text(original)
        for limit in ('-1','0','invalid'):
            (self.groups/'memory.max').write_text(limit)
            with self.subTest(limit=limit),self.assertRaisesRegex(ValueError,'cgroup_invalid'):
                session._effective_total_profile_ram(self.proc,self.groups)

    def test_missing_nonroot_limits_cannot_be_treated_as_unlimited(self):
        (self.proc/'self/cgroup').write_text('0::/system.slice/wangp.service\n')
        ancestor=self.groups/'system.slice'; leaf=ancestor/'wangp.service'
        self.group(ancestor,limit=29*GIB,used=GIB)
        self.group(leaf,limit=40*GIB,used=GIB)
        for path in (leaf/'memory.max',ancestor/'memory.max'):
            value=path.read_text(); path.unlink()
            with self.subTest(missing=path.parent.name),self.assertRaisesRegex(ValueError,'cgroup_invalid'):
                session._effective_total_profile_ram(self.proc,self.groups)
            path.write_text(value)

    def test_unreadable_limit_never_becomes_a_capacity_proof(self):
        self.group(self.groups,limit=29*GIB,used=GIB)
        path=self.groups/'memory.max'; original=Path.read_text
        def read(candidate,*args,**kwargs):
            if candidate==path:
                raise PermissionError('private inaccessible path')
            return original(candidate,*args,**kwargs)
        with patch.object(Path,'read_text',read),self.assertRaisesRegex(ValueError,'^wangp_profile_cgroup_invalid$'):
            session._effective_total_profile_ram(self.proc,self.groups)

    def test_root_limit_metadata_cannot_be_mistaken_for_legitimate_absence(self):
        path=self.groups/'memory.max'; original=Path.lstat
        # A dangling symlink does not satisfy is_file, but lstat still proves
        # it exists. Use its metadata so this check works without OS privileges.
        def link(candidate,*args,**kwargs):
            if candidate==path:
                return NS(st_mode=session.stat.S_IFLNK|0o777)
            return original(candidate,*args,**kwargs)
        with patch.object(Path,'lstat',link),self.assertRaisesRegex(ValueError,'^wangp_profile_cgroup_invalid$'):
            session._effective_total_profile_ram(self.proc,self.groups)
        def inaccessible(candidate,*args,**kwargs):
            if candidate==path:
                raise PermissionError('private inaccessible path')
            return original(candidate,*args,**kwargs)
        with patch.object(Path,'lstat',inaccessible),self.assertRaisesRegex(ValueError,'^wangp_profile_cgroup_invalid$'):
            session._effective_total_profile_ram(self.proc,self.groups)
        path.mkdir()
        with self.assertRaisesRegex(ValueError,'^wangp_profile_cgroup_invalid$'):
            session._effective_total_profile_ram(self.proc,self.groups)

    def test_container_limit_retains_clean_cache_and_excludes_dirty_bytes(self):
        self.group(self.groups,limit=110*GIB,used=80*GIB,cache=20*GIB,dirty=5*GIB)
        self.assertEqual(session._available_profile_ram(self.proc,self.groups),45*GIB)

    def test_vm_nested_group_respects_the_tighter_parent_limit(self):
        (self.proc/'self/cgroup').write_text('0::/system.slice/wangp.service\n')
        self.group(self.groups/'system.slice',limit=100*GIB,used=80*GIB)
        self.group(self.groups/'system.slice/wangp.service',limit=110*GIB,used=5*GIB)
        self.assertEqual(session._available_profile_ram(self.proc,self.groups),20*GIB)
        with patch.object(session, '_available_profile_ram', return_value=95*GIB), \
                self.assertRaisesRegex(ValueError,'memory_headroom_insufficient'):
            session._profile_memory_admission(NS(cuda=NS(mem_get_info=lambda _:(90*GIB,96*GIB))),get_profile(PROFILE))

    def test_malformed_or_unavailable_v2_membership_never_bypasses_admission(self):
        for value in ('0::/../outside\n','2:memory:/legacy\n','0::/missing\n'):
            (self.proc/'self/cgroup').write_text(value)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError,'cgroup_invalid'):
                session._available_profile_ram(self.proc,self.groups)
            with self.subTest(total_scope=value),self.assertRaisesRegex(ValueError,'cgroup_invalid'):
                session._effective_total_profile_ram(self.proc,self.groups)


class TargonTransportTests(unittest.TestCase):
    def test_start_extracts_only_hash_bound_preparer_and_never_replays_its_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            remote = directory/'remote'; remote.mkdir()
            intent_id = '11111111-1111-4111-8111-111111111111'
            work = directory/'work'; (work/intent_id).mkdir(parents=True)
            content = b'# inert preparation fixture; never executed\n'
            archive = remote/'wangp-package.tar.gz'
            with tarfile.open(archive,'w:gz') as stream:
                member = tarfile.TarInfo('deploy/wangp/targon_prepare.py'); member.size=len(content)
                stream.addfile(member,io.BytesIO(content))
                other = tarfile.TarInfo('unrelated.py'); other.size=5
                stream.addfile(other,io.BytesIO(b'never'))
            host = wangp_bootstrap.WanGPSSHHost.__new__(wangp_bootstrap.WanGPSSHHost)
            host.config = NS(provider='targon',work_dir=work,runtime_python='/venv/main/bin/python')
            host.remote_root = str(remote)
            @contextmanager
            def sftp():
                yield NS(open=lambda *args:io.BytesIO(b'synthetic-token'))
            host._source_sftp=sftp
            def execute(code):
                output=io.StringIO()
                with redirect_stdout(output):
                    exec(compile(code.replace("Path('/workspace/h3-studio')",'Path('+repr(str(remote))+')'),
                        '<offline-targon-start>','exec'),{})
                return json.loads(output.getvalue())
            host.run=execute
            identity={'provider':'targon','intent_id':intent_id,'hard_deadline':2000000000,
                'sources':{'wangp-package.tar.gz':hashlib.sha256(archive.read_bytes()).hexdigest()}}
            with patch.dict(sys.modules,{'fcntl':NS(LOCK_EX=2,LOCK_NB=4,flock=lambda *args:None)}), \
                    patch.object(wangp_bootstrap,'private_token_file',return_value='synthetic-token'), \
                    patch.object(wangp_bootstrap.os,'fchmod',lambda *args:None,create=True), \
                    patch('subprocess.Popen',return_value=NS(pid=123)) as process:
                self.assertEqual(host.start(identity),{'state':'started','pid':123})
                self.assertEqual(host.start(identity),{'state':'already_reserved'})
            process.assert_called_once()
            args=process.call_args.args[0]
            self.assertEqual(args[:3],['python3','-u',str(remote/'targon-prepare.py')])
            self.assertEqual(args[3:],['--config',str(remote/'wangp-runtime.json'),'--slot-key',intent_id,
                '--token-file',str(remote/'wangp-token')])
            self.assertEqual((remote/'targon-prepare.py').read_bytes(),content)
            self.assertFalse((remote/'unrelated.py').exists())

    def test_provider_uid_is_safe_but_does_not_change_lium_uuid_validation(self):
        self.assertEqual(boot.provider_instance_id('targon','workload_abc-123'),'workload_abc-123')
        for value in ('../escape','x/y','x y','x'*129):
            with self.assertRaises(boot.BootError): boot.provider_instance_id('targon',value)
        with self.assertRaises(LiumError): boot.provider_instance_id('lium','workload_abc-123')

    def test_username_is_fixed_by_explicit_provider_and_nondefault_port_is_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = NS(provider='targon',known_hosts_file=root/'known',ssh_key_file=root/'key',trust_first_host_key=True)
            client = Mock()
            with patch('paramiko.SSHClient',return_value=client):
                host = boot.SSHHost(config,{'host':'198.51.100.17','port':24317,'username':'ubuntu'})
            self.assertEqual(client.connect.call_args.kwargs['username'],'ubuntu')
            self.assertEqual(client.connect.call_args.kwargs['port'],24317)
            for username in ('root','ubuntu;id','nobody'):
                with self.subTest(username=username), patch('paramiko.SSHClient',return_value=Mock()), \
                        self.assertRaisesRegex(boot.BootError,'ssh_user_mismatch'):
                    boot.SSHHost(config,{'host':'198.51.100.17','port':24317,'username':username})
            host.close()

    def test_root_sftp_uses_fixed_sudo_command_and_closes_its_channel(self):
        host = wangp_bootstrap.WanGPSSHHost.__new__(wangp_bootstrap.WanGPSSHHost)
        host.config = NS(provider='targon')
        channel = Mock(); sftp = Mock()
        host.client = NS(get_transport=lambda:NS(open_session=lambda **kwargs:channel))
        with patch('paramiko.SFTPClient',return_value=sftp):
            with host._source_sftp() as result:
                self.assertIs(result,sftp)
        channel.exec_command.assert_called_once_with('sudo -n /usr/lib/openssh/sftp-server')
        channel.invoke_subsystem.assert_not_called()
        channel.close.assert_called_once()
        sftp.close.assert_called_once()

    def test_slot_identity_retains_targon_instead_of_relabeling_as_lium(self):
        profile = get_profile(PROFILE)
        config = NS(provider='targon',local_port=31001,profile_slot_index=0,deployment_profile_id=PROFILE,
            recipe_ids=('h3-base-fl2va-v1',),model_id=profile['model_id'],configuration_id='targon-candidate',
            engine_manifest_digest=engine_manifest(PROFILE,'fl').digest,output_delivery='native-frames-v1')
        intent = {'id':'11111111-1111-4111-8111-111111111111','pool':'candidate','provider':'targon',
            'provider_instance_id':'workload_abc-123'}
        slot = wangp_bootstrap.make_slot(config,intent,{'gpus':[{'uuid':'GPU-11111111-1111-4111-8111-111111111111'}]},Path.cwd()/'state')
        self.assertEqual(slot.spec.provider,'targon')
        self.assertEqual(slot.spec.worker_id,'targon-11111111111141118111111111111111-gpu0')
        self.assertEqual(slot.spec.instance_id,'workload_abc-123')
        with self.assertRaisesRegex(boot.BootError,'slot_provider_mismatch'):
            wangp_bootstrap.make_slot(config,{**intent,'provider':'lium'},{},Path('/state'))


if __name__ == '__main__':
    unittest.main()
