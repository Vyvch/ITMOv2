from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import datetime
import time
import os

from services import get_domains, evaluate_top_domains
from mcp_client import mcp_client
from models import DomainItem

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

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})

@app.get("/api/domains")
def api_get_domains() -> list[dict]:
    # Returns raw list for the frontend
    domains = get_domains()
    return [d.dict() for d in domains]

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
