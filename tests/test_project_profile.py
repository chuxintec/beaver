from pathlib import Path
import tempfile
import unittest
import json
import shutil
from unittest.mock import patch
from beaver.project_profile import setup_plan, setup_apply, check, rebind_plan, PROFILE
from beaver.io import write_json, sha
from beaver.errors import BeaverError
from test_adoption import candidate


class ProjectProfileTests(unittest.TestCase):
    def setup(self, root):
        project=candidate(root/'project');spec=root/'profile-spec.json'
        write_json(spec,dict(schemaVersion=1,engine='unity',name='Example',writableRoots=['Assets/Game'],generatedRoots=['Assets/Game/Generated']))
        p=setup_plan(project,spec,root/'plan');setup_apply(p['report'],p['planSha256'],root/'setup')
        return project

    def test_bind_is_local_exact_and_rejects_write_outside_approved_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();project=self.setup(root)
            result=check(project,writes=['Assets/Game/Generated/Item.cs']);self.assertEqual(result['profile']['engine'],'unity')
            with self.assertRaises(BeaverError) as e:check(project,writes=['Assets/Other/Human.cs'])
            self.assertEqual(e.exception.code,'PROJECT_WRITE_DENIED')
            with patch('beaver.project_profile.identity',return_value='new implementation'):
                with self.assertRaises(BeaverError) as e:check(project)
            self.assertEqual(e.exception.code,'PROJECT_TOOL_DRIFT')

    def test_relocation_needs_explicit_rebind_and_preserves_project_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();project=self.setup(root);old=json.loads((project/PROFILE).read_text());moved=root/'moved';shutil.move(project,moved)
            with self.assertRaises(BeaverError) as e:check(moved)
            self.assertEqual(e.exception.code,'PROJECT_BINDING_DRIFT')
            path=moved/PROFILE;p=rebind_plan(moved,path,sha(path.read_bytes()),root/'rebind');setup_apply(p['report'],p['planSha256'],root/'rebind-apply')
            self.assertEqual(check(moved)['profile']['projectId'],old['projectId'])

    def test_reviewed_write_policy_is_inert_until_exact_plan_is_applied(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();project=self.setup(root);path=project/PROFILE
            spec=root/'reviewed-policy.json'
            write_json(spec,dict(schemaVersion=1,engine='unity',name='Example',writableRoots=['Assets/Game','Assets/BeaverAcceptance'],generatedRoots=['Assets/Game/Generated']))
            p=rebind_plan(project,path,sha(path.read_bytes()),root/'policy-plan',spec_path=spec)
            with self.assertRaises(BeaverError):check(project,writes=['Assets/BeaverAcceptance/Probe.cs'])
            setup_apply(p['report'],p['planSha256'],root/'policy-apply')
            check(project,writes=['Assets/BeaverAcceptance/Probe.cs'])
            with self.assertRaises(BeaverError):check(project,writes=['Assets/Other/Human.cs'])

    def test_dependency_drift_requires_explicit_review_and_plan_rejects_later_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();project=self.setup(root);manifest=project/'Packages/manifest.json';write_json(manifest,dict(dependencies={'example':'1.0.0'}))
            with self.assertRaises(BeaverError) as e:check(project)
            self.assertEqual(e.exception.code,'PROJECT_AUTHORITY_DRIFT')
            path=project/PROFILE
            with self.assertRaises(BeaverError):rebind_plan(project,path,sha(path.read_bytes()),root/'bad-rebind')
            p=rebind_plan(project,path,sha(path.read_bytes()),root/'rebind',accept_authority_change=True)
            write_json(manifest,dict(dependencies={'example':'2.0.0'}))
            with self.assertRaises(BeaverError) as e:setup_apply(p['report'],p['planSha256'],root/'apply')
            self.assertEqual(e.exception.code,'PROJECT_AUTHORITY_DRIFT')
