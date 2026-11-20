import re, sys, glob
pat = re.compile(sys.argv[1]); files = sys.argv[2:]; ctx = 0
seen=set()
for fn in files:
    s = open(fn, encoding='utf-8', errors='replace').read()
    for m in pat.finditer(s):
        k=m.group(0)
        if k in seen: continue
        seen.add(k); print(fn.split('/')[-1][:40], '|', k[:400])
