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
        QDialog, QDialogButtonBox, QGridLayout, QCheckBox, QSpinBox, QFormLayout,
        QStackedWidget, QPlainTextEdit, QTableWidget, QTableWidgetItem, QHeaderView,
        QSlider
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
SHOTLIST_OBJECT_NAME = "OGPipelineShotListWindow"    # ショットリスト単独起動用の識別名
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
    """フォルダ配下の Pipeline_Movie 内で最新の連番画像を返す。無ければ None。

    連番は Pipeline_Movie フォルダ配下のみを対象とする（他フォルダの連番＝別カットの
    レンダ連番などは検出しない）。最も新しいフレームを含むフォルダの連番を返す。
    """
    if not folder or not os.path.isdir(folder):
        return None
    sep = os.sep
    best = None  # (mtime, dirpath)
    for cur, dirs, files in os.walk(folder):
        # Pipeline_Movie 配下でなければ連番は見ない（走査は続けるが対象外）
        if VIDEO_SUBDIR not in os.path.normpath(cur).replace("/", sep).split(sep):
            continue
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


def stage_container(shot_folder, stage_subpath=""):
    """工程フォルダが入っている実フォルダ（単一・ワイルドカード無し用）。"""
    if stage_subpath:
        return os.path.join(shot_folder, *stage_subpath.replace("\\", "/").split("/"))
    return shot_folder


def _parse_subpath_items(stage_subpath):
    """改行/カンマ区切りの各サブパスを (pattern, name) に分解する。

    "pattern = name" 形式で各サブパスに表示名を付けられる（name 省略可）。
    """
    items = []
    for raw in re.split(r"[\n,]", stage_subpath or ""):
        raw = raw.strip()
        if not raw:
            continue
        if "=" in raw:
            pat, nm = raw.split("=", 1)
            items.append((pat.strip(), nm.strip()))
        else:
            items.append((raw, ""))
    return items


def _glob_one(shot_folder, pat):
    """単一サブパス（`*` 可）を shot_folder 起点で展開し、存在フォルダ群を返す。"""
    cur = [shot_folder]
    for seg in [s for s in pat.replace("\\", "/").split("/") if s]:
        nxt = []
        for d in cur:
            if seg == "*":
                try:
                    for name in sorted(os.listdir(d)):
                        full = os.path.join(d, name)
                        if os.path.isdir(full) and name != VIDEO_SUBDIR:
                            nxt.append(full)
                except Exception:
                    pass
            else:
                full = os.path.join(d, seg)
                if os.path.isdir(full):
                    nxt.append(full)
        cur = nxt
    return cur


def expand_stage_bases(shot_folder, stage_subpath=""):
    """サブパスが指すフォルダ群を返す（複数指定・ワイルドカード対応、重複除去）。"""
    items = _parse_subpath_items(stage_subpath)
    if not items:
        return [shot_folder]
    seen, out = set(), []
    for pat, _nm in items:
        for d in _glob_one(shot_folder, pat):
            k = os.path.normcase(os.path.normpath(d))
            if k not in seen:
                seen.add(k)
                out.append(d)
    return out


def expand_stage_bases_named(shot_folder, stage_subpath=""):
    """[(base_dir, label), ...]。label = サブパスに付けた名前、無ければ末尾フォルダ名。"""
    items = _parse_subpath_items(stage_subpath)
    if not items:
        base = shot_folder
        return [(base, os.path.basename(base.rstrip("/\\")))]
    seen, out = set(), []
    for pat, nm in items:
        for d in _glob_one(shot_folder, pat):
            k = os.path.normcase(os.path.normpath(d))
            if k in seen:
                continue
            seen.add(k)
            out.append((d, nm if nm else os.path.basename(d.rstrip("/\\"))))
    return out


def shot_stage_list(shot_folder, stage_subpath=""):
    """ショットの各工程フォルダ（lay, anm 等）の最新メディアを返す。

    戻り値: [(stage_name, media, mtime), ...]（mtime 昇順）。
    複数ベース（ワイルドカード/複数サブパス）のときは工程名を「<親>/<工程>」で表す。
    """
    out = []
    bases = expand_stage_bases(shot_folder, stage_subpath)
    multi = len(bases) > 1
    for base in bases:
        parent = os.path.basename(base.rstrip("/\\"))
        try:
            for d in sorted(os.listdir(base)):
                full = os.path.join(base, d)
                if not os.path.isdir(full) or d == VIDEO_SUBDIR:
                    continue
                media = pick_folder_media(full)
                if media:
                    label = ("%s/%s" % (parent, d)) if multi else d
                    out.append((label, media, _media_mtime(media)))
        except Exception:
            pass
    out.sort(key=lambda s: s[2])
    return out


def _stage_rank(name):
    """工程の表示順。lay 系 → anm 系 → その他、の順（同順位は名前順）。"""
    low = name.lower()
    if low.startswith("lay") or low.startswith("layout"):
        r = 0
    elif low.startswith("anm") or low.startswith("anim"):
        r = 1
    else:
        r = 2
    return (r, low)


def stage_has_scene(stage_folder):
    """工程フォルダ配下に Maya シーン(.ma/.mb)があるか（再帰、見つけ次第 True）。"""
    try:
        for cur, dirs, files in os.walk(stage_folder):
            for f in files:
                if os.path.splitext(f)[1].lower() in MAYA_EXTENSIONS:
                    return True
    except Exception:
        pass
    return False


def stage_latest_scene_mtime(stage_folder):
    """工程フォルダ配下の Maya シーン(.ma/.mb)の最新更新日時。無ければ None。"""
    best = None
    try:
        for cur, dirs, files in os.walk(stage_folder):
            for f in files:
                if os.path.splitext(f)[1].lower() in MAYA_EXTENSIONS:
                    try:
                        m = os.path.getmtime(os.path.join(cur, f))
                    except Exception:
                        continue
                    if best is None or m > best:
                        best = m
    except Exception:
        pass
    return best


def shot_stage_scene_list(shot_folder, stage_subpath=""):
    """ショットの全工程フォルダと、各工程にシーンファイルがあるかを返す。

    戻り値: [(stage_name, stage_folder, has_scene), ...]（工程順）。
    工程フォルダ＝ <ショット>/<stage_subpath>/ 直下のサブフォルダ（Pipeline_Movie 除外）。
    """
    out = []
    bases = expand_stage_bases(shot_folder, stage_subpath)
    multi = len(bases) > 1
    for base in bases:
        parent = os.path.basename(base.rstrip("/\\"))
        try:
            for d in os.listdir(base):
                full = os.path.join(base, d)
                if not os.path.isdir(full) or d == VIDEO_SUBDIR:
                    continue
                label = ("%s/%s" % (parent, d)) if multi else d
                out.append((label, full, stage_has_scene(full)))
        except Exception:
            pass
    out.sort(key=lambda s: _stage_rank(s[0].split("/")[-1]))
    return out


# 命名規則トークン: 例 test_ep01_sh001_lay_pri_t01_v001 の _t01（テイク）/ _v001（ローカル）
# 接頭辞（t / v）と桁数（2 / 3）はプロジェクト設定で変更できる。区切りは '_' 固定。
DEFAULT_TAKE_PREFIX = "t"
DEFAULT_LOCAL_PREFIX = "v"
DEFAULT_TAKE_DIGITS = 2
DEFAULT_LOCAL_DIGITS = 3


