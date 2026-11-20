// axprobe: research-only macOS Accessibility inspector for the new ChatGPT app (com.openai.codex).
// Subcommands:
//   info                                   app-level attributes + trust status
//   enable                                 set AXManualAccessibility (+ AXEnhancedUserInterface fallback)
//   dump [--root app|window|focused|menubar] [--max-depth N] [--max-nodes N] [--full] --out file.json
//   find  --match k=v[,k=v...]             print matching nodes (keys: role, subrole, id, domid, desc~, title~, value~, class~, rd~, help~)
//   act   --match ... [--nth N] --action AXPress
//   set   --match ... [--nth N] --attr AXValue --value TEXT   (or --bool true|false)
//   get   --match ... [--nth N] --attr NAME
//   param --match ... [--nth N]            probe parameterized attributes on the node
//   observe --seconds N [--match ...]      log AX notifications
// Common: --bundle com.openai.codex (default) | --pid N
import AppKit
import ApplicationServices
import Foundation

// MARK: - arg parsing
var argv = Array(CommandLine.arguments.dropFirst())
guard !argv.isEmpty else { fputs("usage: axprobe <cmd> [opts]\n", stderr); exit(2) }
let cmd = argv.removeFirst()
var opts: [String: String] = [:]
var flags: Set<String> = []
do {
    var i = 0
    while i < argv.count {
        let a = argv[i]
        if a.hasPrefix("--") {
            let k = String(a.dropFirst(2))
            if i + 1 < argv.count && !argv[i + 1].hasPrefix("--") { opts[k] = argv[i + 1]; i += 2 } else { flags.insert(k); i += 1 }
        } else { i += 1 }
    }
}
let bundleID = opts["bundle"] ?? "com.openai.codex"
func resolvePid() -> pid_t {
    if let p = opts["pid"], let v = Int32(p) { return v }
    let apps = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
    guard let app = apps.first else { fputs("app \(bundleID) not running\n", stderr); exit(3) }
    return app.processIdentifier
}
let pid = resolvePid()
let appEl = AXUIElementCreateApplication(pid)
AXUIElementSetMessagingTimeout(appEl, 3.0)

