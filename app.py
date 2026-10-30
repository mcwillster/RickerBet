import sys
import asyncio
import json
import os
import random
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import uvicorn

# --- WINDOWS ASYNCIO FIX ---
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
# ---------------------------

playwright_instance = None
browser = None

# Backend State Management
IGNORED_TEAMS = {'team20', 'team21'}
service_down_history = {}
cached_odds_data = {"status": "Server starting up... Waiting for first data fetch.", "teams": []}

# --- PERSISTENT DATABASE SETUP ---
DB_FILE = "users_db.json"

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

users_db = load_db()

class BetRequest(BaseModel):
    team: str
    amount: float
    odds: float

class RaceBetRequest(BaseModel):
    car: str
    amount: float

async def scoreboard_sync_loop():
    global cached_odds_data, service_down_history
    while True:
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
                cached_odds_data = {"error": "Found 0 teams. React block?"}
                await asyncio.sleep(60)
                continue

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

            cached_odds_data = {
                "status": f"Live: Tracking {total_active_teams} teams (Auto-updating on server)",
                "teams": latest_teams_data
            }
            print("Background Sync Complete: Odds Updated.")
            
        except Exception as e:
            print(f"Background Sync Error: {str(e)}")
            
        await asyncio.sleep(60)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global playwright_instance, browser
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(headless=True)
    
    sync_task = asyncio.create_task(scoreboard_sync_loop())
    yield
    
    sync_task.cancel()
    await browser.close()
    await playwright_instance.stop()

app = FastAPI(lifespan=lifespan)

def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host

@app.get("/")
async def serve_frontend():
    if not os.path.exists("index.html"):
        return {"error": "index.html not found"}
    return FileResponse("index.html")

@app.get("/racing")
async def serve_racing():
    if not os.path.exists("racing.html"):
        return {"error": "racing.html not found"}
    return FileResponse("racing.html")

@app.get("/api/account")
async def get_account(request: Request):
    ip = get_client_ip(request)
    if ip not in users_db:
        users_db[ip] = {"balance": 1000.00, "bets": []}
        save_db(users_db)
    return {"ip": ip, "account": users_db[ip]}

@app.post("/api/bet")
async def place_bet(bet: BetRequest, request: Request):
    ip = get_client_ip(request)
    if ip not in users_db:
        users_db[ip] = {"balance": 1000.00, "bets": []}
        
    user = users_db[ip]
    if bet.amount <= 0 or bet.amount > user["balance"]:
        return {"error": "Invalid amount or insufficient funds."}
        
    user["balance"] -= bet.amount
    user["bets"].append({
        "team": bet.team,
        "amount": bet.amount,
        "odds": bet.odds,
        "potentialPayout": round(bet.amount * bet.odds, 2)
    })
    
    save_db(users_db)
    return {"success": True, "account": user}

@app.post("/api/race")
async def place_race_bet(bet: RaceBetRequest, request: Request):
    ip = get_client_ip(request)
    if ip not in users_db:
        users_db[ip] = {"balance": 1000.00, "bets": []}
        
    user = users_db[ip]
    cars = ["Red", "Blue", "Green", "Yellow"]
    
    if bet.amount <= 0 or bet.amount > user["balance"]:
        return {"error": "Invalid amount or insufficient funds."}
    if bet.car not in cars:
        return {"error": "Invalid car selection."}
        
    # Deduct funds immediately securely on the server
    user["balance"] -= bet.amount
    
    # Run the race on the backend
    winner = random.choice(cars)
    
    # 4 cars = 1/4 odds, payout is 4.0x
    payout = bet.amount * 4 if winner == bet.car else 0
    user["balance"] += payout
    
    user["bets"].append({
        "team": f"Race: {bet.car} (Winner: {winner})",
        "amount": bet.amount,
        "odds": 4.0,
        "potentialPayout": round(payout, 2) if payout > 0 else 0
    })
    
    save_db(users_db)
    return {"success": True, "winner": winner, "payout": payout, "account": user}

@app.get("/api/odds")
async def get_odds():
    return cached_odds_data

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=777, reload=False)