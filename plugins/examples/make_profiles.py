"""Generate explicit content-locked demo compositions from installed source packages."""
import json
from pathlib import Path
from agentlab.abi import PluginDefinition

root=Path(__file__).resolve().parent
packages={name:PluginDefinition.read(root/name) for name in ['services','workflow','basic_loop','external']}

def write(name, instances, entry, templates=None):
    ids={row['plugin'] for row in instances.values()}
    profile=dict(kernel_api=1,composition=name,packages=[dict(id=p.id,version=p.version,digest=p.digest) for p in packages.values() if p.id in ids],instances=instances,entry=entry)
    if templates: profile['templates']=templates
    (root/(name+'.json')).write_text(json.dumps(profile,indent=2)+'\n')

write('hello',{'a':{'plugin':'example.workflow','bindings':{'helper':'b','next':'c'}},'b':{'plugin':'example.services','config':{'prefix':'B'}},'c':{'plugin':'example.services'}}, {'instance':'a','method':'run'})
write('basic',{'agent':{'plugin':'example.basic_loop','bindings':{'model':'model','tools':'tools'}},'model':{'plugin':'example.services'},'tools':{'plugin':'example.services'}},{'instance':'agent','method':'run'})
write('shared',{'agent_a':{'plugin':'example.basic_loop','bindings':{'model':'shared','tools':'tools'}},'agent_b':{'plugin':'example.basic_loop','bindings':{'model':'shared','tools':'tools'}},'shared':{'plugin':'example.services','execution':{'max_inflight':2}},'tools':{'plugin':'example.services'}},{'instance':'agent_a','method':'run'})
write('external-demo',{'harness':{'plugin':'example.external','execution':{'isolation':'process','backend':'local'},'control':{'network':'inherit'}}},{'instance':'harness','method':'run'})
