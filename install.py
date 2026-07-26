"""OG_Pipeline インストーラ（Maya 用・ホットアップデート対応）。

使い方（初回インストール）:
  1) この install.py をブラウザで保存し、Maya のビューポートにドラッグ&ドロップ。
     もしくは Script Editor(Python) で:
         exec(open(r"C:/path/to/install.py").read())

やること:
  - GitHub から OG_Pipeline.py を最新版（SHA 固定 URL＝CDN キャッシュ回避）で取得し、
    Maya のユーザースクリプトフォルダへ原子的に上書き保存。
  - __pycache__ を掃除し、sys.modules から OG_Pipeline をフラッシュ（再起動不要）。
  - シェルフボタンを追加：
      左クリック          … メインツールを起動（OG_Pipeline.main）
      右クリックメニュー  … メイン起動 / ショットリスト起動 / GitHub から更新
  - 完了ダイアログで previous → current バージョンを表示。

以降の更新は、メインUIの「⟳ 更新」ボタン、またはシェルフ右クリック→「GitHub から更新」。
"""

from __future__ import annotations

import os
import re
import sys


# ─── CUSTOMIZE（OG_Pipeline.py の同名定数と一致させること） ───────────────────
_GITHUB_OWNER = "ogshaw03"
_GITHUB_REPO = "OG_Pipeline"
_GITHUB_BRANCH = "main"

_MODULE = "OG_Pipeline"                 # ツール本体 .py（.py 抜き）
_SHELF_BUTTON_LABEL = "OG Pipeline"     # シェルフ表示名
# ─── END CUSTOMIZE ────────────────────────────────────────────────────────

_MODULE_FILE = "%s.py" % _MODULE
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_GITHUB_API = "https://api.github.com/repos/%s/%s" % (_GITHUB_OWNER, _GITHUB_REPO)
_GITHUB_RAW = "https://raw.githubusercontent.com/%s/%s" % (_GITHUB_OWNER, _GITHUB_REPO)


# --------------------------------------------------------------------------- #
# 上書き保存ヘルパー（Windows 安全・原子的）
# --------------------------------------------------------------------------- #
def _force_writable(path):
    import stat
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except Exception:
        pass


def _atomic_write_bytes(target, data):
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    if os.path.exists(target):
        _force_writable(target)
    tmp = target + ".tmp_install"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except Exception:
            pass
    os.replace(tmp, target)


# --------------------------------------------------------------------------- #
# 取得（SHA 固定 URL）
# --------------------------------------------------------------------------- #
def _resolve_latest_sha():
    import json
    import random
    import time
    from urllib.request import Request, urlopen
    salt = "%.6f_%d" % (time.time(), random.randint(0, 2 ** 32))
    req = Request("%s/branches/%s?_=%s" % (_GITHUB_API, _GITHUB_BRANCH, salt),
                  headers={"Accept": "application/vnd.github+json",
                           "Cache-Control": "no-cache",
                           "User-Agent": "%s-installer/%s" % (_MODULE, salt)})
    try:
        with urlopen(req, timeout=30) as resp:
            sha = json.loads(resp.read().decode("utf-8"))["commit"]["sha"]
        print("[%s] resolved %s -> %s" % (_MODULE, _GITHUB_BRANCH, sha[:10]))
        return sha
    except Exception as exc:
        print("[%s] SHA lookup failed (%s); branch 名で続行" % (_MODULE, exc))
        return _GITHUB_BRANCH


def _fetch_module(dest_root):
    """既定は GitHub から取得。開発中にローカルを使うなら OG_PIPELINE_USE_LOCAL=1。"""
    from urllib.request import Request, urlopen
    use_local = os.environ.get("OG_PIPELINE_USE_LOCAL") == "1"
    src_local = os.path.join(_REPO_ROOT, _MODULE_FILE)
    target = os.path.join(dest_root, _MODULE_FILE)
    if use_local and os.path.isfile(src_local):
        print("[%s] USE_LOCAL=1 -> copy %s" % (_MODULE, src_local))
        with open(src_local, "rb") as fh:
            data = fh.read()
    else:
        sha = _resolve_latest_sha()
        url = "%s/%s/%s" % (_GITHUB_RAW, sha, _MODULE_FILE)
        print("[%s] downloading %s" % (_MODULE, url))
        req = Request(url, headers={"Cache-Control": "no-cache",
                                    "User-Agent": "%s-installer" % _MODULE})
        try:
            data = urlopen(req, timeout=30).read()
        except Exception as exc:
            raise RuntimeError("Failed to download %s: %s" % (url, exc))
    _atomic_write_bytes(target, data)
    print("[%s]   -> %s (%d bytes)" % (_MODULE, target, len(data)))


# --------------------------------------------------------------------------- #
# インストール後処理
# --------------------------------------------------------------------------- #
def _verify_install(dest_root):
    p = os.path.join(dest_root, _MODULE_FILE)
    if not os.path.isfile(p) or os.path.getsize(p) == 0:
        raise RuntimeError("Install verification failed — %s missing/empty" % p)


def _clean_pycache(dest_root):
    pycache = os.path.join(dest_root, "__pycache__")
    if not os.path.isdir(pycache):
        return
    for name in os.listdir(pycache):
        if name.startswith("%s." % _MODULE) and name.endswith(".pyc"):
            try:
                _force_writable(os.path.join(pycache, name))
                os.remove(os.path.join(pycache, name))
            except Exception:
                pass