def _clamp_digits(value, default):
    """桁数を 1〜6 に収める。数値化できなければ default。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(6, n))


def make_token_re(prefix):
    """区切り '_' + 接頭辞 のトークン正規表現を作る。例 prefix='t' → (_t)(\\d+)。"""
    return re.compile(r"(_%s)(\d+)" % re.escape(prefix or ""), re.I)


# 既定（接頭辞 t / v）。プロジェクト未設定時のフォールバック。
TAKE_RE = make_token_re(DEFAULT_TAKE_PREFIX)
LOCAL_RE = make_token_re(DEFAULT_LOCAL_PREFIX)


def _parse_version_prefix(field):
    """工程のテイク/ローカル欄文字列から接頭辞を得る。
    't01'→'t' / 'take01'→'take' / 'C001'→'C' / 't'→'t'（数字なしでも接頭辞として扱う）。
    空、または数字だけ（接頭辞なし）の場合は None。"""
    field = (field or "").strip()
    if not field:
        return None
    m = re.match(r"^(.*?)(\d*)$", field)   # 末尾の数字は任意
    prefix = (m.group(1) if m else field).strip()
    return prefix or None


def version_token_re(stages, field, default_prefix):
    """工程リストの field（'take'/'local'）欄の接頭辞からトークン正規表現を作る。

    各工程のテイク/ローカル欄に入力された接頭辞（複数可）を集め、'_接頭辞数字' に
    一致する正規表現を返す。接頭辞が1つも無ければ default_prefix を使う。
    これにより _t## 固定でなく、プロジェクトの表記（take## / C### 等）でも採番できる。
    """
    prefixes = []
    for s in (stages or []):
        p = _parse_version_prefix((s or {}).get(field, ""))
        if p:
            prefixes.append(p)
    if not prefixes:
        prefixes = [default_prefix]
    alt = "|".join(re.escape(p) for p in sorted(set(prefixes), key=len, reverse=True))
    return re.compile(r"(_(?:%s))(\d+)" % alt, re.I)


def _normalize_token_str(value, digits):
    """'t1' → 't01' のように末尾の数字を digits 桁にゼロ埋め。数字が無ければそのまま。"""
    value = (value or "").strip()
    m = re.search(r"^(.*?)(\d+)\s*$", value)
    if not m or not digits:
        return value
    return m.group(1) + str(int(m.group(2))).zfill(digits)


def bump_version_token(stem, regex, digits=None):
    """stem 内の最後のトークン番号を +1。digits 指定時はその桁数で正規化（未指定は現桁数維持）。
    戻り値: (新stem, 変更したか)。"""
    matches = list(regex.finditer(stem))
    if not matches:
        return stem, False
    m = matches[-1]
    width = digits or len(m.group(2))
    n = int(m.group(2)) + 1
    new = m.group(1) + str(n).zfill(width)
    return stem[:m.start()] + new + stem[m.end():], True


def set_version_token(stem, regex, value, digits=None):
    """stem 内の最後のトークンを value（例 't01' / 'v001'）に置換。
    digits 指定時は value の数字をその桁数に正規化する。戻り値: (新stem, 変更したか)。"""
    if not value:
        return stem, False
    if digits:
        value = _normalize_token_str(value, digits)
    matches = list(regex.finditer(stem))
    if not matches:
        return stem, False
    m = matches[-1]
    return stem[:m.start()] + "_" + value + stem[m.end():], True


def shot_folder_of(scene_path, shots_parent):
    """scene_path の祖先のうち、shots_parent の直下にあるフォルダ（＝ショット）を返す。"""
    if not scene_path or not shots_parent:
        return None
    sp = os.path.normcase(os.path.normpath(str(shots_parent)))
    cur = os.path.normpath(os.path.dirname(str(scene_path)))
    while True:
        parent = os.path.dirname(cur)
        if os.path.normcase(parent) == sp:
            return cur
        if parent == cur:
            return None
        cur = parent


def resolve_stage_dir(stage, shot_folder, stage_subpath=""):
    """工程の保存先フォルダを解決する。

    stage['folder'] は「サブパス起点」の相対パスとして扱う:
      - 起点(base) = サブパスがあれば <ショット>/<サブパス>、無ければ <ショット>
      - 保存先 = base / stage['folder']
    stage['folder'] が絶対パスならそのまま使う。
    stage['folder'] 未設定なら base/<工程名> にフォールバックする。
    """
    folder = (stage.get("folder") or "").strip()
    # 絶対パス判定はドライブ(N:)/UNC(\\server) のみ。先頭 / の単独は相対扱い（無視）。
    drive, _ = os.path.splitdrive(folder)
    is_unc = folder.startswith("\\\\") or folder.startswith("//")
    if folder and (drive or is_unc):
        return os.path.normpath(folder)
    folder = folder.lstrip("/\\")   # 先頭スラッシュは無視（付いていても相対として扱う）

    # 起点(base): サブパスがあれば <ショット>/<サブパス>、無ければショットフォルダ自身
    base = shot_folder or ""
    if stage_subpath:
        base = (os.path.join(shot_folder, *stage_subpath.replace("\\", "/").split("/"))
                if shot_folder else stage_subpath)

    if folder:
        parts = folder.replace("\\", "/").split("/")
        return os.path.normpath(os.path.join(base, *parts)) if base else os.path.normpath(folder)
    # folder 未設定 → base/<工程名>
    if base:
        return os.path.normpath(os.path.join(base, stage.get("name", "")))
    return ""


def apply_stage_rename(stem, stage, all_stages=None):
    """工程の置換規則・初期テイク/ローカルを stem に適用した新しい stem を返す。

    置換は現シーン名基準: 工程リストの各トークン（リネーム先／工程名）のうち
    現在の stem に含まれるものを検出し、対象工程のトークンへ置換する（最長一致優先）。
    見つからなければ、対象工程の「リネーム元→リネーム先」でフォールバックする。
    テイク/ローカルは欄に数字が入っているときだけ、その値で初期化する（空欄なら据え置き）。
    採番対象の接頭辞は各工程のテイク/ローカル欄から導く（_t## 固定でなく汎用）。
    戻り値: (新stem, replaced)。replaced は工程トークンの置換が行われたか。
    """
    new = stem
    target = (stage.get("rename_to") or stage.get("name") or "").strip()
    rf = (stage.get("rename_from") or "").strip()

    replaced = False
    if rf:
        # リネーム元が明示されている → これを唯一の基準にする（大文字小文字も厳密）。
        # 見つからなければ置換しない（呼び出し側で中断・警告）。誤った自動置換で
        # 取り違えたまま保存されるのを防ぐ。
        if target and rf in new:
            new = new.replace(rf, target)
            replaced = True
    elif target and all_stages:
        # リネーム元 未設定のときだけ、他工程のトークンを自動検出して置換する。
        tokens = []
        for s in all_stages:
            for tok in (s.get("rename_to"), s.get("name")):
                tok = (tok or "").strip()
                if tok and tok != target and tok in new:
                    tokens.append(tok)
        for tok in sorted(set(tokens), key=len, reverse=True):
            new = new.replace(tok, target)
            replaced = True
            break

    # テイク/ローカルは「数字入りの値」が入っている工程のときだけ初期化する。
    if re.search(r"\d", stage.get("take") or ""):
        take_re = version_token_re(all_stages, "take", DEFAULT_TAKE_PREFIX)
        new, _ = set_version_token(new, take_re, stage["take"])
    if re.search(r"\d", stage.get("local") or ""):
        local_re = version_token_re(all_stages, "local", DEFAULT_LOCAL_PREFIX)
        new, _ = set_version_token(new, local_re, stage["local"])
    return new, replaced



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


# cv2 の import は、保留アンインストール処理を先に走らせるため、
# 全関数定義の後（ファイル末尾の _cv2_startup()）で行う。


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


def uninstall_opencv():
    """opencv-python(-headless) を pip でアンインストールする。戻り値: (成功, ログ)。

    既に import 済みの cv2 は現セッションでは解放されないため、無効化は
    Maya 再起動後に反映される（呼び出し側で案内する）。
    """
    exe = _find_mayapy()
    if not exe:
        return False, "mayapy が見つかりませんでした。手動で `mayapy -m pip uninstall -y opencv-python-headless` を実行してください。"
    logs, ok_any = [], False
    for pkg in ("opencv-python-headless", "opencv-python"):
        try:
            proc = subprocess.run(
                [exe, "-m", "pip", "uninstall", "-y", pkg],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600,
            )
            out = (proc.stdout or b"").decode("utf-8", "replace")[-1000:]
            logs.append("$ pip uninstall %s\n%s" % (pkg, out))
            if proc.returncode == 0 and "not installed" not in out.lower():
                ok_any = True
        except Exception as e:
            logs.append("%s: %s" % (pkg, e))
    cl = _cleanup_cv2_leftovers()
    if cl:
        logs.append("掃除: " + ", ".join(cl))
        ok_any = True
    return ok_any, "\n".join(logs)


def _cleanup_cv2_leftovers():
    """中断した pip uninstall が残す壊れたフォルダ/配布情報を掃除する。

    例: '~v2'（cv2.pyd の残骸）、先頭が '-' の不正 dist（'-pencv-python-headless'）。
    ロード中の cv2.pyd は削除できないため、cv2 を import していない起動直後に有効。
    戻り値: 削除できた項目名のリスト。
    """
    removed = []
    try:
        import site
        dirs = []
        if hasattr(site, "getusersitepackages"):
            us = site.getusersitepackages()
            if isinstance(us, str):
                dirs.append(us)
        for sp in dirs:
            if not sp or not os.path.isdir(sp):
                continue
            for name in os.listdir(sp):
                low = name.lower()
                # pip が残す壊れた項目は先頭が '~' か '-'。cv2/opencv 関連だけ対象にする。
                broken = name.startswith("~") or name.startswith("-")
                related = ("cv2" in low or "v2" in low or "pencv" in low
                           or "opencv" in low)
                if broken and related:
                    full = os.path.join(sp, name)
                    try:
                        if os.path.isdir(full):
                            shutil.rmtree(full, ignore_errors=True)
                        else:
                            os.remove(full)
                        if not os.path.exists(full):
                            removed.append(name)
                    except Exception:
                        pass
    except Exception:
        pass
    return removed


_MAYA_FPS_UNITS = {
    "game": 15.0, "film": 24.0, "pal": 25.0, "ntsc": 30.0,
    "show": 48.0, "palf": 50.0, "ntscf": 60.0,
}


def maya_scene_fps(default=24.0):
    """現在開いている Maya シーンの再生 FPS を返す（取得不可なら default）。

    時間単位は 'film'/'ntsc' 等の別名と '30fps'/'23.976fps' 等の数値表記の両方に対応。
    """
    try:
        import maya.cmds as cmds
        unit = cmds.currentUnit(q=True, time=True)
    except Exception:
        return default
    if not unit:
        return default
    unit = str(unit).strip().lower()
    if unit in _MAYA_FPS_UNITS:
        return _MAYA_FPS_UNITS[unit]
    m = re.match(r"^([\d.]+)\s*fps$", unit)
    if m:
        try:
            v = float(m.group(1))
            return v if v > 0 else default
        except Exception:
            return default
    return default


class Cv2VideoThread(QThread):
    """cv2 で mp4 をバックグラウンドデコードし、縮小済みフレームを QImage で通知する。

    GUI スレッドをブロックしないため UI が固まらない。max_w で解像度を落として
    デコード後にリサイズ（描画コスト・転送量を削減）。
    """
    frameReady = Signal(object)   # QImage

    def __init__(self, path, max_w=640, fps=None, fallback_fps=24.0, parent=None):
        super().__init__(parent)
        self._path = path
        self._max_w = int(max_w) if max_w else 0
        self._fps = fps                       # 明示指定（あれば最優先）
        self._fallback_fps = float(fallback_fps) if fallback_fps else 24.0
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
        # mp4 の埋め込み FPS（＝書き出し時のシーン FPS）を優先。欠落/無効なら
        # シーン FPS のフォールバックを使う（24 固定にしない）。
        src = cap.get(cv2.CAP_PROP_FPS)
        fps = self._fps or (src if (src and src > 0) else self._fallback_fps)
        if not fps or fps <= 0:
            fps = self._fallback_fps
        fps = min(float(fps), 120.0)
        delay = max(8, int(1000.0 / fps))
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


# ── 動画フレームの共有キャッシュ（クリップを一度だけ縮小デコードしRAMからループ）──
# 多数のセルが個別に cv2 で連続デコードすると重い。クリップを1回だけ縮小デコードして
# QImage 列にキャッシュし、各セルはそこからループ表示する（デコードは1回・複数セルで共有）。
_FRAME_CACHE = {}            # key -> list[QImage]（挿入順＝LRU）
_FRAME_CACHE_BYTES = [0]
_FRAME_CACHE_MAX = 320 * 1024 * 1024   # 約320MB上限
_FRAME_PENDING = {}          # key -> {"thread": QThread, "cbs": [fn, ...]}
_LIVE_DECODE_THREADS = set()
_FRAME_MAX_COUNT = 240       # 1クリップの最大保持フレーム数（メモリ・時間の上限）


def _frame_key(path, max_w):
    try:
        mt = int(os.path.getmtime(path))
    except Exception:
        mt = 0
    return (os.path.normcase(os.path.normpath(str(path))), mt, int(max_w or 0))


def _img_bytes(im):
    for attr in ("sizeInBytes", "byteCount"):
        fn = getattr(im, attr, None)
        if fn:
            try:
                return int(fn())
            except Exception:
                pass
    try:
        return im.width() * im.height() * 4
    except Exception:
        return 0


def _frame_cache_get(key):
    fr = _FRAME_CACHE.pop(key, None)
    if fr is not None:
        _FRAME_CACHE[key] = fr   # LRU: 末尾へ
    return fr


def _frame_cache_put(key, frames):
    if not frames:
        return
    _FRAME_CACHE[key] = frames
    _FRAME_CACHE_BYTES[0] += sum(_img_bytes(im) for im in frames)
    while _FRAME_CACHE_BYTES[0] > _FRAME_CACHE_MAX and len(_FRAME_CACHE) > 1:
        old_key = next(iter(_FRAME_CACHE))
        old = _FRAME_CACHE.pop(old_key)
        _FRAME_CACHE_BYTES[0] -= sum(_img_bytes(im) for im in old)


class FrameDecodeThread(QThread):
    """クリップを一度だけ縮小デコードして QImage 列を作り、完了時に done を1回発火する。"""
    done = Signal(object, object)   # key, list[QImage]

    def __init__(self, path, key, max_w, max_frames=_FRAME_MAX_COUNT, parent=None):
        super().__init__(parent)
        self._path = path
        self._key = key
        self._max_w = int(max_w) if max_w else 0
        self._max_frames = int(max_frames)
        self._running = True

    def run(self):
        frames = []
        try:
            import cv2
        except Exception:
            self.done.emit(self._key, frames)
            return
        cap = cv2.VideoCapture(self._path)
        if cap.isOpened():
            try:
                while self._running and len(frames) < self._max_frames:
                    ok, frame = cap.read()
                    if not ok:
                        break
                    h, w = frame.shape[:2]
                    if self._max_w and w > self._max_w:
                        nh = max(1, int(h * self._max_w / float(w)))
                        frame = cv2.resize(frame, (self._max_w, nh))
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    hh, ww = rgb.shape[:2]
                    frames.append(QImage(rgb.data, ww, hh, 3 * ww,
                                         QImage.Format_RGB888).copy())
            except Exception:
                pass
            finally:
                try:
                    cap.release()
                except Exception:
                    pass
        self.done.emit(self._key, frames if self._running else [])

    def stop(self):
        self._running = False


_DECODE_MAX_ACTIVE = 3       # 同時デコード本数の上限（表示数は制限しない・CPU突入抑制）
_DECODE_ACTIVE = [0]


def _on_decode_done(key, frames):
    if frames:
        _frame_cache_put(key, frames)
    pend = _FRAME_PENDING.pop(key, None)
    if pend is not None:
        if pend.get("started"):
            _DECODE_ACTIVE[0] = max(0, _DECODE_ACTIVE[0] - 1)
        for cb in pend["cbs"]:
            try:
                cb(frames)
            except Exception:
                pass
    _pump_decode_queue()


def _drop_decode_thread(th):
    _LIVE_DECODE_THREADS.discard(th)
    try:
        th.deleteLater()
    except Exception:
        pass


def _pump_decode_queue():
    """未起動の保留デコードを、同時実行上限まで順に起動する（バースト抑制）。"""
    for pend in _FRAME_PENDING.values():
        if _DECODE_ACTIVE[0] >= _DECODE_MAX_ACTIVE:
            break
        if not pend.get("started"):
            pend["started"] = True
            _DECODE_ACTIVE[0] += 1
            try:
                pend["thread"].start()
            except Exception:
                _DECODE_ACTIVE[0] = max(0, _DECODE_ACTIVE[0] - 1)


def request_video_frames(path, max_w, on_ready):
    """path の縮小フレーム列を取得。キャッシュにあれば即 on_ready(frames)。無ければ
    デコードを予約する。同時デコードは _DECODE_MAX_ACTIVE 本までに絞り、残りは順番待ち
    にして起動時/スクロール時の CPU バーストを防ぐ（表示数そのものは制限しない）。"""
    key = _frame_key(path, max_w)
    fr = _frame_cache_get(key)
    if fr is not None:
        on_ready(fr)
        return
    pend = _FRAME_PENDING.get(key)
    if pend is not None:
        pend["cbs"].append(on_ready)
        return
    th = FrameDecodeThread(path, key, max_w)
    _FRAME_PENDING[key] = {"thread": th, "cbs": [on_ready], "started": False}
    th.done.connect(_on_decode_done)
    th.finished.connect(lambda _th=th: _drop_decode_thread(_th))
    _LIVE_DECODE_THREADS.add(th)
    _pump_decode_queue()


def stop_all_decodes():
    """保留中のデコードスレッドを停止する（ウィンドウ閉時などのクリーンアップ）。"""
    for pend in list(_FRAME_PENDING.values()):
        try:
            pend["thread"].stop()
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
    """任意の入力を [{'name','path','shots_parent','stage_subpath'}, ...] に正規化する。"""
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
            # 各ショット内で工程フォルダが入っているサブパス（相対）。
            # 例: "ma" → <ショット>/ma/<工程>/ 。空＝ショット直下に工程フォルダ。
            "stage_subpath": str(e.get("stage_subpath", "")).strip().strip("/\\"),
            # サブパスが表す対象の呼称（UI 表示用。例: "キャラ"）。空なら既定表示。
            "subpath_label": str(e.get("subpath_label", "")).strip(),
            # 工程リスト（任意）。設定があれば工程ベースの保存に使う。
            "stages": _normalize_stages(e.get("stages")),
        })
    return out


def _normalize_stages(data):
    """工程リストを正規化する。

    各要素: {name, folder, rename_from, rename_to, take, local}
      - name        … 工程名（例: lay_pri）
      - folder      … ショットフォルダからの相対パス（例: ma/lay_pri）。絶対パスも可。
      - rename_from … Scene 名の置換元トークン（例: lay_pri）
      - rename_to   … 置換先トークン（例: anm_sec）
      - take        … テイクバージョン初期値（例: t01）
      - local       … ローカルバージョン初期値（例: v001）
    """
    out = []
    if not isinstance(data, list):
        return out
    for e in data:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name", "")).strip()
        if not name:
            continue
        out.append({
            "name": name,
            "folder": str(e.get("folder", "")).strip(),
            "rename_from": str(e.get("rename_from", "")).strip(),
            "rename_to": str(e.get("rename_to", "")).strip() or name,
            "take": str(e.get("take", "")).strip(),
            "local": str(e.get("local", "")).strip(),
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


def add_root(name, path, shots_parent="", stage_subpath="", stages=None,
             subpath_label=""):
    """ルートを追加（同名は上書き）し、名前順で保存する。"""
    roots = [r for r in load_roots() if r["name"] != name]
    roots.append({"name": name, "path": path,
                  "shots_parent": (shots_parent or path),
                  "stage_subpath": (stage_subpath or "").strip().strip("/\\"),
                  "subpath_label": (subpath_label or "").strip(),
                  "stages": _normalize_stages(stages or [])})
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


def get_pending_cv2_uninstall():
    """次回起動時に cv2 をアンインストールする予約があるか。"""
    return bool(_read_config().get("pending_cv2_uninstall", False))


def set_pending_cv2_uninstall(value):
    cfg = _read_config()
    cfg["pending_cv2_uninstall"] = bool(value)
    _write_config(cfg)


def get_grid_cols():
    try:
        return min(7, max(3, int(_read_config().get("allshots_grid_cols", 5))))
    except Exception:
        return 5


def set_grid_cols(n):
    cfg = _read_config()
    cfg["allshots_grid_cols"] = min(7, max(3, int(n)))
    _write_config(cfg)


def get_list_rows():
    try:
        return min(20, max(5, int(_read_config().get("allshots_list_rows", 10))))
    except Exception:
        return 8


def set_list_rows(n):
    cfg = _read_config()
    cfg["allshots_list_rows"] = min(20, max(5, int(n)))
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
        self._seq_fps = 24
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
    def set_sequence(self, frames, fps=None):
        self._stop_all()
        self._path = None
        self._frames = list(frames or [])
        self._idx = 0
        # 連番は FPS メタを持たないので、指定が無ければシーン FPS で再生する。
        self._seq_fps = max(1, int(fps if fps else maya_scene_fps()))
        self._openBtn.hide()
        if not self._frames:
            self.clear_player()
            return
        self._placeholder.hide()
        self._frameLabel.show()
        self._counter.show()
        self._show_frame(0)
        if len(self._frames) > 1:
            self._timer.start(max(1, int(1000 / max(1, self._seq_fps))))

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
            # 直前のフレーム/外部ボタンを消してから「読み込み中」だけを表示
            # （前のフレーム＋読み込み中＋外部ボタンが重なってチカつくのを防ぐ）
            self._frameLabel.hide()
            self._frameLabel.clear()
            self._counter.hide()
            self._openBtn.hide()
            self._placeholder.setText("動画を読み込み中…")
            self._placeholder.show()
            self._cv_thread = Cv2VideoThread(path, max_w=640,
                                             fallback_fps=maya_scene_fps(), parent=self)
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

    def suspend(self):
        """再生を止めて cv2 等のファイルロックを解放する（_path/_frames は保持）。"""
        self._stop_all()

    def resume(self):
        """suspend 後、保持している動画/連番の再生を再開する。"""
        # 既存の再生を必ず止めてから再開する。resume が二重に呼ばれても cv2 スレッドが
        # 積み重なって複数スレッドが同じ画面へ描画＝映像が暴れるのを防ぐ。
        self._stop_all()
        if self._path and _HAS_CV2:
            self._got_frame = False
            self._video_token += 1
            self._start_cv2(self._path)
        elif self._frames and len(self._frames) > 1:
            self._timer.start(max(1, int(1000 / max(1, self._seq_fps))))


class IdleReleaseMonitor(QtCore.QObject):
    """一定時間ユーザー操作が無ければ on_idle() を、操作再開で on_active() を呼ぶ。

    以前はアプリ全体のイベントフィルタで入力を監視していたが、ウィンドウ破棄時に
    フィルタが確実に外れず、QApplication が破棄済みオブジェクトを参照して Maya が
    クラッシュする危険があった。そこで、自分（＝親ウィンドウ）が所有する QTimer で
    グローバルなマウス位置を定期ポーリングする方式に変更（アプリへのフィルタ設置なし）。
    タイマーは親と一緒に安全に破棄されるため、再起動/リロードでも落ちない。
    """
    def __init__(self, on_idle, on_active, interval_ms=60000, parent=None):
        super().__init__(parent)
        self._on_idle = on_idle
        self._on_active = on_active
        self._idle = False
        self._interval = int(interval_ms)
        self._elapsed = 0
        self._last_pos = None
        self._poll_ms = 2000
        self._timer = QTimer(self)
        self._timer.setInterval(self._poll_ms)
        self._timer.timeout.connect(self._check)
        self._timer.start()

    def _cursor_pos(self):
        try:
            return QtGui.QCursor.pos()
        except Exception:
            return None

    def _check(self):
        pos = self._cursor_pos()
        moved = (self._last_pos is None) or (pos is not None and pos != self._last_pos)
        self._last_pos = pos
        if moved:
            self.notify_activity()
        else:
            self._elapsed += self._poll_ms
            if not self._idle and self._elapsed >= self._interval:
                self._idle = True
                try:
                    self._on_idle()
                except Exception:
                    pass

    def notify_activity(self):
        """操作があったとみなしてカウントをリセット（必要なら外部からも呼べる）。"""
        self._elapsed = 0
        if self._idle:
            self._idle = False
            try:
                self._on_active()
            except Exception:
                pass

    def stop(self):
        try:
            self._timer.stop()
        except Exception:
            pass


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
    """工程名から安定した表示色を返す。既知工程は固定色、未知は名前ハッシュで割当。

    「<キャラ>/<工程>」形式のときは工程部分（末尾）で既知判定する。
    """
    if not stage:
        return "#e8a838"
    key = stage.lower().split("/")[-1]
    if key in _STAGE_COLOR_MAP:
        return _STAGE_COLOR_MAP[key]
    h = sum(ord(c) for c in stage.lower())
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
                 drill_label="⮞ リーブ", show_header=True, hover=True,
                 cell_w=None, cell_h=None, badges=None, on_activate=None, parent=None):
        super().__init__(parent)
        self._title_text = title
        self._on_activate = on_activate
        # タイルサイズはインスタンスごとに上書き可（スライダーで可変）
        if cell_w:
            self.CELL_W = int(cell_w)
        if cell_h:
            self.CELL_H = int(cell_h)
        self.setFixedWidth(self.CELL_W)
        self.setObjectName("gridCell")
        # QWidget サブクラスはこの属性が無いと QSS の背景/枠（:hover 含む）が描画されない
        self.setAttribute(Qt.WA_StyledBackground, True)
        if hover:
            self.setAttribute(Qt.WA_Hover, True)
            self.setStyleSheet(
                "#gridCell { background: transparent; border: 1px solid transparent;"
                " border-radius: 5px; }"
                "#gridCell:hover { background: #161c2b; border: 1px solid #e8a838; }")
        else:
            self.setStyleSheet(
                "#gridCell { background: transparent; border: 1px solid transparent;"
                " border-radius: 5px; }")
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
        self._mem_frames = None     # RAM 上の縮小フレーム列（共有キャッシュ由来）
        self._mem_timer = None
        self._mem_token = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)

        if show_header:
            head = QHBoxLayout()
            head.setContentsMargins(0, 0, 0, 0)
            head.setSpacing(4)
            name = QLabel(title)
            name.setStyleSheet("color: %s; font-size: 15px; font-weight: bold;"
                               % (title_color or "#e8c87a"))
            head.addWidget(name, 1)
            # バッジ: badges=[(text,color),...] を優先。無ければ従来の単一 stage。
            blist = badges if badges else ([(stage, stage_color(stage))] if stage else [])
            for btext, bcol in blist:
                if not btext:
                    continue
                badge = QLabel(btext)
                badge.setStyleSheet(
                    "color: #0f1117; background: %s; border-radius: 3px;"
                    " padding: 2px 9px; font-size: 12px; font-weight: bold;" % bcol)
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
            openBtn = QPushButton("▸ 開く")
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
            # 一度だけデコード→RAMからループ（多数同時でも軽い・デコードは共有）
            self._playing = True
            if self._mem_frames and self._video_path == self._media[1]:
                # 既に RAM 上にフレームがある → 再デコードせず今の位置から再開
                # （スクロールで再表示するたびに先頭へ戻る＝カクつきを防ぐ）
                self._resume_mem()
            else:
                self._start_mem_video(self._media[1])
        elif kind == "seq" and len(self._frames) > 1:
            if self._seq_timer is None:
                self._seq_timer = QTimer(self)
                self._seq_timer.timeout.connect(self._next_seq)
            # 連番は FPS メタを持たないのでシーン FPS で再生する
            self._seq_timer.start(max(8, int(1000.0 / max(1.0, maya_scene_fps()))))
            self._playing = True
        self._update_toggle_icon()

    def pause(self):
        self._playing = False
        if self._seq_timer:
            self._seq_timer.stop()
        if self._mem_timer:
            self._mem_timer.stop()
        self._mem_token += 1   # 保留中のデコード結果コールバックを無効化
        self._stop_thread()
        self._update_toggle_icon()

    # メモリ再生（クリップを一度だけ縮小デコードして RAM からループ）
    def _decode_max_w(self):
        # デコード解像度を抑える（メモリ・初回デコード負荷を削減）
        return min(int(self.CELL_W) or 240, 256)

    def _start_mem_video(self, path):
        self._video_path = path
        self._mem_token += 1
        token = self._mem_token

        def on_ready(frames, _tok=token):
            try:
                if _tok != self._mem_token or not self._playing:
                    return
                self._mem_frames = frames
                if frames:
                    self._idx = 0
                    if self._mem_timer is None:
                        self._mem_timer = QTimer(self)
                        self._mem_timer.timeout.connect(self._next_mem)
                    self._mem_timer.start(
                        max(8, int(1000.0 / max(1.0, maya_scene_fps()))))
                    self._show_mem(0)
            except RuntimeError:
                pass   # セルが破棄済み

        request_video_frames(path, self._decode_max_w(), on_ready)

    def _resume_mem(self):
        """既に保持しているフレームで、現在位置から再生を再開する（先頭に戻さない）。"""
        if not self._mem_frames:
            return
        if self._mem_timer is None:
            self._mem_timer = QTimer(self)
            self._mem_timer.timeout.connect(self._next_mem)
        self._mem_timer.start(max(8, int(1000.0 / max(1.0, maya_scene_fps()))))
        if self._idx >= len(self._mem_frames):
            self._idx = 0
        self._show_mem(self._idx)

    def _next_mem(self):
        if not self._mem_frames:
            return
        self._idx = (self._idx + 1) % len(self._mem_frames)
        self._show_mem(self._idx)

    def _show_mem(self, i):
        try:
            im = self._mem_frames[i]
            self._view.setPixmap(QPixmap.fromImage(im).scaled(
                self.CELL_W - 8, self.CELL_H, Qt.KeepAspectRatio, Qt.FastTransformation))
        except Exception:
            pass

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
            self._toggleBtn.setText("■" if self._playing else "▶")
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
            self._cv_thread = Cv2VideoThread(path, max_w=self.CELL_W,
                                             fallback_fps=maya_scene_fps(), parent=self)
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

    def mouseDoubleClickEvent(self, event):
        # ダブルクリックで元解像度ビューアを開く
        if event.button() == Qt.LeftButton and self._on_activate and self._media:
            try:
                self._on_activate(self._media, self._title_text)
            except Exception:
                pass
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

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


class ClickableRow(QWidget):
    """クリックで on_click(payload) を呼ぶ行ウィジェット（リスト表示のショット行用）。"""

    def __init__(self, on_click=None, payload=None, parent=None):
        super().__init__(parent)
        self._on_click = on_click
        self._payload = payload
        if on_click:
            self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._on_click:
            try:
                self._on_click(self._payload)
            except Exception:
                pass
        super().mousePressEvent(event)


class SafeComboBox(QComboBox):
    """マルチモニターでドロップダウンが別モニターに出る Qt の不具合対策。

    showPopup 後にポップアップをコンボ直下（同一スクリーン内）へ移動する。
    """
    def showPopup(self):
        super().showPopup()
        try:
            popup = self.view().window()
            below = self.mapToGlobal(self.rect().bottomLeft())
            x, y = below.x(), below.y()
            ph = popup.height()
            pw = popup.width()
            scr = None
            app = QApplication.instance()
            screen_at = getattr(app, "screenAt", None) if app else None
            if screen_at is not None:
                s = screen_at(self.mapToGlobal(self.rect().center()))
                if s is not None:
                    scr = s.availableGeometry()
            if scr is not None:
                if y + ph > scr.bottom():   # 下に入らなければコンボ上へ
                    y = self.mapToGlobal(self.rect().topLeft()).y() - ph
                x = max(scr.left(), min(x, scr.right() - pw + 1))
                y = max(scr.top(), min(y, scr.bottom() - ph + 1))
            popup.move(x, y)
        except Exception:
            pass


class BookmarkSlider(QSlider):
    """ブックマーク位置に目印（縦線）を描くタイムスライダ。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bookmarks = set()

    def paintEvent(self, event):
        super().paintEvent(event)
        try:
            lo, hi = self.minimum(), self.maximum()
            if not self.bookmarks or hi <= lo:
                return
            p = QPainter(self)
            p.setPen(QColor("#e8a838"))
            w = max(1, self.width() - 1)
            span = float(hi - lo)
            top = 1
            bot = max(2, self.height() - 2)
            for bm in self.bookmarks:
                x = int((bm - lo) / span * w)
                p.drawLine(x, top, x, bot)
            p.end()
        except Exception:
            pass


