"""Finite local operations data contracts; declarations do not authenticate events."""
def documents():
    text={'type':'string','minLength':1,'maxLength':256}
    props={k:dict(text) for k in ('event_id','event_type','event_time','player_id','session_id','game_version','config_version','environment')}
    props.update(schema_version={'const':1,'type':'integer'},payload={'type':'object'},economy={'type':'object','additionalProperties':False,'required':['kind','asset_id','quantity','receipt_id'],'properties':dict(kind={'enum':['grant','spend','snapshot']},asset_id=text,quantity={'type':'string','pattern':'^[0-9]{1,19}$'},receipt_id=text)})
    return {'operations-event.schema.json':{'$schema':'https://json-schema.org/draft/2020-12/schema','type':'object','additionalProperties':False,'required':[k for k in props if k not in ('payload','economy')],'properties':props}}
