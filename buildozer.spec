[app]

# ── 应用信息 ────────────────────────────────────────────────
title = Neon Console
package.name = neonconsole
package.domain = org.neondrift

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,so,html,apk,json,txt,md
source.include_patterns = server/**,website/**,libs/**
source.exclude_patterns = bin/**,.buildozer/**,*.pyc,__pycache__/**

version = 1.0.0

# ── 依赖 ────────────────────────────────────────────────────
requirements = python3,kivy,websockets,jnius,openssl

# ── Android 配置 ────────────────────────────────────────────
orientation = portrait
fullscreen = 0
android.api = 33
android.minapi = 24
android.ndk = 25b
android.archs = arm64-v8a
android.allow_backup = True
android.accept_sdk_license = True

# 关键：声明权限
android.permissions = INTERNET,ACCESS_NETWORK_STATE,FOREGROUND_SERVICE

# 关键：让 Android 把 libs/ 中的 .so 提取到 nativeLibraryDir
android.extract_native_libs = true

# 应用图标（可选，若没有可删掉这两行）
# icon.filename = %(source.dir)s/icon.png
# presplash.filename = %(source.dir)s/presplash.png

# ── 日志 ────────────────────────────────────────────────────
log_level = 2

# ── 桌面调试 ────────────────────────────────────────────────
[buildozer]
log_level = 2
warn_on_root = 1