"""Repair known whole-file patch drift only inside disposable host fixtures."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO/'scripts'))
from companion_setup.host import PATCHES,SetupError,patch_overlay


@unittest.skipUnless(os.environ.get('HERMES_SOURCE'),'set HERMES_SOURCE for readonly clean-base host fixtures')
class HostPatchRepairTests(unittest.TestCase):
    def setUp(self):
        self.source=Path(os.environ['HERMES_SOURCE']).resolve(strict=True)
        self.temp=tempfile.TemporaryDirectory(prefix='companion-patch-repair-')
        self.root=Path(self.temp.name)/'host'
        self.root.mkdir()
        self.targets=set()
        for name in PATCHES:
            for line in (REPO/'integration/hermes-host'/(name+'.patch')).read_text().splitlines():
                if line.startswith('+++ b/'):
                    self.targets.add(line[6:])
        self.real_before=self.real_hashes()
        self.baseline={}
        for name in self.targets:
            result=subprocess.run(['git','-C',str(self.source),'show','HEAD:'+name],
                                  capture_output=True)
            if result.returncode==0:
                self.baseline[name]=result.stdout
                path=self.root/name
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(result.stdout)

    def tearDown(self):
        try:
            self.assertEqual(self.real_hashes(),self.real_before,'readonly fixture changed real host source')
        finally:
            self.temp.cleanup()

    def real_hashes(self):
        return {name:hashlib.sha256((self.source/name).read_bytes()).hexdigest()
                if (self.source/name).is_file() else None for name in self.targets}

    def write(self,changes):
        for name,body in changes.items():
            path=self.root/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(body)

    def patched(self):
        changes,states=patch_overlay(REPO,self.root,True)
        self.assertTrue(changes)
        self.assertEqual(set(states),set(PATCHES))
        self.assertEqual(set(states.values()),{'will_apply'})
        self.write(changes)
        repeated,states=patch_overlay(REPO,self.root,True)
        self.assertFalse(repeated)
        self.assertEqual(set(states.values()),{'already_applied'})

    def test_restoring_one_complete_file_to_head_repairs_only_that_file_then_is_idempotent(self):
        self.patched()
        target=self.root/'model_tools.py'
        self.assertEqual(target.read_text().count('check_tool_authority('),3)
        target.write_bytes(self.baseline['model_tools.py'])
        before=target.read_bytes()
        changes,states=patch_overlay(REPO,self.root,True)
        self.assertEqual(set(changes),{'model_tools.py'})
        self.assertEqual(states['napcat-public-turn'],'repair_partial')
        self.assertTrue(all(state=='already_applied' for name,state in states.items()
                            if name!='napcat-public-turn'))
        self.assertEqual(target.read_bytes(),before,'planning must not write the supplied fixture host')
        self.write(changes)
        self.assertEqual(target.read_text().count('check_tool_authority('),3)
        repeated,states=patch_overlay(REPO,self.root,True)
        self.assertFalse(repeated)
        self.assertEqual(set(states.values()),{'already_applied'})

    def test_a_partially_changed_file_is_a_conflict_not_a_hunk_repair(self):
        self.patched()
        target=self.root/'model_tools.py'
        body=target.read_text()
        snippet=('        # Nested RPC and direct registry callers do not traverse AIAgent\'s executor.\n'
                 '        # Recheck the final arguments here; a missing inherited principal is denied\n'
                 '        # by the group authority at the outer execute_code/delegation boundary.\n'
                 '        from agent.tool_authority import check_tool_authority\n'
                 '        refusal = check_tool_authority(function_name, function_args)\n'
                 '        if refusal is not None:\n'
                 '            return _emit(tool_error(refusal), status="blocked",\n'
                 '                         error_type="tool_authorization_denied", error_message=refusal)\n\n')
        self.assertEqual(body.count(snippet),1)
        target.write_text(body.replace(snippet,'',1))
        before=target.read_bytes()
        with self.assertRaisesRegex(SetupError,'napcat-public-turn / model_tools.py'):
            patch_overlay(REPO,self.root,True)
        self.assertEqual(target.read_bytes(),before)

    def test_inherited_git_location_cannot_redirect_planning_to_the_real_host(self):
        self.patched()
        target=self.root/'model_tools.py'
        target.write_bytes(self.baseline['model_tools.py'])
        with patch.dict(os.environ,{'GIT_DIR':str(self.source/'.git'),
                                     'GIT_WORK_TREE':str(self.source),
                                     'GIT_INDEX_FILE':str(self.source/'.git'/'index')}):
            changes,states=patch_overlay(REPO,self.root,True)
        self.assertEqual(set(changes),{'model_tools.py'})
        self.assertEqual(states['napcat-public-turn'],'repair_partial')

    def test_outer_git_repository_cannot_make_composition_skip_patch_paths(self):
        outer=Path(self.temp.name)/'outer'
        outer.mkdir()
        subprocess.run(['git','init','--quiet','--template=',str(outer)],check=True,capture_output=True)
        scratch=outer/'scratch'
        scratch.mkdir()
        with patch.dict(os.environ,{'TMPDIR':str(scratch)}),patch('tempfile.tempdir',None):
            changes,states=patch_overlay(REPO,self.root,True)
        self.assertIn('model_tools.py',changes)
        self.assertEqual(set(states.values()),{'will_apply'})


if __name__=='__main__':
    unittest.main()
