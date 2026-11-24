# LaTeX Compiler: a free Overleaf alternative

Write LaTeX in your browser, click **Compile**, and get a PDF. You can host it for free
from this GitHub repo and use it on any device (phone, tablet, laptop) **without installing anything**.

- **Your PDF always appears.** It compiles in non-stop/force mode, so warnings (and most errors) don't block it.
- **Problems list**: click an error or warning to jump to that line. The full log is one tab away.
- **Overleaf projects**: upload the `.zip` from Overleaf (*Menu → Download → Source*), plus images, `.bib`, `.cls` and `.sty` files.
- BibTeX & **biblatex/biber**, TikZ, beamer, IEEE/ACM/Springer/Elsevier classes, XeLaTeX/LuaLaTeX (`fontspec`).
- **Projects are saved in your browser.** The server stores nothing. Use *Download project (.zip)* as a backup.
- Works on phones (Files / Editor / PDF tabs).
- Optional password so only you can use your server.

---

## Option A: Render (deploys straight from GitHub, easiest)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Ashish9153/local-latex-compiler)

1. Click the button above and sign in to Render **with GitHub** (free, no card needed).
2. When it asks for `APP_PASSWORD`, type a password. Anyone visiting the site will need it (any username works).
3. Click **Deploy**. The first build takes about 5–10 minutes because it installs TeX Live.
4. Open the `https://latex-compiler-xxxx.onrender.com` link it gives you, then bookmark it or add it to your phone's home screen.

Every `git push` to this repo redeploys it automatically.

Free plan limits: the server **sleeps after 15 min idle**, so the first visit afterwards takes about a minute to wake up.
It also has a small CPU, so big documents compile slower. If that bothers you, use Option B.

## Option B: Hugging Face Spaces (free, faster: 2 CPUs / 16 GB RAM)

1. Create a free account at https://huggingface.co.
2. **New Space**: name it `latex-compiler`, choose **SDK: Docker → Blank**, and pick Public or Private.
3. Create a token at https://huggingface.co/settings/tokens. It must be **Write** type.
4. In this GitHub repo, open **Settings → Secrets and variables → Actions**:
   - **Secrets** tab → *New repository secret*: name `HF_TOKEN`, value = the token.
   - **Variables** tab → *New repository variable*: name `HF_SPACE`, value = `your-hf-username/latex-compiler`.
5. Open the **Actions** tab → *Sync to Hugging Face Space* → **Run workflow**.
   After this, every push to GitHub updates the Space automatically.
6. Optional: in the Space's **Settings → Variables and secrets**, add a secret `APP_PASSWORD`.

Your app will be at `https://huggingface.co/spaces/your-hf-username/latex-compiler`.
Spaces also sleep after about 48 h without visitors and wake up when you open them.

## Option C: GitHub Codespaces (nothing to set up)

Click **Code → Codespaces → Create codespace** on this repo, then run:

    sudo apt-get update && sudo apt-get install -y latexmk texlive-latex-extra texlive-science
    python3 latex_compiler.py --host 0.0.0.0 --port 8765

Then open the forwarded port. The free tier gives a monthly allowance of hours.

## Option D: run it on your own computer

Needs Python 3. Double-click `start_windows.bat` (Windows) or run `./start_mac_linux.sh` (Mac/Linux).
If no LaTeX is installed, it offers to download the free Tectonic engine (about 30 MB).
Or use Docker:

    docker build -t latex-compiler . && docker run -p 7860:7860 latex-compiler

---

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `APP_PASSWORD` | *(none)* | Ask visitors for this password |
| `COMPILE_TIMEOUT` | `600` | Max seconds per compile |
| `MAX_UPLOAD_MB` | `60` | Max project size |
| `MAX_PARALLEL_COMPILES` | `2` | Compiles running at the same time |

**Missing a package?** Add the matching Ubuntu `texlive-*` package to the `apt-get install` line in
`Dockerfile` (for example `texlive-lang-cyrillic`, `texlive-lang-chinese`, `texlive-fonts-extra`) and push.

**Security:** documents can't run shell commands (`\write18` is disabled) or read files outside
their own project. Each compile runs in a temporary folder that is deleted afterwards.
