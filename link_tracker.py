# tools/link_tracker/router.py
"""
Link Tracker - monitors configured folders for [[wikilink]] integrity and change history.
Generic over [[target]] / [[target|alias]] syntax - no dependency on the wiki module, point it at any folder
of markdown files. Policy on broken/renamed links: auto (rewrite references), ask (queue for confirmation),
retain (log only, never modify files).
"""
import re, json, uuid
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse

TOOL_META = {"label": "Link Tracker", "icon": "&#x1F517;", "description": "Monitors folders for [[wikilink]] integrity and changes"}
router = APIRouter()
_P = "/tool/link_tracker"
DATA_DIR = Path("./data/link_tracker")
STATE_FILE = DATA_DIR / "state.json"
LOG_FILE = DATA_DIR / "changelog.json"
ENV = {}
UI = None
LINK_RE = re.compile(r'\[\[([^\]]+)\]\]')

def init_module(env: dict):
    global ENV, UI
    ENV = env
    UI = env["templates"].env.globals.get("UI")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print("[link_tracker] ready")

# --- Storage ---

def _load_state() -> dict: return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"folders": {}, "policy": "retain"}
def _save_state(s: dict): STATE_FILE.write_text(json.dumps(s, indent=2))
def _load_log() -> list: return json.loads(LOG_FILE.read_text()) if LOG_FILE.exists() else []
def _save_log(l: list): LOG_FILE.write_text(json.dumps(l[-500:], indent=2))
def _log_event(kind: str, detail: dict, status: str = "applied"):
    log = _load_log(); log.append({"id": uuid.uuid4().hex[:8], "kind": kind, "detail": detail, "status": status, "ts": datetime.utcnow().isoformat()}); _save_log(log)

# --- Scanning ---

def _scan_folder(root: Path) -> dict:
    graph = {}
    if not root.exists(): return graph
    for f in root.rglob("*.md"):
        if f.name.startswith("."): continue
        rel = str(f.relative_to(root)).replace("\\", "/")
        try: text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception: continue
        links = [(t.strip(), a.strip()) for t, _, a in (m.group(1).partition("|") for m in LINK_RE.finditer(text))]
        graph[rel] = links
    return graph

def _backlinks(graph: dict) -> dict:
    bl = {}
    for src, links in graph.items():
        for target, _ in links: bl.setdefault(f"{target}.md", []).append(src)
    return bl

def _apply_rename(root: Path, old_target: str, new_target: str, policy: str):
    if policy == "retain": _log_event("rename_detected", {"old": old_target, "new": new_target}, status="logged"); return
    if policy == "ask": _log_event("rename_detected", {"old": old_target, "new": new_target, "root": str(root)}, status="pending"); return
    count = 0
    for f in root.rglob("*.md"):
        try: text = f.read_text(encoding="utf-8")
        except Exception: continue
        new_text = re.sub(rf'\[\[{re.escape(old_target)}(\|[^\]]*)?\]\]', lambda m: f"[[{new_target}{m.group(1) or ''}]]", text)
        if new_text != text: f.write_text(new_text, encoding="utf-8"); count += 1
    _log_event("rename_applied", {"old": old_target, "new": new_target, "files_updated": count}, status="applied")

