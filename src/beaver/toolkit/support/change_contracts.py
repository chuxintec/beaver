"""Declared UI/UX change boundaries; never authorization or gameplay attestation."""
from jsonschema import Draft202012Validator
from .common import GreaError, digest

INVARIANTS = ['combat_rules','progression','damage_hit_timing','rng_choices','costs_rewards_eligibility','persistence_semantics','animation_events_cancel']
RESKIN_STAGES = ['style_sample', 'art_production', 'sample_replace', 'full_reskin', 'game_tuning']

def legacy_documents():
    text={'type':'string','minLength':1,'maxLength':1024}
    return {'change-contract.schema.json':{'$schema':'https://json-schema.org/draft/2020-12/schema','type':'object','additionalProperties':False,
        'required':['schema_version','scope','baseline_sha256','product_id','baseline_product_id','save_namespace','baseline_save_namespace','design','allowed_changes','invariants'],
        'properties':{'schema_version':{'const':1},'scope':{'const':'ui_ux'},'baseline_sha256':{'type':'string','pattern':'^[0-9a-f]{64}$'},
        'product_id':text,'baseline_product_id':text,'save_namespace':text,'baseline_save_namespace':text,
        'design':{'type':'object','additionalProperties':False,'required':['state','reference','sha256'],'properties':{'state':{'enum':['candidate','approved']},'reference':text,'sha256':{'type':'string','pattern':'^[0-9a-f]{64}$'}}},
        'allowed_changes':{'type':'array','minItems':1,'maxItems':5,'uniqueItems':True,'items':{'enum':['visual_style','layout','navigation','feedback','readability']}},
        'invariants':{'type':'array','minItems':len(INVARIANTS),'maxItems':len(INVARIANTS),'uniqueItems':True,'items':{'enum':INVARIANTS}},
        'ui_ux_budget':text,
        'art_production':{'type':'object','additionalProperties':False,'required':['stage','ledger'],
            'properties':{'stage':{'enum':RESKIN_STAGES},'ledger':artifact_schema(),
                          'reskin_complete':artifact_schema()}}}}}

def documents():
    """Version dispatch keeps the original UI/UX contract unchanged."""
    text={'type':'string','minLength':1,'maxLength':4096}
    refs={'type':'array','minItems':1,'maxItems':128,'uniqueItems':True,'items':text}
    props={k:text for k in ('product_id','baseline_product_id','save_namespace','baseline_save_namespace','user_source','objective')}
    props.update(schema_version={'const':2},scope={'enum':['faithful','reskin','feature','tuning','prototype','operate']},baseline_sha256={'type':'string','pattern':'^[0-9a-f]{64}$'},design=legacy_documents()['change-contract.schema.json']['properties']['design'],allowed_changes=refs,invariants=refs,verification=refs,
        compatibility={'type':'object','additionalProperties':False,'required':['config','save','protocol','pending'],'properties':{k:text for k in ('config','save','protocol','pending')}},
        experiment={'type':'object','additionalProperties':False,'required':['question','keep_discard_criteria'],'properties':{k:text for k in ('question','keep_discard_criteria')}})
    v2={'type':'object','additionalProperties':False,'properties':props,'required':[k for k in props if k!='experiment']}
    return {'change-contract.schema.json':{'$schema':'https://json-schema.org/draft/2020-12/schema','oneOf':[legacy_documents()['change-contract.schema.json'],v2]}}

def artifact_schema():
    return {'type':'object','additionalProperties':False,'required':['reference','sha256'],
            'properties':{'reference':{'type':'string','minLength':1,'maxLength':1024},
                          'sha256':{'type':'string','pattern':'^[0-9a-f]{64}$'}}}

