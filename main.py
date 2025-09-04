import os, re, time, random, pathlib, mimetypes, requests
from urllib.parse import urlparse, quote
from fastapi import FastAPI, Form, Request, Query
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

APP_DIR = pathlib.Path(__file__).parent.resolve()
DL_DIR = APP_DIR / "downloads"
DL_DIR.mkdir(exist_ok=True)

app = FastAPI(title="GitHub OG Thumbnail Downloader")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
GITHUB_GRAPHQL = "https://api.github.com/graphql"

SESSION = requests.Session()

def parse_owner_repo(repo_url: str):
    if not repo_url.startswith(("http://", "https://")):
        repo_url = "https://" + repo_url
    parsed = urlparse(repo_url)
    if "github.com" not in parsed.netloc:
        raise ValueError("URL must be a GitHub repository URL.")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("Could not parse owner/repo from URL.")
    owner = parts[0]
    repo = re.sub(r"\.git$", "", parts[1], flags=re.I)
    return owner, repo

def get_og_via_graphql(owner: str, repo: str, token: str):
    query = """
    query($owner:String!, $name:String!){
      repository(owner:$owner, name:$name){ openGraphImageUrl }
    }"""
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": BROWSER_UA
    }
    r = SESSION.post(GITHUB_GRAPHQL, json={"query": query, "variables": {"owner": owner, "name": repo}},
                     headers=headers, timeout=20)
    r.raise_for_status()
    return (r.json().get("data", {}) or {}).get("repository", {}).get("openGraphImageUrl")

def get_og_from_html(repo_url: str):
    r = SESSION.get(repo_url, headers={"User-Agent": BROWSER_UA}, timeout=20)
    r.raise_for_status()
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text, flags=re.I)
    return m.group(1) if m else None

def resolve_og(repo_url: str):
    owner, repo = parse_owner_repo(repo_url)
    token = os.getenv("GITHUB_TOKEN", "").strip() or None
    if token:
        try:
            url = get_og_via_graphql(owner, repo, token)
            if url:
                return owner, repo, url
        except Exception:
            pass
    url = get_og_from_html(f"https://github.com/{owner}/{repo}")
    if not url:
        raise RuntimeError("Could not find Open Graph image for this repository.")
    return owner, repo, url

def find_cached(owner: str, repo: str) -> pathlib.Path | None:
    for p in DL_DIR.glob(f"{owner}-{repo}-opengraph.*"):
        if p.is_file():
            return p
    return None

def download_with_retry(url: str, owner: str, repo: str, max_retries: int = 5) -> pathlib.Path:
    cached = find_cached(owner, repo)
    if cached:
        return cached

    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": f"https://github.com/{owner}/{repo}",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    backoff = 1.0
    for attempt in range(1, max_retries + 1):
        r = SESSION.get(url, headers=headers, stream=True, timeout=60)
        if r.status_code == 429:
            time.sleep(backoff + random.uniform(0, 0.35))
            backoff *= 2
            continue
        r.raise_for_status()

        ext = ".jpg"
        ctype = (r.headers.get("Content-Type") or "").lower().split(";")[0]
        guess = mimetypes.guess_extension(ctype) if ctype else None
        if guess:
            ext = guess
        elif url.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            ext = pathlib.Path(url).suffix

        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", f"{owner}-{repo}-opengraph{ext}")
        out_path = DL_DIR / safe
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
        return out_path

    raise RuntimeError(
        "GitHub rate-limited the image CDN (429). Try again shortly, or use "
        "'Open image URL' and save it directly in your browser."
    )

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "result": None, "error": None})

@app.post("/", response_class=HTMLResponse)
def fetch(request: Request, repo_url: str = Form(...)):
    try:
        owner, repo, img_url = resolve_og(repo_url)

        return templates.TemplateResponse("index.html", {
            "request": request,
            "result": {
                "owner": owner, "repo": repo,
                "image_url": img_url,
                "download_href": f"/save?owner={quote(owner)}&repo={quote(repo)}&img_url={quote(img_url, safe=':/')}"
            },
            "error": None
        })
    except Exception as e:
        return templates.TemplateResponse("index.html", {"request": request, "result": None, "error": str(e)})

@app.get("/save")
def save(owner: str = Query(...), repo: str = Query(...), img_url: str = Query(...)):
    try:
        saved = download_with_retry(img_url, owner, repo)
        return FileResponse(str(saved), filename=saved.name)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=429 if "rate-limited" in str(e).lower() else 400)

@app.get("/api/og")
def api_og(url: str = Query(..., description="GitHub repo URL")):
    try:
        owner, repo, img_url = resolve_og(url)
        return {"owner": owner, "repo": repo, "image_url": img_url}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