class VideoViewerDialog(QDialog):
    """動画/連番を元解像度で表示し、タイムスライダでフレーム単位スクラブできるビューア。

    mp4 は cv2 でデコード（縮小せず元解像度、表示はウィンドウに合わせて拡縮）。連番は
    画像を直接表示。スライダ／◀|・|▶／←→キーでフレーム移動、▶/■ で再生。
    """
    def __init__(self, media, title="", fps=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title or "ビューア")
        self.setWindowFlags(Qt.Window)
        self.setStyleSheet(STYLE)
        self.resize(1000, 660)
        self._kind = media[0] if media else ""
        self._src = media[1] if media else None
        self._cap = None
        self._total = 0
        self._idx = 0
        self._playing = False
        self._fps = float(fps or maya_scene_fps() or 24.0)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)
        self._bookmarks = set()   # ブックマークしたフレーム番号
        # キーボード操作のためダイアログにフォーカスを保持（子は矢印/Space を奪わない）
        self.setFocusPolicy(Qt.StrongFocus)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._view = QLabel()
        self._view.setAlignment(Qt.AlignCenter)
        self._view.setStyleSheet("background: #000; color: #6b7794;")
        self._view.setMinimumHeight(200)
        lay.addWidget(self._view, 1)

        bar = QWidget()
        bar.setStyleSheet("background: #0a0d14; border-top: 1px solid #1e2435;")
        h = QHBoxLayout(bar)
        h.setContentsMargins(10, 6, 10, 8)
        h.setSpacing(8)
        btn_style = ("QToolButton { background: #141824; color: #cdd6e4;"
                     " border: 1px solid #2a3045; padding: 3px 9px; font-size: 13px; }"
                     "QToolButton:hover { color: #e8c87a; border-color: #e8a838; }")

        def _mk(text, tip, slot):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setStyleSheet(btn_style)
            b.setFocusPolicy(Qt.NoFocus)   # 矢印/Space はダイアログで処理する
            b.clicked.connect(slot)
            h.addWidget(b)
            return b

        self._playBtn = _mk("▶", "再生 / 停止（Space）", self._toggle)
        _mk("◀|", "前のフレーム（←）", lambda: self._step(-1))
        _mk("|▶", "次のフレーム（→）", lambda: self._step(1))
        _mk("|◀★", "前のブックマーク（↑）", lambda: self._goto_bookmark(-1))
        self._bmBtn = _mk("★", "現在フレームをブックマーク（F）", self._toggle_bookmark)
        _mk("★▶|", "次のブックマーク（↓）", lambda: self._goto_bookmark(1))
        self._slider = BookmarkSlider(Qt.Horizontal)
        self._slider.setSingleStep(1)
        self._slider.setPageStep(1)
        self._slider.setFocusPolicy(Qt.NoFocus)
        self._slider.valueChanged.connect(self._on_slider)
        h.addWidget(self._slider, 1)
        self._frameLbl = QLabel("0 / 0")
        self._frameLbl.setStyleSheet("color: #6b7794; font-size: 11px;")
        self._frameLbl.setMinimumWidth(110)
        self._frameLbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        h.addWidget(self._frameLbl)
        lay.addWidget(bar)

        self._load()

    def _load(self):
        if self._kind in ("video", "ext"):
            try:
                import cv2
                self._cap = cv2.VideoCapture(self._src)
                if self._cap.isOpened():
                    self._total = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
                    f = self._cap.get(cv2.CAP_PROP_FPS)
                    if f and f > 0:
                        self._fps = f
                else:
                    self._cap = None
            except Exception:
                self._cap = None
                self._total = 0
        elif self._kind == "seq":
            self._total = len(self._src or [])
        if self._total <= 0:
            self._view.setText("この動画を表示できません（cv2 未導入、または読み込み失敗）")
            self._slider.setEnabled(False)
            self._playBtn.setEnabled(False)
            return
        self._slider.setRange(0, max(0, self._total - 1))
        self._show(0)

    def _paint(self, pm):
        if pm and not pm.isNull():
            self._view.setPixmap(pm.scaled(
                max(1, self._view.width()), max(1, self._view.height()),
                Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _frame_pixmap(self, i):
        if self._kind == "seq":
            try:
                return QPixmap(self._src[i])
            except Exception:
                return None
        if self._cap is not None:
            try:
                import cv2
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, i)
                ok, frame = self._cap.read()
                if ok:
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    hh, ww = rgb.shape[:2]
                    return QPixmap.fromImage(
                        QImage(rgb.data, ww, hh, 3 * ww, QImage.Format_RGB888).copy())
            except Exception:
                pass
        return None

    def _update_frame_label(self):
        mark = "  ★" if self._idx in self._bookmarks else ""
        self._frameLbl.setText("%d / %d%s" % (self._idx + 1, self._total, mark))

    def _show(self, i):
        if self._total <= 0:
            return
        i = max(0, min(int(i), self._total - 1))
        self._idx = i
        self._paint(self._frame_pixmap(i))
        self._update_frame_label()

    def _on_slider(self, v):
        if v != self._idx:
            if self._playing:
                self._pause()   # スクラブ中は停止
            self._show(v)

    def _set_index(self, i):
        i = max(0, min(int(i), self._total - 1))
        self._slider.blockSignals(True)
        self._slider.setValue(i)
        self._slider.blockSignals(False)
        self._show(i)

    def _step(self, d):
        self._pause()
        self._set_index(self._idx + d)

    def _toggle(self):
        self._pause() if self._playing else self._play()

    def _play(self):
        if self._total <= 1:
            return
        self._playing = True
        self._playBtn.setText("■")
        self._timer.start(max(8, int(1000.0 / max(1.0, self._fps))))

    def _pause(self):
        self._playing = False
        self._playBtn.setText("▶")
        self._timer.stop()

    def _advance(self):
        # 再生中はシーク無しの連続読みで滑らかに（連番はインデックス送り）
        if self._kind == "seq":
            self._set_index((self._idx + 1) % self._total)
            return
        if self._cap is None:
            return
        try:
            import cv2
            ok, frame = self._cap.read()
            if not ok:
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self._cap.read()
                if not ok:
                    return
                self._idx = 0
            else:
                self._idx = (self._idx + 1) if self._idx + 1 < self._total else 0
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            hh, ww = rgb.shape[:2]
            self._paint(QPixmap.fromImage(
                QImage(rgb.data, ww, hh, 3 * ww, QImage.Format_RGB888).copy()))
            self._slider.blockSignals(True)
            self._slider.setValue(self._idx)
            self._slider.blockSignals(False)
            self._update_frame_label()
        except Exception:
            pass

    # ── ブックマーク ───────────────────────────────
    def _toggle_bookmark(self):
        if self._total <= 0:
            return
        if self._idx in self._bookmarks:
            self._bookmarks.discard(self._idx)
        else:
            self._bookmarks.add(self._idx)
        self._slider.bookmarks = self._bookmarks
        self._slider.update()
        self._update_frame_label()

    def _goto_bookmark(self, direction):
        if not self._bookmarks:
            return
        self._pause()
        marks = sorted(self._bookmarks)
        if direction > 0:
            nxt = next((m for m in marks if m > self._idx), marks[0])   # 末尾以降は先頭へ巡回
        else:
            nxt = next((m for m in reversed(marks) if m < self._idx), marks[-1])
        self._set_index(nxt)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._total > 0:
            self._show(self._idx)

    def keyPressEvent(self, event):
        k = event.key()
        if k == Qt.Key_Left:
            self._step(-1); return             # ← 前のフレーム
        if k == Qt.Key_Right:
            self._step(1); return              # → 次のフレーム
        if k == Qt.Key_Up:
            self._goto_bookmark(-1); return    # ↑ 前のブックマーク
        if k == Qt.Key_Down:
            self._goto_bookmark(1); return     # ↓ 次のブックマーク
        if k == Qt.Key_F:
            if not event.isAutoRepeat():
                self._toggle_bookmark()        # F 現在フレームをブックマーク
            return
        if k == Qt.Key_Space:
            self._toggle(); return
        super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.setFocus()   # キー操作を受け取れるようフォーカスを持たせる

    def closeEvent(self, event):
        self._pause()
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        super().closeEvent(event)


class AllShotsDialog(QDialog):
    """全ショットの最新動画をグリッドで一覧（工程バッジ・工程ソート）。

    タイル選択で、右サイドバーにそのショットの工程ごとの最新動画を表示する。
    """
    COLS = 5

    def __init__(self, shots_parent, parent=None, stage_subpath="", stages=None,
                 subpath_label=""):
        super().__init__(parent)
        self._stage_subpath = stage_subpath or ""
        self._stages = stages or []   # 工程設定（あれば優先して使う）
        self._subpath_label = subpath_label or ""   # サブパスの呼称（例: キャラ）
        self.setWindowFlags(Qt.Window)
        self.setWindowTitle("All Shots — 最新動画一覧")
        self.setMinimumSize(1200, 640)
        # グリッドに 5 列が収まり、5 行ぶんが見えるサイズで開く
        self.resize(1480, 880)
        self.setStyleSheet(STYLE)
        self._shots_parent = shots_parent
        self._sort_mode = "shot"
        self._view_mode = "grid"   # "grid" / "list"
        self._grid_cols = get_grid_cols()   # グリッド列数 5..7
        self._list_rows = get_list_rows()   # リスト同時表示行数 5..20
        self._cells = []        # グリッド/リストのショットタイル
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
        hl.addWidget(QLabel("表示:"))
        # 表示切替スイッチ（グリッド / リスト）。プルダウンではなく2ボタンで選択。
        sw_style = (
            "QToolButton { background: #141824; color: #6b7794; border: 1px solid #2a3045;"
            " padding: 4px 10px; font-size: 14px; }"
            "QToolButton:hover { color: #e8c87a; }"
            "QToolButton:checked { background: #2a2010; color: #e8a838;"
            " border: 1px solid #e8a838; }")
        self.gridBtn = QToolButton()
        self.gridBtn.setText("▦")        # 四角が並んだタイル表示
        self.gridBtn.setCheckable(True)
        self.gridBtn.setChecked(True)
        self.gridBtn.setToolTip("グリッド表示")
        self.gridBtn.setStyleSheet(sw_style)
        self.gridBtn.clicked.connect(lambda: self._set_view_mode("grid"))
        self.listBtn = QToolButton()
        self.listBtn.setText("≡")        # 縦並びリスト表示
        self.listBtn.setCheckable(True)
        self.listBtn.setToolTip("リスト表示")
        self.listBtn.setStyleSheet(sw_style)
        self.listBtn.clicked.connect(lambda: self._set_view_mode("list"))
        hl.addWidget(self.gridBtn)
        hl.addWidget(self.listBtn)
        hl.addSpacing(12)

        # 表示数スライダー（グリッド=列数5..7 / リスト=同時表示行数5..20）
        self._sizeLabel = QLabel("")
        self._sizeLabel.setStyleSheet("color: #6b7794; font-size: 11px;")
        hl.addWidget(self._sizeLabel)
        self._sizeSlider = QSlider(Qt.Horizontal)
        self._sizeSlider.setFixedWidth(120)
        self._sizeSlider.valueChanged.connect(self._on_size_slider)
        hl.addWidget(self._sizeSlider)
        hl.addSpacing(12)

        # 再生停止トグル（削除可）。ON にすると全再生を止めてファイルロックを解放するので、
        # ショットリストをアクティブにしたままエクスプローラーで削除/移動できる。
        self._playback_suspended = False
        self.pauseAllBtn = QToolButton()
        self.pauseAllBtn.setText("⏸ 再生停止")
        self.pauseAllBtn.setCheckable(True)
        self.pauseAllBtn.setToolTip(
            "再生を止めて動画ファイルのロックを解放します。\n"
            "ON の間はこのウィンドウを開いたままでも外部で削除/移動できます。")
        self.pauseAllBtn.setStyleSheet(sw_style)
        self.pauseAllBtn.toggled.connect(self._on_toggle_pause_all)
        hl.addWidget(self.pauseAllBtn)
        hl.addSpacing(12)

        hl.addWidget(QLabel("工程で絞り込み:"))
        self._filterCombo = SafeComboBox()
        self._filterCombo.addItem("すべて", None)
        self._filterCombo.setToolTip("選んだ工程の動画だけを表示します")
        self._filterCombo.currentIndexChanged.connect(self._on_filter_changed)
        hl.addWidget(self._filterCombo)
        hl.addSpacing(12)

        hl.addWidget(QLabel("並び替え:"))
        self._sortCombo = SafeComboBox()
        self._sortCombo.addItems(["ショット名", "工程"])
        self._sortCombo.currentTextChanged.connect(self._on_sort_changed)
        hl.addWidget(self._sortCombo)
        outer.addWidget(hbar)

        # ── 本体（左:グリッド / 右:工程サイドバー） ──
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.verticalScrollBar().valueChanged.connect(self._schedule_update_visible)
        splitter.addWidget(self._scroll)

        side = QWidget()
        side.setObjectName("detailPanel")
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(0)
        # サイドバー見出し＋閉じるボタン（閉じるとグリッドの再生が戻る）
        head_w = QWidget()
        head_w.setObjectName("detailTitle")
        hrow = QHBoxLayout(head_w)
        hrow.setContentsMargins(10, 0, 6, 0)
        self._sideTitle = QLabel("◈  工程別（タイルを選択）")
        hrow.addWidget(self._sideTitle, 1)
        self._sideCloseBtn = QToolButton()
        self._sideCloseBtn.setText("✕")
        self._sideCloseBtn.setToolTip("工程サイドバーを閉じる")
        self._sideCloseBtn.setStyleSheet(
            "QToolButton { background: transparent; color: #6b7794; border: none;"
            " font-size: 15px; padding: 2px 6px; }"
            "QToolButton:hover { color: #e8a838; }")
        self._sideCloseBtn.clicked.connect(self._close_sidebar)
        self._sideCloseBtn.hide()   # 選択して中身が出たら表示
        hrow.addWidget(self._sideCloseBtn, 0)
        sv.addWidget(head_w)
        self._side_content = QWidget()
        self._side_layout = QVBoxLayout(self._side_content)
        self._side_layout.setContentsMargins(10, 10, 10, 10)
        self._side_layout.setSpacing(10)
        self._side_layout.addStretch()
        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setWidget(self._side_content)
        side_scroll.verticalScrollBar().valueChanged.connect(self._schedule_update_visible)
        self._side_scroll = side_scroll
        sv.addWidget(side_scroll, 1)
        splitter.addWidget(side)
        # グリッド側に 5 列ぶん（約 1130px）を割り当てる
        splitter.setSizes([1130, 330])
        outer.addWidget(splitter, 1)

        self._foot = QLabel("")
        self._foot.setStyleSheet("color: #3a4055; font-size: 10px; padding: 4px 10px;")
        outer.addWidget(self._foot)

        # ── ショットデータ収集 ──
        self._stage_filter = None   # None=すべて（最新工程）／工程名＝その工程で絞り込み
        self._all_stage_names = []  # 絞り込みプルダウンの工程一覧
        self._collect_data()
        self._refresh_filter_combo()

        # 無操作が一定時間続いたら自動で再生停止しロック解放（外部で削除/移動可能に）
        self._idle_suspended = False
        self._idle_mon = IdleReleaseMonitor(
            self._on_idle_release, self._on_idle_active, 60000, self)

    def _collect_data(self):
        """タイルデータを収集する。self._stage_filter が工程名なら、その工程の動画で
        絞り込む（各ショット/モーションのその工程フォルダの最新メディア）。None なら
        従来どおり最新工程の動画を出す。"""
        self._shot_data = []
        shots_parent = self._shots_parent
        flt = getattr(self, "_stage_filter", None)
        has_subpath = bool((self._stage_subpath or "").strip())
        if has_subpath:
            for base, label in expand_stage_bases_named(shots_parent, self._stage_subpath):
                try:
                    children = sorted(os.listdir(base))
                except Exception:
                    children = []
                for child in children:
                    cdir = os.path.join(base, child)
                    if not os.path.isdir(cdir) or child == VIDEO_SUBDIR:
                        continue
                    if flt:
                        media, stg = self._stage_media_for(cdir, flt)
                        if not media:
                            continue
                    else:
                        media = pick_folder_media(cdir)
                        if not media:
                            continue
                        stg = self._stage_badge_for(cdir)
                    badges = []
                    if stg:
                        badges.append((stg, stage_color(stg)))
                    badges.append((label, "#4a9eff"))   # サブパスバッジ（青）
                    self._shot_data.append(
                        {"name": "%s / %s" % (label, child), "folder": cdir,
                         "media": media, "title": child, "badge": label,
                         "badges": badges, "stage": stg or child, "shot": label})
            note = ("工程「%s」の動画" % flt) if flt else "サブパス配下のフォルダ単位の最新動画"
        else:
            try:
                names = sorted(os.listdir(shots_parent))
            except Exception:
                names = []
            for d in names:
                full = os.path.join(shots_parent, d)
                if not os.path.isdir(full):
                    continue
                if flt:
                    media, stage_name = self._stage_media_for(full, flt)
                    if not media:
                        continue
                else:
                    stage_name, media = self._shot_latest(full)
                    if not (media or stage_name):
                        continue
                self._shot_data.append(
                    {"name": d, "folder": full, "media": media,
                     "title": d, "badge": stage_name,
                     "stage": stage_name, "shot": d})
            note = ("工程「%s」の動画" % flt) if flt else "ショットごとの最新工程の動画"

        nshots = len({s.get("shot") for s in self._shot_data})
        self._foot.setText(
            f"{len(self._shot_data)} 件 / {nshots}　（{note}／表示中のみ再生）")
        self._sync_size_slider()
        self._rebuild()

    def _stage_media_for(self, folder, stage_name):
        """指定した工程フォルダの最新メディアを返す。(media, 実工程名) / (None, stage_name)。"""
        for name, media, _mt in self._stage_media_list(folder):
            if name == stage_name or name.split("/")[-1] == stage_name:
                return media, name
        return None, stage_name

    def _available_stage_names(self):
        """絞り込みに使える工程名。工程設定があればその順、無ければ検出順。"""
        if self._stages:
            out = []
            for st in self._stages:
                nm = (st.get("name") or "").strip()
                if nm and nm not in out:
                    out.append(nm)
            return out
        seen = []
        for s in self._shot_data:
            stg = (s.get("stage") or "").split("/")[-1]
            if stg and stg not in seen:
                seen.append(stg)
        return sorted(seen, key=lambda n: (_stage_rank(n)[0], n.lower()))

    def _refresh_filter_combo(self):
        if not hasattr(self, "_filterCombo"):
            return
        self._all_stage_names = self._available_stage_names()
        self._filterCombo.blockSignals(True)
        self._filterCombo.clear()
        self._filterCombo.addItem("すべて", None)
        for nm in self._all_stage_names:
            self._filterCombo.addItem(nm, nm)
        # 現在の絞り込みを選択状態に復元
        idx = self._filterCombo.findData(self._stage_filter)
        self._filterCombo.setCurrentIndex(idx if idx >= 0 else 0)
        self._filterCombo.blockSignals(False)

    def _on_filter_changed(self, *args):
        self._stage_filter = self._filterCombo.currentData()
        self._collect_data()

    # ── 無操作タイムアウトで再生停止／操作再開で復帰 ─────────
    def _on_idle_release(self):
        self._idle_suspended = True
        self.stop_all()

    def _on_idle_active(self):
        self._idle_suspended = False
        if self.isActiveWindow() and not getattr(self, "_playback_suspended", False):
            self._update_visible()

    # ── 工程の解決（工程設定があれば優先） ─────────────────
    def _stage_subpath_for(self, subpath):
        """工程解決に使うサブパス。サブパス指定時はタイルのフォルダが既に解決済み
        （モーション）なので、その直下を工程とするため "" を使う。"""
        if subpath is not None:
            return subpath
        return "" if (self._stage_subpath or "").strip() else self._stage_subpath

    def _stage_media_list(self, shot_folder, subpath=None):
        """各工程の最新メディア [(工程名, media, mtime), ...]。

        工程設定があれば設定の工程・フォルダを使い、無ければフォルダ走査にフォールバック。
        """
        sp = self._stage_subpath_for(subpath)
        if self._stages:
            out = []
            for st in self._stages:
                d = resolve_stage_dir(st, shot_folder, sp)
                if d and os.path.isdir(d):
                    m = pick_folder_media(d)
                    if m:
                        out.append((st["name"], m, _media_mtime(m)))
            return out
        return shot_stage_list(shot_folder, sp)

    def _stage_scene_list(self, shot_folder, subpath=None):
        """各工程のシーン有無 [(工程名, フォルダ, has_scene), ...]（工程順）。

        工程設定があれば設定の工程・フォルダを使い、無ければフォルダ走査にフォールバック。
        """
        sp = self._stage_subpath_for(subpath)
        if self._stages:
            out = []
            for st in self._stages:
                d = resolve_stage_dir(st, shot_folder, sp)
                has = bool(d) and os.path.isdir(d) and stage_has_scene(d)
                out.append((st["name"], d, has))
            return out
        return shot_stage_scene_list(shot_folder, sp)

    def _stage_badge_for(self, motion_folder):
        """そのモーション内の最新工程名を返す（無ければ ""）。

        工程設定があれば設定の工程フォルダ（folder）をモーション起点で解決し、
        シーンがある中で最新のシーンを持つ工程を採用する。
        工程設定が無い場合は、モーション直下の工程フォルダ（サブフォルダ）を走査して
        最新シーンを持つフォルダ名を採用する（＝工程フォルダ名のバッジ）。
        """
        best = None   # (scene_mtime, name)
        if self._stages:
            for st in self._stages:
                d = resolve_stage_dir(st, motion_folder, "")
                if d and os.path.isdir(d):
                    smt = stage_latest_scene_mtime(d)
                    if smt is not None and (best is None or smt > best[0]):
                        best = (smt, st["name"])
            return best[1] if best else ""
        # 工程設定なし: モーション直下の工程フォルダを検出して最新シーンの名前を返す
        try:
            subdirs = sorted(os.listdir(motion_folder))
        except Exception:
            subdirs = []
        best_media = None  # (media_mtime, name) シーンが無いときの保険
        for d in subdirs:
            full = os.path.join(motion_folder, d)
            if not os.path.isdir(full) or d == VIDEO_SUBDIR:
                continue
            smt = stage_latest_scene_mtime(full)
            if smt is not None and (best is None or smt > best[0]):
                best = (smt, d)
            if smt is None:
                m = pick_folder_media(full)
                if m:
                    mt = _media_mtime(m)
                    if best_media is None or mt > best_media[0]:
                        best_media = (mt, d)
        if best:
            return best[1]
        return best_media[1] if best_media else ""

    def _shot_stage_dirs(self, shot_folder, subpath=None):
        """(工程名, フォルダ) のリスト。工程設定があれば設定、無ければ走査。

        複数ベース（ワイルドカード/複数サブパス）のときは工程名を「<親>/<工程>」に。
        """
        sp = self._stage_subpath_for(subpath)
        if self._stages:
            return [(st["name"], resolve_stage_dir(st, shot_folder, sp))
                    for st in self._stages]
        bases = expand_stage_bases(shot_folder, sp)
        multi = len(bases) > 1
        out = []
        for base in bases:
            parent = os.path.basename(base.rstrip("/\\"))
            try:
                for d in sorted(os.listdir(base)):
                    full = os.path.join(base, d)
                    if os.path.isdir(full) and d != VIDEO_SUBDIR:
                        out.append((("%s/%s" % (parent, d)) if multi else d, full))
            except Exception:
                pass
        return out

    def _shot_latest(self, shot_folder):
        """グリッドのタイルに使う「最新工程」とそのメディアを返す。

        シーンデータ(.ma/.mb)がある工程を優先し、その中で最も新しいシーンの工程を最新とする。
        （動画しか無い工程を最新と誤認しないため）。
        シーンのある工程が無ければ、動画が最新の工程にフォールバック。
        戻り値: (stage_name, media)。どちらも無ければ ("", None)。
        """
        def _pick(entries):
            ws = [e for e in entries if e[2] is not None]   # シーンがある工程を優先
            if ws:
                e = max(ws, key=lambda x: x[2])
                return e[0], e[1]
            wm = [e for e in entries if e[1]]                # 無ければ動画が最新
            if wm:
                e = max(wm, key=lambda x: _media_mtime(x[1]))
                return e[0], e[1]
            return None

        # 1) 工程サブフォルダ（<ベース>/<工程>）から探す
        entries = []
        for name, d in self._shot_stage_dirs(shot_folder):
            if d and os.path.isdir(d):
                entries.append((name, pick_folder_media(d), stage_latest_scene_mtime(d)))
        got = _pick(entries)
        if got:
            return got

        # 2) フォールバック: 展開ベース（キャラ等）直下に動画/シーンがある場合
        base_entries = []
        for base in expand_stage_bases(shot_folder, self._stage_subpath):
            if base and os.path.isdir(base):
                base_entries.append((os.path.basename(base.rstrip("/\\")),
                                     pick_folder_media(base),
                                     stage_latest_scene_mtime(base)))
        got = _pick(base_entries)
        if got:
            return got

        # 3) 最終フォールバック: サブパスが一致しなくても、ショット配下を丸ごと
        #    探索して動画/連番があれば表示する（取りこぼし防止）。
        media = pick_folder_media(shot_folder)
        if media:
            return "", media
        return "", None

    # ── 表示モード・ソート・表示数スライダー ──────────────
    def _set_view_mode(self, mode):
        self._view_mode = mode
        self.gridBtn.setChecked(mode == "grid")
        self.listBtn.setChecked(mode == "list")
        self._sync_size_slider()
        self._rebuild()

    def _sync_size_slider(self):
        """現在の表示モードに合わせてスライダーの範囲・値・ラベルを設定する。"""
        self._sizeSlider.blockSignals(True)
        if self._view_mode == "list":
            self._sizeSlider.setRange(5, 20)
            self._sizeSlider.setValue(self._list_rows)
            self._sizeLabel.setText("表示数 %d" % self._list_rows)
        else:
            self._sizeSlider.setRange(3, 7)
            self._sizeSlider.setValue(self._grid_cols)
            self._sizeLabel.setText("列数 %d" % self._grid_cols)
        self._sizeSlider.blockSignals(False)

    def _on_size_slider(self, v):
        if self._view_mode == "list":
            self._list_rows = v
            set_list_rows(v)
            self._sizeLabel.setText("表示数 %d" % v)
        else:
            self._grid_cols = v
            set_grid_cols(v)
            self._sizeLabel.setText("列数 %d" % v)
        self._rebuild()

    def _on_sort_changed(self, text):
        self._sort_mode = "stage" if text == "工程" else "shot"
        self._rebuild()

    def _clear_cells(self, cells, layout=None):
        for cell in cells:
            try:
                cell.stop()
                cell.setParent(None)
                cell.deleteLater()
            except Exception:
                pass
        del cells[:]

    def _sorted_shot_data(self):
        data = list(self._shot_data)
        if self._sort_mode == "stage":
            # 工程順でグループ化し、同工程内はショット名順。
            # 工程設定があればその並び順（上→下）、無ければ既定順(lay→anm→他)。
            # 「<キャラ>/<工程>」形式は末尾（工程）で判定する。
            order = {}
            for i, st in enumerate(self._stages or []):
                nm = (st.get("name") or "").strip().lower()
                if nm and nm not in order:
                    order[nm] = i
            def key(s):
                stg = (s.get("stage") or "").split("/")[-1]
                low = stg.lower()
                rank = order[low] if low in order else (1000 + _stage_rank(stg)[0])
                return (rank, low, s["name"].lower())
            data.sort(key=key)
        else:
            data.sort(key=lambda s: s["name"].lower())
        return data

    def _rebuild(self):
        # 既存セルのスレッドを止めてから、コンテンツごと作り替える
        self._clear_cells(self._cells)
        if self._view_mode == "list":
            self._rebuild_list()
        else:
            self._rebuild_grid()
        QTimer.singleShot(0, self._update_visible)

    def _rebuild_grid(self):
        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(10, 10, 10, 10)
        grid.setSpacing(10)
        cols = max(1, int(self._grid_cols))
        # 利用可能幅に cols 列が収まるようタイル幅を算出（高さは 90/208 比）
        avail = self._scroll.viewport().width() or (self.width() - 360)
        cw = max(120, int((avail - 20 - (cols + 1) * 10) / cols))
        ch = max(60, int(cw * 90 / 208))
        r = c = 0
        for s in self._sorted_shot_data():
            # サブパス設定時は [工程, サブパス] の2バッジ、未設定時は単一の工程バッジ。
            badges = s.get("badges")
            cell = GridVideoCell(s.get("title", s["name"]), s["media"],
                                 stage=("" if badges else s.get("badge", "")),
                                 badges=badges,
                                 on_click=self._select_shot, payload=s["folder"],
                                 folder=None, on_drill=None,
                                 cell_w=cw, cell_h=ch,
                                 on_activate=self._open_viewer, parent=content)
            grid.addWidget(cell, r, c, Qt.AlignLeft | Qt.AlignTop)
            self._cells.append(cell)
            c += 1
            if c >= cols:
                c = 0
                r += 1
        grid.setColumnStretch(cols, 1)   # 余白を右・下へ逃がし左上詰め
        grid.setRowStretch(r + 1, 1)
        self._scroll.setWidget(content)        # 旧コンテンツは破棄される

    def _rebuild_list(self):
        content = QWidget()
        v = QVBoxLayout(content)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(5)

        # 同時に rows 行が収まるようプレビュータイルの高さを算出
        rows = max(1, int(self._list_rows))
        avail_h = self._scroll.viewport().height() or (self.height() - 160)
        th = max(40, int((avail_h - 24) / rows) - 11)   # 行間/余白ぶんを差し引く
        tw = int(th * 208 / 90)

        # 列見出し。サブパス指定時はバッジ列の見出しを呼称（例: キャラ）にする。
        has_sub = bool((self._stage_subpath or "").strip())
        name_head = "名前" if has_sub else "ショット名"
        badge_head = (self._subpath_label or "分類") if has_sub else "工程"
        header = QWidget()
        hh = QHBoxLayout(header)
        hh.setContentsMargins(6, 0, 6, 0)
        for text, w in ((name_head, 150), ("最新動画", tw + 8), (badge_head, 0)):
            lab = QLabel(text)
            lab.setStyleSheet("color: #6b7794; font-size: 11px; font-weight: bold;")
            if w:
                lab.setFixedWidth(w)
            hh.addWidget(lab, 0 if w else 1)
        v.addWidget(header)

        for s in self._sorted_shot_data():
            v.addWidget(self._build_list_row(s, content, tw, th))
        v.addStretch(1)
        self._scroll.setWidget(content)

    def _build_list_row(self, s, parent, tw=None, th=None):
        # 行（ショット列）全体をクリックすると工程サイドバーを表示する
        row = ClickableRow(self._select_shot, s["folder"], parent)
        row.setObjectName("gridCell")
        row.setAttribute(Qt.WA_StyledBackground, True)
        row.setAttribute(Qt.WA_Hover, True)
        row.setStyleSheet(
            "#gridCell { background: transparent; border: 1px solid #1e2435;"
            " border-radius: 5px; }"
            "#gridCell:hover { background: #161c2b; border: 1px solid #e8a838; }")
        h = QHBoxLayout(row)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(10)

        # 名前列（サブパス指定時はモーション名、未設定時はショット名）
        nameLab = QLabel(s.get("title", s["name"]))
        nameLab.setFixedWidth(150)
        nameLab.setStyleSheet("color: #e8c87a; font-size: 14px; font-weight: bold;")
        nameLab.setWordWrap(True)
        h.addWidget(nameLab, 0, Qt.AlignVCenter)

        # 最新動画（プレビュータイル。ヘッダー無し）
        # タイル自身は選択トリガにしない（クリックは行へ伝播して行選択になる）
        # ダブルクリックで元解像度ビューアを開く
        cell = GridVideoCell(s.get("title", s["name"]), s["media"], show_header=False,
                             cell_w=tw, cell_h=th,
                             on_activate=self._open_viewer, parent=row)
        h.addWidget(cell, 0, Qt.AlignVCenter)
        self._cells.append(cell)

        # バッジ列
        badges = QWidget()
        bl = QHBoxLayout(badges)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(5)
        if (self._stage_subpath or "").strip():
            # サブパス指定時: 親（キャラ）名をバッジ表示（グリッドと同じ）
            badge = s.get("badge", "")
            if badge:
                bl.addWidget(self._stage_badge(badge, True))
        else:
            # 未設定時: 工程ごとのシーン有無バッジ（あり＝明るい/なし＝暗い）
            stages = self._stage_scene_list(s["folder"])
            if not stages:
                empty = QLabel("—")
                empty.setStyleSheet("color: #3a4055; font-size: 11px;")
                bl.addWidget(empty)
            for stage_name, _sf, has_scene in stages:
                bl.addWidget(self._stage_badge(stage_name, has_scene))
        bl.addStretch(1)
        h.addWidget(badges, 1, Qt.AlignVCenter)
        return row

    @staticmethod
    def _stage_badge(stage_name, has_scene):
        """工程バッジ。has_scene=True は明るく、False は暗く表示する。"""
        lab = QLabel(stage_name)
        if has_scene:
            col = stage_color(stage_name)
            lab.setStyleSheet(
                "color: #0f1117; background: %s; border-radius: 3px;"
                " padding: 2px 9px; font-size: 12px; font-weight: bold;" % col)
            lab.setToolTip("%s: シーンファイルあり" % stage_name)
        else:
            lab.setStyleSheet(
                "color: #4a5568; background: #161c2b; border: 1px solid #2a3147;"
                " border-radius: 3px; padding: 2px 9px; font-size: 12px;")
            lab.setToolTip("%s: シーンファイルなし" % stage_name)
        return lab

    # ── 工程別サイドバー ───────────────────────────────
    def _select_shot(self, folder):
        self._clear_cells(self._side_cells, self._side_layout)
        # 末尾の stretch を取り除いてから積み直す
        while self._side_layout.count():
            it = self._side_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._sideTitle.setText(f"◈  {Path(folder).name} — 工程別")
        self._sideCloseBtn.show()
        stages = self._stage_media_list(folder)
        if not stages:
            self._side_layout.addWidget(QLabel("工程フォルダに動画が見つかりません"))
        # 工程設定があれば設定順（上＝先頭工程）、無ければ更新の新しい順（上＝最新）
        order = stages if self._stages else list(reversed(stages))

        # 工程名 → 実フォルダ（ワイルドカード/複数サブパスでも同じ命名で対応付け）
        dirs_map = dict(self._shot_stage_dirs(folder))

        def _dir_for(name):
            return dirs_map.get(name, "")

        # サイドバー幅いっぱいにタイルを広げる（右側の空白をなくす）
        try:
            sw = self._side_scroll.viewport().width()
        except Exception:
            sw = 0
        cw = max(180, (sw or 320) - 20)
        ch = max(90, int(cw * 90 / 208))

        for stage_name, media, _mt in order:
            cell = GridVideoCell(stage_name, media, title_color=stage_color(stage_name),
                                 folder=_dir_for(stage_name),
                                 on_drill=self._drill_to, hover=False,
                                 cell_w=cw, cell_h=ch, parent=self._side_content)
            self._side_layout.addWidget(cell)
            self._side_cells.append(cell)
        self._side_layout.addStretch()
        QTimer.singleShot(0, self._update_visible)

    def _open_viewer(self, media, title=""):
        """タイルのダブルクリックで、元解像度＋フレームスクラブのビューアを開く。"""
        if not media:
            return
        try:
            dlg = VideoViewerDialog(media, title=title, fps=maya_scene_fps(), parent=self)
            if not hasattr(self, "_viewers"):
                self._viewers = []
            self._viewers = [v for v in self._viewers if v.isVisible()]
            self._viewers.append(dlg)
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
        except Exception as e:
            print("[OG_Pipeline] ビューアを開けませんでした:", e)

    def _close_sidebar(self):
        """工程サイドバーを閉じる（中身を消してグリッドの再生を戻す）。"""
        self._clear_cells(self._side_cells, self._side_layout)
        while self._side_layout.count():
            it = self._side_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._side_layout.addStretch()
        self._sideTitle.setText("◈  工程別（タイルを選択）")
        self._sideCloseBtn.hide()
        self._update_visible()

    def _drill_to(self, folder):
        """親（メインウィンドウ）のブラウザでこの工程フォルダを開く。
        スタンドアロン（Maya 非依存）ではブラウザが無いのでエクスプローラーで開く。"""
        win = self.parent()
        if win is not None and hasattr(win, "reveal_in_browser"):
            win.reveal_in_browser(folder)
        elif folder:
            open_file_external(folder)

    # ── 表示中のみ再生 ─────────────────────────────────
    def _all_cells(self):
        return self._cells + self._side_cells

    def _on_toggle_pause_all(self, checked):
        """再生停止トグル。ON=全停止しロック解放（削除可）、OFF=表示中を再開。"""
        self._playback_suspended = bool(checked)
        self.pauseAllBtn.setText("▶ 再生再開" if checked else "⏸ 再生停止")
        if checked:
            self.stop_all()
        else:
            self._update_visible()

    def _schedule_update_visible(self, *args):
        """スクロール中は連続発火するため、少し落ち着いてから可視判定する（再生の
        ちらつき／先頭戻りを防ぐデバウンス）。"""
        if not hasattr(self, "_visible_timer"):
            self._visible_timer = QTimer(self)
            self._visible_timer.setSingleShot(True)
            self._visible_timer.timeout.connect(self._update_visible)
        self._visible_timer.start(90)

    def _update_visible(self, *args):
        if not self.isVisible():
            return
        if getattr(self, "_playback_suspended", False) or \
                getattr(self, "_idle_suspended", False):
            return   # 手動停止中／無操作停止中（削除可）は再生しない

        # メモリ再生（デコードは1回・共有）なのでグリッドとサイドバーを同時に再生しても軽い。
        for cell in self._all_cells():
            try:
                visible = not cell.visibleRegion().isEmpty()
            except Exception:
                visible = True
            cell.play() if visible else cell.pause()

    def showEvent(self, event):
        super().showEvent(event)
        # 初回表示後はビューポート幅が確定するので、その実寸でタイルを組み直す
        if not getattr(self, "_did_initial_layout", False):
            self._did_initial_layout = True
            QTimer.singleShot(0, self._rebuild)
        QTimer.singleShot(0, self._update_visible)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # ウィンドウサイズ変更に追従してタイルサイズを組み直す（連続変更はまとめる）
        self._update_visible()
        if not hasattr(self, "_resize_timer"):
            self._resize_timer = QTimer(self)
            self._resize_timer.setSingleShot(True)
            self._resize_timer.timeout.connect(self._rebuild)
        self._resize_timer.start(200)

    def changeEvent(self, event):
        # 非アクティブ（エクスプローラー等へ切替）/最小化になったら再生を止めて
        # 動画ファイルの OS ロックを解放する → その隙に外部で削除/移動できる。
        # アクティブ復帰で表示中タイルの再生を再開する（停止フラグは _update_visible で考慮）。
        try:
            relevant = event.type() in (QtCore.QEvent.WindowStateChange,
                                        QtCore.QEvent.ActivationChange)
        except Exception:
            relevant = False
        super().changeEvent(event)
        if relevant:
            if self.isActiveWindow() and not self.isMinimized():
                self._update_visible()
            else:
                self.stop_all()

    def hideEvent(self, event):
        self.stop_all()
        super().hideEvent(event)

    def stop_all(self):
        for cell in self._all_cells():
            cell.stop()

    def closeEvent(self, event):
        try:
            if getattr(self, "_idle_mon", None) is not None:
                self._idle_mon.stop()
        except Exception:
            pass
        self.stop_all()
        stop_all_decodes()
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
        self._stages = []          # 工程設定（バッジ判定に使用）
        self._stage_subpath = ""
        self.video = VideoPlayer()
        vwrap = QWidget()
        vlay = QVBoxLayout(vwrap)
        vlay.setContentsMargins(12, 10, 12, 6)
        vlay.addWidget(self.video)
        # フォルダを開く（選択中のシーン／フォルダをエクスプローラーで開く）
        self.openFolderBtn = QPushButton("▸  フォルダを開く")
        self.openFolderBtn.setObjectName("refreshBtn")
        self.openFolderBtn.setToolTip("選択中のシーン／フォルダをエクスプローラーで開く")
        self.openFolderBtn.setEnabled(False)
        self.openFolderBtn.clicked.connect(self._open_folder)
        vlay.addWidget(self.openFolderBtn)
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

        self._root_layout = layout
        self.clear()

    def add_bottom_widget(self, w):
        """サイドバー最下部にウィジェットを追加する（動画書き出しグループ等）。"""
        self._root_layout.addWidget(w)

    def _clear_layout(self, layout=None):
        """レイアウト内の全ウィジェットを削除する（サブレイアウトも再帰）。

        SIZE / MODIFIED 行などは addLayout で追加されており、ウィジェットだけ
        見ると取りこぼす。サブレイアウト内の QLabel も確実に消すため再帰する。
        """
        lay = layout if layout is not None else self.contentLayout
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)      # 即座に画面から外す（古い値が残らない）
                w.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())
                item.layout().deleteLater()

    def _open_folder(self):
        """現在表示中のシーン／フォルダをエクスプローラーで開く。"""
        target = self._abs_path or self._shot_folder
        if target:
            reveal_in_explorer(target)

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
        self._shown_mtime = None
        if hasattr(self, "openFolderBtn"):
            self.openFolderBtn.setEnabled(False)
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
            self.show_folder_video(self._shot_folder,
                                   getattr(self, "_stage_subpath", ""),
                                   getattr(self, "_is_shot", False), force=True)
        elif self._abs_path:
            self._show_media(self._abs_path)

    def _stage_for_media(self, media_path, folder, stage_subpath):
        """再生メディアが属する工程名を返す。工程設定があればその工程名（＝設定の name）を
        優先し、無ければフォルダ名から判定する。"""
        if not media_path:
            return ""
        stages = getattr(self, "_stages", None) or []
        if stages:
            mp = os.path.normcase(os.path.normpath(media_path))
            for st in stages:
                d = resolve_stage_dir(st, folder, stage_subpath)
                if not d:
                    continue
                dn = os.path.normcase(os.path.normpath(d))
                try:
                    rel = os.path.relpath(mp, dn)
                except Exception:
                    continue
                if not rel.startswith(".."):
                    return st.get("name") or os.path.basename(d.rstrip("/\\"))
        # フォールバック: フォルダ名から判定
        return self._stage_of_path(media_path, folder, stage_subpath)

    @staticmethod
    def _stage_of_path(media_path, folder, stage_subpath):
        """再生メディアのパスから、それが属する工程フォルダ名を求める。無ければ ""。

        複数ベース（ワイルドカード/複数サブパス）のときは「<親>/<工程>」で返す。
        """
        try:
            mp = os.path.normcase(os.path.normpath(media_path))
            bases = expand_stage_bases(folder, stage_subpath)
            multi = len(bases) > 1
            for base in bases:
                try:
                    rel = os.path.relpath(mp, os.path.normcase(os.path.normpath(base)))
                except Exception:
                    continue
                if rel.startswith(".."):
                    continue
                parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
                if parts:
                    if multi:
                        return "%s/%s" % (os.path.basename(base.rstrip("/\\")), parts[0])
                    return parts[0]
        except Exception:
            pass
        return ""

    def show_folder_video(self, folder, stage_subpath="", is_shot=False, force=False,
                          stages=None):
        """選択フォルダ（ショット／工程）配下の最新メディアをサイドバーで再生する。

        cv2 があれば動画(mp4)を、無ければ Pipeline_Movie の連番を再生する。
        ショットフォルダ選択時は、再生中の動画がどの工程のものかを（工程設定を反映して）表示する。
        """
        # 同じフォルダの再選択ではチカつき防止のため作り直さない（force で強制更新）
        if not force and folder and folder == self._shot_folder and not self._abs_path:
            return
        if stages is not None:
            self._stages = stages
        self.setUpdatesEnabled(False)   # 破棄→再構築の中間描画を抑え、ちらつきを防ぐ
        try:
            self._abs_path = ""
            self._shot_folder = folder
            self._stage_subpath = stage_subpath
            self._is_shot = is_shot
            if hasattr(self, "openFolderBtn"):
                self.openFolderBtn.setEnabled(bool(folder))
            self._clear_layout()
            name = Path(folder).name
            media = pick_folder_media(folder)
            sub_text = "このフォルダに動画／連番はありません"
            media_path = ""
            if hasattr(self, "video"):
                if media is None:
                    self.video.clear_player()
                elif media[0] == "video":
                    self.video.set_video(media[1])
                    sub_text = f"最新動画: {Path(media[1]).name}"
                    media_path = media[1]
                elif media[0] == "seq":
                    self.video.set_sequence(media[1])
                    sub_text = f"最新の連番（{len(media[1])} 枚）"
                    media_path = media[1][0] if media[1] else ""
                else:  # "ext"
                    self.video.set_external(media[1])
                    sub_text = f"動画: {Path(media[1]).name}（外部再生）"
                    media_path = media[1]

            title = QLabel(f"▸  {name}")
            title.setObjectName("detailFilename")
            title.setWordWrap(True)
            self.contentLayout.addWidget(title)

            # 再生中の動画がどの工程のものかを表示（ショット選択時。工程設定を反映）
            stage = self._stage_for_media(media_path, folder, stage_subpath) if (is_shot and media_path) else ""
            if stage:
                badge = QLabel(f"工程:  {stage}")
                badge.setStyleSheet(
                    "color: #0f1117; background: %s; border-radius: 3px;"
                    " padding: 3px 10px; font-size: 12px; font-weight: bold;" % stage_color(stage))
                self.contentLayout.addWidget(badge)

            sub = QLabel(sub_text)
            sub.setObjectName("detailValue")
            sub.setWordWrap(True)
            self.contentLayout.addWidget(sub)
            self.contentLayout.addStretch()
        finally:
            self.setUpdatesEnabled(True)

    def update_info(self, rel_path: str, abs_path: str, size: int, mtime: float):
        # 更新日時・サイズは選択のたびに実ファイルから取り直す
        # （スキャン時にキャッシュした値は保存後などに古くなるため）
        try:
            st = os.stat(abs_path)
            size, mtime = st.st_size, st.st_mtime
        except Exception:
            pass
        # 同じファイル かつ 更新日時も同じならちらつき防止で作り直さない
        if (abs_path and abs_path == self._abs_path and not self._shot_folder
                and mtime == getattr(self, "_shown_mtime", None)):
            return
        self._shown_mtime = mtime
        self.setUpdatesEnabled(False)
        try:
            self._update_info_body(rel_path, abs_path, size, mtime)
        finally:
            self.setUpdatesEnabled(True)

    def refresh_file_times(self):
        """表示中ファイルの更新日時/サイズを実ファイルから取り直し、変化があれば反映する。

        選択中ファイルがツール外/Maya 内で保存された場合でも、再選択せずに
        MODIFIED 表示を最新へ更新する（パネル全体は作り直さずラベルだけ書き換え）。
        """
        import datetime
        if self._shot_folder or not self._abs_path:
            return
        if not getattr(self, "_mtimeVal", None):
            return
        try:
            st = os.stat(self._abs_path)
        except Exception:
            return
        if st.st_mtime == getattr(self, "_shown_mtime", None):
            return
        self._shown_mtime = st.st_mtime
        try:
            self._mtimeVal.setText(
                datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d  %H:%M"))
            if getattr(self, "_sizeVal", None):
                self._sizeVal.setText(self._fmt_size(st.st_size))
        except Exception:
            pass

    def _update_info_body(self, rel_path, abs_path, size, mtime):
        self._abs_path = abs_path
        self._shot_folder = ""
        if hasattr(self, "openFolderBtn"):
            self.openFolderBtn.setEnabled(bool(abs_path))
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

        self._sizeVal = None
        self._mtimeVal = None
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
            if key == "SIZE":
                self._sizeVal = v
            else:
                self._mtimeVal = v

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

        # 末尾カラムが見えるよう右へスクロールし、フォーカスもそのカラムへ移す。
        # （保存処理で一覧を作り直すとフォーカスが検索欄へ飛ぶのを防ぐ）
        if self._columns:
            last = self._columns[-1]._container
            last_list = self._columns[-1]
            QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(last))
            QTimer.singleShot(0, lambda: last_list.setFocus())
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
            it = QListWidgetItem(f"▸  {name}")
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