def validate(value, baseline_sha256=None):
    schema=documents()['change-contract.schema.json']
    errors=list(Draft202012Validator(schema).iter_errors(value))
    if errors:raise GreaError('INVALID_CHANGE_CONTRACT',errors[0].message)
    if value['schema_version']==2:
        if value['scope']=='prototype' and 'experiment' not in value:raise GreaError('INVALID_CHANGE_CONTRACT','Prototype needs a question and observable keep/discard criteria.')
        if value['scope'] not in ('reskin','prototype'):
            if baseline_sha256 is not None and value['baseline_sha256']!=baseline_sha256:raise GreaError('CHANGE_BASELINE_MISMATCH','Contract must bind baseline.')
            return dict(value)
    for name in ('product_id','save_namespace'):
        if value[name].strip().casefold()==value['baseline_'+name].strip().casefold():
            raise GreaError('CHANGE_IDENTITY_COLLISION','Derived product and save namespace must be explicitly isolated.')
    if baseline_sha256 is not None and value['baseline_sha256']!=baseline_sha256:
        raise GreaError('CHANGE_BASELINE_MISMATCH','Contract must bind the selected baseline artifact digest.')
    production=value.get('art_production')
    if production:
        if production['stage']!='game_tuning' and value['allowed_changes']!=['visual_style']:
            raise GreaError('RESKIN_LAYOUT_FROZEN','Art replacement preserves layout, entrances and operations; UI/UX tuning is last.')
        if production['stage'] in ('full_reskin','game_tuning') and value['design']['state']!='approved':
            raise GreaError('RESKIN_STYLE_UNAPPROVED','Full reskin follows user approval of the visible sample style.')
        if production['stage']=='game_tuning' and 'reskin_complete' not in production:
            raise GreaError('RESKIN_NOT_COMPLETE','UI/UX tuning requires an explicit full-reskin and bug-fix completion receipt.')
    return dict(value)

def fingerprint(value):return digest(validate(value))

def theme_documents():
    text={'type':'string','minLength':1,'maxLength':4096}
    sha={'type':'string','pattern':'^[0-9a-f]{64}$'}
    vector=lambda size: {'type':'array','minItems':size,'maxItems':size,'items':{'type':'number'}}
    sprite={'type':'object','additionalProperties':False,'required':['mode','width','height','pivot','border','pixels_per_unit','file_id'],
        'properties':{'mode':{'const':'single'},'width':{'type':'integer','minimum':1},'height':{'type':'integer','minimum':1},'pivot':vector(2),'border':vector(4),'pixels_per_unit':{'type':'number','exclusiveMinimum':0},'file_id':{'type':'integer'}}}
    props={'usage':{'const':'ui_sprite'},'alpha_required':{'type':'boolean'},'asset':text,'replacement':text,'source_sha256':sha,'replacement_sha256':sha,'meta_sha256':sha,'guid':{'type':'string','pattern':'^[0-9a-f]{32}$'},'sprite':sprite,'ledger_record':text}
    return {'theme-map.schema.json':{'$schema':'https://json-schema.org/draft/2020-12/schema','type':'object','additionalProperties':False,'required':['schema_version','change_contract','assets'],
        'properties':{'schema_version':{'const':1},'change_contract':documents()['change-contract.schema.json'],'assets':{'type':'array','minItems':1,'maxItems':256,'items':{'type':'object','additionalProperties':False,'required':[k for k in props if k!='ledger_record'],'properties':props}}}},
        'art-ledger.schema.json':ledger_schema()}

