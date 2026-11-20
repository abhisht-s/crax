#!/usr/bin/env python3
"""Render an axprobe JSON dump as a compact outline + census.
usage: render.py dump.json [--census] [--max-depth N] [--collapse] [--grep text] [--path PREFIX]
"""
import json, sys, collections, argparse

ap = argparse.ArgumentParser()
ap.add_argument("dump")
ap.add_argument("--census", action="store_true")
ap.add_argument("--max-depth", type=int, default=999)
ap.add_argument("--collapse", action="store_true", help="collapse chains of empty AXGroups")
ap.add_argument("--path", default="")
ap.add_argument("--strip", type=int, default=0, help="drop this many leading path segments in display")
ap.add_argument("--textlen", type=int, default=60)
ap.add_argument("--no-text", action="store_true", help="hide static text nodes")
args = ap.parse_args()

d = json.load(open(args.dump))

def fmt(n):
    a = n["attrs"]
    role = a.get("AXRole", "?")
    parts = [role]
    if a.get("AXSubrole"): parts.append(f"/{a['AXSubrole']}")
    for k, lab in (("AXTitle", "t"), ("AXDescription", "d"), ("AXValue", "v"), ("AXPlaceholderValue", "ph"), ("AXHelp", "help")):
        v = a.get(k)
        if isinstance(v, str) and v:
            s = v.replace("\n", "⏎")
            if len(s) > args.textlen: s = s[: args.textlen] + f"…({len(v)})"
            parts.append(f'{lab}="{s}"')
        elif v not in (None, "", [], {}) and k == "AXValue":
            parts.append(f"v={v}")
    if a.get("AXIdentifier"): parts.append(f"id={a['AXIdentifier']}")
    if a.get("AXDOMIdentifier"): parts.append(f"#{a['AXDOMIdentifier']}")
    cl = a.get("AXDOMClassList")
    if isinstance(cl, list) and cl:
        c = ".".join(str(x) for x in cl)
        parts.append("." + (c[:70] + "…" if len(c) > 70 else c))
    elif isinstance(cl, dict):
        parts.append(f".[{cl.get('arrayCount')} classes]")
    if a.get("AXFocused") is True: parts.append("FOCUSED")
    if a.get("AXEnabled") is False: parts.append("DISABLED")
    if a.get("AXSelected") is True: parts.append("SELECTED")
    if "AXExpanded" in a: parts.append(f"expanded={a['AXExpanded']}")
    if a.get("AXHasPopup"): parts.append(f"popup={a.get('AXPopupValue', a['AXHasPopup'])}")
    if a.get("AXARIALive"): parts.append(f"live={a['AXARIALive']}")
    if a.get("AXElementBusy") is True: parts.append("BUSY")
    acts = [x for x in n.get("actions", []) if x not in ("AXShowMenu", "AXScrollToVisible") and not (x=="AXPress" and a.get("AXRole") in ("AXGroup","AXStaticText","AXHeading","AXCell","AXRow","AXImage","AXList","AXListMarker"))]
    if acts: parts.append("acts=" + ",".join(x.replace("AX", "") for x in acts))
    st = [x for x in n.get("settable", []) if x not in ("AXFocused","AXSelectedTextMarkerRange","AXCustomContent","AXSelectedText","AXSelectedTextRange")]
    if st: parts.append("set=" + ",".join(x.replace("AX", "") for x in st))
    p, s = n.get("pos"), n.get("size")
    if p and s: parts.append(f"@{int(p['x'])},{int(p['y'])} {int(s['w'])}x{int(s['h'])}")
    return " ".join(parts)

def boring(n):
    a = n["attrs"]
    return a.get("AXRole") == "AXGroup" and not a.get("AXSubrole") and not a.get("AXDescription") and not a.get("AXTitle") \
        and not a.get("AXDOMIdentifier") and len(n.get("children", [])) == 1

def walk(n, depth=0, chain=0):
    if args.path and not (n["path"].startswith(args.path) or args.path.startswith(n["path"])):
        return
    a = n["attrs"]
    if args.no_text and a.get("AXRole") == "AXStaticText":
        return
    if args.collapse and boring(n) and n["path"].startswith(args.path):
        for c in n.get("children", []): walk(c, depth, chain + 1)
        return
    if depth > args.max_depth: return
    pfx = "  " * depth
    print(f"{pfx}{'~'*min(chain,9)}[{n['path'][2*args.strip:] if args.strip else n['path']}] {fmt(n)}" + (" …TRUNC" if n.get("truncated") else ""))
    for c in n.get("children", []): walk(c, depth + 1)

if args.census:
    roles = collections.Counter(); sub = collections.Counter(); acts = collections.Counter(); attrsets = collections.defaultdict(set)
    settable = collections.defaultdict(set); params = collections.defaultdict(set); classes = collections.Counter(); domids = collections.Counter()
    rd = collections.Counter(); maxdepth = 0; total = 0
    def c(n):
        global maxdepth, total
        total += 1; maxdepth = max(maxdepth, n["depth"])
        a = n["attrs"]; r = a.get("AXRole", "?")
        roles[r] += 1; sub[(r, a.get("AXSubrole"))] += 1; rd[(r, a.get("AXRoleDescription"))] += 1
        for x in n.get("actions", []): acts[(r, x)] += 1
        attrsets[r].update(n.get("attrNames", []))
        settable[r].update(n.get("settable", []))
        params[r].update(n.get("paramAttrs", []))
        cl = a.get("AXDOMClassList")
        if isinstance(cl, list):
            for x in cl: classes[x] += 1
        if a.get("AXDOMIdentifier"): domids[a["AXDOMIdentifier"]] += 1
        for k in n.get("children", []): c(k)
    c(d)
    print(f"total={total} maxdepth={maxdepth}")
    print("\n## roles"); [print(f"  {v:5d} {k}") for k, v in roles.most_common()]
    print("\n## role/subrole"); [print(f"  {v:5d} {k}") for k, v in sub.most_common()]
    print("\n## role/roledescription"); [print(f"  {v:5d} {k}") for k, v in rd.most_common()]
    print("\n## actions"); [print(f"  {v:5d} {k}") for k, v in sorted(acts.items())]
    print("\n## settable per role"); [print(f"  {k}: {sorted(v)}") for k, v in settable.items() if v]
    print("\n## param attrs per role"); [print(f"  {k}: {sorted(v)}") for k, v in params.items() if v]
    print("\n## attribute names per role"); [print(f"  {k}: {sorted(v)}") for k, v in attrsets.items()]
    print("\n## dom ids"); [print(f"  {v:4d} {k}") for k, v in domids.most_common(200)]
    print("\n## top dom classes"); [print(f"  {v:4d} {k}") for k, v in classes.most_common(150)]
else:
    walk(d)