# ─── プロジェクト設定ダイアログ ───────────────────────────────────────────────
class ProjectSettingsDialog(QDialog):
    """プロジェクト（ルート）の登録/編集。

    指定項目:
      - 名前
      - プロジェクトルート（path）
      - ショットフォルダの親（shots_parent。直下のフォルダ＝ショット）
      - 工程フォルダのサブパス（stage_subpath。各ショット内の相対パス）
        例: sh001/ma/<工程>/... のとき "ma"。空＝ショット直下に工程フォルダ。
    """

    STAGE_COLS = ["工程名", "工程フォルダ", "リネーム元", "リネーム先", "テイク", "ローカル"]

    def __init__(self, parent=None, entry=None, current_startup=None):
        super().__init__(parent)
        self.setWindowTitle("プロジェクト設定")
        self.setStyleSheet(STYLE)
        self.setMinimumSize(820, 600)
        entry = entry or {}
        self.imported = False   # ダイアログ内でインポートしたか（呼び出し側が再読込）
        self.deleted = False    # ダイアログ内でプロジェクトを削除したか

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(12)

        title = QLabel("◈  プロジェクト設定")
        title.setStyleSheet("font-size: 14px; color: #e8a838; letter-spacing: 1px;")
        outer.addWidget(title)

        # プロジェクト選択プルダウン（既存を切替 or 新規）
        psel = QHBoxLayout()
        psel.addWidget(QLabel("プロジェクト:"))
        self.projectCombo = SafeComboBox()
        self._all_roots = load_roots()
        for r in self._all_roots:
            self.projectCombo.addItem(r["name"], r["name"])
        self.projectCombo.addItem("＋ 新規プロジェクト", None)
        cur_name = entry.get("name", "")
        ci = self.projectCombo.findData(cur_name) if cur_name else -1
        self.projectCombo.setCurrentIndex(ci if ci >= 0 else self.projectCombo.count() - 1)
        self.projectCombo.activated.connect(self._on_project_selected)
        psel.addWidget(self.projectCombo, 1)
        self.deleteProjectBtn = QPushButton("🗑 削除")
        self.deleteProjectBtn.setObjectName("refreshBtn")
        self.deleteProjectBtn.setToolTip("選択中のプロジェクト登録を削除（ディスク上のファイルは消しません）")
        self.deleteProjectBtn.clicked.connect(self._delete_project)
        psel.addWidget(self.deleteProjectBtn)
        outer.addLayout(psel)

        form = QFormLayout()
        form.setSpacing(10)

        self.nameEdit = QLineEdit(entry.get("name", ""))
        self.nameEdit.setPlaceholderText("プロジェクト名（一覧に表示）")
        form.addRow("名前:", self.nameEdit)

        self.rootEdit = QLineEdit(entry.get("path", ""))
        form.addRow("プロジェクトルート:", self._with_browse(self.rootEdit, self._browse_root))

        self.shotsEdit = QLineEdit(entry.get("shots_parent", ""))
        self.shotsEdit.setPlaceholderText("この直下のフォルダをショットとして扱う（空＝ルート）")
        form.addRow("ショットフォルダの親:",
                    self._with_browse(self.shotsEdit, self._browse_shots))

        # サブパスは表で入力（左=パス / 右=表示名）。1行＝1サブパス。
        self.subpathTable = QTableWidget(0, 2)
        self.subpathTable.setHorizontalHeaderLabels(["サブパス（* 可・空=直下）", "表示名（省略可）"])
        self.subpathTable.verticalHeader().setVisible(False)
        shdr = self.subpathTable.horizontalHeader()
        shdr.setSectionResizeMode(0, QHeaderView.Stretch)
        shdr.setSectionResizeMode(1, QHeaderView.Stretch)
        self.subpathTable.setFixedHeight(96)
        for pat, nm in _parse_subpath_items(entry.get("stage_subpath", "")):
            self._add_subpath_row(pat, nm)
        form.addRow("サブパス（この直下がタイル）:", self.subpathTable)

        sprow = QHBoxLayout()
        addSp = QPushButton("＋ 行を追加")
        addSp.setObjectName("refreshBtn")
        addSp.clicked.connect(lambda: self._add_subpath_row())
        delSp = QPushButton("－ 選択行を削除")
        delSp.setObjectName("refreshBtn")
        delSp.clicked.connect(self._del_subpath_row)
        exSp = QPushButton("例から取得…")
        exSp.setObjectName("refreshBtn")
        exSp.clicked.connect(self._browse_stage)
        sprow.addWidget(addSp)
        sprow.addWidget(delSp)
        sprow.addWidget(exSp)
        sprow.addStretch(1)
        form.addRow("", self._wrap(sprow))

        # バッジ列の見出し呼称（リスト表示の列名。例: キャラ）
        self.labelEdit = QLineEdit(entry.get("subpath_label", ""))
        self.labelEdit.setPlaceholderText("サブパス分類の呼称（リスト列見出し。例: キャラ）")
        form.addRow("サブパスの呼称:", self.labelEdit)

        # 次回も使用（起動時にこのプロジェクトを自動選択）。既定 ON。
        # 既に別プロジェクトが起動時設定なら OFF、未設定/自分なら ON。
        nm = entry.get("name", "")
        self.startupCheck = QCheckBox("次回も使用（起動時にこのプロジェクトを自動選択）")
        self.startupCheck.setChecked(current_startup in (None, "", nm))
        form.addRow("起動時:", self.startupCheck)

        outer.addLayout(form)

        hint = QLabel(
            "サブパスは『ショット親からの相対パス』。そこで辿り着いたフォルダの"
            "『直下の各フォルダ』が1タイル＝その配下の最新動画になります（カンマで複数可、"
            "`*` は任意の1フォルダ）。個別登録は不要です。\n"
            "例) 直下にキャラ／その中にモーション の構成で、\n"
            "　・特定キャラのモーションだけ → サブパスに『Boss_003』（キャラ名）\n"
            "　・全キャラのモーション → 『*』\n"
            "　・空欄 → 直下フォルダ（ショット）ごとに最新1つ（従来）")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #4a5568; font-size: 10px;")
        outer.addWidget(hint)

        # ── 工程リスト ──
        stage_title = QLabel("◈  工程リスト（設定すると工程ベースの保存が有効になります）")
        stage_title.setStyleSheet("font-size: 12px; color: #e8a838;")
        outer.addWidget(stage_title)

        self.stageTable = QTableWidget(0, len(self.STAGE_COLS))
        self.stageTable.setHorizontalHeaderLabels(self.STAGE_COLS)
        self.stageTable.verticalHeader().setVisible(False)
        hdr = self.stageTable.horizontalHeader()
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)   # 工程フォルダ列を伸ばす
        for c in (0, 2, 3, 4, 5):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        for st in entry.get("stages", []) or []:
            self._add_stage_row(st)
        outer.addWidget(self.stageTable, 1)

        srow = QHBoxLayout()
        addStageBtn = QPushButton("＋ 工程を追加")
        addStageBtn.setObjectName("refreshBtn")
        addStageBtn.clicked.connect(lambda: self._add_stage_row())
        delStageBtn = QPushButton("－ 選択行を削除")
        delStageBtn.setObjectName("refreshBtn")
        delStageBtn.clicked.connect(self._del_stage_row)
        upStageBtn = QPushButton("↑ 上へ")
        upStageBtn.setObjectName("refreshBtn")
        upStageBtn.setToolTip("選択した工程を上へ（この並び順がショットリストの工程順になります）")
        upStageBtn.clicked.connect(lambda: self._move_stage_row(-1))
        downStageBtn = QPushButton("↓ 下へ")
        downStageBtn.setObjectName("refreshBtn")
        downStageBtn.setToolTip("選択した工程を下へ")
        downStageBtn.clicked.connect(lambda: self._move_stage_row(1))
        srow.addWidget(addStageBtn)
        srow.addWidget(delStageBtn)
        srow.addWidget(upStageBtn)
        srow.addWidget(downStageBtn)
        srow.addStretch(1)
        shint = QLabel("並び順（上→下）がショットリストの工程順・絞り込みの順になります。"
                       "別工程へ保存＝現シーン名の『リネーム元→リネーム先』置換＋テイク/ローカル初期化。"
                       "例: 工程名=lay_pri / 工程フォルダ=ma/lay_pri / リネーム元=lay_pri / "
                       "リネーム先=anm_sec / テイク=t01 / ローカル=v001。"
                       "テイク/ローカルは任意（空欄＝現シーンの番号を据え置き／数字入り＝その値で初期化）。"
                       "TAKE UP はテイク欄の接頭辞（例 t・take・C 等／数字なしでも可）＋数字を+1、"
                       "VERSION UP は名前末尾の番号を+1。")
        shint.setStyleSheet("color: #4a5568; font-size: 10px;")
        srow.addWidget(shint)
        outer.addLayout(srow)

        # インポート / エクスポート（プロジェクト設定 JSON の取り込み・書き出し）
        io_row = QHBoxLayout()
        io_row.setSpacing(6)
        importBtn = QPushButton("⭳  インポート")
        importBtn.setObjectName("refreshBtn")
        importBtn.setToolTip("プロジェクト設定 JSON を取り込む（既存に統合）")
        importBtn.clicked.connect(self._do_import)
        exportBtn = QPushButton("⭱  エクスポート")
        exportBtn.setObjectName("refreshBtn")
        exportBtn.setToolTip("登録済みプロジェクトを JSON として書き出す")
        exportBtn.clicked.connect(self._do_export)
        io_row.addWidget(importBtn)
        io_row.addWidget(exportBtn)
        io_row.addStretch(1)
        outer.addLayout(io_row)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        # Enter で誤って閉じない（テキスト入力確定とOKを混同しない）
        for b in btns.buttons():
            b.setAutoDefault(False)
            b.setDefault(False)
        outer.addWidget(btns)

    def keyPressEvent(self, event):
        # Enter/Return では閉じない（入力欄の確定のみ）。OK はボタンで明示クリック。
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)

    def use_startup(self):
        return self.startupCheck.isChecked()

    def _do_import(self):
        fp, _ = QFileDialog.getOpenFileName(
            self, "プロジェクト設定をインポート", str(Path.home()),
            "JSON (*.json);;すべて (*.*)")
        if not fp:
            return
        try:
            n = import_roots_file(fp)
            self.imported = True
            QMessageBox.information(self, "インポート", "%d 件を取り込みました。" % n)
        except Exception as e:
            QMessageBox.warning(self, "インポート失敗", str(e))

    def _do_export(self):
        fp, _ = QFileDialog.getSaveFileName(
            self, "プロジェクト設定をエクスポート",
            os.path.join(str(Path.home()), "og_pipeline_roots.json"),
            "JSON (*.json)")
        if not fp:
            return
        try:
            export_roots_file(fp)
            QMessageBox.information(self, "エクスポート", "書き出しました:\n%s" % fp)
        except Exception as e:
            QMessageBox.warning(self, "エクスポート失敗", str(e))

    def _with_browse(self, edit, slot, label="参照…"):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        h.addWidget(edit, 1)
        b = QPushButton(label)
        b.setObjectName("refreshBtn")
        b.clicked.connect(slot)
        h.addWidget(b)
        return w

    @staticmethod
    def _wrap(layout):
        """レイアウトを QWidget に包んで返す（QFormLayout の addRow 用）。"""
        w = QWidget()
        w.setLayout(layout)
        return w

    # ── サブパス表の行操作 ─────────────────────────────
    def _add_subpath_row(self, pattern="", name=""):
        r = self.subpathTable.rowCount()
        self.subpathTable.insertRow(r)
        self.subpathTable.setItem(r, 0, QTableWidgetItem(pattern))
        self.subpathTable.setItem(r, 1, QTableWidgetItem(name))

    def _del_subpath_row(self):
        rows = sorted({i.row() for i in self.subpathTable.selectedIndexes()}, reverse=True)
        for r in rows:
            self.subpathTable.removeRow(r)

    def _subpaths_from_table(self):
        """表を 'パス = 表示名'（名前なしは 'パス'）の改行区切り文字列にする。"""
        lines = []
        for r in range(self.subpathTable.rowCount()):
            def cell(c):
                it = self.subpathTable.item(r, c)
                return it.text().strip() if it else ""
            pat, nm = cell(0), cell(1)
            if not pat:
                continue
            lines.append("%s = %s" % (pat, nm) if nm else pat)
        return "\n".join(lines)

    def _browse_root(self):
        d = QFileDialog.getExistingDirectory(self, "プロジェクトルートを選択",
                                             self.rootEdit.text() or str(Path.home()))
        if d:
            self.rootEdit.setText(d)
            if not self.nameEdit.text().strip():
                self.nameEdit.setText(Path(d).name)
            if not self.shotsEdit.text().strip():
                self.shotsEdit.setText(d)

    def _browse_shots(self):
        start = self.shotsEdit.text() or self.rootEdit.text() or str(Path.home())
        d = QFileDialog.getExistingDirectory(
            self, "ショットフォルダの親階層を選択（直下＝ショット）", start)
        if d:
            self.shotsEdit.setText(d)

    def _browse_stage(self):
        """例として1ショット内の『工程フォルダが入っているフォルダ』を選び、相対サブパスを算出。"""
        sp = self.shotsEdit.text().strip() or self.rootEdit.text().strip()
        start = sp or str(Path.home())
        d = QFileDialog.getExistingDirectory(
            self, "工程フォルダが入っているフォルダを選択（例: sh001/ma）", start)
        if not d:
            return
        sub = self._derive_subpath(d, sp)
        self._add_subpath_row(sub, "")   # 表に1行追加

    @staticmethod
    def _derive_subpath(picked, shots_parent):
        """選んだフォルダから、ショットフォルダ起点の相対サブパスを求める。

        shots_parent/<shot>/<sub...> を選んだとき <sub...> を返す。
        ショットフォルダ自身を選んだら空。算出不能なら空。
        """
        if not shots_parent:
            return ""
        try:
            rel = os.path.relpath(os.path.normpath(picked), os.path.normpath(shots_parent))
        except Exception:
            return ""
        if rel.startswith("..") or rel == ".":
            return ""
        parts = [p for p in rel.replace("\\", "/").split("/") if p]
        if len(parts) <= 1:
            return ""           # ショットフォルダ自身 → 直下に工程
        return "/".join(parts[1:])

    # ── プロジェクト選択（切替 / 新規） ─────────────────
    def _on_project_selected(self, _idx):
        name = self.projectCombo.currentData()
        if name is None:   # 新規プロジェクト
            self._load_project({})
            self.nameEdit.setFocus()
            return
        entry = find_root_entry(name) or {}
        self._load_project(entry)

    def _delete_project(self):
        """選択中の既存プロジェクトの登録を削除する（ファイル自体は消さない）。"""
        name = self.projectCombo.currentData()
        if not name:
            QMessageBox.information(
                self, "プロジェクト削除",
                "削除できる既存プロジェクトが選択されていません（新規は削除不要です）。")
            return
        r = QMessageBox.question(
            self, "プロジェクト削除",
            "プロジェクト『%s』の登録を削除しますか？\n"
            "（この操作は登録一覧から消すだけで、ディスク上のファイルは削除しません）" % name,
            QMessageBox.Yes | QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        remove_root(name)
        if get_startup_root() == name:
            clear_startup_root()
        self.deleted = True
        # プルダウンとフォームを更新（削除後は新規状態にする）
        self._all_roots = load_roots()
        self.projectCombo.blockSignals(True)
        self.projectCombo.clear()
        for rr in self._all_roots:
            self.projectCombo.addItem(rr["name"], rr["name"])
        self.projectCombo.addItem("＋ 新規プロジェクト", None)
        self.projectCombo.setCurrentIndex(self.projectCombo.count() - 1)
        self.projectCombo.blockSignals(False)
        self._load_project({})
        QMessageBox.information(self, "プロジェクト削除", "『%s』を削除しました。" % name)

    def _load_project(self, entry):
        """フォーム各欄を entry の内容で更新する。"""
        self.nameEdit.setText(entry.get("name", ""))
        self.rootEdit.setText(entry.get("path", ""))
        self.shotsEdit.setText(entry.get("shots_parent", ""))
        self.subpathTable.setRowCount(0)
        for pat, nm in _parse_subpath_items(entry.get("stage_subpath", "")):
            self._add_subpath_row(pat, nm)
        self.labelEdit.setText(entry.get("subpath_label", ""))
        self.stageTable.setRowCount(0)
        for st in entry.get("stages", []) or []:
            self._add_stage_row(st)
        cur_startup = get_startup_root()
        nm = entry.get("name", "")
        self.startupCheck.setChecked(cur_startup in (None, "", nm))

    # ── 工程リストの行操作 ─────────────────────────────
    def _add_stage_row(self, stage=None):
        stage = stage or {}
        r = self.stageTable.rowCount()
        self.stageTable.insertRow(r)
        vals = [stage.get("name", ""), stage.get("folder", ""),
                stage.get("rename_from", ""), stage.get("rename_to", ""),
                stage.get("take", ""), stage.get("local", "")]
        for c, v in enumerate(vals):
            self.stageTable.setItem(r, c, QTableWidgetItem(v))

    def _del_stage_row(self):
        rows = sorted({i.row() for i in self.stageTable.selectedIndexes()}, reverse=True)
        for r in rows:
            self.stageTable.removeRow(r)

    def _move_stage_row(self, delta):
        """選択した工程行を delta（-1=上 / +1=下）方向へ入れ替える。並び順＝工程順。"""
        t = self.stageTable
        rows = sorted({i.row() for i in t.selectedIndexes()})
        if not rows:
            return
        r = rows[0]
        nr = r + delta
        if nr < 0 or nr >= t.rowCount():
            return
        for c in range(t.columnCount()):
            a = t.takeItem(r, c)
            b = t.takeItem(nr, c)
            t.setItem(r, c, b)
            t.setItem(nr, c, a)
        t.selectRow(nr)

    def _stages_from_table(self):
        out = []
        for r in range(self.stageTable.rowCount()):
            def cell(c):
                it = self.stageTable.item(r, c)
                return it.text().strip() if it else ""
            name = cell(0)
            if not name:
                continue
            out.append({"name": name, "folder": cell(1),
                        "rename_from": cell(2), "rename_to": cell(3) or name,
                        "take": cell(4), "local": cell(5)})
        return out

    def _on_ok(self):
        if not self.nameEdit.text().strip() or not self.rootEdit.text().strip():
            QMessageBox.warning(self, "プロジェクト設定", "名前とプロジェクトルートは必須です。")
            return
        self.accept()

    def values(self):
        return {
            "name": self.nameEdit.text().strip(),
            "path": self.rootEdit.text().strip(),
            "shots_parent": self.shotsEdit.text().strip() or self.rootEdit.text().strip(),
            # 複数行＝複数サブパス。全体の前後空白だけ除去（各行は展開側で処理）。
            "stage_subpath": self._subpaths_from_table(),
            "subpath_label": self.labelEdit.text().strip(),
            "stages": self._stages_from_table(),
        }


# ─── 環境設定ダイアログ ───────────────────────────────────────────────────────
class SettingsDialog(QDialog):
    """左サイドバーでセクションを切り替える環境設定。

    セクション:
      - 動画書き出し … 書き出し方式 / 保存時の自動更新 / 最小間隔
      - mp4 有効化   … cv2 のインストール / アンインストール
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("環境設定")
        self.setStyleSheet(STYLE)
        self.setMinimumSize(680, 420)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 左: セクション選択サイドバー
        self.nav = QListWidget()
        self.nav.setFixedWidth(170)
        self.nav.setObjectName("detailPanel")
        self.nav.addItem("▸  動画書き出し")
        self.nav.addItem("▦  mp4 有効化")
        outer.addWidget(self.nav)

        # 右: セクション本体
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_export_page())
        self.stack.addWidget(self._build_mp4_page())
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(16, 14, 16, 14)
        rv.setSpacing(12)
        rv.addWidget(self.stack, 1)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        rv.addWidget(btns)
        outer.addWidget(right, 1)

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

    # ── 動画書き出しセクション ─────────────────────────
    def _build_export_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        title = QLabel("◈  動画書き出し")
        title.setStyleSheet("font-size: 14px; color: #e8a838; letter-spacing: 1px;")
        v.addWidget(title)

        form = QFormLayout()
        form.setSpacing(10)

        self.methodCombo = SafeComboBox()
        self.methodCombo.addItem("プレイブラスト（現在のビューを撮影）", "playblast")
        self.methodCombo.addItem("ハードウェア（別プロセスで裏で書き出し）", "hardware")
        idx = self.methodCombo.findData(get_export_method())
        if idx >= 0:
            self.methodCombo.setCurrentIndex(idx)
        form.addRow("自動更新の書き出し方式:", self.methodCombo)

        self.autoCheck = QCheckBox("シーンを保存するたびに動画を更新する")
        self.autoCheck.setChecked(get_auto_export_on_save())
        form.addRow("自動更新:", self.autoCheck)

        self.intervalSpin = QSpinBox()
        self.intervalSpin.setRange(0, 600)
        self.intervalSpin.setSuffix(" 分")
        self.intervalSpin.setValue(get_auto_export_interval_min())
        self.intervalSpin.setToolTip(
            "前回の動画更新からこの分数以上経過しているときだけ書き出します（0=毎回）。")
        form.addRow("最小間隔:", self.intervalSpin)

        v.addLayout(form)

        hint = QLabel("※ 自動更新は「保存のたび」に判定し、最後の更新から指定分数未満なら"
                      "スキップします。手動書き出しの方式はムービーバーのプルダウンで個別に選べます。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #4a5568; font-size: 10px;")
        v.addWidget(hint)
        v.addStretch(1)
        return page

    # ── mp4 有効化セクション ───────────────────────────
    def _build_mp4_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        title = QLabel("◈  mp4 有効化（OpenCV / cv2）")
        title.setStyleSheet("font-size: 14px; color: #e8a838; letter-spacing: 1px;")
        v.addWidget(title)

        self.cv2StatusLabel = QLabel()
        self.cv2StatusLabel.setStyleSheet("font-size: 12px;")
        v.addWidget(self.cv2StatusLabel)

        desc = QLabel(
            "mp4 等の埋め込み再生には OpenCV(cv2) が必要です。--user 領域に導入するため"
            "共有 Maya 本体は変更しません。アンインストールは Maya 再起動後に反映されます。")
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #4a5568; font-size: 10px;")
        v.addWidget(desc)

        row = QHBoxLayout()
        self.cv2InstallBtn = QPushButton("⭳  インストール")
        self.cv2InstallBtn.setObjectName("refreshBtn")
        self.cv2InstallBtn.clicked.connect(self._do_install_cv2)
        self.cv2UninstallBtn = QPushButton("✕  アンインストール")
        self.cv2UninstallBtn.setObjectName("refreshBtn")
        self.cv2UninstallBtn.clicked.connect(self._do_uninstall_cv2)
        row.addWidget(self.cv2InstallBtn)
        row.addWidget(self.cv2UninstallBtn)
        row.addStretch(1)
        v.addLayout(row)

        self.cv2Log = QPlainTextEdit()
        self.cv2Log.setReadOnly(True)
        self.cv2Log.setPlaceholderText("pip 実行ログがここに表示されます。")
        self.cv2Log.setStyleSheet(
            "background: #0a0d14; color: #9aa6c0; border: 1px solid #1e2435;"
            " font-size: 10px;")
        v.addWidget(self.cv2Log, 1)

        self._refresh_cv2_status()
        return page

    def _refresh_cv2_status(self):
        if get_pending_cv2_uninstall():
            self.cv2StatusLabel.setText("状態: ▸ 次回 Maya 起動時にアンインストール予約済み")
        elif _HAS_CV2:
            self.cv2StatusLabel.setText("状態: ✓ 有効（mp4 を埋め込み再生できます）")
        else:
            self.cv2StatusLabel.setText("状態: ▲ 無効（mp4 は外部プレイヤー／連番のみ）")
        self.cv2InstallBtn.setText("⟳  再インストール" if _HAS_CV2 else "⭳  インストール")

    def _do_install_cv2(self):
        set_pending_cv2_uninstall(False)   # 予約があれば取り消す
        self.cv2Log.setPlainText("インストール中…（数分かかる場合があります）")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok, log = install_opencv()
        finally:
            QApplication.restoreOverrideCursor()
        self.cv2Log.setPlainText(log or "")
        self._refresh_cv2_status()
        self._notify_parent_cv2()
        if ok:
            QMessageBox.information(self, "完了", "cv2 を導入しました。mp4 が埋め込み再生されます。")
        else:
            QMessageBox.warning(self, "インストール失敗",
                                "cv2 を導入できませんでした。ログを確認してください。")

    def _do_uninstall_cv2(self):
        # 現セッションで cv2 が読み込まれていると cv2.pyd が OS ロックされ、
        # どのプロセスからも削除できない（WinError 5）。その場合は次回起動時に
        # （cv2 を import する前に）アンインストールするよう予約する。
        cv2_loaded = ("cv2" in sys.modules) or _HAS_CV2
        if cv2_loaded:
            r = QMessageBox.question(
                self, "アンインストール（次回起動時）",
                "現在 cv2 が使用中のため、今すぐは削除できません"
                "（ファイルが OS にロックされています）。\n\n"
                "次回 Maya 起動時に、cv2 を読み込む前に自動でアンインストールします。\n"
                "予約しますか？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if r != QMessageBox.Yes:
                return
            set_pending_cv2_uninstall(True)
            self.cv2StatusLabel.setText("状態: ▸ 次回 Maya 起動時にアンインストール予約済み")
            self.cv2Log.setPlainText(
                "次回 Maya 起動時に cv2 をアンインストールします。\n"
                "（予約を取り消すには、もう一度この画面でインストールを実行してください）")
            return

        # cv2 未ロード（再起動直後など）→ 即時アンインストール可能
        self.cv2Log.setPlainText("アンインストール中…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok, log = uninstall_opencv()
        finally:
            QApplication.restoreOverrideCursor()
        self.cv2Log.setPlainText(log or "")
        self._refresh_cv2_status()
        if ok:
            QMessageBox.information(self, "完了", "アンインストールしました。")
        else:
            QMessageBox.warning(self, "アンインストール",
                                "対象が見つからないか失敗しました。ログを確認してください。")

    def _notify_parent_cv2(self):
        """親ウィンドウの mp4 有効化ボタン表示とプレビューを更新する。"""
        win = self.parent()
        try:
            if win is not None and hasattr(win, "enableMp4Btn"):
                win.enableMp4Btn.setVisible(not _HAS_CV2)
            if win is not None and hasattr(win, "detailPanel"):
                win.detailPanel.reload_video()
        except Exception:
            pass

    def save(self):
        """動画書き出しセクションの設定を永続化する。"""
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
        self._active_root_name = None
        self.active_root = None
        self.active_shots_parent = None
        self.active_stage_subpath = ""   # 各ショット内の工程フォルダ相対サブパス
        self.active_subpath_label = ""   # サブパスの呼称（例: キャラ）
        self.active_stages = []          # 工程リスト（プロジェクト設定）
        self._last_export_at = {}    # {正規化シーンパス: 最終書き出し time.time()}
        self._save_job = None        # SceneSaved scriptJob の ID
        self._current_folder = None  # ブラウザでリーブ中のフォルダ（新規保存先候補）
        self._hw_watchers = []       # ハードウェア書き出しの完了監視タイマー

        self.setStyleSheet(STYLE)
        self._build_ui()

        # 起動時: 有効プロジェクトを決定（起動時設定→先頭）してブラウザに反映
        self._reload_roots_combo()
        self._apply_root()

        # 現在のシーン名を定期的に更新（ツール外で開閉されても追従する）
        self._update_current_scene_label()
        self._sceneTimer = QTimer(self)
        self._sceneTimer.timeout.connect(self._update_current_scene_label)
        self._sceneTimer.start(1500)

        # 保存時の自動書き出し（SceneSaved を監視。判定は実行時に設定を読む）
        self._register_save_job()

        # 無操作が続いたら詳細パネルの再生を止めてファイルロックを解放
        self._idle_suspended = False
        self._idle_mon = IdleReleaseMonitor(
            self._on_idle_release, self._on_idle_active, 60000, self)

        # 起動時フォーカスを検索欄ではなくブラウザに置く
        QTimer.singleShot(0, self._focus_browser)

    def _on_idle_release(self):
        self._idle_suspended = True
        try:
            self.detailPanel.video.suspend()
        except Exception:
            pass

    def _on_idle_active(self):
        self._idle_suspended = False
        if self.isActiveWindow() and not getattr(self, "_playback_suspended", False):
            try:
                self.detailPanel.video.resume()
            except Exception:
                pass

    def _focus_browser(self):
        """ブラウザの先頭カラムにフォーカスを当てる（検索欄の自動フォーカス回避）。"""
        try:
            cols = getattr(self.browser, "_columns", [])
            if cols:
                cols[0].setFocus()
        except Exception:
            pass

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
            self._set_export_status(
                "自動書き出しをスキップ（前回更新から%d分未満／あと約%d分）"
                % (get_auto_export_interval_min(), remain))
            return
        self._last_export_at[key] = now
        method = get_export_method()   # 自動更新は環境設定の方式に従う
        if method == "hardware":
            ok, msg, proc = export_hardware_background(cur)
            self._set_export_status(("▸ 自動: " if ok else "▲ 自動: ") + msg)
            if ok and proc is not None:
                self._watch_hw_export(cur, proc, label="自動")
        else:
            self._set_export_status("▸ 自動プレイブラスト中…")
            self._playblast(cur)

    def _shot_number_of(self, scene_path):
        """シーンのショットナンバーを返す。ショットフォルダ名→ファイル名の sh### の順で判定。"""
        if not scene_path:
            return ""
        sp = getattr(self, "active_shots_parent", None)
        if sp:
            try:
                sf = shot_folder_of(scene_path, sp)
                if sf:
                    return os.path.basename(sf)
            except Exception:
                pass
        m = re.search(r"sh(?:fs)?\d+", os.path.basename(scene_path), re.I)
        return m.group(0) if m else ""

    def _update_current_scene_label(self):
        """ヘッダー中央に現在開いている Maya シーン名とショットナンバーを表示する。"""
        name = ""
        try:
            import maya.cmds as cmds
            name = cmds.file(q=True, sceneName=True) or ""
        except Exception:
            name = ""
        if name:
            self.currentSceneLabel.setText("▸  " + os.path.basename(name))
            self.currentSceneLabel.setToolTip(name)
            shot = self._shot_number_of(name)
            self.currentShotLabel.setText(shot)
            self.currentShotLabel.setVisible(bool(shot))
        else:
            self.currentSceneLabel.setText("▸  (未保存のシーン)")
            self.currentSceneLabel.setToolTip("")
            self.currentShotLabel.setText("")
            self.currentShotLabel.setVisible(False)
        # 表示中ファイルの更新日時も追従させる（保存後に再選択しなくても反映）
        try:
            self.detailPanel.refresh_file_times()
        except Exception:
            pass

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

        # 中央: ショットナンバー（バッジ）＋ シーン名。
        # バッジは AlignVCenter で配置し、ヘッダー全高に伸びて金色ラインを
        # 隠さないようにする（伸びると不透明背景が下線を覆ってしまうため）。
        self.currentShotLabel = QLabel("")
        self.currentShotLabel.setStyleSheet(
            "color: #e8a838; font-size: 19px; font-weight: bold;"
            " background: transparent; border: none;")
        self.currentShotLabel.setVisible(False)
        layout.addWidget(self.currentShotLabel, 0, Qt.AlignVCenter)
        layout.addSpacing(12)   # ショットナンバーとシーン名の間に少し余白

        self.currentSceneLabel = QLabel("▸  (シーン未取得)")
        self.currentSceneLabel.setStyleSheet(
            "color: #e8c87a; font-size: 13px; font-weight: bold;"
            " background: transparent; border: none;")
        self.currentSceneLabel.setAlignment(Qt.AlignVCenter)
        layout.addWidget(self.currentSceneLabel, 0, Qt.AlignVCenter)
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

        # プロジェクト名は単純なテキスト（黄色）で表示。選択はプロジェクト設定から。
        self.projectLabel = QLabel("（未選択）")
        self.projectLabel.setStyleSheet(
            "color: #e8a838; font-size: 19px; font-weight: bold;"
            " background: transparent; border: none;")
        layout.addWidget(self.projectLabel)

        # プロジェクトの選択/登録/編集・インポート/エクスポート・次回も使用は
        # すべてこのダイアログに集約した。
        self.addRootBtn = QPushButton("◆  プロジェクト設定")
        self.addRootBtn.setObjectName("refreshBtn")
        self.addRootBtn.setToolTip("プロジェクトの登録/編集・インポート/エクスポート・起動時設定")
        self.addRootBtn.clicked.connect(self._add_root)
        layout.addWidget(self.addRootBtn)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.VLine)
        sep2.setStyleSheet("color: #1e2435;")
        layout.addWidget(sep2)

        self.allShotsBtn = QPushButton("▦  ショットリスト")
        self.allShotsBtn.setObjectName("refreshBtn")
        self.allShotsBtn.setToolTip("全ショットの最新動画を一覧（グリッド／リスト）・自動再生")
        self.allShotsBtn.clicked.connect(self._open_all_shots)
        layout.addWidget(self.allShotsBtn)

        # cv2 が無い環境向け: mp4 埋め込み再生を有効化（cv2 を --user 導入）
        self.enableMp4Btn = QPushButton("▸  mp4再生を有効化")
        self.enableMp4Btn.setObjectName("refreshBtn")
        self.enableMp4Btn.setToolTip("opencv-python を --user 導入して mp4 を埋め込み再生（共有 Maya は変更しません）")
        self.enableMp4Btn.clicked.connect(self._install_cv2)
        self.enableMp4Btn.setVisible(not _HAS_CV2)
        layout.addWidget(self.enableMp4Btn)

        layout.addStretch()

        # 右上: 環境設定（書き出し方式・保存時の自動更新など）
        self.settingsBtn = QPushButton("◆  環境設定")
        self.settingsBtn.setObjectName("refreshBtn")
        self.settingsBtn.setToolTip("書き出し方式・保存時の動画自動更新などを設定")
        self.settingsBtn.clicked.connect(self._open_settings)
        layout.addWidget(self.settingsBtn)
        return bar

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
            # 環境設定で自動更新 ON/OFF が変わったら、保存バー側のチェックも合わせる
            try:
                self.autoExportCheck.blockSignals(True)
                self.autoExportCheck.setChecked(get_auto_export_on_save())
                self.autoExportCheck.blockSignals(False)
            except Exception:
                pass
            # 環境設定は自動更新用。手動の方式プルダウンとは独立なので同期しない。
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
        self._all_shots_dlg = AllShotsDialog(
            self.active_shots_parent, self,
            stage_subpath=getattr(self, "active_stage_subpath", ""),
            stages=getattr(self, "active_stages", []),
            subpath_label=getattr(self, "active_subpath_label", ""))
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

    def changeEvent(self, event):
        # ウィンドウが非アクティブ（エクスプローラー等へ切替）になったら詳細パネルの
        # 動画再生を止めてファイルの OS ロックを解放する → 外部で削除/移動できる。
        # アクティブ復帰時は再生を再開する。手動停止中（_playback_suspended）は再開しない。
        try:
            if event.type() == QtCore.QEvent.ActivationChange:
                vp = getattr(getattr(self, "detailPanel", None), "video", None)
                if vp is not None:
                    if self.isActiveWindow():
                        if not getattr(self, "_playback_suspended", False) \
                                and not getattr(self, "_idle_suspended", False):
                            vp.resume()
                    else:
                        vp.suspend()
        except Exception:
            pass
        super().changeEvent(event)

    def closeEvent(self, event):
        """ウィンドウを閉じる際、実行中のデコード/検索スレッドを確実に止める。

        実行中の QThread が破棄されると Qt がプロセスごと落ちる（＝Maya クラッシュ）。
        埋め込みプレイヤー・全ショットダイアログ・検索スレッドを明示的に停止する。
        """
        try:
            self._sceneTimer.stop()
        except Exception:
            pass
        try:
            if getattr(self, "_idle_mon", None) is not None:
                self._idle_mon.stop()
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
            self.statusLabel.setText("▲  cv2 の導入に失敗しました")

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
        # クリック時のみフォーカス（起動時やカラム再構築で自動フォーカスされないように）
        self.searchBar.setFocusPolicy(Qt.ClickFocus)
        self.searchBar.setPlaceholderText("ファイル名またはパスで検索（ルート以下を再帰検索）…")
        self.searchBar.textChanged.connect(self._apply_view)
        layout.addWidget(self.searchBar, 1)

        filter_label = QLabel("TYPE:")
        filter_label.setStyleSheet("color: #3a4055; font-size: 11px; letter-spacing: 1px;")
        layout.addWidget(filter_label)

        self.typeFilter = SafeComboBox()
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

        # 「フォルダを開く」は詳細パネル（サイドバー）側に移動した

        # 配置順: TAKE UP / SAVE TO STAGE / VERSION UP / SAVE AS / SAVE NEW SCENE

        # テイクバージョンを +1 して保存（工程設定の命名規則 _t## を増やす）
        self.takeUpBtn = QPushButton("⇧T  TAKE UP")
        self.takeUpBtn.setObjectName("refreshBtn")
        self.takeUpBtn.setToolTip("テイクバージョン（_t##）を +1 して同じフォルダに保存")
        self.takeUpBtn.clicked.connect(self._take_up_save)
        ab_layout.addWidget(self.takeUpBtn)

        # 別工程として保存（工程設定に従いリネーム＋テイク/ローカルを初期値にリセット）
        self.saveStageBtn = QPushButton("↪  SAVE TO STAGE")
        self.saveStageBtn.setToolTip("別工程として保存：選択した工程のフォルダに、命名規則でリネームして保存（テイク/ローカルは初期値）")
        self.saveStageBtn.setObjectName("refreshBtn")
        self.saveStageBtn.clicked.connect(self._save_to_stage)
        ab_layout.addWidget(self.saveStageBtn)

        # 名前末尾の番号を +1 してローカルバージョンを上げて保存
        self.versionUpBtn = QPushButton("⇧  VERSION UP")
        self.versionUpBtn.setObjectName("refreshBtn")
        self.versionUpBtn.setToolTip("ローカルバージョン（末尾番号 / v###）を +1 して同じフォルダに保存")
        self.versionUpBtn.clicked.connect(self._version_up_save)
        ab_layout.addWidget(self.versionUpBtn)

        # 現在開いているシーンを、そのシーンのフォルダを既定にして別名保存する
        self.saveAsBtn = QPushButton("⤓  SAVE AS")
        self.saveAsBtn.setObjectName("refreshBtn")
        self.saveAsBtn.setToolTip("現在のシーンを、開いているシーンのフォルダを既定にして保存")
        self.saveAsBtn.clicked.connect(self._save_scene_as)
        ab_layout.addWidget(self.saveAsBtn)

        # 現在リーブ（表示）中のフォルダに新規シーンを保存
        self.saveNewBtn = QPushButton("✚  SAVE NEW SCENE")
        self.saveNewBtn.setObjectName("refreshBtn")
        self.saveNewBtn.setToolTip("現在ブラウザで開いている（リーブ中の）フォルダに新規シーンを保存")
        self.saveNewBtn.clicked.connect(self._save_new_scene)
        ab_layout.addWidget(self.saveNewBtn)

        self.openBtn = QPushButton("▶  OPEN SCENE")
        self.openBtn.setObjectName("openBtn")
        self.openBtn.setEnabled(False)
        self.openBtn.clicked.connect(self._open_scene)
        ab_layout.addWidget(self.openBtn)

        layout.addWidget(action_bar)

        # 保存時の動画書き出しトグル＋書き出し専用ステータス（セーブ系ボタンの直下）。
        # 書き出し状態は他の操作で消えないよう、通常ステータスと分けて常時表示する。
        sm_bar = QWidget()
        sm_bar.setStyleSheet("background: #0a0d14; border-top: 1px solid #1e2435;")
        sm_bar.setFixedHeight(28)
        sm = QHBoxLayout(sm_bar)
        sm.setContentsMargins(16, 2, 16, 2)
        sm.setSpacing(10)
        self.autoExportCheck = QCheckBox("保存時に動画書き出し")
        self.autoExportCheck.setChecked(get_auto_export_on_save())
        self.autoExportCheck.setToolTip(
            "Scene 保存時に自動で動画を書き出す（方式・最小間隔は環境設定に従う）")
        self.autoExportCheck.toggled.connect(
            lambda v: set_auto_export_on_save(bool(v)))
        sm.addWidget(self.autoExportCheck)
        sm.addSpacing(12)
        sm.addWidget(QLabel("書き出し:"))
        self.exportStatusLabel = QLabel("—")
        self.exportStatusLabel.setStyleSheet("color: #e8a838; font-size: 11px;")
        self.exportStatusLabel.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sm.addWidget(self.exportStatusLabel, 1)
        layout.addWidget(sm_bar)
        return panel

    def _set_export_status(self, text):
        """動画書き出し専用ステータスを更新する（通常ステータスとは別枠・即時再描画）。"""
        try:
            self.exportStatusLabel.setText(text)
            self.exportStatusLabel.repaint()
        except Exception:
            pass

    def _build_movie_bar(self) -> QWidget:
        """動画書き出しグループ（サイドバー下部に配置）。

        誤クリックで OPEN SCENE の近くから書き出しが走らないよう、サイドバーへ隔離。
        方式プルダウンは手動書き出し用（環境設定の自動更新とは独立）。
        """
        movie_bar = QWidget()
        movie_bar.setStyleSheet("background: #0a0d14; border-top: 1px solid #1e2435;")
        mb = QVBoxLayout(movie_bar)
        mb.setContentsMargins(12, 6, 12, 8)
        mb.setSpacing(8)

        # プルダウン（折りたたみ）見出し。既定は閉じておき、誤操作を防ぐ。
        self.exportToggle = QToolButton()
        self.exportToggle.setText("▸  動画書き出し")
        self.exportToggle.setCheckable(True)
        self.exportToggle.setChecked(False)
        self.exportToggle.setArrowType(Qt.RightArrow)
        self.exportToggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.exportToggle.setStyleSheet(
            "QToolButton { color: #9aa6c0; font-size: 12px; border: none;"
            " padding: 2px; }"
            "QToolButton:hover { color: #e8c87a; }")
        self.exportToggle.clicked.connect(self._toggle_export_panel)
        mb.addWidget(self.exportToggle)

        # 折りたたみ対象（既定で非表示）。グローバル QSS の min-height を上書きして
        # ハイライト（ホバー枠）が大きくならない・重ならないよう小さくする。
        self.exportContent = QWidget()
        ec = QVBoxLayout(self.exportContent)
        ec.setContentsMargins(2, 2, 2, 2)   # ホバー/フォーカス枠が端で切れないよう余白
        ec.setSpacing(10)

        # 「保存時に自動書き出し」のトグルは環境設定に集約したため、ここには置かない。

        # 方式プルダウン（手動書き出し用。環境設定とは独立。初期値は設定から）
        mrow = QHBoxLayout()
        mrow.setContentsMargins(0, 0, 0, 0)
        mrow.setSpacing(6)
        mlab = QLabel("方式:")
        mlab.setStyleSheet("font-size: 11px;")
        mrow.addWidget(mlab)
        self.exportMethodCombo = SafeComboBox()
        self.exportMethodCombo.setMinimumWidth(180)
        # 枠（通常/ホバー/フォーカス）を全辺明示して、ハイライトが欠けないようにする。
        self.exportMethodCombo.setStyleSheet(
            "QComboBox { border: 1px solid #2a3045; border-radius: 3px; padding: 4px 8px; }"
            "QComboBox:hover, QComboBox:focus, QComboBox:on { border: 1px solid #e8a838; }"
            "QComboBox QAbstractItemView { min-width: 180px; }")
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
        mrow.addWidget(self.exportMethodCombo, 1)
        ec.addLayout(mrow)

        # 現在シーンを Pipeline_Movie に書き出し（最小間隔は無視＝常に実行）
        self.playblastBtn = QPushButton("▸  書き出し実行")
        self.playblastBtn.setObjectName("refreshBtn")
        self.playblastBtn.setFixedHeight(30)
        self.playblastBtn.setStyleSheet(
            "#refreshBtn { font-size: 12px; min-height: 0; padding: 4px 10px; }")
        self.playblastBtn.setToolTip("現在のシーンを Pipeline_Movie にシーン名と同名で書き出す（手動は間隔制限なし）")
        self.playblastBtn.clicked.connect(self._playblast_current)
        ec.addWidget(self.playblastBtn)

        self.exportContent.setVisible(False)   # 既定は閉じる
        mb.addWidget(self.exportContent)
        return movie_bar

    def _toggle_export_panel(self):
        vis = self.exportToggle.isChecked()
        self.exportContent.setVisible(vis)
        self.exportToggle.setArrowType(Qt.DownArrow if vis else Qt.RightArrow)

    def _build_detail_panel(self) -> QWidget:
        self.detailPanel = DetailPanel()
        # 動画書き出しはサイドバー下部に隔離（OPEN SCENE の誤クリック回避）
        self.detailPanel.add_bottom_widget(self._build_movie_bar())
        return self.detailPanel

    # ════════════════════════════════════════════════════════════════════
    #  ルート（プロジェクト）管理
    # ════════════════════════════════════════════════════════════════════
    def _current_root_name(self):
        return getattr(self, "_active_root_name", None)

    def _reload_roots_combo(self, select_name=None):
        """有効プロジェクトを決めてラベルに反映する（旧プルダウンの置き換え）。

        select_name が指定され存在すればそれを、無ければ現在値→起動時設定→先頭、の順。
        """
        roots = load_roots()
        names = [r["name"] for r in roots]
        target = None
        for cand in (select_name, getattr(self, "_active_root_name", None),
                     get_startup_root()):
            if cand and cand in names:
                target = cand
                break
        if target is None and names:
            target = names[0]
        self._active_root_name = target
        self.projectLabel.setText(target or "（未選択）")
        self.projectLabel.setToolTip(target or "")

    def _select_in_combo(self, name):
        """指定プロジェクトを有効にする（旧プルダウン選択の置き換え）。"""
        roots = {r["name"] for r in load_roots()}
        if name in roots:
            self._active_root_name = name
        self.projectLabel.setText(self._active_root_name or "（未選択）")
        return self._active_root_name == name

    def _apply_root(self):
        """選択中のルートを有効化し、ブラウザに反映する。"""
        name = self._current_root_name()
        if not name:
            self.active_root = None
            self.active_shots_parent = None
            self.active_subpath_label = ""
            self.active_stage_subpath = ""
            self.active_stages = []
            self.rootPathLabel.setText("▸  ルート未登録")
            self.browser.set_root(None)
            self.statusLabel.setText(
                "プロジェクトルート未登録 — [プロジェクト設定] か [⭳ インポート] で登録してください"
            )
            return
        entry = find_root_entry(name) or {}
        self.active_root = entry.get("path") or find_root_path(name)
        self.active_shots_parent = entry.get("shots_parent") or self.active_root
        self.active_stage_subpath = entry.get("stage_subpath", "") or ""
        self.active_subpath_label = entry.get("subpath_label", "") or ""
        self.active_stages = entry.get("stages", []) or []
        self.rootPathLabel.setText(f"▸  {self.active_root}")
        self._apply_view()

    def _add_root(self):
        # 選択中のプロジェクトがあれば編集用に初期値として読み込む
        cur_name = self._current_root_name()
        cur_entry = find_root_entry(cur_name) if cur_name else None
        dlg = ProjectSettingsDialog(self, entry=cur_entry,
                                    current_startup=get_startup_root())
        accepted = dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec()
        if accepted:
            v = dlg.values()
            add_root(v["name"], v["path"], v["shots_parent"], v["stage_subpath"],
                     v.get("stages"), subpath_label=v.get("subpath_label", ""))
            # 「次回も使用」チェックを起動時設定に反映
            if dlg.use_startup():
                set_startup_root(v["name"])
            elif get_startup_root() == v["name"]:
                clear_startup_root()
            self._reload_roots_combo(select_name=v["name"])
            self._select_in_combo(v["name"])
            self._apply_root()
            sub = v["stage_subpath"] or "（ショット直下）"
            self.statusLabel.setText(
                f"✓  プロジェクト保存: {v['name']}（ショット親: {v['shots_parent']} ／ 工程サブパス: {sub}）")
        elif dlg.imported or dlg.deleted:
            # OK されなくても、インポート/削除で変わった一覧を反映
            self._reload_roots_combo(select_name=cur_name)
            self._select_in_combo(cur_name)
            self._apply_root()
            self.statusLabel.setText(
                "✓  プロジェクト設定をインポートしました" if dlg.imported
                else "✓  プロジェクトを削除しました")

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
        is_shot = self._is_shot_folder(folder)
        label = "ショット" if is_shot else "フォルダ"
        self.selectedLabel.setText(f"{label}: {Path(folder).name}（配下の最新動画）")
        self.detailPanel.show_folder_video(
            folder, getattr(self, "active_stage_subpath", ""), is_shot,
            stages=getattr(self, "active_stages", []))

    # ════════════════════════════════════════════════════════════════════
    #  表示（カラム表示／再帰検索）
    # ════════════════════════════════════════════════════════════════════
    def _apply_view(self, *args):
        ext = self.typeFilter.currentText()
        self.browser.ext_filter = None if ext == "ALL" else ext

        self._selected_path = ""
        self.openBtn.setEnabled(False)
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
            self.selectedLabel.setText("ファイルを選択してください")
            self.detailPanel.clear()
            return
        self._selected_path = info["abs"]
        self._current_folder = os.path.dirname(info["abs"])   # リーブ中フォルダ
        self.openBtn.setEnabled(True)
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
                f"▲  現在のシーンはこのルート配下にありません: {cur}"
            )

    # ════════════════════════════════════════════════════════════════════
    #  右クリックメニュー
    # ════════════════════════════════════════════════════════════════════
    def _show_context_menu(self, path, global_pos):
        menu = QMenu(self)
        act_open = menu.addAction("▶  シーンを開く")
        act_import = menu.addAction("▤  インポート")
        act_folder = menu.addAction("▸  フォルダを開く")
        menu.addSeparator()
        act_pb = menu.addAction("▸  プレイブラスト書き出し")
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
        # カラムをリセットせず、保存先まで展開して新バージョンを反映
        self._reveal_saved(new_path)

    def _reveal_saved(self, path):
        """保存したファイルを、カラムをルートまでリセットせずに表示する。

        reveal_path はルートから保存先までのカラムを構築して末尾を選択する
        （= その場の表示と同じパスに収まる）。ルート外で到達できないときだけ
        通常の一覧更新にフォールバックする。
        """
        try:
            if self.browser.reveal_path(path):
                return
        except Exception:
            pass
        self._apply_view()

    def _save_renamed(self, target_dir, new_stem, cmds):
        """new_stem を target_dir に保存する共通処理。拡張子は現在のシーンに合わせる。"""
        cur = cmds.file(q=True, sceneName=True) or ""
        ext = os.path.splitext(cur)[1].lower() if cur else ".ma"
        if ext not in (".ma", ".mb"):
            ext = ".ma"
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception:
            pass
        save_path = os.path.join(target_dir, new_stem + ext)
        if os.path.exists(save_path):
            r = QMessageBox.question(
                self, "上書き確認", "%s は既に存在します。上書きしますか？" % os.path.basename(save_path),
                QMessageBox.Yes | QMessageBox.No)
            if r != QMessageBox.Yes:
                return None
        ftype = "mayaAscii" if ext == ".ma" else "mayaBinary"
        cmds.file(rename=save_path)
        cmds.file(save=True, type=ftype)
        self._apply_view()
        try:
            self.browser.reveal_path(save_path)
        except Exception:
            pass
        return save_path

    def _take_up_save(self):
        """テイクバージョン（_t##）を +1 して同じフォルダに保存する。"""
        try:
            import maya.cmds as cmds
        except ImportError:
            QMessageBox.information(self, "TAKE UP",
                                    "Maya 内で実行すると、テイク番号を +1 して保存します。")
            return
        cur = cmds.file(q=True, sceneName=True) or ""
        if not cur:
            QMessageBox.warning(self, "TAKE UP", "保存済みのシーンがありません。")
            return
        stem, ext = os.path.splitext(os.path.basename(cur))
        # 採番対象の接頭辞は工程設定のテイク欄から導く（_t## 固定でなく汎用）。
        take_re = version_token_re(getattr(self, "active_stages", []), "take",
                                   DEFAULT_TAKE_PREFIX)
        new_stem, ok = bump_version_token(stem, take_re)
        if not ok:
            QMessageBox.warning(
                self, "TAKE UP",
                "テイク番号が見つかりませんでした（工程設定のテイク欄の接頭辞＋数字）:\n"
                + os.path.basename(cur))
            return
        saved = self._save_renamed(os.path.dirname(cur), new_stem, cmds)
        if saved:
            self.statusLabel.setText(f"✓  テイクアップ保存: {Path(saved).name}")

    def _save_to_stage(self):
        """別工程として保存。工程設定があればリネーム＋テイク/ローカル初期化して工程フォルダへ。

        工程設定（プロジェクト設定の工程リスト）が無い場合は SAVE AS にフォールバックする。
        """
        try:
            import maya.cmds as cmds
        except ImportError:
            QMessageBox.information(self, "別工程へ保存",
                                    "Maya 内で実行すると、選択した工程フォルダにリネーム保存します。")
            return
        stages = getattr(self, "active_stages", []) or []
        if not stages:
            # 工程設定なし → 現状機能（SAVE AS）にフォールバック
            self.statusLabel.setText("工程リストが未設定のため SAVE AS を使用します（プロジェクト設定で工程を登録できます）")
            self._save_scene_as()
            return
        cur = cmds.file(q=True, sceneName=True) or ""
        if not cur:
            QMessageBox.warning(self, "別工程へ保存", "保存済みのシーンがありません。")
            return

        # 対象工程を選ぶ（ブラウザ選択中フォルダに一致する工程を初期選択）
        names = [s["name"] for s in stages]
        default_idx = 0
        cf = os.path.normcase(os.path.normpath(str(self._current_folder or "")))
        shot_folder = shot_folder_of(cur, self.active_shots_parent)
        for i, s in enumerate(stages):
            d = resolve_stage_dir(s, shot_folder, self.active_stage_subpath)
            if d and os.path.normcase(os.path.normpath(d)) == cf:
                default_idx = i
                break
        choice, ok = QInputDialog.getItem(
            self, "別工程へ保存", "保存先の工程を選択:", names, default_idx, False)
        if not ok or not choice:
            return
        stage = stages[names.index(choice)]

        if not shot_folder:
            QMessageBox.warning(
                self, "別工程へ保存",
                "現在のシーンからショットフォルダを特定できませんでした。\n"
                "（プロジェクト設定の『ショットフォルダの親』を確認してください）")
            return
        target_dir = resolve_stage_dir(stage, shot_folder, self.active_stage_subpath)
        stem = os.path.splitext(os.path.basename(cur))[0]
        # 現シーン名基準のトークン置換（リネーム元→リネーム先 ＋ 初期テイク/ローカル）
        new_stem, replaced = apply_stage_rename(stem, stage, stages)
        if not replaced:
            rf = (stage.get("rename_from") or stage.get("rename_to")
                  or stage.get("name") or "").strip()
            QMessageBox.warning(
                self, "別工程へ保存",
                "リネーム元の文字列が現在のシーン名に見つかりませんでした。\n"
                "保存を中断しました。\n\n"
                "シーン名: %s\nリネーム元: %s\n\n"
                "工程設定の『リネーム元』が現在のシーン名に含まれているか"
                "（大文字小文字も含め）確認してください。"
                % (stem, rf or "（未設定）"))
            return
        saved = self._save_renamed(target_dir, new_stem, cmds)
        if saved:
            self.statusLabel.setText(
                f"✓  別工程保存 [{stage['name']}]: {stem} → {Path(saved).stem}（{target_dir}）")

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
            self._set_export_status("現在のシーンが未保存です（書き出し先がありません）")
            return

        # 手動書き出しも自動更新のスロットル基準時刻に反映する
        self._last_export_at[os.path.normcase(os.path.normpath(cur))] = time.time()
        method = self.exportMethodCombo.currentData() or "playblast"
        if method == "hardware":
            ok, msg, proc = export_hardware_background(cur)
            self._set_export_status(("▸  " if ok else "▲  ") + msg)
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
        stem = Path(scene_path).stem
        logp = os.path.join(os.path.dirname(scene_path), VIDEO_SUBDIR, stem,
                            "_oghw_log.txt")

        def _poll():
            # 完了判定: プロセス終了 / ワーカーが最後に書く _oghw_log.txt の生成 /
            # 30分のタイムアウト、のいずれか（mayabatch は終了が遅れることがある）。
            done = (proc.poll() is not None) or os.path.isfile(logp)
            if not done and (time.time() - started) < 1800:
                return
            timer.stop()
            try:
                self._hw_watchers.remove(timer)
            except Exception:
                pass
            frames = find_scene_sequence(scene_path)
            n = len(frames) if frames else 0
            tag = (label + " ") if label else ""
            if n > 0:
                self._set_export_status(
                    "✓  %s動画書き出し完了: %s（連番 %d 枚）" % (tag, stem, n))
                self.detailPanel.reload_video()
                self._refresh_all_shots_if_open()
            else:
                detail = ""
                try:
                    if os.path.isfile(logp):
                        with open(logp, "r", encoding="utf-8", errors="replace") as fh:
                            lines = fh.read().strip().splitlines()
                            detail = lines[-1] if lines else ""
                except Exception:
                    pass
                self._set_export_status(
                    "▲  %s書き出し完了しましたがフレーム未生成（_oghw_log.txt 参照）%s"
                    % (tag, ("／" + detail) if detail else ""))

        timer.timeout.connect(_poll)
        timer.start(700)
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
            self.statusLabel.setText(f"▲  出力フォルダ作成失敗: {e}")
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
            self._set_export_status("▲  プレイブラスト失敗（詳細はダイアログ参照）")
            return

        self._set_export_status(
            f"✓  動画書き出し完了: {Path(scene_path).stem}"
            f"（連番 {len(find_scene_sequence(scene_path))} 枚 → {seq_dir}）")
        self.detailPanel.reload_video()   # 選択中シーンならサイドバーで連番再生
        self._refresh_all_shots_if_open()

    def _open_in_explorer(self):
        """選択中のシーンのフォルダを OS のファイラで開く（ファイルを選択状態にする）。"""
        if not self._selected_path:
            return
        if reveal_in_explorer(self._selected_path):
            self.statusLabel.setText(
                f"▸  フォルダを開きました: {Path(self._selected_path).parent}"
            )
        else:
            self.statusLabel.setText("▲  フォルダを開けませんでした")

    def _save_scene_as(self):
        """別名保存。ブラウザで別の工程フォルダを選択中なら、その選択中フォルダに保存する。

        ファイル名は現在のシーン名を初期値として引き継ぎ、手入力で変更できる。
        Maya のホットキー／プロジェクトは変更しない。
        """
        try:
            import maya.cmds as cmds
        except ImportError:
            QMessageBox.information(
                self, "SAVE AS（スタンドアロンモード）",
                "Maya 内で実行すると、ブラウザで選択中のフォルダに、\n"
                "現在のシーン名を初期値とした別名保存ダイアログを表示します。",
                QMessageBox.Ok,
            )
            return

        cur = cmds.file(q=True, sceneName=True) or ""

        # 保存先フォルダの優先順位:
        #   1) ブラウザで選択中（リーブ中）のフォルダ ＝ 別工程を選んでいればそこ
        #   2) 現在開いているシーンのフォルダ
        #   3) 最後に選択したファイルのフォルダ / ワークスペース
        target = ""
        if self._current_folder and os.path.isdir(str(self._current_folder)):
            target = str(self._current_folder)
        if not target and cur:
            target = os.path.dirname(cur)
        if not target and self._selected_path:
            target = os.path.dirname(self._selected_path)
        if not target:
            try:
                target = cmds.workspace(q=True, dir=True)
            except Exception:
                target = ""
        if not target or not os.path.isdir(target):
            self.statusLabel.setText("保存先フォルダが未確定です（ブラウザでフォルダを選択してください）")
            return

        # 既定ファイル名 ＝ 現在のシーン名（拡張子込み）。手入力で変更可。
        default_name = os.path.basename(cur) if cur else "untitled.ma"
        name, ok = QInputDialog.getText(
            self, "SAVE AS",
            "保存先: %s\nファイル名:" % target, text=default_name)
        if not ok or not name.strip():
            return
        name = name.strip()

        # 拡張子の補完（無ければ現在のシーンの拡張子、既定 .ma）
        ext = os.path.splitext(name)[1].lower()
        if ext not in (".ma", ".mb"):
            cur_ext = os.path.splitext(cur)[1].lower() if cur else ".ma"
            if cur_ext not in (".ma", ".mb"):
                cur_ext = ".ma"
            name += cur_ext
            ext = cur_ext

        save_path = os.path.join(target, name)
        if os.path.exists(save_path):
            r = QMessageBox.question(
                self, "上書き確認",
                "%s は既に存在します。上書きしますか？" % name,
                QMessageBox.Yes | QMessageBox.No,
            )
            if r != QMessageBox.Yes:
                return

        ftype = "mayaAscii" if ext == ".ma" else "mayaBinary"
        try:
            cmds.file(rename=save_path)
            cmds.file(save=True, type=ftype)
        except Exception as e:
            self.statusLabel.setText("▲  保存に失敗しました: %s" % e)
            QMessageBox.warning(self, "保存失敗", str(e))
            return
        self.statusLabel.setText(f"✓  保存しました: {name} → {target}")
        self._reveal_saved(save_path)

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
            self.statusLabel.setText("▲  新規保存に失敗しました: %s" % e)
            QMessageBox.warning(self, "新規保存失敗", str(e))
            return
        self.statusLabel.setText(f"✓  新規シーンを保存しました: {Path(save_path).name}")
        # カラムをリセットせず、保存先まで展開して反映
        self._reveal_saved(save_path)


