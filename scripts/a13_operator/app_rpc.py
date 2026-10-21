# SPDX-License-Identifier: AGPL-3.0-only
"""Private local launcher client; never projects reusable capabilities."""
import json,os,re,stat,urllib.request,urllib.parse,http.cookiejar
from pathlib import Path

class AppClient:
    def __init__(self,root):
        self.root=Path(root)
        p=self.root/'native-control.json';info=p.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:raise ValueError('private_control_required')
        native=json.loads(p.read_text());port=native['port']
        if type(port) is not int or not 49152<=port<=65535:raise ValueError('private_port_invalid')
        self.origin=f'http://127.0.0.1:{port}'
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        request=urllib.request.Request(self.origin+'/native-session',data=b'{}',headers={'Content-Type':'application/json','X-Native-Authorization':native['credential']})
        with self.opener.open(request,timeout=10) as response:response.read()
        html=(self.root/'open-demo.html').read_text()
        action=re.search(r'action="([^"]+)"',html).group(1)
        launch=re.search(r'name="launch" value="([^"]+)"',html).group(1)
        if action!=self.origin+'/session':raise ValueError('launcher_origin_mismatch')
        request=urllib.request.Request(action,data=urllib.parse.urlencode({'launch':launch}).encode(),headers={'Origin':self.origin})
        with self.opener.open(request,timeout=10) as response:response.read()
    def call(self,path,body=None):
        if not path.startswith('/api/') or any(c in path for c in ['?','#','%','\\']):raise ValueError('api_path_invalid')
        request=urllib.request.Request(self.origin+path,data=None if body is None else json.dumps(body).encode(),headers={'Origin':self.origin,'Content-Type':'application/json','X-Mycelium-Action':'1'})
        try:
            with self.opener.open(request,timeout=300) as response:return json.load(response)
        except urllib.error.HTTPError as exc:
            try:code=json.loads(exc.read(4096)).get('error','unknown')
            except Exception:code='unknown'
            if not isinstance(code,str) or not re.fullmatch('[a-z0-9_]{1,128}',code):code='unknown'
            raise RuntimeError(f'local_api_http_{exc.code}:{code}') from None

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--path',required=True);parser.add_argument('--body');parser.add_argument('--output',required=True)
    args=parser.parse_args();value=AppClient(args.root).call(args.path,None if args.body is None else json.loads(Path(args.body).read_text()))
    target=Path(args.output);fd=os.open(target,os.O_CREAT|os.O_TRUNC|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(value,stream)
    print(json.dumps({'completed':True,'path':args.path}))
