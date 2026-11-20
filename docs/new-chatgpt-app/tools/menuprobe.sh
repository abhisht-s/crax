#!/bin/bash
# usage: menuprobe.sh <trigger-match> <outfile>
D=$(dirname "$0")
"$D/axprobe" act --root window --match "$1" >/dev/null && sleep 1.2
"$D/axprobe" dump --root window --out "$2" >/dev/null
python3 - "$2" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
web=d
def find_web(n):
    if n['attrs'].get('AXRole')=='AXWebArea': return n
    for c in n.get('children',[]):
        r=find_web(c)
        if r: return r
w=find_web(d); top=w['children'][0]
portals=[c for c in top['children'] if c['attrs'].get('AXDOMIdentifier')!='root' and c.get('childCount')]
def walk(n,ind):
    a=n['attrs'];r=a.get('AXRole')
    interesting = r not in ('AXGroup','AXImage') or a.get('AXTitle') or a.get('AXDescription') or a.get('AXSubrole') in ('AXApplicationDialog','AXLandmarkRegion')
    if interesting and r!='AXStaticText':
        txt=[c['attrs'].get('AXValue') for c in n.get('children',[]) if c['attrs'].get('AXRole')=='AXStaticText']
        print('  '*ind+f"{r}{'/'+a['AXSubrole'] if a.get('AXSubrole') else ''} t={(a.get('AXTitle') or '')[:50]!r} d={(a.get('AXDescription') or '')[:40]!r} v={a.get('AXValue')!r:.30} #{a.get('AXDOMIdentifier','')} {'SEL' if a.get('AXSelected') else ''} {'DISABLED' if a.get('AXEnabled') is False else ''} exp={a.get('AXExpanded','')} acts={[x[2:] for x in n.get('actions',[]) if x not in('AXShowMenu','AXScrollToVisible')]}")
        ind+=1
    for c in n.get('children',[]): walk(c,ind)
for p in portals:
    print('--- portal', p['path'], p['attrs'].get('AXDOMClassList'))
    walk(p,0)
PY
"$D/axprobe" set --root window --match 'role=AXTextArea,desc=Ask ChatGPT' --attr AXFocused --bool true >/dev/null
sleep 0.6
