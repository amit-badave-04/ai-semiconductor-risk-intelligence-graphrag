import re,html,sys
for f in sys.argv[1:]:
    t=open(f,encoding='utf8',errors='ignore').read()
    t=re.sub(r'<(script|style)[^>]*>.*?</\1>','',t,flags=re.S)
    t=re.sub(r'<(br|/p|/tr|/h\d|/li|/div|/table)[^>]*>','\n',t)
    t=re.sub(r'</t[dh]>',' | ',t)
    t=re.sub(r'<[^>]+>','',t)
    t=html.unescape(t)
    t=re.sub(r'[ \t]+',' ',t)
    t=re.sub(r'\n\s*\n+','\n',t)
    out=f.rsplit('.',1)[0]+'.txt'
    open(out,'w',encoding='utf8').write(t)
    print(out,len(t))
