"""Storyboard contracts: synthetic entities/receipts, no model/provider calls."""
import copy
import unittest
from unittest.mock import patch

from sqlalchemy import func, select

from studio_platform.capabilities import capabilities
from studio_platform.generation_draft import read_draft, plan_body
from studio_platform.guided import delete_entity
from studio_platform.project_validation import validate_project
from studio_platform.repository import jobs
from studio_platform.source_snapshot import source_snapshot
from studio_platform.storyboard import contract
from test_platform_api import project
import test_platform_generation_draft as draft_fixtures


def storyboard_project():
    value = project()
    template = value["entities"][0]
    def entity(ident, kind, data=None, description="", title=None):
        result = {**copy.deepcopy(template), "id": ident, "type": kind, "parentId": None,
            "title": title or ident, "description": description, "data": data or {}}
        value["entities"].append(result)
        return result
    for ident, kind in (("portrait", "image"), ("night", "image"), ("room-image", "image"),
                        ("sword-image", "image"), ("voice", "audio"), ("voice-other", "audio"),
                        ("ambient", "audio"), ("motion", "video"), ("extra", "image")):
        entity(ident, kind, {"cloudAssetId": "receipt-" + ident, "fileId": "file-" + ident,
            "metadata": {"source_duration": 10, "kind": kind}})
    entity("hero", "character", {"looks": [
        {"id": "day", "name": "白天", "description": "蓝色制服", "version": 1,
            "gallery": {}, "imageIds": ["portrait", "night"]},
        {"id": "rain", "name": "雨夜", "description": "黑色雨衣", "version": 1,
            "gallery": {}, "imageIds": ["night"], "voiceId": "whisper"}],
        "defaultLookId": "day", "voices": [{"id": "main", "assetId": "voice", "clipStart": 1},
            {"id": "whisper", "assetId": "voice-other", "clipStart": 2}], "mainVoiceId": "main"},
        "沉着的旅人", "林岚")
    entity("room", "location", {"versions": [{"id": "evening", "name": "傍晚", "description": "金色斜光",
        "imageIds": ["room-image"], "version": 1}], "defaultVersionId": "evening",
        "ambient": {"assetId": "ambient", "clipStart": 0}}, "旧车站", "站台")
    entity("sword", "prop", {"imageIds": ["sword-image"]}, "刻有符文的铁剑", "铁剑")
    value["journey"] = {"storyboard": {"style": "电影动画，墨蓝夜色"}}
    value["entities"][2]["data"].update(prompt="拔剑后向前冲刺", cast=[{"characterId": "hero", "lookId": ""}],
        h3={"recipeId": "h3-base-ref2va-v1", "controls": {"duration": 5, "resolution": "480P", "seed": "42"}},
        storyboard={"version": 1, "mode": "ref", "quality": "draft", "place": {"locationId": "room", "versionId": ""},
            "propIds": ["sword"], "firstFrameId": "portrait", "lastFrameId": "night", "continuePrevious": False,
            "extraRefs": [{"entityId": "extra"}, {"entityId": "motion", "clipStart": 1}], "previousFrame": None})
    return value


def cap(image_count=1, audio_count=1, total=3):
    from studio_platform.capabilities import RECIPES, control_schema
    return {"capabilities_version": "sixnine-h3-base-20261004-v1", "deployment_profiles": [],
        "recipes": [{"id": ident, "mode": mode, "controls": control_schema(), "limits": {
            "max_images": image_count, "max_audios": audio_count, "max_videos": 1,
            "max_total_files": total, "max_total_audio_duration": 3 * audio_count,
            "max_total_video_duration": 3}} for ident, mode in RECIPES.items()]}


