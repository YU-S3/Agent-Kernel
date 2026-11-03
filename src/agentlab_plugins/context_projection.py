"""Deterministic projection: keep facts and assistant reasoning intact in storage."""
import hashlib
import json
from copy import deepcopy


def text_size(messages):
    view=deepcopy(messages)
    for m in view:
        if isinstance(m.get('content'),list):
            for part in m['content']:
                if part.get('type')=='image_url':part['image_url']={'url':'[image; provider-specific token cost]'}
    return len(json.dumps(view,ensure_ascii=False))


def compact_text(text, limit):
    if len(text)<=limit:return text
    # Mark excerpts explicitly; never pretend clipped JSON is a complete object.
    receipt={'projection':'excerpt','original_chars':len(text),'sha256':hashlib.sha256(text.encode()).hexdigest(),
        'notice':'Untrusted output excerpt; full result is in history.'}
    count=max(0,limit-len(json.dumps(receipt))-30)
    while True:
        value=json.dumps(dict(receipt,head=text[:count*2//3],tail=text[-(count//3):] if count//3 else ''),ensure_ascii=False)
        if len(value)<=limit or count==0:return value
        count=max(0,count-(len(value)-limit))


def project(messages, config, final=False, protected_from=None):
    view=deepcopy(messages);before=text_size(view);limit=config.get('max_chars',200000)
    starts=[i for i,m in enumerate(view) if m['role']=='user'];count=max(1,int(config.get('turns',20)))
    # An episode may contain planning/finalization instructions as user messages.
    # Never mistake those instructions for a new task and discard its original input.
    protected_from=starts[-1] if protected_from is None and starts else (protected_from or 0)
    cut=min(starts[-count],protected_from) if len(starts)>count else 0
    if cut:view=[m for m in view[:cut] if m['role']=='system']+view[cut:]
    # Final answer is tool-free; reasoning is not required on this transport round.
    if final:
        for m in view:m.pop('reasoning_content',None)
    originals=deepcopy(view);transformed=0
    for round_index,cap in enumerate((config.get('tool_chars',4096),2048,1024,256)):
        if round_index and (not limit or text_size(view)<=limit*.8):break
        seen={};view=deepcopy(originals);transformed=0
        for i,m in enumerate(view):
            if m['role']!='tool' or not isinstance(m.get('content'),str):continue
            original=m['content'];digest=hashlib.sha256(original.encode()).hexdigest()
            if digest in seen:m['content']=f'[Repeated tool output; same as tool_call_id={seen[digest]}; sha256={digest}]'
            else:seen[digest]=m['tool_call_id'];m['content']=compact_text(original,cap)
            transformed+=int(m['content']!=original)
    if limit and text_size(view)>limit and protected_from>cut:
        # Rebuild after dropping older episodes so duplicate references cannot dangle.
        current=[m for m in messages[:protected_from] if m['role']=='system']+messages[protected_from:]
        out=project(current,config,final,0)
        out.update(original_chars=before,facts_count=len(messages),projected=True)
        return out
    after=text_size(view)
    return {'messages':view,'facts_count':len(messages),'visible_count':len(view),'input_chars':after,
        'original_chars':before,'projected':view!=messages,'transformed_outputs':transformed,
        'fits':not limit or after<=limit,'near_limit':bool(limit and after>=limit*.85),'capacity_unit':'text_characters',
        'strategy':'tool-excerpts-preserve-pairing.v1','final':final}