// MARK: - AX helpers
func attrNames(_ e: AXUIElement) -> [String] {
    var names: CFArray?
    guard AXUIElementCopyAttributeNames(e, &names) == .success, let n = names as? [String] else { return [] }
    return n
}
func paramAttrNames(_ e: AXUIElement) -> [String] {
    var names: CFArray?
    guard AXUIElementCopyParameterizedAttributeNames(e, &names) == .success, let n = names as? [String] else { return [] }
    return n
}
func actionNames(_ e: AXUIElement) -> [String] {
    var names: CFArray?
    guard AXUIElementCopyActionNames(e, &names) == .success, let n = names as? [String] else { return [] }
    return n
}
func actionDesc(_ e: AXUIElement, _ a: String) -> String {
    var d: CFString?
    guard AXUIElementCopyActionDescription(e, a as CFString, &d) == .success, let s = d else { return "" }
    return s as String
}
func raw(_ e: AXUIElement, _ name: String) -> (AXError, CFTypeRef?) {
    var v: CFTypeRef?
    let err = AXUIElementCopyAttributeValue(e, name as CFString, &v)
    return (err, v)
}
func str(_ e: AXUIElement, _ name: String) -> String? {
    let (err, v) = raw(e, name)
    guard err == .success, let v = v else { return nil }
    if CFGetTypeID(v) == CFStringGetTypeID() { return (v as! String) }
    if CFGetTypeID(v) == CFAttributedStringGetTypeID() { return (v as! NSAttributedString).string }
    return nil
}
func bool(_ e: AXUIElement, _ name: String) -> Bool? {
    let (err, v) = raw(e, name)
    guard err == .success, let v = v else { return nil }
    if CFGetTypeID(v) == CFBooleanGetTypeID() { return CFBooleanGetValue((v as! CFBoolean)) }
    if CFGetTypeID(v) == CFNumberGetTypeID() { return (v as! NSNumber).boolValue }
    return nil
}
func settable(_ e: AXUIElement, _ name: String) -> Bool {
    var s: DarwinBoolean = false
    return AXUIElementIsAttributeSettable(e, name as CFString, &s) == .success && s.boolValue
}
func children(_ e: AXUIElement) -> [AXUIElement] {
    let (err, v) = raw(e, kAXChildrenAttribute)
    guard err == .success, let arr = v as? [AXUIElement] else { return [] }
    return arr
}
func elementBrief(_ e: AXUIElement) -> String {
    let r = str(e, kAXRoleAttribute) ?? "?"
    let d = str(e, kAXDescriptionAttribute) ?? str(e, kAXTitleAttribute) ?? ""
    return "<\(r)\(d.isEmpty ? "" : " \"\(d.prefix(40))\"")>"
}
func describe(_ v: CFTypeRef, depth: Int = 0) -> Any {
    let t = CFGetTypeID(v)
    if t == CFStringGetTypeID() { return v as! String }
    if t == CFAttributedStringGetTypeID() { return ["attributedString": (v as! NSAttributedString).string] }
    if t == CFBooleanGetTypeID() { return CFBooleanGetValue((v as! CFBoolean)) }
    if t == CFNumberGetTypeID() { return v as! NSNumber }
    if t == CFURLGetTypeID() { return ["url": (v as! URL).absoluteString] }
    if t == AXUIElementGetTypeID() { return ["element": elementBrief(v as! AXUIElement)] }
    if t == AXValueGetTypeID() {
        let av = v as! AXValue
        switch AXValueGetType(av) {
        case .cgPoint: var p = CGPoint.zero; AXValueGetValue(av, .cgPoint, &p); return ["x": p.x, "y": p.y]
        case .cgSize: var s = CGSize.zero; AXValueGetValue(av, .cgSize, &s); return ["w": s.width, "h": s.height]
        case .cgRect: var r = CGRect.zero; AXValueGetValue(av, .cgRect, &r); return ["x": r.origin.x, "y": r.origin.y, "w": r.size.width, "h": r.size.height]
        case .cfRange: var r = CFRange(); AXValueGetValue(av, .cfRange, &r); return ["loc": r.location, "len": r.length]
        default: return "AXValue(other)"
        }
    }
    if t == CFArrayGetTypeID() {
        let arr = v as! [CFTypeRef]
        if depth > 0 { return ["arrayCount": arr.count] }
        if arr.count > 12 { return ["arrayCount": arr.count, "first": arr.prefix(3).map { describe($0, depth: depth + 1) }] }
        return arr.map { describe($0, depth: depth + 1) }
    }
    if t == CFDictionaryGetTypeID() { return ["dict": String(describing: v).prefix(300)] }
    return "type#\(t) " + String(String(describing: v).prefix(80))
}

// Attributes we do NOT expand in --full mode (huge or recursive)
let skipFull: Set<String> = [kAXChildrenAttribute, "AXChildrenInNavigationOrder", "AXVisibleChildren", "AXParent", "AXTopLevelUIElement", "AXWindow",
                             "AXLinkedUIElements", "AXRows", "AXColumns", "AXVisibleRows", "AXSelectedRows", "AXSelectedChildren", "AXContents",
                             "AXSharedTextUIElements", "AXSharedCharacterRange", "AXSharedFocusElements", "AXTitleUIElement", "AXServesAsTitleForUIElements",
                             "AXFocusableAncestor", "AXEditableAncestor", "AXHighestEditableAncestor", "AXStartTextMarker", "AXEndTextMarker",
                             "AXSelectedTextMarkerRange", "AXFocusedWindow", "AXMainWindow", "AXFocusedUIElement", "AXMenuBar", "AXExtrasMenuBar",
                             "AXWindows", "AXDetailsElements", "AXErrorMessageElements", "AXLabelUIElements", "AXOwns", "AXCustomContent", "AXDescribedBy"]

