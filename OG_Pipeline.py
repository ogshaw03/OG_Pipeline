"""
OG_Pipeline — Maya Scene Opener

機能:
  - プロジェクトごとに変わるルートパスを JSON として保存／インポートし、
    プルダウンで切り替える。
  - [★ 次回も使用] ボタンで、選択中のルートを次回起動時に自動適用する。
  - フォルダの潜り込みはドリルダウン（ツリー展開）ではなく、
    Finder ライクな横並びカラム（Miller カラム）で表示する。

設定ファイル（プロジェクト非依存・Maya のバージョンに依存しない通常ファイル）:
  <userAppDir>/og_pipeline/roots.json    … 登録済みルート一覧
  <userAppDir>/og_pipeline/_config.json  … 起動時に自動適用するルート名
  ※ Maya 外ではホームディレクトリ配下に作成される。
"""

import os
import sys
import re
import json
import time
import shutil
import subprocess
from pathlib import Path

try:
    from PySide2 import QtWidgets, QtCore, QtGui
    from PySide2.QtCore import Qt, QThread, Signal, QSize, QTimer, QUrl
    from PySide2.QtGui import QFont, QColor, QPalette, QIcon, QPixmap, QPainter, QLinearGradient, QImage
    from PySide2.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QLineEdit,
        QSplitter, QFrame, QScrollArea, QComboBox, QMessageBox,
        QSizePolicy, QToolButton, QStatusBar, QProgressBar, QFileDialog,
        QListWidget, QListWidgetItem, QInputDialog, QMenu,
        QDialog, QDialogButtonBox, QGridLayout, QCheckBox, QSpinBox, QFormLayout
    )
except ImportError:
    try:
        from PySide6 import QtWidgets, QtCore, QtGui
        from PySide6.QtCore import Qt, QThread, Signal, QSize, QTimer, QUrl
        from PySide6.QtGui import QFont, QColor, QPalette, QIcon, QPixmap, QPainter, QLinearGradient, QImage
        from PySide6.QtWidgets import (
            QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
            QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QLineEdit,
            QSplitter, QFrame, QScrollArea, QComboBox, QMessageBox,
            QSizePolicy, QToolButton, QStatusBar, QProgressBar, QFileDialog,
            QListWidget, QListWidgetItem, QInputDialog, QMenu,
            QDialog, QDialogButtonBox, QGridLayout, QCheckBox, QSpinBox, QFormLayout
        )
    except ImportError:
        raise ImportError("PySide2 または PySide6 が必要です。")

# ─── 定数 ────────────────────────────────────────────────────────────────────
MAYA_EXTENSIONS = {".ma", ".mb"}
WINDOW_OBJECT_NAME = "OGPipelineSceneOpenerWindow"   # 多重起動検出用の安定識別名
VIDEO_SUBDIR = "Pipeline_Movie"                      # プレイブラスト出力フォルダ名
VIDEO_EXTS = [".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v"]

# QtMultimedia（動画再生）。Maya 同梱 PySide には無いことがあるため任意依存とする。
# 描画は QVideoWidget ではなく QLabel に行う（Maya 内で QVideoWidget が黒画面になる
# 問題を回避し、連番画像と同じ内蔵プレイヤーで mp4 も再生するため）。
_QT_MM = None
try:
    from PySide2.QtMultimedia import (QMediaPlayer, QMediaContent,
                                      QAbstractVideoSurface, QVideoFrame, QAbstractVideoBuffer)
    from PySide2.QtGui import QImage
    _QT_MM = 2
except Exception:
    try:
        from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink
        from PySide6.QtGui import QImage
        _QT_MM = 6
    except Exception:
        _QT_MM = None


# PySide2: 動画フレームを受け取って QLabel に渡すサーフェス（別スレッド対策で Signal 経由）
_FrameSurface = None
if _QT_MM == 2:
    class _FrameSurface(QAbstractVideoSurface):
        newImage = Signal(object)

        def supportedPixelFormats(self, handleType=QAbstractVideoBuffer.NoHandle):
            # RGB 系のみ申告 → バックエンドが RGB32 等へ変換して present してくれる
            return [QVideoFrame.Format_RGB32, QVideoFrame.Format_ARGB32,
                    QVideoFrame.Format_ARGB32_Premultiplied,
                    QVideoFrame.Format_RGB24, QVideoFrame.Format_BGR32]

        def present(self, frame):
            try:
                f = QVideoFrame(frame)
                if f.map(QAbstractVideoBuffer.ReadOnly):
                    fmt = QVideoFrame.imageFormatFromPixelFormat(f.pixelFormat())
                    if fmt != QImage.Format_Invalid:
                        img = QImage(f.bits(), f.width(), f.height(), f.bytesPerLine(), fmt)
                        self.newImage.emit(img.copy())
                    f.unmap()
            except Exception:
                pass
            return True


SEQ_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".exr")


def find_scene_video(scene_path):
    """シーンと同名の動画を movies フォルダから探す。無ければ None。"""
    if not scene_path:
        return None
    p = Path(scene_path)
    folder = p.parent / VIDEO_SUBDIR
    for ext in VIDEO_EXTS:
        cand = folder / (p.stem + ext)
        if cand.exists():
            return str(cand)
    return None


def find_scene_sequence(scene_path):
    """Pipeline_Movie/<シーン名>/ 内の連番画像（ソート済みパスのリスト）を返す。無ければ None。"""
    if not scene_path:
        return None
    p = Path(scene_path)
    seq_dir = p.parent / VIDEO_SUBDIR / p.stem
    if seq_dir.is_dir():
        frames = sorted(
            str(f) for f in seq_dir.iterdir()
            if f.is_file() and f.suffix.lower() in SEQ_EXTS
        )
        if frames:
            return frames
    return None


def find_latest_video_under(folder):
    """フォルダ配下を再帰探索し、更新日時が最新の動画ファイルのパスを返す。

    シーンフォルダ直下／movie フォルダ／Pipeline_Movie など場所を問わず、
    動画ファイル(VIDEO_EXTS)の中で mtime 最新のものを採用する。無ければ None。
    """
    if not folder or not os.path.isdir(folder):
        return None
    best = None  # (mtime, path)
    for cur, dirs, files in os.walk(folder):
        for f in files:
            if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                full = os.path.join(cur, f)
                try:
                    m = os.path.getmtime(full)
                except Exception:
                    m = 0.0
                if best is None or m > best[0]:
                    best = (m, full)
    return best[1] if best else None


def find_latest_sequence_under(folder):
    """フォルダ配下で最新の連番画像（同一フォルダ内の画像群）を返す。無ければ None。

    工程フォルダ／ショットフォルダ配下の Pipeline_Movie/<stem>/ などを再帰探索し、
    最も新しいフレームを含むフォルダの連番（ソート済み）を返す。
    """
    if not folder or not os.path.isdir(folder):
        return None
    best = None  # (mtime, dirpath)
    for cur, dirs, files in os.walk(folder):
        imgs = [f for f in files if os.path.splitext(f)[1].lower() in SEQ_EXTS]
        if not imgs:
            continue
        try:
            m = max(os.path.getmtime(os.path.join(cur, f)) for f in imgs)
        except Exception:
            m = 0.0
        if best is None or m > best[0]:
            best = (m, cur)
    if not best:
        return None
    d = best[1]
    frames = sorted(os.path.join(d, f) for f in os.listdir(d)
                    if os.path.splitext(f)[1].lower() in SEQ_EXTS)
    return frames or None


def pick_folder_media(folder):
    """選択フォルダ配下の再生対象を決める。

    優先順位:
      - cv2 が使える & 動画あり          → ("video", path)   … cv2 で埋め込み再生
      - 連番あり                          → ("seq", [frames]) … フリップブック再生
      - 動画はあるが cv2 無し & 連番無し  → ("ext", path)     … 外部プレイヤーのみ
      - どれも無し                        → None
    cv2 が無い場合は「連番」を優先フォールバックにする。
    """
    # cv2 が使えるなら動画を先に探し、見つかれば連番探索（os.walk）は省略する。
    # ネットワーク/OneDrive 上ではフォルダ走査が遅いため、無駄な走査を減らす。
    video = find_latest_video_under(folder)
    if _HAS_CV2 and video:
        return ("video", video)
    seq = find_latest_sequence_under(folder)
    if seq:
        return ("seq", seq)
    if video:
        return ("ext", video)
    return None


def _media_mtime(media):
    """メディア（("video"/"ext", path) または ("seq", [frames])）の更新日時。"""
    if not media:
        return 0.0
    kind, val = media[0], media[1]
    try:
        if kind == "seq":
            return max(os.path.getmtime(f) for f in val)
        return os.path.getmtime(val)
    except Exception:
        return 0.0


def shot_stage_list(shot_folder):
    """ショット直下の各工程フォルダ（lay, anm 等）の最新メディアを返す。

    戻り値: [(stage_name, media, mtime), ...]（mtime 昇順）。
    工程フォルダ＝ショット直下のサブフォルダ（Pipeline_Movie は除外）。
    """
    out = []
    try:
        for d in sorted(os.listdir(shot_folder)):
            full = os.path.join(shot_folder, d)
            if not os.path.isdir(full) or d == VIDEO_SUBDIR:
                continue
            media = pick_folder_media(full)
            if media:
                out.append((d, media, _media_mtime(media)))
    except Exception:
        pass
    out.sort(key=lambda s: s[2])
    return out


def open_file_external(path):
    """OS の既定アプリでファイルを開く。"""
    try:
        if sys.platform.startswith("win"):
            os.startfile(os.path.normpath(path))   # noqa: P204
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception as e:
        print("[OG_Pipeline] 動画を開けませんでした:", e)
        return False


# ─── 動画デコード（OpenCV）。あれば mp4 を埋め込み再生できる ──────────────────────
_HAS_CV2 = False


def _ensure_user_site():
    """--user インストール先（ユーザー site-packages）を sys.path に追加。"""
    try:
        import site
        for us in {site.getusersitepackages()} if hasattr(site, "getusersitepackages") else set():
            if us and os.path.isdir(us) and us not in sys.path:
                sys.path.append(us)
    except Exception:
        pass


def _try_import_cv2():
    global _HAS_CV2
    try:
        import cv2  # noqa: F401
        _HAS_CV2 = True
        return True
    except Exception:
        _ensure_user_site()
        try:
            import cv2  # noqa: F401
            _HAS_CV2 = True
        except Exception:
            _HAS_CV2 = False
    return _HAS_CV2


_try_import_cv2()


def _find_mayapy():
    """mayapy 実行ファイルのパスを返す（無ければ None）。"""
    d = os.path.dirname(sys.executable)
    cands = [os.path.join(d, "mayapy.exe"), os.path.join(d, "mayapy"),
             os.path.join(d, "bin", "mayapy.exe"), os.path.join(d, "bin", "mayapy")]
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def _find_maya_batch():
    """バッチ実行用の Maya 実行ファイルを返す。(path, is_batch_exe) or (None, False)。

    mayabatch(.exe) があればそれを優先（is_batch_exe=True）。
    無ければ maya(.exe) を使い、呼び出し側で -batch を付ける（is_batch_exe=False）。
    """
    d = os.path.dirname(sys.executable)
    search = [d, os.path.join(d, "bin")]
    for base in search:
        for name in ("mayabatch.exe", "mayabatch"):
            c = os.path.join(base, name)
            if os.path.isfile(c):
                return c, True
    for base in search:
        for name in ("maya.exe", "maya"):
            c = os.path.join(base, name)
            if os.path.isfile(c):
                return c, False
    return None, False


def _hw_worker_source(scene_path, seq_dir, stem):
    """別プロセス(mayabatch)内で実行するハードウェアレンダー用ワーカースクリプト。

    Viewport 2.0（mayaHardware2）で再生範囲を連番JPEGに書き出し、
    Pipeline_Movie/<stem>/ に <stem>.####.jpg として保存する。
    成否は seq_dir 内の _oghw_log.txt に記録する。
    """
    # パスはリテラルとして安全に埋め込む（repr でエスケープ）
    return (
        "import os, shutil, traceback\n"
        "import maya.cmds as cmds\n"
        "SCENE = %r\n"
        "SEQ_DIR = %r\n"
        "STEM = %r\n"
        "log = []\n"
        "def w(m):\n"
        "    log.append(str(m))\n"
        "try:\n"
        "    if not os.path.isdir(SEQ_DIR):\n"
        "        os.makedirs(SEQ_DIR)\n"
        "    try:\n"
        "        start = int(cmds.playbackOptions(q=True, min=True))\n"
        "        end = int(cmds.playbackOptions(q=True, max=True))\n"
        "    except Exception:\n"
        "        start, end = 1, 1\n"
        "    try:\n"
        "        wdt = int(cmds.getAttr('defaultResolution.width')) or 1280\n"
        "        hgt = int(cmds.getAttr('defaultResolution.height')) or 720\n"
        "    except Exception:\n"
        "        wdt, hgt = 1280, 720\n"
        "    try:\n"
        "        cmds.setAttr('defaultRenderGlobals.currentRenderer', 'mayaHardware2', type='string')\n"
        "    except Exception as e:\n"
        "        w('renderer set failed: %%s' %% e)\n"
        "    try:\n"
        "        cmds.setAttr('defaultRenderGlobals.imageFormat', 8)\n"  # 8 = JPEG
        "    except Exception:\n"
        "        pass\n"
        "    made = 0\n"
        "    for f in range(start, end + 1):\n"
        "        try:\n"
        "            cmds.currentTime(f)\n"
        "            out = cmds.ogsRender(width=wdt, height=hgt, currentFrame=True)\n"
        "            src = out[0] if isinstance(out, (list, tuple)) and out else out\n"
        "            if src and os.path.isfile(src):\n"
        "                dst = os.path.join(SEQ_DIR, '%%s.%%04d.jpg' %% (STEM, f))\n"
        "                shutil.copy2(src, dst)\n"
        "                made += 1\n"
        "            else:\n"
        "                w('frame %%d: no output (%%s)' %% (f, src))\n"
        "        except Exception as e:\n"
        "            w('frame %%d error: %%s' %% (f, e))\n"
        "    w('done: %%d/%%d frames -> %%s' %% (made, end - start + 1, SEQ_DIR))\n"
        "except Exception:\n"
        "    w(traceback.format_exc())\n"
        "finally:\n"
        "    try:\n"
        "        with open(os.path.join(SEQ_DIR, '_oghw_log.txt'), 'w') as fh:\n"
        "            fh.write('\\n'.join(log))\n"
        "    except Exception:\n"
        "        pass\n"
    ) % (scene_path, seq_dir, stem)


def export_hardware_background(scene_path):
    """別プロセス(mayabatch)でハードウェアレンダー書き出しをバックグラウンド起動する。

    現在の Maya セッションをブロックしない（Popen して即 return）。
    戻り値: (ok, message, proc)。ok=True は「起動できた」を意味し、完了は意味しない。
    proc は起動したプロセス（完了監視に使う）。失敗時は None。
    """
    batch_exe, is_batch = _find_maya_batch()
    if not batch_exe:
        return False, "mayabatch / maya 実行ファイルが見つかりませんでした。", None
    if not scene_path or not os.path.isfile(scene_path):
        return False, "シーンファイルが見つかりません（保存後に実行してください）。", None

    stem = Path(scene_path).stem
    seq_dir = os.path.join(os.path.dirname(scene_path), VIDEO_SUBDIR, stem)
    try:
        if os.path.isdir(seq_dir):
            shutil.rmtree(seq_dir, ignore_errors=True)
        os.makedirs(seq_dir, exist_ok=True)
    except Exception as e:
        return False, "出力フォルダを作成できませんでした: %s" % e, None

    # ワーカースクリプトをスクラッチに書き出す
    worker_dir = get_config_dir()
    worker_py = os.path.join(worker_dir, "_oghw_worker.py")
    try:
        with open(worker_py, "w", encoding="utf-8") as fh:
            fh.write(_hw_worker_source(scene_path, seq_dir, stem))
    except Exception as e:
        return False, "ワーカースクリプトを書き出せませんでした: %s" % e, None

    # MEL の -command から python ワーカーを実行する
    mel_cmd = 'python("import runpy; runpy.run_path(r\'%s\')")' % worker_py.replace("\\", "/")
    args = [batch_exe]
    if not is_batch:
        args.append("-batch")
    args += ["-file", scene_path, "-command", mel_cmd]

    # Windows ではコンソール窓を出さない
    kwargs = {}
    try:
        if sys.platform.startswith("win"):
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            kwargs["startupinfo"] = si
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    except Exception:
        pass

    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **kwargs)
    except Exception as e:
        return False, "バックグラウンド起動に失敗しました: %s" % e, None
    return True, "バックグラウンドで書き出し中…（裏で処理しています）", proc