# ─── cv2 起動処理（import 前に保留アンインストールを実行） ──────────────────────
def _cv2_startup():
    """モジュール読み込み時の cv2 セットアップ。

    保留中の cv2 アンインストール予約があれば、cv2 を import する前に
    （＝ファイルがロックされていない状態で）実行して確定させる。
    予約が無ければ通常どおり cv2 を import する。
    """
    try:
        if get_pending_cv2_uninstall():
            set_pending_cv2_uninstall(False)
            ok, log = uninstall_opencv()
            print("[OG_Pipeline] 予約された cv2 アンインストールを実行しました:\n"
                  + (log or ""))
            return   # cv2 は import しない（無効化を確定）
    except Exception as e:
        print("[OG_Pipeline] 保留アンインストール処理でエラー:", e)
    _try_import_cv2()


_cv2_startup()


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


def _close_windows_named(name):
    """指定 objectName のトップレベルウィンドウを閉じる（reload 耐性・戻り値=閉じた数）。"""
    app = QApplication.instance()
    if app is None:
        return 0
    closed = 0
    for widget in app.topLevelWidgets():
        try:
            if widget.objectName() == name:
                widget.close()
                widget.deleteLater()
                closed += 1
        except Exception:
            pass
    return closed


def _close_existing_windows():
    """既存の OG_Pipeline メインウィンドウを閉じる（多重起動/ reload 防止）。"""
    return _close_windows_named(WINDOW_OBJECT_NAME)


