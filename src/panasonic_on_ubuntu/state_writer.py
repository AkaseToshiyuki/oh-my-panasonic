"""Coalesce unchanged runtime snapshots without weakening ownership records."""
import json
import time


def signature(path):
    try:
        info=path.stat()
        return (info.st_dev,info.st_ino,info.st_mtime_ns,info.st_ctime_ns,info.st_size,info.st_mode)
    except FileNotFoundError:return None


class JsonWriter:
    def __init__(self,path,mode=0o644,heartbeat=None):
        self.path,self.mode,self.heartbeat=path,mode,heartbeat
        self.previous=self.identity=None
        self.written_at=0

    def write(self,value):
        data={k:v for k,v in value.items() if self.heartbeat is None or k!='timestamp'}
        payload=json.dumps(data,ensure_ascii=False,separators=(',',':'))
        now=time.monotonic()
        if (payload==self.previous and signature(self.path)==self.identity
                and (self.heartbeat is None or 0<=now-self.written_at<self.heartbeat)):
            return False
        content=payload
        if self.heartbeat is not None:
            stamp=json.dumps(value['timestamp'],allow_nan=False)
            content='{"timestamp":'+stamp+(','+payload[1:] if data else '}')
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
        temporary=self.path.with_suffix('.new')
        temporary.write_text(content)
        temporary.chmod(self.mode)
        temporary.replace(self.path)
        self.identity=signature(self.path)
        self.previous,self.written_at=payload,now
        return True
