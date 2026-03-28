"""
NinjaBR Proxy Reverso
Proxy que permite acessar ferramentas web usando cookies armazenados.
Sistema de sessao unica: 1 usuario por vez por ferramenta.
"""
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
from fastapi import FastAPI, Request, Response, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
import uvicorn

app = FastAPI(title="NinjaBR Proxy", version="2.0")

# Sessoes ativas: {slug: {user_id, started_at, expires_at}}
_sessions = {}
SESSION_TIMEOUT = 1800  # 30 minutos de inatividade = libera sessao

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Arquivo de configuracao de ferramentas e cookies
TOOLS_FILE = "tools_config.json"
ADMIN_KEY = "ninja-admin-2026"  # chave para endpoints admin


def load_tools():
    if os.path.exists(TOOLS_FILE):
        with open(TOOLS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_tools(data):
    with open(TOOLS_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ============================================================
# ADMIN - Gerenciamento de ferramentas e cookies
# ============================================================

@app.get("/admin/tools")
def list_tools(key: str = Query(...)):
    """Lista todas as ferramentas configuradas"""
    if key != ADMIN_KEY:
        raise HTTPException(403, "Chave invalida")
    tools = load_tools()
    # Ocultar valores de cookies
    safe = {}
    for name, config in tools.items():
        safe[name] = {
            "name": config.get("name"),
            "target": config.get("target"),
            "enabled": config.get("enabled", True),
            "cookies_count": len(config.get("cookies", {})),
            "headers_count": len(config.get("headers", {})),
        }
    return {"tools": safe}


@app.post("/admin/tools/add")
def add_tool(key: str = Query(...), tool: dict = None):
    """Adiciona ou atualiza uma ferramenta
    Body: {
        "slug": "captions",
        "name": "Captions AI",
        "target": "https://captions.ai",
        "cookies": {"cookie_name": "cookie_value", ...},
        "headers": {"header_name": "header_value", ...},
        "enabled": true
    }
    """
    if key != ADMIN_KEY:
        raise HTTPException(403, "Chave invalida")
    if not tool or "slug" not in tool:
        raise HTTPException(400, "slug obrigatorio")

    tools = load_tools()
    slug = tool["slug"]
    tools[slug] = {
        "name": tool.get("name", slug),
        "target": tool.get("target", ""),
        "cookies": tool.get("cookies", {}),
        "headers": tool.get("headers", {}),
        "enabled": tool.get("enabled", True),
    }
    save_tools(tools)
    return {"status": "saved", "slug": slug}


@app.post("/admin/tools/update-cookies")
def update_cookies(key: str = Query(...), slug: str = Query(...), cookies: dict = None):
    """Atualiza apenas os cookies de uma ferramenta"""
    if key != ADMIN_KEY:
        raise HTTPException(403, "Chave invalida")
    tools = load_tools()
    if slug not in tools:
        raise HTTPException(404, "Ferramenta nao encontrada")
    tools[slug]["cookies"] = cookies or {}
    save_tools(tools)
    return {"status": "updated", "slug": slug}


@app.delete("/admin/tools/remove")
def remove_tool(key: str = Query(...), slug: str = Query(...)):
    """Remove uma ferramenta"""
    if key != ADMIN_KEY:
        raise HTTPException(403, "Chave invalida")
    tools = load_tools()
    if slug in tools:
        del tools[slug]
        save_tools(tools)
    return {"status": "removed"}


# ============================================================
# SESSAO UNICA - 1 usuario por vez por ferramenta
# ============================================================

def _clean_expired():
    """Remove sessoes expiradas"""
    now = time.time()
    expired = [s for s, d in _sessions.items() if d["expires_at"] < now]
    for s in expired:
        del _sessions[s]


@app.post("/session/start")
def session_start(slug: str = Query(...), user_id: str = Query(...)):
    """Inicia sessao - reserva ferramenta para 1 usuario"""
    _clean_expired()

    if slug in _sessions:
        current = _sessions[slug]
        if current["user_id"] != user_id:
            remaining = int(current["expires_at"] - time.time())
            return {
                "status": "occupied",
                "occupied_by": current["user_id"],
                "remaining_seconds": max(0, remaining),
                "message": f"Ferramenta em uso. Libera em {remaining // 60} min.",
            }

    _sessions[slug] = {
        "user_id": user_id,
        "started_at": time.time(),
        "expires_at": time.time() + SESSION_TIMEOUT,
    }
    return {"status": "started", "user_id": user_id, "timeout_minutes": SESSION_TIMEOUT // 60}


@app.post("/session/end")
def session_end(slug: str = Query(...), user_id: str = Query(...)):
    """Libera sessao manualmente"""
    if slug in _sessions and _sessions[slug]["user_id"] == user_id:
        del _sessions[slug]
        return {"status": "ended"}
    return {"status": "not_found"}


@app.get("/session/status")
def session_status(slug: str = Query(...)):
    """Verifica status da sessao"""
    _clean_expired()
    if slug in _sessions:
        s = _sessions[slug]
        return {
            "status": "occupied",
            "user_id": s["user_id"],
            "remaining_seconds": max(0, int(s["expires_at"] - time.time())),
        }
    return {"status": "available"}


@app.get("/session/all")
def session_all(key: str = Query(...)):
    """Lista todas as sessoes ativas (admin)"""
    if key != ADMIN_KEY:
        raise HTTPException(403, "Chave invalida")
    _clean_expired()
    return {"sessions": {k: {**v, "remaining": max(0, int(v["expires_at"] - time.time()))} for k, v in _sessions.items()}}


# ============================================================
# PROXY - Acessa ferramentas com cookies armazenados
# ============================================================

@app.api_route("/proxy/{slug}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(slug: str, path: str, request: Request):
    """Proxy reverso - acessa a ferramenta com cookies do servidor"""
    tools = load_tools()
    if slug not in tools:
        raise HTTPException(404, "Ferramenta nao encontrada")

    config = tools[slug]
    if not config.get("enabled", True):
        raise HTTPException(403, "Ferramenta desabilitada")

    # Verificar sessao - quem esta usando?
    _clean_expired()
    user_id = request.query_params.get("user_id", request.headers.get("x-user-id", ""))

    if slug in _sessions:
        current = _sessions[slug]
        if user_id and current["user_id"] != user_id:
            raise HTTPException(423, f"Ferramenta em uso por outro usuario. Tente novamente em {int((current['expires_at'] - time.time()) // 60)} minutos.")
        # Renovar timeout
        current["expires_at"] = time.time() + SESSION_TIMEOUT

    target = config["target"].rstrip("/")
    url = f"{target}/{path}"

    # Query params
    if request.query_params:
        url += "?" + str(request.query_params)

    # Headers
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Accept": request.headers.get("accept", "*/*"),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": target + "/",
    }
    # Adicionar headers customizados
    for k, v in config.get("headers", {}).items():
        headers[k] = v

    # Cookies
    cookies = config.get("cookies", {})

    # Body para POST
    body = None
    if request.method in ["POST", "PUT", "PATCH"]:
        body = await request.body()

    try:
        r = requests.request(
            method=request.method,
            url=url,
            headers=headers,
            cookies=cookies,
            data=body,
            timeout=30,
            allow_redirects=False,
            stream=True,
        )

        # Reescrever URLs no response para apontar pro proxy
        content_type = r.headers.get("content-type", "")

        # Headers de resposta
        response_headers = {}
        for k, v in r.headers.items():
            if k.lower() not in ["transfer-encoding", "content-encoding", "content-length", "set-cookie"]:
                response_headers[k] = v

        response_headers["Access-Control-Allow-Origin"] = "*"

        if "text/html" in content_type:
            # Reescrever links no HTML
            html = r.text
            parsed = urlparse(target)
            base_domain = f"{parsed.scheme}://{parsed.netloc}"

            # Substituir URLs absolutas
            html = html.replace(base_domain, f"/proxy/{slug}")
            html = html.replace(f'href="/', f'href="/proxy/{slug}/')
            html = html.replace(f"href='/", f"href='/proxy/{slug}/")
            html = html.replace(f'src="/', f'src="/proxy/{slug}/')
            html = html.replace(f"src='/", f"src='/proxy/{slug}/")
            html = html.replace(f'action="/', f'action="/proxy/{slug}/')

            return HTMLResponse(content=html, status_code=r.status_code, headers=response_headers)

        elif "redirect" in str(r.status_code) or r.status_code in [301, 302, 303, 307, 308]:
            location = r.headers.get("location", "")
            if location.startswith("/"):
                response_headers["location"] = f"/proxy/{slug}{location}"
            return Response(status_code=r.status_code, headers=response_headers)

        else:
            return Response(
                content=r.content,
                status_code=r.status_code,
                headers=response_headers,
                media_type=content_type,
            )

    except Exception as e:
        raise HTTPException(502, f"Erro no proxy: {str(e)}")


# ============================================================
# PUBLIC - Lista ferramentas disponiveis (sem cookies)
# ============================================================

@app.get("/tools")
def public_tools():
    """Lista ferramentas disponiveis para o Hub"""
    tools = load_tools()
    public = []
    for slug, config in tools.items():
        if config.get("enabled", True):
            public.append({
                "slug": slug,
                "name": config.get("name", slug),
                "url": f"/proxy/{slug}/",
            })
    return {"tools": public}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"service": "NinjaBR Proxy", "tools": "/tools", "admin": "/admin/tools?key=KEY"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)
