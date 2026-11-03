"""Public multimodal message contract, shared by Drivers and model transports."""
import base64
from copy import deepcopy
from urllib.parse import urlparse
from .common import fail


def task_content(value, supports_images=False):
    if isinstance(value,str):return value
    if not isinstance(value,list) or not value:fail('INPUT.INVALID_CONTENT')
    clean=[];images=0
    for part in value:
        if not isinstance(part,dict):fail('INPUT.INVALID_CONTENT')
        if part.get('type')=='text' and isinstance(part.get('text'),str):clean.append({'type':'text','text':part['text']})
        elif part.get('type')=='image_url':
            if not supports_images:fail('INPUT.MODEL_VISION_UNSUPPORTED','模型连接未声明支持图片，尚未发送请求')
            source=part.get('image_url',{})
            if not isinstance(source,dict):fail('INPUT.INVALID_IMAGE')
            url=source.get('url','');images+=1
            if not isinstance(url,str) or images>4:fail('INPUT.INVALID_IMAGE')
            if url.startswith('data:'):
                header,sep,body=url.partition(',')
                if header not in ('data:image/png;base64','data:image/jpeg;base64','data:image/webp;base64') or not sep:fail('INPUT.INVALID_IMAGE')
                try:raw=base64.b64decode(body,validate=True)
                except ValueError:fail('INPUT.INVALID_IMAGE')
                if len(raw)>4*1024*1024 or not raw:fail('INPUT.IMAGE_SIZE_LIMIT')
                valid=(header.startswith('data:image/png') and raw.startswith(b'\x89PNG\r\n\x1a\n') or
                    header.startswith('data:image/jpeg') and raw.startswith(b'\xff\xd8\xff') or
                    header.startswith('data:image/webp') and raw.startswith(b'RIFF') and raw[8:12]==b'WEBP')
                if not valid:fail('INPUT.INVALID_IMAGE')
            elif urlparse(url).scheme!='https' or not urlparse(url).hostname:fail('INPUT.INVALID_IMAGE_URL')
            detail=source.get('detail','auto')
            if detail not in ('auto','low','high'):fail('INPUT.INVALID_IMAGE_DETAIL')
            clean.append({'type':'image_url','image_url':{'url':url,'detail':detail}})
        else:fail('INPUT.INVALID_CONTENT')
    return clean


def append_text(content, text):
    return content+'\n\n'+text if isinstance(content,str) else [*deepcopy(content),{'type':'text','text':text}]
