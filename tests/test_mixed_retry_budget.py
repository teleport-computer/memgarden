"""Mixed retry reasons must share the configured model-call budget."""
import itertools,json
import pytest
from memgarden import GardenComponent,CaptureRequest,MaintenanceRequest

def make_session(lane,cap):
    garden=GardenComponent(model=None,max_capture_retries=cap)
    if lane=='capture':
        return garden.capture_session(CaptureRequest(window='Synthetic conversation',locale='en'))
    cards=[{'id':f'card-{i}','summary':f'Fact {i}'} for i in range(15)]
    return garden.maintenance_session(MaintenanceRequest(cards=cards,all_cards=cards,locale='en',known_ids=tuple(c['id'] for c in cards)))

def answer(lane,kind,empty):
    key='cards' if lane=='capture' else 'consolidations'
    if kind=='valid':return json.dumps({key:[]}),False
    if kind=='semantic':
        return json.dumps({'cards':[{'action':'supersede','bucket':'Facts','summary':'Updated fact','content':'A synthetic updated fact.','importance':0.5,'pulse':0.2}]}),False
    if kind=='truncation':return ('' if empty else '{"'+key+'":['),True
    return '{"'+key+'": invalid}',False

@pytest.mark.parametrize('lane',['capture','dream'])
@pytest.mark.parametrize('cap',[0,1,2,3])
@pytest.mark.parametrize('empty',[False,True])
@pytest.mark.parametrize('pattern',[('format','truncation'),('truncation','format'),('format','format','truncation'),('truncation','truncation')])
def test_mixed_failure_paths_are_bounded(lane,cap,empty,pattern):
    session=make_session(lane,cap);calls=0
    for kind in itertools.islice(itertools.cycle(pattern),cap+3):
        if session.next_prompt() is None:break
        raw,truncated=answer(lane,kind,empty);session.feed(raw,truncated=truncated);calls+=1
    assert session.next_prompt() is None
    assert calls<=cap+1
    result=session.result()
    assert result.error is not None
    if lane=='capture':assert result.retried<=cap

@pytest.mark.parametrize('cap',[0,1,2,3])
@pytest.mark.parametrize('empty',[False,True])
def test_semantic_then_truncation_shares_budget(cap,empty):
    session=make_session('capture',cap);calls=0
    for kind in ('semantic','truncation','valid'):
        if session.next_prompt() is None:break
        session.feed(*answer('capture',kind,empty)[:1],truncated=answer('capture',kind,empty)[1]);calls+=1
    assert session.next_prompt() is None
    assert calls<=cap+1
    assert session.result().retried<=cap

@pytest.mark.parametrize('lane',['capture','dream'])
@pytest.mark.parametrize('first',['format','truncation'])
def test_valid_retry_result_is_retained(lane,first):
    session=make_session(lane,1)
    raw,truncated=answer(lane,first,False);session.feed(raw,truncated=truncated)
    assert session.next_prompt() is not None
    raw,truncated=answer(lane,'valid',False);session.feed(raw,truncated=truncated)
    assert session.next_prompt() is None
    result=session.result();assert result.error is None
    assert (result.cards if lane=='capture' else result.consolidations)==[]

@pytest.mark.parametrize('truncated',[False,True])
def test_exhausted_semantic_retry_keeps_good_card_and_drops_bad_target(truncated):
    session=make_session('capture',1)
    good={'action':'add','bucket':'Facts','summary':'Prefers tea','content':'The user prefers tea to coffee.','importance':0.6,'pulse':0.2}
    bad={'action':'supersede','target_id':'','bucket':'Facts','summary':'Changed preference','content':'A changed preference without a target.'}
    reply=json.dumps({'cards':[good,bad]})
    session.feed(reply)
    assert session.next_prompt() is not None
    session.feed(reply,truncated=truncated)
    assert session.next_prompt() is None
    result=session.result()
    assert result.error is None and result.retried==1
    assert len(result.cards)==1 and result.cards[0]['content']==good['content']
    assert all(m.get('op')!='supersede' for m in result.mutations)