let core = [kAXRoleAttribute, kAXSubroleAttribute, kAXRoleDescriptionAttribute, kAXTitleAttribute, kAXDescriptionAttribute,
            kAXValueAttribute, kAXIdentifierAttribute, kAXHelpAttribute, "AXPlaceholderValue", "AXDOMIdentifier", "AXDOMClassList",
            kAXEnabledAttribute, kAXFocusedAttribute, kAXSelectedAttribute, "AXExpanded", "AXRequired", "AXVisited", "AXInvalid",
            "AXARIALive", "AXARIABusy", "AXARIARelevant", "AXARIAAtomic", "AXLiveRegion", "AXElementBusy", "AXHeadingLevel" /* not std */, "AXURL",
            "AXSelectedText", "AXNumberOfCharacters", "AXInsertionPointLineNumber", "AXEditableAncestor", "AXAutocompleteValue", "AXHasPopup", "AXPopupValue",
            "AXKeyShortcutsValue", "AXRoleDescription", "AXMenuItemCmdChar", "AXMenuItemCmdModifiers", "AXMenuItemMarkChar", "AXTitleUIElement"]

var nodeCount = 0
let maxDepth = Int(opts["max-depth"] ?? "60")!
let maxNodes = Int(opts["max-nodes"] ?? "30000")!
let full = flags.contains("full")

func snapshot(_ e: AXUIElement, path: String, depth: Int) -> [String: Any] {
    nodeCount += 1
    var n: [String: Any] = ["path": path, "depth": depth]
    let names = attrNames(e)
    var attrs: [String: Any] = [:]
    let wanted = full ? names.filter { !skipFull.contains($0) } : core.filter { names.contains($0) }
    for a in wanted {
        let (err, v) = raw(e, a)
        if err == .success, let v = v {
            if a == kAXValueAttribute || a == "AXSelectedText", CFGetTypeID(v) == CFStringGetTypeID() {
                let s = v as! String
                attrs[a] = s.count > 400 ? String(s.prefix(400)) + "…[\(s.count) chars]" : s
            } else { attrs[a] = describe(v) }
        }
    }
    if let p = raw(e, kAXPositionAttribute).1, let s = raw(e, kAXSizeAttribute).1 {
        n["pos"] = describe(p); n["size"] = describe(s)
    }
    n["attrs"] = attrs
    n["attrNames"] = names
    let settables = names.filter { settable(e, $0) }
    if !settables.isEmpty { n["settable"] = settables }
    let acts = actionNames(e)
    if !acts.isEmpty { n["actions"] = acts }
    let pa = paramAttrNames(e)
    if !pa.isEmpty { n["paramAttrs"] = pa }
    if depth < maxDepth && nodeCount < maxNodes {
        let kids = children(e)
        n["childCount"] = kids.count
        n["children"] = kids.enumerated().map { (i, c) in snapshot(c, path: "\(path).\(i)", depth: depth + 1) }
    } else {
        n["truncated"] = true
        n["childCount"] = children(e).count
    }
    return n
}

func rootElement() -> AXUIElement {
    switch opts["root"] ?? "app" {
    case "window":
        if let w = raw(appEl, kAXFocusedWindowAttribute).1 { return (w as! AXUIElement) }
        if let ws = raw(appEl, kAXWindowsAttribute).1 as? [AXUIElement], let w = ws.first { return w }
        return appEl
    case "focused":
        if let f = raw(appEl, kAXFocusedUIElementAttribute).1 { return (f as! AXUIElement) }
        return appEl
    case "menubar":
        if let m = raw(appEl, kAXMenuBarAttribute).1 { return (m as! AXUIElement) }
        return appEl
    default: return appEl
    }
}

