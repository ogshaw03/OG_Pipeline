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
import json
from pathlib import Path

try:
    from PySide2 import QtWidgets, QtCore, QtGui
    from PySide2.QtCore import Qt, QThread, Signal, QSize, QTimer
    from PySide2.QtGui import QFont, QColor, QPalette, QIcon, QPixmap, QPainter, QLinearGradient
    from PySide2.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QLineEdit,
        QSplitter, QFrame, QScrollArea, QComboBox, QMessageBox,
        QSizePolicy, QToolButton, QStatusBar, QProgressBar, QFileDialog,
        QListWidget, QListWidgetItem, QInputDialog
    )
except ImportError:
    try:
        from PySide6 import QtWidgets, QtCore, QtGui
        from PySide6.QtCore import Qt, QThread, Signal, QSize, QTimer
        from PySide6.QtGui import QFont, QColor, QPalette, QIcon, QPixmap, QPainter, QLinearGradient
        from PySide6.QtWidgets import (
            QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
            QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QLineEdit,
            QSplitter, QFrame, QScrollArea, QComboBox, QMessageBox,
            QSizePolicy, QToolButton, QStatusBar, QProgressBar, QFileDialog,
            QListWidget, QListWidgetItem, QInputDialog
        )
    except ImportError:
        raise ImportError("PySide2 または PySide6 が必要です。")

# ─── 定数 ────────────────────────────────────────────────────────────────────
MAYA_EXTENSIONS = {".ma", ".mb"}


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
    """任意の入力を [{'name','path'}, ...] に正規化する。"""
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
        out.append({"name": name, "path": path})
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


def add_root(name, path):
    """ルートを追加（同名は上書き）し、名前順で保存する。"""
    roots = [r for r in load_roots() if r["name"] != name]
    roots.append({"name": name, "path": path})
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
#browserColumn {
    background: #0f1117; border: none; border-right: 1px solid #1e2435;
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

        self.content = QWidget()
        self.content.setStyleSheet("background: transparent;")
        self.contentLayout = QVBoxLayout(self.content)
        self.contentLayout.setContentsMargins(16, 12, 16, 12)
        self.contentLayout.setSpacing(8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.content)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        layout.addWidget(scroll)

        self.clear()

    def _clear_layout(self):
        while self.contentLayout.count():
            item = self.contentLayout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def clear(self):
        self._clear_layout()
        placeholder = QLabel("ファイルを選択すると\n詳細が表示されます")
        placeholder.setStyleSheet("color: #2a3045; font-size: 11px;")
        placeholder.setAlignment(Qt.AlignCenter)
        self.contentLayout.addWidget(placeholder)
        self.contentLayout.addStretch()

    def update_info(self, rel_path: str, abs_path: str, size: int, mtime: float):
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

    COL_WIDTH = 240

    def __init__(self, parent=None):
        super().__init__(parent)
        self.root = None
        self.ext_filter = None       # None / ".ma" / ".mb"
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
        self.scroll.setWidget(self.container)
        outer.addWidget(self.scroll)

    # ── 公開 API ──────────────────────────────────────────────
    def set_root(self, path):
        self.root = Path(path) if path else None
        self.refresh()

    def set_ext_filter(self, ext):
        self.ext_filter = ext
        self.refresh()

    def refresh(self):
        self._clear_columns()
        if self.root and self.root.exists():
            self._add_column(self.root)

    def show_search_results(self, items):
        """検索結果（[(rel, abs, size, mtime), ...]）を 1 カラムにフラット表示する。"""
        self._clear_columns()
        lw = self._make_column(width=self.COL_WIDTH * 2)
        for rel, abs_p, _size, _mtime in items:
            it = QListWidgetItem(rel)
            it.setData(Qt.UserRole, ("file", abs_p))
            ext = Path(abs_p).suffix.lower()
            it.setForeground(QColor("#e8a838" if ext == ".ma" else "#4a9eff"))
            lw.addItem(it)
        self.hbox.addWidget(lw)
        self._columns.append(lw)
        self._update_width()

    # ── 内部処理 ──────────────────────────────────────────────
    def _clear_columns(self):
        for w in self._columns:
            w.setParent(None)
            w.deleteLater()
        self._columns = []
        self._update_width()

    def _make_column(self, width=None):
        lw = QListWidget()
        lw.setObjectName("browserColumn")
        lw.setFixedWidth(width or self.COL_WIDTH)
        lw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lw.itemClicked.connect(lambda item, w=lw: self._on_clicked(w, item))
        lw.itemDoubleClicked.connect(lambda item, w=lw: self._on_double(w, item))
        return lw

    def _list_dir(self, dir_path):
        dirs, files = [], []
        try:
            with os.scandir(str(dir_path)) as it:
                for entry in it:
                    try:
                        if entry.is_dir():
                            dirs.append(entry.name)
                        elif entry.is_file():
                            suf = Path(entry.name).suffix.lower()
                            if suf in MAYA_EXTENSIONS:
                                if self.ext_filter and suf != self.ext_filter:
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
        lw = self._make_column()
        dirs, files = self._list_dir(dir_path)
        for name in dirs:
            it = QListWidgetItem(f"📁  {name}")
            it.setData(Qt.UserRole, ("dir", str(Path(dir_path) / name)))
            it.setForeground(QColor("#cdb27a"))
            lw.addItem(it)
        for name in files:
            p = Path(dir_path) / name
            ext = p.suffix.lower()
            it = QListWidgetItem(f"    {name}")
            it.setData(Qt.UserRole, ("file", str(p)))
            it.setForeground(QColor("#e8a838" if ext == ".ma" else "#4a9eff"))
            lw.addItem(it)
        self.hbox.addWidget(lw)
        self._columns.append(lw)
        self._update_width()
        QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(lw))

    def _trim_after(self, lw):
        """lw より右のカラムをすべて取り除く。"""
        try:
            idx = self._columns.index(lw)
        except ValueError:
            return
        while len(self._columns) > idx + 1:
            w = self._columns.pop()
            w.setParent(None)
            w.deleteLater()
        self._update_width()

    def _on_clicked(self, lw, item):
        self._trim_after(lw)
        kind, path = item.data(Qt.UserRole)
        if kind == "dir":
            self.file_selected.emit(None)
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
        total = sum(w.width() for w in self._columns)
        self.container.setMinimumWidth(max(1, total))


