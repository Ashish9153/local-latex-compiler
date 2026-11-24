#!/usr/bin/env python3
"""
LaTeX Compiler  —  a free, self-hostable Overleaf alternative (editor + PDF preview in the browser).

  * Deploy it from GitHub to a free host (Render / Hugging Face Spaces) and use it from any
    device, phone included, with nothing installed. Or run it locally with Python.
  * ALWAYS tries to produce a PDF: compiles in non-stop / force mode, so warnings
    (and most recoverable errors) never block the PDF.
  * Clean list of errors / warnings; click one to jump to the line.
  * Multi-file projects: upload images, .bib, .cls, .sty or a whole Overleaf .zip.
  * Projects are stored in the browser (IndexedDB); the server keeps nothing.

Requirements: Python 3.8+ (standard library only) and a TeX engine. The Dockerfile has everything.

Usage:
    python latex_compiler.py                    # local: start and open the browser
    python latex_compiler.py --host 0.0.0.0     # serve to the network/internet
    python latex_compiler.py --cli thesis.tex   # compile one file in the terminal
    python latex_compiler.py --install-tectonic # download a LaTeX engine (local use)

Environment variables (hosting):
    PORT                    port to listen on (set automatically by Render / HF)
    APP_PASSWORD            if set, the site asks for this password (any username)
    COMPILE_TIMEOUT         seconds per compile (default 600)
    MAX_UPLOAD_MB           max project size per request (default 60)
    MAX_PARALLEL_COMPILES   simultaneous compiles (default 2)
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
COMPILE_TIMEOUT = int(os.environ.get("COMPILE_TIMEOUT", "600"))  # seconds
HOSTED = False  # set True when served to the internet (see main())

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


SECRET_ENV = ("APP_PASSWORD", "HF_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


def run(cmd, cwd, timeout, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env.update(extra_env or {})
    env.setdefault("max_print_line", "10000")  # keep log lines unwrapped (TeX Live)
    if HOSTED:
        env["openin_any"] = "p"    # documents may not read files outside their project
        env["openout_any"] = "p"
        env["shell_escape"] = "f"  # never run shell commands from a document
    kw = {"start_new_session": True} if os.name != "nt" else {}
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, env=env, **kw)
    except FileNotFoundError:
        return -998, f"Engine not found: {cmd[0]}\n"
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(p.pid, 9)  # kill latexmk AND the pdflatex it started
            else:
                p.kill()
        except OSError:
            pass
        out, _ = p.communicate()
        return -999, out.decode("utf-8", "replace") + f"\n\n*** Compilation stopped after {int(timeout)}s (timeout). ***\n"


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
#
# The server is STATELESS: projects live in the browser (IndexedDB). On every compile the
# browser sends the project files, the server compiles them in a temporary folder, sends the
# PDF back and deletes the folder. That makes it safe to host for free (Render, Hugging Face
# Spaces, ...) where the server's disk is wiped on every restart.

import base64
import hmac
import tempfile

MAX_BODY = int(os.environ.get("MAX_UPLOAD_MB", "60")) * 1024 * 1024
JOBS = threading.BoundedSemaphore(int(os.environ.get("MAX_PARALLEL_COMPILES", "2")))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


def clean_rel(path):
    """Normalise a project-relative path and reject anything that escapes the project."""
    path = path.replace("\\", "/").lstrip("/")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or ":" in parts[0]:
        raise ValueError(f"bad file name: {path!r}")
    return "/".join(parts)


def guess_main(files):
    texs = [f for f in files if f.lower().endswith(".tex")]
    for pref in ("main.tex", "thesis.tex", "report.tex", "paper.tex"):
        if pref in texs:
            return pref
    for f in texs:
        if "\\documentclass" in str(files[f].get("t", ""))[:5000]:
            return f
    return texs[0] if texs else None


def unzip_project(data):
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n
                 and not n.split("/")[-1].startswith(".")]
        tops = {n.split("/")[0] for n in names}
        strip = len(tops) == 1 and all("/" in n for n in names)  # Overleaf-style single top folder
        total = 0
        for n in names:
            rel = clean_rel(n.split("/", 1)[1] if strip else n)
            raw = z.read(n)
            total += len(raw)
            if total > MAX_BODY:
                raise ValueError("zip is too large")
            if Path(rel).suffix.lower() in TEXT_EXT:
                files[rel] = {"t": raw.decode("utf-8", "replace")}
            else:
                files[rel] = {"b": base64.b64encode(raw).decode()}
    return {"files": files, "main": guess_main(files)}


def write_files(root: Path, files):
    for rel, f in files.items():
        dest = root / clean_rel(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if "t" in f:
            dest.write_text(f["t"], "utf-8", newline="")
        else:
            dest.write_bytes(base64.b64decode(f.get("b", "")))


def zip_project(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, f in files.items():
            z.writestr(clean_rel(rel), f["t"].encode() if "t" in f else base64.b64decode(f.get("b", "")))
    return buf.getvalue()


INSTALL_STATE = {"running": False, "msg": ""}


class Handler(BaseHTTPRequestHandler):
    server_version = "LatexCompiler/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        if HOSTED:
            sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

    def send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        if not APP_PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                pw = base64.b64decode(h[6:]).decode("utf-8", "replace").split(":", 1)[-1]
                if hmac.compare_digest(pw.encode(), APP_PASSWORD.encode()):
                    return True
            except ValueError:
                pass
        self.send(401, "Password required", "text/plain",
                  {"WWW-Authenticate": 'Basic realm="LaTeX Compiler (any username)"'})
        return False

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError(f"upload too large (limit {MAX_BODY // 1048576} MB)")
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        if not self.authorized():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if path == "/healthz":
            return self.send(200, "ok", "text/plain")
        if path == "/api/status":
            return self.send(200, {"engines": list(available_engines()), "hosted": HOSTED,
                                   "install": INSTALL_STATE, "timeout": COMPILE_TIMEOUT,
                                   "max_mb": MAX_BODY // 1048576})
        self.send(404, "not found", "text/plain")

    def do_POST(self):
        if not self.authorized():
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/compile":
                d = json.loads(self.body())
                files, main = d["files"], clean_rel(d["main"])
                if main not in files:
                    raise ValueError(f"main file {main!r} is not in the project")
                with JOBS, tempfile.TemporaryDirectory(prefix="tex-") as tmp:
                    root = Path(tmp)
                    write_files(root, files)
                    res = compile_project(root, main, d.get("engine", "auto"))
                    pdf = Path(res["pdf"]).read_bytes() if res["pdf"] else None
                res["pdf"] = base64.b64encode(pdf).decode() if pdf else None
                res["log"] = res["log"].replace(tmp, ".")
                res.pop("raw_output", None)
                return self.send(200, res)
            if path == "/api/unzip":
                return self.send(200, unzip_project(self.body()))
            if path == "/api/zip":
                d = json.loads(self.body())
                return self.send(200, zip_project(d["files"]), "application/zip")
            if path == "/api/install_tectonic" and not HOSTED:
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
        except (KeyError, ValueError, TypeError, zipfile.BadZipFile, json.JSONDecodeError,
                base64.binascii.Error) as e:
            self.send(400, {"error": str(e)})


# --------------------------------------------------------------------------- web page

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LaTeX Compiler</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%233fb37f'/><text x='16' y='22' font-size='15' font-family='serif' font-weight='bold' text-anchor='middle' fill='%2306140d'>TeX</text></svg>">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/theme/material-darker.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/stex/stex.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
<style>
:root{--bg:#16181d;--panel:#1e2128;--line:#2c3039;--fg:#e4e6eb;--mute:#9aa0ab;--acc:#3fb37f;--err:#ef5b5b;--warn:#e7b340;--box:#7a8cff}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,Segoe UI,Roboto,sans-serif}
body{display:flex;flex-direction:column}
header{display:flex;gap:8px;align-items:center;padding:8px 12px;background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
header b{margin-right:4px;color:var(--acc)}
select,button,input[type=text]{background:#262a33;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 10px;font:inherit;max-width:100%}
button{cursor:pointer;white-space:nowrap}button:hover{border-color:#4a505c}
#compile{background:var(--acc);color:#06140d;border:none;font-weight:600;padding:7px 18px}
#compile:disabled{opacity:.6;cursor:wait}
.spacer{flex:1}#stat{color:var(--mute);font-size:13px}
main{flex:1;min-height:0;display:grid;grid-template-columns:210px 1fr 1fr}
.pane{min-width:0;min-height:0}
#side{border-right:1px solid var(--line);overflow:auto;padding:8px;background:var(--panel)}
#side h4{margin:10px 4px 6px;color:var(--mute);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.f{padding:5px 8px;border-radius:5px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px;display:flex;gap:6px;align-items:center}
.f:hover{background:#2a2e37}.f.on{background:#2f3a35;color:#bff0d8}.f .m{color:var(--acc);font-size:11px}
.f .nm{overflow:hidden;text-overflow:ellipsis}.f .x{margin-left:auto;color:var(--mute);visibility:hidden;padding:0 4px}.f:hover .x,.f.on .x{visibility:visible}
.acts{display:flex;flex-direction:column;gap:6px;margin-top:8px}
.note{color:var(--mute);font-size:12px;margin:12px 4px;line-height:1.5}
#edwrap{display:flex;flex-direction:column;border-right:1px solid var(--line)}
#ed{flex:1;min-height:0}.CodeMirror{height:100%;font-size:14px}
#issues{height:32%;overflow:auto;border-top:1px solid var(--line);background:#14161a}
#issues .tabs{position:sticky;top:0;display:flex;gap:4px;padding:6px;background:#14161a;border-bottom:1px solid var(--line)}
#issues .tabs button{padding:3px 10px;font-size:12px}#issues .tabs button.on{border-color:var(--acc)}
.iss{padding:6px 10px;border-bottom:1px solid #20232a;cursor:pointer;font-size:13px;display:flex;gap:8px}
.iss:hover{background:#1c1f25}.tag{font-size:11px;font-weight:700;border-radius:4px;padding:1px 6px;height:fit-content}
.error .tag{background:var(--err);color:#fff}.warning .tag{background:var(--warn);color:#201500}.badbox .tag{background:var(--box);color:#fff}
.ln{color:var(--mute);white-space:nowrap}
#raw{white-space:pre-wrap;word-break:break-word;font:12px/1.4 ui-monospace,Consolas,monospace;padding:8px;color:#c9ccd3;display:none}
#viewwrap{display:flex;flex-direction:column;background:#525659}
#vbar{display:flex;gap:6px;align-items:center;padding:5px 8px;background:#3b3e42;font-size:13px}
#vbar button{padding:3px 9px}
#view{flex:1;overflow:auto;position:relative}
#view canvas{display:block;margin:10px auto;box-shadow:0 2px 8px rgba(0,0,0,.4);background:#fff;max-width:none}
#view iframe,#view img{width:100%;height:100%;border:0;display:block}
#empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#ddd;text-align:center;padding:30px}
.hl-line{background:rgba(239,91,91,.25)!important}
#mtabs{display:none}
@media(max-width:900px){
  header{padding:6px 8px;gap:6px}header b{display:none}
  main{grid-template-columns:1fr}#side:not(.show),#edwrap:not(.show),#viewwrap:not(.show){display:none}#edwrap.show,#viewwrap.show{display:flex;flex-direction:column}
  #side.show{display:block}#edwrap,#side{border:0}
  #mtabs{display:flex;border-top:1px solid var(--line);background:var(--panel)}
  #mtabs button{flex:1;border:0;border-radius:0;padding:11px;background:none;color:var(--mute)}
  #mtabs button.on{color:var(--acc);box-shadow:inset 0 2px 0 var(--acc)}
  .hide-m{display:none}
}
</style></head><body>
<header>
  <b>LaTeX&nbsp;Compiler</b>
  <select id="proj" title="Project"></select>
  <select id="main" title="Main file (the one with \documentclass)"></select>
  <select id="engine" class="hide-m" title="Engine"><option value="auto">Engine: auto</option></select>
  <button id="compile" title="Ctrl+S / Ctrl+Enter">▶ Compile</button>
  <label class="hide-m" style="color:var(--mute);font-size:13px"><input type="checkbox" id="auto"> auto</label>
  <span id="stat"></span><span class="spacer"></span>
  <button id="inst" style="display:none">⬇ Install Tectonic</button>
  <button id="dl">⤓ PDF</button>
</header>
<main>
  <div id="side" class="pane">
    <h4>Files</h4><div id="files"></div>
    <div class="acts">
      <button id="newf">+ New file</button>
      <button id="up">⤒ Upload files / Overleaf .zip</button>
      <input type="file" id="upin" multiple hidden>
    </div>
    <h4>Project</h4>
    <div class="acts">
      <button id="newp">+ New project</button>
      <button id="renp">✎ Rename project</button>
      <button id="exp">⤓ Download project (.zip)</button>
      <button id="delp">🗑 Delete project</button>
    </div>
    <p class="note">Your projects are saved <b>in this browser</b> (nothing is stored on the server).
      Use “Download project” now and then as a backup, or before switching devices.</p>
  </div>
  <div id="edwrap" class="pane show">
    <div id="ed"></div>
    <div id="issues">
      <div class="tabs"><button data-t="issues" class="on">Problems</button><button data-t="raw">Raw log</button><span id="counts" style="margin-left:auto;color:var(--mute);font-size:12px;align-self:center"></span></div>
      <div id="list"></div><div id="raw"></div>
    </div>
  </div>
  <div id="viewwrap" class="pane">
    <div id="vbar"><button id="zo">−</button><span id="zl">Fit</span><button id="zi">+</button><button id="zf">Fit width</button><span class="spacer"></span><span id="pages" style="color:#ccc"></span></div>
    <div id="view"><div id="empty">Press&nbsp;<b>Compile</b>&nbsp;to see your PDF.</div></div>
  </div>
</main>
<nav id="mtabs"><button data-p="side">📁 Files</button><button data-p="edwrap" class="on">✎ Editor</button><button data-p="viewwrap">📄 PDF</button></nav>
<script>
const $=s=>document.querySelector(s), enc=encodeURIComponent;
const TEXT=/\.(tex|bib|cls|sty|bst|txt|md|cfg|def|clo|bbx|cbx|lbx|dtx|ins|csv|dat|tikz|pgf|ltx|json|ya?ml|py|m|latexmkrc)$/i;
let S={project:null,file:null,data:null,busy:false,pdf:null,scale:0};

/* ---------- storage: IndexedDB (falls back to memory if blocked) ---------- */
const DB={db:null,mem:{},
  async open(){try{this.db=await new Promise((ok,no)=>{const r=indexedDB.open('latex-compiler',1);
    r.onupgradeneeded=()=>r.result.createObjectStore('projects');r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error)})}
    catch(e){console.warn('IndexedDB unavailable, projects will not persist',e)}},
  tx(mode,fn){return new Promise((ok,no)=>{const t=this.db.transaction('projects',mode),st=t.objectStore('projects'),r=fn(st);
    t.oncomplete=()=>ok(r&&r.result);t.onerror=()=>no(t.error)})},
  async keys(){return this.db?(await this.tx('readonly',s=>s.getAllKeys())).sort():Object.keys(this.mem).sort()},
  async get(k){return this.db?this.tx('readonly',s=>s.get(k)):this.mem[k]},
  async put(k,v){if(this.db)await this.tx('readwrite',s=>s.put(v,k));else this.mem[k]=v},
  async del(k){if(this.db)await this.tx('readwrite',s=>s.delete(k));else delete this.mem[k]}};
const pref={get:k=>{try{return localStorage.getItem(k)}catch(e){return null}},set:(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}}};

const STARTER=String.raw`\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage{amsmath, amssymb}
\usepackage{graphicx}
\usepackage[margin=1in]{geometry}
\usepackage{hyperref}

\title{My First Document}
\author{Your Name}
\date{\today}

\begin{document}
\maketitle

\section{Introduction}
Hello! This PDF was compiled \textbf{for free}.

\begin{equation}
  e^{i\pi} + 1 = 0
\end{equation}

\end{document}
`;

/* ---------- editor (CodeMirror, or plain textarea if the CDN is unreachable) ---------- */
if(typeof CodeMirror==='undefined'){window.CodeMirror=function(host,opt){
  const ta=document.createElement('textarea');ta.spellcheck=false;
  ta.style.cssText='width:100%;height:100%;resize:none;border:0;outline:0;padding:10px;background:#212121;color:#eee;font:14px/1.5 ui-monospace,Consolas,monospace;tab-size:2';
  host.appendChild(ta);const cbs=[],o={readOnly:false};ta.addEventListener('input',()=>cbs.forEach(f=>f()));
  ta.addEventListener('keydown',e=>{if(e.key==='Tab'){e.preventDefault();document.execCommand('insertText',false,'  ')}
    if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){e.preventDefault();compile()}});
  const pos=l=>{const t=ta.value.split('\n');let n=0;for(let i=0;i<l&&i<t.length;i++)n+=t[i].length+1;return n};
  return{getValue:()=>ta.value,setValue:v=>{ta.value=v},on:(ev,f)=>cbs.push(f),clearHistory(){},refresh(){},
    getOption:k=>o[k],setOption:(k,v)=>{o[k]=v;if(k==='readOnly')ta.readOnly=v},addLineClass(){},removeLineClass(){},focus:()=>ta.focus(),
    setCursor:c=>{const a=pos(c.line);ta.focus();ta.setSelectionRange(a,a+(ta.value.split('\n')[c.line]||'').length)},
    scrollIntoView:c=>{ta.scrollTop=Math.max(0,c.line*21-ta.clientHeight/2)}}}}
const ed=CodeMirror($('#ed'),{mode:'stex',theme:'material-darker',lineNumbers:true,lineWrapping:true,indentUnit:2,
  extraKeys:{'Ctrl-S':()=>compile(),'Cmd-S':()=>compile(),'Ctrl-Enter':()=>compile(),'Cmd-Enter':()=>compile()}});
let saveT,autoT,loading=false;
ed.on('change',()=>{if(loading||!S.file||ed.getOption('readOnly'))return;S.data.files[S.file]={t:ed.getValue()};
  clearTimeout(saveT);saveT=setTimeout(saveProject,700);clearTimeout(autoT);if($('#auto').checked)autoT=setTimeout(compile,1800)});
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='s'){e.preventDefault();compile()}});
async function saveProject(){if(S.project&&S.data){S.data.updated=Date.now();await DB.put(S.project,S.data)}}

/* ---------- api ---------- */
async function api(url,body,raw){const r=await fetch(url,{method:'POST',headers:raw?{}:{'Content-Type':'application/json'},body:raw?body:JSON.stringify(body)});
  if(!r.ok){let m=await r.text();try{m=JSON.parse(m).error||m}catch(e){}throw new Error(m)}return r}

/* ---------- projects & files ---------- */
async function loadProjects(sel){let ps=await DB.keys();
  if(!ps.length){await DB.put('my-first-project',{files:{'main.tex':{t:STARTER}},main:'main.tex'});ps=['my-first-project']}
  const p=$('#proj');p.innerHTML='';ps.forEach(n=>p.add(new Option(n,n)));
  const want=sel||pref.get('proj');p.value=ps.includes(want)?want:ps[0];await openProject(p.value)}
async function openProject(name){clearTimeout(saveT);await saveProject();S.project=name;pref.set('proj',name);
  S.data=await DB.get(name)||{files:{},main:null};S.file=null;
  if(!S.data.files[S.data.main])S.data.main=guessMain();
  renderFiles();renderMain();clearPdf();
  await openFile(pref.get('file:'+name)in S.data.files?pref.get('file:'+name):S.data.main||Object.keys(S.data.files).find(f=>TEXT.test(f)))}
function guessMain(){const t=Object.keys(S.data.files).filter(f=>/\.tex$/i.test(f));
  return t.find(f=>f==='main.tex')||t.find(f=>/\\documentclass/.test((S.data.files[f].t||'').slice(0,5000)))||t[0]||null}
function renderMain(){const m=$('#main');m.innerHTML='';Object.keys(S.data.files).filter(f=>/\.tex$/i.test(f)).sort().forEach(f=>m.add(new Option(f,f)));m.value=S.data.main||''}
const esc=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;');
function renderFiles(){const el=$('#files');el.innerHTML='';Object.keys(S.data.files).sort().forEach(f=>{const d=document.createElement('div');
  d.className='f'+(f===S.file?' on':'');d.title=f;
  d.innerHTML=`<span>${/\.tex$/i.test(f)?'📄':/\.(png|jpe?g|pdf|svg|eps|gif)$/i.test(f)?'🖼':'📎'}</span><span class=nm>${esc(f)}</span>${f===S.data.main?'<span class=m>main</span>':''}<span class=x title="Rename / delete">⋯</span>`;
  d.onclick=e=>{if(e.target.classList.contains('x'))return fileMenu(f);openFile(f);mtab('edwrap')};el.appendChild(d)})}
async function fileMenu(f){const n=prompt(`Rename "${f}" (leave empty to DELETE it):`,f);if(n===null||n===f)return;
  if(n===''){if(!confirm('Delete '+f+'?'))return;delete S.data.files[f]}
  else{S.data.files[n.trim()]=S.data.files[f];delete S.data.files[f];if(S.data.main===f)S.data.main=n.trim()}
  if(!S.data.files[S.data.main])S.data.main=guessMain();await saveProject();S.file=null;renderFiles();renderMain();openFile(S.data.main)}
function mime(f){return({png:'image/png',jpg:'image/jpeg',jpeg:'image/jpeg',gif:'image/gif',svg:'image/svg+xml',pdf:'application/pdf'})[f.split('.').pop().toLowerCase()]}
async function openFile(f){if(!f){loading=true;ed.setValue('');loading=false;return}S.file=f;pref.set('file:'+S.project,f);renderFiles();
  const d=S.data.files[f];loading=true;
  if('t' in d){ed.setOption('readOnly',false);ed.setValue(d.t);ed.clearHistory()}
  else{ed.setOption('readOnly',true);ed.setValue('% '+f+' is a binary file ('+Math.round(d.b.length*.75/1024)+' KB). It is sent along when you compile.');
    if(mime(f)&&!/pdf$/i.test(f)){$('#view').innerHTML=`<img style="object-fit:contain" src="data:${mime(f)};base64,${d.b}">`;S.pdf=null}}
  loading=false;setTimeout(()=>ed.refresh(),0)}
const b64=buf=>{let s='',a=new Uint8Array(buf);for(let i=0;i<a.length;i+=0x8000)s+=String.fromCharCode.apply(null,a.subarray(i,i+0x8000));return btoa(s)};
async function addUploads(list){for(const f of list){$('#stat').textContent='Reading '+f.name+'…';
  if(/\.zip$/i.test(f.name)){const r=await(await api('/api/unzip',f,true)).json();Object.assign(S.data.files,r.files);
    if(r.main&&(!S.data.main||!confirm('Keep "'+S.data.main+'" as main file? (Cancel = use "'+r.main+'")')))S.data.main=r.main}
  else if(TEXT.test(f.name))S.data.files[f.name]={t:await f.text()};
  else S.data.files[f.name]={b:b64(await f.arrayBuffer())}}
  if(!S.data.files[S.data.main])S.data.main=guessMain();await saveProject();renderFiles();renderMain();$('#stat').textContent='Uploaded ✔';openFile(S.data.main)}

/* ---------- compile ---------- */
async function compile(){if(S.busy)return;if(!S.data.main){alert('Add a .tex file first.');return}
  S.busy=true;const b=$('#compile');b.disabled=true;b.textContent='⏳ Compiling…';$('#stat').textContent='';
  clearTimeout(saveT);await saveProject();const t0=Date.now();
  try{const r=await(await api('/api/compile',{main:S.data.main,engine:$('#engine').value,files:S.data.files})).json();
    renderIssues(r);$('#raw').textContent=r.log||'';
    const e=r.issues.filter(i=>i.type==='error').length;
    $('#stat').innerHTML=(r.pdf?'<span style="color:var(--acc)">✔ PDF ready</span>':'<span style="color:var(--err)">✘ No PDF — see Problems / Raw log</span>')+
      ` · ${r.seconds}s`+(r.pdf&&e?` · <span style="color:var(--warn)">${e} error(s) to fix</span>`:'');
    if(r.pdf){await showPdf(r.pdf);mtab('viewwrap')}else{setTab(r.issues.some(i=>i.type==='error')?'issues':'raw');mtab('edwrap')}
  }catch(err){$('#stat').innerHTML='<span style="color:var(--err)">'+esc(err.message.slice(0,200))+'</span>'}
  finally{S.busy=false;b.disabled=false;b.textContent='▶ Compile'}}
function clearPdf(){S.pdf=null;$('#view').innerHTML='<div id="empty">Press&nbsp;<b>Compile</b>&nbsp;to see your PDF.</div>';$('#pages').textContent=''}
async function showPdf(b64s){const bin=atob(b64s),u=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i);
  S.pdf=new Blob([u],{type:'application/pdf'});await renderPdf()}
async function renderPdf(){if(!S.pdf)return;const v=$('#view'),keep=v.scrollTop/(v.scrollHeight||1);
  if(!window.pdfjsLib){const url=URL.createObjectURL(S.pdf);v.innerHTML=`<iframe src="${url}#view=FitH"></iframe>`;return}
  pdfjsLib.GlobalWorkerOptions.workerSrc='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
  const doc=await pdfjsLib.getDocument({data:new Uint8Array(await S.pdf.arrayBuffer())}).promise;
  const frag=document.createDocumentFragment(),dpr=window.devicePixelRatio||1,width=v.clientWidth-24;
  for(let n=1;n<=doc.numPages;n++){const pg=await doc.getPage(n),vp1=pg.getViewport({scale:1});
    const sc=S.scale||Math.max(.3,width/vp1.width),vp=pg.getViewport({scale:sc*dpr}),c=document.createElement('canvas');
    c.width=vp.width;c.height=vp.height;c.style.width=(vp.width/dpr)+'px';c.style.height=(vp.height/dpr)+'px';
    await pg.render({canvasContext:c.getContext('2d'),viewport:vp}).promise;frag.appendChild(c)}
  v.innerHTML='';v.appendChild(frag);v.scrollTop=keep*v.scrollHeight;
  $('#pages').textContent=doc.numPages+' page'+(doc.numPages>1?'s':'');$('#zl').textContent=S.scale?Math.round(S.scale*100)+'%':'Fit'}
function zoom(k){S.scale=k?Math.min(4,Math.max(.25,(S.scale||fitScale())*k)):0;renderPdf()}
function fitScale(){const c=$('#view canvas');return c?parseFloat(c.style.width)/(c.width/(window.devicePixelRatio||1)):1}
function renderIssues(r){const L=$('#list');L.innerHTML='';const c={error:0,warning:0,badbox:0};r.issues.forEach(i=>c[i.type]++);
  $('#counts').textContent=`${c.error} errors · ${c.warning} warnings · ${c.badbox} boxes`;
  if(!r.issues.length)L.innerHTML='<div class=iss style="color:var(--mute)">No problems found 🎉</div>';
  r.issues.forEach(i=>{const d=document.createElement('div');d.className='iss '+i.type;
    d.innerHTML=`<span class=tag>${i.type==='badbox'?'BOX':i.type.toUpperCase()}</span><span class=ln>${esc(i.file||'')}${i.line?':'+i.line:''}</span><span></span>`;
    d.lastChild.textContent=i.msg;d.onclick=()=>jump(i);L.appendChild(d)});setTab('issues')}
let hl;async function jump(i){if(!i.line)return;const f=i.file&&Object.keys(S.data.files).find(x=>x===i.file||x.endsWith('/'+i.file));
  if(f&&f!==S.file)await openFile(f);mtab('edwrap');const n=i.line-1;if(hl!=null)ed.removeLineClass(hl,'background','hl-line');
  hl=n;ed.addLineClass(n,'background','hl-line');ed.setCursor({line:n,ch:0});ed.scrollIntoView({line:n,ch:0},120);ed.focus()}
function setTab(t){document.querySelectorAll('#issues .tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));
  $('#list').style.display=t==='issues'?'':'none';$('#raw').style.display=t==='raw'?'block':'none'}
function mtab(p){if(innerWidth>900)return;document.querySelectorAll('.pane').forEach(x=>x.classList.toggle('show',x.id===p));
  document.querySelectorAll('#mtabs button').forEach(b=>b.classList.toggle('on',b.dataset.p===p));if(p==='edwrap')setTimeout(()=>ed.refresh(),0);if(p==='viewwrap'&&S.pdf&&!S.scale)renderPdf()}
function download(blob,name){const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),5000)}

/* ---------- wiring ---------- */
document.querySelectorAll('#issues .tabs button').forEach(b=>b.onclick=()=>setTab(b.dataset.t));
document.querySelectorAll('#mtabs button').forEach(b=>b.onclick=()=>mtab(b.dataset.p));
$('#compile').onclick=compile;
$('#zi').onclick=()=>zoom(1.2);$('#zo').onclick=()=>zoom(1/1.2);$('#zf').onclick=()=>zoom(0);
let rz;addEventListener('resize',()=>{clearTimeout(rz);rz=setTimeout(()=>{if(!S.scale)renderPdf()},300)});
$('#proj').onchange=e=>openProject(e.target.value);
$('#main').onchange=async e=>{S.data.main=e.target.value;await saveProject();renderFiles()};
$('#dl').onclick=()=>S.pdf?download(S.pdf,(S.data.main||'document').replace(/.*\//,'').replace(/\.tex$/i,'')+'.pdf'):alert('Compile first.');
$('#newp').onclick=async()=>{const n=(prompt('New project name:')||'').trim();if(!n)return;
  if((await DB.keys()).includes(n))return alert('A project with that name already exists.');
  await saveProject();await DB.put(n,{files:{'main.tex':{t:STARTER}},main:'main.tex'});await loadProjects(n)};
$('#renp').onclick=async()=>{const n=(prompt('Rename project to:',S.project)||'').trim();if(!n||n===S.project)return;
  await saveProject();await DB.put(n,S.data);await DB.del(S.project);S.project=null;await loadProjects(n)};
$('#delp').onclick=async()=>{if(!confirm('Delete project "'+S.project+'" from this browser? Download it first if you need it.'))return;
  await DB.del(S.project);S.project=null;S.data=null;await loadProjects()};
$('#exp').onclick=async()=>{await saveProject();const r=await api('/api/zip',{files:S.data.files});download(await r.blob(),S.project+'.zip')};
$('#newf').onclick=async()=>{const n=(prompt('File name (e.g. chapter1.tex, refs.bib, sections/intro.tex):')||'').trim();if(!n)return;
  if(!S.data.files[n])S.data.files[n]={t:''};if(!S.data.main&&/\.tex$/i.test(n))S.data.main=n;await saveProject();renderFiles();renderMain();openFile(n)};
$('#up').onclick=()=>$('#upin').click();
$('#upin').onchange=async e=>{try{await addUploads([...e.target.files])}catch(err){$('#stat').textContent='Upload failed: '+err.message}e.target.value=''};
$('#view').addEventListener('dragover',e=>e.preventDefault());
document.addEventListener('drop',e=>{if(e.dataTransfer.files.length){e.preventDefault();addUploads([...e.dataTransfer.files])}});
document.addEventListener('dragover',e=>e.preventDefault());
$('#inst').onclick=async()=>{await api('/api/install_tectonic',{});status()};
addEventListener('visibilitychange',()=>{if(document.hidden)saveProject()});
async function status(){try{const s=await(await fetch('/api/status')).json();const eng=$('#engine');eng.length=1;s.engines.forEach(n=>eng.add(new Option(n,n)));
  $('#inst').style.display=(!s.hosted&&!s.engines.includes('tectonic'))?'':'none';
  if(s.install.running){$('#stat').textContent=s.install.msg;setTimeout(status,1500)}
  else if(!s.engines.length)$('#stat').textContent=s.hosted?'Server has no LaTeX engine installed.':'No LaTeX engine found — click “Install Tectonic”.'}catch(e){}}
(async()=>{await DB.open();await status();await loadProjects()})();
</script></body></html>
"""


# --------------------------------------------------------------------------- main

def main():
    global HOSTED
    ap = argparse.ArgumentParser(description="Free LaTeX compiler with a browser editor")
    ap.add_argument("--host", default=None, help="interface to listen on (0.0.0.0 = public)")
    ap.add_argument("--port", type=int, default=None)
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

    # Cloud hosts (Render, Hugging Face, Railway, Fly...) tell us the port via $PORT.
    port = a.port or int(os.environ.get("PORT", "0") or 0) or 8765
    host = a.host or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    HOSTED = host not in ("127.0.0.1", "localhost", "::1")

    if not HOSTED and not available_engines():
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

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    url = f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}/"
    print(f"\n  LaTeX Compiler running on {host}:{port}  ({'public server' if HOSTED else url})")
    print(f"  Engines: {', '.join(available_engines()) or 'none'}")
    print(f"  Password protection: {'ON' if APP_PASSWORD else 'off'}", flush=True)
    if not HOSTED and not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")


if __name__ == "__main__":
    main()
