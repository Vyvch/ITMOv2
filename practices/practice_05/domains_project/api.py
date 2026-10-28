from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import datetime
import time
import os

from services import (
    get_domains, evaluate_top_domains, filter_domains, InvalidPatternError,
    get_wayback_history, shutdown_service, HistoryPending, WaybackError,
)
from mcp_client import mcp_client
from models import DomainItem, WaybackPending

app = FastAPI(title="Freeing Domains API")

# Setting up templates directory
current_dir = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(current_dir, "templates"))

@app.on_event("startup")
async def startup_event():
    mcp_client.start()

@app.on_event("shutdown")
async def shutdown_event():
    mcp_client.close()
    await shutdown_service()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})

@app.get("/api/domains")
def api_get_domains(regex: str | None = None) -> list[dict]:
    # Returns raw list for the frontend; optional RE2 filter by domain name
    domains = get_domains()
    if regex:
        try:
            domains = filter_domains(domains, regex)
        except InvalidPatternError as e:
            raise HTTPException(status_code=400, detail=f"Некорректное регулярное выражение: {e}")
    return [d.dict() for d in domains]

@app.get("/api/wayback")
async def api_wayback(domain: str = "") -> JSONResponse:
    # История домена в веб-архиве; формат — specs/wayback-contract.md.
    # Пустое значение по умолчанию: без параметра — 400 invalid_domain, а не 422 FastAPI.
    try:
        history = await get_wayback_history(domain)
    except HistoryPending as e:
        body = WaybackPending(domain=e.domain, retry_after=e.retry_after)
        return JSONResponse(status_code=202, content=body.model_dump(),
                            headers={"Retry-After": str(e.retry_after)})
    except WaybackError as e:
        # JSONResponse, а не HTTPException: тело ошибки — {"code", "detail"} на верхнем уровне
        headers = {"Retry-After": str(e.retry_after)} if e.retry_after else None
        return JSONResponse(status_code=e.status, content={"code": e.code, "detail": e.detail},
                            headers=headers)
    return JSONResponse(content=history.model_dump())

_LLM_CACHE = {}

@app.get("/api/top")
def api_get_top(date_type: str = "today") -> list[dict]:
    global _LLM_CACHE
    
    # Check cache (1 hour)
    cached = _LLM_CACHE.get(date_type)
    if cached and time.time() - cached["timestamp"] < 3600:
        return cached["data"]
        
    domains_data = get_domains()
    if not domains_data:
        return []
    
    if date_type == "tomorrow":
        target_date = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    else:
        target_date = datetime.date.today().isoformat()
    
    target_domains = [d for d in domains_data if d.date == target_date]
    
    top_20 = evaluate_top_domains(target_domains)
    result = []
    for item in top_20:
        whois_text = mcp_client.get_whois(item.domain)
        result.append({
            "domain": item.domain, 
            "date": item.date,
            "reason": item.reason,
            "whois": whois_text
        })
        
    _LLM_CACHE[date_type] = {"data": result, "timestamp": time.time()}
    return result
