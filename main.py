import asyncio
import uuid
import random
import string
import json
from typing import Dict, List, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# === MODELS ===
class Move(BaseModel):
    token: str
    x: int
    y: int

class GameState(BaseModel):
    id: str
    grid: List[List[Dict[str, Any]]]
    current_player: str

# === STORAGE ===
games: Dict[str, GameState] = {}
subscribers: Dict[str, List[asyncio.Queue]] = {}
waiting_player: str | None = None
initial_moves_done: Dict[str, Dict[str, bool]] = {}
room_codes: Dict[str, str] = {}
player_tokens: Dict[str, dict] = {}

# === HELPERS ===
def new_empty_grid():
    return [[{} for _ in range(5)] for _ in range(5)]

def ensure_game_initialized(gid: str):
    if gid not in games:
        games[gid] = GameState(id=gid, grid=new_empty_grid(), current_player="RED")
    subscribers.setdefault(gid, [])
    initial_moves_done.setdefault(gid, {"RED": False, "BLUE": False})

def generate_room_code(length: int = 4) -> str:
    while True:
        code = ''.join(random.choices(string.ascii_uppercase, k=length))
        if code not in room_codes:
            return code

async def broadcast(gid: str, msg: dict):
    for q in list(subscribers.get(gid, [])):
        await q.put(msg)

def get_neighbors(x: int, y: int, w: int, h: int):
    res = []
    if x > 0: res.append((x - 1, y))
    if x < w - 1: res.append((x + 1, y))
    if y > 0: res.append((x, y - 1))
    if y < h - 1: res.append((x, y + 1))
    return res

# ==================================================================
# === MATCHMAKING SSE ===
# ==================================================================
@app.get("/matchmake/stream")
async def matchmake_stream(request: Request):
    global waiting_player  # ✅ use global instead of nonlocal
    q = asyncio.Queue()
    player_id = str(uuid.uuid4())[:8]
    print(f"[Server] Player {player_id} connected to matchmaking.")

    async def event_generator():
        global waiting_player  # ✅ correct usage here too
        try:
            if waiting_player:
                # Match found
                gid = str(uuid.uuid4())[:8]
                ensure_game_initialized(gid)

                red_token = str(uuid.uuid4())
                blue_token = str(uuid.uuid4())
                player_tokens[red_token] = {"game_id": gid, "role": "RED"}
                player_tokens[blue_token] = {"game_id": gid, "role": "BLUE"}

                # Send both players match events
                await waiting_player.put({
                    "type": "matched",
                    "game_id": gid,
                    "role": "RED",
                    "token": red_token
                })
                yield f"data: {json.dumps({'type': 'matched', 'game_id': gid, 'role': 'BLUE', 'token': blue_token})}\n\n"

                waiting_player = None
            else:
                waiting_player = q
                yield f"data: {json.dumps({'type': 'waiting', 'game_id': player_id, 'role': 'RED'})}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=10)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield "data: {}\n\n"
        finally:
            if waiting_player == q:
                waiting_player = None
                print(f"[Server] Player {player_id} left queue.")

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# === PRIVATE ROOMS ===
# ==================================================================
@app.post("/create_room")
async def create_room():
    gid = str(uuid.uuid4())[:8]
    ensure_game_initialized(gid)
    code = generate_room_code()
    room_codes[code] = gid

    red_token = str(uuid.uuid4())
    player_tokens[red_token] = {"game_id": gid, "role": "RED"}

    print(f"[Server] Created private room {gid} (code={code})")
    return {"game_id": gid, "room_code": code, "role": "RED", "token": red_token}


@app.post("/join_room/{code}")
async def join_room(code: str):
    code = code.upper()
    gid = room_codes.get(code)
    if not gid:
        raise HTTPException(status_code=404, detail="Room not found")

    ensure_game_initialized(gid)
    blue_token = str(uuid.uuid4())
    player_tokens[blue_token] = {"game_id": gid, "role": "BLUE"}
    print(f"[Server] Player joined {gid} (via {code})")
    return {"game_id": gid, "role": "BLUE", "token": blue_token}

# ==================================================================
# === GAME STREAM ===
# ==================================================================
@app.get("/stream/{gid}")
async def stream_game(gid: str, request: Request):
    ensure_game_initialized(gid)
    q = asyncio.Queue()
    subscribers[gid].append(q)
    print(f"[Server] Game stream opened: {gid} (total {len(subscribers[gid])})")

    async def event_generator():
        try:
            yield f"data: {json.dumps(games[gid].dict())}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=10)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield "data: {}\n\n"
        finally:
            subscribers[gid].remove(q)
            print(f"[Server] Game stream closed: {gid} ({len(subscribers[gid])} left)")

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# ==================================================================
# === MOVES ===
# ==================================================================
@app.post("/move")
async def move_endpoint(move: Move):
    if move.token not in player_tokens:
        raise HTTPException(status_code=401, detail="Invalid token")

    player_info = player_tokens[move.token]
    gid = player_info["game_id"]
    player = player_info["role"]

    ensure_game_initialized(gid)
    state = games[gid]
    grid = state.grid

    if move.y < 0 or move.y >= len(grid) or move.x < 0 or move.x >= len(grid[0]):
        return {"error": "Invalid coordinates"}

    cell = grid[move.y][move.x]
    owner = cell.get("owner")
    count = cell.get("count", 0)

    # Initial placement
    if not initial_moves_done[gid][player]:
        if not owner:
            grid[move.y][move.x] = {"owner": player, "count": 1}
            initial_moves_done[gid][player] = True
        else:
            return {"error": "Invalid initial move"}

        state.current_player = "BLUE" if player == "RED" else "RED"
        await broadcast(gid, state.dict())
        return {"ok": True}

    if owner != player:
        return {"error": "Not your cell"}

    grid[move.y][move.x] = {"owner": player, "count": count + 1}
    await process_reactions(gid, player)
    state.current_player = "BLUE" if player == "RED" else "RED"
    await broadcast(gid, state.dict())
    return {"ok": True}


# ==================================================================
# === REACTIONS ===
# ==================================================================
async def process_reactions(gid: str, player: str):
    state = games[gid]
    grid = state.grid
    w, h = len(grid[0]), len(grid)
    changed = True
    while changed:
        changed = False
        to_explode = []
        for y in range(h):
            for x in range(w):
                if grid[y][x].get("count", 0) >= 4:
                    to_explode.append((x, y, grid[y][x]["owner"]))
        if not to_explode:
            break
        for x, y, owner in to_explode:
            grid[y][x] = {}
            for nx, ny in get_neighbors(x, y, w, h):
                ncell = grid[ny][nx]
                grid[ny][nx] = {"owner": owner, "count": ncell.get("count", 0) + 1}
            changed = True
        await asyncio.sleep(0.05)