class StoryboardPureTests(unittest.TestCase):
    def setUp(self):
        self.project = storyboard_project()
        self.find = lambda ident: next(e for e in self.project["entities"] if e["id"] == ident)

    def test_old_document_unchanged_and_schema_discoverable(self):
        old = project()
        before = copy.deepcopy(old)
        validate_project(old)
        self.assertEqual(old, before)
        self.assertNotIn("storyboard", read_draft(old, "shot-one")[0])
        self.assertEqual(contract()["fields"]["shot.data.storyboard"]["properties"]["mode"]["enum"], ["fl", "ref"])

    def test_profile_validation_and_invalid_binding_ranges(self):
        validate_project(self.project)
        changes = [lambda p: p["entities"][-3]["data"]["voices"][0].update(clipStart=9),
            lambda p: p["entities"][-3]["data"]["voices"][0].update(assetId="portrait"),
            lambda p: p["entities"][-3]["data"].update(defaultLookId="missing"),
            lambda p: p["entities"][-3]["data"]["looks"][0].update(imageIds=[{}]),
            lambda p: p["entities"][-2]["data"]["versions"][0].update(imageIds=["voice"]),
            lambda p: p["entities"][-1]["data"].update(imageIds=["sword-image", "sword-image"]),
            lambda p: p["entities"][2]["data"]["storyboard"].update(mode=[]),
            lambda p: p["entities"][2]["data"]["storyboard"].update(firstFrameId=None)]
        for change in changes:
            value = copy.deepcopy(self.project)
            change(value)
            with self.assertRaises(ValueError):
                validate_project(value)

    def test_default_override_prompt_order_priority_and_dynamic_limits(self):
        before = copy.deepcopy(self.project)
        draft, issues = read_draft(self.project, "shot-one", capabilities=cap())
        self.assertEqual(issues, [])
        self.assertEqual([part["kind"] for part in draft["storyboard"]["prompt_parts"]], ["style", "place", "cast", "prop", "action"])
        self.assertEqual(draft["prompt"], "\n".join(part["text"] for part in draft["storyboard"]["prompt_parts"]))
        self.assertEqual(draft["inputs"]["images"], [{"asset_id": "receipt-portrait"}])
        self.assertEqual(draft["inputs"]["audios"], [{"asset_id": "receipt-voice", "source_range": {"start": 1, "end": 4}}])
        self.assertEqual(draft["inputs"]["videos"][0]["include_audio"], False)
        self.assertEqual([ref["entity_id"] for ref in draft["storyboard"]["references"] if not ref["sent"]], ["room-image", "sword-image", "extra", "ambient"])
        wide, _ = read_draft(self.project, "shot-one", capabilities=cap(4, 2, 7))
        self.assertEqual(len(wide["inputs"]["images"]), 4)
        self.assertEqual(len(wide["inputs"]["audios"]), 2)
        self.assertEqual(self.project, before)
        self.find("shot-one")["data"]["cast"][0]["lookId"] = "rain"
        override, _ = read_draft(self.project, "shot-one", capabilities=cap())
        self.assertEqual(override["inputs"]["images"][0]["asset_id"], "receipt-night")
        self.assertEqual(override["inputs"]["audios"][0]["asset_id"], "receipt-voice-other")

    def test_inactive_mode_preserved_and_explicit_profile_not_substituted(self):
        shot = self.find("shot-one")
        saved = copy.deepcopy(shot["data"]["storyboard"])
        shot["data"]["storyboard"]["mode"] = "fl"
        shot["data"]["h3"]["recipeId"] = "h3-base-fl2va-v1"
        draft, issues = read_draft(self.project, "shot-one", capabilities=cap())
        self.assertFalse(issues)
        self.assertEqual(draft["inputs"]["images"], [])
        self.assertEqual(draft["inputs"]["first_frame"], {"asset_id": "receipt-portrait"})
        self.assertEqual(shot["data"]["storyboard"]["extraRefs"], saved["extraRefs"])
        shot["data"]["storyboard"]["mode"] = "ref"
        shot["data"]["h3"]["recipeId"] = "h3-base-ref2va-v1"
        self.assertEqual(read_draft(self.project, "shot-one", capabilities=cap())[0]["inputs"]["first_frame"], None)
        shot["data"]["h3"]["deploymentProfileId"] = "explicit-profile"
        shot["data"]["storyboard"]["quality"] = "final"
        draft, issues = read_draft(self.project, "shot-one", capabilities=cap())
        self.assertEqual(draft["deployment_profile_id"], "explicit-profile")
        self.assertIn("当前参考输入范围尚未读取，请刷新生成能力", issues)

    def test_full_native_ref_anchors_survive_without_consuming_ordinary_reference_slots(self):
        profile='h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6'
        shot=self.find('shot-one')
        shot['data']['h3']['deploymentProfileId']=profile
        shot['data']['storyboard'].update(place=None,propIds=[],extraRefs=[])
        current=cap(1,1,2)
        current['deployment_profiles']=[{'id':profile,'generation_support':{'ref':{
            'limits':copy.deepcopy(current['recipes'][1]['limits'])}}}]
        before=copy.deepcopy(self.project)
        draft,issues=read_draft(self.project,'shot-one',capabilities=current)
        self.assertFalse(issues)
        self.assertEqual(draft['inputs']['first_frame'],{'asset_id':'receipt-portrait'})
        self.assertEqual(draft['inputs']['last_frame'],{'asset_id':'receipt-night'})
        self.assertEqual(draft['inputs']['images'],[{'asset_id':'receipt-portrait'}])
        self.assertNotIn('首尾帧输入已保留，全能参考模式不发送',draft['storyboard']['notes'])
        self.assertEqual(self.project,before)
        # Native REF can use anchors alone. It must not invent another
        # reference merely to satisfy the old reference-only authoring rule.
        shot['data']['cast']=[]
        shot['data']['storyboard'].update(place=None,propIds=[],extraRefs=[])
        only_anchors,issues=read_draft(self.project,'shot-one',capabilities=current)
        self.assertFalse(issues)
        self.assertEqual(only_anchors['inputs']['images'],[])
        self.assertEqual(only_anchors['inputs']['first_frame'],{'asset_id':'receipt-portrait'})

    def test_full_native_storyboard_preserves_explicit_existing_guide_associations(self):
        profile='h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6'
        shot=self.find('shot-one')
        shot['data']['cast']=[]
        shot['data']['storyboard'].update(place=None,propIds=[],extraRefs=[{'entityId':'extra'}])
        shot['data']['h3'].update(deploymentProfileId=profile,guides=[
            {'media_id':'night','time_seconds':1,'use_audio':False},
            {'media_id':'night','time_seconds':2,'use_audio':False}])
        current=cap(1,0,1)
        current['deployment_profiles']=[{'id':profile,'generation_support':{'ref':{
            'limits':copy.deepcopy(current['recipes'][1]['limits'])}}}]
        before=copy.deepcopy(self.project)
        draft,issues=read_draft(self.project,'shot-one',capabilities=current)
        self.assertFalse(issues)
        self.assertEqual(draft['inputs']['guides'],[
            {'media_id':'receipt-night','time_seconds':1,'use_audio':False},
            {'media_id':'receipt-night','time_seconds':2,'use_audio':False}])
        self.assertEqual(len(draft['inputs']['images']),1)
        self.assertEqual(self.project,before)
        shot['data']['h3']['deploymentProfileId']='h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-cgroups-v4'
        current['deployment_profiles'][0]['id']=shot['data']['h3']['deploymentProfileId']
        legacy,_=read_draft(self.project,'shot-one',capabilities=current)
        self.assertEqual(legacy['inputs']['guides'],[])

    def test_full_native_fl_control_video_range_matches_requested_duration_before_native_padding(self):
        from comfy_workflow import native_output_spec
        from studio_platform.capabilities import compile_request
        profile='h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6'
        shot=self.find('shot-one')
        shot['data']['h3'].update(deploymentProfileId=profile,recipeId='h3-base-fl2va-v1',
            controls={'duration':5,'resolution':'480P','seed':'42','video_conditioning':'denoise','denoise':.5})
        shot['data']['storyboard'].update(mode='fl',firstFrameId='',lastFrameId='',
            extraRefs=[{'entityId':'motion','clipStart':1}])
        # Native normalization retains 124 frames from a five-second selection;
        # it does not require selecting 124/24 source seconds or silently use3s.
        draft,issues=read_draft(self.project,'shot-one',capabilities=cap())
        self.assertFalse(issues)
        self.assertEqual(draft['inputs']['videos'],[{'asset_id':'receipt-motion',
            'source_range':{'start':1,'end':6},'include_audio':False}])
        calls=[]
        body=plan_body(self.project,'shot-one',cap(),
            lambda ident,start,end:calls.append((ident,start,end)) or 'derived-motion')
        self.assertEqual(calls,[('receipt-motion',1,6)])
        def asset(_identity):
            return {'metadata':{'kind':'video','model_ready':True,'width':832,'height':480,
                'duration':124/24,'frame_count':124,'fps':24,'source_duration':5,'has_audio':False},
                'model':{'key':'owners/superdan/assets/derived-motion/file','sha256':'a'*64,'size_bytes':4}}
        compiled,_=compile_request(body,asset,backend='wangp-worker')
        self.assertEqual(compiled['output_spec']['frames'],124)
        self.assertEqual(compiled['output_spec'],native_output_spec(compiled['request']))
        shot['data']['h3']['video_audio']={'motion':True}
        self.assertTrue(read_draft(self.project,'shot-one',capabilities=cap())[0]['inputs']['videos'][0]['include_audio'])
        # A deliberately selected guide range is independently authored and is
        # not rewritten to the control-video length.
        from studio_platform.generation_draft import configure
        configure(self.project,{'shot_id':'shot-one','inputs':{'guides':[
            {'media_id':'receipt-motion','time_seconds':0,'use_audio':False,
             'source_range':{'start':2,'end':5}}]}},{'receipt-motion':self.find('motion')})
        self.assertEqual(shot['data']['h3']['guides'][0]['source_range'],
            {'start':2,'end':5,'fileId':'file-motion'})
        before=copy.deepcopy(self.project)
        self.assertEqual(read_draft(self.project,'shot-one',capabilities=cap())[0]['inputs']['guides'][0]['source_range'],
            {'start':2,'end':5})
        calls=[]
        planned=plan_body(self.project,'shot-one',cap(),
            lambda ident,start,end:calls.append((ident,start,end)) or f'derived-{start}-{end}')
        self.assertEqual(calls,[('receipt-motion',1,6),('receipt-motion',2,5)])
        self.assertEqual(planned['inputs']['guides'][0]['media_id'],'derived-2-5')
        self.assertEqual(self.project,before)
        self.find('motion')['data']['fileId']='replaced-motion'
        blocked,issues=read_draft(self.project,'shot-one',capabilities=cap())
        self.assertTrue(any('原素材文件已改变，请重新确认选段' in issue for issue in issues))
        self.assertEqual(blocked['inputs']['guides'],[])
        with self.assertRaisesRegex(ValueError,'原素材文件已改变'):
            plan_body(self.project,'shot-one',cap(),lambda *_:self.fail('stale input was derived'))
        self.find('motion')['data']['fileId']='file-motion'
        for metadata,data_duration in (({'duration':10},None),({},10)):
            self.find('motion')['data']['metadata']=metadata
            if data_duration is not None:
                self.find('motion')['data']['duration']=data_duration
            self.assertFalse(read_draft(self.project,'shot-one',capabilities=cap())[1])
        self.find('motion')['data'].pop('duration')
        self.assertTrue(any('缺少已核验' in issue for issue in
            read_draft(self.project,'shot-one',capabilities=cap())[1]))
        self.find('motion')['data']['metadata']['source_duration']=4
        blocked,issues=read_draft(self.project,'shot-one',capabilities=cap())
        self.assertTrue(any('控制视频选段超出原素材' in issue for issue in issues))
        self.assertEqual(blocked['inputs']['videos'],[])

    def test_full_native_reference_limits_block_instead_of_silently_omitting_authored_inputs(self):
        shot=self.find('shot-one')
        shot['data']['h3']['deploymentProfileId']='h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6'
        for current,reason in ((cap(),'数量上限'),(cap(4,2,7),'总时长上限')):
            current['recipes'][1]['limits']['max_total_audio_duration']=3
            current['deployment_profiles']=[{'id':shot['data']['h3']['deploymentProfileId'],
                'generation_support':{'ref':{'limits':copy.deepcopy(current['recipes'][1]['limits'])}}}]
            draft,issues=read_draft(self.project,'shot-one',capabilities=current)
            self.assertTrue(any(reason in issue for issue in issues),issues)
            self.assertTrue(any(not reference['sent'] for reference in draft['storyboard']['references']))
            with self.assertRaisesRegex(ValueError,reason):
                plan_body(self.project,'shot-one',current,lambda *_:self.fail('over-limit input was derived'))
        shot['data']['h3'].pop('deploymentProfileId')
        self.assertFalse(read_draft(self.project,'shot-one',capabilities=cap())[1])

    def test_explicit_cast_and_null_place_override_scene_without_loss_on_opt_in(self):
        shot, scene = self.find("shot-one"), self.find("scene-one")
        scene["data"].update(cast=[{"characterId": "hero", "lookId": "rain"}], locationId="room")
        shot["data"]["storyboard"]["place"] = None
        shot["data"]["cast"] = []
        draft, issues = read_draft(self.project, "shot-one", capabilities=cap())
        self.assertFalse(issues)
        self.assertNotIn("cast", [p["kind"] for p in draft["storyboard"]["prompt_parts"]])
        self.assertNotIn("place", [p["kind"] for p in draft["storyboard"]["prompt_parts"]])
        cleared = source_snapshot(self.project, "shot-one")
        self.find("hero")["description"] += " unrelated removed actor change"
        self.assertEqual(source_snapshot(self.project, "shot-one"), cleared)
        del shot["data"]["cast"]
        inherited, _ = read_draft(self.project, "shot-one", capabilities=cap())
        cast = next(p for p in inherited["storyboard"]["prompt_parts"] if p["kind"] == "cast")
        self.assertIn("雨夜", cast["text"])
        shot["data"]["cast"] = [{"characterId": "hero", "lookId": "day"}]
        explicit, _ = read_draft(self.project, "shot-one", capabilities=cap())
        cast = next(p for p in explicit["storyboard"]["prompt_parts"] if p["kind"] == "cast")
        self.assertIn("白天", cast["text"])
        self.assertNotIn("雨夜", cast["text"])

    def test_profile_resource_limits_apply_without_historical_joint_whitelist(self):
        shot = self.find("shot-one")
        shot["data"]["h3"]["deploymentProfileId"] = "exact-profile"
        current = cap(4, 2, 8)
        current["deployment_profiles"] = [{"id": "exact-profile", "generation_support": {"ref": {
            "limits": current["recipes"][1]["limits"], "constraints": {"input_limits": {"max_images": 1,
                "max_audios": 1, "max_videos": 1}, "max_reference_files": 3}, "joint_cases": []}}}]
        draft, issues = read_draft(self.project, "shot-one", capabilities=current)
        self.assertFalse(issues)
        self.assertEqual(draft["deployment_profile_id"], "exact-profile")
        self.assertEqual(len(draft["inputs"]["images"]), 1)
        self.assertEqual(draft["controls"], shot["data"]["h3"]["controls"])

    def test_distinct_voice_clips_from_one_source_are_not_deduplicated(self):
        other = copy.deepcopy(self.find("hero"))
        other.update(id="other-hero", title="另一个人")
        other["data"]["voices"][0]["clipStart"] = 4
        self.project["entities"].append(other)
        self.find("shot-one")["data"]["cast"].append({"characterId": "other-hero", "lookId": ""})
        draft, issues = read_draft(self.project, "shot-one", capabilities=cap(1, 2, 4))
        self.assertFalse(issues)
        self.assertEqual([entry["source_range"]["start"] for entry in draft["inputs"]["audios"]], [1, 4])
        self.assertEqual(len(draft["inputs"]["images"]), 1)

    def test_snapshot_includes_style_profiles_files_and_ignores_unused_media(self):
        original = source_snapshot(self.project, "shot-one")
        for ident in ("portrait", "voice", "room-image", "ambient", "sword-image"):
            changed = copy.deepcopy(self.project)
            next(e for e in changed["entities"] if e["id"] == ident)["data"]["fileId"] = "changed"
            self.assertNotEqual(original, source_snapshot(changed, "shot-one"), ident)
        changed = copy.deepcopy(self.project)
        changed["journey"]["storyboard"]["style"] = "新风格"
        self.assertNotEqual(original, source_snapshot(changed, "shot-one"))
        changed = copy.deepcopy(self.project)
        unrelated = copy.deepcopy(changed["entities"][3])
        unrelated.update(id="unrelated", data={"fileId": "before"})
        changed["entities"].append(unrelated)
        before = source_snapshot(changed, "shot-one")
        unrelated["data"]["fileId"] = "after"
        self.assertEqual(before, source_snapshot(changed, "shot-one"))

    def test_delete_media_and_profile_reconciles_new_draft_without_deleting_files(self):
        for ident in ("portrait", "voice", "room", "sword"):
            delete_entity(self.project, ident, False)
            validate_project(self.project)
        shot = self.find("shot-one")["data"]["storyboard"]
        self.assertEqual(shot["firstFrameId"], "")
        self.assertEqual(shot["place"], None)
        self.assertEqual(shot["propIds"], [])
        self.assertTrue(self.find("sword-image"))

    def test_previous_selected_candidate_requires_saved_exact_last_frame(self):
        shot = self.find("shot-one")
        prior = copy.deepcopy(shot)
        prior.update(id="prior", order=0, data={"seconds": 5, "selectedAssetId": "chosen"})
        shot["order"] = 1
        chosen = {**copy.deepcopy(self.find("motion")), "id": "chosen", "data": {"cloudArtifactId": "artifact-one"}}
        frame = {**copy.deepcopy(self.find("portrait")), "id": "saved-frame", "data": {"cloudAssetId": "receipt-frame", "fileId": "frame-file",
            "sourceSelection": {"kind": "artifact_frame", "artifact_id": "artifact-one", "position": "last"}}}
        self.project["entities"].extend([prior, chosen, frame])
        shot["data"]["h3"]["recipeId"] = "h3-base-fl2va-v1"
        shot["data"]["storyboard"].update(mode="fl", continuePrevious=True,
            previousFrame={"shotId": "prior", "selectedAssetId": "chosen", "frameAssetId": "saved-frame"})
        draft, issues = read_draft(self.project, "shot-one", capabilities=cap())
        self.assertEqual(issues, [])
        self.assertEqual(draft["inputs"]["first_frame"], {"asset_id": "receipt-frame"})
        with self.assertRaisesRegex(ValueError, "服务端核对"):
            plan_body(self.project, "shot-one", cap(), lambda *a: None)
        calls = []
        planned = plan_body(self.project, "shot-one", cap(), lambda *a: None, validate_frame=lambda *a: calls.append(a))
        self.assertEqual(calls, [("receipt-frame", "artifact-one")])
        self.assertEqual(planned["inputs"]["first_frame"], {"asset_id": "receipt-frame"})
        original = source_snapshot(self.project, "shot-one")
        prior["data"]["selectedAssetId"] = "motion"
        self.assertNotEqual(original, source_snapshot(self.project, "shot-one"))
        self.assertTrue(read_draft(self.project, "shot-one", capabilities=cap())[1])