def _rescan(folder: str, policy: str) -> tuple[int,int,int]:
    root = Path(folder).resolve()
    new_graph = _scan_folder(root)
    state = _load_state()
    old_graph = state.get("folders", {}).get(folder, {}).get("graph", {})

    added_files, removed_files = set(new_graph) - set(old_graph), set(old_graph) - set(new_graph)
    for f in added_files: _log_event("file_added", {"folder": folder, "file": f})
    for f in removed_files: _log_event("file_removed", {"folder": folder, "file": f})

    existing = set(new_graph)
    for src, links in new_graph.items():
        old_links, new_links = {t for t, _ in old_graph.get(src, [])}, {t for t, _ in links}
        for t in new_links - old_links: _log_event("link_added", {"folder": folder, "file": src, "target": t})
        for t in old_links - new_links: _log_event("link_removed", {"folder": folder, "file": src, "target": t})

    broken = []
    for src, links in new_graph.items():
        for target, _ in links:
            if f"{target}.md" in existing: continue
            candidates = [f for f in added_files if Path(f).stem == Path(target).name]
            (lambda: _apply_rename(root, target, candidates[0][:-3], policy))() if len(candidates) == 1 else broken.append({"file": src, "target": target})
    if broken: _log_event("broken_links", {"folder": folder, "items": broken}, status="logged")

    state.setdefault("folders", {})[folder] = {"graph": new_graph, "backlinks": _backlinks(new_graph), "scanned": datetime.utcnow().isoformat()}
    _save_state(state)
    return len(added_files), len(removed_files), len(broken)

# --- HTML ---

def _log_row(e):
    k, d = e["kind"], e["detail"]
    text = {"file_added": f"+ file {d.get('file')}", "file_removed": f"- file {d.get('file')}",
            "link_added": f"+ link {d.get('file')} -&gt; [[{d.get('target')}]]", "link_removed": f"- link {d.get('file')} -&gt; [[{d.get('target')}]]",
            "rename_applied": f"&#x21BA; renamed [[{d.get('old')}]] -&gt; [[{d.get('new')}]] ({d.get('files_updated')} files)",
            "broken_links": f"&#x26A0; {len(d.get('items',[]))} broken link(s) in {d.get('folder')}"}.get(k, k)
    return f'<div style="padding:.3rem .6rem;border-bottom:var(--board-thick) solid var(--border);font-size:.76rem;display:flex;gap:.5rem"><span style="color:var(--text_muted);flex-shrink:0">{e["ts"][:16].replace("T"," ")}</span><span>{text}</span></div>'

def _pending_row(e):
    d = e["detail"]
    return f"""<div style="padding:.4rem .6rem;border-bottom:var(--board-thick) solid var(--border);font-size:.78rem;display:flex;align-items:center;gap:.5rem">
        <span style="flex:1">Rename detected: [[{d.get('old')}]] &#x2192; [[{d.get('new')}]]</span>
        <button class="cm-qbtn" hx-post="{_P}/pending/{e['id']}/accept" hx-target="#lt-pending" hx-swap="outerHTML">Accept</button>
        <button class="cm-qbtn" style="color:#ff5f5f" hx-post="{_P}/pending/{e['id']}/reject" hx-target="#lt-pending" hx-swap="outerHTML">Reject</button>
    </div>"""

async def _pending_fragment():
    pending = [e for e in _load_log() if e["status"] == "pending"]
    return HTMLResponse(f'<div id="lt-pending">{"".join(_pending_row(e) for e in pending) or "<div style=color:var(--text_muted);font-size:.8rem>No pending suggestions.</div>"}</div>')

# --- Routes ---

