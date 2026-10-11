#!/usr/bin/env python3
"""
NEON DRIFT 服务器
- 账号密码注册/登录（uid + password）
- 房间匹配 / 快速匹配 / 排位
- 游戏内状态转发
- 好友 / 邮件
- 管理员调试命令（GUI / WebSocket 均可调用）
- 玩家数据默认存放于 Android 私有目录 files/playerData.json
依赖: pip install websockets
"""
import asyncio
import hashlib
import json
import os
import secrets
import time
import uuid
import websockets

HOST = "0.0.0.0"
PORT = 9080


def _default_data_file() -> str:
    """Android → 私有 files 目录；桌面 → 脚本所在目录。"""
    # 1) Android（p4a 环境提供 android.storage）
    try:
        from android.storage import app_storage_path  # type: ignore
        base = app_storage_path()
        return os.path.join(base, "playerData.json")
    except Exception:
        pass
    # 2) Kivy 的通用私有目录 API
    try:
        from kivy.app import App
        app = App.get_running_app()
        if app is not None and app.user_data_dir:
            return os.path.join(app.user_data_dir, "playerData.json")
    except Exception:
        pass
    # 3) 桌面回退
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "playerData.json")


DATA_FILE = _default_data_file()


def set_data_file(path: str) -> None:
    """外部（例如 main.py）显式指定数据文件路径。"""
    global DATA_FILE
    DATA_FILE = path
    d = os.path.dirname(path)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


MATCH_MIN_PLAYERS = 2
MATCH_MAX_PLAYERS = 3
MATCH_TIMEOUT = 8.0
WORKER_INTERVAL = 0.5
PBKDF2_ROUNDS = 100000

clients = {}
users = {}
rooms = {}
matchmaking_queues = {"casual": [], "ranked": []}
match_join_times = {}

# GUI 日志回调
_log_cb = None


def log(msg):
    print(msg)
    if _log_cb:
        try:
            _log_cb(str(msg))
        except Exception:
            pass


def set_log_callback(cb):
    global _log_cb
    _log_cb = cb


# ==================================================
#  密码哈希
# ==================================================
def hash_password(password: str, salt_hex: str = "") -> tuple:
    if not salt_hex:
        salt_hex = secrets.token_hex(16)
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt, PBKDF2_ROUNDS)
    return dk.hex(), salt_hex


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    calc, _ = hash_password(password, salt_hex)
    return secrets.compare_digest(calc, hash_hex)


# ==================================================
#  持久化
# ==================================================
def load_data():
    global users
    d = os.path.dirname(DATA_FILE)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    if not os.path.exists(DATA_FILE):
        log(f"[DB] 首次启动，将在 {DATA_FILE} 创建数据库")
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        users = data.get("users", {})
        for uid, u in users.items():
            u.setdefault("ws", None)
            u.setdefault("room_id", None)
            u.setdefault("friends", [])
            u.setdefault("pending_in", [])
            u.setdefault("pending_out", [])
            u.setdefault("mails", [])
            u.setdefault("last_seen", 0)
            u.setdefault("password_hash", "")
            u.setdefault("password_salt", "")
            u.setdefault("registered", False)
        log(f"[DB] 已从 {DATA_FILE} 加载 {len(users)} 个账号")
    except Exception as e:
        log(f"[DB] 加载失败: {e}")


