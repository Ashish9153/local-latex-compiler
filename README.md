# Local LaTeX — free, offline Overleaf replacement

## Start
1. Install **Python 3** (https://www.python.org/downloads/ — on Windows tick "Add Python to PATH").
2. Double-click `start_windows.bat` (Windows) or run `./start_mac_linux.sh` (Mac/Linux).
3. If it says *"No LaTeX engine found"*, press **Enter** to download **Tectonic** (free, about 30 MB, one time only).
   Tectonic downloads any LaTeX packages it needs the first time you compile, so that first compile needs internet and is slower.
4. Your browser opens at http://127.0.0.1:8765

## Using it
- **Upload files / .zip**: upload your Overleaf project (Overleaf menu → Download → Source gives a .zip).
- Choose the right **Main** file (the one with `\documentclass`).
- **Ctrl+S** or **Ctrl+Enter** compiles. Tick **auto-compile** to recompile as you type.
- **Problems** tab: click an error or warning to jump to that line. **Raw log** shows the full LaTeX log.
- **⤓ PDF** downloads the result. Projects are saved in `~/LatexProjects/<project>`, and PDFs go to its `_build` folder.

## Why your PDF now always shows up
The compiler runs LaTeX in non-stop and force mode, so warnings never block the PDF,
and most recoverable errors still give a PDF (the status bar tells you to fix them).
Compiles can run for up to 10 minutes, so big documents don't hit a short time limit.
You only get no PDF on fatal errors, such as a missing package/file or a missing `\end{document}`.
The Problems tab shows the exact cause.

## Terminal only
    python latex_compiler.py --cli mythesis.tex