class StoryboardHttpTests(unittest.TestCase):
    setUp = draft_fixtures.GenerationDraftTests.setUp
    login = draft_fixtures.GenerationDraftTests.login
    setup_project = draft_fixtures.GenerationDraftTests.setup_project
    source = draft_fixtures.GenerationDraftTests.source
    preflight = draft_fixtures.GenerationDraftTests.preflight

    def derived(self, owner, ident, start, end):
        record = self.app.state.assets.get(owner, ident)
        derived_id = "derived-" + ident
        self.source(derived_id, record["kind"], duration=end - start)
        return {"asset_id": derived_id, "status": "ready"}

    def save_storyboard(self):
        self.setup_project()
        value = storyboard_project()
        self.find = lambda ident: next(e for e in value["entities"] if e["id"] == ident)
        for entity in value["entities"]:
            if entity["type"] in {"image", "video", "audio"}:
                self.source(entity["data"]["cloudAssetId"], entity["type"])
        response = self.client.put("/v1/projects/story-one", json={"expected_version": 1, "project": value})
        self.assertEqual(response.status_code, 200, response.text)
        return value

    def test_preview_preflight_parity_receipt_owner_isolation_and_no_jobs(self):
        value = self.save_storyboard()
        current_cap = capabilities(self.settings)
        preview, _ = read_draft(value, "shot-one", capabilities=current_cap)
        with patch.object(self.app.state.assets, "derive", side_effect=self.derived):
            response = self.preflight()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["effective_request"]["prompt"], preview["prompt"])
        self.assertEqual(response.json()["effective_request"]["inputs"]["images"], [entry["asset_id"] for entry in preview["inputs"]["images"]])
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(conn.scalar(select(func.count()).select_from(jobs)), 0)
        self.login("supervan")
        self.assertEqual(self.client.get("/v1/projects/story-one/shots/shot-one/generation-draft").status_code, 404)
        self.assertEqual(self.client.post("/v1/projects/story-one/shots/shot-one/generation-plans", json={"expected_version": 2}).status_code, 404)

    def test_changed_profile_invalidates_accepted_preflight_source(self):
        value = self.save_storyboard()
        with patch.object(self.app.state.assets, "derive", side_effect=self.derived):
            response = self.preflight()
        self.assertEqual(response.status_code, 201, response.text)
        plan = response.json()
        value["journey"]["storyboard"]["style"] = "new style"
        self.client.put("/v1/projects/story-one", json={"expected_version": 2, "project": value}).raise_for_status()
        response = self.client.post("/v1/jobs", json={"plan_id": plan["plan_id"]}, headers={"Idempotency-Key": "stale-storyboard-one"})
        self.assertEqual(response.status_code, 409, response.text)

    def test_cross_owner_or_project_media_receipt_is_not_authorized_by_document(self):
        value = self.save_storyboard()
        self.source("foreign-owner", "image", owner="supervan")
        self.source("foreign-project", "image", project="another-story")
        for offset, receipt in enumerate(("foreign-owner", "foreign-project")):
            self.find("portrait")["data"]["cloudAssetId"] = receipt
            self.client.put("/v1/projects/story-one", json={"expected_version": 2 + offset, "project": value}).raise_for_status()
            # Restrict selected kinds to avoid unrelated derivative work in this
            # ownership test; the same compiler still reaches the foreign image.
            value["entities"][-3]["data"].update(mainVoiceId="")
            value["entities"][-2]["data"].update(ambient=None)
            value["entities"][2]["data"]["storyboard"]["extraRefs"] = []
            with patch.object(self.app.state.assets, "derive", side_effect=self.derived):
                response = self.preflight()
            self.assertEqual(response.status_code, 404, response.text)
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(conn.scalar(select(func.count()).select_from(jobs)), 0)

    def test_forged_frame_document_provenance_cannot_pass_authoritative_receipt_check(self):
        value = self.save_storyboard()
        shot = self.find("shot-one")
        prior = copy.deepcopy(shot)
        prior.update(id="prior", order=0, data={"seconds": 5, "selectedAssetId": "chosen"})
        shot["order"] = 1
        chosen = {**copy.deepcopy(self.find("motion")), "id": "chosen", "data": {"cloudArtifactId": "artifact-one"}}
        frame = {**copy.deepcopy(self.find("portrait")), "id": "saved-frame"}
        frame["data"]["sourceSelection"] = {"kind": "artifact_frame", "artifact_id": "artifact-one", "position": "last"}
        value["entities"].extend([prior, chosen, frame])
        shot["data"]["h3"]["recipeId"] = "h3-base-fl2va-v1"
        shot["data"]["storyboard"].update(mode="fl", continuePrevious=True,
            previousFrame={"shotId": "prior", "selectedAssetId": "chosen", "frameAssetId": "saved-frame"})
        self.client.put("/v1/projects/story-one", json={"expected_version": 2, "project": value}).raise_for_status()
        response = self.preflight()
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("素材收据核对", response.json()["detail"])
        with self.app.state.repository.engine.connect() as conn:
            self.assertEqual(conn.scalar(select(func.count()).select_from(jobs)), 0)


if __name__ == "__main__":
    unittest.main()
