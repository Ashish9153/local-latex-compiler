#!/usr/bin/env python3
"""
Free Local LaTeX Compiler  —  a tiny offline "Overleaf" that runs on your own computer.

  * Editor + PDF preview in your browser (http://127.0.0.1:8765)
  * ALWAYS tries to produce a PDF: compiles in non-stop / force mode, so warnings
    (and even most recoverable errors) never block the PDF.
  * Shows a clean list of errors / warnings; click one to jump to the line.
  * Multi-file projects: upload images, .bib, .cls, .sty or a whole Overleaf .zip.
  * Engines (auto-detected, in this order): Tectonic, latexmk, pdflatex/xelatex/lualatex.
    If none are installed it can download Tectonic for you (free, ~30 MB, fetches
    LaTeX packages automatically on first use).

Requirements: Python 3.8+ only (no pip packages).

Usage:
    python latex_compiler.py                 # start the app and open the browser
    python latex_compiler.py --port 9000     # different port
    python latex_compiler.py --install-tectonic
    python latex_compiler.py --cli thesis.tex   # compile one file in the terminal, no UI
"""

import argparse
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
APP_DIR = HOME / ".latex-compiler"
BIN_DIR = APP_DIR / "bin"
PROJECTS_DIR = HOME / "LatexProjects"
BUILD = "_build"
COMPILE_TIMEOUT = 600  # seconds — far more generous than Overleaf's free plan

TEXT_EXT = {".tex", ".bib", ".cls", ".sty", ".bst", ".txt", ".md", ".cfg", ".def",
            ".clo", ".bbx", ".cbx", ".lbx", ".dtx", ".ins", ".csv", ".dat", ".tikz",
            ".pgf", ".latexmkrc", ".ltx", ".log", ".json", ".yaml", ".yml", ".py", ".m"}

STARTER = r"""\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage{amsmath, amssymb}
\usepackage{graphicx}
\usepackage[margin=1in]{geometry}
\usepackage{hyperref}

\title{My First Local Document}
\author{Your Name}
\date{\today}

\begin{document}
\maketitle

\section{Introduction}
Hello! This PDF was compiled \textbf{for free} on your own computer.

\begin{equation}
  e^{i\pi} + 1 = 0
\end{equation}

\end{document}
"""


# --------------------------------------------------------------------------- engines

def exe(name):
    """Find an executable on PATH or in our private bin dir."""
    suffix = ".exe" if os.name == "nt" else ""
    local = BIN_DIR / (name + suffix)
    if local.exists():
        return str(local)
    return shutil.which(name)


def available_engines():
    found = {}
    for name in ("tectonic", "latexmk", "pdflatex", "xelatex", "lualatex"):
        p = exe(name)
        if p:
            found[name] = p
    return found


