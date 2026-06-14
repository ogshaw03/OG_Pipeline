"""
Maya Scene Opener Pipeline Tool

ルートフォルダ（スキャン対象）はツール上で設定し、QSettings に保存される。
コード内に固定パスは持たない。初回起動時にフォルダ選択を促す。
"""

import sys
from pathlib import Path

try:
    from PySide2 import QtWidgets, QtCore, QtGui
    from PySide2.QtCore import Qt, QThread, Signal, QSize, QTimer, QSettings
    from PySide2.QtGui import QFont, QColor, QPalette, QIcon, QPixmap, QPainter, QLinearGradient
    from PySide2.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QLineEdit,
        QSplitter, QFrame, QScrollArea, QComboBox, QMessageBox,
        QSizePolicy, QToolButton, QStatusBar, QProgressBar, QFileDialog
    )
except ImportError:
    try:
        from PySide6 import QtWidgets, QtCore, QtGui
        from PySide6.QtCore import Qt, QThread, Signal, QSize, QTimer, QSettings
        from PySide6.QtGui import QFont, QColor, QPalette, QIcon, QPixmap, QPainter, QLinearGradient
        from PySide6.QtWidgets import (
            QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
            QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QLineEdit,
            QSplitter, QFrame, QScrollArea, QComboBox, QMessageBox,
            QSizePolicy, QToolButton, QStatusBar, QProgressBar, QFileDialog
        )
    except ImportError:
        raise ImportError("PySide2 または PySide6 が必要です。")

# ─── 定数 ────────────────────────────────────────────────────────────────────
# ルートパスは固定せず、ユーザー設定（QSettings）に永続化する。
SETTINGS_ORG = "PipelineTools"
SETTINGS_APP = "MayaSceneOpener"
SETTINGS_KEY_ROOT = "root_path"
MAYA_EXTENSIONS = {".ma", ".mb"}

