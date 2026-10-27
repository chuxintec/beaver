import unittest
from beaver.ui_inspection import analyze, _validate_spec
from beaver.errors import BeaverError

class UIInspectionTests(unittest.TestCase):
    def setUp(self):
        self.spec={'schemaVersion':1,'mode':'static-prefab','assets':['Assets/UI/Panel.prefab'],
                   'expectations':[{'asset':'Assets/UI/Panel.prefab','nodePath':'Confirm','componentType':'UnityEngine.UI.Button','field':'m_Interactable','equals':True,'source':'approved UI spec'}]}
        self.snapshot={'schemaVersion':1,'mode':'static-prefab','runtimeObserved':False,'projectIdentity':'test-project','assets':[{'path':'Assets/UI/Panel.prefab','missingScripts':0,
            'guid':'a'*32,'localId':11400000,'sourceSha256':'source-hash','metaSha256':'meta-hash','nodes':[{'path':'Confirm','activeInHierarchy':True,'rect':[0,0,100,40]}],
            'components':[{'nodePath':'Confirm','type':'UnityEngine.UI.Button','fields':[{'name':'m_Interactable','propertyPath':'m_Interactable','value':'true','valueType':'Boolean','referenceState':'','objectReference':None}]},{'nodePath':'Confirm','type':'UnityEngine.UI.Image','fields':[{'name':'m_RaycastTarget','value':'true','valueType':'Boolean'}]}],
            'events':[{'nodePath':'Confirm','componentType':'UnityEngine.UI.Button','persistentCalls':[{'method':'Submit'}]}]}]}
    def test_explicit_field_expectation_clear_but_no_acceptance_claims(self):
        result=analyze(self.snapshot,self.spec)
        self.assertEqual(result['status'],'clear')
        self.assertEqual(result['visualAcceptance'],'not-established')
        self.assertEqual(result['interactionAcceptance'],'not-established')
    def test_mismatch_issue_has_stable_identity_and_provenance(self):
        self.snapshot['assets'][0]['components'][0]['fields'][0]['value']='false'
        a=analyze(self.snapshot,self.spec);b=analyze(self.snapshot,self.spec)
        issue=a['issues'][0]
        self.assertEqual(issue['id'],b['issues'][0]['id'])
        self.assertEqual(issue['expectationSource'],'approved UI spec')
        self.assertEqual(issue['certainty'],'high')
    def test_missing_reference_distinguished_from_null(self):
        self.snapshot['assets'][0]['components'][0]['fields']=[{'name':'target','propertyPath':'target','value':'<missing>','referenceState':'missing','objectReference':None}]
        result=analyze(self.snapshot,{'schemaVersion':1,'mode':'static-prefab','assets':self.spec['assets']})
        self.assertEqual(result['issues'][0]['code'],'MISSING_REFERENCE')
    def test_selection_must_match_exactly(self):
        self.snapshot['assets'][0]['path']='Assets/UI/Other.prefab'
        with self.assertRaises(BeaverError): analyze(self.snapshot,self.spec)
    def test_mode_and_asset_kind_are_explicit(self):
        with self.assertRaises(BeaverError): _validate_spec({'schemaVersion':1,'assets':['Assets/UI/Panel.prefab']})
        with self.assertRaises(BeaverError): _validate_spec({'schemaVersion':1,'mode':'static-scene','assets':['Assets/UI/Panel.prefab']})


    def test_boolean_expectation_uses_serialized_value_type(self):
        self.snapshot['assets'][0]['components'][0]['fields'][0]['value']='true'
        result=analyze(self.snapshot,self.spec)
        self.assertEqual(result['status'],'clear')

    def test_provenance_and_asset_identity_are_attached_to_findings(self):
        self.snapshot['assets'][0]['components'][0]['fields'][0]['value']='false'
        issue=analyze(self.snapshot,self.spec)['issues'][0]
        self.assertEqual(issue['assetGuid'],'a'*32)
        self.assertEqual(issue['assetLocalId'],11400000)
        self.assertEqual(issue['projectIdentity'],'test-project')
        self.assertEqual(issue['expectationSource'],'approved UI spec')

    def test_rect_can_be_native_array_and_inactive_text_is_not_flagged(self):
        self.snapshot['projectIdentity']='test-project'
        self.snapshot['assets'][0]['nodes'][0]['rect']=[0,0,0,0]
        self.snapshot['assets'][0]['nodes'][0]['activeInHierarchy']=False
        self.snapshot['assets'][0]['overflow']=[{'nodePath':'Confirm','observed':'overflow'}]
        self.snapshot['assets'][0]['missingGlyphs']=[{'nodePath':'Confirm','characters':'0378','glyphLookupStatus':'missing'}]
        result=analyze(self.snapshot,{'schemaVersion':1,'mode':'static-prefab','assets':self.spec['assets']})
        self.assertEqual(result['issues'],[])

    def test_runtime_requires_real_collection_marker_and_does_not_claim_click(self):
        spec={'schemaVersion':1,'mode':'runtime','assets':['Assets/UI/Panel.prefab']}
        snap=dict(self.snapshot,mode='runtime',runtimeObserved=False)
        with self.assertRaises(BeaverError): analyze(snap,spec)
        snap['runtimeObserved']=True
        report=analyze(snap,spec)
        self.assertEqual(report['interactionAcceptance'],'not-established')

    def test_missing_expectation_provenance_is_rejected(self):
        exp=dict(self.spec['expectations'][0]);exp.pop('source')
        with self.assertRaises(BeaverError): _validate_spec(dict(self.spec,expectations=[exp]))


    def test_resource_selection_is_exact(self):
        spec=dict(self.spec,resources=['Assets/UI/icon.png'])
        snapshot=dict(self.snapshot,resources=[{'path':'Assets/UI/icon.png','guid':'b'*32,'localId':21300000,'type':'UnityEngine.Sprite','assignableTypes':['UnityEngine.Object'],'identityState':'available'}])
        report=analyze(snapshot,spec)
        self.assertEqual(report['status'],'clear')
        with self.assertRaises(BeaverError):
            analyze(dict(snapshot,resources=[]),spec)

    def test_native_geometry_and_layout_findings_are_candidates(self):
        self.snapshot['assets'][0]['components'] += [
            {'nodePath':'Confirm','type':'UnityEngine.UI.VerticalLayoutGroup','fields':[]},
            {'nodePath':'Confirm','type':'UnityEngine.UI.ContentSizeFitter','fields':[]},
        ]
        self.snapshot['assets'][0]['nodes'][0].update(anchorMin=[0.8,0],anchorMax=[0.2,1])
        self.snapshot['assets'][0]['occlusionCandidates']=[{'nodePath':'Confirm','relatedNodePath':'Cover','observed':'overlap'}]
        result=analyze(self.snapshot,{'schemaVersion':1,'mode':'static-prefab','assets':self.spec['assets']})
        codes={row['code'] for row in result['issues']}
        self.assertIn('LAYOUT_CONFLICT_CANDIDATE',codes)
        self.assertIn('INVALID_ANCHOR_CANDIDATE',codes)
        self.assertIn('GRAPHIC_OVERLAP_CANDIDATE',codes)
        self.assertEqual(result['visualAcceptance'],'not-established')

    def test_no_empty_onclick_issue_is_generated(self):
        self.snapshot['assets'][0]['events']=[{'nodePath':'Confirm','componentType':'UnityEngine.UI.Button','field':'onClick','persistentCalls':[]}]
        result=analyze(self.snapshot,{'schemaVersion':1,'mode':'static-prefab','assets':self.spec['assets']})
        self.assertNotIn('BUTTON_UNWIRED',{row['code'] for row in result['issues']})

    def test_invalid_persistent_target_method_is_reported(self):
        self.snapshot['assets'][0]['events']=[{'nodePath':'Confirm','componentType':'UnityEngine.UI.Button','field':'onClick','persistentCalls':[{}],'invalidCalls':['Confirm:target/method missing']}]
        result=analyze(self.snapshot,{'schemaVersion':1,'mode':'static-prefab','assets':self.spec['assets']})
        self.assertIn('BUTTON_INVALID_PERSISTENT_CALL',{row['code'] for row in result['issues']})


    def test_runtime_blocked_center_hit_is_observation_candidate_only(self):
        spec={'schemaVersion':1,'mode':'runtime','assets':['Assets/UI/Panel.prefab']}
        hit={'stage':'button-center','state':'blocked_hit','buttonPath':'Confirm','screenPosition':[120.0,80.0],
             'topHitPath':'Overlay/Capture','topHitType':'UnityEngine.UI.Image','topHitCanvasPath':'OverlayCanvas',
             'topHitCamera':'UICamera','raycastCamera':'UICamera','canvasRenderMode':'ScreenSpaceCamera','cameraState':'available','buttonIsAncestor':False,'raycastObserved':True,
             'source':'live EventSystem.RaycastAll at active Button RectTransform center'}
        snap=dict(self.snapshot,mode='runtime',runtimeObserved=True)
        snap['assets'][0]['hitTests']=[hit]
        report=analyze(snap,spec)
        issue=next(row for row in report['issues'] if row['code']=='RUNTIME_BUTTON_BLOCKED_HIT_CANDIDATE')
        self.assertEqual(issue['observed']['topHitPath'],'Overlay/Capture')
        self.assertEqual(issue['expectationSource'],'live EventSystem.RaycastAll sample')
        self.assertEqual(report['interactionAcceptance'],'not-established')

    def test_unknown_runtime_hit_does_not_create_blocked_candidate(self):
        spec={'schemaVersion':1,'mode':'runtime','assets':['Assets/UI/Panel.prefab']}
        snap=dict(self.snapshot,mode='runtime',runtimeObserved=True)
        snap['assets'][0]['hitTests']=[{'state':'unknown','raycastObserved':False,'buttonPath':'Confirm'}]
        report=analyze(snap,spec)
        self.assertNotIn('RUNTIME_BUTTON_BLOCKED_HIT_CANDIDATE',{row['code'] for row in report['issues']})


    def test_vector_expectation_uses_serialized_numeric_components(self):
        spec={'schemaVersion':1,'mode':'static-prefab','assets':['Assets/UI/Panel.prefab'],
              'expectations':[{'asset':'Assets/UI/Panel.prefab','nodePath':'Confirm','componentType':'UnityEngine.RectTransform',
                               'field':'m_AnchorMin','equals':[0.5,0.5],'source':'approved anchor spec'}]}
        self.snapshot['assets'][0]['components'].append({'nodePath':'Confirm','type':'UnityEngine.RectTransform',
            'fields':[{'name':'m_AnchorMin','propertyPath':'m_AnchorMin','value':'(0.5, 0.5)','valueType':'Vector2','numbers':[0.5,0.5]}]})
        self.assertEqual(analyze(self.snapshot,spec)['status'],'clear')

    def test_tmp_unknown_glyph_api_is_reported_as_unknown_not_missing(self):
        self.snapshot['assets'][0]['missingGlyphs']=[{'nodePath':'Confirm','componentType':'TMPro.TextMeshProUGUI',
            'glyphLookupStatus':'unknown','glyphLookupError':'unsupported overload'}]
        report=analyze(self.snapshot,{'schemaVersion':1,'mode':'static-prefab','assets':self.spec['assets']})
        self.assertIn('GLYPH_LOOKUP_UNKNOWN',{row['code'] for row in report['issues']})
        self.assertNotIn('MISSING_GLYPH',{row['code'] for row in report['issues']})


    def test_unicode_missing_glyph_is_reported_only_for_confirmed_miss(self):
        self.snapshot['assets'][0]['missingGlyphs']=[{'nodePath':'Confirm','componentType':'TMPro.TextMeshProUGUI',
            'characters':'0378','glyphLookupStatus':'missing'}]
        result=analyze(self.snapshot,{'schemaVersion':1,'mode':'static-prefab','assets':self.spec['assets']})
        issue=next(row for row in result['issues'] if row['code']=='MISSING_GLYPH')
        self.assertEqual(issue['observed'],'0378')

if __name__=='__main__':unittest.main()