# ─── メインウィンドウ ─────────────────────────────────────────────────────────
class OGPipelineWindow(QWidget):
    """
    QWidget ベース — Maya 内では QMainWindow を使わない。
    Maya のメインウィンドウを親に受け取り、独立した子ウィンドウとして表示する。
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Window)
        self.setWindowTitle("OG_Pipeline — Scene Opener")
        self.setMinimumSize(1000, 680)
        self.resize(1240, 760)

        self._selected_path = ""
        self._scan_thread = None
        self._pending_query = ""
        self._loading_combo = False
        self.active_root = None

        self.setStyleSheet(STYLE)
        self._build_ui()

        # 起動時: 登録済みルートを読み込み、必要なら自動適用ルートを選択する
        self._reload_roots_combo()
        if self.rootCombo.count() > 0:
            self._select_in_combo(get_startup_root())
        self._apply_root()

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

        self.addRootBtn = QPushButton("＋  追加")
        self.addRootBtn.setObjectName("refreshBtn")
        self.addRootBtn.setToolTip("フォルダを選んでルートを新規登録")
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

        layout.addStretch()
        return bar

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
        layout.addWidget(self.browser, 1)

        action_bar = QWidget()
        action_bar.setStyleSheet("background: #0a0d14; border-top: 1px solid #1e2435;")
        action_bar.setFixedHeight(56)
        ab_layout = QHBoxLayout(action_bar)
        ab_layout.setContentsMargins(16, 8, 16, 8)

        self.selectedLabel = QLabel("ファイルが選択されていません")
        self.selectedLabel.setStyleSheet("color: #2a3045; font-size: 11px;")
        ab_layout.addWidget(self.selectedLabel, 1)

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
        return panel

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
            self.rootPathLabel.setText("▸  ルート未登録")
            self.browser.set_root(None)
            self.statusLabel.setText(
                "プロジェクトルート未登録 — [＋ 追加] か [⭳ インポート] で登録してください"
            )
            return
        self.active_root = find_root_path(name)
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
        add_root(name, folder)
        self._reload_roots_combo(select_name=name)
        self._apply_root()
        self.statusLabel.setText(f"✓  ルートを追加: {name}")

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
        filtered = [r for r in results if q in r[0].lower()]
        self.browser.show_search_results(filtered)
        self.statusLabel.setText(f"検索: {len(filtered)} 件ヒット  |  {self.active_root}")

    # ════════════════════════════════════════════════════════════════════
    #  選択
    # ════════════════════════════════════════════════════════════════════
    def _on_file_selected(self, info):
        if not info:
            self._selected_path = ""
            self.openBtn.setEnabled(False)
            self.importBtn.setEnabled(False)
            self.selectedLabel.setText("ファイルを選択してください")
            self.detailPanel.clear()
            return
        self._selected_path = info["abs"]
        self.openBtn.setEnabled(True)
        self.importBtn.setEnabled(True)
        self.selectedLabel.setText(f"選択: {Path(info['abs']).name}")
        self.detailPanel.update_info(info["rel"], info["abs"], info["size"], info["mtime"])

    def _open_path(self, path):
        self._selected_path = path
        self._open_scene()

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
                    # 新規（未名）シーンは save できないため saveAs ダイアログを出す
                    if cmds.file(q=True, sceneName=True):
                        cmds.file(save=True)
                    else:
                        save_path, _ = QFileDialog.getSaveFileName(
                            self, "シーンを保存", "", "Maya Files (*.ma *.mb)"
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
#   3) 初回は [＋ 追加] でプロジェクトルートを登録（または [⭳ インポート] で JSON を取込）。
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


def main():
    """
    外部から呼び出す公開関数。
    既にウィンドウが開いている場合は前面に移動する（多重起動防止）。
    """
    if QApplication.instance() is None:
        print("[OG_Pipeline] エラー: Maya のスクリプトエディタから実行してください。")
        return None

    for widget in QApplication.instance().topLevelWidgets():
        if isinstance(widget, OGPipelineWindow):
            widget.show()
            widget.raise_()
            widget.activateWindow()
            return widget

    maya_main = _get_maya_main_window()
    win = OGPipelineWindow(parent=maya_main)
    win.show()
    win.raise_()
    win.activateWindow()
    return win


# ─── スタンドアロン実行（Maya 外での単体テスト用） ──────────────────────────────
if __name__ == "__main__":
    app = QApplication.instance() or QApplication(sys.argv)
    window = OGPipelineWindow()
    window.show()
    sys.exit(app.exec_() if hasattr(app, "exec_") else app.exec())
