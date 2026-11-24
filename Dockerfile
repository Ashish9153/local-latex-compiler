# LaTeX Compiler — container image used by Render, Hugging Face Spaces, Railway, Fly.io, etc.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PORT=7860

# A broad TeX Live selection (covers most theses, papers, IEEE/ACM/Springer/Elsevier classes,
# TikZ, beamer, biblatex+biber, XeLaTeX/LuaLaTeX). Add more texlive-* packages here if needed,
# e.g. texlive-lang-cyrillic, texlive-lang-chinese, texlive-fonts-extra (large).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 latexmk biber \
        texlive-latex-base texlive-latex-recommended texlive-latex-extra \
        texlive-fonts-recommended texlive-science texlive-pictures \
        texlive-bibtex-extra texlive-publishers texlive-plain-generic \
        texlive-xetex texlive-luatex lmodern fonts-lmodern cm-super \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/texlive/texmf-dist/doc

# The web server runs as root ONLY so that it can run every compile as its own throw-away
# user (uid 61000+): compiles then cannot read each other's files or the server's secrets.
# (If a host forces a non-root user, the app still works, just without per-compile isolation.)
RUN (userdel -r ubuntu 2>/dev/null || true) && useradd -m -u 1000 app
WORKDIR /app
COPY latex_compiler.py ./
COPY static ./static
RUN chmod -R a+rX /app

# Warm up font caches so the first real compile is fast
RUN mkdir /tmp/w && cd /tmp/w \
 && printf '\\documentclass{article}\\usepackage{amsmath,tikz,graphicx,hyperref}\\begin{document}Hi $x^2$\\end{document}\n' > a.tex \
 && printf '\\documentclass{article}\\usepackage{fontspec}\\begin{document}Hi\\end{document}\n' > b.tex \
 && pdflatex -interaction=nonstopmode a.tex >/dev/null \
 && (lualatex -interaction=nonstopmode b.tex >/dev/null || true) \
 && cd / && rm -rf /tmp/w && chmod -R a+rX /var/lib/texmf

EXPOSE 7860
HEALTHCHECK --interval=60s --timeout=5s CMD python3 -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/healthz')" || exit 1
CMD ["sh", "-c", "exec python3 latex_compiler.py --host 0.0.0.0 --port ${PORT}"]
