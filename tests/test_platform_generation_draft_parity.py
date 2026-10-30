"""Browser/server saved-draft contract: CPU-only, no HTTP or model execution."""

import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from studio_platform.capabilities import capabilities
from studio_platform.generation_draft import plan_body, selected_recipe
from studio_platform.settings import Settings
from test_platform_api import project


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT.parent / 'video-studio-design/studio-app/src/cloud-model.js'
BROWSER_MODEL = (
    CANONICAL if CANONICAL.is_file() else ROOT / 'inkseq-web/src/cloud-model.js'
)


@unittest.skipUnless(
    shutil.which('node') and BROWSER_MODEL.is_file(),
    'Node and frontend source required',
)
class DraftParityTests(unittest.TestCase):
    def setUp(self):
        self.document = project()
        self.shot = self.document['entities'][2]
        self.shot['data']['prompt'] = '  A ceramic bird takes flight.  '
        self.shot['data']['h3'] = {'recipeId': 'h3-base-fl2va-v1', 'controls': {}}
        self.caps = capabilities(
            Settings(Path(tempfile.gettempdir()) / 'unused-parity-no-io')
        )
        for recipe in self.caps['recipes']:
            recipe['deployment_preset'] = {
                'applies_to': 'unset_controls_only',
                'controls': {'encoder_device': 'cpu', 'video_decode': 'tiled'},
            }

    def media(self, ident, kind, role=None, selected=None):
        entity = dict(
            id=ident,
            type=kind,
            parentId=None,
            title=ident,
            description='',
            order=0,
            status='draft',
            version=1,
            data={
                'fileId': 'file-' + ident,
                'cloudAssetId': 'receipt-' + ident,
                'metadata': {'source_duration': 12},
                'missingFile': False,
            },
        )
        self.document['entities'].append(entity)
        if role:
            link = {
                'id': 'link-' + ident,
                'source': ident,
                'target': self.shot['id'],
                'role': role,
            }
            self.document['links'].append(link)
            if selected:
                self.shot['data'].setdefault('referenceRanges', {})[link['id']] = {
                    'fileId': entity['data']['fileId'],
                    **selected,
                }
        return entity

    def assert_parity(self):
        recipe = selected_recipe(self.caps['recipes'], self.shot['data']['h3'])
        # Node uses the actual browser pure functions, with a fake derived-asset
        # lookup equivalent to the injected CPU derivative callback below.
        script = """
import fs from 'node:fs';
const {buildPlanPayload, referenceSpecs} = await import(process.argv[1]);
const x=JSON.parse(fs.readFileSync(0,'utf8')), assetMap={};
const derived=(id,r)=>`${id}-clip-${r.start}-${r.end}`;
for(const ref of referenceSpecs(x.project,x.shot,{mode:x.recipe.mode}).references)if(ref.range)assetMap[ref.key]=derived(ref.entity.data.cloudAssetId,ref.range);
const shot=x.project.entities.find(e=>e.id===x.shot);
for(const [i,g] of (shot.data.h3.guides||[]).entries())if(g.source_range){const e=x.project.entities.find(e=>e.id===g.media_id);assetMap['guide:'+i]=derived(e.data.cloudAssetId,g.source_range);}
process.stdout.write(JSON.stringify(buildPlanPayload(x.project,x.shot,{recipe:x.recipe,capabilitiesVersion:x.version,assetMap})));
"""
        value = subprocess.run(
            [
                shutil.which('node'),
                '--input-type=module',
                '-e',
                script,
                BROWSER_MODEL.as_uri(),
            ],
            input=json.dumps(
                {
                    'project': self.document,
                    'shot': self.shot['id'],
                    'recipe': recipe,
                    'version': self.caps['capabilities_version'],
                }
            ),
            encoding='utf-8',
            capture_output=True,
            timeout=20,
            check=True,
        )
        browser = json.loads(value.stdout)
        calls = []

        def derive(ident, start, end):
            calls.append((ident, start, end))
            return f'{ident}-clip-{start:g}-{end:g}'

        server = plan_body(
            copy.deepcopy(self.document), self.shot['id'], self.caps, derive
        )
        # Absent optional first/last frame and explicit null mean the same input.
        for body in (browser, server):
            for field in ('first_frame', 'last_frame'):
                if body['inputs'].get(field) is None:
                    body['inputs'].pop(field, None)
        self.assertEqual(server, browser)
        return calls

    def test_text_defaults_presets_prompt_and_uint64_controls(self):
        self.shot['data']['h3']['controls'] = {
            'seed': '18446744073709551615',
            'duration': 5,
            'steps': 50,
            'generate_audio': False,
        }
        self.assertEqual(self.assert_parity(), [])

    def test_missing_prompt_uses_existing_description(self):
        self.shot['data'].pop('prompt')
        self.assert_parity()

    def test_first_and_last_frame(self):
        self.media('first', 'image', 'firstFrame')
        self.media('last', 'image', 'lastFrame')
        self.assert_parity()

    def test_reference_ranges_guide_reuse_and_audio_switch(self):
        self.shot['data']['h3']['recipeId'] = 'h3-base-ref2va-v1'
        self.media('identity', 'image', 'identity')
        self.media('motion', 'video', 'motion', {'start': 1, 'end': 4})
        self.media('voice', 'audio', 'audio', {'start': 2, 'end': 5})
        self.shot['data']['h3']['video_audio'] = {'motion': False}
        self.shot['data']['h3']['guides'] = [
            {
                'media_id': 'motion',
                'time_seconds': 1,
                'use_audio': False,
                'source_range': {'fileId': 'file-motion', 'start': 1, 'end': 4},
            }
        ]
        self.assertEqual(len(self.assert_parity()), 2)

    def test_inherited_character_order_and_same_media_dedup(self):
        self.shot['data']['h3']['recipeId'] = 'h3-base-ref2va-v1'
        self.media('scene-ref', 'image', 'identity')
        self.document['links'].append(
            {
                'id': 'same-media',
                'source': 'scene-ref',
                'target': self.shot['id'],
                'role': 'reference',
            }
        )
        for name in ('zed', 'amy'):
            self.media(name + '-image', 'image')
            self.document['entities'].append(
                dict(
                    id=name,
                    type='character',
                    parentId=None,
                    title=name,
                    description='',
                    order=0,
                    status='draft',
                    version=1,
                    data={
                        'looks': [{'id': 'look', 'gallery': {'front': name + '-image'}}]
                    },
                )
            )
        self.document['entities'][1]['data']['cast'] = [
            {'characterId': 'zed', 'lookId': 'look'},
            {'characterId': 'amy', 'lookId': 'look'},
        ]
        self.assert_parity()

    def story_visuals(self):
        for ident in ('portrait', 'station', 'room'):
            self.media(ident, 'image')
        for ident, images in (
            ('station-location', ['station']),
            ('room-location', ['room']),
        ):
            self.document['entities'].append(
                dict(
                    id=ident,
                    type='location',
                    parentId=None,
                    title=ident,
                    description='',
                    order=0,
                    status='draft',
                    version=1,
                    data={'referenceAssetIds': images},
                )
            )
        self.document['entities'].append(
            dict(
                id='actor',
                type='character',
                parentId=None,
                title='Actor',
                description='',
                order=0,
                status='draft',
                version=1,
                data={'looks': [{'id': 'look', 'gallery': {'front': 'portrait'}}]},
            )
        )
        self.document['entities'][1]['data'].update(
            cast=[{'characterId': 'actor', 'lookId': 'look'}],
            locationId='station-location',
        )

    @unittest.skipUnless(
        CANONICAL.is_file(),
        'New local UX source is deliberately not in the production snapshot',
    )
    def test_canonical_ref_input_mode_inherits_cast_and_shot_location_override(self):
        self.story_visuals()
        self.shot['data']['h3'] = {'inputMode': 'ref', 'controls': {}}
        self.shot['data']['locationId'] = 'room-location'
        self.assertEqual(self.assert_parity(), [])

    @unittest.skipUnless(
        CANONICAL.is_file(),
        'New local UX source is deliberately not in the production snapshot',
    )
    def test_canonical_fl_keeps_cast_locations_without_adding_ordinary_inputs(self):
        self.story_visuals()
        self.media('frame', 'image', 'firstFrame')
        self.shot['data']['h3'].update(inputMode='ref')  # Explicit FL recipe wins.
        self.assertEqual(self.assert_parity(), [])

    @unittest.skipUnless(
        CANONICAL.is_file(),
        'New local UX source is deliberately not in the production snapshot',
    )
    def test_canonical_linked_locations_and_explicit_media_deduplicate(self):
        self.story_visuals()
        self.shot['data']['h3']['recipeId'] = 'h3-base-ref2va-v1'
        self.document['links'].extend(
            [
                {
                    'id': 'direct-station',
                    'source': 'station',
                    'target': self.shot['id'],
                    'role': 'reference',
                },
                {
                    'id': 'linked-room',
                    'source': 'room-location',
                    'target': self.shot['id'],
                    'role': 'location',
                },
                {
                    'id': 'linked-station',
                    'source': 'station-location',
                    'target': self.shot['id'],
                    'role': 'reference',
                },
            ]
        )
        self.assertEqual(self.assert_parity(), [])


if __name__ == '__main__':
    unittest.main()
