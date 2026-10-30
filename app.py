import sys
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import uvicorn
import os

# --- WINDOWS ASYNCIO FIX ---
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
# ---------------------------

playwright_instance = None
browser = None

# Backend State Management
IGNORED_TEAMS = {'team20', 'team21'}
service_down_history = {}

# In-memory database for users based on IP address
# Format: {"192.168.1.50": {"balance": 1000.0, "bets": []}}
users_db = {}

class BetRequest(BaseModel):
    team: str
    amount: float
    odds: float

@asynccontextmanager
async def lifespan(app: FastAPI):
    global playwright_instance, browser
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(headless=True)
    yield
    await browser.close()
    await playwright_instance.stop()

app = FastAPI(lifespan=lifespan)

def get_client_ip(request: Request) -> str:
    # Check headers first in case the server is behind a VPN/Proxy
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host

@app.get("/")
async def serve_frontend():
    if not os.path.exists("index.html"):
        return {"error": "index.html not found in the same directory"}
    return FileResponse("index.html")

@app.get("/api/account")
async def get_account(request: Request):
    ip = get_client_ip(request)
    # Initialize a new account with $1000 if the IP is new
    if ip not in users_db:
        users_db[ip] = {"balance": 1000.00, "bets": []}
    
    return {"ip": ip, "account": users_db[ip]}

@app.post("/api/bet")
async def place_bet(bet: BetRequest, request: Request):
    ip = get_client_ip(request)
    
    if ip not in users_db:
        users_db[ip] = {"balance": 1000.00, "bets": []}
        
    user = users_db[ip]
    
    if bet.amount <= 0 or bet.amount > user["balance"]:
        return {"error": "Invalid amount or insufficient funds."}
        
    # Deduct balance and record bet
    user["balance"] -= bet.amount
    user["bets"].append({
        "team": bet.team,
        "amount": bet.amount,
        "odds": bet.odds,
        "potentialPayout": round(bet.amount * bet.odds, 2)
    })
    
    return {"success": True, "account": user}

@app.get("/api/odds")
async def get_odds():
    global service_down_history
    try:
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()
        
        await page.goto("https://scoring.irsec.club/scoreboard")
        await page.wait_for_selector("ol > li", timeout=15000)
        
        html = await page.content()
        await context.close()
        
        soup = BeautifulSoup(html, 'html.parser')
        team_rows = soup.select('ol > li')
        
        if not team_rows:
            return {"error": "Found 0 teams. React block?"}

        parsed_teams = []
        max_score = 0

        for row in team_rows:
            name_el = row.select_one('.truncate.font-bold.uppercase')
            pts_el = row.select_one('.text-right.font-bold.tabular-nums')
            
            if name_el and pts_el:
                name = name_el.get_text(strip=True)
                if name.lower() in IGNORED_TEAMS:
                    continue

                pts_text = pts_el.get_text(strip=True).replace(',', '')
                pts = int(pts_text) if pts_text.isdigit() else 0
                if pts > max_score:
                    max_score = pts

                if name not in service_down_history:
                    service_down_history[name] = {}

                service_divs = row.select('div[title]')
                up_tiles = 0
                accumulated_down_penalty = 0

                for div in service_divs:
                    title_raw = div.get('title', '')
                    if '—' in title_raw:
                        svc_string = title_raw.split('—')[1].strip()
                        if ':' in svc_string:
                            parts = svc_string.split(':')
                            svc_name = parts[0].strip()
                            svc_status = parts[1].strip() if len(parts) > 1 else 'Unknown'

                            if svc_status == 'Up':
                                up_tiles += 1
                                service_down_history[name][svc_name] = 0
                            else:
                                service_down_history[name][svc_name] = service_down_history[name].get(svc_name, 0) + 1
                                accumulated_down_penalty += service_down_history[name][svc_name]

                parsed_teams.append({
                    'name': name,
                    'pts': pts,
                    'upTiles': up_tiles,
                    'downPenalty': accumulated_down_penalty
                })

        total_active_teams = len(parsed_teams)
        latest_teams_data = []
        
        for team in parsed_teams:
            point_deficit = max(0, max_score - team['pts'])
            odds = 1.05
            odds += (point_deficit * 0.012)
            odds += ((11 - team['upTiles']) * 0.4)
            odds += (team['downPenalty'] * 0.25)
            odds = odds * (1 + (total_active_teams * 0.015))
            odds = max(1.01, min(odds, 999.99))
            
            team['odds'] = f"{odds:.2f}"
            latest_teams_data.append(team)

        return {
            "status": f"Live: Tracking {total_active_teams} teams",
            "teams": latest_teams_data
        }
    except Exception as e:
        print(f"Playwright/Parser Error: {str(e)}")
        return {"error": str(e)}

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)