def install_opencv():
    """opencv-python-headless を --user で導入する。戻り値: (成功, ログ)。

    --user なので共有 Maya 本体は変更せず、管理者権限も不要。
    """
    exe = _find_mayapy()
    if not exe:
        return False, "mayapy が見つかりませんでした。手動で `mayapy -m pip install --user opencv-python-headless` を実行してください。"
    try:
        proc = subprocess.run(
            [exe, "-m", "pip", "install", "--user", "opencv-python-headless"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900,
        )
        log = (proc.stdout or b"").decode("utf-8", "replace")[-2000:]
        ok = (proc.returncode == 0)
        if ok:
            _try_import_cv2()
        return (ok and _HAS_CV2), log
    except Exception as e:
        return False, str(e)


class Cv2VideoThread(QThread):
    """cv2 で mp4 をバックグラウンドデコードし、縮小済みフレームを QImage で通知する。

    GUI スレッドをブロックしないため UI が固まらない。max_w で解像度を落として
    デコード後にリサイズ（描画コスト・転送量を削減）。
    """
    frameReady = Signal(object)   # QImage

    def __init__(self, path, max_w=640, fps=None, parent=None):
        super().__init__(parent)
        self._path = path
        self._max_w = int(max_w) if max_w else 0
        self._fps = fps
        self._running = True

    def run(self):
        try:
            import cv2
        except Exception:
            return
        cap = cv2.VideoCapture(self._path)
        if not cap.isOpened():
            try:
                cap.release()
            except Exception:
                pass
            return
        src = cap.get(cv2.CAP_PROP_FPS) or 24.0
        fps = self._fps or src
        if not fps or fps <= 0 or fps > 60:
            fps = 24.0
        delay = max(10, int(1000.0 / fps))
        try:
            while self._running:
                ok, frame = cap.read()
                if not ok:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, frame = cap.read()
                    if not ok:
                        break
                h, w = frame.shape[:2]
                if self._max_w and w > self._max_w:
                    nh = max(1, int(h * self._max_w / float(w)))
                    frame = cv2.resize(frame, (self._max_w, nh))
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                hh, ww = rgb.shape[:2]
                img = QImage(rgb.data, ww, hh, 3 * ww, QImage.Format_RGB888).copy()
                self.frameReady.emit(img)
                self.msleep(delay)
        except Exception:
            pass
        finally:
            try:
                cap.release()
            except Exception:
                pass

    def stop(self):
        # GUI スレッドをブロックしないよう wait しない。run() は次のループで抜ける。
        self._running = False


# 実行中スレッドを保持しておくための保管庫。
# QThread を「実行中のまま」破棄すると Qt がプロセスを abort する（＝Maya クラッシュ）。
# stop() は wait しない設計なので、終了するまで参照を保持し finished で自己破棄させる。
_LIVE_CV_THREADS = set()


def _release_cv_thread(th):
    """cv2 スレッドを安全に停止・解放する。

    実行中の QThread が親ウィジェット破棄に巻き込まれて落ちないよう、
    親から切り離し→保管庫で保持→finished で deleteLater、という流れにする。
    GUI はブロックしない（wait しない）。
    """
    if th is None:
        return
    try:
        th.frameReady.disconnect()
    except Exception:
        pass
    try:
        th.stop()                       # _running = False（次ループで抜ける）
    except Exception:
        pass
    try:
        th.setParent(None)              # 親(セル/プレイヤー)破棄に巻き込まれないよう切り離す
    except Exception:
        pass
    _LIVE_CV_THREADS.add(th)

    def _drop(_th=th):
        _LIVE_CV_THREADS.discard(_th)
        try:
            _th.deleteLater()
        except Exception:
            pass
    try:
        th.finished.connect(_drop)
    except Exception:
        pass
    # 既に終了済みなら即解放（finished が飛ばないケースの保険）。
    try:
        if not th.isRunning():
            _drop()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  ルート設定の永続化（JSON）
#  Playblast ツールと同じ方針: optionVar ではなく通常ファイルに保存する。
#  バージョン非依存・prefs リセット耐性・即時書き込み（クラッシュ耐性）が得られる。
# ═══════════════════════════════════════════════════════════════════════════════
def get_config_dir():
    """設定 JSON を保存するディレクトリ（プロジェクト非依存）。"""
    base = None
    try:
        import maya.cmds as cmds
        base = cmds.internalVar(userAppDir=True)
    except Exception:
        base = None
    if not base:
        base = os.path.expanduser("~")
    path = os.path.join(base, "og_pipeline")
    if not os.path.isdir(path):
        try:
            os.makedirs(path)
        except Exception as e:
            print("[OG_Pipeline] 設定フォルダ作成エラー:", e)
    return path


def roots_path():
    return os.path.join(get_config_dir(), "roots.json")


def _config_path():
    return os.path.join(get_config_dir(), "_config.json")


def _normalize_entries(data):
    """任意の入力を [{'name','path','shots_parent'}, ...] に正規化する。"""
    if isinstance(data, dict) and "roots" in data:
        data = data["roots"]
    elif isinstance(data, dict) and "path" in data:
        data = [data]
    if not isinstance(data, list):
        return []
    out, seen = [], set()
    for e in data:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name", "")).strip()
        path = str(e.get("path", "")).strip()
        if not name or not path or name in seen:
            continue
        seen.add(name)
        out.append({
            "name": name,
            "path": path,
            # ショットフォルダの親階層（直下のフォルダ＝ショット）。未設定なら root 自身。
            "shots_parent": str(e.get("shots_parent", "")).strip() or path,
        })
    return out


def load_roots():
    """登録済みルートの一覧を返す。"""
    try:
        with open(roots_path(), "r", encoding="utf-8") as fh:
            return _normalize_entries(json.load(fh))
    except Exception:
        return []


def save_roots(roots):
    try:
        with open(roots_path(), "w", encoding="utf-8") as fh:
            json.dump({"roots": roots}, fh, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[OG_Pipeline] ルート保存エラー:", e)


def add_root(name, path, shots_parent=""):
    """ルートを追加（同名は上書き）し、名前順で保存する。"""
    roots = [r for r in load_roots() if r["name"] != name]
    roots.append({"name": name, "path": path,
                  "shots_parent": (shots_parent or path)})
    roots.sort(key=lambda r: r["name"].lower())
    save_roots(roots)
    return roots


def remove_root(name):
    roots = [r for r in load_roots() if r["name"] != name]
    save_roots(roots)
    return roots


def find_root_path(name):
    for r in load_roots():
        if r["name"] == name:
            return r["path"]
    return None


def find_root_entry(name):
    for r in load_roots():
        if r["name"] == name:
            return r
    return None


def import_roots_file(filepath):
    """外部 JSON を読み込み、ストアにマージする。戻り値: 取り込んだ件数。"""
    with open(filepath, "r", encoding="utf-8") as fh:
        entries = _normalize_entries(json.load(fh))
    roots = load_roots()
    for e in entries:
        roots = [r for r in roots if r["name"] != e["name"]]  # 同名は上書き
        roots.append(e)
    roots.sort(key=lambda r: r["name"].lower())
    save_roots(roots)
    return len(entries)


def export_roots_file(filepath, roots=None):
    """ルート設定 JSON を書き出す（他環境へ共有・配布できる形式）。"""
    if roots is None:
        roots = load_roots()
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump({"roots": roots}, fh, ensure_ascii=False, indent=2)


def _read_config():
    try:
        with open(_config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_config(data):
    try:
        with open(_config_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[OG_Pipeline] 設定保存エラー:", e)


def get_startup_root():
    """次回起動時に自動適用するルート名。未設定なら None。"""
    return _read_config().get("startup_root") or None


def set_startup_root(name):
    cfg = _read_config()
    cfg["startup_root"] = name
    _write_config(cfg)


def clear_startup_root():
    cfg = _read_config()
    if "startup_root" in cfg:
        cfg.pop("startup_root", None)
        _write_config(cfg)


# 動画書き出し方式: "playblast"（同一セッションでビューを撮る）/
#                    "hardware"（別 mayabatch プロセスでハードウェアレンダー＝バックグラウンド）
EXPORT_METHODS = ("playblast", "hardware")


def get_export_method():
    m = _read_config().get("export_method")
    return m if m in EXPORT_METHODS else "playblast"


def set_export_method(method):
    if method not in EXPORT_METHODS:
        return
    cfg = _read_config()
    cfg["export_method"] = method
    _write_config(cfg)


def get_auto_export_on_save():
    """シーン保存のたびに動画を自動更新するか。"""
    return bool(_read_config().get("auto_export_on_save", False))


def set_auto_export_on_save(value):
    cfg = _read_config()
    cfg["auto_export_on_save"] = bool(value)
    _write_config(cfg)


def get_auto_export_interval_min():
    """自動書き出しの最小間隔（分）。前回更新からこの分数未満なら書き出さない。"""
    try:
        return max(0, int(_read_config().get("auto_export_interval_min", 1)))
    except Exception:
        return 1


def set_auto_export_interval_min(minutes):
    cfg = _read_config()
    try:
        cfg["auto_export_interval_min"] = max(0, int(minutes))
    except Exception:
        cfg["auto_export_interval_min"] = 1
    _write_config(cfg)


def get_manual_export_method():
    """手動書き出しの方式（ムービーバーのプルダウン）。未設定なら自動更新の方式に従う。"""
    m = _read_config().get("manual_export_method")
    if m in EXPORT_METHODS:
        return m
    return get_export_method()


def set_manual_export_method(method):
    if method not in EXPORT_METHODS:
        return
    cfg = _read_config()
    cfg["manual_export_method"] = method
    _write_config(cfg)


def reveal_in_explorer(path):
    """OS のファイラでパスを開く。ファイルなら選択状態で、フォルダならそのまま開く。

    Playblast ツールの open_in_explorer と同じ方針でクロスプラットフォーム対応。
    成否を bool で返す。
    """
    p = os.path.normpath(str(path))
    is_file = os.path.isfile(p)
    folder = p if os.path.isdir(p) else os.path.dirname(p)
    # 存在するフォルダまで親を遡る
    while folder and not os.path.isdir(folder):
        parent = os.path.dirname(folder)
        if parent == folder:
            folder = None
            break
        folder = parent
    if not folder:
        return False
    try:
        if sys.platform.startswith("win"):
            if is_file:
                subprocess.Popen('explorer /select,"{}"'.format(p))
            else:
                subprocess.Popen('explorer "{}"'.format(folder))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", p] if is_file else ["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
        return True
    except Exception as e:
        print("[OG_Pipeline] フォルダを開けませんでした:", e)
        return False


# ─── スタイル ────────────────────────────────────────────────────────────────
STYLE = """
QMainWindow, QWidget {
    background-color: #0f1117;
    color: #c8ccd4;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 12px;
}

#header {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #1a1f2e, stop:0.5 #141824, stop:1 #0f1117);
    border-bottom: 2px solid #e8a838;
}
#appTitle {
    font-size: 20px; font-weight: bold; color: #e8a838; letter-spacing: 4px;
}
#appSubtitle { font-size: 10px; color: #4a5568; letter-spacing: 2px; }
#rootPathLabel {
    font-size: 10px; color: #4a9eff; letter-spacing: 1px;
    padding: 2px 8px; background: #141824; border-left: 2px solid #4a9eff;
}

#toolbar {
    background: #141824; border-bottom: 1px solid #1e2435;
}

#searchBar {
    background: #1a1f2e; border: 1px solid #2a3045; border-radius: 3px;
    color: #c8ccd4; padding: 5px 10px; min-height: 28px;
}
#searchBar:focus { border-color: #e8a838; background: #1e2435; }

QComboBox {
    background: #1a1f2e; border: 1px solid #2a3045; border-radius: 3px;
    color: #c8ccd4; padding: 4px 8px; min-height: 28px;
}
QComboBox:hover { border-color: #e8a838; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background: #1a1f2e; border: 1px solid #e8a838; color: #c8ccd4;
    selection-background-color: #2a2010; selection-color: #e8a838;
}

/* ─── カラムブラウザ（横並びパネル） ─── */
#columnScroll { border: none; background: #0f1117; }
#browserCol { border-right: 1px solid #1e2435; }
#browserColHeader {
    background: #141824; color: #e8a838;
    font-size: 11px; font-weight: bold; letter-spacing: 1px;
    padding: 4px 8px;
    border-bottom: 1px solid #2a3045;
}
#browserColumn {
    background: #0f1117; border: none;
    color: #c8ccd4; font-family: "Consolas", monospace; font-size: 12px;
    outline: none;
}
#browserColumn::item { padding: 5px 8px; border-bottom: 1px solid #141824; }
#browserColumn::item:hover { background: #1a1f2e; color: #e8c87a; }
#browserColumn::item:selected {
    background: #2a2010; color: #e8a838; border-left: 3px solid #e8a838;
}

#detailPanel { background: #0d1018; border-left: 2px solid #1e2435; }
#detailTitle {
    color: #e8a838; font-size: 13px; font-weight: bold; letter-spacing: 2px;
    padding: 12px 16px 6px; border-bottom: 1px solid #1e2435;
}
#detailKey { color: #4a9eff; font-size: 11px; }
#detailValue { color: #9aa3b0; font-size: 11px; }
#detailFilename { color: #e8c87a; font-size: 13px; font-weight: bold; }
#detailPath { color: #3a4a6a; font-size: 10px; }

QPushButton {
    background: #1a1f2e; color: #c8ccd4; border: 1px solid #2a3045;
    border-radius: 3px; padding: 6px 16px; min-height: 28px;
}
QPushButton:hover { background: #1e2435; border-color: #4a9eff; color: #4a9eff; }
QPushButton:pressed { background: #141824; border-color: #e8a838; }

#openBtn {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #2a1e00, stop:1 #1a1300);
    color: #e8a838; border: 1px solid #e8a838; border-radius: 3px;
    padding: 8px 24px; font-size: 13px; font-weight: bold; letter-spacing: 2px;
    min-height: 36px; min-width: 140px;
}
#openBtn:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #3a2a00, stop:1 #2a1e00); color: #ffd060;
}
#openBtn:disabled { background: #141824; color: #2a3045; border-color: #1e2435; }

#importBtn {
    background: #0d1a1a; color: #3dcfb8; border: 1px solid #2a4a44;
    min-height: 36px; min-width: 120px;
}
#importBtn:hover { background: #112222; border-color: #3dcfb8; }
#importBtn:disabled { background: #141824; color: #2a3045; border-color: #1e2435; }

#refreshBtn {
    background: #141824; color: #4a9eff; border: 1px solid #2a3045;
    padding: 5px 12px; min-height: 28px;
}
#refreshBtn:hover { border-color: #4a9eff; background: #1a1f2e; }

QSplitter::handle { background: #1e2435; width: 2px; }
QSplitter::handle:hover { background: #e8a838; }

QProgressBar {
    background: #1a1f2e; border: 1px solid #2a3045; border-radius: 2px;
    height: 4px; text-align: center;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #e8a838, stop:1 #ffd060); border-radius: 2px;
}

QScrollBar:vertical { background: #0f1117; width: 8px; border: none; }
QScrollBar::handle:vertical { background: #2a3045; border-radius: 4px; min-height: 20px; }
QScrollBar::handle:vertical:hover { background: #e8a838; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar:horizontal { background: #0f1117; height: 8px; border: none; }
QScrollBar::handle:horizontal { background: #2a3045; border-radius: 4px; min-width: 20px; }
QScrollBar::handle:horizontal:hover { background: #e8a838; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
"""


# ─── 再帰検索スレッド（検索バー用） ──────────────────────────────────────────────
class ScanThread(QThread):
    found = Signal(list)            # [(rel_path, abs_path, size, mtime), ...]
    finished_scan = Signal(int)

    def __init__(self, root: Path, extension_filter=None):
        super().__init__()
        self.root = root
        self.extension_filter = extension_filter  # None / ".ma" / ".mb"

    def run(self):
        results = []
        if not self.root.exists():
            self.found.emit(results)
            self.finished_scan.emit(0)
            return
        try:
            for path in self.root.rglob("*"):
                if self.isInterruptionRequested():
                    return
                suf = path.suffix.lower()
                if suf in MAYA_EXTENSIONS:
                    if self.extension_filter and suf != self.extension_filter:
                        continue
                    try:
                        st = path.stat()
                        results.append((str(path.relative_to(self.root)),
                                        str(path), st.st_size, st.st_mtime))
                    except Exception:
                        pass
        except Exception as e:
            print("[OG_Pipeline] 検索エラー:", e)
        self.found.emit(results)
        self.finished_scan.emit(len(results))


# ─── 動画プレイヤー（サイドバー） ───────────────────────────────────────────────
class VideoPlayer(QWidget):
    """シーンと同名のプレイブラスト動画をサイドバーで再生する。

    QtMultimedia があれば埋め込み再生（ループ）、無ければ外部プレイヤーで開くボタン。
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._path = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self._placeholder = QLabel("ファイルを選択すると\n動画を表示します")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet(
            "color: #2a3045; font-size: 11px; background: #05070c; border: 1px solid #1e2435;"
        )
        self._placeholder.setMinimumHeight(150)
        lay.addWidget(self._placeholder)

        # 連番画像のフリップブック再生用
        self._frames = []
        self._idx = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._next_frame)
        # cv2 による mp4 再生用（バックグラウンドデコード）
        self._cv_thread = None
        self._frameLabel = QLabel()
        self._frameLabel.setAlignment(Qt.AlignCenter)
        self._frameLabel.setMinimumHeight(150)
        self._frameLabel.setStyleSheet("background: #000;")
        self._frameLabel.hide()
        lay.addWidget(self._frameLabel)
        self._counter = QLabel("")
        self._counter.setAlignment(Qt.AlignCenter)
        self._counter.setStyleSheet("color: #4a5568; font-size: 10px;")
        self._counter.hide()
        lay.addWidget(self._counter)

        # 動画ファイル（mp4 等）用。QVideoWidget は使わず、フレームを上の QLabel に描く。
        # 動画再生は cv2（バックグラウンドスレッド）で行う。QtMultimedia は
        # Maya では再生不可かつ重いため使用しない。
        self._player = None
        self._got_frame = False
        self._video_token = 0

        self._openBtn = QPushButton("▶  外部プレイヤーで開く")
        self._openBtn.setObjectName("refreshBtn")
        self._openBtn.clicked.connect(self._open_external)
        self._openBtn.hide()
        lay.addWidget(self._openBtn)

    # ── 共通 ──────────────────────────────────────────────
    def _stop_all(self):
        self._timer.stop()
        if self._cv_thread is not None:
            _release_cv_thread(self._cv_thread)
            self._cv_thread = None

    def clear_player(self):
        self._stop_all()
        self._frames = []
        self._path = None
        self._frameLabel.hide()
        self._frameLabel.clear()
        self._counter.hide()
        self._openBtn.hide()
        self._placeholder.setText("動画なし（プレイブラスト未作成）")
        self._placeholder.show()

    def _paint_image(self, img):
        """QImage を frameLabel に表示（mp4 フレーム・連番共通の描画先）。"""
        try:
            if img is None or img.isNull():
                return
            if not self._got_frame:          # 最初のフレーム到達 → 埋め込み再生成功
                self._got_frame = True
                self._placeholder.hide()
                self._frameLabel.show()
            w = self._frameLabel.width()
            h = self._frameLabel.height()
            if w < 10 or h < 10:
                w = max(self.width() - 8, 240)
                h = 150
            self._frameLabel.setPixmap(
                QPixmap.fromImage(img).scaled(w, h, Qt.KeepAspectRatio, Qt.FastTransformation)
            )
        except Exception:
            pass

    def _on_player_error(self, *args):
        # 再生エラー（コーデック無し等）→ 外部再生へフォールバック
        if self._path:
            self._video_unavailable()

    def _video_unavailable(self):
        self._timer.stop()
        try:
            self._player.stop()
        except Exception:
            pass
        self._frameLabel.hide()
        self._placeholder.setText(
            "この環境では mp4 を埋め込み再生できません。\n"
            "下の［外部プレイヤーで開く］で再生してください。"
        )
        self._placeholder.show()
        self._openBtn.show()

    def _video_watchdog(self, token):
        # 一定時間フレームが来なければ埋め込み不可と判断
        if token == self._video_token and self._path and not self._got_frame:
            self._video_unavailable()

    # ── 連番画像（フリップブック） ─────────────────────────
    def set_sequence(self, frames, fps=24):
        self._stop_all()
        self._path = None
        self._frames = list(frames or [])
        self._idx = 0
        self._openBtn.hide()
        if not self._frames:
            self.clear_player()
            return
        self._placeholder.hide()
        self._frameLabel.show()
        self._counter.show()
        self._show_frame(0)
        if len(self._frames) > 1:
            self._timer.start(max(1, int(1000 / max(1, fps))))

    def _show_frame(self, i):
        try:
            pm = QPixmap(self._frames[i])
            if not pm.isNull():
                # レイアウト前で label サイズが未確定(0)のときは横幅を見繕う
                w = self._frameLabel.width()
                h = self._frameLabel.height()
                if w < 10 or h < 10:
                    w = max(self.width() - 8, 240)
                    h = 150
                self._frameLabel.setPixmap(
                    pm.scaled(w, h, Qt.KeepAspectRatio, Qt.FastTransformation)
                )
            self._counter.setText(f"連番再生  {i + 1}/{len(self._frames)}")
        except Exception:
            pass

    def _next_frame(self):
        if not self._frames:
            self._timer.stop()
            return
        self._idx = (self._idx + 1) % len(self._frames)
        self._show_frame(self._idx)

    # ── 動画ファイル（mp4：cv2 でバックグラウンド再生） ─────
    def set_video(self, path):
        """動画ファイルを再生。cv2 があれば埋め込み、無ければ外部ボタン。

        ※ Maya 同梱 Qt の QtMultimedia は再生不可かつ重いため使用しない。
        """
        self._stop_all()
        self._frames = []
        self._counter.hide()
        self._path = path
        self._got_frame = False
        self._video_token += 1
        if not path:
            self.clear_player()
            return
        if _HAS_CV2 and self._start_cv2(path):
            return
        self.set_external(path)

    def set_external(self, path):
        """埋め込み再生せず、外部プレイヤーで開くボタンのみ表示する。"""
        self._stop_all()
        self._path = path
        self._frames = []
        self._counter.hide()
        self._frameLabel.hide()
        self._placeholder.setText(
            "mp4 は埋め込み再生できません（cv2 未導入）。\n"
            "［外部プレイヤーで開く］、または［mp4再生を有効化］で cv2 を導入してください。"
        )
        self._placeholder.show()
        self._openBtn.show()

    # ── cv2 による mp4 再生（別スレッドでデコード→QLabel に描画） ─
    def _start_cv2(self, path):
        try:
            self._placeholder.setText("動画を読み込み中…")
            self._placeholder.show()
            self._openBtn.show()
            self._cv_thread = Cv2VideoThread(path, max_w=640, parent=self)
            self._cv_thread.frameReady.connect(self._paint_image)
            self._cv_thread.start()
            token = self._video_token
            QTimer.singleShot(2500, lambda: self._cv_watchdog(token))
            return True
        except Exception as e:
            print("[OG_Pipeline] cv2 再生エラー:", e)
            return False

    def _cv_watchdog(self, token):
        # 2.5秒待ってもフレームが来なければ開けなかったと判断 → 外部再生
        if token == self._video_token and self._path and not self._got_frame:
            self.set_external(self._path)

    def _open_external(self):
        if self._path:
            open_file_external(self._path)

    def stop(self):
        self._stop_all()


# ─── 全ショット動画一覧（グリッド・自動再生） ───────────────────────────────────
# 工程ごとの色。バッジ・サイドバーのラベルに使う（暗い文字が乗る前提の明るめの色）。
_STAGE_COLOR_MAP = {
    "lay_pri": "#4a90d9",   # 青
    "lay_anm": "#5fb878",   # 緑
    "anm_pri": "#e8a838",   # 琥珀
    "anm_sec": "#d9734a",   # 橙
}
_STAGE_PALETTE = [
    "#4a90d9", "#5fb878", "#e8a838", "#d9734a",
    "#9b6dd6", "#46c4b8", "#d96d9e", "#c5c043",
]


def stage_color(stage):
    """工程名から安定した表示色を返す。既知工程は固定色、未知は名前ハッシュで割当。"""
    if not stage:
        return "#e8a838"
    key = stage.lower()
    if key in _STAGE_COLOR_MAP:
        return _STAGE_COLOR_MAP[key]
    h = sum(ord(c) for c in key)
    return _STAGE_PALETTE[h % len(_STAGE_PALETTE)]


class GridVideoCell(QWidget):
    """グリッド内の1セル。media = pick_folder_media() の結果。

    QtMultimedia は使わない（Maya で再生不可かつ重い）。cv2 動画 / 連番 / 外部のみ。
    操作: ホバーでハイライト / 中ボタンドラッグでスクラブ / 右下の小ボタンで再生停止 /
    下部のボタンで工程フォルダへドリル・エクスプローラーで開く。
    """
    CELL_W, CELL_H = 208, 90

    def __init__(self, title, media, stage="", on_click=None, payload=None,
                 title_color=None, folder=None, on_drill=None,
                 drill_label="⮞ リーブ", parent=None):
        super().__init__(parent)
        self.setFixedWidth(self.CELL_W)
        self.setObjectName("gridCell")
        # QWidget サブクラスはこの属性が無いと QSS の背景/枠（:hover 含む）が描画されない
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setStyleSheet(
            "#gridCell { background: transparent; border: 1px solid transparent;"
            " border-radius: 5px; }"
            "#gridCell:hover { background: #161c2b; border: 1px solid #e8a838; }")
        self._cv_thread = None
        self._frames = []
        self._idx = 0
        self._seq_timer = None
        self._on_click = on_click
        self._payload = payload
        self._on_drill = on_drill
        self._user_paused = False
        self._scrubbing = False
        self._scrub_cap = None
        self._scrub_total = 0
        self._video_path = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        name = QLabel(title)
        name.setStyleSheet("color: %s; font-size: 15px; font-weight: bold;"
                           % (title_color or "#e8c87a"))
        head.addWidget(name, 1)
        if stage:
            badge = QLabel(stage)
            badge.setStyleSheet(
                "color: #0f1117; background: %s; border-radius: 3px;"
                " padding: 2px 9px; font-size: 12px; font-weight: bold;" % stage_color(stage))
            head.addWidget(badge)
        lay.addLayout(head)

        if on_click:
            self.setCursor(Qt.PointingHandCursor)

        self._view = QLabel()
        self._view.setAlignment(Qt.AlignCenter)
        self._view.setFixedHeight(self.CELL_H)
        self._view.setStyleSheet("background: #000; color: #3a4055; border: 1px solid #1e2435;")
        # マウスイベントはセルで一括処理する（スクラブ/選択）。
        self._view.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self._view)

        # 右下の小さな再生/停止ボタン（view の上にオーバーレイ）
        self._toggleBtn = QPushButton("▶", self)
        self._toggleBtn.setCursor(Qt.PointingHandCursor)
        self._toggleBtn.setFixedSize(24, 20)
        self._toggleBtn.setToolTip("再生 / 停止")
        self._toggleBtn.setStyleSheet(
            "QPushButton { background: rgba(15,17,23,190); color: #e8c87a;"
            " border: 1px solid #2a3147; border-radius: 3px; font-size: 12px; padding: 0; }"
            "QPushButton:hover { background: rgba(232,168,56,220); color: #0f1117; }")
        self._toggleBtn.clicked.connect(self._toggle_play)

        self._media = media
        self._playing = False
        kind = media[0] if media else None
        if kind == "video":
            self._view.setText("…")
            self._video_path = media[1]
            sub = Path(media[1]).name
        elif kind == "seq":
            self._frames = media[1]
            self._show_seq(0)            # 先頭フレームだけ静止表示
            sub = "連番 %d 枚" % len(self._frames)
        elif kind == "ext":
            self._view.setText("外部で再生")
            self._video_path = media[1]
            btn = QPushButton("▶  外部で開く")
            btn.setObjectName("refreshBtn")
            btn.clicked.connect(lambda _=False, p=media[1]: open_file_external(p))
            lay.addWidget(btn)
            sub = Path(media[1]).name
        else:
            self._view.setText("動画なし")
            sub = "—"
        s = QLabel(sub)
        s.setStyleSheet("color: #4a5568; font-size: 9px;")
        s.setWordWrap(True)
        lay.addWidget(s)

        # 埋め込み再生できるメディアだけトグルボタンを出す
        if not self._playable():
            self._toggleBtn.hide()

        # 下部ボタン: 工程フォルダへドリル / エクスプローラーで開く（小型）
        if folder:
            brow = QHBoxLayout()
            brow.setContentsMargins(0, 1, 0, 0)
            brow.setSpacing(3)
            mini_btn = ("QPushButton { background: #1a2030; color: #9aa6c0;"
                        " border: 1px solid #2a3147; border-radius: 3px;"
                        " font-size: 10px; padding: 1px 4px; }"
                        "QPushButton:hover { background: #232b40; color: #e8c87a; }")
            # 工程（ドリル）ボタンは on_drill が渡されたときだけ表示する
            if on_drill:
                drillBtn = QPushButton(drill_label)
                drillBtn.setFixedHeight(17)
                drillBtn.setStyleSheet(mini_btn)
                drillBtn.setToolTip("ブラウザでこの工程フォルダを開く")
                drillBtn.clicked.connect(lambda _=False, f=folder: self._do_drill(f))
                brow.addWidget(drillBtn, 1)
            openBtn = QPushButton("📂 開く")
            openBtn.setFixedHeight(17)
            openBtn.setStyleSheet(mini_btn)
            openBtn.setToolTip("エクスプローラーでフォルダを開く")
            openBtn.clicked.connect(lambda _=False, f=folder: reveal_in_explorer(f))
            brow.addWidget(openBtn, 1)
            lay.addLayout(brow)

        QTimer.singleShot(0, self._position_overlay)

    # ── メディア種別 ───────────────────────────────────
    def _playable(self):
        """埋め込み再生（動画 or 連番）できるか。"""
        if not self._media:
            return False
        kind = self._media[0]
        return (kind == "video" and _HAS_CV2) or (kind == "seq" and len(self._frames) > 1)

    def _scrubbable(self):
        if not self._media:
            return False
        kind = self._media[0]
        return (kind == "seq" and len(self._frames) > 1) or \
               (kind in ("video", "ext") and _HAS_CV2 and self._video_path)

    # ── 再生制御（表示中のセルだけ再生して負荷を抑える） ─────
    def play(self):
        # ユーザーが停止中／スクラブ中は自動再生しない
        if self._playing or self._user_paused or self._scrubbing or not self._media:
            return
        kind = self._media[0]
        if kind == "video":
            self._playing = self._start_cv2(self._media[1])
        elif kind == "seq" and len(self._frames) > 1:
            if self._seq_timer is None:
                self._seq_timer = QTimer(self)
                self._seq_timer.timeout.connect(self._next_seq)
            self._seq_timer.start(100)   # 約10fps
            self._playing = True
        self._update_toggle_icon()

    def pause(self):
        self._playing = False
        if self._seq_timer:
            self._seq_timer.stop()
        self._stop_thread()
        self._update_toggle_icon()

    def _toggle_play(self):
        if self._playing:
            self._user_paused = True
            self.pause()
        else:
            self._user_paused = False
            self.play()
        self._update_toggle_icon()

    def _update_toggle_icon(self):
        try:
            self._toggleBtn.setText("⏸" if self._playing else "▶")
        except Exception:
            pass

    def _paint(self, img):
        try:
            if img and not img.isNull():
                self._view.setPixmap(QPixmap.fromImage(img).scaled(
                    self.CELL_W - 8, self.CELL_H, Qt.KeepAspectRatio, Qt.FastTransformation))
        except Exception:
            pass

    # 連番
    def _show_seq(self, i):
        try:
            pm = QPixmap(self._frames[i])
            if not pm.isNull():
                self._view.setPixmap(pm.scaled(self.CELL_W - 8, self.CELL_H,
                                               Qt.KeepAspectRatio, Qt.FastTransformation))
        except Exception:
            pass

    def _next_seq(self):
        if not self._frames:
            return
        self._idx = (self._idx + 1) % len(self._frames)
        self._show_seq(self._idx)

    # cv2 動画（別スレッドでデコード）
    def _start_cv2(self, path):
        try:
            self._cv_thread = Cv2VideoThread(path, max_w=self.CELL_W, parent=self)
            self._cv_thread.frameReady.connect(self._paint)
            self._cv_thread.start()
            return True
        except Exception:
            self._cv_thread = None
            return False

    def _stop_thread(self):
        if self._cv_thread is not None:
            _release_cv_thread(self._cv_thread)
            self._cv_thread = None

    def stop(self):
        self.pause()
        self._release_scrub_cap()

    def _do_drill(self, folder):
        if self._on_drill:
            try:
                self._on_drill(folder)
            except Exception:
                pass

    # ── オーバーレイ（再生/停止ボタン）の配置 ───────────────
    def _position_overlay(self):
        try:
            g = self._view.geometry()
            bw, bh = self._toggleBtn.width(), self._toggleBtn.height()
            self._toggleBtn.move(g.right() - bw - 6, g.bottom() - bh - 6)
            self._toggleBtn.raise_()
        except Exception:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_overlay()

    # ── 中ボタンスクラブ ───────────────────────────────
    def _begin_scrub(self):
        self._scrubbing = True
        # 自動再生を止める（ユーザー停止フラグは触らない）
        self._playing = False
        if self._seq_timer:
            self._seq_timer.stop()
        self._stop_thread()

    def _end_scrub(self):
        self._scrubbing = False
        self._release_scrub_cap()
        # スクラブ前に再生していた状態へ戻す（ユーザー停止中・非表示なら戻さない）
        if not self._user_paused:
            try:
                visible = not self.visibleRegion().isEmpty()
            except Exception:
                visible = True
            if visible:
                self.play()

    def _scrub_to(self, x_in_cell):
        g = self._view.geometry()
        w = max(1, g.width())
        frac = min(0.9999, max(0.0, (x_in_cell - g.x()) / float(w)))
        if self._frames:
            i = int(frac * len(self._frames))
            self._show_seq(min(i, len(self._frames) - 1))
        elif self._video_path and _HAS_CV2:
            cap = self._ensure_scrub_cap()
            if cap is not None and self._scrub_total > 0:
                try:
                    import cv2
                    target = int(frac * self._scrub_total)
                    cap.set(cv2.CAP_PROP_POS_FRAMES, target)
                    ok, frame = cap.read()
                    if ok:
                        self._show_cv_frame(frame)
                except Exception:
                    pass

    def _ensure_scrub_cap(self):
        if self._scrub_cap is not None:
            return self._scrub_cap
        try:
            import cv2
            cap = cv2.VideoCapture(self._video_path)
            if not cap.isOpened():
                cap.release()
                return None
            self._scrub_cap = cap
            self._scrub_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
            return cap
        except Exception:
            return None

    def _release_scrub_cap(self):
        if self._scrub_cap is not None:
            try:
                self._scrub_cap.release()
            except Exception:
                pass
        self._scrub_cap = None
        self._scrub_total = 0

    def _show_cv_frame(self, frame):
        try:
            import cv2
            h, w = frame.shape[:2]
            mw = self.CELL_W
            if w > mw:
                nh = max(1, int(h * mw / float(w)))
                frame = cv2.resize(frame, (mw, nh))
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            hh, ww = rgb.shape[:2]
            img = QImage(rgb.data, ww, hh, 3 * ww, QImage.Format_RGB888).copy()
            self._paint(img)
        except Exception:
            pass

    # ── マウス操作 ─────────────────────────────────────
    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton and self._scrubbable():
            self._begin_scrub()
            self._scrub_to(event.pos().x())
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._on_click:
            try:
                self._on_click(self._payload)
            except Exception:
                pass
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._scrubbing:
            self._scrub_to(event.pos().x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._scrubbing and event.button() == Qt.MiddleButton:
            self._end_scrub()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class AllShotsDialog(QDialog):
    """全ショットの最新動画をグリッドで一覧（工程バッジ・工程ソート）。

    タイル選択で、右サイドバーにそのショットの工程ごとの最新動画を表示する。
    """
    COLS = 5

    def __init__(self, shots_parent, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Window)
        self.setWindowTitle("All Shots — 最新動画一覧")
        self.setMinimumSize(1200, 640)
        # グリッドに 5 列が収まり、5 行ぶんが見えるサイズで開く
        self.resize(1480, 880)
        self.setStyleSheet(STYLE)
        self._shots_parent = shots_parent
        self._sort_mode = "shot"
        self._cells = []        # グリッド（ショット）タイル
        self._side_cells = []   # サイドバー（工程）タイル

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── ヘッダー（タイトル＋ソート） ──
        hbar = QWidget()
        hbar.setObjectName("toolbar")
        hl = QHBoxLayout(hbar)
        hl.setContentsMargins(10, 6, 10, 6)
        title = QLabel("◈  ALL SHOTS")
        title.setObjectName("appTitle")
        title.setStyleSheet("font-size: 14px; color: #e8a838; letter-spacing: 2px;")
        hl.addWidget(title)
        hl.addStretch()
        hl.addWidget(QLabel("並び替え:"))
        self._sortCombo = QComboBox()
        self._sortCombo.addItems(["ショット名", "工程"])
        self._sortCombo.currentTextChanged.connect(self._on_sort_changed)
        hl.addWidget(self._sortCombo)
        outer.addWidget(hbar)

        # ── 本体（左:グリッド / 右:工程サイドバー） ──
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        self._grid_content = QWidget()
        self._grid = QGridLayout(self._grid_content)
        self._grid.setContentsMargins(10, 10, 10, 10)
        self._grid.setSpacing(10)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._grid_content)
        self._scroll.verticalScrollBar().valueChanged.connect(self._update_visible)
        splitter.addWidget(self._scroll)

        side = QWidget()
        side.setObjectName("detailPanel")
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(0)
        self._sideTitle = QLabel("◈  工程別（タイルを選択）")
        self._sideTitle.setObjectName("detailTitle")
        sv.addWidget(self._sideTitle)
        self._side_content = QWidget()
        self._side_layout = QVBoxLayout(self._side_content)
        self._side_layout.setContentsMargins(10, 10, 10, 10)
        self._side_layout.setSpacing(10)
        self._side_layout.addStretch()
        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setWidget(self._side_content)
        side_scroll.verticalScrollBar().valueChanged.connect(self._update_visible)
        sv.addWidget(side_scroll, 1)
        splitter.addWidget(side)
        # グリッド側に 5 列ぶん（約 1130px）を割り当てる
        splitter.setSizes([1130, 330])
        outer.addWidget(splitter, 1)

        self._foot = QLabel("")
        self._foot.setStyleSheet("color: #3a4055; font-size: 10px; padding: 4px 10px;")
        outer.addWidget(self._foot)

        # ── ショットデータ収集（各ショット = 最新工程のメディア＋工程名） ──
        self._shot_data = []   # [{name, folder, media, stage, stage_folder}]
        try:
            names = sorted(os.listdir(shots_parent))
        except Exception:
            names = []
        for d in names:
            full = os.path.join(shots_parent, d)
            if not os.path.isdir(full):
                continue
            stages = shot_stage_list(full)
            if stages:
                stage_name, media, _mt = stages[-1]   # 最新工程
            else:
                media, stage_name = pick_folder_media(full), ""
            if media:
                stage_folder = os.path.join(full, stage_name) if stage_name else full
                self._shot_data.append(
                    {"name": d, "folder": full, "media": media,
                     "stage": stage_name, "stage_folder": stage_folder})

        self._foot.setText(
            f"動画あり {len(self._shot_data)} / {len(names)} ショット　"
            "（表示中のみ再生／タイル選択で工程別を表示）")
        self._rebuild_grid()

    # ── グリッド構築・ソート ────────────────────────────
    def _on_sort_changed(self, text):
        self._sort_mode = "stage" if text == "工程" else "shot"
        self._rebuild_grid()

    def _clear_cells(self, cells, layout):
        for cell in cells:
            try:
                cell.stop()
                cell.setParent(None)
                cell.deleteLater()
            except Exception:
                pass
        del cells[:]

    def _rebuild_grid(self):
        self._clear_cells(self._cells, self._grid)
        data = list(self._shot_data)
        if self._sort_mode == "stage":
            data.sort(key=lambda s: (s["stage"].lower(), s["name"].lower()))
        else:
            data.sort(key=lambda s: s["name"].lower())
        r = c = 0
        for s in data:
            # グリッドのタイルは下部ボタンなし（リーブ・開くともサイドバー側に集約）
            cell = GridVideoCell(s["name"], s["media"], stage=s["stage"],
                                 on_click=self._select_shot, payload=s["folder"],
                                 folder=None, on_drill=None,
                                 parent=self._grid_content)
            self._grid.addWidget(cell, r, c, Qt.AlignLeft | Qt.AlignTop)
            self._cells.append(cell)
            c += 1
            if c >= self.COLS:
                c = 0
                r += 1
        # 余ったスペースを右・下へ逃がして、タイルを左上詰めにする
        self._grid.setColumnStretch(self.COLS, 1)
        self._grid.setRowStretch(r + 1, 1)
        QTimer.singleShot(0, self._update_visible)

    # ── 工程別サイドバー ───────────────────────────────
    def _select_shot(self, folder):
        self._clear_cells(self._side_cells, self._side_layout)
        # 末尾の stretch を取り除いてから積み直す
        while self._side_layout.count():
            it = self._side_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._sideTitle.setText(f"◈  {Path(folder).name} — 工程別")
        stages = shot_stage_list(folder)
        if not stages:
            self._side_layout.addWidget(QLabel("工程フォルダに動画が見つかりません"))
        for stage_name, media, _mt in reversed(stages):   # 新しい工程を上に
            cell = GridVideoCell(stage_name, media, title_color=stage_color(stage_name),
                                 folder=os.path.join(folder, stage_name),
                                 on_drill=self._drill_to, parent=self._side_content)
            self._side_layout.addWidget(cell)
            self._side_cells.append(cell)
        self._side_layout.addStretch()
        QTimer.singleShot(0, self._update_visible)

    def _drill_to(self, folder):
        """親（メインウィンドウ）のブラウザでこの工程フォルダを開く。"""
        win = self.parent()
        if win is not None and hasattr(win, "reveal_in_browser"):
            win.reveal_in_browser(folder)

    # ── 表示中のみ再生 ─────────────────────────────────
    def _all_cells(self):
        return self._cells + self._side_cells

    def _update_visible(self, *args):
        if not self.isVisible():
            return
        for cell in self._all_cells():
            try:
                visible = not cell.visibleRegion().isEmpty()
            except Exception:
                visible = True
            cell.play() if visible else cell.pause()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._update_visible)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_visible()

    def changeEvent(self, event):
        try:
            relevant = event.type() in (event.WindowStateChange, event.ActivationChange)
        except Exception:
            relevant = False
        super().changeEvent(event)
        if relevant:
            if self.isActiveWindow() and not self.isMinimized():
                self._update_visible()
            else:
                for cell in self._all_cells():
                    cell.pause()

    def hideEvent(self, event):
        for cell in self._all_cells():
            cell.pause()
        super().hideEvent(event)

    def stop_all(self):
        for cell in self._all_cells():
            cell.stop()

    def closeEvent(self, event):
        self.stop_all()
        super().closeEvent(event)


# ─── 詳細パネル ──────────────────────────────────────────────────────────────
class DetailPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("detailPanel")
        self.setMinimumWidth(240)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel("◈  FILE DETAILS")
        title.setObjectName("detailTitle")
        layout.addWidget(title)

        # 動画プレイヤー（シーンと同名のプレイブラストを再生）
        self._abs_path = ""
        self._shot_folder = ""
        self.video = VideoPlayer()
        vwrap = QWidget()
        vlay = QVBoxLayout(vwrap)
        vlay.setContentsMargins(12, 10, 12, 6)
        vlay.addWidget(self.video)
        layout.addWidget(vwrap)

        self.content = QWidget()
        self.content.setStyleSheet("background: transparent;")
        self.contentLayout = QVBoxLayout(self.content)
        self.contentLayout.setContentsMargins(16, 12, 16, 12)
        self.contentLayout.setSpacing(8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.content)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        layout.addWidget(scroll, 1)

        self.clear()

    def _clear_layout(self):
        while self.contentLayout.count():
            item = self.contentLayout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _show_media(self, abs_path):
        """連番画像があればフリップブック再生、無ければ単一動画、どちらも無ければクリア。"""
        seq = find_scene_sequence(abs_path)
        if seq:
            self.video.set_sequence(seq)
        else:
            self.video.set_video(find_scene_video(abs_path))

    def clear(self):
        self._clear_layout()
        self._abs_path = ""
        self._shot_folder = ""
        if hasattr(self, "video"):
            self.video.clear_player()
        placeholder = QLabel("ファイルを選択すると\n詳細が表示されます")
        placeholder.setStyleSheet("color: #2a3045; font-size: 11px;")
        placeholder.setAlignment(Qt.AlignCenter)
        self.contentLayout.addWidget(placeholder)
        self.contentLayout.addStretch()

    def reload_video(self):
        """現在表示中の動画／連番を再探索して反映（プレイブラスト直後など）。"""
        if self._shot_folder:
            self.show_folder_video(self._shot_folder)
        elif self._abs_path:
            self._show_media(self._abs_path)

    def show_folder_video(self, folder):
        """選択フォルダ（ショット／工程）配下の最新メディアをサイドバーで再生する。

        cv2 があれば動画(mp4)を、無ければ Pipeline_Movie の連番を再生する。
        """
        self._abs_path = ""
        self._shot_folder = folder
        self._clear_layout()
        name = Path(folder).name
        media = pick_folder_media(folder)
        sub_text = "このフォルダに動画／連番はありません"
        if hasattr(self, "video"):
            if media is None:
                self.video.clear_player()
            elif media[0] == "video":
                self.video.set_video(media[1])
                sub_text = f"最新動画: {Path(media[1]).name}"
            elif media[0] == "seq":
                self.video.set_sequence(media[1])
                sub_text = f"最新の連番（{len(media[1])} 枚）"
            else:  # "ext"
                self.video.set_external(media[1])
                sub_text = f"動画: {Path(media[1]).name}（外部再生）"

        title = QLabel(f"📁  {name}")
        title.setObjectName("detailFilename")
        title.setWordWrap(True)
        self.contentLayout.addWidget(title)
        sub = QLabel(sub_text)
        sub.setObjectName("detailValue")
        sub.setWordWrap(True)
        self.contentLayout.addWidget(sub)
        self.contentLayout.addStretch()

    def update_info(self, rel_path: str, abs_path: str, size: int, mtime: float):
        self._abs_path = abs_path
        self._shot_folder = ""
        if hasattr(self, "video"):
            self._show_media(abs_path)
        self._clear_layout()
        import datetime

        p = Path(abs_path)
        ext = p.suffix.lower()
        size_str = self._fmt_size(size)
        mtime_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d  %H:%M")

        fn_label = QLabel(p.name)
        fn_label.setObjectName("detailFilename")
        fn_label.setWordWrap(True)
        self.contentLayout.addWidget(fn_label)

        path_label = QLabel(str(Path(rel_path).parent))
        path_label.setObjectName("detailPath")
        path_label.setWordWrap(True)
        self.contentLayout.addWidget(path_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #1e2435; margin: 4px 0;")
        self.contentLayout.addWidget(sep)

        type_color = "#e8a838" if ext == ".ma" else "#4a9eff"
        type_label = QLabel(f"  {ext.upper()}  ")
        type_label.setStyleSheet(
            f"color: {type_color}; border: 1px solid {type_color}; "
            f"padding: 2px 6px; font-size: 11px; letter-spacing: 1px;"
        )
        type_label.setFixedWidth(60)
        self.contentLayout.addWidget(type_label)
        self.contentLayout.addSpacing(4)

        for key, val in [("SIZE", size_str), ("MODIFIED", mtime_str)]:
            row = QHBoxLayout()
            k = QLabel(key)
            k.setObjectName("detailKey")
            k.setFixedWidth(72)
            v = QLabel(val)
            v.setObjectName("detailValue")
            row.addWidget(k)
            row.addWidget(v)
            self.contentLayout.addLayout(row)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet("color: #1e2435; margin: 4px 0;")
        self.contentLayout.addWidget(sep2)

        fp_key = QLabel("FULL PATH")
        fp_key.setObjectName("detailKey")
        self.contentLayout.addWidget(fp_key)
        fp_val = QLabel(abs_path)
        fp_val.setObjectName("detailPath")
        fp_val.setWordWrap(True)
        self.contentLayout.addWidget(fp_val)

        self.contentLayout.addStretch()

    @staticmethod
    def _fmt_size(size: int) -> str:
        s = float(size)
        for unit in ["B", "KB", "MB", "GB"]:
            if s < 1024:
                return f"{s:.1f} {unit}"
            s /= 1024
        return f"{s:.1f} TB"


# ─── カラムブラウザ（Finder ライクな横並びパネル） ───────────────────────────────
class ColumnBrowser(QWidget):
    """
    フォルダの潜り込みをドリルダウン（ツリー展開）ではなく、
    選択するたびに右へカラムを追加していく Miller カラム方式で表示する。
    """
    file_selected = Signal(object)   # 選択ファイル情報 dict、解除時は None
    file_activated = Signal(str)     # ダブルクリックで開く（絶対パス）
    context_requested = Signal(str, object)  # 右クリック: (絶対パス, グローバル座標)
    folder_selected = Signal(str)    # フォルダ選択: 絶対パス

    COL_WIDTH = 240        # 既定（最小）幅
    COL_MIN_WIDTH = 200    # カラムの下限幅
    COL_MAX_WIDTH = 640    # カラムの上限幅（これを超える場合はツールチップで全文表示）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.root = None
        self.ext_filter = None       # None / ".ma" / ".mb"
        # 検索フィルタ: None なら通常ブラウズ。set のときはヒットにつながる
        # フォルダ／ファイルだけをカラムに表示する。
        self._allowed_files = None   # set[str] (normpath)
        self._allowed_dirs = None    # set[str] (normpath)
        self._columns = []
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("columnScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self.container = QWidget()
        self.hbox = QHBoxLayout(self.container)
        self.hbox.setContentsMargins(0, 0, 0, 0)
        self.hbox.setSpacing(0)
        # 末尾スペーサー: カラムを常に左詰めにし、余白は右側に逃がす。
        # （これが無いと余白が配分されてカラムが右に寄る）
        self.hbox.addStretch(1)
        self.scroll.setWidget(self.container)
        outer.addWidget(self.scroll)

    # ── 公開 API ──────────────────────────────────────────────
    def set_root(self, path):
        """通常ブラウズ（検索フィルタ解除）。"""
        self.root = Path(path) if path else None
        self._allowed_files = None
        self._allowed_dirs = None
        self.refresh()

    def set_ext_filter(self, ext):
        self.ext_filter = ext
        self.refresh()

    def apply_search_filter(self, files, dirs):
        """検索ヒット集合でカラムを絞り込む。表示形式は通常ドリルと同じ。

        files / dirs は normpath 済みの絶対パス集合。
        dirs にはヒットの祖先フォルダ（ルートまで）を含める。
        """
        self._allowed_files = files
        self._allowed_dirs = dirs
        self.refresh()

    def refresh(self):
        self._clear_columns()
        if self.root and self.root.exists():
            self._add_column(self.root)

    def reveal_path(self, target):
        """root から target（ファイル/フォルダ）までカラムを展開し、末尾を選択する。

        戻り値: 到達できれば True。target が root 配下に無ければ False。
        """
        if not self.root:
            return False
        target = Path(target)
        try:
            rel = target.relative_to(self.root)
        except ValueError:
            return False  # ルート配下ではない

        # 検索フィルタは解除して通常表示で潜る
        self._allowed_files = None
        self._allowed_dirs = None
        self._clear_columns()
        self._add_column(self.root)

        for part in rel.parts:
            lw = self._columns[-1]
            item = self._find_item(lw, part)
            if item is None:
                return False
            lw.setCurrentItem(item)
            item.setSelected(True)
            kind, path = item.data(Qt.UserRole)
            if kind == "dir":
                self._add_column(Path(path))
            else:
                self.file_selected.emit(self._file_info(path))
                break

        # 末尾カラムが見えるよう右へスクロール
        if self._columns:
            last = self._columns[-1]._container
            QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(last))
        return True

    @staticmethod
    def _find_item(lw, name):
        """カラム内で、保存パスの末尾名が name に一致する項目を返す（大文字小文字無視）。"""
        target = name.lower()
        for i in range(lw.count()):
            it = lw.item(i)
            data = it.data(Qt.UserRole)
            if data and Path(data[1]).name.lower() == target:
                return it
        return None

    # ── 内部処理 ──────────────────────────────────────────────
    def _clear_columns(self):
        for w in self._columns:
            w._container.setParent(None)
            w._container.deleteLater()
        self._columns = []
        self._update_width()

    def _make_column(self, title, width=None):
        """ヘッダー（親フォルダ名）＋リストの複合カラムを作る。返すのはリスト本体。"""
        container = QWidget()
        container.setObjectName("browserCol")
        container.setFixedWidth(width or self.COL_WIDTH)
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QLabel(title or "/")
        header.setObjectName("browserColHeader")
        header.setFixedHeight(24)
        header.setToolTip(title or "")
        v.addWidget(header)

        lw = QListWidget()
        lw.setObjectName("browserColumn")
        # フォントを明示設定し、幅計測(fontMetrics)と実描画を一致させる
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        f.setPixelSize(12)
        lw.setFont(f)
        lw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lw.setTextElideMode(Qt.ElideNone)   # 名前を「…」で省略しない
        lw.itemClicked.connect(lambda item, w=lw: self._on_clicked(w, item))
        lw.itemDoubleClicked.connect(lambda item, w=lw: self._on_double(w, item))
        lw.setContextMenuPolicy(Qt.CustomContextMenu)
        lw.customContextMenuRequested.connect(lambda pos, w=lw: self._on_context(w, pos))
        v.addWidget(lw, 1)

        lw._container = container   # クリック処理はリスト本体を参照、レイアウトは container
        return lw

    def _on_context(self, lw, pos):
        """ファイル項目を右クリックしたら、絶対パスとグローバル座標を通知する。"""
        item = lw.itemAt(pos)
        if item is None:
            return
        data = item.data(Qt.UserRole)
        if data and data[0] == "file":
            self.context_requested.emit(data[1], lw.viewport().mapToGlobal(pos))

    def _list_dir(self, dir_path):
        dirs, files = [], []
        filtering = self._allowed_files is not None
        try:
            with os.scandir(str(dir_path)) as it:
                for entry in it:
                    try:
                        full = os.path.normpath(os.path.join(str(dir_path), entry.name))
                        if entry.is_dir():
                            # 検索中は、ヒットへつながるフォルダだけを表示する
                            if filtering and full not in self._allowed_dirs:
                                continue
                            dirs.append(entry.name)
                        elif entry.is_file():
                            suf = Path(entry.name).suffix.lower()
                            if suf in MAYA_EXTENSIONS:
                                if self.ext_filter and suf != self.ext_filter:
                                    continue
                                if filtering and full not in self._allowed_files:
                                    continue
                                files.append(entry.name)
                    except Exception:
                        pass
        except Exception:
            pass
        dirs.sort(key=str.lower)
        files.sort(key=str.lower)
        return dirs, files

    def _add_column(self, dir_path):
        dir_path = Path(dir_path)
        # ヘッダーにはこのカラムが表示している（＝項目の親）フォルダ名を出す。
        title = dir_path.name or str(dir_path)
        lw = self._make_column(title)
        dirs, files = self._list_dir(dir_path)
        for name in dirs:
            it = QListWidgetItem(f"📁  {name}")
            it.setData(Qt.UserRole, ("dir", str(Path(dir_path) / name)))
            it.setForeground(QColor("#cdb27a"))
            it.setToolTip(name)
            lw.addItem(it)
        for name in files:
            p = Path(dir_path) / name
            ext = p.suffix.lower()
            it = QListWidgetItem(f"    {name}")
            it.setData(Qt.UserRole, ("file", str(p)))
            it.setForeground(QColor("#e8a838" if ext == ".ma" else "#4a9eff"))
            it.setToolTip(name)
            lw.addItem(it)
        # 中身（最長の名前）に合わせてカラム幅を決める → 長い名前が切れない。
        self._fit_column(lw, title)
        # 末尾スペーサーの手前に挿入して、カラムを左詰めで右へ伸ばしていく。
        self.hbox.insertWidget(len(self._columns), lw._container)
        self._columns.append(lw)
        self._update_width()

    def _fit_column(self, lw, title=""):
        """カラム幅を最長項目（とヘッダー）に合わせる。上限を超えたらクランプ。"""
        fm = lw.fontMetrics()

        def text_w(s):
            try:
                return fm.horizontalAdvance(s)   # Qt 5.11+
            except AttributeError:
                return fm.boundingRect(s).width()

        w = text_w(title)
        for i in range(lw.count()):
            w = max(w, text_w(lw.item(i).text()))
        w += 40   # 左パディング・選択枠・スクロールバー等の余白
        w = max(self.COL_MIN_WIDTH, min(int(w), self.COL_MAX_WIDTH))
        lw._container.setFixedWidth(w)

    def _trim_after(self, lw):
        """lw より右のカラムをすべて取り除く。"""
        try:
            idx = self._columns.index(lw)
        except ValueError:
            return
        while len(self._columns) > idx + 1:
            w = self._columns.pop()
            w._container.setParent(None)
            w._container.deleteLater()
        self._update_width()

    def _on_clicked(self, lw, item):
        self._trim_after(lw)
        kind, path = item.data(Qt.UserRole)
        if kind == "dir":
            self.folder_selected.emit(path)
            self._add_column(Path(path))
        else:
            self.file_selected.emit(self._file_info(path))

    def _on_double(self, lw, item):
        kind, path = item.data(Qt.UserRole)
        if kind == "file":
            self.file_activated.emit(path)

    def _file_info(self, path):
        p = Path(path)
        try:
            st = p.stat()
            rel = str(p.relative_to(self.root)) if self.root else p.name
            return {"rel": rel, "abs": str(p), "size": st.st_size, "mtime": st.st_mtime}
        except Exception:
            return {"rel": p.name, "abs": str(p), "size": 0, "mtime": 0.0}

    def _update_width(self):
        # setFixedWidth 済みなので minimumWidth が確定幅。実寸(width)はレイアウト前だと
        # 未確定なため使わない。総幅をコンテナ最小幅にして、枠を超えたら水平スクロールさせる。
        total = sum(w._container.minimumWidth() for w in self._columns)
        self.container.setMinimumWidth(max(1, total))


# ─── リファレンス編集ダイアログ（Reference Editor 風） ──────────────────────────
REF_DIALOG_STYLE = """
QDialog { background:#0f1117; color:#c8ccd4; font-family:"Consolas",monospace; font-size:12px; }
#refTitle { color:#e8a838; font-size:13px; font-weight:bold; letter-spacing:2px;
            padding:10px 12px; background:#141824; border-bottom:2px solid #e8a838; }
#refSub { color:#3a4a6a; font-size:10px; padding:4px 12px; }
#refHeadRow { background:#141824; border-bottom:1px solid #2a3045; }
#refHead { color:#e8a838; font-size:11px; letter-spacing:1px; padding:4px 6px; }
#refNode { color:#e8c87a; }
#refNs { color:#4a9eff; }
#refType { color:#3dcfb8; }
QLineEdit { background:#1a1f2e; border:1px solid #2a3045; border-radius:3px;
            color:#c8ccd4; padding:4px 6px; }
QLineEdit:focus { border-color:#e8a838; }
QPushButton { background:#1a1f2e; color:#c8ccd4; border:1px solid #2a3045;
              border-radius:3px; padding:4px 10px; }
QPushButton:hover { border-color:#4a9eff; color:#4a9eff; }
QScrollArea { border:none; }
"""


class ReferenceEditDialog(QDialog):
    """シーンを開かずに .ma のリファレンスを編集する（Reference Editor 風の表表示）。

    refinfos: [{'path','namespace','refnode','type','unloaded'}, ...]
    各行: 選択 / Reference Node / Namespace / Unload / Type / File Path / 参照 / Remove
    """
    def __init__(self, file_path, refinfos, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Reference Editor — " + Path(file_path).name)
        self.setMinimumSize(900, 480)
        self.setStyleSheet(REF_DIALOG_STYLE)
        self._rows = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        title = QLabel("◈  REFERENCE EDITOR")
        title.setObjectName("refTitle")
        outer.addWidget(title)
        sub = QLabel(f"{file_path}    —    {len(refinfos)} references")
        sub.setObjectName("refSub")
        sub.setWordWrap(True)
        outer.addWidget(sub)

        # 一括置換バー（選択行のパス内文字列を置換。未選択なら全行）
        repl_bar = QWidget()
        rb = QHBoxLayout(repl_bar)
        rb.setContentsMargins(10, 6, 10, 6)
        rb.setSpacing(6)
        sel_all = QPushButton("全選択/解除")
        sel_all.clicked.connect(self._toggle_select_all)
        rb.addWidget(sel_all)
        rb.addWidget(QLabel("パス置換:"))
        self._findEdit = QLineEdit()
        self._findEdit.setPlaceholderText("検索（例: D:/Animation）")
        self._findEdit.returnPressed.connect(self._apply_replace)
        self._replEdit = QLineEdit()
        self._replEdit.setPlaceholderText("置換後（例: N:/Animation）")
        self._replEdit.returnPressed.connect(self._apply_replace)
        apply_btn = QPushButton("選択行に置換")
        apply_btn.setAutoDefault(False)
        apply_btn.clicked.connect(self._apply_replace)
        rb.addWidget(self._findEdit, 1)
        rb.addWidget(QLabel("→"))
        rb.addWidget(self._replEdit, 1)
        rb.addWidget(apply_btn)
        outer.addWidget(repl_bar)

        cols = ["", "Reference Node", "Namespace", "Load", "Type", "File Path", "", "Remove"]
        widths = [28, 150, 110, 48, 44, 0, 64, 60]

        # 列ヘッダ
        head = QWidget()
        head.setObjectName("refHeadRow")
        hg = QGridLayout(head)
        hg.setContentsMargins(10, 0, 10, 0)
        hg.setHorizontalSpacing(8)
        for c, h in enumerate(cols):
            lab = QLabel(h)
            lab.setObjectName("refHead")
            hg.addWidget(lab, 0, c)
        for c, w in enumerate(widths):
            if w:
                hg.setColumnMinimumWidth(c, w)
        hg.setColumnStretch(5, 1)
        outer.addWidget(head)

        # 行（スクロール内）
        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(10, 6, 10, 6)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        for c, w in enumerate(widths):
            if w:
                grid.setColumnMinimumWidth(c, w)
        grid.setColumnStretch(5, 1)

        for r, info in enumerate(refinfos):
            sel = QCheckBox()
            sel.setToolTip("一括置換の対象に含める")
            node = QLabel(info.get("refnode") or "—")
            node.setObjectName("refNode")
            ns_edit = QLineEdit(info.get("namespace", ""))
            ns_edit.setObjectName("refNs")
            ns_edit.setToolTip("ネームスペース（-ns）。書き換え可能")
            load_cb = QCheckBox()
            load_cb.setChecked(not bool(info.get("unloaded")))
            load_cb.setToolTip("Maya の Reference Editor と同じ：チェック＝ロード／外す＝アンロード")
            typ = QLabel(self._short_type(info.get("type", "")))
            typ.setObjectName("refType")
            path_edit = QLineEdit(info["path"])
            path_edit.setToolTip(info["path"])
            browse = QPushButton("参照…")
            browse.setFixedWidth(64)
            browse.setAutoDefault(False)
            browse.clicked.connect(lambda _=False, e=path_edit: self._browse(e))
            remove = QCheckBox()
            remove.setToolTip("チェックで保存時にこのリファレンスを削除")
            remove.toggled.connect(lambda checked, pe=path_edit, ne=ns_edit:
                                   (pe.setEnabled(not checked), ne.setEnabled(not checked)))
            grid.addWidget(sel, r, 0)
            grid.addWidget(node, r, 1)
            grid.addWidget(ns_edit, r, 2)
            grid.addWidget(load_cb, r, 3, Qt.AlignCenter)
            grid.addWidget(typ, r, 4)
            grid.addWidget(path_edit, r, 5)
            grid.addWidget(browse, r, 6)
            grid.addWidget(remove, r, 7, Qt.AlignCenter)
            self._rows.append({"info": info, "sel": sel, "ns": ns_edit,
                               "load": load_cb, "path": path_edit, "remove": remove})
        grid.setRowStretch(len(refinfos), 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        foot = QWidget()
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(10, 6, 10, 10)
        hint = QLabel("シーンは開かずに .ma を直接書き換えます（保存時にバックアップを作成）")
        hint.setObjectName("refSub")
        fl.addWidget(hint, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        for b in (buttons.button(QDialogButtonBox.Save), buttons.button(QDialogButtonBox.Cancel)):
            if b:
                b.setAutoDefault(False)   # Enter でダイアログが閉じないように
                b.setDefault(False)
        save_btn = buttons.button(QDialogButtonBox.Save)
        if save_btn:
            save_btn.setText("Replace（保存）")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        fl.addWidget(buttons)
        outer.addWidget(foot)

    def keyPressEvent(self, event):
        # Enter/Return ではダイアログを閉じない（誤操作防止）
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)

    @staticmethod
    def _short_type(t):
        return {"mayaAscii": ".ma", "mayaBinary": ".mb"}.get(t, t or "")

    def _browse(self, edit):
        start = os.path.dirname(edit.text()) or str(Path.home())
        fp, _ = QFileDialog.getOpenFileName(
            self, "リファレンス先を選択", start, "Maya Files (*.ma *.mb);;All Files (*)"
        )
        if fp:
            edit.setText(fp)

    def _toggle_select_all(self):
        new_state = not all(r["sel"].isChecked() for r in self._rows) if self._rows else True
        for r in self._rows:
            r["sel"].setChecked(new_state)

    def _apply_replace(self):
        """選択行（無ければ全行）のパス欄で、検索文字列を置換文字列に置き換える。"""
        find = self._findEdit.text()
        if not find:
            return
        repl = self._replEdit.text()
        targets = [r for r in self._rows if r["sel"].isChecked()] or list(self._rows)
        changed = 0
        for r in targets:
            cur = r["path"].text()
            new = cur.replace(find, repl)
            if new != cur:
                r["path"].setText(new)
                changed += 1
        self.setWindowTitle(f"Reference Editor — {changed} 件のパスを置換")

    def changes(self):
        """変更（パス/ネームスペース/アンロード/削除）のあった参照のリストを返す。

        各要素: {refnode, old_path, new_path, old_ns, new_ns,
                 old_unload, new_unload, remove}
        """
        out = []
        for r in self._rows:
            info = r["info"]
            old_path = info["path"]
            old_ns = info.get("namespace", "")
            old_unload = bool(info.get("unloaded"))
            new_path = r["path"].text().strip()
            new_ns = r["ns"].text().strip()
            new_unload = not r["load"].isChecked()   # Load チェック→ロード, 外す→アンロード
            remove = r["remove"].isChecked()
            if remove or (new_path and new_path != old_path) or (new_ns != old_ns) \
                    or (new_unload != old_unload):
                out.append({
                    "refnode": info.get("refnode", ""),
                    "old_path": old_path,
                    "new_path": new_path or old_path,
                    "old_ns": old_ns,
                    "new_ns": new_ns,
                    "old_unload": old_unload,
                    "new_unload": new_unload,
                    "remove": remove,
                })
        return out

    def remove_count(self):
        return sum(1 for r in self._rows if r["remove"].isChecked())


# ─── 環境設定ダイアログ ───────────────────────────────────────────────────────
class SettingsDialog(QDialog):
    """書き出し方式・保存時の自動更新・自動更新の最小間隔を設定する。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("環境設定")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(440)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(12)

        title = QLabel("◈  環境設定")
        title.setStyleSheet("font-size: 14px; color: #e8a838; letter-spacing: 1px;")
        outer.addWidget(title)

        form = QFormLayout()
        form.setSpacing(10)

        # 書き出し方式
        self.methodCombo = QComboBox()
        self.methodCombo.addItem("プレイブラスト（現在のビューを撮影）", "playblast")
        self.methodCombo.addItem("ハードウェア（別プロセスで裏で書き出し）", "hardware")
        idx = self.methodCombo.findData(get_export_method())
        if idx >= 0:
            self.methodCombo.setCurrentIndex(idx)
        form.addRow("動画の書き出し方式:", self.methodCombo)

        # 保存のたびに自動更新
        self.autoCheck = QCheckBox("シーンを保存するたびに動画を更新する")
        self.autoCheck.setChecked(get_auto_export_on_save())
        form.addRow("自動更新:", self.autoCheck)

        # 最小間隔（分）
        self.intervalSpin = QSpinBox()
        self.intervalSpin.setRange(0, 600)
        self.intervalSpin.setSuffix(" 分")
        self.intervalSpin.setValue(get_auto_export_interval_min())
        self.intervalSpin.setToolTip(
            "前回の動画更新からこの分数以上経過しているときだけ書き出します（0=毎回）。")
        form.addRow("最小間隔:", self.intervalSpin)

        outer.addLayout(form)

        hint = QLabel("※ 自動更新は「保存のたび」に判定し、最後の更新から指定分数未満なら"
                      "スキップします。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #4a5568; font-size: 10px;")
        outer.addWidget(hint)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    def save(self):
        """設定を永続化する。"""
        set_export_method(self.methodCombo.currentData() or "playblast")
        set_auto_export_on_save(self.autoCheck.isChecked())
        set_auto_export_interval_min(self.intervalSpin.value())


# ─── メインウィンドウ ─────────────────────────────────────────────────────────
class OGPipelineWindow(QWidget):
    """
    QWidget ベース — Maya 内では QMainWindow を使わない。
    Maya のメインウィンドウを親に受け取り、独立した子ウィンドウとして表示する。
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Window)
        # 多重起動の検出に使う安定した識別名（reload してもクラスに依存しない）
        self.setObjectName(WINDOW_OBJECT_NAME)
        self.setWindowTitle("OG_Pipeline — Scene Opener")
        self.setMinimumSize(1000, 680)
        self.resize(1240, 760)

        self._selected_path = ""
        self._scan_thread = None
        self._pending_query = ""
        self._loading_combo = False
        self.active_root = None
        self.active_shots_parent = None
        self._last_export_at = {}    # {正規化シーンパス: 最終書き出し time.time()}
        self._save_job = None        # SceneSaved scriptJob の ID
        self._current_folder = None  # ブラウザでリーブ中のフォルダ（新規保存先候補）
        self._hw_watchers = []       # ハードウェア書き出しの完了監視タイマー

        self.setStyleSheet(STYLE)
        self._build_ui()

        # 起動時: 登録済みルートを読み込み、必要なら自動適用ルートを選択する
        self._reload_roots_combo()
        if self.rootCombo.count() > 0:
            self._select_in_combo(get_startup_root())
        self._apply_root()

        # 現在のシーン名を定期的に更新（ツール外で開閉されても追従する）
        self._update_current_scene_label()
        self._sceneTimer = QTimer(self)
        self._sceneTimer.timeout.connect(self._update_current_scene_label)
        self._sceneTimer.start(1500)

        # 保存時の自動書き出し（SceneSaved を監視。判定は実行時に設定を読む）
        self._register_save_job()

    def _register_save_job(self):
        """Maya の SceneSaved イベントを監視する scriptJob を登録する。"""
        try:
            import maya.cmds as cmds
        except Exception:
            return
        try:
            self._save_job = cmds.scriptJob(
                event=["SceneSaved", self._on_scene_saved], protected=False)
        except Exception as e:
            print("[OG_Pipeline] SceneSaved scriptJob 登録失敗:", e)

    def _kill_save_job(self):
        if self._save_job is None:
            return
        try:
            import maya.cmds as cmds
            if cmds.scriptJob(exists=self._save_job):
                cmds.scriptJob(kill=self._save_job, force=True)
        except Exception:
            pass
        self._save_job = None

    def _existing_output_mtime(self, scene_path):
        """既存の出力（連番/動画）の最新 mtime。無ければ None。"""
        times = []
        try:
            frames = find_scene_sequence(scene_path)
            if frames:
                times.append(max(os.path.getmtime(f) for f in frames))
        except Exception:
            pass
        try:
            vid = find_scene_video(scene_path)
            if vid:
                times.append(os.path.getmtime(vid))
        except Exception:
            pass
        return max(times) if times else None

    def _on_scene_saved(self):
        """シーン保存時に呼ばれる。設定が ON かつ最小間隔を満たすときだけ書き出す。"""
        if not get_auto_export_on_save():
            return
        try:
            import maya.cmds as cmds
            cur = cmds.file(q=True, sceneName=True) or ""
        except Exception:
            cur = ""
        if not cur:
            return
        interval = get_auto_export_interval_min() * 60.0
        now = time.time()
        key = os.path.normcase(os.path.normpath(cur))
        last = self._last_export_at.get(key)
        if last is None:
            last = self._existing_output_mtime(cur)   # セッションをまたいでも判定できる
        if interval > 0 and last is not None and (now - last) < interval:
            remain = int((interval - (now - last)) / 60) + 1
            self.statusLabel.setText(
                "自動書き出しをスキップ（前回更新から%d分未満／あと約%d分）"
                % (get_auto_export_interval_min(), remain))
            return
        self._last_export_at[key] = now
        method = get_export_method()   # 自動更新は環境設定の方式に従う
        if method == "hardware":
            ok, msg, proc = export_hardware_background(cur)
            self.statusLabel.setText(("🎬 自動: " if ok else "⚠ 自動: ") + msg)
            if ok and proc is not None:
                self._watch_hw_export(cur, proc, label="自動")
        else:
            self.statusLabel.setText("🎬 自動プレイブラスト中…")
            self._playblast(cur)

    def _update_current_scene_label(self):
        """ヘッダー中央に現在開いている Maya シーン名を表示する。"""
        name = ""
        try:
            import maya.cmds as cmds
            name = cmds.file(q=True, sceneName=True) or ""
        except Exception:
            name = ""
        if name:
            self.currentSceneLabel.setText("🎬  " + os.path.basename(name))
            self.currentSceneLabel.setToolTip(name)
        else:
            self.currentSceneLabel.setText("🎬  (未保存のシーン)")
            self.currentSceneLabel.setToolTip("")

    # ════════════════════════════════════════════════════════════════════
    #  UI 構築
    # ════════════════════════════════════════════════════════════════════
    def _build_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_header())
        root_layout.addWidget(self._build_root_bar())
        root_layout.addWidget(self._build_toolbar())

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_file_panel())
        splitter.addWidget(self._build_detail_panel())
        splitter.setSizes([900, 280])
        root_layout.addWidget(splitter, 1)

        # ステータスバー
        status_bar = QWidget()
        status_bar.setStyleSheet("background: #0a0d14; border-top: 1px solid #1e2435;")
        status_bar.setFixedHeight(22)
        sb_layout = QHBoxLayout(status_bar)
        sb_layout.setContentsMargins(8, 0, 8, 0)
        sb_layout.setSpacing(8)
        self.statusLabel = QLabel("準備完了")
        self.statusLabel.setStyleSheet("color: #3a4055; font-size: 11px;")
        sb_layout.addWidget(self.statusLabel, 1)
        self.progressBar = QProgressBar()
        self.progressBar.setFixedWidth(160)
        self.progressBar.setFixedHeight(6)
        self.progressBar.setTextVisible(False)
        self.progressBar.hide()
        sb_layout.addWidget(self.progressBar)
        root_layout.addWidget(status_bar)

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("header")
        header.setFixedHeight(72)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 0, 20, 0)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        app_title = QLabel("OG_PIPELINE")
        app_title.setObjectName("appTitle")
        subtitle = QLabel("MAYA SCENE OPENER  //  SHOT BROWSER")
        subtitle.setObjectName("appSubtitle")
        title_col.addWidget(app_title)
        title_col.addWidget(subtitle)
        layout.addLayout(title_col)
        layout.addStretch()

        # 中央: 現在開いている Maya シーン名
        self.currentSceneLabel = QLabel("🎬  (シーン未取得)")
        self.currentSceneLabel.setStyleSheet(
            "color: #e8c87a; font-size: 13px; font-weight: bold;")
        self.currentSceneLabel.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.currentSceneLabel)
        layout.addStretch()

        self.rootPathLabel = QLabel("▸  ルート未選択")
        self.rootPathLabel.setObjectName("rootPathLabel")
        layout.addWidget(self.rootPathLabel)
        return header

    def _build_root_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("toolbar")
        bar.setFixedHeight(46)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(8)

        lab = QLabel("PROJECT:")
        lab.setStyleSheet("color: #3a4055; font-size: 11px; letter-spacing: 1px;")
        layout.addWidget(lab)

        self.rootCombo = QComboBox()
        self.rootCombo.setMinimumWidth(240)
        self.rootCombo.activated.connect(lambda _i: self._apply_root())
        layout.addWidget(self.rootCombo)

        self.useBtn = QPushButton("★  次回も使用")
        self.useBtn.setObjectName("refreshBtn")
        self.useBtn.setToolTip("選択中のルートを次回起動時に自動設定（再度押すと解除）")
        self.useBtn.clicked.connect(self._use_for_startup)
        layout.addWidget(self.useBtn)

        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet("color: #1e2435;")
        layout.addWidget(sep)

        self.addRootBtn = QPushButton("⚙  プロジェクト設定")
        self.addRootBtn.setObjectName("refreshBtn")
        self.addRootBtn.setToolTip("フォルダを選んでプロジェクト（ルート）を新規登録")
        self.addRootBtn.clicked.connect(self._add_root)
        layout.addWidget(self.addRootBtn)

        self.importRootBtn = QPushButton("⭳  インポート")
        self.importRootBtn.setObjectName("refreshBtn")
        self.importRootBtn.setToolTip("ルート設定 JSON を取り込む")
        self.importRootBtn.clicked.connect(self._import_roots)
        layout.addWidget(self.importRootBtn)

        self.exportRootBtn = QPushButton("⭱  エクスポート")
        self.exportRootBtn.setObjectName("refreshBtn")
        self.exportRootBtn.setToolTip("登録済みルートを JSON として書き出す")
        self.exportRootBtn.clicked.connect(self._export_roots)
        layout.addWidget(self.exportRootBtn)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.VLine)
        sep2.setStyleSheet("color: #1e2435;")
        layout.addWidget(sep2)

        self.allShotsBtn = QPushButton("🎞  全ショット動画")
        self.allShotsBtn.setObjectName("refreshBtn")
        self.allShotsBtn.setToolTip("全ショットの最新動画をグリッドで一覧・自動再生")
        self.allShotsBtn.clicked.connect(self._open_all_shots)
        layout.addWidget(self.allShotsBtn)

        # cv2 が無い環境向け: mp4 埋め込み再生を有効化（cv2 を --user 導入）
        self.enableMp4Btn = QPushButton("🎬  mp4再生を有効化")
        self.enableMp4Btn.setObjectName("refreshBtn")
        self.enableMp4Btn.setToolTip("opencv-python を --user 導入して mp4 を埋め込み再生（共有 Maya は変更しません）")
        self.enableMp4Btn.clicked.connect(self._install_cv2)
        self.enableMp4Btn.setVisible(not _HAS_CV2)
        layout.addWidget(self.enableMp4Btn)

        layout.addStretch()

        # 右上: 環境設定（書き出し方式・保存時の自動更新など）
        self.settingsBtn = QPushButton("🛠  環境設定")
        self.settingsBtn.setObjectName("refreshBtn")
        self.settingsBtn.setToolTip("書き出し方式・保存時の動画自動更新などを設定")
        self.settingsBtn.clicked.connect(self._open_settings)
        layout.addWidget(self.settingsBtn)
        return bar

    def _on_auto_export_toggled(self, checked):
        set_auto_export_on_save(checked)
        self.statusLabel.setText(
            "保存時の自動書き出しを %s にしました" % ("ON" if checked else "OFF"))

    def _on_manual_method_changed(self, _idx):
        method = self.exportMethodCombo.currentData() or "playblast"
        set_manual_export_method(method)   # 選択を記憶（次回起動時も維持）
        self.statusLabel.setText(
            "手動書き出しの方式: %s"
            % ("ハードウェア(裏)" if method == "hardware" else "プレイブラスト"))

    def _open_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec():
            dlg.save()
            # 環境設定は自動更新用。手動の方式プルダウンとは独立なので同期しない。
            # 自動書き出しの ON/OFF だけ、ムービーバーのチェックと同期する。
            try:
                self.autoExportCheck.blockSignals(True)
                self.autoExportCheck.setChecked(get_auto_export_on_save())
                self.autoExportCheck.blockSignals(False)
            except Exception:
                pass
            self.statusLabel.setText(
                "環境設定を保存しました（自動更新の方式: %s ／ 自動更新: %s ／ 最小間隔: %d分）"
                % (get_export_method(),
                   "ON" if get_auto_export_on_save() else "OFF",
                   get_auto_export_interval_min()))

    def _open_all_shots(self):
        if not self.active_shots_parent or not os.path.isdir(str(self.active_shots_parent)):
            self.statusLabel.setText("ショットフォルダの親が未設定です（[プロジェクト設定] で指定）")
            return
        # 既存ウィンドウは閉じる（裏でデコードスレッドが溜まるのを防ぐ）
        old = getattr(self, "_all_shots_dlg", None)
        if old is not None:
            try:
                old.stop_all()
                old.close()
                old.deleteLater()
            except Exception:
                pass
        self._all_shots_dlg = AllShotsDialog(self.active_shots_parent, self)
        self._all_shots_dlg.show()
        self._all_shots_dlg.raise_()

    def reveal_in_browser(self, folder):
        """ブラウザ（Miller カラム）でフォルダまで潜って表示し、前面に出す。"""
        if not folder or not os.path.isdir(str(folder)):
            self.statusLabel.setText("フォルダが見つかりません: %s" % folder)
            return
        # 検索中だと邪魔なのでクリアし、ルートを active_root に戻してから潜る
        try:
            self.searchBar.blockSignals(True)
            self.searchBar.clear()
            self.searchBar.blockSignals(False)
        except Exception:
            pass
        self.browser.set_root(self.active_root)
        ok = self.browser.reveal_path(folder)
        self.raise_()
        self.activateWindow()
        if not ok:
            self.statusLabel.setText("ブラウザで表示できませんでした（ルート外）: %s" % folder)
        else:
            self.statusLabel.setText("▸  %s" % folder)

    def closeEvent(self, event):
        """ウィンドウを閉じる際、実行中のデコード/検索スレッドを確実に止める。

        実行中の QThread が破棄されると Qt がプロセスごと落ちる（＝Maya クラッシュ）。
        埋め込みプレイヤー・全ショットダイアログ・検索スレッドを明示的に停止する。
        """
        try:
            self._sceneTimer.stop()
        except Exception:
            pass
        for t in getattr(self, "_hw_watchers", []):
            try:
                t.stop()
            except Exception:
                pass
        self._kill_save_job()
        try:
            self.detailPanel.video.stop()
        except Exception:
            pass
        dlg = getattr(self, "_all_shots_dlg", None)
        if dlg is not None:
            try:
                dlg.stop_all()
                dlg.close()
            except Exception:
                pass
        if self._scan_thread is not None and self._scan_thread.isRunning():
            try:
                self._scan_thread.requestInterruption()
                self._scan_thread.quit()
                self._scan_thread.wait(3000)
            except Exception:
                pass
        super().closeEvent(event)

    def _install_cv2(self):
        r = QMessageBox.question(
            self, "mp4 再生を有効化",
            "opencv-python-headless を --user でインストールします。\n"
            "（共有 Maya 本体は変更せず、ユーザー領域に入ります。数分かかる場合があります）\n\n"
            "ネットワーク/プロキシ環境では失敗することがあります。続行しますか？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if r != QMessageBox.Yes:
            return
        self.statusLabel.setText("cv2 をインストール中…（しばらくお待ちください）")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok, log = install_opencv()
        finally:
            QApplication.restoreOverrideCursor()
        if ok:
            self.enableMp4Btn.setVisible(False)
            self.statusLabel.setText("✓  cv2 を導入しました。mp4 を埋め込み再生できます")
            self.detailPanel.reload_video()
            QMessageBox.information(self, "完了",
                                    "cv2 を導入しました。mp4 が埋め込み再生されます。\n"
                                    "（うまく読み込めない場合は Maya を再起動してください）")
        else:
            QMessageBox.warning(self, "インストール失敗",
                                "cv2 を導入できませんでした。ログ:\n\n" + (log or "")[-1500:])
            self.statusLabel.setText("⚠  cv2 の導入に失敗しました")

    def _build_toolbar(self) -> QWidget:
        toolbar = QWidget()
        toolbar.setObjectName("toolbar")
        toolbar.setFixedHeight(48)
        layout = QHBoxLayout(toolbar)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        search_icon = QLabel("⌕")
        search_icon.setStyleSheet("color: #3a4055; font-size: 16px;")
        layout.addWidget(search_icon)

        self.searchBar = QLineEdit()
        self.searchBar.setObjectName("searchBar")
        self.searchBar.setPlaceholderText("ファイル名またはパスで検索（ルート以下を再帰検索）…")
        self.searchBar.textChanged.connect(self._apply_view)
        layout.addWidget(self.searchBar, 1)

        filter_label = QLabel("TYPE:")
        filter_label.setStyleSheet("color: #3a4055; font-size: 11px; letter-spacing: 1px;")
        layout.addWidget(filter_label)

        self.typeFilter = QComboBox()
        self.typeFilter.addItems(["ALL", ".ma", ".mb"])
        self.typeFilter.setFixedWidth(80)
        self.typeFilter.currentTextChanged.connect(self._apply_view)
        layout.addWidget(self.typeFilter)

        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet("color: #1e2435;")
        layout.addWidget(sep)

        self.gotoCurrentBtn = QPushButton("◎  現在のシーン")
        self.gotoCurrentBtn.setObjectName("refreshBtn")
        self.gotoCurrentBtn.setToolTip("現在開いているシーンの保存先フォルダまでカラムを展開する")
        self.gotoCurrentBtn.clicked.connect(self._goto_current_scene)
        layout.addWidget(self.gotoCurrentBtn)

        self.refreshBtn = QPushButton("↻  REFRESH")
        self.refreshBtn.setObjectName("refreshBtn")
        self.refreshBtn.clicked.connect(self._apply_view)
        layout.addWidget(self.refreshBtn)
        return toolbar

    def _build_file_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.browser = ColumnBrowser()
        self.browser.file_selected.connect(self._on_file_selected)
        self.browser.file_activated.connect(self._open_path)
        self.browser.context_requested.connect(self._show_context_menu)
        self.browser.folder_selected.connect(self._on_folder_selected)
        layout.addWidget(self.browser, 1)

        action_bar = QWidget()
        action_bar.setStyleSheet("background: #0a0d14; border-top: 1px solid #1e2435;")
        action_bar.setFixedHeight(56)
        ab_layout = QHBoxLayout(action_bar)
        ab_layout.setContentsMargins(16, 8, 16, 8)

        self.selectedLabel = QLabel("ファイルが選択されていません")
        self.selectedLabel.setStyleSheet("color: #2a3045; font-size: 11px;")
        ab_layout.addWidget(self.selectedLabel, 1)

        # 選択中のシーンのフォルダをエクスプローラー（OS のファイラ）で開く
        self.openFolderBtn = QPushButton("📂  フォルダを開く")
        self.openFolderBtn.setObjectName("refreshBtn")
        self.openFolderBtn.setToolTip("選択中のシーンのフォルダをエクスプローラーで開く")
        self.openFolderBtn.setEnabled(False)
        self.openFolderBtn.clicked.connect(self._open_in_explorer)
        ab_layout.addWidget(self.openFolderBtn)

        # 現在開いているシーンを、そのシーンのフォルダを既定にして別名保存する
        self.saveAsBtn = QPushButton("⤓  SAVE AS")
        self.saveAsBtn.setObjectName("refreshBtn")
        self.saveAsBtn.setToolTip("現在のシーンを、開いているシーンのフォルダを既定にして保存")
        self.saveAsBtn.clicked.connect(self._save_scene_as)
        ab_layout.addWidget(self.saveAsBtn)

        # 名前末尾の番号を +1 してローカルバージョンを上げて保存
        self.versionUpBtn = QPushButton("⇧  VERSION UP")
        self.versionUpBtn.setObjectName("refreshBtn")
        self.versionUpBtn.setToolTip("ファイル名末尾の番号を +1 して同じフォルダに保存")
        self.versionUpBtn.clicked.connect(self._version_up_save)
        ab_layout.addWidget(self.versionUpBtn)

        # 現在リーブ（表示）中のフォルダに新規シーンを保存
        self.saveNewBtn = QPushButton("✚  SAVE NEW SCENE")
        self.saveNewBtn.setObjectName("refreshBtn")
        self.saveNewBtn.setToolTip("現在ブラウザで開いている（リーブ中の）フォルダに新規シーンを保存")
        self.saveNewBtn.clicked.connect(self._save_new_scene)
        ab_layout.addWidget(self.saveNewBtn)

        self.importBtn = QPushButton("▤  IMPORT")
        self.importBtn.setObjectName("importBtn")
        self.importBtn.setEnabled(False)
        self.importBtn.clicked.connect(self._import_scene)
        ab_layout.addWidget(self.importBtn)

        self.openBtn = QPushButton("▶  OPEN SCENE")
        self.openBtn.setObjectName("openBtn")
        self.openBtn.setEnabled(False)
        self.openBtn.clicked.connect(self._open_scene)
        ab_layout.addWidget(self.openBtn)

        layout.addWidget(action_bar)

        # ── 動画書き出し操作バー（ファイル操作バーの一つ下） ──
        layout.addWidget(self._build_movie_bar())
        return panel

    def _build_movie_bar(self) -> QWidget:
        """動画書き出し専用の操作バー。方式プルダウンは手動書き出し用（環境設定とは独立）。"""
        movie_bar = QWidget()
        movie_bar.setStyleSheet("background: #0a0d14; border-top: 1px solid #1e2435;")
        movie_bar.setFixedHeight(48)
        mb = QHBoxLayout(movie_bar)
        mb.setContentsMargins(16, 7, 16, 7)
        mb.setSpacing(8)

        title = QLabel("🎬  動画書き出し")
        title.setStyleSheet("color: #3a4055; font-size: 11px; letter-spacing: 1px;")
        mb.addWidget(title)

        # 保存時の自動書き出し ON/OFF（環境設定と同じ値。ここで素早く切替できる）
        self.autoExportCheck = QCheckBox("保存時に自動書き出し")
        self.autoExportCheck.setToolTip(
            "ON のときだけ、シーン保存（Ctrl+S）で自動書き出しします（最小間隔は環境設定）。\n"
            "OFF（既定）なら保存しても書き出しは走りません。")
        self.autoExportCheck.setChecked(get_auto_export_on_save())
        self.autoExportCheck.toggled.connect(self._on_auto_export_toggled)
        mb.addWidget(self.autoExportCheck)

        mb.addStretch(1)

        mb.addWidget(QLabel("方式:"))
        # 手動書き出し用の方式プルダウン（環境設定とは独立。初期値は設定から）
        self.exportMethodCombo = QComboBox()
        self.exportMethodCombo.addItem("プレイブラスト", "playblast")
        self.exportMethodCombo.addItem("ハードウェア(裏)", "hardware")
        self.exportMethodCombo.setToolTip(
            "手動書き出しの方式（環境設定の自動更新とは独立。選択は記憶されます）\n"
            "プレイブラスト: 現在のビューを撮る（一瞬画面が止まる）\n"
            "ハードウェア(裏): 別プロセスでレンダー（手元を止めない／画面に出ない）")
        idx = self.exportMethodCombo.findData(get_manual_export_method())
        if idx >= 0:
            self.exportMethodCombo.setCurrentIndex(idx)
        self.exportMethodCombo.activated.connect(self._on_manual_method_changed)
        mb.addWidget(self.exportMethodCombo)

        # 現在シーンを Pipeline_Movie に書き出し（最小間隔は無視＝常に実行）
        self.playblastBtn = QPushButton("🎬  動画書き出し")
        self.playblastBtn.setObjectName("refreshBtn")
        self.playblastBtn.setToolTip("現在のシーンを Pipeline_Movie にシーン名と同名で書き出す（手動は間隔制限なし）")
        self.playblastBtn.clicked.connect(self._playblast_current)
        mb.addWidget(self.playblastBtn)
        return movie_bar

    def _build_detail_panel(self) -> QWidget:
        self.detailPanel = DetailPanel()
        return self.detailPanel

    # ════════════════════════════════════════════════════════════════════
    #  ルート（プロジェクト）管理
    # ════════════════════════════════════════════════════════════════════
    def _current_root_name(self):
        idx = self.rootCombo.currentIndex()
        return self.rootCombo.itemData(idx) if idx >= 0 else None

    def _reload_roots_combo(self, select_name=None):
        """ストアからプルダウンを再構築する。起動時設定には ★ を付ける。"""
        self._loading_combo = True
        self.rootCombo.clear()
        startup = get_startup_root()
        for r in load_roots():
            label = ("★  " + r["name"]) if r["name"] == startup else r["name"]
            self.rootCombo.addItem(label, r["name"])
        self._loading_combo = False
        if select_name is not None:
            self._select_in_combo(select_name)

    def _select_in_combo(self, name):
        for i in range(self.rootCombo.count()):
            if self.rootCombo.itemData(i) == name:
                self.rootCombo.setCurrentIndex(i)
                return True
        if self.rootCombo.count() > 0:
            self.rootCombo.setCurrentIndex(0)
        return False

    def _apply_root(self):
        """選択中のルートを有効化し、ブラウザに反映する。"""
        name = self._current_root_name()
        if not name:
            self.active_root = None
            self.active_shots_parent = None
            self.rootPathLabel.setText("▸  ルート未登録")
            self.browser.set_root(None)
            self.statusLabel.setText(
                "プロジェクトルート未登録 — [プロジェクト設定] か [⭳ インポート] で登録してください"
            )
            return
        entry = find_root_entry(name) or {}
        self.active_root = entry.get("path") or find_root_path(name)
        self.active_shots_parent = entry.get("shots_parent") or self.active_root
        self.rootPathLabel.setText(f"▸  {self.active_root}")
        self._apply_view()

    def _add_root(self):
        folder = QFileDialog.getExistingDirectory(
            self, "プロジェクトルートを選択", str(Path.home())
        )
        if not folder:
            return
        default_name = Path(folder).name or folder
        name, ok = QInputDialog.getText(
            self, "プロジェクト名", "このルートの名前:", text=default_name
        )
        if not ok or not name.strip():
            return
        name = name.strip()

        # ショットフォルダの「親」階層を指定（その直下のフォルダ＝ショットと判別）。
        # キャンセル時はルート自身を親とする（root 直下がショット）。
        QMessageBox.information(
            self, "ショットフォルダの親を選択",
            "次に『ショットフォルダの一つ上の階層』を選んでください。\n"
            "そのフォルダの直下にあるフォルダをショットとして扱います。\n"
            "（ルート直下がショットならルートを選択 / キャンセルでルート）",
        )
        shots_parent = QFileDialog.getExistingDirectory(
            self, "ショットフォルダの親階層を選択（直下＝ショット）", folder
        ) or folder

        add_root(name, folder, shots_parent)
        self._reload_roots_combo(select_name=name)
        self._apply_root()
        self.statusLabel.setText(f"✓  ルートを追加: {name}（ショット親: {shots_parent}）")

    # ── フォルダ選択（配下の最新動画を表示） ───────────────
    def _is_shot_folder(self, folder):
        """folder が「ショットフォルダの親」の直下なら True（＝ショットフォルダ）。"""
        if not self.active_shots_parent:
            return False
        try:
            a = os.path.normcase(os.path.normpath(os.path.dirname(folder)))
            b = os.path.normcase(os.path.normpath(self.active_shots_parent))
            return a == b
        except Exception:
            return False

    def _on_folder_selected(self, folder):
        # フォルダ（ショット／工程フォルダ等）選択 → 配下の最新動画を再生
        self._current_folder = folder    # リーブ中フォルダ（新規保存先の候補）
        self._selected_path = ""
        self.openBtn.setEnabled(False)
        self.importBtn.setEnabled(False)
        self.openFolderBtn.setEnabled(False)
        label = "ショット" if self._is_shot_folder(folder) else "フォルダ"
        self.selectedLabel.setText(f"{label}: {Path(folder).name}（配下の最新動画）")
        self.detailPanel.show_folder_video(folder)

    def _import_roots(self):
        fp, _ = QFileDialog.getOpenFileName(
            self, "ルート設定 JSON をインポート", str(Path.home()), "JSON Files (*.json)"
        )
        if not fp:
            return
        prev = self._current_root_name()
        try:
            count = import_roots_file(fp)
        except Exception as e:
            QMessageBox.warning(self, "インポート失敗", f"JSON を読み込めませんでした:\n{e}")
            return
        self._reload_roots_combo(select_name=prev)
        self._apply_root()
        self.statusLabel.setText(f"✓  インポート完了: {count} 件")

    def _export_roots(self):
        roots = load_roots()
        if not roots:
            QMessageBox.information(self, "エクスポート", "登録済みのルートがありません。")
            return
        fp, _ = QFileDialog.getSaveFileName(
            self, "ルート設定 JSON をエクスポート",
            str(Path.home() / "og_roots.json"), "JSON Files (*.json)"
        )
        if not fp:
            return
        if not fp.lower().endswith(".json"):
            fp += ".json"
        try:
            export_roots_file(fp, roots)
        except Exception as e:
            QMessageBox.warning(self, "エクスポート失敗", str(e))
            return
        self.statusLabel.setText(f"✓  エクスポート: {fp}")

    def _use_for_startup(self):
        """選択中のルートを次回起動時の自動設定にする（トグル）。"""
        name = self._current_root_name()
        if not name:
            return
        if get_startup_root() == name:
            clear_startup_root()
            self.statusLabel.setText(f"次回起動時の自動設定を解除: {name}")
        else:
            set_startup_root(name)
            self.statusLabel.setText(f"★  次回起動時に自動設定: {name}")
        self._reload_roots_combo(select_name=name)  # ★ マーカーを更新（再スキャンなし）

    # ════════════════════════════════════════════════════════════════════
    #  表示（カラム表示／再帰検索）
    # ════════════════════════════════════════════════════════════════════
    def _apply_view(self, *args):
        ext = self.typeFilter.currentText()
        self.browser.ext_filter = None if ext == "ALL" else ext

        self._selected_path = ""
        self.openBtn.setEnabled(False)
        self.importBtn.setEnabled(False)
        self.openFolderBtn.setEnabled(False)
        self.detailPanel.clear()
        self.selectedLabel.setText("ファイルを選択してください")

        if not self.active_root:
            self.browser.set_root(None)
            return

        query = self.searchBar.text().strip().lower()
        if not query:
            self.browser.set_root(self.active_root)
            self.statusLabel.setText(f"▸  {self.active_root}")
        else:
            # 検索中もカラム表示を保つため、ルートを保持したまま結果でフィルタする
            self.browser.root = Path(self.active_root)
            self._start_search(query, self.browser.ext_filter)

    def _start_search(self, query, ext):
        if self._scan_thread is not None and self._scan_thread.isRunning():
            self._scan_thread.requestInterruption()
            self._scan_thread.quit()
            self._scan_thread.wait(3000)
        self._pending_query = query
        self.statusLabel.setText("検索中…")
        self.progressBar.setRange(0, 0)
        self.progressBar.show()
        self._scan_thread = ScanThread(Path(self.active_root), ext)
        self._scan_thread.found.connect(self._on_search_found)
        self._scan_thread.finished_scan.connect(lambda c: self.progressBar.hide())
        self._scan_thread.start()

    def _on_search_found(self, results):
        q = self._pending_query
        root_norm = os.path.normpath(str(self.active_root))
        files, dirs = set(), set()
        for rel, abs_p, _size, _mtime in results:
            if q not in rel.lower():
                continue
            files.add(os.path.normpath(abs_p))
            # ヒットの親フォルダからルートまでを「表示対象フォルダ」に積む
            p = os.path.normpath(os.path.dirname(abs_p))
            while True:
                dirs.add(p)
                if p == root_norm:
                    break
                parent = os.path.dirname(p)
                if parent == p:
                    break
                p = parent
        # 通常ドリルと同じカラム表示のまま、ヒットだけに絞り込む
        self.browser.apply_search_filter(files, dirs)
        self.statusLabel.setText(f"検索: {len(files)} 件ヒット  |  {self.active_root}")

    # ════════════════════════════════════════════════════════════════════
    #  選択
    # ════════════════════════════════════════════════════════════════════
    def _on_file_selected(self, info):
        if not info:
            self._selected_path = ""
            self.openBtn.setEnabled(False)
            self.importBtn.setEnabled(False)
            self.openFolderBtn.setEnabled(False)
            self.selectedLabel.setText("ファイルを選択してください")
            self.detailPanel.clear()
            return
        self._selected_path = info["abs"]
        self._current_folder = os.path.dirname(info["abs"])   # リーブ中フォルダ
        self.openBtn.setEnabled(True)
        self.importBtn.setEnabled(True)
        self.openFolderBtn.setEnabled(True)
        self.selectedLabel.setText(f"選択: {Path(info['abs']).name}")
        self.detailPanel.update_info(info["rel"], info["abs"], info["size"], info["mtime"])

    def _open_path(self, path):
        self._selected_path = path
        self._open_scene()

    def _goto_current_scene(self):
        """現在開いているシーンの保存先フォルダまでカラムを展開して選択する。"""
        try:
            import maya.cmds as cmds
            cur = cmds.file(q=True, sceneName=True)
        except ImportError:
            cur = ""  # スタンドアロン
        if not cur:
            self.statusLabel.setText("現在のシーンは未保存です（保存先がありません）")
            return
        if not self.active_root:
            self.statusLabel.setText("ルートが選択されていません")
            return

        # 検索中なら通常表示へ戻す
        if self.searchBar.text():
            self.searchBar.blockSignals(True)
            self.searchBar.clear()
            self.searchBar.blockSignals(False)

        if self.browser.reveal_path(cur):
            self.statusLabel.setText(f"◎  現在のシーン: {Path(cur).name}")
        else:
            self.statusLabel.setText(
                f"⚠  現在のシーンはこのルート配下にありません: {cur}"
            )

    # ════════════════════════════════════════════════════════════════════
    #  右クリックメニュー
    # ════════════════════════════════════════════════════════════════════
    def _show_context_menu(self, path, global_pos):
        menu = QMenu(self)
        act_open = menu.addAction("▶  シーンを開く")
        act_import = menu.addAction("▤  インポート")
        act_folder = menu.addAction("📂  フォルダを開く")
        menu.addSeparator()
        act_pb = menu.addAction("🎬  プレイブラスト書き出し")
        vid = find_scene_video(path)
        act_playvid = menu.addAction("▶  動画を再生") if vid else None
        act_ref = menu.addAction("⊟  リファレンスを編集…")
        chosen = menu.exec_(global_pos) if hasattr(menu, "exec_") else menu.exec(global_pos)
        if chosen is None:
            return
        self._selected_path = path
        if chosen == act_open:
            self._open_scene()
        elif chosen == act_import:
            self._import_scene()
        elif chosen == act_folder:
            self._open_in_explorer()
        elif chosen == act_pb:
            self._playblast(path)
        elif act_playvid is not None and chosen == act_playvid:
            open_file_external(vid)
        elif chosen == act_ref:
            self._edit_references(path)

    def _edit_references(self, path):
        """シーンを開かずに .ma のリファレンスパスを直接編集する。"""
        if Path(path).suffix.lower() != ".ma":
            QMessageBox.information(
                self, "リファレンス編集",
                "直接編集は .ma のみ対応です。\n"
                "（.mb はバイナリのため、Maya 内で開いて Reference Editor を使用してください）",
            )
            return

        # 対象が現在 Maya で開かれている場合は注意喚起
        try:
            import maya.cmds as cmds
            cur = cmds.file(q=True, sceneName=True) or ""
            if os.path.normcase(os.path.normpath(cur)) == os.path.normcase(os.path.normpath(path)):
                r = QMessageBox.question(
                    self, "確認",
                    "このシーンは現在 Maya で開かれています。\n"
                    "ディスク上のファイルを直接書き換えても開いているシーンには反映されず、\n"
                    "そのシーンを保存すると編集が上書きされます。続行しますか？",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if r != QMessageBox.Yes:
                    return
        except ImportError:
            pass

        refinfos = self._parse_ma_reference_info(path)
        if not refinfos:
            QMessageBox.information(
                self, "リファレンス編集",
                f"{Path(path).name} に編集できるリファレンスは見つかりませんでした。",
            )
            return

        dlg = ReferenceEditDialog(path, refinfos, self)
        ok = dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec()
        if not ok:
            return
        changes = dlg.changes()
        if not changes:
            self.statusLabel.setText("リファレンスの変更はありません")
            return
        n_remove = dlg.remove_count()
        if n_remove:
            r = QMessageBox.question(
                self, "リファレンス削除の確認",
                f"{n_remove} 件のリファレンスを .ma から削除します。\n"
                "（バックアップは作成されます）よろしいですか？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if r != QMessageBox.Yes:
                return
        try:
            backup, n, removed = self._rewrite_ma_references(path, changes)
        except Exception as e:
            QMessageBox.warning(self, "保存失敗", f"書き換えに失敗しました:\n{e}")
            return
        msg = f"✓  リファレンス更新: {n} 件"
        if removed:
            msg += f" / 削除: {removed} 件"
        msg += f"  (バックアップ: {Path(backup).name})"
        self.statusLabel.setText(msg)

    @staticmethod
    def _ma_flag(s, flag):
        """file 行から -ns/-rfn/-typ などのフラグ値（直後の引用文字列）を取り出す。"""
        m = re.search(re.escape(flag) + r'\s+"((?:[^"\\]|\\.)*)"', s)
        return m.group(1).replace('\\"', '"') if m else ""

    @staticmethod
    def _ma_flag_num(s, flag):
        """-dr 1 のような引用なし数値フラグの値を返す（無ければ None）。"""
        m = re.search(re.escape(flag) + r'\s+(-?\d+)\b', s)
        return int(m.group(1)) if m else None

    # 値を取るフラグ（この直後の引用トークンはパスではなく値）
    _REF_VALUE_FLAGS = {"-ns", "-rfn", "-typ", "-op", "-rdn", "-pmt", "-rpr",
                        "-namespace", "-referenceNode", "-type", "-options"}

    @classmethod
    def _ref_path_match(cls, line):
        """file 行から参照パスの引用トークン（Match）を返す。無ければ None。

        判定: ①直前トークンが「値を取るフラグ」でない引用トークン（位置引数）を優先。
              ②それが無い場合、パスらしい引用トークン（/ や \\、.ma/.mb 等の拡張子を含む）
                にフォールバック。
        例: `file -rdi 1 -ns "ns" -rfn "nsRN" -typ "mayaAscii" "path.ma";` → "path.ma"
            パスを持たない -rdi 行（末尾が -typ "mayaAscii" 等）では None。
        """
        matches = list(re.finditer(r'"(?:[^"\\]|\\.)*"', line))

        def looks_path(m):
            v = m.group(0)[1:-1]
            return ("/" in v or "\\" in v
                    or re.search(r"\.(ma|mb|abc|fbx|obj|usd[acz]?)$", v, re.IGNORECASE) is not None)

        positional = []
        for m in matches:
            toks = line[:m.start()].rstrip().split()
            prev = toks[-1] if toks else ""
            if prev not in cls._REF_VALUE_FLAGS:
                positional.append(m)

        path_like_pos = [m for m in positional if looks_path(m)]
        if path_like_pos:
            return path_like_pos[-1]
        if positional:
            return positional[-1]
        # フォールバック: パスらしい引用トークンの最後（フラグ値判定が外れた場合の保険）
        path_like_any = [m for m in matches if looks_path(m)]
        return path_like_any[-1] if path_like_any else None

    @classmethod
    def _ref_path(cls, line):
        m = cls._ref_path_match(line)
        return m.group(0)[1:-1].replace('\\"', '"') if m else None

    @staticmethod
    def _split_ref_statements(content):
        """content を [(is_ref, text), ...] に分割する。

        Maya の file コマンドは複数行に分かれることがある（例: -typ "mayaAscii" の
        次の行にパス）。`file -r/-rdi` で始まる行から、行末が ';' で終わる行までを
        1つの参照文(text)としてまとめる。それ以外は物理行のまま通す。
        連結すると元の content を完全再現する（改行コードも保持）。
        """
        lines = content.splitlines(keepends=True)
        segs = []
        i, n = 0, len(lines)
        while i < n:
            s = lines[i].strip()
            if s.startswith("file ") and re.search(r"\s-r(di)?\b", s):
                group = [lines[i]]
                while not group[-1].rstrip().endswith(";"):
                    i += 1
                    if i >= n:
                        break
                    group.append(lines[i])
                segs.append((True, "".join(group)))
                i += 1
            else:
                segs.append((False, lines[i]))
                i += 1
        return segs

    @classmethod
    def _parse_ma_reference_info(cls, path):
        """.ma の参照文から [{'key','path','namespace','refnode','type'}] を抽出。

        複数行にまたがる file 文も1文として解析する。同定キーは reference node
        （-rfn）。同じパスを別ネームスペースで複数参照するケースを区別するため、
        パスではなく refNode で重複除去する（-rfn が無ければパスにフォールバック）。
        """
        infos = []
        seen = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
                content = f.read()
        except Exception:
            return infos
        for is_ref, text in cls._split_ref_statements(content):
            if not is_ref:
                continue
            p = cls._ref_path(text) or ""
            ns = cls._ma_flag(text, "-ns")
            rfn = cls._ma_flag(text, "-rfn")
            typ = cls._ma_flag(text, "-typ")
            is_rdi = re.match(r'\s*file\s+-rdi\b', text) is not None
            # ロード/アンロードは -rdi 文の -dr で決まる（有=アンロード, 無=ロード）。
            # -r 文の -dr は常に 1 で状態を表さないため無視する。
            unloaded = is_rdi and (cls._ma_flag_num(text, "-dr") == 1)
            if not rfn and not p:
                continue
            key = ("rfn:" + rfn) if rfn else ("path:" + p)
            if key not in seen:
                info = {"key": key, "path": p, "namespace": ns,
                        "refnode": rfn, "type": typ, "unloaded": unloaded}
                seen[key] = info
                infos.append(info)
            else:
                info = seen[key]
                info["namespace"] = info["namespace"] or ns
                info["type"] = info["type"] or typ
                info["path"] = info["path"] or p
                if is_rdi:    # 状態は -rdi 文から採用
                    info["unloaded"] = unloaded
        return infos

    @staticmethod
    def _set_rdi_unloaded(text, unloaded):
        """-rdi 文のロード状態を設定する。

        アンロード = -dr 1 を付与（Maya は -rfn の直前に置く）。
        ロード = -dr フラグを除去。-r 文側は変更しない（常に -dr 1 のまま）。
        """
        has_dr = re.search(r'-dr\s+-?\d+\b', text) is not None
        if unloaded:
            if has_dr:
                return re.sub(r'-dr\s+-?\d+', '-dr 1', text, count=1)
            if re.search(r'\s-rfn\b', text):
                return re.sub(r'(\s)(-rfn\b)', r'\g<1>-dr 1 \g<2>', text, count=1)
            return re.sub(r'(\bfile\s+-rdi\s+\d+)', r'\1 -dr 1', text, count=1)
        # ロード: -dr フラグを取り除く
        if has_dr:
            return re.sub(r'\s*-dr\s+-?\d+', '', text, count=1)
        return text

    @classmethod
    def _rewrite_ma_references(cls, path, changes):
        """.ma の参照文を changes に従って書き換える（パス/ns/アンロード/削除）。

        複数行にまたがる file 文にも対応。各文は refNode（無ければパス）で対象判定。
        改行コードは維持し、書き換え前にタイムスタンプ付きバックアップを作成する。
        戻り値: (バックアップパス, 変更した参照数, 削除した参照数)。
        """
        import shutil
        import datetime

        remove_rfns = {c["refnode"] for c in changes if c.get("remove") and c.get("refnode")}
        remove_paths = {c["old_path"] for c in changes if c.get("remove") and not c.get("refnode")}
        by_refnode = {c["refnode"]: c for c in changes
                      if c.get("refnode") and not c.get("remove")}
        by_path = {c["old_path"]: c for c in changes
                   if not c.get("refnode") and not c.get("remove")}

        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            content = f.read()

        count = 0
        removed_rfns_seen = set()
        removed_paths_seen = set()
        out_parts = []
        for is_ref, text in cls._split_ref_statements(content):
            if is_ref:
                rfn = cls._ma_flag(text, "-rfn")
                pm = cls._ref_path_match(text)
                old_path = pm.group(0)[1:-1].replace('\\"', '"') if pm else None

                # 削除対象（refNode 一致、または refNode 無しでパス一致）→ 文ごと破棄
                if (rfn and rfn in remove_rfns) or (not rfn and old_path in remove_paths):
                    if rfn:
                        removed_rfns_seen.add(rfn)
                    elif old_path:
                        removed_paths_seen.add(old_path)
                    continue

                ch = by_refnode.get(rfn) if rfn else (by_path.get(old_path) if old_path else None)
                if ch:
                    changed = False
                    if pm is not None and ch["new_path"] != ch["old_path"]:
                        newtok = ch["new_path"].replace('"', '\\"')
                        text = text[:pm.start()] + '"' + newtok + '"' + text[pm.end():]
                        changed = True
                    if ch.get("new_ns") != ch.get("old_ns"):
                        new_text = cls._replace_flag(text, "-ns", ch.get("new_ns", ""))
                        if new_text != text:
                            text = new_text
                            changed = True
                    # ロード/アンロードは -rdi 文の -dr 有無で表す（-r 文は触らない）
                    if ch.get("new_unload") != ch.get("old_unload") and re.match(r'\s*file\s+-rdi\b', text):
                        text = cls._set_rdi_unloaded(text, ch["new_unload"])
                        changed = True
                    if changed:
                        count += 1
            out_parts.append(text)

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = f"{path}.{ts}.bak"
        shutil.copy2(path, backup)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("".join(out_parts))
        removed = len(removed_rfns_seen) + len(removed_paths_seen)
        return backup, count, removed

    @staticmethod
    def _replace_flag(line, flag, new_value):
        """行内の `flag "..."` の値を new_value に置換（最初の1箇所のみ）。"""
        newesc = new_value.replace('"', '\\"')
        return re.sub(re.escape(flag) + r'\s+"(?:[^"\\]|\\.)*"',
                      flag + ' "' + newesc + '"', line, count=1)

    # ════════════════════════════════════════════════════════════════════
    #  Maya アクション
    # ════════════════════════════════════════════════════════════════════
    def _open_scene(self):
        if not self._selected_path:
            return
        path = self._selected_path
        try:
            import maya.cmds as cmds

            if cmds.file(q=True, modified=True):
                reply = QMessageBox.question(
                    self,
                    "未保存の変更",
                    "現在のシーンに未保存の変更があります。\nシーンを開く前に保存しますか？",
                    QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                )
                if reply == QMessageBox.Cancel:
                    return
                if reply == QMessageBox.Save:
                    # 新規（未名）シーンは save できないため saveAs ダイアログを出す。
                    # 既定フォルダは「これから開くシーンのフォルダ」にする。
                    if cmds.file(q=True, sceneName=True):
                        cmds.file(save=True)
                    else:
                        start_dir = os.path.dirname(path)
                        save_path, _ = QFileDialog.getSaveFileName(
                            self, "シーンを保存", start_dir, "Maya Files (*.ma *.mb)"
                        )
                        if not save_path:
                            return
                        ftype = "mayaAscii" if save_path.lower().endswith(".ma") else "mayaBinary"
                        cmds.file(rename=save_path)
                        cmds.file(save=True, type=ftype)

            cmds.file(path, open=True, force=True)
            self.statusLabel.setText(f"✓  シーンを開きました: {Path(path).name}")
        except ImportError:
            QMessageBox.information(
                self,
                "シーンを開く（スタンドアロンモード）",
                f"Maya コマンド:\n\ncmds.file(\n    r'{path}',\n    open=True,\n    force=True\n)",
                QMessageBox.Ok,
            )
            self.statusLabel.setText(f"[Standalone]  open: {Path(path).name}")

    def _import_scene(self):
        if not self._selected_path:
            return
        path = self._selected_path
        try:
            import maya.cmds as cmds
            cmds.file(path, i=True,
                      type="mayaAscii" if path.lower().endswith(".ma") else "mayaBinary",
                      ignoreVersion=True, mergeNamespacesOnClash=False,
                      namespace=":", options="v=0;")
            self.statusLabel.setText(f"✓  インポートしました: {Path(path).name}")
        except ImportError:
            QMessageBox.information(
                self,
                "インポート（スタンドアロンモード）",
                f"Maya コマンド:\n\ncmds.file(\n    r'{path}',\n    i=True,\n    ignoreVersion=True\n)",
                QMessageBox.Ok,
            )
            self.statusLabel.setText(f"[Standalone]  import: {Path(path).name}")

    @staticmethod
    def _next_version_path(path):
        """ファイル名末尾の数字グループを +1 した、未使用のパスを返す。

        例: An_Emy08_atk01_001.ma -> An_Emy08_atk01_002.ma
        既に存在する番号はスキップし、空いている次の番号まで進める。
        ゼロ埋め桁数は維持する。数字が無ければ None。
        """
        p = Path(path)
        stem, ext = p.stem, p.suffix
        last = None
        for last in re.finditer(r"\d+", stem):
            pass  # 末尾の数字グループを採用
        if last is None:
            return None
        start, end = last.start(), last.end()
        width = len(last.group())
        n = int(last.group()) + 1
        folder = p.parent
        while True:
            new_stem = stem[:start] + str(n).zfill(width) + stem[end:]
            candidate = folder / (new_stem + ext)
            if not candidate.exists():
                return str(candidate)
            n += 1

    def _version_up_save(self):
        """現在のシーンの名前末尾番号を +1 して、同じフォルダにローカルバージョン保存。"""
        try:
            import maya.cmds as cmds
        except ImportError:
            base = self._selected_path or ""
            nxt = self._next_version_path(base) if base else None
            QMessageBox.information(
                self, "VERSION UP（スタンドアロンモード）",
                ("Maya 内で実行すると、現在のシーンを次のバージョンで保存します。\n\n"
                 f"プレビュー:\n{base or '(未選択)'}\n  → {nxt or '(番号なし)'}"),
                QMessageBox.Ok,
            )
            return

        cur = cmds.file(q=True, sceneName=True)
        if not cur:
            QMessageBox.warning(
                self, "VERSION UP",
                "保存済みのシーンがありません。先に名前を付けて保存してください。",
            )
            return
        new_path = self._next_version_path(cur)
        if not new_path:
            QMessageBox.warning(
                self, "VERSION UP",
                f"ファイル名に番号が見つかりませんでした:\n{Path(cur).name}",
            )
            return
        ftype = "mayaAscii" if new_path.lower().endswith(".ma") else "mayaBinary"
        cmds.file(rename=new_path)
        cmds.file(save=True, type=ftype)
        self.statusLabel.setText(f"✓  バージョンアップ保存: {Path(new_path).name}")
        # 現在のルート配下なら一覧を更新して新バージョンを反映
        self._apply_view()

    def _playblast_current(self):
        """現在 Maya で開いているシーンを、手動プルダウンの方式で動画書き出しする。

        手動書き出しは最小間隔を無視して常に実行する。
        """
        try:
            import maya.cmds as cmds
            cur = cmds.file(q=True, sceneName=True) or ""
        except ImportError:
            QMessageBox.information(
                self, "動画書き出し",
                "Maya 内で実行すると、現在のシーンを Pipeline_Movie に書き出します。",
            )
            return
        if not cur:
            self.statusLabel.setText("現在のシーンが未保存です（書き出し先がありません）")
            return

        # 手動書き出しも自動更新のスロットル基準時刻に反映する
        self._last_export_at[os.path.normcase(os.path.normpath(cur))] = time.time()
        method = self.exportMethodCombo.currentData() or "playblast"
        if method == "hardware":
            ok, msg, proc = export_hardware_background(cur)
            self.statusLabel.setText(("🎬  " if ok else "⚠  ") + msg)
            if not ok:
                QMessageBox.warning(self, "ハードウェア書き出し", msg)
            elif proc is not None:
                self._watch_hw_export(cur, proc, label="手動")
            return
        self._playblast(cur)

    # ── ハードウェア書き出し（別プロセス）の完了監視 ─────────────
    def _watch_hw_export(self, scene_path, proc, label=""):
        """別プロセスの書き出し完了をポーリングし、完了時にステータスへログを出す。"""
        if not hasattr(self, "_hw_watchers"):
            self._hw_watchers = []
        timer = QTimer(self)
        started = time.time()

        def _poll():
            # 30分でタイムアウト監視を打ち切る（プロセスは残しても監視だけ終了）
            if proc.poll() is None and (time.time() - started) < 1800:
                return
            timer.stop()
            try:
                self._hw_watchers.remove(timer)
            except Exception:
                pass
            frames = find_scene_sequence(scene_path)
            n = len(frames) if frames else 0
            stem = Path(scene_path).stem
            tag = (label + " ") if label else ""
            if n > 0:
                self.statusLabel.setText(
                    "✅  %s動画書き出し完了: %s（連番 %d 枚）" % (tag, stem, n))
                self.detailPanel.reload_video()
                self._refresh_all_shots_if_open()
            else:
                detail = ""
                try:
                    logp = os.path.join(os.path.dirname(scene_path), VIDEO_SUBDIR,
                                        stem, "_oghw_log.txt")
                    if os.path.isfile(logp):
                        with open(logp, "r", encoding="utf-8", errors="replace") as fh:
                            detail = fh.read().strip().splitlines()[-1:] or [""]
                            detail = detail[0]
                except Exception:
                    pass
                self.statusLabel.setText(
                    "⚠  %s書き出し完了しましたがフレーム未生成（_oghw_log.txt 参照）%s"
                    % (tag, ("／" + detail) if detail else ""))

        timer.timeout.connect(_poll)
        timer.start(1000)
        self._hw_watchers.append(timer)

    def _refresh_all_shots_if_open(self):
        dlg = getattr(self, "_all_shots_dlg", None)
        if dlg is not None:
            try:
                if dlg.isVisible():
                    dlg._update_visible()
            except Exception:
                pass

    def _playblast(self, scene_path):
        """シーンを movies フォルダに「シーン名と同名」でプレイブラスト書き出しする。"""
        try:
            import maya.cmds as cmds
        except ImportError:
            QMessageBox.information(
                self, "プレイブラスト",
                "Maya 内で実行してください（cmds.playblast を使用します）。",
            )
            return

        # 対象シーンが開かれていなければ開く（プレイブラストは現在ビューを撮るため）
        cur = cmds.file(q=True, sceneName=True) or ""
        if os.path.normcase(os.path.normpath(cur)) != os.path.normcase(os.path.normpath(scene_path)):
            r = QMessageBox.question(
                self, "プレイブラスト",
                f"{Path(scene_path).name} を開いてプレイブラストします。よろしいですか？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if r != QMessageBox.Yes:
                return
            self._selected_path = scene_path
            self._open_scene()
            cur = cmds.file(q=True, sceneName=True) or ""
            if os.path.normcase(os.path.normpath(cur)) != os.path.normcase(os.path.normpath(scene_path)):
                return

        import shutil

        stem = Path(scene_path).stem
        # 連番画像はコーデック非依存で確実。movies/<シーン名>/ に出力する。
        seq_dir = os.path.join(os.path.dirname(scene_path), VIDEO_SUBDIR, stem)
        try:
            if os.path.isdir(seq_dir):
                shutil.rmtree(seq_dir, ignore_errors=True)   # 古いフレームを掃除
            os.makedirs(seq_dir, exist_ok=True)
        except Exception as e:
            self.statusLabel.setText(f"⚠  出力フォルダ作成失敗: {e}")
            return

        # 空き容量チェック
        try:
            free_mb = shutil.disk_usage(seq_dir).free / (1024 * 1024)
            if free_mb < 200:
                r = QMessageBox.question(
                    self, "空き容量の警告",
                    f"出力先の空きが残り {free_mb:.0f}MB です。続行しますか？",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if r != QMessageBox.Yes:
                    return
        except Exception:
            pass

        try:
            width = int(cmds.getAttr("defaultResolution.width"))
            height = int(cmds.getAttr("defaultResolution.height"))
            if width <= 0 or height <= 0:
                width, height = 1280, 720
        except Exception:
            width, height = 1280, 720

        seq_base = os.path.join(seq_dir, stem).replace("\\", "/")
        log = []
        ok = False
        for comp in ("jpg", "png"):   # 連番画像（コーデック不要）
            try:
                cmds.playblast(filename=seq_base, format="image", compression=comp,
                               widthHeight=[width, height], percent=100, quality=90,
                               framePadding=4, forceOverwrite=True, viewer=False,
                               showOrnaments=True, clearCache=True)
                frames = find_scene_sequence(scene_path)
                if frames:
                    ok = True
                    log.append(f"  ✓ image/{comp} → {len(frames)} フレーム")
                    break
                log.append(f"  ✗ image/{comp}: フレーム未生成")
            except Exception as e:
                log.append(f"  ✗ image/{comp}: {e}")
                continue

        print("[OG_Pipeline] playblast 試行:\n" + "\n".join(log))

        if not ok:
            detail = "\n".join(log) or "(試行なし)"
            QMessageBox.warning(
                self, "プレイブラスト失敗",
                "連番画像を書き出せませんでした:\n\n" + detail,
            )
            self.statusLabel.setText("⚠  プレイブラスト失敗（詳細はダイアログ参照）")
            return

        self.statusLabel.setText(
            f"✅  動画書き出し完了: {Path(scene_path).stem}"
            f"（連番 {len(find_scene_sequence(scene_path))} 枚 → {seq_dir}）")
        self.detailPanel.reload_video()   # 選択中シーンならサイドバーで連番再生
        self._refresh_all_shots_if_open()

    def _open_in_explorer(self):
        """選択中のシーンのフォルダを OS のファイラで開く（ファイルを選択状態にする）。"""
        if not self._selected_path:
            return
        if reveal_in_explorer(self._selected_path):
            self.statusLabel.setText(
                f"📂  フォルダを開きました: {Path(self._selected_path).parent}"
            )
        else:
            self.statusLabel.setText("⚠  フォルダを開けませんでした")

    def _save_scene_as(self):
        """現在開いているシーンを、そのシーンのフォルダを既定にして別名保存する。

        Maya のホットキー／プロジェクトは変更しない。ツール内の保存ダイアログだけ、
        開いているシーンのフォルダを開始位置にする（Ctrl+Shift+S のツール版）。
        """
        try:
            import maya.cmds as cmds
        except ImportError:
            QMessageBox.information(
                self, "SAVE AS（スタンドアロンモード）",
                "Maya 内で実行すると、現在のシーンのフォルダを既定にした\n"
                "保存ダイアログ（fileDialog2）を表示します。",
                QMessageBox.Ok,
            )
            return

        # 開始フォルダの優先順位:
        #   1) 現在開いているシーンのフォルダ
        #   2) ツールで最後に選択したファイルのフォルダ
        #   3) 現在のワークスペース
        cur = cmds.file(q=True, sceneName=True)
        start = ""
        if cur:
            start = os.path.dirname(cur)
        elif self._selected_path:
            start = os.path.dirname(self._selected_path)
        if not start:
            try:
                start = cmds.workspace(q=True, dir=True)
            except Exception:
                start = ""

        res = cmds.fileDialog2(
            fileMode=0,                      # 保存（存在しないファイル名も可）
            caption="Save Scene As",
            startingDirectory=start,
            fileFilter="Maya ASCII (*.ma);;Maya Binary (*.mb)",
        )
        if not res:
            return
        save_path = res[0]
        ftype = "mayaAscii" if save_path.lower().endswith(".ma") else "mayaBinary"
        cmds.file(rename=save_path)
        cmds.file(save=True, type=ftype)
        self.statusLabel.setText(f"✓  保存しました: {Path(save_path).name}")

    def _save_new_scene(self):
        """現在ブラウザでリーブ中のフォルダを既定にして、新規シーンを保存する。"""
        try:
            import maya.cmds as cmds
        except ImportError:
            QMessageBox.information(
                self, "SAVE NEW SCENE（スタンドアロンモード）",
                "Maya 内で実行すると、現在リーブ中のフォルダを既定にした\n"
                "保存ダイアログを表示し、新規シーンとして保存します。",
                QMessageBox.Ok,
            )
            return

        # 保存先フォルダ: リーブ中フォルダ → 選択中ファイルのフォルダ → ルート
        start = self._current_folder or ""
        if not start and self._selected_path:
            start = os.path.dirname(self._selected_path)
        if not start and self.active_root:
            start = str(self.active_root)
        if not start or not os.path.isdir(start):
            self.statusLabel.setText("保存先フォルダが未確定です（ブラウザでフォルダを選択してください）")
            return

        # 未保存の変更があれば確認（新規シーン作成で破棄されるため）
        if cmds.file(q=True, modified=True):
            r = QMessageBox.question(
                self, "新規シーン",
                "現在のシーンに未保存の変更があります。\n"
                "新規シーンを作成すると失われます。続行しますか？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if r != QMessageBox.Yes:
                return

        res = cmds.fileDialog2(
            fileMode=0,
            caption="Save New Scene",
            startingDirectory=start,
            fileFilter="Maya ASCII (*.ma);;Maya Binary (*.mb)",
        )
        if not res:
            return
        save_path = res[0]
        ftype = "mayaAscii" if save_path.lower().endswith(".ma") else "mayaBinary"
        try:
            cmds.file(new=True, force=True)          # 新規シーン
            cmds.file(rename=save_path)
            cmds.file(save=True, type=ftype)
        except Exception as e:
            self.statusLabel.setText("⚠  新規保存に失敗しました: %s" % e)
            QMessageBox.warning(self, "新規保存失敗", str(e))
            return
        self.statusLabel.setText(f"✓  新規シーンを保存しました: {Path(save_path).name}")
        self._apply_view()   # 一覧に反映
        # 保存したフォルダまでブラウザを展開
        try:
            self.browser.reveal_path(save_path)
        except Exception:
            pass


# ─── エントリーポイント ────────────────────────────────────────────────────────
# 【使い方】
#   1) このファイルを Maya 標準の scripts フォルダに OG_Pipeline.py として保存。
#        Windows : <ドキュメント>/maya/scripts/   または  /maya/<version>/scripts/
#        macOS   : ~/Library/Preferences/Autodesk/maya/scripts/
#      ※ このフォルダは Maya 起動時に自動で sys.path に入るため、パス指定は不要。
#
#   2) Maya スクリプトエディタ（またはシェルフボタン）から下記を実行:
#
#       import importlib, OG_Pipeline
#       importlib.reload(OG_Pipeline)
#       OG_Pipeline.main()
#
#   3) 初回は [プロジェクト設定] でプロジェクトルートを登録（または [⭳ インポート] で JSON を取込）。
#      [★ 次回も使用] を押すと、そのルートが次回起動時に自動で選択される。
#
# 【重要】QApplication は絶対に新規作成しない。
#         Maya はすでに独自の QApplication を持っており、
#         二重に作成するとクラッシュ・再起動の原因になります。

def _get_maya_main_window():
    """Maya メインウィンドウを QWidget として取得する（親設定用）。"""
    try:
        import maya.OpenMayaUI as omui
        try:
            from shiboken2 import wrapInstance
        except ImportError:
            from shiboken6 import wrapInstance
        ptr = omui.MQtUtil.mainWindow()
        if ptr is not None:
            return wrapInstance(int(ptr), QWidget)
    except Exception:
        pass
    return None


def _close_existing_windows():
    """既存の OG_Pipeline ウィンドウを objectName で検出して閉じる。

    importlib.reload するとクラスが再定義され isinstance では旧ウィンドウを
    検出できないため、文字列の objectName で判定する（reload 耐性）。
    戻り値: 閉じた数。
    """
    app = QApplication.instance()
    if app is None:
        return 0
    closed = 0
    for widget in app.topLevelWidgets():
        try:
            if widget.objectName() == WINDOW_OBJECT_NAME:
                widget.close()
                widget.deleteLater()
                closed += 1
        except Exception:
            pass
    return closed


def main():
    """
    外部から呼び出す公開関数。
    既存ウィンドウがあれば閉じてから1つだけ開く（多重起動・reload による重複を防止）。
    """
    if QApplication.instance() is None:
        print("[OG_Pipeline] エラー: Maya のスクリプトエディタから実行してください。")
        return None

    _close_existing_windows()

    maya_main = _get_maya_main_window()
    win = OGPipelineWindow(parent=maya_main)
    win.show()
    win.raise_()
    win.activateWindow()
    return win


# ─── スタンドアロン実行（Maya 外での単体テスト用） ──────────────────────────────
# 注意: ファイル全体を Maya のスクリプトエディタに貼り付けて実行すると __name__ は
# "__main__" になる。その場合 sys.exit(app.exec_()) を実行すると Maya 上で
# SystemExit が発生するため、Maya 内では main() を呼ぶだけにする。
if __name__ == "__main__":
    try:
        import maya.cmds as _cmds  # noqa: F401
        _in_maya = True
    except ImportError:
        _in_maya = False

    if _in_maya:
        main()
    else:
        app = QApplication.instance() or QApplication(sys.argv)
        window = OGPipelineWindow()
        window.show()
        sys.exit(app.exec_() if hasattr(app, "exec_") else app.exec())