STYLE = """
/* ═══════════════════════════════════════════════
   OG_Pipeline Tool – Dark Industrial Theme
═══════════════════════════════════════════════ */

QMainWindow, QWidget {
    background-color: #0f1117;
    color: #c8ccd4;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 12px;
}

/* ─── ヘッダーバー ─── */
#header {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #1a1f2e, stop:0.5 #141824, stop:1 #0f1117);
    border-bottom: 2px solid #e8a838;
    padding: 0px;
}

#appTitle {
    font-family: "Consolas", monospace;
    font-size: 20px;
    font-weight: bold;
    color: #e8a838;
    letter-spacing: 4px;
}

#appSubtitle {
    font-size: 10px;
    color: #4a5568;
    letter-spacing: 2px;
}

#rootPathLabel {
    font-size: 10px;
    color: #4a9eff;
    letter-spacing: 1px;
    padding: 2px 8px;
    background: #141824;
    border-left: 2px solid #4a9eff;
}

/* ─── ツールバー ─── */
#toolbar {
    background: #141824;
    border-bottom: 1px solid #1e2435;
    padding: 4px 8px;
}

/* ─── 検索バー ─── */
#searchBar {
    background: #1a1f2e;
    border: 1px solid #2a3045;
    border-radius: 3px;
    color: #c8ccd4;
    padding: 5px 10px;
    font-family: "Consolas", monospace;
    font-size: 12px;
    min-height: 28px;
}
#searchBar:focus {
    border-color: #e8a838;
    background: #1e2435;
}
#searchBar::placeholder { color: #3a4055; }

/* ─── フィルター コンボ ─── */
QComboBox {
    background: #1a1f2e;
    border: 1px solid #2a3045;
    border-radius: 3px;
    color: #c8ccd4;
    padding: 4px 8px;
    min-height: 28px;
    font-family: "Consolas", monospace;
}
QComboBox:hover { border-color: #e8a838; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background: #1a1f2e;
    border: 1px solid #e8a838;
    color: #c8ccd4;
    selection-background-color: #2a2010;
    selection-color: #e8a838;
}

/* ─── ツリービュー ─── */
#fileTree {
    background: #0f1117;
    border: none;
    color: #c8ccd4;
    font-family: "Consolas", monospace;
    font-size: 12px;
    alternate-background-color: #111318;
    outline: none;
}
#fileTree::item {
    padding: 4px 2px;
    border-bottom: 1px solid #141824;
}
#fileTree::item:hover {
    background: #1a1f2e;
    color: #e8c87a;
}
#fileTree::item:selected {
    background: #2a2010;
    color: #e8a838;
    border-left: 3px solid #e8a838;
}
#fileTree::branch:has-children:!has-siblings:closed,
#fileTree::branch:closed:has-children:has-siblings {
    image: url(none);
    border-image: none;
}
QHeaderView::section {
    background: #141824;
    color: #e8a838;
    border: none;
    border-bottom: 1px solid #2a3045;
    border-right: 1px solid #1e2435;
    padding: 5px 8px;
    font-family: "Consolas", monospace;
    font-size: 11px;
    letter-spacing: 1px;
}

/* ─── 詳細パネル ─── */
#detailPanel {
    background: #0d1018;
    border-left: 2px solid #1e2435;
}
#detailTitle {
    color: #e8a838;
    font-size: 13px;
    font-weight: bold;
    letter-spacing: 2px;
    padding: 12px 16px 6px;
    border-bottom: 1px solid #1e2435;
}
#detailKey {
    color: #4a9eff;
    font-size: 11px;
}
#detailValue {
    color: #9aa3b0;
    font-size: 11px;
}
#detailFilename {
    color: #e8c87a;
    font-size: 13px;
    font-weight: bold;
    word-wrap: break-word;
}
#detailPath {
    color: #3a4a6a;
    font-size: 10px;
    word-wrap: break-word;
}

/* ─── ボタン ─── */
QPushButton {
    background: #1a1f2e;
    color: #c8ccd4;
    border: 1px solid #2a3045;
    border-radius: 3px;
    padding: 6px 16px;
    font-family: "Consolas", monospace;
    font-size: 12px;
    min-height: 28px;
}
QPushButton:hover {
    background: #1e2435;
    border-color: #4a9eff;
    color: #4a9eff;
}
QPushButton:pressed {
    background: #141824;
    border-color: #e8a838;
}

#openBtn {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #2a1e00, stop:1 #1a1300);
    color: #e8a838;
    border: 1px solid #e8a838;
    border-radius: 3px;
    padding: 8px 24px;
    font-size: 13px;
    font-weight: bold;
    letter-spacing: 2px;
    min-height: 36px;
    min-width: 140px;
}
#openBtn:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #3a2a00, stop:1 #2a1e00);
    color: #ffd060;
}
#openBtn:pressed {
    background: #1a1300;
}
#openBtn:disabled {
    background: #141824;
    color: #2a3045;
    border-color: #1e2435;
}

#importBtn {
    background: #0d1a1a;
    color: #3dcfb8;
    border: 1px solid #2a4a44;
    min-height: 36px;
    min-width: 120px;
    letter-spacing: 1px;
}
#importBtn:hover {
    background: #112222;
    border-color: #3dcfb8;
}
#importBtn:disabled {
    background: #141824;
    color: #2a3045;
    border-color: #1e2435;
}

#refreshBtn {
    background: #141824;
    color: #4a9eff;
    border: 1px solid #2a3045;
    padding: 5px 12px;
    min-height: 28px;
    letter-spacing: 1px;
}
#refreshBtn:hover {
    border-color: #4a9eff;
    background: #1a1f2e;
}

/* ─── スプリッター ─── */
QSplitter::handle {
    background: #1e2435;
    width: 2px;
}
QSplitter::handle:hover { background: #e8a838; }

/* ─── ステータスバー ─── */
QStatusBar {
    background: #0a0d14;
    color: #3a4055;
    border-top: 1px solid #1e2435;
    font-size: 11px;
    font-family: "Consolas", monospace;
    padding: 2px 8px;
}

/* ─── プログレスバー ─── */
QProgressBar {
    background: #1a1f2e;
    border: 1px solid #2a3045;
    border-radius: 2px;
    height: 4px;
    text-align: center;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #e8a838, stop:1 #ffd060);
    border-radius: 2px;
}

/* ─── スクロールバー ─── */
QScrollBar:vertical {
    background: #0f1117;
    width: 8px;
    border: none;
}
QScrollBar::handle:vertical {
    background: #2a3045;
    border-radius: 4px;
    min-height: 20px;
}
QScrollBar::handle:vertical:hover { background: #e8a838; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }

QScrollBar:horizontal {
    background: #0f1117;
    height: 8px;
    border: none;
}
QScrollBar::handle:horizontal {
    background: #2a3045;
    border-radius: 4px;
    min-width: 20px;
}
QScrollBar::handle:horizontal:hover { background: #e8a838; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }

/* ─── セパレーター ─── */
#separator {
    background: #1e2435;
    max-height: 1px;
}
"""

