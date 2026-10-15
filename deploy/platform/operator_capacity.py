#!/usr/bin/python3
"""Protected host activation for the existing operator controller.

No default action, schema migration, budget initialization or provider call.
Run start under the prepared systemd unit; TERM requests a graceful drain only.
The API sees immutable public deployment metadata and the existing database;
only the controller receives the existing SSH identity and private credential.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

import release
from check_config import validate as validate_base

ROOT = Path('/srv/sixnine/operator-capacity')
RUNTIME = ROOT/'runtime.json'
REGISTRY = ROOT/'registry.json'
PROFILES = ROOT/'execution-profiles.json'
SETTINGS = ROOT/'operator-settings.json'
KEY = Path('/srv/sixnine/gpu-scaler/identity/key')
METADATA = Path('/srv/sixnine/lium-runtime-import.json')
TARGON_METADATA = Path('/srv/sixnine/targon-runtime-import.json')
TARGON_GUARD = Path('/srv/sixnine/targon-guard')
SERVICE = 'operator-controller'
FACTORY = 'studio_platform.operator_runtime:create_controller_from_stdin'
MODULE = 'studio_platform.operator_controller'
CANONICAL_ENTRYPOINT = 'from studio_platform.operator_controller import main; raise SystemExit(main())'
LABEL = 'com.sixnine.operator.prepared-hash'
CPU_HEALTH = "import json,urllib.request; h=json.load(urllib.request.urlopen('http://127.0.0.1:8845/healthz',timeout=5)); assert h['auth_ready'] and not h['generation_enabled'] and not h['render_enabled'] and not h['cloud_creation_enabled'] and h['execution_backend']=='disabled'"
GPU_HEALTH = CPU_HEALTH.replace("not h['generation_enabled']", "h['generation_enabled']").replace("=='disabled'", "=='wangp-worker'")


def bind(path, readonly=False):
    return {'type':'bind','source':Path(path).as_posix(),'target':Path(path).as_posix(),
        'read_only':readonly,'bind':{'create_host_path':False}}


def atomic(path, value):
    temporary = path.with_suffix('.next')
    release.require(not temporary.is_symlink(), 'operator_record_link_forbidden')
    with temporary.open('w',encoding='utf-8') as output:
        json.dump(value,output,sort_keys=True,allow_nan=False)
        output.flush(); os.fsync(output.fileno())
    temporary.chmod(0o640)
    temporary.replace(path)
    release.sync_directory(path.parent)


def protected_file(path, maximum=1024*1024):
    path = Path(path)
    release.require(path.is_absolute() and '..' not in path.parts, 'operator_path_invalid')
    for parent in path.parents:
        release.protected_directory(parent)
    release.regular(path,root_owned=True,maximum=maximum)
    return path


def credential_references(runtime):
    references={}
    legacy={'secret_arn','secret_version_id'}&set(runtime)
    release.require(not legacy or legacy=={'secret_arn','secret_version_id'},'operator_runtime_identity_mismatch')
    if legacy: references['lium']={key:runtime[key] for key in legacy}
    providers=runtime.get('provider_credentials',{})
    release.require(isinstance(providers,dict) and not set(providers)-{'targon'},'operator_runtime_identity_mismatch')
    for provider,reference in providers.items():
        release.require(isinstance(reference,dict) and set(reference)=={'secret_arn','secret_version_id'},
            'operator_runtime_identity_mismatch')
        references[provider]=reference
    release.require(bool(references) and all(isinstance(value,str) and value
        for reference in references.values() for value in reference.values()),'operator_runtime_identity_mismatch')
    return references


def credential_files(runtime):
    files={}
    for provider,reference in credential_references(runtime).items():
        path=METADATA if provider=='lium' else TARGON_METADATA
        protected_file(path)
        metadata=release._protected_json(path)
        release.require(reference['secret_arn']==metadata.get('secret_arn')
            and reference['secret_version_id']==metadata.get('version_id')
            and metadata.get('service')==provider and metadata.get('profile')==provider+'--rig-root',
            'operator_runtime_identity_mismatch')
        files[str(path)]=release.checksum(path)
    return files


def guard_configuration(runtime):
    release.require(runtime.get('cleanup_guard_dir')==TARGON_GUARD.as_posix(),'operator_guard_path_invalid')
    for path in (TARGON_GUARD,*TARGON_GUARD.parents,TARGON_GUARD/'receipts'):
        release.protected_directory(path)
    requests=TARGON_GUARD/'requests'
    info=requests.lstat()
    release.require(stat.S_ISDIR(info.st_mode) and not requests.is_symlink()
        and (os.name=='nt' or info.st_uid==10001 and not info.st_mode&0o077),'operator_guard_requests_not_private')
    path=protected_file(TARGON_GUARD/'config.json')
    value=release._protected_json(path)
    reference=credential_references(runtime)['targon']
    fields={'schema_version','directory','org_slug','resource_names','image_names','approval_start','approval_end',
        'maximum_seconds','secret_arn','secret_version_id'}
    release.require(set(value)==fields and type(value.get('schema_version')) is int and value['schema_version']==1
        and value.get('directory')==TARGON_GUARD.as_posix()
        and value.get('secret_arn')==reference['secret_arn']
        and value.get('secret_version_id')==reference['secret_version_id']
        and isinstance(value.get('org_slug'),str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}',value['org_slug'])
        and all(isinstance(value.get(key),list) and 1<=len(value[key])<=128
            and all(isinstance(item,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',item) for item in value[key])
            for key in ('resource_names','image_names'))
        and all(type(value.get(key)) in (int,float) and math.isfinite(value[key]) for key in ('approval_start','approval_end'))
        and value['approval_start']<value['approval_end'] and type(value.get('maximum_seconds')) is int
        and 120<=value['maximum_seconds']<=14400, 'operator_guard_identity_mismatch')
    return value,{str(path):release.checksum(path)}


def configuration():
    """Read only non-secret, root-controlled configuration and exact sources."""
    for folder in (ROOT,ROOT/'sources',ROOT/'provider-manifests'):
        release.protected_directory(folder)
    for folder in (ROOT/'control',ROOT/'tmp'):
        info = folder.lstat()
        release.require(stat.S_ISDIR(info.st_mode) and not folder.is_symlink()
            and (os.name=='nt' or info.st_uid==10001 and not info.st_mode&0o077),
            'operator_runtime_directory_not_private')
    key = release.regular(KEY)
    release.require(os.name=='nt' or key.st_uid==10001 and stat.S_IMODE(key.st_mode)==0o400,
        'operator_ssh_identity_permissions_invalid')
    for parent in KEY.parents:
        release.protected_directory(parent)
    for path in (RUNTIME,REGISTRY,PROFILES,SETTINGS):
        protected_file(path)
    runtime = release._protected_json(RUNTIME)
    paths = {'registry_file':REGISTRY.as_posix(),'work_dir':str(ROOT/'control'),
        'ssh_key_file':KEY.as_posix(),'known_hosts_file':str(ROOT/'control'/'known_hosts')}
    release.require(all(runtime.get(k)==v for k,v in paths.items())
        and runtime.get('credential_source')=='aws_runtime',
        'operator_runtime_identity_mismatch')
    references=credential_references(runtime)
    credential_hashes=credential_files(runtime)
    registry = release._protected_json(REGISTRY,maximum=1024*1024)
    release.require(set(registry)=={'schema_version','bindings'} and registry['schema_version']==1
        and isinstance(registry['bindings'],list) and 1<=len(registry['bindings'])<=128,
        'operator_registry_invalid')
    default = default_profile()
    release.require(any(b.get('runtime_profile_id')==default and b.get('enabled') is True
        for b in registry['bindings']), 'operator_default_profile_not_enabled')
    files = {**credential_hashes,**{str(p):release.checksum(p) for p in (RUNTIME,REGISTRY,PROFILES,SETTINGS)}}
    guard=None
    if 'targon' in references:
        guard,guard_hashes=guard_configuration(runtime)
        files.update(guard_hashes)
    else:
        release.require('cleanup_guard_dir' not in runtime,'operator_guard_configuration_conflict')
    for binding in registry['bindings']:
        provider=binding.get('launch',{}).get('provider')
        release.require(provider in references,'operator_runtime_provider_unconfigured')
        boot = binding['boot']
        path = Path(boot['provider_manifest_file'])
        release.require(path.parent==ROOT/'provider-manifests','operator_provider_manifest_path_invalid')
        protected_file(path)
        files[str(path)] = release.checksum(path)
        if provider=='targon':
            manifest=release._protected_json(path)
            release.require(manifest.get('org_slug')==guard.get('org_slug')
                and manifest.get('resource_name') in guard.get('resource_names',[])
                and manifest.get('image_name') in guard.get('image_names',[])
                and type(guard.get('maximum_seconds')) is int
                and type(manifest.get('max_lifetime_seconds',7200)) is int
                and guard['maximum_seconds']>=manifest.get('max_lifetime_seconds',7200)
                and type(guard.get('approval_start')) in (int,float)
                and type(guard.get('approval_end')) in (int,float)
                and guard['approval_start']<manifest.get('approved_until',0)<=guard['approval_end'],
                'operator_guard_manifest_mismatch')
        release.require(isinstance(boot['source_dirs'],list) and isinstance(boot['source_sha256'],list)
            and len(boot['source_dirs'])==len(boot['source_sha256']), 'operator_sources_invalid')
        for folder, hashes in zip(boot['source_dirs'],boot['source_sha256']):
            folder = Path(folder)
            release.require(folder.is_relative_to(ROOT/'sources') and folder!=ROOT/'sources',
                'operator_source_path_invalid')
            release.require(set(hashes)=={'wangp-bootstrap.py','wangp-manifest.json','wangp-runtime.json','wangp-package.tar.gz'},
                'operator_source_set_invalid')
            for name, digest in hashes.items():
                path = protected_file(folder/name,16*1024**2)
                release.require(release.checksum(path)==digest,'operator_source_changed')
                files[str(path)] = digest
    return runtime, files


def shared_mounts():
    return [bind(path,True) for path in (RUNTIME,REGISTRY,PROFILES,ROOT/'sources',ROOT/'provider-manifests')]


def default_profile():
    value = release._protected_json(SETTINGS)
    release.require(set(value)=={'schema_version','default_deployment_profile_id'}
        and type(value['schema_version']) is int and value['schema_version']==1
        and isinstance(value['default_deployment_profile_id'],str)
        and re.fullmatch(r'[A-Za-z0-9_.-]{1,200}',value['default_deployment_profile_id']),
        'operator_default_profile_invalid')
    return value['default_deployment_profile_id']


def controller(image, runtime=None):
    guard_mounts=[]
    if runtime is not None and 'targon' in credential_references(runtime):
        release.require(runtime.get('cleanup_guard_dir')==TARGON_GUARD.as_posix(),'operator_guard_path_invalid')
        guard_mounts=[bind(TARGON_GUARD,True),bind(TARGON_GUARD/'requests')]
    return {'image':image,'pull_policy':'never','user':'10001:10001','init':True,'restart':'no',
        'read_only':True,'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],
        'pids_limit':192,'mem_limit':1024**3,'cpus':.75,
        'environment':{'SIXNINE_DATA':'/data','SIXNINE_DATABASE_URL_FILE':'/run/secrets/app_database_url',
            'PUBLIC_ORIGIN':'https://inkseq.com','SIXNINE_AUTH_MODE':'password',
            'SIXNINE_GENERATION_ENABLED':'1','SIXNINE_RENDER_ENABLED':'0',
            'SIXNINE_EXECUTION_BACKEND':'wangp-worker','SIXNINE_CLOUD_CREATION_ENABLED':'0',
            'SIXNINE_STORAGE_PROVIDER':'local','SIXNINE_EXECUTION_PROFILES_FILE':PROFILES.as_posix(),
            'AWS_EC2_METADATA_DISABLED':'true'},
        # Accidental compose up is inert. Only host launch appends --enabled.
        'command':['python','-m',MODULE,'--factory',FACTORY,'--config',RUNTIME.as_posix()],
        'secrets':[{'source':'app_database_url','target':'/run/secrets/app_database_url'}],
        'volumes':shared_mounts()+guard_mounts+[bind(KEY,True),bind(ROOT/'control'),
            {'type':'bind','source':(ROOT/'tmp').as_posix(),'target':'/tmp','bind':{'create_host_path':False}},
            {'type':'bind','source':'/srv/sixnine/platform-data','target':'/data','bind':{'create_host_path':False}}],
        'networks':{'database':{},'edge':{}},
        'depends_on':{'db':{'condition':'service_healthy','required':True}},
        'logging':{'driver':'json-file','options':{'max-size':'10m','max-file':'3'}}}


def overlay(image, profile_id, runtime=None, *, owners='superdan,supervan'):
    return {'services':{'app':{'environment':{'H3_OPERATOR_RUNTIME_CONFIG':RUNTIME.as_posix(),
        'SIXNINE_EXECUTION_PROFILES_FILE':PROFILES.as_posix(),'SIXNINE_GENERATION_ENABLED':'1',
        'SIXNINE_EXECUTION_BACKEND':'wangp-worker','AWS_EC2_METADATA_DISABLED':'true',
        'SIXNINE_DEFAULT_DEPLOYMENT_PROFILE_ID':profile_id,
        'SIXNINE_OPERATOR_CAPACITY_OWNERS':owners},
        'volumes':shared_mounts(),'healthcheck':{'test':['CMD','python','-c',GPU_HEALTH]}},
        SERVICE:controller(image,runtime)}}


def validate_rendered(value, directory, version, image, profile_id, runtime=None, *, owners='superdan,supervan'):
    config = copy.deepcopy(value)
    if version in ('2.38.2','v2.38.2'):
        for service in config['services'].values():
            for mount in service.get('volumes',[]):
                if mount.get('type')=='bind' and mount.get('bind')=={}:
                    mount['bind']={'create_host_path':False}
    actual = config['services'].pop(SERVICE,None)
    if isinstance(actual,dict):
        if actual.get('mem_limit')==str(1024**3): actual['mem_limit']=1024**3
        if actual.get('entrypoint',False) is None: del actual['entrypoint']
        for mount in actual.get('volumes',[]):
            if mount.get('read_only') is False: del mount['read_only']
    expected = controller(image,runtime)
    for mount in expected['volumes']:
        if mount.get('read_only') is False: del mount['read_only']
    release.require(actual==expected,'operator_controller_configuration_invalid')
    app = config['services']['app']
    wanted = overlay(image,profile_id,runtime,owners=owners)['services']['app']
    for key,value in wanted['environment'].items():
        release.require(app['environment'].get(key)==value,'operator_app_environment_invalid')
        del app['environment'][key]
    app['environment'].update(SIXNINE_GENERATION_ENABLED='0',SIXNINE_EXECUTION_BACKEND='disabled')
    for mount in wanted['volumes']:
        release.require(app['volumes'].count(mount)==1,'operator_app_mount_invalid')
        app['volumes'].remove(mount)
    release.require(app['image']==image and app['healthcheck']['test']==wanted['healthcheck']['test'],
        'operator_app_identity_invalid')
    app['healthcheck']['test']=['CMD','python','-c',CPU_HEALTH]
    return validate_base(config,deployment_directory=directory,compose_version=version)


def compose_args(directory,*arguments):
    return ['compose','--project-directory',str(directory),'-f',str(directory/'compose.yaml'),
        '-f',str(ROOT/'overlay.json'),*arguments]


def compose(directory,environment,*arguments,timeout=180):
    return release.command(compose_args(directory,*arguments),environment=environment,timeout=timeout)


def approved_current():
    commit,directory,environment = release.current_application(release.ROOT)
    image = json.loads(release.command(['image','inspect',environment['SIXNINE_IMAGE']],environment=environment))[0]
    expected = release.manifest(directory,commit)
    release.verify_running_app(directory,environment,{**expected,
        'archive_image_ids':release.validate_image_archive(directory/'image.tar.gz',expected)})
    return commit,directory,environment,image['Id']


def no_competing_controller(environment):
    for folder in ('gpu-acceptance','gpu-scaler'):
        marker = release.ROOT/folder/'active.json'
        if marker.exists() or marker.is_symlink():
            release.require(release._protected_json(marker).get('active') is False,'legacy_controller_requires_restore')
    for name in ('gpu-worker','gpu-controller',SERVICE):
        raw = release.command(['ps','--quiet','--filter','label=com.docker.compose.project=sixnine-platform',
            '--filter','label=com.docker.compose.service='+name],environment=environment,timeout=20)
        release.require(not raw.strip(),'operator_competing_controller')


PROBE = '''import json,time
from sqlalchemy import create_engine,text
from studio_platform.settings import Settings
from studio_platform.operator_runtime import create_registry,load_runtime_config
from studio_platform.execution_profiles import read_profiles
from pathlib import Path
settings=Settings.from_environment()
runtime=load_runtime_config(RUNTIME)
registry=create_registry(RUNTIME)
policies=read_profiles(PROFILES)
assert policies
for policy in policies.values():
 if not policy['enabled']: continue
 matches=[b for b in registry.bindings.values() if b.enabled
  and b.runtime_profile_id==policy['deployment_profile_id']
  and list(b.recipe_ids)==policy['recipe_ids']
  and all(getattr(b,k)==policy[k] for k in ('pool','configuration_id','model_id','engine_manifest_digest'))]
 assert matches, 'operator_execution_binding_mismatch'
engine=create_engine(settings.database_url)
from studio_platform.repository import instance_intents, manually_reviewed_inactive
from sqlalchemy import select
with engine.connect() as conn:
 conn.execute(text('SET TRANSACTION READ ONLY'))
 queries={
 'active_jobs': "SELECT count(*) FROM platform_jobs WHERE status NOT IN ('succeeded','failed','cancelled')",
 'unsafe_attempts': "SELECT count(*) FROM platform_attempts WHERE status NOT IN ('succeeded','failed','cancelled','deferred') OR ((submission_started_at IS NOT NULL OR upstream_task_id IS NOT NULL) AND upstream_stopped != 1)",
 'live_instances': "SELECT count(*) FROM platform_instance_intents WHERE state != 'destroyed'",
 'bound_workers': "SELECT count(*) FROM platform_registered_workers WHERE current_job_id IS NOT NULL OR (state != 'retired' AND expires_at > :now)",
 'pending_commands': "SELECT count(*) FROM platform_operator_capacity_commands WHERE state IN ('accepted','running','waiting','unknown')",
 'billing_pending': "SELECT count(*) FROM platform_instance_intents i WHERE NOT EXISTS (SELECT 1 FROM platform_budget_reservations r WHERE r.reference_type = 'instance' AND r.reference_id=i.id) OR EXISTS (SELECT 1 FROM platform_budget_reservations r WHERE r.reference_type='instance' AND r.reference_id=i.id AND r.state='reserved')"}
 counts={k:conn.execute(text(q),{'now':time.time()}).scalar_one() for k,q in queries.items()}
 counts['live_instances']=sum(not manually_reviewed_inactive(conn,row) for row in
  conn.execute(select(instance_intents).where(instance_intents.c.state!='destroyed')).mappings())
engine.dispose()
print(json.dumps({'schema_version':1,'observed_at':time.time(),'config_valid':True,'provider_calls_enabled':False,'counts':counts}))
'''


def probe(directory,environment):
    # One-off private metadata/SQL validation; no factory, loader, schema or policy writes.
    script = PROBE.replace('RUNTIME',repr(RUNTIME.as_posix())).replace('PROFILES',repr(PROFILES.as_posix()))
    raw = compose(directory,environment,'run','--rm','-T','--no-deps','--entrypoint','python',SERVICE,'-c',script,timeout=60)
    release.require(len(raw)<=8192,'operator_probe_invalid')
    value = json.loads(raw)
    counts = value.get('counts')
    release.require(value.get('schema_version')==1 and value.get('config_valid') is True
        and value.get('provider_calls_enabled') is False and isinstance(counts,dict)
        and set(counts)=={'active_jobs','unsafe_attempts','live_instances','bound_workers','pending_commands','billing_pending'}
        and all(type(v) is int and v>=0 for v in counts.values()),'operator_probe_invalid')
    return value


def require_quiet(value):
    release.require(all(value['counts'][key]==0 for key in
        ('active_jobs','unsafe_attempts','live_instances','bound_workers','pending_commands')),
        'operator_obligations_require_reconciliation')


def unit_text():
    return '''[Unit]
Description=Sixnine protected operator capacity supervisor
After=docker.service network-online.target
Requires=docker.service
[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 /opt/sixnine-release/operator_capacity.py start
Restart=no
KillMode=process
TimeoutStopSec=infinity
SendSIGKILL=no
UMask=0077
[Install]
WantedBy=multi-user.target
'''


def prepare():
    runtime,files = configuration()
    commit,directory,environment,image_id = approved_current()
    no_competing_controller(environment)
    marker = ROOT/'active.json'
    if marker.exists() or marker.is_symlink():
        release.require(release._protected_json(marker).get('active') is False,'operator_barrier_retained')
    atomic(ROOT/'overlay.json',overlay(environment['SIXNINE_IMAGE'],default_profile(),runtime))
    version = release.command(['compose','version','--short'],environment=environment).decode().strip()
    validate_rendered(json.loads(compose(directory,environment,'config','--format','json')),directory,version,environment['SIXNINE_IMAGE'],default_profile(),runtime)
    checked = probe(directory,environment)
    require_quiet(checked)
    record = {'schema_version':1,'commit':commit,'image_id':image_id,
        'runtime_config_sha256':release.canonical_hash(runtime),'files':files}
    path = ROOT/'prepared.json'
    if path.exists():
        release.require(release._protected_json(path,maximum=2*1024**2)==record,'operator_prepared_identity_changed')
    else: atomic(path,record)
    unit = ROOT/'supervisor.service'
    if unit.exists():
        protected_file(unit)
        release.require(unit.read_text()==unit_text(),'operator_supervisor_unit_changed')
    else:
        with unit.open('x') as output: output.write(unit_text())
        unit.chmod(0o644)
    return {'state':'prepared_not_started','commit':commit,'config_valid':True,'billing_pending':checked['counts']['billing_pending']}


def prepared():
    """Resolve the immutable execution release, independently of the current API."""
    runtime,files = configuration()
    value = release._protected_json(ROOT/'prepared.json',maximum=2*1024**2)
    commit=value.get('commit')
    release.require(isinstance(commit,str) and release.SHA.fullmatch(commit),'operator_prepared_identity_changed')
    directory=release.ROOT/'releases'/commit
    release.approved_manifest(release.ROOT,directory,commit)
    expected=release.manifest(directory,commit)
    environment=release.deployment_environment(release.ROOT/'site.env',commit)
    release.approved_configuration(directory,environment)
    identities=release.validate_image_archive(directory/'image.tar.gz',expected)
    images=json.loads(release.command(['image','inspect',environment['SIXNINE_IMAGE']],environment=environment))
    release.require(isinstance(images,list) and len(images)==1 and images[0].get('Id') in identities,
        'operator_execution_image_unapproved')
    image_id=images[0]['Id']
    release.require(type(value.get('schema_version')) is int and value=={'schema_version':1,'commit':commit,'image_id':image_id,
        'runtime_config_sha256':release.canonical_hash(runtime),'files':files},'operator_prepared_identity_changed')
    original=release._protected_json(ROOT/'overlay.json')
    owners=original.get('services',{}).get('app',{}).get('environment',{}).get('SIXNINE_OPERATOR_CAPACITY_OWNERS')
    release.require(owners in ('superdan','superdan,supervan') and
        original==overlay(environment['SIXNINE_IMAGE'],default_profile(),runtime,owners=owners),
        'operator_overlay_changed')
    return runtime,value,directory,environment


def app_overlay(*, admission):
    """Separately protected API permissions; never edit the execution overlay."""
    release.require(admission in ('open','closed'),'operator_admission_invalid')
    if admission=='closed': return {'services':{'app':{}}}
    original=release._protected_json(ROOT/'overlay.json')
    app=copy.deepcopy(original['services']['app'])
    settings=ROOT/'app-settings.json'
    if settings.exists() or settings.is_symlink():
        value=release._protected_json(settings)
        release.require(set(value)=={'schema_version','capacity_owners'}
            and type(value.get('schema_version')) is int and value['schema_version']==1
            and value['capacity_owners'] in (['superdan'],['superdan','supervan']),
            'operator_app_settings_invalid')
        app['environment']['SIXNINE_OPERATOR_CAPACITY_OWNERS']=','.join(value['capacity_owners'])
    return {'services':{'app':app}}


def validate_app_rendered(value,directory,version,image,expected):
    """Validate only the app overlay; no controller service is rendered or changed."""
    config=copy.deepcopy(value)
    app=config.get('services',{}).get('app',{})
    wanted=expected['services']['app']
    for key,value in wanted.get('environment',{}).items():
        release.require(app.get('environment',{}).get(key)==value,'operator_app_environment_invalid')
        del app['environment'][key]
    app['environment'].update(SIXNINE_GENERATION_ENABLED='0',SIXNINE_EXECUTION_BACKEND='disabled')
    if version in ('2.38.2','v2.38.2'):
        for mount in app.get('volumes',[]):
            if mount.get('type')=='bind' and mount.get('bind')=={}: mount['bind']={'create_host_path':False}
    for mount in wanted.get('volumes',[]):
        release.require(app.get('volumes',[]).count(mount)==1,'operator_app_mount_invalid')
        app['volumes'].remove(mount)
    release.require(app.get('image')==image and app.get('healthcheck',{}).get('test')==
        wanted.get('healthcheck',{'test':['CMD','python','-c',CPU_HEALTH]})['test'],
        'operator_app_identity_invalid')
    app['healthcheck']['test']=['CMD','python','-c',CPU_HEALTH]
    return validate_base(config,deployment_directory=directory,compose_version=version)


def pin_for(value):
    digest = release.canonical_hash(value)
    return {'version':1,'active':True,'commit':value['commit'],'image_id':value['image_id'],
        'prepared_hash':digest,'runtime_config_sha256':value['runtime_config_sha256'],
        'container_name':'sixnine-operator-'+digest[:20],'state':'launching','admission':'closed'}


def checked_pin(value):
    pin = release._protected_json(ROOT/'active.json')
    expected = pin_for(value)
    release.require(type(pin.get('version')) is int
        and set(pin) in (set(expected),set(expected)|{'controller_id'})
        and all(pin.get(k)==v for k,v in expected.items() if k not in ('active','state','admission'))
        and type(pin.get('active')) is bool and pin.get('admission') in ('open','closed'),
        'operator_active_identity_changed')
    return pin


def inspect_controller(environment,pin):
    rows = json.loads(release.command(['inspect',pin['container_name']],environment=environment,timeout=20))
    release.require(isinstance(rows,list) and len(rows)==1,'operator_container_unknown')
    value = rows[0]; labels = value.get('Config',{}).get('Labels',{})
    release.require(value.get('Name')=='/'+pin['container_name'] and value.get('Image')==pin['image_id']
        and labels.get('com.docker.compose.project')=='sixnine-platform'
        and labels.get('com.docker.compose.service')==SERVICE and labels.get(LABEL)==pin['prepared_hash'],
        'operator_container_identity_changed')
    return value.get('State',{})


def receipt(pin, *, fresh=False):
    path = ROOT/'control'/'controller-status.json'
    info = release.regular(path,maximum=16384)
    release.require(os.name=='nt' or info.st_uid==10001 and stat.S_IMODE(info.st_mode)==0o600,
        'operator_status_permissions_invalid')
    value = json.loads(path.read_text())
    release.require(value.get('schema_version')==1
        and value.get('runtime_config_sha256')==pin['runtime_config_sha256']
        and isinstance(value.get('controller_id'),str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',value['controller_id'])
        and (not pin.get('controller_id') or pin['controller_id']==value['controller_id'])
        and type(value.get('observed_at')) in (int,float) and math.isfinite(value['observed_at'])
        and value.get('cloud_removal_confirmed') is False and value.get('billing_settled') is False,
        'operator_status_identity_invalid')
    if fresh: release.require(0<=time.time()-value['observed_at']<=30,'operator_status_stale')
    return value


def launch(directory,environment,runtime,pin, *, loader_factory=None,targon_loader_factory=None,popen=subprocess.Popen):
    from studio_platform.lium_runtime_aws import AwsLiumLoader
    references=credential_references(runtime)
    loaders=[]
    envelope = None
    try:
        credentials={}
        for provider,reference in references.items():
            if provider=='lium':
                factory=loader_factory or AwsLiumLoader
            else:
                from studio_platform.targon_runtime_aws import AwsTargonLoader
                factory=targon_loader_factory or AwsTargonLoader
            loader=factory(reference['secret_arn'],reference['secret_version_id'])
            loaders.append(loader)
            loaded=loader(provider,profile=provider+'--rig-root')
            credentials[provider]={'secret_arn':reference['secret_arn'],'version_id':reference['secret_version_id'],
                'payload':{'schema_version':1,'service':loaded.service,'profile':loaded.profile,
                    'base_url':loaded.base_url,'primary_key_variable':loaded.primary_key_variable,'api_key':loaded.api_key}}
        envelope=credentials['lium'] if set(credentials)=={'lium'} else {'schema_version':2,'credentials':credentials}
        payload = json.dumps(envelope,separators=(',',':')).encode()
        release.require(len(payload)<=(24576 if set(credentials)=={'lium'} else 49152),'operator_credential_envelope_limit')
        args = [release.DOCKER,'--host','unix:///var/run/docker.sock',*compose_args(directory,'run','-T',
            '--no-deps','--name',pin['container_name'],'--label',LABEL+'='+pin['prepared_hash'],
            # Import main canonically: -m defines a second __main__ class that
            # fails the factory-result isinstance check before the first tick.
            '--entrypoint','python',SERVICE,'-c',CANONICAL_ENTRYPOINT,'--factory',FACTORY,'--config',RUNTIME.as_posix(),'--enabled')]
        process = popen(args,env=environment,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,start_new_session=True)
        try: process.stdin.write(payload); process.stdin.close()
        except Exception: raise release.ReleaseError('operator_credential_delivery_unknown') from None
        return process
    finally:
        if envelope is not None: envelope.clear()
        for loader in loaders:
            try: loader.close()
            except Exception: pass


def close_admission(directory,environment,pin):
    # Resolve the current approved compatible API, retaining the execution pin.
    closed = {**pin,'admission':'closed'}
    atomic(ROOT/'active.json',closed)
    release.restore_current_cpu_locked(release.ROOT,operator_pin=closed)


def request_drain(directory,environment,pin):
    close_admission(directory,environment,pin)
    state = inspect_controller(environment,pin)
    if state.get('Running') is True:
        release.command(['kill','--signal','TERM',pin['container_name']],environment=environment,timeout=20)
    return state


def restore(directory,environment,value):
    pin = checked_pin(value)
    close_admission(directory,environment,pin)
    state = inspect_controller(environment,pin)
    release.require(state.get('Running') is False and state.get('Restarting') is False
        and state.get('OOMKilled') is False and state.get('Status')=='exited' and state.get('ExitCode')==0,
        'operator_exit_unconfirmed_barrier_retained')
    proof = receipt(pin)
    release.require(proof.get('state')=='shutdown_complete' and proof.get('local_connections_released') is True,
        'operator_collection_unconfirmed_barrier_retained')
    checked = probe(directory,environment)
    require_quiet(checked)
    atomic(ROOT/'active.json',{**pin,'active':False,'state':'restored','admission':'closed'})
    return {'state':'cpu_restored','billing_pending':checked['counts']['billing_pending'],
        'billing_settled':checked['counts']['billing_pending']==0,'budgets_unchanged':True}


def start(*, clock=time.monotonic,sleep=time.sleep):
    import fcntl
    stopping = [False]; handlers = {}
    def signal_stop(*_): stopping[0]=True
    process = directory = environment = value = pin = None
    for sig in (signal.SIGTERM,signal.SIGINT):
        handlers[sig]=signal.getsignal(sig); signal.signal(sig,signal_stop)
    try:
        with (release.ROOT/'release.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            runtime,value,directory,environment = prepared()
            release.require(approved_current()[0]==value['commit'],'operator_start_release_changed')
            no_competing_controller(environment)
            # No replay after any launch intent, including failed/unknown delivery.
            release.require(not (ROOT/'active.json').exists(),'operator_previous_launch_requires_reconciliation')
            release.require(not (ROOT/'control'/'controller-status.json').exists(),'operator_previous_status_requires_reconciliation')
            require_quiet(probe(directory,environment))
            release.require(not stopping[0],'operator_start_interrupted')
            pin = pin_for(value); atomic(ROOT/'active.json',pin)
            process = launch(directory,environment,runtime,pin)
            deadline = clock()+90
            while True:
                if stopping[0]: break
                release.require(process.poll() is None,'operator_early_exit_barrier_retained')
                try:
                    state=inspect_controller(environment,pin); proof=receipt(pin,fresh=True)
                    if (state.get('Running') is True and state.get('Restarting') is False
                            and state.get('OOMKilled') is False and proof.get('state')=='running'):
                        pin={**pin,'state':'running','controller_id':proof['controller_id']}
                        atomic(ROOT/'active.json',pin)
                        compose(directory,environment,'up','-d','--no-deps','app')
                        release.wait_ready(directory,environment)
                        pin={**pin,'admission':'open'};atomic(ROOT/'active.json',pin)
                        break
                except (OSError,ValueError,release.ReleaseError):
                    pass
                release.require(clock()<deadline,'operator_startup_unconfirmed_barrier_retained')
                sleep(2)
        # Never time out/terminate the Docker client or its collectors. A lost
        # connection is uncertainty; the durable marker continues to block CD.
        while process.poll() is None:
            if stopping[0]:
                with (release.ROOT/'release.lock').open('a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX)
                    request_drain(directory,environment,checked_pin(value))
                stopping[0]=False
            sleep(2)
        with (release.ROOT/'release.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            release.require(process.returncode==0,'operator_client_exit_unknown_barrier_retained')
            return restore(directory,environment,value)
    except Exception:
        if pin is not None:
            # Close new admission and request TERM, but never stop/remove/replay.
            try:
                with (release.ROOT/'release.lock').open('a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX)
                    request_drain(directory,environment,checked_pin(value))
            except Exception: pass
        raise
    finally:
        for sig,handler in handlers.items(): signal.signal(sig,handler)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(json.dumps({'state':'disabled','action_required':True}));return 0
    try:
        release.require(args in ([a] for a in ('prepare','validate','start','status','drain','restore')),
            'operator_action_invalid')
        release.check_host(release.ROOT)
        if args==['start']: result=start()
        else:
            import fcntl
            with (release.ROOT/'release.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                if args==['prepare']: result=prepare()
                else:
                    _,value,directory,environment=prepared()
                    if args==['validate']: result=probe(directory,environment)
                    elif args==['restore']: result=restore(directory,environment,value)
                    else:
                        pin=checked_pin(value)
                        if args==['drain']: request_drain(directory,environment,pin)
                        result={'state':'drain_requested' if args==['drain'] else 'observed',
                            'container':inspect_controller(environment,pin),'controller':receipt(pin),
                            'admission':pin['admission'],'barrier_retained':pin['active']}
        print(json.dumps(result,sort_keys=True));return 0
    except Exception as error:
        code=str(error) if isinstance(error,release.ReleaseError) else 'operator_host_check_failed'
        print(json.dumps({'state':'incomplete','code':code}));return 1


if __name__=='__main__':
    raise SystemExit(main())
