# SPDX-License-Identifier: AGPL-3.0-only
"""Verify the staged candidate or rollback; no network unless --live/--base.

Only the named apex origin is contacted, with {} on closed POST routes. No
prompts, entry traffic, credentials or mutations. Loopback --base is for tests.
"""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def verify(release, mode, base, *, client_ip=None):
    from mycelium_swarm_directory.surface_probe import probe
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def fetch(path, method='GET'):
        headers = {'User-Agent':'mycelium-surface-probe/1 (+https://mycelium.now/swarms/)',
                   'Content-Type':'application/json', 'Cache-Control':'no-cache'}
        if client_ip:
            headers['Cf-Connecting-Ip'] = client_ip
        request = urllib.request.Request(base + path, method=method, headers=headers,
                                         data=b'{}' if method == 'POST' else None)
        try:
            response = opener.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, {k.lower():v for k,v in response.headers.items()}, response.read()

    result = {'mode':mode, 'base':base, 'ok':False, 'pages':[], 'guards':[]}
    def require(condition, reason):
        if not condition:
            raise ValueError(reason)
    try:
        result['profile_a'] = probe(base, client_ip=client_ip)
        require(result['profile_a']['verdict'] == 'closed_and_cookie_free', 'Profile A not closed/cookie-free')
        for method in ('GET', 'HEAD'):
            status, headers, body = fetch('/', method)
            result['pages'].append({'path':'/', 'method':method, 'status':status, 'location':headers.get('location')})
            require('set-cookie' not in headers, 'Root set a cookie')
            if mode == 'candidate':
                require(status == 200 and 'text/html' in headers.get('content-type',''), 'Candidate root is not explainer HTML')
                require("script-src 'self'" in headers.get('content-security-policy',''), 'Root CSP missing')
                if method == 'GET':
                    require(b'id="demo-route"' in body and b'/apex.js' in body and b'The fast demo route' not in body, 'Wrong explainer')
            else:
                require(status == 302 and headers.get('location') == '/swarms/', 'Rollback root is not legacy redirect')
        paths = [('/swarms/', 'index.html'), ('/swarms/swarms.js','swarms.js'),
                 ('/swarms/swarms.css','swarms.css'), ('/swarms/site.css','site.css')]
        if mode == 'candidate': paths.append(('/apex.js','apex.js'))
        for path, filename in paths:
            status, headers, body = fetch(path)
            row = {'path':path,'status':status,'sha256':hashlib.sha256(body).hexdigest(),
                   'cookie_free':'set-cookie' not in headers}
            result['pages'].append(row)
            require(status == 200 and row['cookie_free'], 'Asset unavailable or cookie: '+path)
            require("script-src 'self'" in headers.get('content-security-policy',''), 'Asset CSP missing: '+path)
            if filename == 'index.html':
                # Cloudflare can inject HTML; the CSS/JS bytes must still match.
                require(b'/swarms/site.css' in body and b'/swarms/swarms.js' in body, 'Wrong directory HTML')
            else:
                row['matches_checkout'] = body == (release/'mycelium_swarm_directory/web'/filename).read_bytes()
                require(row['matches_checkout'], 'Asset byte mismatch: '+path)
        for method, path in [('GET','/chat'),('GET','/chat/'),('GET','/chat/chat.js'),
                             ('GET','/chat/chat.css'),('POST','/chat/api/state'),
                             ('POST','/chat/api/send'),('POST','/chat/api/events'),
                             ('GET','/swarms/chat'),('GET','/swarms/chat.js'),('GET','/swarms/notice'),
                             ('POST','/'),('POST','/apex.js')]:
            status, headers, _ = fetch(path, method)
            result['guards'].append({'method':method,'path':path,'status':status,'cookie_free':'set-cookie' not in headers})
            require(status in (403,404) and 'set-cookie' not in headers, 'Unexpected open route: '+path)
        if mode == 'rollback':
            status, _, _ = fetch('/apex.js')
            require(status == 404, 'Rollback unexpectedly serves apex.js')
        status, headers, body = fetch('/swarms/api/v1/swarms')
        require(status == 200 and 'set-cookie' not in headers, 'Directory list unavailable or cookie')
        rows = json.loads(body)['swarms']
        require(isinstance(rows,list), 'Invalid directory list')
        # Demo availability is reported, not a prerequisite for honest offline copy.
        result['demo_listings'] = [{k:r.get(k) for k in ('swarm_id','status','members','last_seen_seconds')}
                                   for r in rows if r.get('entry_origin') == 'https://demo.mycelium.now']
        result['ok'] = True
    except Exception as error:
        result['error'] = str(error)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('mode', choices=('candidate','rollback'))
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--live', action='store_true', help='OWNER APPROVAL: verify https://mycelium.now')
    group.add_argument('--base', help='Loopback HTTP origin for isolated verification tests')
    args = parser.parse_args(argv)
    stage = Path(__file__).resolve().parent
    config = json.loads((stage/'deployment.json').read_text())
    release = Path(config[args.mode]['path'])
    if not args.live and not args.base:
        print(json.dumps({'dry_run':True,'mode':args.mode,'release':str(release),'base':'https://mycelium.now'}))
        return 0
    client_ip = None
    base = 'https://mycelium.now'
    if args.base:
        parts = urlsplit(args.base)
        if (parts.scheme != 'http' or parts.path or parts.query or parts.fragment or parts.username
                or not ipaddress.ip_address(parts.hostname).is_loopback):
            parser.error('--base must be a loopback HTTP origin')
        base, client_ip = args.base, '203.0.113.7'
    sys.path.insert(0, str(release))
    result = verify(release, args.mode, base, client_ip=client_ip)
    (stage/(args.mode+'-http-'+str(time.time_ns())+'.json')).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
