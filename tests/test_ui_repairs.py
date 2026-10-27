from pathlib import Path
import tempfile
import unittest
from beaver.errors import BeaverError
from beaver.ui_repairs import actions
from beaver.io import sha


class UIRepairGuards(unittest.TestCase):
    def setup_case(self, root):
        p=root/'Assets/Panel.prefab';p.parent.mkdir();p.write_text('owned source')
        Path(str(p)+'.meta').write_text('guid: '+'a'*32)
        snapshot=dict(assets=[dict(path='Assets/Panel.prefab',sourceSha256=sha(p.read_bytes()),metaSha256=sha(Path(str(p)+'.meta').read_bytes()),components=[dict(nodePath='Title',type='TMPro.TextMeshProUGUI',fields=[dict(propertyPath='m_fontSize',referenceState='',valueType='Float')])])])
        repair=dict(asset='Assets/Panel.prefab',nodePath='Title',componentType='TMPro.TextMeshProUGUI',field='m_fontSize',mode='float',value=20,source='approved typography spec')
        return snapshot,repair

    def test_object_reference_cannot_bypass_guarded_wiring(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d).resolve();s,repair=self.setup_case(r)
            s['assets'][0]['components'][0]['fields'][0]['referenceState']='bound'
            with self.assertRaisesRegex(BeaverError,'UI_REPAIR_REFERENCE'):actions(r,s,[repair])

    def test_later_manual_source_edit_blocks_correction(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d).resolve();s,repair=self.setup_case(r)
            (r/'Assets/Panel.prefab').write_text('manual change')
            with self.assertRaisesRegex(BeaverError,'UI_REPAIR_DRIFT'):actions(r,s,[repair])

    def test_boolean_is_not_an_integer_font_size(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d).resolve();s,repair=self.setup_case(r);repair['value']=True
            with self.assertRaisesRegex(BeaverError,'UI_REPAIR_VALUE'):actions(r,s,[repair])
