import json, struct, sys, os
src, out = sys.argv[1], sys.argv[2]
f = open(src, 'rb')
_, hsize, _, jlen = struct.unpack('<IIII', f.read(16))
header = json.loads(f.read(jlen))
base = 8 + hsize
n = 0; listing = []
def walk(node, path):
    global n
    for name, e in node.get('files', {}).items():
        p = os.path.join(path, name)
        if 'files' in e: walk(e, p)
        elif e.get('unpacked'): listing.append((p, e.get('size'), 'unpacked'))
        elif 'offset' in e:
            listing.append((p, e['size'], ''))
            if p.endswith(('.js', '.json', '.html', '.css', '.mjs', '.cjs')) and '/node_modules/' not in p:
                dst = os.path.join(out, p.lstrip('/'))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                f.seek(base + int(e['offset'])); open(dst, 'wb').write(f.read(e['size'])); n += 1
walk(header, '')
open(os.path.join(out, '_listing.txt'), 'w').write('\n'.join(f'{s}\t{u}\t{p}' for p, s, u in listing))
print('files', len(listing), 'extracted', n)
