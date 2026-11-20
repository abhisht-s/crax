#!/usr/bin/env python3
"""Summarize a sequence of axprobe window dumps captured during a send/stream."""
import json, glob, os, sys, hashlib

d = sys.argv[1]
send_at = float(open(os.path.join(os.path.dirname(d), "send_at.txt")).read().split()[1])
files = sorted(glob.glob(os.path.join(d, "*.json")))

def flat(n, out):
    out.append(n)
    for c in n.get("children", []): flat(c, out)
    return out

def text_of(n):
    a = n["attrs"]
    if a.get("AXRole") == "AXStaticText":
        v = a.get("AXValue")
        return v if isinstance(v, str) else ""
    return "".join(text_of(c) for c in n.get("children", [])) if a.get("AXRole") not in ("AXButton", "AXPopUpButton", "AXCheckBox") else ""

def find_container(n):
    # container whose children include role headings
    kids = n.get("children", [])
    if any(k["attrs"].get("AXRole") == "AXHeading" and k["attrs"].get("AXTitle") in ("You said:", "ChatGPT said:") for k in kids):
        return n
    for c in kids:
        r = find_container(c)
        if r: return r

prev = None
for f in files:
    t = float(os.path.basename(f).split("-", 1)[1][:-5]) - send_at
    try: root = json.load(open(f))
    except Exception: continue
    nodes = flat(root, [])
    A = lambda n: n["attrs"]
    send = [n for n in nodes if A(n).get("AXRole") == "AXButton" and A(n).get("AXDescription") == "Send"]
    stop = [n for n in nodes if A(n).get("AXRole") == "AXButton" and A(n).get("AXDescription") in ("Stop", "Stop streaming", "Stop generating")]
    comp = [n for n in nodes if A(n).get("AXRole") == "AXTextArea" and A(n).get("AXDescription") == "Ask ChatGPT"]
    busy = [n for n in nodes if A(n).get("AXElementBusy") is True]
    web = [n for n in nodes if A(n).get("AXRole") == "AXWebArea"]
    live = [text_of(n).strip() for n in nodes if A(n).get("AXARIALive") and text_of(n).strip()]
    cont = find_container(root)
    sig = {}
    if cont:
        kids = cont["children"]
        heads = [i for i, k in enumerate(kids) if A(k).get("AXRole") == "AXHeading" and A(k).get("AXTitle") in ("You said:", "ChatGPT said:")]
        sig["turns"] = "".join("U" if A(kids[i]).get("AXTitle") == "You said:" else "A" for i in heads)
        last_a = [i for i in heads if A(kids[i]).get("AXTitle") == "ChatGPT said:"]
        if last_a:
            seg = kids[last_a[-1] + 1:]
            txt = "".join(text_of(k) for k in seg)
            btns = []
            for k in seg:
                for m in flat(k, []):
                    if A(m).get("AXRole") in ("AXButton", "AXPopUpButton", "AXCheckBox"):
                        btns.append(A(m).get("AXDescription") or A(m).get("AXTitle"))
            sig["a_len"] = len(txt); sig["a_hash"] = hashlib.sha1(txt.encode()).hexdigest()[:8]
            sig["a_btns"] = ",".join(b for b in btns if b)[:120]
            sig["a_head"] = txt[:50].replace("\n", "⏎")
            sig["seg_roles"] = ",".join(A(k).get("AXRole", "?")[2:] + ("/" + ".".join(A(k).get("AXDOMClassList", [])[:2]) if isinstance(A(k).get("AXDOMClassList"), list) else "") for k in seg)[:160]
    row = {
        "t": round(t, 2),
        "web": web[0]["attrs"].get("AXTitle") if web else None,
        "send": None if not send else ("en" if A(send[0]).get("AXEnabled") else "dis"),
        "stop": None if not stop else (A(stop[0]).get("AXDescription"), A(stop[0]).get("AXEnabled")),
        "comp": (A(comp[0]).get("AXValue") or "")[:20] if comp else None,
        "busy": [(A(b).get("AXRole"), ".".join((A(b).get("AXDOMClassList") or [])[:2]) if isinstance(A(b).get("AXDOMClassList"), list) else "") for b in busy][:3],
        "live": [l[:60] for l in live][:4],
        **sig,
    }
    key = {k: v for k, v in row.items() if k != "t"}
    if key != prev:
        print(json.dumps(row, ensure_ascii=False))
        prev = key
