import json,re,html,sys,urllib.request
tid=sys.argv[1]; n=int(sys.argv[2]) if len(sys.argv)>2 else 1200
req=urllib.request.Request(f'https://community.fly.io/t/{tid}.json',headers={'User-Agent':'Mozilla/5.0'})
d=json.load(urllib.request.urlopen(req))
print(d['title'], d.get('created_at'))
for p in d['post_stream']['posts']:
    t=re.sub(r'<[^>]+>','',p['cooked'])
    print(p['created_at'],p['username'],':',html.unescape(t)[:n].replace('\n',' ')); print('--')