def install_tectonic(log=print):
    """Download the latest Tectonic release binary from GitHub into ~/.latex-compiler/bin."""
    sysname = platform.system().lower()
    mach = platform.machine().lower()
    arch = "aarch64" if mach in ("arm64", "aarch64") else "x86_64"
    if sysname == "linux":
        target = f"{arch}-unknown-linux-musl"
    elif sysname == "darwin":
        target = f"{arch}-apple-darwin"
    elif sysname == "windows":
        target = "x86_64-pc-windows-msvc"
    else:
        raise RuntimeError(f"Unsupported OS: {sysname}")

    log(f"Looking up latest Tectonic release for {target} ...")
    api = "https://api.github.com/repos/tectonic-typesetting/tectonic/releases/latest"
    req = urllib.request.Request(api, headers={"User-Agent": "local-latex-compiler",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        rel = json.load(r)
    asset = next((a for a in rel.get("assets", [])
                  if target in a["name"] and a["name"].endswith((".tar.gz", ".zip"))), None)
    if not asset:
        raise RuntimeError("Could not find a Tectonic download for this platform. "
                           "Install manually: https://tectonic-typesetting.github.io/")
    log(f"Downloading {asset['name']} ({asset['size'] // 1_000_000} MB) ...")
    req = urllib.request.Request(asset["browser_download_url"],
                                 headers={"User-Agent": "local-latex-compiler"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    name = "tectonic.exe" if sysname == "windows" else "tectonic"
    if asset["name"].endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next(m for m in z.namelist() if m.endswith(name))
            (BIN_DIR / name).write_bytes(z.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            member = next(m for m in t.getmembers() if m.name.endswith(name))
            (BIN_DIR / name).write_bytes(t.extractfile(member).read())
    dest = BIN_DIR / name
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    log(f"Tectonic installed at {dest}")
    return str(dest)


# --------------------------------------------------------------------------- compiling

def needs_unicode_engine(src):
    return bool(re.search(r"\\usepackage(\[[^\]]*\])?\{[^}]*\b(fontspec|polyglossia|unicode-math|xeCJK|luacode)\b", src))


def run(cmd, cwd, timeout, extra_env=None):
    env = dict(os.environ)
    env.update(extra_env or {})
    env.setdefault("max_print_line", "10000")  # keep log lines unwrapped (TeX Live)
    try:
        p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           stdin=subprocess.DEVNULL, timeout=timeout, env=env)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace")
        return -999, out + f"\n\n*** Compilation stopped after {timeout}s (timeout). ***\n"
    except FileNotFoundError:
        return -998, f"Engine not found: {cmd[0]}\n"


def compile_project(root: Path, main: str, engine: str = "auto", timeout=COMPILE_TIMEOUT):
    """Compile root/main. Returns dict with pdf path (or None), log text, issues."""
    t0 = time.time()
    main_path = (root / main).resolve()
    if not main_path.exists():
        return {"ok": False, "pdf": None, "log": f"Main file '{main}' not found.", "issues": [],
                "engine": None, "seconds": 0}
    rel_dir = main_path.parent
    stem = main_path.stem
    outdir = root / BUILD
    outdir.mkdir(exist_ok=True)
    src = main_path.read_text("utf-8", "replace")
    engines = available_engines()
    unicode_doc = needs_unicode_engine(src)

    if engine == "auto":
        if "tectonic" in engines:
            engine = "tectonic"
        elif "latexmk" in engines:
            engine = "latexmk"
        elif unicode_doc and "xelatex" in engines:
            engine = "xelatex"
        elif "pdflatex" in engines:
            engine = "pdflatex"
        elif engines:
            engine = next(iter(engines))
        else:
            return {"ok": False, "pdf": None, "issues": [], "engine": None, "seconds": 0,
                    "log": "No LaTeX engine found.\nClick 'Install Tectonic' in the toolbar "
                           "(or run: python latex_compiler.py --install-tectonic)."}

    pdf_out = outdir / (stem + ".pdf")
    # remove stale PDF so we never show an old one by mistake
    if pdf_out.exists():
        try:
            pdf_out.unlink()
        except OSError:
            pass

    log_parts = []
    cwd = str(rel_dir)
    out_abs = str(outdir.resolve())
    deadline = lambda: max(10, timeout - (time.time() - t0))

    if engine == "tectonic":
        base = [engines["tectonic"], "--outdir", out_abs, "--keep-logs", "--keep-intermediates",
                "--synctex", main_path.name]
        code, out = run(base[:1] + ["-Z", "continue-on-errors"] + base[1:], cwd, deadline())
        if "continue-on-errors" in out and ("unrecognized" in out.lower() or "invalid" in out.lower()
                                            or "unknown" in out.lower()):
            code, out = run(base, cwd, deadline())
        log_parts.append(out)
    elif engine == "latexmk":
        mode = {"xelatex": "-xelatex", "lualatex": "-lualatex"}.get(
            "xelatex" if unicode_doc else "pdf", "-pdf")
        cmd = [engines["latexmk"], mode, "-f", "-interaction=nonstopmode", "-file-line-error",
               "-synctex=1", f"-outdir={out_abs}", main_path.name]
        code, out = run(cmd, cwd, deadline())
        log_parts.append(out)
    else:
        if engine not in engines:
            return {"ok": False, "pdf": None, "issues": [], "engine": engine, "seconds": 0,
                    "log": f"Engine '{engine}' is not installed."}
        cmd = [engines[engine], "-interaction=nonstopmode", "-file-line-error", "-synctex=1",
               f"-output-directory={out_abs}", main_path.name]
        code, out = run(cmd, cwd, deadline())
        log_parts.append(out)
        aux = outdir / (stem + ".aux")
        bcf = outdir / (stem + ".bcf")
        ran_bib = False
        if bcf.exists() and exe("biber"):
            c2, o2 = run([exe("biber"), "--input-directory", out_abs, "--output-directory", out_abs,
                          stem], cwd, deadline(), {"BIBINPUTS": str(rel_dir) + os.pathsep})
            log_parts.append(o2); ran_bib = True
        elif aux.exists() and exe("bibtex") and re.search(r"\\(citation|bibdata)", aux.read_text("utf-8", "replace")):
            sep = os.pathsep
            c2, o2 = run([exe("bibtex"), stem], out_abs, deadline(),
                         {"BIBINPUTS": str(rel_dir) + sep, "BSTINPUTS": str(rel_dir) + sep})
            log_parts.append(o2); ran_bib = True
        passes = 2 if ran_bib else (1 if re.search(r"Rerun|undefined references|\\tableofcontents|\\ref\{", out + src) else 0)
        for _ in range(passes):
            code, out = run(cmd, cwd, deadline())
            log_parts.append(out)

    full_log = "\n".join(log_parts)
    texlog = outdir / (stem + ".log")
    texlog_text = texlog.read_text("utf-8", "replace") if texlog.exists() else ""
    # the final .log reflects the last pass only (no stale "rerun" warnings); tectonic also
    # prints its own "error:" lines on stdout, so include those for tectonic.
    parse_src = texlog_text if texlog_text else full_log
    if engine == "tectonic" or not pdf_out.exists():
        parse_src += "\n" + full_log
    issues = parse_issues(parse_src, main_path.name)
    pdf = pdf_out if pdf_out.exists() and pdf_out.stat().st_size > 0 else None
    return {
        "ok": pdf is not None,
        "pdf": str(pdf) if pdf else None,
        "log": (texlog_text or full_log) if pdf else (full_log + "\n\n" + texlog_text),
        "raw_output": full_log,
        "issues": issues,
        "engine": engine,
        "seconds": round(time.time() - t0, 1),
    }


ERR_FILELINE = re.compile(r"^(?:error: )?(?:\./)?([^\s:][^:\n]*\.(?:tex|sty|cls|bib|ltx)):(\d+):\s*(.+)$")
TEC_WARN = re.compile(r"^warning: (?:\./)?([^:\n]+\.(?:tex|sty|cls|bib)):(\d+):\s*(.+)$")
LATEX_WARN = re.compile(r"^(?:LaTeX|Package|Class) ?(\S*) Warning: (.+?)(?: on input line (\d+))?\.?$")
BOX = re.compile(r"^(Overfull|Underfull) \\[hv]box .*?(?:at lines? (\d+))")


def parse_issues(log, default_file):
    issues, seen = [], set()
    lines = log.splitlines()
    for i, ln in enumerate(lines):
        item = None
        m = TEC_WARN.match(ln)
        if m:
            item = ("warning", m.group(1), int(m.group(2)), m.group(3))
        elif (m := ERR_FILELINE.match(ln)):
            item = ("error", m.group(1), int(m.group(2)), m.group(3))
        elif ln.startswith("! "):
            msg, line_no = ln[2:], None
            for nxt in lines[i + 1:i + 12]:
                lm = re.match(r"^l\.(\d+)\s?(.*)", nxt)
                if lm:
                    line_no = int(lm.group(1)); msg += "  →  " + lm.group(2).strip(); break
            item = ("error", default_file, line_no, msg)
        elif (m := LATEX_WARN.match(ln)):
            msg = m.group(2)
            j = i + 1
            while j < len(lines) and lines[j].startswith("(") and len(msg) < 300 and m.group(1):
                msg += " " + lines[j].strip("() "); j += 1
            ln_m = re.search(r"on input line (\d+)", msg)
            line_no = int(m.group(3)) if m.group(3) else (int(ln_m.group(1)) if ln_m else None)
            item = ("warning", default_file, line_no, (m.group(1) + ": " if m.group(1) else "") + msg)
        elif (m := BOX.match(ln)):
            item = ("badbox", default_file, int(m.group(2)) if m.group(2) else None, ln.strip())
        elif ln.startswith("error:") and "halted" not in ln:
            item = ("error", default_file, None, ln[6:].strip())
        if item:
            key = (item[0], item[2], item[3][:120])
            if key not in seen:
                seen.add(key)
                issues.append({"type": item[0], "file": item[1], "line": item[2], "msg": item[3]})
    order = {"error": 0, "warning": 1, "badbox": 2}
    issues.sort(key=lambda x: order[x["type"]])
    return issues[:500]


# --------------------------------------------------------------------------- server

def safe_path(project, rel=""):
    root = (PROJECTS_DIR / project).resolve()
    if PROJECTS_DIR.resolve() not in root.parents:
        raise ValueError("bad project")
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ValueError("bad path")
    return root, p


def list_files(root: Path):
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if rel.parts and (rel.parts[0] == BUILD or rel.parts[0].startswith(".") or "__MACOSX" in rel.parts):
            continue
        if p.is_file():
            out.append(rel.as_posix())
    return out


def guess_main(root: Path):
    files = list_files(root)
    texs = [f for f in files if f.endswith(".tex")]
    for pref in ("main.tex", "thesis.tex", "report.tex", "paper.tex"):
        if pref in texs:
            return pref
    for f in texs:
        try:
            if "\\documentclass" in (root / f).read_text("utf-8", "replace")[:5000]:
                return f
        except OSError:
            pass
    return texs[0] if texs else None


INSTALL_STATE = {"running": False, "msg": ""}


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalLatex/1.0"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def q(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        q = self.q()
        try:
            if path == "/":
                return self.send(200, PAGE, "text/html; charset=utf-8")
            if path == "/api/status":
                return self.send(200, {"engines": list(available_engines()), "install": INSTALL_STATE,
                                       "projects_dir": str(PROJECTS_DIR)})
            if path == "/api/projects":
                PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
                return self.send(200, sorted(p.name for p in PROJECTS_DIR.iterdir() if p.is_dir()))
            if path == "/api/files":
                root, _ = safe_path(q["project"])
                return self.send(200, {"files": list_files(root), "main": guess_main(root)})
            if path == "/api/file":
                _, p = safe_path(q["project"], q["path"])
                data = p.read_bytes()
                if p.suffix.lower() in TEXT_EXT or p.name.startswith(".latexmkrc"):
                    return self.send(200, {"text": data.decode("utf-8", "replace")})
                ctype = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
                         ".jpeg": "image/jpeg", ".svg": "image/svg+xml"}.get(p.suffix.lower(),
                                                                              "application/octet-stream")
                return self.send(200, data, ctype)
            if path == "/api/pdf":
                root, p = safe_path(q["project"], str(Path(BUILD) / (Path(q["main"]).stem + ".pdf")))
                if not p.exists():
                    return self.send(404, "No PDF yet", "text/plain")
                return self.send(200, p.read_bytes(), "application/pdf")
            self.send(404, "not found", "text/plain")
        except (KeyError, ValueError, FileNotFoundError) as e:
            self.send(400, {"error": str(e)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        q = self.q()
        try:
            if path == "/api/new_project":
                name = re.sub(r"[^\w\- ]", "", json.loads(self.body())["name"]).strip() or "project"
                root, _ = safe_path(name)
                root.mkdir(parents=True, exist_ok=True)
                if not any(root.glob("*.tex")):
                    (root / "main.tex").write_text(STARTER, "utf-8")
                return self.send(200, {"name": name})
            if path == "/api/save":
                d = json.loads(self.body())
                _, p = safe_path(d["project"], d["path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(d["content"], "utf-8", newline="")
                return self.send(200, {"ok": True})
            if path == "/api/upload":
                root, p = safe_path(q["project"], q["path"])
                data = self.body()
                if p.suffix.lower() == ".zip":
                    with zipfile.ZipFile(io.BytesIO(data)) as z:
                        names = [n for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n]
                        # strip a single common top folder (typical Overleaf zip layout)
                        tops = {n.split("/")[0] for n in names}
                        strip = len(tops) == 1 and all("/" in n for n in names)
                        for n in names:
                            rel = n.split("/", 1)[1] if strip else n
                            _, dest = safe_path(q["project"], str(Path(p.parent.relative_to(root)) / rel))
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(z.read(n))
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                return self.send(200, {"ok": True})
            if path == "/api/delete":
                d = json.loads(self.body())
                _, p = safe_path(d["project"], d["path"])
                p.unlink()
                return self.send(200, {"ok": True})
            if path == "/api/rename":
                d = json.loads(self.body())
                _, a = safe_path(d["project"], d["path"])
                _, b = safe_path(d["project"], d["to"])
                b.parent.mkdir(parents=True, exist_ok=True)
                a.rename(b)
                return self.send(200, {"ok": True})
            if path == "/api/compile":
                d = json.loads(self.body())
                root, _ = safe_path(d["project"])
                res = compile_project(root, d["main"], d.get("engine", "auto"))
                res["pdf"] = bool(res["pdf"])
                return self.send(200, res)
            if path == "/api/install_tectonic":
                if not INSTALL_STATE["running"]:
                    def job():
                        INSTALL_STATE.update(running=True, msg="Starting download ...")
                        try:
                            install_tectonic(lambda m: INSTALL_STATE.update(msg=m))
                            INSTALL_STATE["msg"] = "done"
                        except Exception as e:  # noqa
                            INSTALL_STATE["msg"] = f"failed: {e}"
                        INSTALL_STATE["running"] = False
                    threading.Thread(target=job, daemon=True).start()
                return self.send(200, INSTALL_STATE)
            self.send(404, "not found", "text/plain")
        except (KeyError, ValueError, FileNotFoundError, zipfile.BadZipFile, json.JSONDecodeError) as e:
            self.send(400, {"error": str(e)})


# --------------------------------------------------------------------------- web page

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Local LaTeX</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/theme/material-darker.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/stex/stex.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/search/searchcursor.min.js"></script>
<style>
:root{--bg:#16181d;--panel:#1e2128;--line:#2c3039;--fg:#e4e6eb;--mute:#9aa0ab;--acc:#3fb37f;--err:#ef5b5b;--warn:#e7b340;--box:#7a8cff}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,Segoe UI,Roboto,sans-serif}
header{display:flex;gap:8px;align-items:center;padding:8px 12px;background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
header b{margin-right:8px;color:var(--acc)}
select,button,input{background:#262a33;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 10px;font:inherit}
button{cursor:pointer}button:hover{border-color:#4a505c}
#compile{background:var(--acc);color:#06140d;border:none;font-weight:600;padding:7px 18px}
#compile:disabled{opacity:.6;cursor:wait}
.spacer{flex:1}#stat{color:var(--mute);font-size:13px}
main{display:grid;grid-template-columns:200px 1fr 1fr;height:calc(100% - 50px)}
#side{border-right:1px solid var(--line);overflow:auto;padding:8px;background:var(--panel)}
#side h4{margin:6px 4px;color:var(--mute);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.f{padding:4px 8px;border-radius:5px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px;display:flex;gap:6px}
.f:hover{background:#2a2e37}.f.on{background:#2f3a35;color:#bff0d8}.f .m{color:var(--acc);font-size:11px}
.f .x{margin-left:auto;color:var(--mute);visibility:hidden}.f:hover .x{visibility:visible}
#side .acts{display:flex;flex-direction:column;gap:6px;margin-top:10px}
#edwrap{display:flex;flex-direction:column;min-width:0;border-right:1px solid var(--line)}
#ed{flex:1;min-height:0}.CodeMirror{height:100%;font-size:14px}
#issues{height:34%;overflow:auto;border-top:1px solid var(--line);background:#14161a}
#issues .tabs{position:sticky;top:0;display:flex;gap:4px;padding:6px;background:#14161a;border-bottom:1px solid var(--line)}
#issues .tabs button{padding:3px 10px;font-size:12px}#issues .tabs button.on{border-color:var(--acc)}
.iss{padding:6px 10px;border-bottom:1px solid #20232a;cursor:pointer;font-size:13px;display:flex;gap:8px}
.iss:hover{background:#1c1f25}.tag{font-size:11px;font-weight:700;border-radius:4px;padding:1px 6px;height:fit-content}
.error .tag{background:var(--err);color:#fff}.warning .tag{background:var(--warn);color:#201500}.badbox .tag{background:var(--box);color:#fff}
.ln{color:var(--mute);white-space:nowrap}
#raw{white-space:pre-wrap;font:12px/1.4 ui-monospace,Consolas,monospace;padding:8px;color:#c9ccd3;display:none}
#view{position:relative;background:#525659}#view iframe,#view img{width:100%;height:100%;border:0}
#empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#ddd;text-align:center;padding:30px}
.hl-line{background:rgba(239,91,91,.25)!important}
@media(max-width:900px){main{grid-template-columns:1fr;grid-auto-rows:minmax(60vh,auto);height:auto}#side{max-height:30vh}}
</style></head><body>
<header>
  <b>Local LaTeX</b>
  <select id="proj" title="Project"></select>
  <button id="newp">+ Project</button>
  <label style="color:var(--mute)">Main:</label><select id="main"></select>
  <select id="engine" title="Engine"><option value="auto">Engine: auto</option></select>
  <button id="compile">▶ Compile</button>
  <label style="color:var(--mute);font-size:13px"><input type="checkbox" id="auto"> auto-compile</label>
  <span id="stat"></span><span class="spacer"></span>
  <button id="inst" style="display:none">⬇ Install Tectonic</button>
  <a id="dl" download><button>⤓ PDF</button></a>
</header>
<main>
  <div id="side">
    <h4>Files</h4><div id="files"></div>
    <div class="acts">
      <button id="newf">+ New file</button>
      <button id="up">⤒ Upload files / .zip</button>
      <input type="file" id="upin" multiple hidden>
    </div>
  </div>
  <div id="edwrap">
    <div id="ed"></div>
    <div id="issues">
      <div class="tabs"><button data-t="issues" class="on">Problems</button><button data-t="raw">Raw log</button><span id="counts" style="margin-left:auto;color:var(--mute);font-size:12px;align-self:center"></span></div>
      <div id="list"></div><div id="raw"></div>
    </div>
  </div>
  <div id="view"><div id="empty">Press <b>&nbsp;Compile&nbsp;</b> (Ctrl+S / Ctrl+Enter) to see your PDF.</div></div>
</main>
<script>
const $=s=>document.querySelector(s);
let S={project:null,file:null,main:null,dirty:false,files:[],busy:false};
const api=async(u,o)=>{const r=await fetch(u,o);if(!r.ok){let t=await r.text();throw new Error(t)}return r};
const post=(u,d)=>api(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(r=>r.json());
// Offline fallback: if the CodeMirror CDN can't be reached, use a plain textarea editor.
if(typeof CodeMirror==='undefined'){window.CodeMirror=function(host,opt){
  const ta=document.createElement('textarea');ta.spellcheck=false;
  ta.style.cssText='width:100%;height:100%;resize:none;border:0;outline:0;padding:10px;background:#212121;color:#eee;font:14px/1.5 ui-monospace,Consolas,monospace;tab-size:2';
  host.appendChild(ta);const cbs=[],o={readOnly:false};
  ta.addEventListener('input',()=>cbs.forEach(f=>f()));
  ta.addEventListener('keydown',e=>{if(e.key==='Tab'){e.preventDefault();document.execCommand('insertText',false,'  ')}
    if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){e.preventDefault();opt.extraKeys['Ctrl-Enter']()}});
  const pos=l=>{const t=ta.value.split('\n');let n=0;for(let i=0;i<l&&i<t.length;i++)n+=t[i].length+1;return n};
  return{getValue:()=>ta.value,setValue:v=>{ta.value=v},on:(ev,f)=>cbs.push(f),clearHistory(){},
    getOption:k=>o[k],setOption:(k,v)=>{o[k]=v;if(k==='readOnly')ta.readOnly=v},
    addLineClass(){},removeLineClass(){},focus:()=>ta.focus(),
    setCursor:c=>{const a=pos(c.line);ta.focus();ta.setSelectionRange(a,a+(ta.value.split('\n')[c.line]||'').length)},
    scrollIntoView:c=>{ta.scrollTop=Math.max(0,c.line*21-ta.clientHeight/2)}}}}
const ed=CodeMirror($('#ed'),{mode:'stex',theme:'material-darker',lineNumbers:true,lineWrapping:true,indentUnit:2,
  extraKeys:{'Ctrl-S':()=>compile(),'Cmd-S':()=>compile(),'Ctrl-Enter':()=>compile(),'Cmd-Enter':()=>compile()}});
let autoT;ed.on('change',()=>{S.dirty=true;clearTimeout(autoT);if($('#auto').checked)autoT=setTimeout(compile,1500)});
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='s'){e.preventDefault();compile()}});

async function status(){const s=await (await api('/api/status')).json();
  const eng=$('#engine');eng.length=1;s.engines.forEach(n=>eng.add(new Option(n,n)));
  $('#inst').style.display=s.engines.includes('tectonic')?'none':'';
  if(s.install.running){$('#stat').textContent=s.install.msg;setTimeout(status,1500)}
  else if(s.install.msg&&s.install.msg!=='done'&&s.install.msg.startsWith('failed'))$('#stat').textContent=s.install.msg;
  if(!s.engines.length&&!s.install.running)$('#stat').textContent='No LaTeX engine found — click “Install Tectonic”.';
  return s}
async function loadProjects(sel){let ps=await (await api('/api/projects')).json();
  if(!ps.length){await post('/api/new_project',{name:'my-first-project'});ps=['my-first-project']}
  const p=$('#proj');p.innerHTML='';ps.forEach(n=>p.add(new Option(n,n)));
  p.value=sel||localStorage.getItem('proj')||ps[0];if(!p.value)p.value=ps[0];await openProject(p.value)}
async function openProject(name){await saveCur();S.project=name;localStorage.setItem('proj',name);
  const d=await (await api('/api/files?project='+enc(name))).json();S.files=d.files;
  S.main=localStorage.getItem('main:'+name);if(!S.files.includes(S.main))S.main=d.main;
  renderFiles();const m=$('#main');m.innerHTML='';S.files.filter(f=>f.endsWith('.tex')).forEach(f=>m.add(new Option(f,f)));m.value=S.main||'';
  await openFile(S.main||S.files.find(f=>/\.(tex|bib)$/.test(f))||S.files[0]);showPdf()}
const enc=encodeURIComponent;
function renderFiles(){const el=$('#files');el.innerHTML='';S.files.forEach(f=>{const d=document.createElement('div');
  d.className='f'+(f===S.file?' on':'');d.title=f;
  d.innerHTML=`<span>${f.endsWith('.tex')?'📄':/\.(png|jpe?g|pdf|svg|eps)$/i.test(f)?'🖼':'📎'}</span><span>${f.replace(/&/g,'&amp;').replace(/</g,'&lt;')}</span>${f===S.main?'<span class=m>main</span>':''}<span class=x title=Delete>✕</span>`;
  d.onclick=e=>{if(e.target.classList.contains('x'))return delFile(f);openFile(f)};el.appendChild(d)})}
async function openFile(f){if(!f)return;await saveCur();S.file=f;renderFiles();
  if(/\.(png|jpe?g|gif|svg|pdf)$/i.test(f)){const u='/api/file?project='+enc(S.project)+'&path='+enc(f);
    $('#view').innerHTML=/pdf$/i.test(f)?`<iframe src="${u}"></iframe>`:`<img style="object-fit:contain" src="${u}">`;return}
  const r=await api('/api/file?project='+enc(S.project)+'&path='+enc(f));
  const ct=r.headers.get('content-type')||'';
  if(!ct.includes('json')){ed.setValue('% Binary file: '+f);ed.setOption('readOnly',true);S.dirty=false;return}
  const d=await r.json();ed.setOption('readOnly',false);ed.setValue(d.text);ed.clearHistory();S.dirty=false}
async function saveCur(){if(S.project&&S.file&&S.dirty&&!ed.getOption('readOnly')){S.dirty=false;
  await post('/api/save',{project:S.project,path:S.file,content:ed.getValue()})}}
async function delFile(f){if(!confirm('Delete '+f+'?'))return;await post('/api/delete',{project:S.project,path:f});
  if(S.file===f){S.file=null;S.dirty=false}await openProject(S.project)}
function showPdf(){if(!S.main)return;const u='/api/pdf?project='+enc(S.project)+'&main='+enc(S.main)+'&t='+Date.now();
  fetch(u,{method:'GET'}).then(r=>{if(r.ok){$('#view').innerHTML=`<iframe src="${u}#view=FitH"></iframe>`;$('#dl').href=u;
    $('#dl').download=S.main.replace(/.*\//,'').replace(/\.tex$/,'.pdf')}})}
async function compile(){if(S.busy||!S.main)return;S.busy=true;const b=$('#compile');b.disabled=true;b.textContent='⏳ Compiling…';
  $('#stat').textContent='';try{await saveCur();
  const r=await post('/api/compile',{project:S.project,main:S.main,engine:$('#engine').value});
  renderIssues(r);$('#raw').textContent=r.log||'';
  const e=r.issues.filter(i=>i.type==='error').length,w=r.issues.filter(i=>i.type==='warning').length;
  $('#stat').innerHTML=(r.pdf?'<span style="color:var(--acc)">✔ PDF ready</span>':'<span style="color:var(--err)">✘ No PDF produced</span>')+
    ` · ${r.engine||'-'} · ${r.seconds}s`+(r.pdf&&e?' · <span style="color:var(--warn)">compiled despite '+e+' error(s) — fix them!</span>':'');
  if(r.pdf)showPdf();else{setTab('raw');if(!r.issues.length)setTab('raw')}
  }catch(err){$('#stat').textContent='Error: '+err.message}finally{S.busy=false;b.disabled=false;b.textContent='▶ Compile'}}
function renderIssues(r){const L=$('#list');L.innerHTML='';
  const c={error:0,warning:0,badbox:0};r.issues.forEach(i=>c[i.type]++);
  $('#counts').textContent=`${c.error} errors · ${c.warning} warnings · ${c.badbox} bad boxes`;
  if(!r.issues.length)L.innerHTML='<div class=iss style="color:var(--mute)">No problems found 🎉</div>';
  r.issues.forEach(i=>{const d=document.createElement('div');d.className='iss '+i.type;
    d.innerHTML=`<span class=tag>${i.type==='badbox'?'BOX':i.type.toUpperCase()}</span><span class=ln>${i.file||''}${i.line?':'+i.line:''}</span><span></span>`;
    d.lastChild.textContent=i.msg;d.onclick=()=>jump(i);L.appendChild(d)});setTab('issues')}
let hl;async function jump(i){if(!i.line)return;let f=i.file&&S.files.find(x=>x===i.file||x.endsWith('/'+i.file));
  if(f&&f!==S.file)await openFile(f);const n=i.line-1;if(hl!=null)ed.removeLineClass(hl,'background','hl-line');
  hl=n;ed.addLineClass(n,'background','hl-line');ed.setCursor({line:n,ch:0});ed.scrollIntoView({line:n,ch:0},120);ed.focus()}
function setTab(t){document.querySelectorAll('#issues .tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));
  $('#list').style.display=t==='issues'?'':'none';$('#raw').style.display=t==='raw'?'block':'none'}
document.querySelectorAll('#issues .tabs button').forEach(b=>b.onclick=()=>setTab(b.dataset.t));
$('#compile').onclick=compile;
$('#proj').onchange=e=>openProject(e.target.value);
$('#main').onchange=e=>{S.main=e.target.value;localStorage.setItem('main:'+S.project,S.main);renderFiles();showPdf()};
$('#newp').onclick=async()=>{const n=prompt('Project name:');if(!n)return;const r=await post('/api/new_project',{name:n});await loadProjects(r.name)};
$('#newf').onclick=async()=>{const n=prompt('File name (e.g. chapter1.tex, refs.bib, sections/intro.tex):');if(!n)return;
  await post('/api/save',{project:S.project,path:n,content:''});await openProject(S.project);openFile(n)};
$('#up').onclick=()=>$('#upin').click();
$('#upin').onchange=async e=>{for(const f of e.target.files){$('#stat').textContent='Uploading '+f.name+'…';
  await api('/api/upload?project='+enc(S.project)+'&path='+enc(f.name),{method:'POST',body:f})}
  $('#stat').textContent='Uploaded.';e.target.value='';await openProject(S.project)};
$('#inst').onclick=async()=>{await post('/api/install_tectonic',{});status()};
window.addEventListener('beforeunload',()=>{if(S.dirty)navigator.sendBeacon&&saveCur()});
setInterval(saveCur,5000);
status().then(()=>loadProjects());
</script></body></html>
"""


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Free local LaTeX compiler with browser editor")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--install-tectonic", action="store_true")
    ap.add_argument("--cli", metavar="FILE.tex", help="compile a file in the terminal (no UI)")
    ap.add_argument("--engine", default="auto")
    a = ap.parse_args()

    if a.install_tectonic:
        install_tectonic()
        return

    if a.cli:
        f = Path(a.cli).resolve()
        if not available_engines():
            ans = input("No LaTeX engine found. Download Tectonic now (~30 MB)? [Y/n] ").strip().lower()
            if ans in ("", "y", "yes"):
                install_tectonic()
        res = compile_project(f.parent, f.name, a.engine)
        for i in res["issues"]:
            print(f"[{i['type'].upper():7}] {i['file']}:{i['line'] or '?'}  {i['msg']}")
        if res["pdf"]:
            print(f"\nPDF ready: {res['pdf']}  ({res['engine']}, {res['seconds']}s)")
        else:
            print("\nNo PDF produced. Last part of the log:\n" + res["log"][-3000:])
            sys.exit(1)
        return

    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    if not available_engines():
        print("No LaTeX engine was found on this computer.")
        try:
            ans = input("Download the free Tectonic engine now (~30 MB)? [Y/n] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans in ("", "y", "yes"):
            try:
                install_tectonic()
            except Exception as e:  # noqa
                print(f"Download failed: {e}\nYou can retry from the 'Install Tectonic' button.")

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}/"
    print(f"\n  Local LaTeX is running →  {url}")
    print(f"  Your projects are saved in: {PROJECTS_DIR}")
    print(f"  Engines found: {', '.join(available_engines()) or 'none'}")
    print("  Press Ctrl+C to stop.\n")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")


if __name__ == "__main__":
    main()
