#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NEON DRIFT 服务器 - TUI 控制台
用法:
    python server_tui.py [port]

依赖:
    pip install websockets

前置:
    server.py 需与本文件同目录。
"""
import asyncio
import os
import sys
import threading
import time

# ---- 让 import server 能找到同目录的 server.py ----
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import server as gs  # noqa: E402


# ==================================================
#  TUI 服务器包装
# ==================================================
class TuiServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 9080):
        self.host = host
        self.port = port
        self.loop: asyncio.AbstractEventLoop = None
        self.thread: threading.Thread = None
        self._ready = threading.Event()

    # ---------- 日志 ----------
    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        # 用 \r 清掉当前输入行再打印，避免命令打到一半被日志打断
        sys.stdout.write(f"\r[{ts}] {msg}\n")
        sys.stdout.flush()

    # ---------- 后台 loop ----------
    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(
                gs.run_server(self.host, self.port,
                              on_log=self._log,
                              on_ready=self._ready.set)
            )
        except Exception as e:
            self._log(f"[严重] 服务器退出: {e}")
        finally:
            try:
                self.loop.close()
            except Exception:
                pass

    def start(self) -> bool:
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        return self._ready.wait(timeout=3.0)

    def stop(self):
        if self.loop and self.loop.is_running():
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except Exception:
                pass

    # ---------- 提交协程到服务器 loop ----------
    def submit(self, coro, timeout: float = 8.0):
        if not self.loop or not self.loop.is_running():
            print("服务器未启动")
            return
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout=timeout)
        except Exception as e:
            print(f"执行异常: {e}")
            return None


# ==================================================
#  帮助
# ==================================================
BANNER = r"""
==========================================================
   _   _ _____ ___  _   _   ___  ___  ___ _____ _____
  | \ | | ____/ _ \| \ | | |   \| _ \/ _ \_   _|  ___|
  |  \| |  _|| | | |  \| | | |) |   / (_) || | | |_
  | |\  | |__| |_| | |\  | | |\ \ |_\ \__, || | |  _|
  |_| \_|_____\___/|_| \_| |_| \_\___/  /_/ |_| |_|

                    服务器 TUI 控制台
==========================================================
"""


def print_help():
    print(gs.ADMIN_HELP)
    print("TUI 专用命令:")
    print("  quit / exit / q        退出并关闭服务器")
    print("  clear / cls            清屏")
    print("  conn                   显示当前连接信息")


# ==================================================
#  主循环
# ==================================================
def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9080

    print(BANNER)
    print(f"启动端口: {port}")
    print(f"数据文件: {gs.DATA_FILE}")
    print()

    srv = TuiServer(host="0.0.0.0", port=port)
    if not srv.start():
        print("❌ 服务器启动超时（端口被占用？）")
        sys.exit(1)

    print(f"✅ 服务器已启动  ws://0.0.0.0:{port}")
    print(f"   局域网地址: ws://<本机IP>:{port}")
    print()
    print("输入 help 查看命令，输入 quit 退出")
    print("=" * 58)

    # ---- 管理员登录 ----
    admin_uid = input("管理员 UID (回车跳过，稍后用 login 登录): ").strip()
    admin_pw = ""
    if admin_uid:
        admin_pw = input("管理员密码: ").strip()

    print()
    print("=" * 58)

    # ---- 命令循环 ----
    while True:
        try:
            prompt = f"[{admin_uid or '未登录'}] > "
            raw = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            break

        cmd = raw.strip()
        if not cmd:
            continue

        low = cmd.lower()

        # ---- TUI 内置命令 ----
        if low in ("quit", "exit", "q"):
            print("关闭服务器...")
            srv.stop()
            break

        if low in ("clear", "cls"):
            os.system("cls" if os.name == "nt" else "clear")
            continue

        if low == "conn":
            print(f"  监听端口: {port}")
            print(f"  在线人数: {sum(1 for u in gs.users.values() if u.get('ws'))}")
            print(f"  房间数量: {len(gs.rooms)}")
            continue

        if low == "help":
            print_help()
            continue

        # ---- 重新登录（切换管理员）----
        if low == "login":
            admin_uid = input("管理员 UID: ").strip()
            admin_pw = input("管理员密码: ").strip()
            print(f"已切换管理员: {admin_uid}")
            continue

        # ---- 交给 server.py 的 admin_execute ----
        if not admin_uid:
            print("尚未登录管理员。输入 login 登录。")
            continue

        async def _task():
            u = gs.users.get(admin_uid)
            if not u or not u.get("registered"):
                return ("auth_fail", "账号不存在")
            if not gs.verify_password(admin_pw,
                                       u.get("password_hash", ""),
                                       u.get("password_salt", "")):
                return ("auth_fail", "密码错误")
            res = await gs.admin_execute(cmd, from_uid=admin_uid)
            return ("ok", res)

        result = srv.submit(_task())
        if result is None:
            continue
        tag, msg = result
        if tag == "auth_fail":
            print(f"❌ {msg}")
        else:
            print(msg)


if __name__ == "__main__":
    main()