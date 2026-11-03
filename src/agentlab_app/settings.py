"""Local connection settings. Secrets never enter compositions or observations."""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

DEFAULT = {'base_url':'https://api.deepseek.com','model':'deepseek-flash',
           'api_key_env':'DEEPSEEK_API_KEY','thinking':'enabled','reasoning_effort':'high',
           'max_tokens':None,'timeout':600,'stream':True}


def read_env(path):
    values={}
    if Path(path).is_file():
        for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
            line=line.strip()
            if not line or line.startswith('#'):continue
            if line.startswith('export '):line=line[7:]
            key,sep,value=line.partition('=')
            if sep and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key.strip()):
                value=value.strip()
                if len(value)>1 and value[0]==value[-1] and value[0] in ('"',"'"):value=value[1:-1]
                values[key.strip()]=value
    return values


class ModelSettings:
    def __init__(self,home,env_file):
        self.home=Path(home);self.env_file=Path(env_file)
        self.path=self.home/'model.json';self.secret=self.home/'credentials.json'

    def config(self):
        return {**DEFAULT,**(json.loads(self.path.read_text()) if self.path.exists() else {})}

    def key(self,config=None):
        c=config or self.config();name=c['api_key_env']
        stored=json.loads(self.secret.read_text()) if self.secret.exists() else {}
        # Stored keys are endpoint-bound: editing a relay URL never sends a previous key to it.
        return stored.get(c['base_url'], '') or os.environ.get(name) or read_env(self.env_file).get(name,'')

    def public(self):
        c=self.config();return {**c,'key_configured':bool(self.key(c)),'env_file':str(self.env_file)}

    def save(self,data):
        if set(data)-set(DEFAULT)-{'api_key'}:raise ValueError('Unknown model connection field')
        c={**self.config(),**{k:v for k,v in data.items() if k in DEFAULT}}
        u=urlparse(c['base_url'])
        if u.scheme not in ('https','http') or not u.hostname or u.username or u.password or u.query or u.fragment:raise ValueError('Invalid API base URL')
        if u.scheme=='http' and u.hostname not in ('localhost','127.0.0.1','::1'):raise ValueError('Remote endpoints require HTTPS')
        c['base_url']=c['base_url'].rstrip('/')
        if not isinstance(c['model'],str) or not c['model'].strip():raise ValueError('Model name required')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',c['api_key_env']):raise ValueError('Invalid environment variable')
        if c['thinking'] not in ('enabled','disabled','default') or c['reasoning_effort'] not in ('low','high','max','default'):raise ValueError('Invalid thinking setting')
        if c['max_tokens'] is not None and (type(c['max_tokens']) is not int or c['max_tokens']<1):raise ValueError('Invalid output limit')
        if type(c['timeout']) is not int or not 1<=c['timeout']<=3600:raise ValueError('Invalid timeout')
        if type(c['stream']) is not bool:raise ValueError('Invalid stream setting')
        self.home.mkdir(parents=True,exist_ok=True,mode=0o700)
        def write(path,value):
            temp=path.with_suffix('.tmp')
            fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as f:json.dump(value,f,ensure_ascii=False,indent=2)
            os.replace(temp,path);os.chmod(path,0o600)
        if data.get('api_key'):
            stored=json.loads(self.secret.read_text()) if self.secret.exists() else {}
            stored[c['base_url']]=str(data['api_key']).strip();write(self.secret,stored)
        write(self.path,c)
        return self.public()