// MARK: - matching
struct Match { let e: AXUIElement; let path: String }
func parseMatch(_ s: String) -> [(String, String)] {
    s.split(separator: ",").map { p in
        let kv = p.split(separator: "=", maxSplits: 1).map(String.init)
        return (kv[0], kv.count > 1 ? kv[1] : "")
    }
}
func matches(_ e: AXUIElement, _ crit: [(String, String)]) -> Bool {
    for (k, v) in crit {
        let contains = k.hasSuffix("~")
        let key = contains ? String(k.dropLast()) : k
        let actual: String
        switch key {
        case "role": actual = str(e, kAXRoleAttribute) ?? ""
        case "subrole": actual = str(e, kAXSubroleAttribute) ?? ""
        case "id": actual = str(e, kAXIdentifierAttribute) ?? ""
        case "domid": actual = str(e, "AXDOMIdentifier") ?? ""
        case "desc": actual = str(e, kAXDescriptionAttribute) ?? ""
        case "title": actual = str(e, kAXTitleAttribute) ?? ""
        case "value": actual = str(e, kAXValueAttribute) ?? ""
        case "rd": actual = str(e, kAXRoleDescriptionAttribute) ?? ""
        case "help": actual = str(e, kAXHelpAttribute) ?? ""
        case "ph": actual = str(e, "AXPlaceholderValue") ?? ""
        case "class":
            actual = ((raw(e, "AXDOMClassList").1 as? [String]) ?? []).joined(separator: " ")
        case "focused": actual = (bool(e, kAXFocusedAttribute) ?? false) ? "true" : "false"
        default: actual = str(e, key) ?? ""
        }
        if contains { if !actual.localizedCaseInsensitiveContains(v) { return false } } else if actual != v { return false }
    }
    return true
}
func findAll(_ crit: [(String, String)], root: AXUIElement) -> [Match] {
    var out: [Match] = []
    var stack: [(AXUIElement, String, Int)] = [(root, "0", 0)]
    var visited = 0
    while let (e, p, d) = stack.popLast() {
        visited += 1; if visited > maxNodes { break }
        if matches(e, crit) { out.append(Match(e: e, path: p)) }
        if d < maxDepth { for (i, c) in children(e).enumerated().reversed() { stack.append((c, "\(p).\(i)", d + 1)) } }
    }
    return out
}
func brief(_ m: Match) -> [String: Any] {
    var n = snapshot(m.e, path: m.path, depth: 0)
    n.removeValue(forKey: "children"); n.removeValue(forKey: "attrNames")
    return n
}
func pick() -> Match {
    guard let m = opts["match"] else { fputs("--match required\n", stderr); exit(2) }
    let all = findAll(parseMatch(m), root: rootElement())
    let nth = Int(opts["nth"] ?? "0")!
    guard nth < all.count else { fputs("no match (found \(all.count)) for \(m)\n", stderr); exit(4) }
    return all[nth]
}
func emit(_ obj: Any) {
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys])
    if let out = opts["out"] { try! data.write(to: URL(fileURLWithPath: out)); print("wrote \(out) nodes=\(nodeCount)") }
    else { print(String(data: data, encoding: .utf8)!) }
}