def _resolve_project_entry():
    """ショットリスト用のプロジェクト設定を1件決める。
    起動時プロジェクト→単一→複数なら選択ダイアログ。無ければ None。"""
    roots = load_roots()
    if not roots:
        QMessageBox.warning(_get_maya_main_window(), "ショットリスト",
                            "プロジェクトが登録されていません。\n"
                            "メインツールの［プロジェクト設定］で登録してください。")
        return None
    name = get_startup_root()
    entry = find_root_entry(name) if name else None
    if entry is None:
        if len(roots) == 1:
            entry = roots[0]
        else:
            names = [r["name"] for r in roots]
            choice, ok = QInputDialog.getItem(
                _get_maya_main_window(), "ショットリスト",
                "プロジェクトを選択:", names, 0, False)
            if not ok or not choice:
                return None
            entry = find_root_entry(choice)
    return entry


def open_shot_list():
    """ショットリスト（全ショットウィンドウ）だけをスタンドアロンで開く公開関数。

    メインウィンドウを起動せず、登録済みプロジェクト（起動時設定→単一→選択）の
    ショットフォルダ親を対象に全ショットウィンドウを表示する。
    """
    if QApplication.instance() is None:
        print("[OG_Pipeline] エラー: Maya のスクリプトエディタから実行してください。")
        return None
    entry = _resolve_project_entry()
    if entry is None:
        return None
    shots_parent = entry.get("shots_parent") or entry.get("path")
    if not shots_parent or not os.path.isdir(str(shots_parent)):
        QMessageBox.warning(_get_maya_main_window(), "ショットリスト",
                            "ショットフォルダの親が見つかりません:\n%s" % shots_parent)
        return None
    _close_windows_named(SHOTLIST_OBJECT_NAME)
    maya_main = _get_maya_main_window()
    dlg = AllShotsDialog(shots_parent, parent=maya_main,
                         stage_subpath=entry.get("stage_subpath", ""),
                         stages=entry.get("stages", []),
                         subpath_label=entry.get("subpath_label", ""))
    dlg.setObjectName(SHOTLIST_OBJECT_NAME)
    dlg.setWindowTitle("OG_Pipeline — ショットリスト（%s）" % entry.get("name", ""))
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg


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