@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    state = _load_state()
    folders_txt = "\n".join(state.get("folders", {}).keys())
    policy = state.get("policy", "retain")
    log = list(reversed(_load_log()))[:60]
    pending = [e for e in log if e["status"] == "pending"]
    policy_opts = "".join(f'<option value="{p}" {"selected" if p==policy else ""}>{l}</option>' for p,l in [("auto","Auto - rewrite references automatically"),("ask","Ask - queue rename suggestions for confirmation"),("retain","Retain - log only, never modify files")])
    return HTMLResponse(f"""<div style="max-width:60rem;margin:0 auto;padding:1.5rem">
        <h2 style="margin:0 0 1rem;font-size:1.1rem">Link Tracker</h2>
        <form hx-post="{_P}/settings/save" hx-target="#lt-status" style="display:flex;flex-direction:column;gap:.6rem;margin-bottom:1.5rem">
            <label style="font-size:.78rem;color:var(--text_muted)">Monitored folders (one path per line)<textarea name="folders" class="module-select" rows="4" style="width:100%;font-family:var(--font-mono);font-size:.78rem">{UI.escape(folders_txt)}</textarea></label>
            <label style="font-size:.78rem;color:var(--text_muted)">Policy on broken/renamed links<select name="policy" class="module-select">{policy_opts}</select></label>
            <button type="submit" class="ui-btn">Save</button><span id="lt-status"></span>
        </form>
        <button class="ui-btn" hx-post="{_P}/rescan" hx-target="#lt-results" style="margin-bottom:.6rem">&#x21BA; Rescan All Folders</button>
        <div id="lt-results" style="font-size:.8rem;color:var(--text_muted);margin-bottom:1rem"></div>
        {f'<h3 style="font-size:.9rem">Pending Suggestions ({len(pending)})</h3><div id="lt-pending">{"".join(_pending_row(e) for e in pending)}</div>' if pending else ""}
        <h3 style="font-size:.9rem;margin-top:1rem">Recent Changes</h3>
        <div style="max-height:24rem;overflow-y:auto;border:var(--board-thick) solid var(--border);border-radius:var(--radius)">{"".join(_log_row(e) for e in log) or '<div style="padding:.5rem;color:var(--text_muted);font-size:.8rem">No activity yet - rescan to populate.</div>'}</div>
        <h3 style="font-size:.9rem;margin-top:1rem">Backlinks Lookup</h3>
        <form hx-post="{_P}/backlinks" hx-target="#lt-backlinks" style="display:flex;gap:.4rem">
            <input type="text" name="path" placeholder="folder + file.md" class="module-select" style="flex:1">
            <button class="ui-btn">Find</button>
        </form>
        <div id="lt-backlinks" style="font-size:.8rem;margin-top:.4rem"></div>
    </div>""")

@router.post("/settings/save", response_class=HTMLResponse)
async def settings_save(request: Request, folders: str = Form(""), policy: str = Form("retain")):
    state = _load_state()
    folder_list = [x.strip() for x in folders.splitlines() if x.strip()]
    state["folders"] = {f: state.get("folders", {}).get(f, {}) for f in folder_list}
    state["policy"] = policy
    _save_state(state)
    return HTMLResponse('<span style="color:var(--accent)">&#x2713; Saved</span>')

@router.post("/rescan", response_class=HTMLResponse)
async def rescan(request: Request):
    state = _load_state(); policy = state.get("policy", "retain")
    results = [_rescan(f, policy) for f in state.get("folders", {})]
    add, rm, broken = sum(r[0] for r in results), sum(r[1] for r in results), sum(r[2] for r in results)
    return HTMLResponse(f'<span style="color:var(--accent)">Scanned {len(results)} folder(s) - +{add} files, -{rm} files, {broken} broken link(s)</span>')

@router.post("/pending/{eid}/accept", response_class=HTMLResponse)
async def pending_accept(eid: str):
    log = _load_log(); entry = next((e for e in log if e["id"] == eid), None)
    if entry:
        d = entry["detail"]; _apply_rename(Path(d["root"]), d["old"], d["new"], "auto")
        entry["status"] = "applied"; _save_log(log)
    return await _pending_fragment()

@router.post("/pending/{eid}/reject", response_class=HTMLResponse)
async def pending_reject(eid: str):
    log = _load_log(); entry = next((e for e in log if e["id"] == eid), None)
    if entry: entry["status"] = "rejected"; _save_log(log)
    return await _pending_fragment()

@router.post("/backlinks", response_class=HTMLResponse)
async def backlinks(request: Request, path: str = Form(...)):
    for data in _load_state().get("folders", {}).values():
        bl = data.get("backlinks", {}).get(path)
        if bl: return HTMLResponse("".join(f'<div style="padding:.15rem 0">{UI.escape(b)}</div>' for b in bl))
    return HTMLResponse('<div style="color:var(--text_muted)">No backlinks found.</div>')