def ledger_schema():
    text={'type':'string','minLength':1,'maxLength':1024}
    refs=lambda minimum=0: {'type':'array','minItems':minimum,'maxItems':256,'uniqueItems':True,'items':text}
    spine_props={k:text for k in ('editor_version','runtime_version','skeleton','reason')}
    spine_props.update({k:refs() for k in ('attachments','animations','events','atlas','textures','exports','evidence')})
    spine_props['state']={'enum':['not_required','pending','authored','exported','integrated','verified']}
    props={k:text for k in ('id','feature_id','original_resource','design_source','design_version')}
    props.update({k:refs() for k in ('source_art','slices','targets','evidence')})
    props['provenance']={'type':'object','additionalProperties':False,'properties':{**{k:text for k in ('source_url','creator','license','license_reference','generation_tool','tool_version','model','prompt_reference')},'restrictions':refs(),'source_references':refs()}}
    props.update(kind={'enum':['interface','icon','character','monster','effect','level']},
                 state={'enum':['planned','designed','produced','integrated','verified']},
                 spine={'type':'object','additionalProperties':False,'required':['state'],'properties':spine_props})
    return {'$schema':'https://json-schema.org/draft/2020-12/schema','type':'object','additionalProperties':False,
        'required':['schema_version','selection','settings','records'],
        'properties':{'schema_version':{'const':1},
            'selection':{'type':'object','additionalProperties':False,'required':['interfaces','characters','monsters'],
                         'properties':{k:refs(1) for k in ('interfaces','characters','monsters')}},
            'settings':{'type':'object','additionalProperties':False,'required':['interface','character','monster','effect','level'],
                        'properties':{k:text for k in ('interface','character','monster','effect','level')}},
            'records':{'type':'array','minItems':1,'maxItems':4096,'items':{
                'type':'object','additionalProperties':False,
                'required':[k for k in props if k not in ('spine','evidence','provenance')],'properties':props}}}}

def validate_ledger(value):
    """Validate declared production links, never media, approval or Spine execution."""
    errors=list(Draft202012Validator(ledger_schema()).iter_errors(value))
    if errors:raise ValueError(errors[0].message)
    records={}
    for row in value['records']:
        key=row['id'].strip().casefold()
        if not key or key in records:raise ValueError('Empty or duplicate art ledger resource ID')
        records[key]=row
        spine=row.get('spine')
        if row['kind'] in ('character','monster','effect') and not spine:
            raise ValueError('Declare real Spine state or an explicit not-required reason')
        if spine:
            state=spine['state']
            if state=='not_required' and not spine.get('reason','').strip():raise ValueError('Explain non-Spine production')
            if state in ('exported','integrated','verified'):
                for field in ('editor_version','runtime_version','skeleton','attachments','animations','atlas','textures','exports'):
                    if not spine.get(field):raise ValueError('Spine export missing '+field)
                if 'events' not in spine:raise ValueError('Declare event names, including an explicit empty list')
            if state in ('integrated','verified') and not row['targets']:raise ValueError('Spine integration needs target references')
            if state=='verified' and not spine.get('evidence'):raise ValueError('Spine verification needs explicit evidence references')
        if row['state'] in ('produced','integrated','verified'):
            if not row['source_art']:raise ValueError('Produced art needs source references')
            if spine and spine['state'] in ('pending','authored'):raise ValueError('Static art or authored skeleton is not a complete exported Spine asset')
        if row['state'] in ('integrated','verified') and not row['targets']:raise ValueError('Integration needs target references')
        if row['state']=='verified' and not row.get('evidence'):raise ValueError('Verification needs explicit evidence references')
    for group,kind in (('interfaces','interface'),('characters','character'),('monsters','monster')):
        selected=[x.strip().casefold() for x in value['selection'][group]]
        if len(set(selected))!=len(selected):raise ValueError('Selection contains duplicate normalized resource IDs')
        for key in value['selection'][group]:
            if key.strip().casefold() not in records or records[key.strip().casefold()]['kind']!=kind:
                raise ValueError('Selection must name an existing resource of the correct kind')
    return dict(status='validated_declared_art_ledger',records=len(records),
                selection={k:len(v) for k,v in value['selection'].items()},
                declared_states={state:sum(r['state']==state for r in value['records']) for state in ('planned','designed','produced','integrated','verified')},
                limitations=['References, versions, states and evidence are declarations; referenced media are not opened.',
                             'No design approval, Spine export/import/runtime, gameplay or Unity application is attested.'])