def save_data():
    try:
        d = os.path.dirname(DATA_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        clean = {}
        for uid, u in users.items():
            clean[uid] = {
                "id": uid,
                "name": u.get("name", ""),
                "password_hash": u.get("password_hash", ""),
                "password_salt": u.get("password_salt", ""),
                "registered": u.get("registered", False),
                "friends": list(u.get("friends", [])),
                "pending_in": list(u.get("pending_in", [])),
                "pending_out": list(u.get("pending_out", [])),
                "mails": u.get("mails", []),
                "last_seen": u.get("last_seen", 0),
            }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({"users": clean}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"[DB] 保存失败: {e}")


def ensure_user(uid, name):
    if uid not in users:
        users[uid] = {
            "id": uid, "name": name,
            "password_hash": "", "password_salt": "",
            "registered": False,
            "friends": [], "pending_in": [], "pending_out": [],
            "mails": [],
            "ws": None, "room_id": None,
            "last_seen": time.time(),
        }
        users[uid]["mails"].append({
            "id": uuid.uuid4().hex[:8], "from": "SYSTEM",
            "title": "欢迎来到 NEON DRIFT",
            "body": "感谢加入。新人礼包 500 金币已发放。",
            "ts": time.time(), "read": False, "claimed": False,
            "reward_gold": 500,
        })
    else:
        users[uid]["name"] = name
        users[uid].setdefault("ws", None)
        users[uid].setdefault("room_id", None)
        users[uid].setdefault("friends", [])
        users[uid].setdefault("pending_in", [])
        users[uid].setdefault("pending_out", [])
        users[uid].setdefault("mails", [])
        users[uid].setdefault("last_seen", 0)
        users[uid].setdefault("password_hash", "")
        users[uid].setdefault("password_salt", "")
        users[uid].setdefault("registered", False)
    return users[uid]


def is_online(uid):
    u = users.get(uid)
    return bool(u and u.get("ws") is not None)


def find_uid_by_name(name):
    nl = name.lower()
    for uid, u in users.items():
        if u.get("name", "").lower() == nl:
            return uid
    return None


# ==================================================
#  发送 / 视图
# ==================================================
async def send(ws, d):
    try:
        await ws.send(json.dumps(d, ensure_ascii=False))
    except Exception:
        pass


def friend_view(uid):
    u = users.get(uid, {})
    return {"id": uid, "name": u.get("name", "?"),
            "online": is_online(uid), "room_id": u.get("room_id")}


def mail_view(uid):
    return users.get(uid, {}).get("mails", [])


def room_public(r):
    return {
        "id": r["id"], "name": r["name"], "host": r["host"],
        "mode": r["mode"], "ai_config": r["ai_config"],
        "players": r["players"], "max_players": r["max_players"],
        "status": r["status"], "player_count": len(r["players"]),
        "ai_count": int(r["ai_config"].get("ai_count", 30)),
    }


async def push_friend_list(uid):
    u = users.get(uid)
    if not u: return
    ws = u.get("ws")
    if not ws: return
    await send(ws, {
        "type": "friend_list",
        "friends": [friend_view(fid) for fid in u.get("friends", [])],
        "pending_in": [friend_view(fid) for fid in u.get("pending_in", [])],
        "pending_out": [friend_view(fid) for fid in u.get("pending_out", [])],
    })


async def push_mail_list(uid):
    u = users.get(uid)
    if not u: return
    ws = u.get("ws")
    if not ws: return
    await send(ws, {"type": "mail_list", "mails": mail_view(uid)})


async def broadcast_room(rid):
    r = rooms.get(rid)
    if not r: return
    msg = json.dumps({"type": "room_update", "room": room_public(r)},
                     ensure_ascii=False)
    for p in r["players"]:
        u = users.get(p["id"])
        if u and u.get("ws"):
            try:
                await u["ws"].send(msg)
            except Exception:
                pass


async def broadcast_game(rid, msg_dict, exclude_id=None):
    r = rooms.get(rid)
    if not r: return
    msg = json.dumps(msg_dict, ensure_ascii=False)
    for p in r["players"]:
        if p["id"] == exclude_id: continue
        u = users.get(p["id"])
        if u and u.get("ws"):
            try:
                await u["ws"].send(msg)
            except Exception:
                pass


async def broadcast_queue(mode):
    q = matchmaking_queues.get(mode, [])
    msg = json.dumps({
        "type": "queue_status", "mode": mode,
        "count": len(q), "max": MATCH_MAX_PLAYERS,
    })
    for uid in q:
        u = users.get(uid)
        if u and u.get("ws"):
            try:
                await u["ws"].send(msg)
            except Exception:
                pass


# ==================================================
#  账号
# ==================================================
async def on_register(ws, msg):
    uid = str(msg.get("uid", "")).strip()
    password = str(msg.get("password", ""))
    name = str(msg.get("name", "Pilot")).strip()[:24]
    if len(uid) < 6:
        await send(ws, {"type": "auth_failed",
                        "reason": "UID 太短（至少 6 位）"})
        return
    if len(password) < 4:
        await send(ws, {"type": "auth_failed",
                        "reason": "密码至少 4 位"})
        return
    if not name:
        name = "Pilot"

    u = users.get(uid)
    if u and u.get("registered", False):
        await send(ws, {"type": "auth_failed",
                        "reason": "该 UID 已注册，请用登录"})
        return

    h, s = hash_password(password)
    ensure_user(uid, name)
    users[uid]["password_hash"] = h
    users[uid]["password_salt"] = s
    users[uid]["registered"] = True
    users[uid]["name"] = name
    users[uid]["ws"] = ws
    clients[ws] = uid
    save_data()

    log(f"[AUTH] register uid={uid} name={name}")
    await send(ws, {"type": "auth_ok", "uid": uid, "name": name,
                    "is_new": True})
    await push_friend_list(uid)
    await push_mail_list(uid)


async def on_login(ws, msg):
    uid = str(msg.get("uid", "")).strip()
    password = str(msg.get("password", ""))
    if not uid or not password:
        await send(ws, {"type": "auth_failed", "reason": "缺少 UID 或密码"})
        return
    u = users.get(uid)
    if not u or not u.get("registered", False):
        await send(ws, {"type": "auth_failed",
                        "reason": "该 UID 未注册"})
        return
    if u.get("ws") is not None:
        await send(ws, {"type": "auth_failed",
                        "reason": "该账号已在别处登录"})
        return
    if not verify_password(password, u["password_hash"], u["password_salt"]):
        await send(ws, {"type": "auth_failed", "reason": "密码错误"})
        return

    u["ws"] = ws
    u["last_seen"] = time.time()
    clients[ws] = uid
    log(f"[AUTH] login uid={uid} name={u.get('name','?')}")
    await send(ws, {"type": "auth_ok", "uid": uid,
                    "name": u.get("name", "Pilot"), "is_new": False})
    await push_friend_list(uid)
    await push_mail_list(uid)
    for fid in u.get("friends", []):
        if is_online(fid):
            await push_friend_list(fid)


async def on_change_password(ws, msg):
    uid = clients.get(ws)
    if not uid: return
    old_pw = str(msg.get("old_password", ""))
    new_pw = str(msg.get("new_password", ""))
    u = users.get(uid)
    if not u: return
    if not verify_password(old_pw, u.get("password_hash", ""),
                            u.get("password_salt", "")):
        await send(ws, {"type": "error", "msg": "原密码错误"})
        return
    if len(new_pw) < 4:
        await send(ws, {"type": "error", "msg": "新密码至少 4 位"})
        return
    h, s = hash_password(new_pw)
    u["password_hash"] = h
    u["password_salt"] = s
    save_data()
    await send(ws, {"type": "error", "msg": "密码已修改"})


# ==================================================
#  匹配
# ==================================================
async def on_queue_join(uid, mode):
    u = users.get(uid)
    if not u: return
    if u.get("room_id"):
        await send(u["ws"], {"type": "error", "msg": "你已在房间中"})
        return
    if mode not in matchmaking_queues:
        await send(u["ws"], {"type": "error", "msg": "未知队列"})
        return
    for m in matchmaking_queues:
        if uid in matchmaking_queues[m]:
            matchmaking_queues[m].remove(uid)
            await broadcast_queue(m)
    if uid not in matchmaking_queues[mode]:
        matchmaking_queues[mode].append(uid)
        match_join_times[uid] = time.time()
    await send(u["ws"], {
        "type": "queue_joined", "mode": mode,
        "count": len(matchmaking_queues[mode]),
        "max": MATCH_MAX_PLAYERS,
    })
    await broadcast_queue(mode)


async def on_queue_leave(uid):
    for m in matchmaking_queues:
        if uid in matchmaking_queues[m]:
            matchmaking_queues[m].remove(uid)
            await broadcast_queue(m)
    match_join_times.pop(uid, None)
    u = users.get(uid)
    if u and u.get("ws"):
        await send(u["ws"], {"type": "queue_left"})


async def start_match_room(mode, uids):
    rid = uuid.uuid4().hex[:8]
    players = []
    for uid in uids:
        u = users.get(uid)
        if not u: continue
        players.append({"id": uid, "name": u.get("name", "?"), "ready": True})
    if not players: return
    host_id = players[0]["id"]
    r = {
        "id": rid,
        "name": ("排位 " if mode == "ranked" else "匹配 ") + rid,
        "host": host_id,
        "mode": "matchmaking",
        "ai_config": {
            "ai_count": 30,
            "difficulty": "hard" if mode == "ranked" else "normal",
            "ring_speed": "normal",
            "team_size": len(players),
            "pure_pvp": True,
            "host_id": host_id,
        },
        "players": players,
        "max_players": MATCH_MAX_PLAYERS,
        "status": "playing",
    }
    rooms[rid] = r
    for p in players:
        u = users.get(p["id"])
        if u: u["room_id"] = rid
    config = {
        "mode": "matchmaking",
        "ai_count": 30,
        "difficulty": "hard" if mode == "ranked" else "normal",
        "ring_speed": "normal",
        "team_size": len(players),
        "team": players,
        "ranked": mode == "ranked",
        "pure_pvp": True,
        "host_id": host_id,
    }
    msg = json.dumps({"type": "game_start", "room_id": rid, "config": config},
                     ensure_ascii=False)
    for p in players:
        u = users.get(p["id"])
        if u and u.get("ws"):
            try:
                await u["ws"].send(msg)
            except Exception:
                pass
    log(f"[MATCH] room={rid} mode={mode} host={host_id} players={len(players)}")


async def matchmaking_worker(mode):
    while True:
        try:
            await asyncio.sleep(WORKER_INTERVAL)
            q = matchmaking_queues.get(mode, [])
            if not q: continue
            now = time.time()
            should_start = False
            if len(q) >= MATCH_MAX_PLAYERS:
                should_start = True
            else:
                first = q[0]
                waited = now - match_join_times.get(first, now)
                if waited >= MATCH_TIMEOUT and len(q) >= MATCH_MIN_PLAYERS:
                    should_start = True
                elif waited >= MATCH_TIMEOUT * 2:
                    should_start = True
            if should_start:
                n = min(len(q), MATCH_MAX_PLAYERS)
                players = q[:n]
                matchmaking_queues[mode] = q[n:]
                for uid in players:
                    match_join_times.pop(uid, None)
                await start_match_room(mode, players)
                await broadcast_queue(mode)
        except Exception as e:
            log(f"match worker error: {e}")


# ==================================================
#  游戏内转发
# ==================================================
async def forward_game_msg(uid, msg):
    u = users.get(uid)
    if not u:
        return
    rid = u.get("room_id")
    if not rid or rid not in rooms:
        if u.get("ws"):
            try:
                await u["ws"].send(json.dumps({"type": "not_in_room"}))
            except Exception:
                pass
        return
    r = rooms[rid]
    if r["status"] != "playing":
        if u.get("ws"):
            try:
                await u["ws"].send(json.dumps({"type": "not_in_room"}))
            except Exception:
                pass
        return

    t = msg.get("type", "")

    if t == "sync":
        await broadcast_game(rid, {
            "type": "peer_state",
            "id": uid,
            "pos": msg.get("pos", [0, 0, 0]),
            "yaw": msg.get("yaw", 0.0),
            "pitch": msg.get("pitch", 0.0),
            "hp": msg.get("hp", 100),
            "firing": bool(msg.get("firing", False)),
            "weapon": msg.get("weapon", ""),
        }, exclude_id=uid)

    elif t == "ai_sync":
        if uid != r["host"]: return
        await broadcast_game(rid, {
            "type": "ai_sync", "states": msg.get("states", []),
        }, exclude_id=uid)

    elif t == "hit":
        target = str(msg.get("target", ""))
        dmg = float(msg.get("dmg", 0))
        head = bool(msg.get("head", False))
        if target == "": return
        await broadcast_game(rid, {
            "type": "peer_hit",
            "from": uid, "target": target,
            "dmg": dmg, "head": head,
        })
        target_u = users.get(target)
        if target_u and target_u.get("room_id") == rid:
            if target_u.get("ws"):
                try:
                    await target_u["ws"].send(json.dumps({
                        "type": "took_damage",
                        "from": uid, "dmg": dmg, "head": head,
                    }))
                except Exception:
                    pass
        elif target.startswith("ai_") and uid != r["host"]:
            host_u = users.get(r["host"])
            if host_u and host_u.get("ws"):
                try:
                    await host_u["ws"].send(json.dumps({
                        "type": "remote_ai_hit",
                        "from": uid, "target": target,
                        "dmg": dmg, "head": head,
                    }))
                except Exception:
                    pass

    elif t == "kill":
        victim = str(msg.get("victim", ""))
        await broadcast_game(rid, {
            "type": "kill", "killer": uid, "victim": victim,
        })

    elif t == "ai_killed":
        if uid != r["host"]: return
        await broadcast_game(rid, {
            "type": "ai_killed",
            "ai_id": str(msg.get("ai_id", "")),
            "killer": str(msg.get("killer", "")),
        }, exclude_id=uid)


# ==================================================
#  好友 / 邮件
# ==================================================
async def on_friend_request(uid, target_name):
    target_uid = find_uid_by_name(target_name)
    if not target_uid:
        await send(users[uid]["ws"], {"type": "error", "msg": "找不到该玩家"})
        return
    if target_uid == uid:
        await send(users[uid]["ws"], {"type": "error", "msg": "不能加自己"})
        return
    me = users[uid]; you = users[target_uid]
    if target_uid in me.get("friends", []):
        await send(me["ws"], {"type": "error", "msg": "已经是好友"})
        return
    if target_uid in me.get("pending_out", []):
        await send(me["ws"], {"type": "error", "msg": "已发送过请求"})
        return
    if target_uid in me.get("pending_in", []):
        me["pending_in"].remove(target_uid)
        you["pending_out"].remove(uid)
        me.setdefault("friends", []).append(target_uid)
        you.setdefault("friends", []).append(uid)
        save_data()
        await push_friend_list(uid)
        await push_friend_list(target_uid)
        return
    me.setdefault("pending_out", []).append(target_uid)
    you.setdefault("pending_in", []).append(uid)
    save_data()
    await push_friend_list(uid)
    if you.get("ws"):
        await send(you["ws"], {
            "type": "friend_request",
            "from_id": uid, "from_name": me.get("name", "?"),
        })
        await push_friend_list(target_uid)


async def on_friend_accept(uid, from_id):
    me = users[uid]
    if from_id not in me.get("pending_in", []): return
    you = users.get(from_id)
    if not you: return
    me["pending_in"].remove(from_id)
    you.get("pending_out", []).remove(uid)
    me.setdefault("friends", []).append(from_id)
    you.setdefault("friends", []).append(uid)
    save_data()
    await push_friend_list(uid)
    await push_friend_list(from_id)


async def on_friend_reject(uid, from_id):
    me = users[uid]
    if from_id in me.get("pending_in", []):
        me["pending_in"].remove(from_id)
    you = users.get(from_id)
    if you: you.get("pending_out", []).remove(uid)
    save_data()
    await push_friend_list(uid)
    if you: await push_friend_list(from_id)


async def on_friend_remove(uid, friend_id):
    me = users[uid]
    if friend_id in me.get("friends", []):
        me["friends"].remove(friend_id)
    you = users.get(friend_id)
    if you and uid in you.get("friends", []):
        you["friends"].remove(uid)
    save_data()
    await push_friend_list(uid)
    if you: await push_friend_list(friend_id)


async def on_invite_to_room(uid, friend_id):
    me = users.get(uid)
    rid = me.get("room_id")
    if not rid or rid not in rooms:
        await send(me["ws"], {"type": "error", "msg": "你不在房间里"})
        return
    if friend_id not in me.get("friends", []):
        await send(me["ws"], {"type": "error", "msg": "不是好友"})
        return
    you = users.get(friend_id)
    if not you or not you.get("ws"):
        await send(me["ws"], {"type": "error", "msg": "对方不在线"})
        return
    r = rooms[rid]
    await send(you["ws"], {
        "type": "room_invite",
        "from_name": me.get("name", "?"),
        "room_id": rid, "room_name": r["name"],
    })


async def on_mail_read(uid, mail_id):
    u = users.get(uid)
    if not u: return
    for m in u.get("mails", []):
        if m["id"] == mail_id:
            m["read"] = True
            break
    save_data()
    await push_mail_list(uid)


async def on_mail_claim(uid, mail_id):
    u = users.get(uid)
    if not u: return
    for m in u.get("mails", []):
        if m["id"] == mail_id and not m.get("claimed", False):
            m["claimed"] = True
            m["read"] = True
            break
    save_data()
    await push_mail_list(uid)


# ==================================================
#  房间
# ==================================================
async def on_create_room(uid, msg):
    u = users[uid]
    old_rid = u.get("room_id")
    if old_rid and old_rid not in rooms:
        u["room_id"] = None
        old_rid = None
    if old_rid:
        await send(u["ws"], {"type": "error", "msg": "已在房间中"})
        return
    rid = uuid.uuid4().hex[:8]
    r = {
        "id": rid,
        "name": str(msg.get("name", "未命名房间"))[:32],
        "host": uid,
        "mode": str(msg.get("mode", "ai_match")),
        "ai_config": msg.get("ai_config", {
            "ai_count": 30, "difficulty": "normal",
            "ring_speed": "normal", "team_size": 3,
            "host_id": uid,
        }),
        "players": [{"id": uid, "name": u["name"], "ready": True}],
        "max_players": 3, "status": "waiting",
    }
    if "host_id" not in r["ai_config"]:
        r["ai_config"]["host_id"] = uid
    rooms[rid] = r
    u["room_id"] = rid
    await send(u["ws"], {"type": "room_created", "room_id": rid,
                         "room": room_public(r)})
    await broadcast_room(rid)


async def on_join_room(uid, rid):
    u = users[uid]
    old_rid = u.get("room_id")
    if old_rid and old_rid not in rooms:
        u["room_id"] = None
        old_rid = None
    if old_rid:
        await send(u["ws"], {"type": "error", "msg": "已在房间中"})
        return
    if rid not in rooms:
        await send(u["ws"], {"type": "error", "msg": "房间不存在"})
        return
    r = rooms[rid]
    if r["status"] != "waiting":
        await send(u["ws"], {"type": "error", "msg": "房间已开局"})
        return
    if len(r["players"]) >= r["max_players"]:
        await send(u["ws"], {"type": "error", "msg": "房间已满"})
        return
    r["players"].append({"id": uid, "name": u["name"], "ready": False})
    u["room_id"] = rid
    await send(u["ws"], {"type": "room_joined", "room": room_public(r)})
    await broadcast_room(rid)


async def do_leave_room(uid):
    u = users.get(uid)
    if not u: return
    rid = u.get("room_id")
    u["room_id"] = None
    if u.get("ws"):
        await send(u["ws"], {"type": "room_left"})
    if not rid or rid not in rooms: return
    r = rooms[rid]
    r["players"] = [p for p in r["players"] if p["id"] != uid]
    if not r["players"]:
        del rooms[rid]
    else:
        if r["host"] == uid:
            r["host"] = r["players"][0]["id"]
            await broadcast_game(rid, {"type": "host_changed",
                                        "new_host": r["host"]})
        await broadcast_game(rid, {"type": "peer_left", "id": uid})
        await broadcast_room(rid)


async def on_set_ready(uid, ready):
    u = users.get(uid)
    rid = u.get("room_id") if u else None
    if not rid or rid not in rooms: return
    r = rooms[rid]
    for p in r["players"]:
        if p["id"] == uid:
            p["ready"] = bool(ready)
    await broadcast_room(rid)


async def on_start_game(uid):
    u = users[uid]
    rid = u.get("room_id")
    if not rid or rid not in rooms: return
    r = rooms[rid]
    if r["host"] != uid:
        await send(u["ws"], {"type": "error", "msg": "只有房主能开始"})
        return
    r["status"] = "playing"
    config = {
        "mode": r["mode"],
        "ai_count": int(r["ai_config"].get("ai_count", 30)),
        "difficulty": str(r["ai_config"].get("difficulty", "normal")),
        "ring_speed": str(r["ai_config"].get("ring_speed", "normal")),
        "team_size": len(r["players"]),
        "team": r["players"],
        "host_id": r["host"],
    }
    msg = json.dumps({"type": "game_start", "room_id": rid, "config": config},
                     ensure_ascii=False)
    for p in r["players"]:
        pu = users.get(p["id"])
        if pu and pu.get("ws"):
            try:
                await pu["ws"].send(msg)
            except Exception:
                pass


# ==================================================
#  管理员调试命令
# ==================================================
ADMIN_HELP = """可用命令：
  help                                显示帮助
  list                                列出所有在线玩家
  whois <uid>                         查看玩家详情
  kick <uid>                          踢下线
  hp <uid> <0-100>                    设置 HP
  heal <uid>                          回满 HP
  god <uid> on|off                    无敌开关
  speed <uid> <mult>                  移动速度倍率（0.1 - 10）
  tp <uid> <x> <y> <z>                传送
  give <uid> <item> [qty]             给物品
  kill <uid>                          击杀
  spawnai <uid> <n> [hp] [spd]        生成 N 个 AI
  killall <uid>                       清空该玩家 AI
  broadcast <msg>                     全服公告
  name <uid> <new>                    改名
  rooms                               列出所有房间
  stop                                关闭服务器
"""


async def _push_admin(uid, payload):
    u = users.get(uid)
    if not u or not u.get("ws"):
        return False
    await send(u["ws"], payload)
    return True


async def admin_execute(cmd: str, from_uid: str = "gui") -> str:
    parts = cmd.strip().split()
    if not parts:
        return "空命令"
    op, args = parts[0].lower(), parts[1:]

    if op == "help":
        return ADMIN_HELP

    if op == "list":
        online = [(uid, u) for uid, u in users.items()
                  if u.get("ws") is not None]
        if not online:
            return "无在线玩家"
        out = [f"在线 {len(online)} 人:"]
        for uid, u in online:
            out.append(f"  {uid}({u.get('name','?')}) "
                       f"room={u.get('room_id') or '-'}")
        return "\n".join(out)

    if op == "rooms":
        if not rooms:
            return "无房间"
        return "\n".join(
            f"{r['id']} [{r['status']}] {r['name']} "
            f"host={r['host']} players={len(r['players'])}"
            for r in rooms.values()
        )

    if op == "whois":
        if not args: return "用法: whois <uid>"
        uid = args[0]
        u = users.get(uid)
        if not u: return "该 UID 不存在"
        return (f"{uid}\n  name={u.get('name')}\n"
                f"  online={is_online(uid)}\n"
                f"  room={u.get('room_id')}\n"
                f"  registered={u.get('registered')}\n"
                f"  last_seen={u.get('last_seen')}")

    if op == "broadcast":
        msg = " ".join(args)
        n = 0
        for u in users.values():
            if u.get("ws"):
                try:
                    await u["ws"].send(json.dumps({
                        "type": "admin_cmd",
                        "cmd": "broadcast", "msg": msg,
                    }, ensure_ascii=False))
                    n += 1
                except Exception:
                    pass
        return f"已广播给 {n} 人: {msg}"

    if op == "stop":
        loop = asyncio.get_event_loop()
        loop.call_later(0.3, loop.stop)
        return "服务器正在关闭..."

    target = args[0] if args else ""
    if not target:
        return f"{op}: 需要 <uid> 参数"

    if op == "kick":
        u = users.get(target)
        if not u or not u.get("ws"):
            return "目标不在线"
        try:
            await u["ws"].close()
        except Exception:
            pass
        return f"已踢下线 {target}"

    if op == "hp":
        if len(args) < 2: return "用法: hp <uid> <0-100>"
        try:
            v = max(0.0, min(100.0, float(args[1])))
        except ValueError:
            return "数值无效"
        ok = await _push_admin(target, {
            "type": "admin_cmd", "cmd": "set_hp", "hp": v})
        return f"set_hp={v} → {target}" if ok else "目标不在线"

    if op == "heal":
        ok = await _push_admin(target, {
            "type": "admin_cmd", "cmd": "set_hp", "hp": 100.0})
        return f"已回满 {target}" if ok else "目标不在线"

    if op == "god":
        on = (len(args) > 1 and args[1].lower() in ("on", "1", "true"))
        ok = await _push_admin(target, {
            "type": "admin_cmd", "cmd": "god", "on": on})
        return f"godmode={on} → {target}" if ok else "目标不在线"

    if op == "speed":
        if len(args) < 2: return "用法: speed <uid> <mult>"
        try:
            m = max(0.1, min(10.0, float(args[1])))
        except ValueError:
            return "数值无效"
        ok = await _push_admin(target, {
            "type": "admin_cmd", "cmd": "speed", "mult": m})
        return f"speed={m} → {target}" if ok else "目标不在线"

    if op == "tp":
        if len(args) < 4: return "用法: tp <uid> <x> <y> <z>"
        try:
            x, y, z = float(args[1]), float(args[2]), float(args[3])
        except ValueError:
            return "坐标无效"
        ok = await _push_admin(target, {
            "type": "admin_cmd", "cmd": "tp", "pos": [x, y, z]})
        return f"tp→({x},{y},{z}) {target}" if ok else "目标不在线"

    if op == "give":
        if len(args) < 2: return "用法: give <uid> <item> [qty]"
        item = args[1]
        try:
            qty = int(args[2]) if len(args) > 2 else 1
        except ValueError:
            qty = 1
        ok = await _push_admin(target, {
            "type": "admin_cmd", "cmd": "give",
            "item": item, "qty": qty})
        return f"give {item} x{qty} → {target}" if ok else "目标不在线"

    if op == "kill":
        ok = await _push_admin(target, {
            "type": "admin_cmd", "cmd": "kill"})
        return f"kill → {target}" if ok else "目标不在线"

    if op == "spawnai":
        if len(args) < 2: return "用法: spawnai <uid> <n> [hp] [spd]"
        try:
            n = int(args[1])
            hp = float(args[2]) if len(args) > 2 else 3.0
            spd = float(args[3]) if len(args) > 3 else 4.2
        except ValueError:
            return "参数无效"
        ok = await _push_admin(target, {
            "type": "admin_cmd", "cmd": "spawn_ai",
            "count": n, "hp": hp, "speed": spd})
        return f"spawn_ai x{n} → {target}" if ok else "目标不在线"

    if op == "killall":
        ok = await _push_admin(target, {
            "type": "admin_cmd", "cmd": "kill_all_ai"})
        return f"kill_all_ai → {target}" if ok else "目标不在线"

    if op == "name":
        if len(args) < 2: return "用法: name <uid> <new>"
        new_name = " ".join(args[1:])[:24]
        u = users.get(target)
        if not u: return "该 UID 不存在"
        u["name"] = new_name
        save_data()
        return f"{target} 改名为 {new_name}"

    return f"未知命令: {op}  （输入 help 查看）"


async def on_admin_debug(ws, msg):
    """客户端通过 WebSocket 发送的 admin_debug 请求。"""
    uid = str(msg.get("uid", ""))
    pw = str(msg.get("password", ""))
    cmd = str(msg.get("cmd", ""))
    req_id = str(msg.get("req_id", ""))

    u = users.get(uid)
    if not u or not u.get("registered"):
        await send(ws, {"type": "admin_result", "req_id": req_id,
                        "ok": False, "msg": "账号不存在"})
        return
    if not verify_password(pw, u.get("password_hash", ""),
                            u.get("password_salt", "")):
        await send(ws, {"type": "admin_result", "req_id": req_id,
                        "ok": False, "msg": "密码错误"})
        return
    try:
        res = await admin_execute(cmd, from_uid=uid)
        await send(ws, {"type": "admin_result", "req_id": req_id,
                        "ok": True, "msg": res})
        log(f"[ADMIN:{uid}] {cmd} → {res}")
    except Exception as e:
        await send(ws, {"type": "admin_result", "req_id": req_id,
                        "ok": False, "msg": f"执行失败: {e}"})


# ==================================================
#  消息分发
# ==================================================
async def handle(ws, msg):
    t = msg.get("type", "")

    # 未登录：允许 register / login / admin_debug
    if ws not in clients:
        if t == "register":
            await on_register(ws, msg)
        elif t == "login":
            await on_login(ws, msg)
        elif t == "admin_debug":
            await on_admin_debug(ws, msg)
        else:
            await send(ws, {"type": "error", "msg": "请先登录"})
        return

    uid = clients[ws]

    if t in ("sync", "ai_sync", "hit", "kill", "ai_killed"):
        await forward_game_msg(uid, msg)
        return

    if t == "set_name":
        name = str(msg.get("name", ""))[:24]
        if name:
            users[uid]["name"] = name
            save_data()
            for fid in users[uid].get("friends", []):
                if is_online(fid):
                    await push_friend_list(fid)

    elif t == "change_password":
        await on_change_password(ws, msg)

    elif t == "list_rooms":
        arr = [room_public(r) for r in rooms.values()
               if r["status"] == "waiting"]
        await send(ws, {"type": "rooms", "rooms": arr})

    elif t == "create_room":
        await on_create_room(uid, msg)
    elif t == "join_room":
        await on_join_room(uid, str(msg.get("room_id", "")))
    elif t == "leave_room":
        await do_leave_room(uid)
    elif t == "set_ready":
        await on_set_ready(uid, bool(msg.get("ready", False)))
    elif t == "start_game":
        await on_start_game(uid)

    elif t == "queue_join":
        await on_queue_join(uid, str(msg.get("mode", "casual")))
    elif t == "queue_leave":
        await on_queue_leave(uid)

    elif t == "friend_request":
        await on_friend_request(uid, str(msg.get("name", "")))
    elif t == "friend_accept":
        await on_friend_accept(uid, str(msg.get("from_id", "")))
    elif t == "friend_reject":
        await on_friend_reject(uid, str(msg.get("from_id", "")))
    elif t == "friend_remove":
        await on_friend_remove(uid, str(msg.get("friend_id", "")))
    elif t == "friend_list":
        await push_friend_list(uid)
    elif t == "invite_to_room":
        await on_invite_to_room(uid, str(msg.get("friend_id", "")))

    elif t == "mail_list":
        await push_mail_list(uid)
    elif t == "mail_read":
        await on_mail_read(uid, str(msg.get("mail_id", "")))
    elif t == "mail_claim":
        await on_mail_claim(uid, str(msg.get("mail_id", "")))

    elif t == "admin_debug":
        await on_admin_debug(ws, msg)


async def handler(ws):
    uid = None
    try:
        log(f"[WS] new connection from {ws.remote_address}")
        async for raw in ws:
            try:
                msg = json.loads(raw)
                if isinstance(msg, dict):
                    await handle(ws, msg)
            except json.JSONDecodeError:
                pass
    except websockets.ConnectionClosed:
        pass
    except Exception as e:
        log(f"handler error: {e}")
    finally:
        uid = clients.pop(ws, None)
        if uid:
            users[uid]["ws"] = None
            users[uid]["last_seen"] = time.time()
            for m in matchmaking_queues:
                if uid in matchmaking_queues[m]:
                    matchmaking_queues[m].remove(uid)
                    await broadcast_queue(m)
            match_join_times.pop(uid, None)
            await do_leave_room(uid)
            for fid in users[uid].get("friends", []):
                if is_online(fid):
                    await push_friend_list(fid)
            save_data()
            log(f"[-] {uid} offline. total={len(clients)}")


# ==================================================
#  启动入口
# ==================================================
async def run_server(host=None, port=None, on_log=None, on_ready=None):
    """可复用启动入口（供 Kivy / 直接运行共用）。"""
    global HOST, PORT
    if host is not None: HOST = host
    if port is not None: PORT = port
    if on_log is not None:
        set_log_callback(on_log)

    load_data()
    log(f"NEON DRIFT server on {HOST}:{PORT}")
    log(f"data file: {DATA_FILE}")

    asyncio.create_task(matchmaking_worker("casual"))
    asyncio.create_task(matchmaking_worker("ranked"))

    async with websockets.serve(handler, HOST, PORT, max_size=2**20):
        if on_ready:
            try:
                on_ready()
            except Exception:
                pass
        await asyncio.Future()


async def main():
    await run_server(HOST, PORT)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nshutdown")