// MARK: - commands
switch cmd {
case "info":
    var info: [String: Any] = ["pid": pid, "trusted": AXIsProcessTrusted(), "bundle": bundleID]
    var a: [String: Any] = [:]
    for name in attrNames(appEl) where !["AXChildren", "AXChildrenInNavigationOrder"].contains(name) {
        if let v = raw(appEl, name).1 { a[name] = describe(v) } else { a[name] = "err \(raw(appEl, name).0.rawValue)" }
    }
    info["appAttrs"] = a
    info["appSettable"] = attrNames(appEl).filter { settable(appEl, $0) }
    info["appActions"] = actionNames(appEl)
    info["probe_AXManualAccessibility"] = String(describing: raw(appEl, "AXManualAccessibility").0.rawValue) + " " + String(describing: bool(appEl, "AXManualAccessibility"))
    info["probe_AXEnhancedUserInterface"] = String(describing: raw(appEl, "AXEnhancedUserInterface").0.rawValue) + " " + String(describing: bool(appEl, "AXEnhancedUserInterface"))
    info["childCount"] = children(appEl).count
    emit(info)
case "enable":
    let attr = opts["attr"] ?? "AXManualAccessibility"
    let val: CFBoolean = (opts["bool"] ?? "true") == "true" ? kCFBooleanTrue : kCFBooleanFalse
    let err = AXUIElementSetAttributeValue(appEl, attr as CFString, val)
    print("set \(attr)=\(val == kCFBooleanTrue) -> AXError \(err.rawValue)")
case "dump":
    emit(snapshot(rootElement(), path: "0", depth: 0))
case "find":
    let all = findAll(parseMatch(opts["match"] ?? ""), root: rootElement())
    emit(all.prefix(Int(opts["limit"] ?? "50")!).map(brief))
case "act":
    let m = pick()
    let action = opts["action"] ?? "AXPress"
    let err = AXUIElementPerformAction(m.e, action as CFString)
    print("\(action) on \(m.path) \(elementBrief(m.e)) -> AXError \(err.rawValue)")
case "set":
    let m = pick()
    let attr = opts["attr"] ?? kAXValueAttribute
    let v: CFTypeRef = opts["bool"] != nil ? (opts["bool"] == "true" ? kCFBooleanTrue : kCFBooleanFalse) : (opts["value"] ?? "") as CFString
    let err = AXUIElementSetAttributeValue(m.e, attr as CFString, v)
    print("set \(attr) on \(m.path) \(elementBrief(m.e)) settable=\(settable(m.e, attr)) -> AXError \(err.rawValue)")
case "get":
    let m = pick()
    let attr = opts["attr"] ?? kAXValueAttribute
    let (err, v) = raw(m.e, attr)
    if let v = v { emit(["path": m.path, "attr": attr, "value": describe(v)]) } else { print("AXError \(err.rawValue)") }
case "param":
    let m = pick()
    var res: [String: Any] = ["path": m.path, "paramAttrs": paramAttrNames(m.e)]
    let n = (raw(m.e, kAXNumberOfCharactersAttribute).1 as? NSNumber)?.intValue ?? 0
    res["AXNumberOfCharacters"] = n
    var range = CFRange(location: 0, length: min(n, 200))
    if let rv = AXValueCreate(.cfRange, &range) {
        for p in ["AXStringForRange", "AXAttributedStringForRange", "AXBoundsForRange", "AXRTFForRange"] {
            var out: CFTypeRef?
            let err = AXUIElementCopyParameterizedAttributeValue(m.e, p as CFString, rv, &out)
            res[p] = out != nil ? describe(out!) : "AXError \(err.rawValue)"
        }
    }
    emit(res)
case "observe":
    let secs = Double(opts["seconds"] ?? "10")!
    let target = opts["match"] != nil ? pick().e : appEl
    var observer: AXObserver?
    let start = Date()
    let cb: AXObserverCallbackWithInfo = { _, el, note, info, _ in
        let t = String(format: "%.3f", Date().timeIntervalSince1970)
        let r = str(el, kAXRoleAttribute) ?? "?"
        let d = (str(el, kAXDescriptionAttribute) ?? str(el, kAXTitleAttribute) ?? "").prefix(50)
        let v = (str(el, kAXValueAttribute) ?? "").count
        let dom = str(el, "AXDOMIdentifier") ?? ""
        var extra = ""
        if let info = info as? [String: Any] { extra = " info=" + String(String(describing: info).prefix(160)) }
        print("\(t) \(note) role=\(r) desc=\"\(d)\" domid=\(dom) valueLen=\(v)\(extra)")
        fflush(stdout)
    }
    guard AXObserverCreateWithInfoCallback(pid, cb, &observer) == .success, let obs = observer else { print("observer create failed"); exit(5) }
    let notes = ["AXValueChanged", "AXUIElementDestroyed", "AXFocusedUIElementChanged", "AXSelectedTextChanged", "AXSelectedChildrenChanged",
                 "AXLayoutChanged", "AXLiveRegionChanged", "AXLiveRegionCreated", "AXCreated", "AXRowCountChanged", "AXTitleChanged",
                 "AXElementBusyChanged", "AXLoadComplete", "AXExpandedChanged", "AXMenuOpened", "AXMenuClosed", "AXWindowCreated",
                 "AXFocusedWindowChanged", "AXAnnouncementRequested", "AXInvalidStatusChanged", "AXAutocorrectionOccurred"]
    var reg: [String: Int32] = [:]
    for n in notes { reg[n] = AXObserverAddNotification(obs, target, n as CFString, nil).rawValue }
    print("registered: \(reg)"); fflush(stdout)
    CFRunLoopAddSource(CFRunLoopGetCurrent(), AXObserverGetRunLoopSource(obs), .defaultMode)
    CFRunLoopRunInMode(.defaultMode, secs, false)
    print("observed for \(Date().timeIntervalSince(start))s")
default:
    fputs("unknown cmd \(cmd)\n", stderr); exit(2)
}