# ─── ファイルスキャン スレッド ────────────────────────────────────────────────
class ScanThread(QThread):
    found = Signal(list)     # [(rel_path, abs_path, size, mtime), ...]
    progress = Signal(str)
    finished_scan = Signal(int)

    def __init__(self, root: Path, extension_filter=None):
        super().__init__()
        self.root = root
        self.extension_filter = extension_filter  # None = all maya files

    def run(self):
        results = []
        if not self.root.exists():
            self.found.emit(results)
            self.finished_scan.emit(0)
            return
        try:
            for path in self.root.rglob("*"):
                # リフレッシュ等で中断要求が来たら速やかに抜ける
                if self.isInterruptionRequested():
                    return
                if path.suffix.lower() in MAYA_EXTENSIONS:
                    if self.extension_filter and path.suffix.lower() != self.extension_filter:
                        continue
                    try:
                        stat = path.stat()
                        rel = path.relative_to(self.root)
                        results.append((
                            str(rel),
                            str(path),
                            stat.st_size,
                            stat.st_mtime,
                        ))
                    except Exception:
                        pass
        except Exception as e:
            self.progress.emit(f"エラー: {e}")
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
        layout.addStretch()

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
        filename = p.name
        ext = p.suffix.lower()
        size_str = self._fmt_size(size)
        mtime_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d  %H:%M")

        # ─ ファイル名 ─
        fn_label = QLabel(filename)
        fn_label.setObjectName("detailFilename")
        fn_label.setWordWrap(True)
        self.contentLayout.addWidget(fn_label)

        # ─ パス ─
        path_label = QLabel(str(Path(rel_path).parent))
        path_label.setObjectName("detailPath")
        path_label.setWordWrap(True)
        self.contentLayout.addWidget(path_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #1e2435; margin: 4px 0;")
        self.contentLayout.addWidget(sep)

        # ─ タイプバッジ ─
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

        # ─ フルパス ─
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
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


# ─── ツリーアイテム（数値ソート対応） ──────────────────────────────────────────
class FileTreeItem(QTreeWidgetItem):
    """
    SIZE / MODIFIED 列を表示用の整形文字列ではなく、
    UserRole+1 に格納した生の数値で比較してソートするアイテム。
    数値が無い列（フォルダ名など）は通常の文字列比較にフォールバックする。
    """
    SORT_ROLE = Qt.UserRole + 1

    def __lt__(self, other):
        tree = self.treeWidget()
        col = tree.sortColumn() if tree is not None else 0
        a = self.data(col, self.SORT_ROLE)
        b = other.data(col, self.SORT_ROLE)
        if a is not None and b is not None:
            return a < b
        return super().__lt__(other)


# ─── メインウィンドウ ─────────────────────────────────────────────────────────
class MayaSceneOpener(QWidget):
    """
    QWidget ベース — Maya 内では QMainWindow を使わない。
    Maya のメインウィンドウを親に受け取り、独立した子ウィンドウとして表示する。
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        # Qt.Window フラグで独立ウィンドウとして表示（親が設定されていても）
        self.setWindowFlags(Qt.Window)
        self.setWindowTitle("OG_Pipeline — Scene Opener")
        self.setMinimumSize(1000, 680)
        self.resize(1200, 760)

        self._all_items = []
        self._selected_path = ""
        self._scan_thread = None

        # ルートパスをユーザー設定から復元（無ければ未設定）
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        saved = self.settings.value(SETTINGS_KEY_ROOT, "", type=str)
        self.root_path = Path(saved) if saved else None

        self.setStyleSheet(STYLE)
        self._build_ui()

        if self.root_path is None:
            # 初回起動: ウィンドウ表示後にフォルダ選択を促す
            QTimer.singleShot(0, self._set_root_path)
        else:
            self._refresh()

    # ════════════════════════════════════════════════════════════════════
    #  UI 構築
    # ════════════════════════════════════════════════════════════════════
    def _build_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ── ヘッダー ──
        root_layout.addWidget(self._build_header())

        # ── ツールバー ──
        root_layout.addWidget(self._build_toolbar())

        # ── メインコンテンツ (スプリッター) ──
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_file_panel())
        splitter.addWidget(self._build_detail_panel())
        splitter.setSizes([740, 280])
        root_layout.addWidget(splitter, 1)

        # ── ステータスバー (QWidget で自作、QMainWindow不要) ──
        status_bar = QWidget()
        status_bar.setStyleSheet(
            "background: #0a0d14; border-top: 1px solid #1e2435;"
        )
        status_bar.setFixedHeight(22)
        sb_layout = QHBoxLayout(status_bar)
        sb_layout.setContentsMargins(8, 0, 8, 0)
        sb_layout.setSpacing(8)

        self.statusLabel = QLabel("準備完了")
        self.statusLabel.setStyleSheet(
            "color: #3a4055; font-size: 11px; font-family: 'Consolas', monospace;"
        )
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

        # ロゴ + タイトル
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

        # ルートパス（ユーザー設定で動的に更新）
        self.rootPathLabel = QLabel()
        self.rootPathLabel.setObjectName("rootPathLabel")
        layout.addWidget(self.rootPathLabel)
        self._update_root_label()

        return header

    def _update_root_label(self):
        if self.root_path:
            self.rootPathLabel.setText(f"▸  {self.root_path}")
        else:
            self.rootPathLabel.setText("▸  ルート未設定")

    def _build_toolbar(self) -> QWidget:
        toolbar = QWidget()
        toolbar.setObjectName("toolbar")
        toolbar.setFixedHeight(48)
        layout = QHBoxLayout(toolbar)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        # 検索
        search_icon = QLabel("⌕")
        search_icon.setStyleSheet("color: #3a4055; font-size: 16px;")
        layout.addWidget(search_icon)

        self.searchBar = QLineEdit()
        self.searchBar.setObjectName("searchBar")
        self.searchBar.setPlaceholderText("ファイル名またはパスで検索…")
        self.searchBar.textChanged.connect(self._filter)
        layout.addWidget(self.searchBar, 1)

        # フィルター
        filter_label = QLabel("TYPE:")
        filter_label.setStyleSheet("color: #3a4055; font-size: 11px; letter-spacing: 1px;")
        layout.addWidget(filter_label)

        self.typeFilter = QComboBox()
        self.typeFilter.addItems(["ALL", ".ma", ".mb"])
        self.typeFilter.setFixedWidth(80)
        self.typeFilter.currentTextChanged.connect(self._filter)
        layout.addWidget(self.typeFilter)

        # セパレーター
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet("color: #1e2435;")
        layout.addWidget(sep)

        # ルートフォルダ設定
        self.rootBtn = QPushButton("📁  SET ROOT")
        self.rootBtn.setObjectName("refreshBtn")
        self.rootBtn.clicked.connect(self._set_root_path)
        layout.addWidget(self.rootBtn)

        # リフレッシュ
        self.refreshBtn = QPushButton("↻  REFRESH")
        self.refreshBtn.setObjectName("refreshBtn")
        self.refreshBtn.clicked.connect(self._refresh)
        layout.addWidget(self.refreshBtn)

        return toolbar

    def _set_root_path(self):
        """ルートフォルダをダイアログで選択し、QSettings に保存して再スキャンする。"""
        start = str(self.root_path) if self.root_path else str(Path.home())
        folder = QFileDialog.getExistingDirectory(
            self, "ルートフォルダを選択", start
        )
        if not folder:
            return
        self.root_path = Path(folder)
        self.settings.setValue(SETTINGS_KEY_ROOT, str(self.root_path))
        self._update_root_label()
        self._refresh()

    def _build_file_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ツリー
        self.tree = QTreeWidget()
        self.tree.setObjectName("fileTree")
        self.tree.setAlternatingRowColors(True)
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["  SHOT / FILE", "TYPE", "SIZE", "MODIFIED"])
        self.tree.setColumnWidth(0, 420)
        self.tree.setColumnWidth(1, 55)
        self.tree.setColumnWidth(2, 80)
        self.tree.setColumnWidth(3, 140)
        self.tree.setSortingEnabled(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setExpandsOnDoubleClick(True)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        layout.addWidget(self.tree, 1)

        # アクションバー
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
    #  データ取得・表示
    # ════════════════════════════════════════════════════════════════════
    def _refresh(self):
        # 実行中の古いスキャンを停止し、遅延結果による上書きを防ぐ
        if self._scan_thread is not None and self._scan_thread.isRunning():
            self._scan_thread.requestInterruption()
            self._scan_thread.quit()
            self._scan_thread.wait(3000)

        self.tree.clear()
        self._all_items = []
        self._selected_path = ""
        self.openBtn.setEnabled(False)
        self.importBtn.setEnabled(False)
        self.detailPanel.clear()

        # ルート未設定なら案内のみ表示してスキャンしない
        if not self.root_path:
            self.progressBar.hide()
            self.statusLabel.setText("ルートフォルダが未設定です — [SET ROOT] から選択してください")
            self.selectedLabel.setText("ルート未設定")
            return

        self.selectedLabel.setText("スキャン中…")
        self.progressBar.setRange(0, 0)
        self.progressBar.show()
        self.statusLabel.setText(f"スキャン中: {self.root_path}")
        self.refreshBtn.setEnabled(False)

        # スキャンは常に全 Maya ファイルを取得し、絞り込みは表示時(_filter)で行う。
        # （スキャン時に絞ると _all_items が偏り、タイプ切替で表示が消えるため）
        self._scan_thread = ScanThread(self.root_path, None)
        self._scan_thread.found.connect(self._on_scan_done)
        self._scan_thread.finished_scan.connect(self._on_scan_finished)
        self._scan_thread.start()

    def _on_scan_done(self, results: list):
        self._all_items = results
        # 現在の検索語・タイプフィルタを反映して表示する
        self._filter()

    def _on_scan_finished(self, count: int):
        self.progressBar.hide()
        self.refreshBtn.setEnabled(True)
        if count == 0:
            if not self.root_path or not self.root_path.exists():
                self.statusLabel.setText(f"⚠  パスが見つかりません: {self.root_path}")
                self.selectedLabel.setText("パスが存在しません")
            else:
                self.statusLabel.setText("Maya ファイルが見つかりませんでした")
                self.selectedLabel.setText("ファイルなし")
        else:
            self.statusLabel.setText(f"✓  {count} ファイルが見つかりました  |  {self.root_path}")
            self.selectedLabel.setText("ファイルを選択してください")

    def _populate_tree(self, items: list):
        self.tree.clear()
        import datetime

        # ショットフォルダ (rel_path の第一階層) でグルーピング
        groups: dict[str, list] = {}
        for rel, abs_p, size, mtime in items:
            parts = Path(rel).parts
            shot = parts[0] if len(parts) > 1 else "__ROOT__"
            groups.setdefault(shot, []).append((rel, abs_p, size, mtime))

        for shot in sorted(groups.keys()):
            if shot == "__ROOT__":
                parent = self.tree.invisibleRootItem()
            else:
                parent = QTreeWidgetItem(self.tree)
                parent.setText(0, f"  📁  {shot}")
                parent.setForeground(0, QColor("#6a7a9a"))
                parent.setFont(0, QFont("Consolas", 11, QFont.Bold))
                parent.setData(0, Qt.UserRole, None)  # フォルダはパスなし

            for rel, abs_p, size, mtime in sorted(groups[shot], key=lambda x: x[0]):
                p = Path(abs_p)
                ext = p.suffix.lower()
                mtime_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d  %H:%M")
                size_str = DetailPanel._fmt_size(size)
                type_color = "#e8a838" if ext == ".ma" else "#4a9eff"

                child = FileTreeItem()
                child.setText(0, f"    {p.name}")
                child.setText(1, ext.upper())
                child.setText(2, size_str)
                child.setText(3, mtime_str)
                child.setForeground(1, QColor(type_color))
                child.setForeground(2, QColor("#4a5568"))
                child.setForeground(3, QColor("#4a5568"))
                child.setData(0, Qt.UserRole, (rel, abs_p, size, mtime))
                # 数値ソート用の生の値（SIZE / MODIFIED 列）
                child.setData(2, FileTreeItem.SORT_ROLE, size)
                child.setData(3, FileTreeItem.SORT_ROLE, mtime)

                if shot == "__ROOT__":
                    self.tree.addTopLevelItem(child)
                else:
                    parent.addChild(child)

            if shot != "__ROOT__":
                parent.setExpanded(True)

    # ════════════════════════════════════════════════════════════════════
    #  フィルタリング
    # ════════════════════════════════════════════════════════════════════
    def _filter(self):
        query = self.searchBar.text().lower().strip()
        ext_filter = self.typeFilter.currentText()
        if ext_filter == "ALL":
            ext_filter = None

        filtered = []
        for rel, abs_p, size, mtime in self._all_items:
            if ext_filter and not abs_p.lower().endswith(ext_filter):
                continue
            if query and query not in rel.lower():
                continue
            filtered.append((rel, abs_p, size, mtime))
        self._populate_tree(filtered)
        self.statusLabel.setText(f"{len(filtered)} ファイル表示中  (全 {len(self._all_items)} 件)")

    # ════════════════════════════════════════════════════════════════════
    #  イベントハンドラ
    # ════════════════════════════════════════════════════════════════════
    def _on_selection_changed(self):
        items = self.tree.selectedItems()
        if not items:
            self._selected_path = ""
            self.openBtn.setEnabled(False)
            self.importBtn.setEnabled(False)
            self.selectedLabel.setText("ファイルを選択してください")
            self.detailPanel.clear()
            return

        item = items[0]
        data = item.data(0, Qt.UserRole)
        if data is None:
            # フォルダ行
            self._selected_path = ""
            self.openBtn.setEnabled(False)
            self.importBtn.setEnabled(False)
            self.selectedLabel.setText("フォルダ — ファイルを選択してください")
            self.detailPanel.clear()
            return

        rel, abs_p, size, mtime = data
        self._selected_path = abs_p
        self.openBtn.setEnabled(True)
        self.importBtn.setEnabled(True)
        short = Path(rel).name
        self.selectedLabel.setText(f"選択: {short}")
        self.detailPanel.update_info(rel, abs_p, size, mtime)

    def _on_double_click(self, item, column):
        data = item.data(0, Qt.UserRole)
        if data:
            self._open_scene()

    # ════════════════════════════════════════════════════════════════════
    #  Mayaアクション
    # ════════════════════════════════════════════════════════════════════
    def _open_scene(self):
        if not self._selected_path:
            return
        path = self._selected_path

        # Maya 内部から実行されている場合
        try:
            import maya.cmds as cmds

            # 未保存の変更を確認
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
                            self, "シーンを保存", "",
                            "Maya Files (*.ma *.mb)"
                        )
                        if not save_path:
                            return  # 保存をキャンセルしたらオープンも中止
                        ftype = "mayaAscii" if save_path.lower().endswith(".ma") else "mayaBinary"
                        cmds.file(rename=save_path)
                        cmds.file(save=True, type=ftype)

            cmds.file(path, open=True, force=True)
            self.statusLabel.setText(f"✓  シーンを開きました: {Path(path).name}")
        except ImportError:
            # スタンドアロン（テスト）モード
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
#   1) このファイルを Maya 標準の scripts フォルダに maya_scene_opener.py として保存。
#        Windows : <ドキュメント>/maya/scripts/   または  /maya/<version>/scripts/
#        macOS   : ~/Library/Preferences/Autodesk/maya/scripts/
#      ※ このフォルダは Maya 起動時に自動で sys.path に入るため、パス指定は不要。
#
#   2) Maya スクリプトエディタ（またはシェルフボタン）から下記を実行:
#
#       import importlib, maya_scene_opener
#       importlib.reload(maya_scene_opener)
#       maya_scene_opener.main()
#
#   3) 初回起動時にスキャンするルートフォルダを選択。設定は保存され次回以降は自動復元。
#      （ツールバーの [SET ROOT] でいつでも変更可能）
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

    # 既存ウィンドウを探して前面に出す
    for widget in QApplication.instance().topLevelWidgets():
        if isinstance(widget, MayaSceneOpener):
            widget.show()
            widget.raise_()
            widget.activateWindow()
            return widget

    # Maya メインウィンドウを親に設定して新規作成
    maya_main = _get_maya_main_window()
    win = MayaSceneOpener(parent=maya_main)
    win.show()
    win.raise_()
    win.activateWindow()
    return win


# ─── スタンドアロン実行（Maya 外での単体テスト用） ──────────────────────────────
# Maya の外（通常のターミナル）から `python maya_scene_opener.py` で起動できる。
# Maya 内ではこのブロックは実行されず、必ず main() を使うこと。
if __name__ == "__main__":
    app = QApplication.instance() or QApplication(sys.argv)
    window = MayaSceneOpener()
    window.show()
    sys.exit(app.exec_() if hasattr(app, "exec_") else app.exec())