def _flush_imports():
    sys.modules.pop(_MODULE, None)


def _read_installed_version(dest_root):
    p = os.path.join(dest_root, _MODULE_FILE)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r'\s*__version__\s*=\s*[\'"]([^\'"]+)[\'"]', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return "(unknown)"


def _close_existing_windows():
    try:
        from maya import cmds  # noqa: F401
    except Exception:
        return
    try:
        sys.modules.pop(_MODULE, None)
        import OG_Pipeline as _t
        _t._close_existing_windows()
        _t._close_windows_named(_t.SHOTLIST_OBJECT_NAME)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# シェルフボタン（左=メイン起動 / 右クリック=メニュー）
# --------------------------------------------------------------------------- #
_LAUNCH_MAIN = (
    "import sys\n"
    "sys.modules.pop(%r, None)\n"
    "import %s as _t; _t.main()\n" % (_MODULE, _MODULE)
)
_LAUNCH_SHOTLIST = (
    "import sys\n"
    "sys.modules.pop(%r, None)\n"
    "import %s as _t; _t.open_shot_list()\n" % (_MODULE, _MODULE)
)
_UPDATE_CMD = (
    "import json, urllib.request\n"
    "_api = 'https://api.github.com/repos/%s/%s/branches/%s'\n"
    "_sha = json.loads(urllib.request.urlopen(_api, timeout=30).read())['commit']['sha']\n"
    "_u = 'https://raw.githubusercontent.com/%s/%s/' + _sha + '/install.py'\n"
    "print('[%s] update via SHA', _sha[:10])\n"
    "exec(compile(urllib.request.urlopen(_u, timeout=30).read(),\n"
    "             'install.py (from GitHub)', 'exec'),\n"
    "     {'__name__': 'install', '__file__': '<github>'})\n"
    % (_GITHUB_OWNER, _GITHUB_REPO, _GITHUB_BRANCH,
       _GITHUB_OWNER, _GITHUB_REPO, _MODULE)
)


def _add_shelf_button():
    from maya import cmds, mel
    top_shelf = mel.eval("$tmp = $gShelfTopLevel")
    if not top_shelf or not cmds.tabLayout(top_shelf, exists=True):
        return
    current = cmds.tabLayout(top_shelf, q=True, selectTab=True)
    if not current:
        return
    for child in cmds.shelfLayout(current, q=True, ca=True) or []:
        try:
            if cmds.shelfButton(child, q=True, label=True) == _SHELF_BUTTON_LABEL:
                cmds.deleteUI(child)
        except Exception:
            pass
    button = cmds.shelfButton(
        parent=current,
        label=_SHELF_BUTTON_LABEL,
        annotation="左クリック: メイン起動 / 右クリック: メニュー（メイン・ショットリスト・更新）",
        image="pythonFamily.png",
        imageOverlayLabel="OGPL",
        command=_LAUNCH_MAIN,
        sourceType="python",
    )
    popup = cmds.popupMenu(parent=button, button=3)
    cmds.menuItem(parent=popup, label="OG_Pipeline を起動",
                  command=_LAUNCH_MAIN, sourceType="python")
    cmds.menuItem(parent=popup, label="ショットリストを起動",
                  command=_LAUNCH_SHOTLIST, sourceType="python")
    cmds.menuItem(parent=popup, divider=True)
    cmds.menuItem(parent=popup, label="GitHub から更新",
                  command=_UPDATE_CMD, sourceType="python")


# --------------------------------------------------------------------------- #
# エントリポイント
# --------------------------------------------------------------------------- #
def install():
    from maya import cmds
    user_scripts = cmds.internalVar(userScriptDir=True).rstrip("/\\")
    if not os.path.isdir(user_scripts):
        os.makedirs(user_scripts)

    prev_version = _read_installed_version(user_scripts)
    _close_existing_windows()
    _fetch_module(user_scripts)
    _clean_pycache(user_scripts)
    _verify_install(user_scripts)
    _flush_imports()

    if user_scripts not in sys.path:
        sys.path.insert(0, user_scripts)

    _add_shelf_button()
    new_version = _read_installed_version(user_scripts)

    print("[%s] %s" % (_MODULE, "=" * 55))
    print("[%s] installed to:     %s" % (_MODULE, user_scripts))
    print("[%s] previous version: %s" % (_MODULE, prev_version))
    print("[%s] current  version: %s" % (_MODULE, new_version))
    print("[%s] %s" % (_MODULE, "=" * 55))

    try:
        cmds.confirmDialog(
            title=_SHELF_BUTTON_LABEL,
            message=("インストール先:\n%s\n\nバージョン: %s → %s\n\n"
                     "シェルフボタン『%s』を更新しました。\n"
                     "左クリックでメイン起動、右クリックでメニュー（ショットリスト／更新）。"
                     % (user_scripts, prev_version, new_version, _SHELF_BUTTON_LABEL)),
            button=["OK"])
    except Exception:
        pass
    return user_scripts


def onMayaDroppedPythonFile(*_args):
    install()


# Script Editor で exec(open(...).read()) された場合は __name__ 判定を通らないため、
# Maya 内なら import 時点で install() を実行（install() は冪等）。
try:
    from maya import cmds as _cmds  # noqa: F401
    install()
except ImportError:
    pass
