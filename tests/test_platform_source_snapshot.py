"""Shared location media remains part of the immutable generation source."""
import copy
import unittest

from studio_platform.project_validation import validate_project
from studio_platform.source_snapshot import source_snapshot
from test_platform_api import project


class LocationSourceTests(unittest.TestCase):
    def setUp(self):
        self.project = project()
        template = self.project['entities'][0]
        for ident, kind in [('room','location'), ('room-image','image'), ('other-image','image')]:
            self.project['entities'].append({**copy.deepcopy(template), 'id':ident, 'type':kind,
                'parentId':None, 'data':{}})
        self.project['entities'][1]['data']['locationId'] = 'room'
        self.project['entities'][3]['data']['referenceAssetIds'] = ['room-image']

    def test_location_media_changes_invalidate_plan_without_manual_version_change(self):
        validate_project(self.project)
        original = source_snapshot(self.project, 'shot-one')
        self.project['entities'][4]['data']['fileId'] = 'replacement'
        self.assertNotEqual(original, source_snapshot(self.project, 'shot-one'))

    def test_unused_media_does_not_invalidate_plan(self):
        original = source_snapshot(self.project, 'shot-one')
        self.project['entities'][5]['data']['fileId'] = 'replacement'
        self.assertEqual(original, source_snapshot(self.project, 'shot-one'))

    def test_location_structure_rejects_wrong_type_missing_and_duplicate_references(self):
        for kind, mutate in [
            ('wrong location', lambda p: p['entities'][1]['data'].update(locationId='room-image')),
            ('missing image', lambda p: p['entities'][3]['data'].update(referenceAssetIds=['missing'])),
            ('duplicate image', lambda p: p['entities'][3]['data'].update(referenceAssetIds=['room-image','room-image'])),
            ('wrong image', lambda p: p['entities'][3]['data'].update(referenceAssetIds=['scene-one']))]:
            with self.subTest(kind=kind):
                value=copy.deepcopy(self.project)
                mutate(value)
                with self.assertRaises(ValueError):
                    validate_project(value)


if __name__ == '__main__':
    unittest.main()
