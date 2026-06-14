# -*- coding: utf-8 -*-
"""
OG_PlayBlastTool_UI - PySide UI (Maya 2023+)

Render Setup レンダーレイヤーの作成と、レイヤー単位のプレイブラストを行うツール。
コアロジック（レンダーレイヤー作成・プレイブラスト・テンプレート入出力・
出力先解決）は OG_PlayBlastTool_core を再利用し、UI のみ PySide2 / PySide6 で
構築している。
"""

import os
import maya.cmds as cmds
from maya import OpenMayaUI as omui

import OG_PlayBlastTool_core as core

# PySide2 (Maya 2023/2024) / PySide6 (Maya 2025+) 両対応
try:
    from PySide2 import QtCore, QtGui, QtWidgets
    from shiboken2 import wrapInstance
except ImportError:  # pragma: no cover
    from PySide6 import QtCore, QtGui, QtWidgets
    from shiboken6 import wrapInstance


MAIN_OBJECT_NAME = "PlayblastToolPySideWindow"
TPL_OBJECT_NAME  = "PlayblastTemplateEditorPySide"


# ---------------------------------------------------------------
# スタイル（cmd ライクに見えないモダンなダークテーマ）
# ---------------------------------------------------------------

STYLESHEET = """
QWidget {
    background-color: #2b2e33;
    color: #e6e6e6;
    font-family: 'Segoe UI', 'Yu Gothic UI', 'Meiryo', sans-serif;
    font-size: 11px;
}
QGroupBox {
    border: 1px solid #3c4047;
    border-radius: 6px;
    margin-top: 12px;
    padding: 8px 8px 6px 8px;
    background-color: #30343b;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 1px 6px;
    color: #8ab4f8;
    font-weight: bold;
}
QTabWidget::pane {
    border: 1px solid #3c4047;
    border-radius: 6px;
    top: -1px;
}
QTabBar::tab {
    background: #2b2e33;
    border: 1px solid #3c4047;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 5px 14px;
    margin-right: 2px;
}
QTabBar::tab:selected { background: #30343b; color: #8ab4f8; }
QLabel#hint { color: #9aa0a6; }
QLabel#preview {
    color: #c7d2e0;
    background-color: #25282d;
    border: 1px solid #3c4047;
    border-radius: 6px;
    padding: 8px;
}
QPushButton {
    background-color: #3a3f47;
    border: 1px solid #4a5059;
    border-radius: 5px;
    padding: 4px 10px;
}
QPushButton:hover   { background-color: #454b54; }
QPushButton:pressed { background-color: #2f343b; }
QPushButton#primary {
    background-color: #2e7d32;
    border: none;
    color: #ffffff;
    font-weight: bold;
}
QPushButton#primary:hover   { background-color: #34913a; }
QPushButton#primary:pressed { background-color: #276b2b; }
QPushButton#accent {
    background-color: #3367d6;
    border: none;
    color: #ffffff;
    font-weight: bold;
}
QPushButton#accent:hover   { background-color: #3b73e8; }
QPushButton#danger {
    background-color: #a23b3b;
    border: none;
    color: #ffffff;
    font-weight: bold;
}
QPushButton#danger:hover { background-color: #b84444; }
QPushButton#star {
    background-color: #8a7320;
    border: none;
    color: #fff8e1;
    font-weight: bold;
}
QPushButton#star:hover { background-color: #9c8226; }
QLineEdit, QComboBox, QSpinBox, QListWidget {
    background-color: #25282d;
    border: 1px solid #3c4047;
    border-radius: 5px;
    padding: 3px 5px;
    selection-background-color: #3d6fb5;
}
QListWidget::item { padding: 2px 4px; }
QListWidget::item:selected { background-color: #3d6fb5; color: #ffffff; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView {
    background-color: #25282d;
    selection-background-color: #3d6fb5;
    border: 1px solid #3c4047;
}
QCheckBox, QRadioButton { spacing: 6px; }
QScrollArea { border: none; }
QSplitter::handle { background-color: #3c4047; }
"""


def maya_main_window():
    ptr = omui.MQtUtil.mainWindow()
    if ptr is None:
        return None
    return wrapInstance(int(ptr), QtWidgets.QWidget)


def _delete_existing(object_name):
    """同名の既存ウィンドウを閉じて破棄する。"""
    parent = maya_main_window()
    if not parent:
        return
    for w in parent.findChildren(QtWidgets.QWidget, object_name):
        try:
            w.close()
            w.deleteLater()
        except Exception:
            pass


# ---------------------------------------------------------------
# テンプレートエディタ（別ウィンドウ）
# ---------------------------------------------------------------

class TemplateEditorDialog(QtWidgets.QDialog):

    def __init__(self, parent=None, on_saved=None):
        super(TemplateEditorDialog, self).__init__(parent)
        self._on_saved = on_saved
        self.setObjectName(TPL_OBJECT_NAME)
        self.setWindowTitle("OG_PlayBlastTool - テンプレート エディタ")
        self.setStyleSheet(STYLESHEET)
        self.setMinimumWidth(460)
        self._build()
        self._refresh_load_combo()
        self.populate(core.default_template())
        self._update_preview()

    # -- UI 構築 ---------------------------------------------------

    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # 既存テンプレート読込
        load_box = QtWidgets.QGroupBox("既存テンプレートを読み込み")
        load_lay = QtWidgets.QHBoxLayout(load_box)
        self.load_combo = QtWidgets.QComboBox()
        load_btn = QtWidgets.QPushButton("読み込み")
        load_btn.clicked.connect(self._on_load)
        load_lay.addWidget(self.load_combo, 1)
        load_lay.addWidget(load_btn)
        root.addWidget(load_box)

        # 基本設定
        base_box = QtWidgets.QGroupBox("基本設定")
        form = QtWidgets.QFormLayout(base_box)
        form.setLabelAlignment(QtCore.Qt.AlignRight)

        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setPlaceholderText("例: ShotReview_PNG")
        form.addRow("テンプレート名:", self.name_edit)

        self.format_combo = QtWidgets.QComboBox()
        self.format_combo.addItems(core.format_labels())
        form.addRow("書き出し形式:", self.format_combo)

        spin_row = QtWidgets.QHBoxLayout()
        self.padding_spin = QtWidgets.QSpinBox()
        self.padding_spin.setRange(1, 8)
        self.padding_spin.setValue(4)
        self.scale_spin = QtWidgets.QSpinBox()
        self.scale_spin.setRange(1, 200)
        self.scale_spin.setValue(100)
        self.scale_spin.setSuffix(" %")
        spin_row.addWidget(QtWidgets.QLabel("フレームパディング:"))
        spin_row.addWidget(self.padding_spin)
        spin_row.addSpacing(16)
        spin_row.addWidget(QtWidgets.QLabel("スケール:"))
        spin_row.addWidget(self.scale_spin)
        spin_row.addStretch(1)
        form.addRow("出力品質:", self._wrap(spin_row))

        self.ornaments_chk = QtWidgets.QCheckBox("オーナメント（HUD・解像度ゲート等）を表示する")
        self.panzoom_chk = QtWidgets.QCheckBox("2D Pan/Zoom を無効化してプレイブラストする")
        self.panzoom_chk.setChecked(True)
        form.addRow("", self.ornaments_chk)
        form.addRow("", self.panzoom_chk)

        # カメラ
        cam_row = QtWidgets.QHBoxLayout()
        self.cam_group = QtWidgets.QButtonGroup(self)
        self.cam_active = QtWidgets.QRadioButton("アクティブ")
        self.cam_persp  = QtWidgets.QRadioButton("パース(persp)")
        self.cam_render = QtWidgets.QRadioButton("レンダ設定")
        self.cam_active.setChecked(True)
        for rb in (self.cam_active, self.cam_persp, self.cam_render):
            self.cam_group.addButton(rb)
            cam_row.addWidget(rb)
        cam_row.addStretch(1)
        form.addRow("カメラ:", self._wrap(cam_row))
        root.addWidget(base_box)

        # 出力先（多階層）
        out_box = QtWidgets.QGroupBox("出力先（シーンファイル基準・多階層対応）")
        out_lay = QtWidgets.QVBoxLayout(out_box)

        lvl_row = QtWidgets.QHBoxLayout()
        lvl_row.addWidget(QtWidgets.QLabel("シーンフォルダから遡る階層数:"))
        self.level_spin = QtWidgets.QSpinBox()
        self.level_spin.setRange(0, 20)
        self.level_spin.valueChanged.connect(self._update_preview)
        lvl_row.addWidget(self.level_spin)
        lvl_row.addStretch(1)
        out_lay.addLayout(lvl_row)

        hint = QtWidgets.QLabel("サブフォルダ（上から順に階層化されます）")
        hint.setObjectName("hint")
        out_lay.addWidget(hint)

        self.sub_list = QtWidgets.QListWidget()
        self.sub_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.sub_list.setMaximumHeight(130)
        out_lay.addWidget(self.sub_list)

        add_row = QtWidgets.QHBoxLayout()
        self.sub_edit = QtWidgets.QLineEdit()
        self.sub_edit.setPlaceholderText("フォルダ名（playblast/v01 のように / 区切りも可）")
        self.sub_edit.returnPressed.connect(self._on_add_sub)
        add_btn = QtWidgets.QPushButton("追加")
        add_btn.clicked.connect(self._on_add_sub)
        add_row.addWidget(self.sub_edit, 1)
        add_row.addWidget(add_btn)
        out_lay.addLayout(add_row)

        mv_row = QtWidgets.QHBoxLayout()
        rm_btn = QtWidgets.QPushButton("選択を削除")
        up_btn = QtWidgets.QPushButton("↑ 上へ")
        dn_btn = QtWidgets.QPushButton("↓ 下へ")
        rm_btn.clicked.connect(self._on_remove_sub)
        up_btn.clicked.connect(lambda: self._move_sub(-1))
        dn_btn.clicked.connect(lambda: self._move_sub(1))
        for b in (rm_btn, up_btn, dn_btn):
            mv_row.addWidget(b)
        out_lay.addLayout(mv_row)
        root.addWidget(out_box)

        # プレビュー
        self.preview = QtWidgets.QLabel("")
        self.preview.setObjectName("preview")
        self.preview.setWordWrap(True)
        self.preview.setMinimumHeight(54)
        root.addWidget(self.preview)

        # 保存 / 削除 / 閉じる
        btn_row = QtWidgets.QHBoxLayout()
        save_btn = QtWidgets.QPushButton("保存")
        save_btn.setObjectName("primary")
        export_btn = QtWidgets.QPushButton("エクスポート...")
        del_btn = QtWidgets.QPushButton("削除")
        del_btn.setObjectName("danger")
        close_btn = QtWidgets.QPushButton("閉じる")
        save_btn.clicked.connect(self._on_save)
        export_btn.clicked.connect(self._on_export)
        del_btn.clicked.connect(self._on_delete)
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(save_btn, 2)
        btn_row.addWidget(export_btn, 2)
        btn_row.addWidget(del_btn, 1)
        btn_row.addWidget(close_btn, 1)
        root.addLayout(btn_row)

    @staticmethod
    def _wrap(layout):
        w = QtWidgets.QWidget()
        w.setLayout(layout)
        return w

    # -- データ <-> UI --------------------------------------------

    def _camera_mode(self):
        if self.cam_render.isChecked():
            return "render"
        if self.cam_persp.isChecked():
            return "persp"
        return "active"

    def collect(self):
        tpl = core.default_template()
        tpl["name"]            = self.name_edit.text().strip()
        fmt, comp = core.format_from_label(self.format_combo.currentText())
        tpl["format"], tpl["compression"] = fmt, comp
        tpl["show_ornaments"]  = self.ornaments_chk.isChecked()
        tpl["disable_panzoom"] = self.panzoom_chk.isChecked()
        tpl["camera_mode"]     = self._camera_mode()
        tpl["frame_padding"]   = self.padding_spin.value()
        tpl["scale_percent"]   = self.scale_spin.value()
        tpl["output_spec"]["scene_parent_level"] = self.level_spin.value()
        tpl["output_spec"]["subfolders"] = [
            self.sub_list.item(i).text() for i in range(self.sub_list.count())
        ]
        return core.merge_template(tpl)

    def populate(self, tpl):
        tpl = core.merge_template(tpl)
        self.name_edit.setText(tpl["name"])
        self.format_combo.setCurrentText(
            core.label_from_format(tpl["format"], tpl["compression"]))
        self.ornaments_chk.setChecked(bool(tpl["show_ornaments"]))
        self.panzoom_chk.setChecked(bool(tpl["disable_panzoom"]))
        mode = tpl.get("camera_mode", "active")
        self.cam_active.setChecked(mode == "active")
        self.cam_persp.setChecked(mode == "persp")
        self.cam_render.setChecked(mode == "render")
        self.padding_spin.setValue(int(tpl["frame_padding"]))
        self.scale_spin.setValue(int(tpl["scale_percent"]))
        self.level_spin.setValue(int(tpl["output_spec"]["scene_parent_level"]))
        self.sub_list.clear()
        self.sub_list.addItems(tpl["output_spec"]["subfolders"])

    # -- コールバック ---------------------------------------------

    def _refresh_load_combo(self, select=None):
        self.load_combo.blockSignals(True)
        self.load_combo.clear()
        names = core.template_display_names()   # 先頭に内部デフォルトを常に含む
        self.load_combo.addItems(names)
        if select and select in names:
            self.load_combo.setCurrentText(select)
        self.load_combo.blockSignals(False)

    def _on_load(self):
        name = self.load_combo.currentText()
        if not name:
            return
        self.populate(core.load_template_or_default(name))
        self._update_preview()

    def _on_add_sub(self):
        txt = self.sub_edit.text().strip()
        if not txt:
            return
        for part in txt.replace("\\", "/").split("/"):
            part = part.strip()
            if part:
                self.sub_list.addItem(part)
        self.sub_edit.clear()
        self._update_preview()

    def _on_remove_sub(self):
        for item in self.sub_list.selectedItems():
            self.sub_list.takeItem(self.sub_list.row(item))
        self._update_preview()

    def _move_sub(self, delta):
        row = self.sub_list.currentRow()
        if row < 0:
            return
        new = row + delta
        if new < 0 or new >= self.sub_list.count():
            return
        item = self.sub_list.takeItem(row)
        self.sub_list.insertItem(new, item)
        self.sub_list.setCurrentRow(new)
        self._update_preview()

    def _update_preview(self):
        tpl = self.collect()
        base = core.resolve_output_base(tpl, custom_folder=None)
        if base:
            msg = ("解決される出力先:\n{}\n"
                   "（各レイヤーはこの直下に <レイヤー名>/ で書き出されます）".format(base))
        else:
            subs = tpl["output_spec"]["subfolders"]
            rel = os.path.join(*subs) if subs else "(直下)"
            msg = ("シーン未保存のため絶対パスは未確定です。\n"
                   "シーンフォルダから {} 階層遡り → {}".format(
                       tpl["output_spec"]["scene_parent_level"], rel))
        self.preview.setText(msg)

    def _on_save(self):
        tpl = self.collect()
        if not tpl["name"]:
            QtWidgets.QMessageBox.warning(self, "エラー", "テンプレート名を入力してください。")
            return
        try:
            path = core.save_template(tpl)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "保存エラー", str(e))
            return
        self._refresh_load_combo(select=tpl["name"])
        if callable(self._on_saved):
            self._on_saved()
        QtWidgets.QMessageBox.information(
            self, "保存完了",
            "テンプレート '{}' を保存しました。\n保存先:\n{}".format(tpl["name"], path))

    def _on_export(self):
        tpl = self.collect()
        if not tpl["name"]:
            QtWidgets.QMessageBox.warning(self, "エラー", "先にテンプレート名を入力してください。")
            return
        start = os.path.join(core.get_templates_dir(), tpl["name"] + ".json")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "テンプレートをエクスポート", start, "JSON Files (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        import json
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(tpl, fh, ensure_ascii=False, indent=2)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "エクスポートエラー", str(e))
            return
        QtWidgets.QMessageBox.information(
            self, "エクスポート完了", "テンプレートをエクスポートしました:\n{}".format(path))

    def _on_delete(self):
        name = self.name_edit.text().strip()
        if not name:
            return
        ans = QtWidgets.QMessageBox.question(
            self, "削除確認", "テンプレート '{}' を削除しますか？".format(name),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if ans != QtWidgets.QMessageBox.Yes:
            return
        if core.delete_template(name):
            self._refresh_load_combo()
            if callable(self._on_saved):
                self._on_saved()
            QtWidgets.QMessageBox.information(self, "削除完了", "削除しました。")


# ---------------------------------------------------------------
# メインウィンドウ
# ---------------------------------------------------------------

class PlayblastToolWindow(QtWidgets.QDialog):

    def __init__(self, parent=None):
        super(PlayblastToolWindow, self).__init__(parent or maya_main_window())
        self.setObjectName(MAIN_OBJECT_NAME)
        self.setWindowTitle("OG_PlayBlastTool")
        self.setStyleSheet(STYLESHEET)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.Window)
        self.resize(560, 660)
        self.setMinimumSize(520, 560)

        # データモデル
        self._layer_defs    = []                       # [{"name": str}, ...]
        self._layer_obj_map = {}                        # name -> [long path, ...]
        self._layer_map     = {}                        # 表示名 -> renderLayer ノード
        self._current_template = core.default_template()

        self._build()
        self.refresh_template_combo()
        self.refresh_playblast_list()

        # 起動時テンプレートがあれば自動適用（'Default' は内部初期設定として常に有効）
        startup = core.get_startup_template()
        if not startup or not (core.is_default_template(startup)
                               or startup in core.list_template_names()):
            startup = core.DEFAULT_TEMPLATE_NAME
        idx = self.template_combo.findText(startup)
        if idx >= 0:
            self.template_combo.setCurrentIndex(idx)
        self.apply_template(core.load_template_or_default(startup))
        self._update_startup_label()

    # -- UI 構築 ---------------------------------------------------

    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # 最上部: プレイブラスト実行（常に表示）
        self.playblast_btn = QtWidgets.QPushButton("▶  選択したレイヤーをプレイブラスト")
        self.playblast_btn.setObjectName("primary")
        self.playblast_btn.setMinimumHeight(36)
        self.playblast_btn.clicked.connect(self.on_playblast)
        root.addWidget(self.playblast_btn)

        root.addWidget(self._build_template_box())

        # タブで縦の占有を抑える
        tabs = QtWidgets.QTabWidget()
        root.addWidget(tabs, 1)

        # タブ1: プレイブラスト（対象選択 / 設定 / 出力先）
        pb_tab = QtWidgets.QWidget()
        pb_lay = QtWidgets.QVBoxLayout(pb_tab)
        pb_lay.setContentsMargins(6, 8, 6, 6)
        pb_lay.setSpacing(8)
        pb_lay.addWidget(self._build_target_box(), 1)
        pb_lay.addWidget(self._build_settings_box())
        pb_lay.addWidget(self._build_output_box())
        tabs.addTab(pb_tab, "プレイブラスト")

        # タブ2: レンダーレイヤー作成
        cr_tab = QtWidgets.QWidget()
        cr_lay = QtWidgets.QVBoxLayout(cr_tab)
        cr_lay.setContentsMargins(6, 8, 6, 6)
        cr_lay.setSpacing(8)
        cr_lay.addWidget(self._build_layerdef_box(), 1)
        cr_lay.addWidget(self._build_create_box())
        tabs.addTab(cr_tab, "レイヤー作成")

    def _build_template_box(self):
        box = QtWidgets.QGroupBox("テンプレート（書き出し設定をプロジェクト横断で再利用）")
        lay = QtWidgets.QVBoxLayout(box)

        row = QtWidgets.QHBoxLayout()
        self.template_combo = QtWidgets.QComboBox()
        self.template_combo.currentIndexChanged.connect(self._on_template_changed)
        refresh_btn = QtWidgets.QPushButton("更新")
        apply_btn = QtWidgets.QPushButton("適用")
        apply_btn.setObjectName("accent")
        delete_btn = QtWidgets.QPushButton("削除")
        delete_btn.setObjectName("danger")
        import_btn = QtWidgets.QPushButton("インポート...")
        edit_btn = QtWidgets.QPushButton("テンプレート編集...")
        refresh_btn.clicked.connect(self.refresh_template_combo)
        apply_btn.clicked.connect(self.on_apply_template)
        delete_btn.clicked.connect(self.on_delete_template)
        import_btn.clicked.connect(self.on_import_template)
        edit_btn.clicked.connect(self.on_open_editor)
        row.addWidget(self.template_combo, 1)
        row.addWidget(refresh_btn)
        row.addWidget(apply_btn)
        row.addWidget(delete_btn)
        row.addWidget(import_btn)
        row.addWidget(edit_btn)
        lay.addLayout(row)

        srow = QtWidgets.QHBoxLayout()
        self.startup_label = QtWidgets.QLabel("起動時テンプレート: (未設定)")
        self.startup_label.setObjectName("hint")
        star_btn = QtWidgets.QPushButton("★ 次回も使用")
        star_btn.setObjectName("star")
        clear_btn = QtWidgets.QPushButton("解除")
        star_btn.clicked.connect(self.on_set_startup)
        clear_btn.clicked.connect(self.on_clear_startup)
        srow.addWidget(self.startup_label, 1)
        srow.addWidget(star_btn)
        srow.addWidget(clear_btn)
        lay.addLayout(srow)
        return box

    def _build_target_box(self):
        box = QtWidgets.QGroupBox("現在のレンダーレイヤー（プレイブラスト対象）")
        lay = QtWidgets.QVBoxLayout(box)
        sel_row = QtWidgets.QHBoxLayout()
        all_btn = QtWidgets.QPushButton("全選択")
        none_btn = QtWidgets.QPushButton("全解除")
        refresh_layers_btn = QtWidgets.QPushButton("リストを更新")
        all_btn.clicked.connect(lambda: self.playblast_list.selectAll())
        none_btn.clicked.connect(lambda: self.playblast_list.clearSelection())
        refresh_layers_btn.clicked.connect(self.refresh_playblast_list)
        sel_row.addWidget(all_btn)
        sel_row.addWidget(none_btn)
        sel_row.addStretch(1)
        sel_row.addWidget(refresh_layers_btn)
        lay.addLayout(sel_row)
        self.playblast_list = QtWidgets.QListWidget()
        self.playblast_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        lay.addWidget(self.playblast_list, 1)
        return box

    def _build_layerdef_box(self):
        box = QtWidgets.QGroupBox("レンダーレイヤー定義 / オブジェクトマッピング")
        rl = QtWidgets.QVBoxLayout(box)
        rl.addWidget(self._bold("新規作成するレンダーレイヤー一覧"))
        self.layer_def_list = QtWidgets.QListWidget()
        self.layer_def_list.currentRowChanged.connect(self._on_layer_def_selected)
        self.layer_def_list.setMaximumHeight(110)
        rl.addWidget(self.layer_def_list)

        def_row = QtWidgets.QHBoxLayout()
        self.new_layer_edit = QtWidgets.QLineEdit()
        self.new_layer_edit.setPlaceholderText("レイヤー名")
        self.new_layer_edit.returnPressed.connect(self.on_add_layer_def)
        add_btn = QtWidgets.QPushButton("追加")
        rm_btn = QtWidgets.QPushButton("選択削除")
        add_btn.clicked.connect(self.on_add_layer_def)
        rm_btn.clicked.connect(self.on_remove_layer_def)
        def_row.addWidget(self.new_layer_edit, 1)
        def_row.addWidget(add_btn)
        def_row.addWidget(rm_btn)
        rl.addLayout(def_row)

        rl.addWidget(self._bold("コレクションに追加するオブジェクト"))
        self.current_layer_label = QtWidgets.QLabel("レイヤーを選択してください")
        self.current_layer_label.setObjectName("hint")
        self.obj_count_label = QtWidgets.QLabel("")
        self.obj_count_label.setObjectName("hint")
        rl.addWidget(self.current_layer_label)
        rl.addWidget(self.obj_count_label)
        self.obj_list = QtWidgets.QListWidget()
        self.obj_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        rl.addWidget(self.obj_list, 1)

        obj_row = QtWidgets.QHBoxLayout()
        capture_btn = QtWidgets.QPushButton("選択を取り込む")
        capture_btn.setObjectName("accent")
        rm_obj_btn = QtWidgets.QPushButton("選択行を削除")
        clr_btn = QtWidgets.QPushButton("このレイヤーをクリア")
        capture_btn.clicked.connect(self.on_capture_selection)
        rm_obj_btn.clicked.connect(self.on_remove_sel_obj)
        clr_btn.clicked.connect(self.on_clear_layer_objs)
        obj_row.addWidget(capture_btn)
        obj_row.addWidget(rm_obj_btn)
        obj_row.addWidget(clr_btn)
        rl.addLayout(obj_row)
        clr_all_btn = QtWidgets.QPushButton("全レイヤーのオブジェクトをクリア")
        clr_all_btn.clicked.connect(self.on_clear_all_objs)
        rl.addWidget(clr_all_btn)
        return box

    def _build_create_box(self):
        box = QtWidgets.QGroupBox("レンダーレイヤー作成")
        lay = QtWidgets.QVBoxLayout(box)
        empty_btn = QtWidgets.QPushButton("▶  空のコレクションでレンダーレイヤーを作成")
        empty_btn.clicked.connect(lambda: self.on_create_layers(with_objects=False))
        with_btn = QtWidgets.QPushButton(
            "▶  レイヤーごとのオブジェクトをコレクションに含めて作成")
        with_btn.setObjectName("accent")
        with_btn.clicked.connect(lambda: self.on_create_layers(with_objects=True))
        lay.addWidget(empty_btn)
        lay.addWidget(with_btn)
        return box

    def _build_settings_box(self):
        box = QtWidgets.QGroupBox("プレイブラスト設定")
        form = QtWidgets.QFormLayout(box)
        form.setLabelAlignment(QtCore.Qt.AlignRight)

        self.format_combo = QtWidgets.QComboBox()
        self.format_combo.addItems(core.format_labels())
        form.addRow("書き出し形式:", self.format_combo)

        spin_row = QtWidgets.QHBoxLayout()
        self.scale_spin = QtWidgets.QSpinBox()
        self.scale_spin.setRange(1, 200)
        self.scale_spin.setValue(100)
        self.scale_spin.setSuffix(" %")
        self.padding_spin = QtWidgets.QSpinBox()
        self.padding_spin.setRange(1, 8)
        self.padding_spin.setValue(4)
        spin_row.addWidget(QtWidgets.QLabel("スケール:"))
        spin_row.addWidget(self.scale_spin)
        spin_row.addSpacing(16)
        spin_row.addWidget(QtWidgets.QLabel("フレームパディング:"))
        spin_row.addWidget(self.padding_spin)
        spin_row.addStretch(1)
        form.addRow("出力品質:", TemplateEditorDialog._wrap(spin_row))

        self.ornaments_chk = QtWidgets.QCheckBox("オーナメント（HUD・解像度ゲート等）を表示する")
        form.addRow("", self.ornaments_chk)

        cam_row = QtWidgets.QHBoxLayout()
        self.cam_group = QtWidgets.QButtonGroup(self)
        self.cam_active = QtWidgets.QRadioButton("アクティブカメラ")
        self.cam_persp  = QtWidgets.QRadioButton("パース(persp)")
        self.cam_render = QtWidgets.QRadioButton("レンダリング設定")
        self.cam_active.setChecked(True)
        self.cam_active.toggled.connect(self._on_camera_toggled)
        for rb in (self.cam_active, self.cam_persp, self.cam_render):
            self.cam_group.addButton(rb)
            cam_row.addWidget(rb)
        cam_row.addStretch(1)
        form.addRow("カメラ:", TemplateEditorDialog._wrap(cam_row))

        self.panzoom_chk = QtWidgets.QCheckBox(
            "2D Pan/Zoom を無効化してプレイブラスト（終了後に元に戻す）")
        self.panzoom_chk.setChecked(True)
        form.addRow("", self.panzoom_chk)

        hint = QtWidgets.QLabel(
            "ディスプレイサイズはレンダー設定 (defaultResolution) に従います。\n"
            "※ QuickTime / AVI は環境・Mayaバージョンにより利用できない場合があります。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        form.addRow("", hint)
        return box

    def _build_output_box(self):
        box = QtWidgets.QGroupBox("出力先フォルダ")
        lay = QtWidgets.QVBoxLayout(box)

        row = QtWidgets.QHBoxLayout()
        self.output_edit = QtWidgets.QLineEdit()
        self.output_edit.setPlaceholderText(
            "空欄の場合はテンプレートの出力先指定（シーン基準）に従います")
        self.output_edit.textChanged.connect(self._update_output_preview)
        browse_btn = QtWidgets.QPushButton("参照...")
        browse_btn.clicked.connect(self.on_browse_folder)
        row.addWidget(self.output_edit, 1)
        row.addWidget(browse_btn)
        lay.addLayout(row)

        row2 = QtWidgets.QHBoxLayout()
        scene_btn = QtWidgets.QPushButton("シーンフォルダを使用（クリア）")
        explorer_btn = QtWidgets.QPushButton("エクスプローラで開く")
        explorer_btn.setObjectName("accent")
        scene_btn.clicked.connect(lambda: self.output_edit.clear())
        explorer_btn.clicked.connect(self.on_open_explorer)
        row2.addWidget(scene_btn)
        row2.addWidget(explorer_btn)
        lay.addLayout(row2)

        self.backup_chk = QtWidgets.QCheckBox(
            "プレイブラスト前に old フォルダへバックアップする")
        lay.addWidget(self.backup_chk)

        self.output_preview = QtWidgets.QLabel("出力先: -")
        self.output_preview.setObjectName("preview")
        self.output_preview.setWordWrap(True)
        lay.addWidget(self.output_preview)
        return box

    @staticmethod
    def _bold(text):
        lbl = QtWidgets.QLabel(text)
        f = lbl.font()
        f.setBold(True)
        lbl.setFont(f)
        return lbl

    # -- テンプレート <-> UI --------------------------------------

    def _camera_mode(self):
        if self.cam_render.isChecked():
            return "render"
        if self.cam_persp.isChecked():
            return "persp"
        return "active"

    def collect_template(self):
        tpl = core.merge_template(self._current_template)
        fmt, comp = core.format_from_label(self.format_combo.currentText())
        tpl["format"], tpl["compression"] = fmt, comp
        tpl["show_ornaments"]  = self.ornaments_chk.isChecked()
        tpl["frame_padding"]   = self.padding_spin.value()
        tpl["scale_percent"]   = self.scale_spin.value()
        tpl["disable_panzoom"] = self.panzoom_chk.isChecked()
        tpl["camera_mode"]     = self._camera_mode()
        return tpl

    def apply_template(self, tpl):
        tpl = core.merge_template(tpl)
        self._current_template = tpl
        self.format_combo.setCurrentText(
            core.label_from_format(tpl["format"], tpl["compression"]))
        self.ornaments_chk.setChecked(bool(tpl["show_ornaments"]))
        self.padding_spin.setValue(int(tpl["frame_padding"]))
        self.scale_spin.setValue(int(tpl["scale_percent"]))
        self.panzoom_chk.setChecked(bool(tpl["disable_panzoom"]))
        mode = tpl.get("camera_mode", "active")
        self.cam_active.setChecked(mode == "active")
        self.cam_persp.setChecked(mode == "persp")
        self.cam_render.setChecked(mode == "render")
        self._update_output_preview()

    def _update_output_preview(self):
        custom = self.output_edit.text().strip()
        base = core.resolve_output_base(
            core.merge_template(self._current_template),
            custom_folder=(custom or None))
        self.output_preview.setText(
            "出力先: {}".format(base) if base else "出力先: (シーン未保存 / 未設定)")

    def _on_camera_toggled(self):
        # アクティブカメラ選択時は現在のビューカメラをキャッシュ
        if self.cam_active.isChecked():
            try:
                core._cached_active_camera = core._get_active_camera()
            except Exception:
                core._cached_active_camera = None
        else:
            core._cached_active_camera = None

    # -- テンプレート操作 -----------------------------------------

    def refresh_template_combo(self):
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        # 先頭に内部デフォルトを常に含める
        names = core.template_display_names()
        self.template_combo.addItems(names)
        # 適用中テンプレートが残っていれば選択表示を維持（表示と適用状態の不一致を防ぐ）
        current = (self._current_template or {}).get("name")
        if current in names:
            self.template_combo.setCurrentText(current)
        self.template_combo.blockSignals(False)

    def _on_template_changed(self, *args):
        """プルダウンの選択が変わったら即座に適用する（出力先指定も反映）。"""
        name = self.template_combo.currentText()
        if name:
            self.apply_template(core.load_template_or_default(name))

    def on_apply_template(self):
        name = self.template_combo.currentText()
        if not name:
            return
        self.apply_template(core.load_template_or_default(name))

    def on_delete_template(self):
        name = self.template_combo.currentText()
        if not name:
            return
        if core.is_default_template(name):
            QtWidgets.QMessageBox.information(
                self, "削除不可",
                "'{}' は内部の初期設定のため削除できません。".format(name))
            return
        ans = QtWidgets.QMessageBox.question(
            self, "削除確認", "テンプレート '{}' を削除しますか？".format(name),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if ans != QtWidgets.QMessageBox.Yes:
            return
        if core.delete_template(name):
            # 起動時テンプレートが削除対象なら設定も解除
            if core.get_startup_template() == name:
                core.clear_startup_template()
                self._update_startup_label()
            self.refresh_template_combo()

    def on_open_editor(self):
        _delete_existing(TPL_OBJECT_NAME)
        dlg = TemplateEditorDialog(parent=self, on_saved=self.refresh_template_combo)
        # 現在の設定を初期表示
        dlg.populate(self.collect_template())
        dlg._update_preview()
        dlg.show()

    def on_import_template(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "テンプレート JSON をインポート", "",
            "JSON Files (*.json);;All Files (*.*)")
        if not paths:
            return
        import json
        imported = []
        for src in paths:
            try:
                with open(src, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as e:
                cmds.warning("[PlayblastTool] 読込失敗 ({}): {}".format(src, e))
                continue
            tpl = core.merge_template(data)
            name = (tpl.get("name") or "").strip() or \
                os.path.splitext(os.path.basename(src))[0]
            tpl["name"] = name
            if os.path.isfile(core.template_path(name)):
                ans = QtWidgets.QMessageBox.question(
                    self, "上書き確認",
                    "テンプレート '{}' は既に存在します。上書きしますか？".format(name),
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.Yes)
                if ans != QtWidgets.QMessageBox.Yes:
                    continue
            try:
                core.save_template(tpl)
                imported.append(name)
            except Exception as e:
                cmds.warning("[PlayblastTool] 保存失敗 ({}): {}".format(name, e))
        if imported:
            self.refresh_template_combo()
            idx = self.template_combo.findText(imported[-1])
            if idx >= 0:
                self.template_combo.setCurrentIndex(idx)
            # インポートしたテンプレートを即適用（出力先指定も反映）
            self.apply_template(core.load_template_or_default(imported[-1]))
            QtWidgets.QMessageBox.information(
                self, "インポート完了",
                "{} 件のテンプレートをインポートしました:\n{}".format(
                    len(imported), "\n".join(imported)))
        else:
            QtWidgets.QMessageBox.information(
                self, "インポート", "インポートされたテンプレートはありません。")

    # -- 起動時テンプレート ---------------------------------------

    def _update_startup_label(self):
        name = core.get_startup_template()
        self.startup_label.setText(
            "起動時テンプレート: {}".format(name) if name else "起動時テンプレート: (未設定)")

    def on_set_startup(self):
        name = self.template_combo.currentText()
        if not name:
            return
        core.set_startup_template(name)
        self._update_startup_label()
        QtWidgets.QMessageBox.information(
            self, "設定完了", "次回起動時に '{}' を自動で適用します。".format(name))

    def on_clear_startup(self):
        core.clear_startup_template()
        self._update_startup_label()

    # -- レイヤー定義リスト（右ペイン上） --------------------------

    def refresh_layer_def_list(self):
        self.layer_def_list.blockSignals(True)
        self.layer_def_list.clear()
        for defn in self._layer_defs:
            name = defn["name"]
            cnt = len(self._layer_obj_map.get(name, []))
            label = "{} ({} objs)".format(name, cnt) if cnt else name
            self.layer_def_list.addItem(label)
        self.layer_def_list.blockSignals(False)

    def _selected_layer_def_name(self):
        item = self.layer_def_list.currentItem()
        if not item:
            return None
        return item.text().split(" (")[0]

    def _on_layer_def_selected(self, *args):
        name = self._selected_layer_def_name()
        if not name:
            self.current_layer_label.setText("レイヤーを選択してください")
            self.obj_count_label.setText("")
            self.obj_list.clear()
            return
        objs = self._layer_obj_map.get(name, [])
        self.current_layer_label.setText("レイヤー: {}".format(name))
        self.obj_count_label.setText("{}個のオブジェクト".format(len(objs)))
        self._refresh_obj_list(objs)

    def _refresh_obj_list(self, objs):
        self.obj_list.clear()
        self.obj_list.addItems([o.split("|")[-1] for o in objs])

    def on_add_layer_def(self):
        name = self.new_layer_edit.text().strip()
        if not name:
            return
        if any(d["name"] == name for d in self._layer_defs):
            QtWidgets.QMessageBox.information(
                self, "重複", "'{}' は既にリストにあります。".format(name))
            return
        self._layer_defs.append({"name": name})
        self._layer_obj_map.setdefault(name, [])
        self.new_layer_edit.clear()
        self.refresh_layer_def_list()

    def on_remove_layer_def(self):
        name = self._selected_layer_def_name()
        if not name:
            return
        self._layer_defs = [d for d in self._layer_defs if d["name"] != name]
        self._layer_obj_map.pop(name, None)
        self.refresh_layer_def_list()
        self.current_layer_label.setText("レイヤーを選択してください")
        self.obj_count_label.setText("")
        self.obj_list.clear()

    # -- オブジェクトマッピング -----------------------------------

    def on_capture_selection(self):
        name = self._selected_layer_def_name()
        if not name:
            QtWidgets.QMessageBox.information(
                self, "レイヤー未選択", "レイヤー一覧からレイヤーを選択してください。")
            return
        sel = cmds.ls(selection=True, long=True)
        if not sel:
            QtWidgets.QMessageBox.information(
                self, "選択なし", "ビューポートでオブジェクトを選択してください。")
            return
        current = self._layer_obj_map.setdefault(name, [])
        for obj in sel:
            if obj not in current:
                current.append(obj)
        self.obj_count_label.setText("{}個のオブジェクト".format(len(current)))
        self._refresh_obj_list(current)
        self.refresh_layer_def_list()

    def on_remove_sel_obj(self):
        name = self._selected_layer_def_name()
        if not name:
            return
        sel_short = {i.text() for i in self.obj_list.selectedItems()}
        current = self._layer_obj_map.get(name, [])
        self._layer_obj_map[name] = [
            o for o in current if o.split("|")[-1] not in sel_short]
        self.obj_count_label.setText(
            "{}個のオブジェクト".format(len(self._layer_obj_map[name])))
        self._refresh_obj_list(self._layer_obj_map[name])
        self.refresh_layer_def_list()

    def on_clear_layer_objs(self):
        name = self._selected_layer_def_name()
        if not name:
            return
        self._layer_obj_map[name] = []
        self.obj_count_label.setText("0個のオブジェクト")
        self.obj_list.clear()
        self.refresh_layer_def_list()

    def on_clear_all_objs(self):
        for k in self._layer_obj_map:
            self._layer_obj_map[k] = []
        self.obj_count_label.setText("0個のオブジェクト")
        self.obj_list.clear()
        self.refresh_layer_def_list()

    # -- レイヤー作成 ---------------------------------------------

    def on_create_layers(self, with_objects):
        if not self._layer_defs:
            QtWidgets.QMessageBox.information(
                self, "レイヤーなし", "作成するレイヤーがありません。")
            return
        obj_map = None
        if with_objects:
            total = sum(len(v) for v in self._layer_obj_map.values())
            if total == 0:
                ans = QtWidgets.QMessageBox.question(
                    self, "オブジェクト未設定",
                    "すべてのレイヤーにオブジェクトが設定されていません。\n"
                    "空のコレクションで作成しますか？",
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No)
                if ans != QtWidgets.QMessageBox.Yes:
                    return
            obj_map = self._layer_obj_map

        results = core.create_render_setup_layers(self._layer_defs, layer_objects_map=obj_map)
        lines = []
        for r in results:
            icon = "✓" if r["status"] in ("created", "exists") else "✗"
            lines.append("{} {}  |  {}".format(icon, r["layer"], r["message"]))
        QtWidgets.QMessageBox.information(
            self, "レンダーレイヤー作成完了",
            "\n".join(lines) if lines else "結果なし")
        # 作成済みなので新規リストをクリア
        self._layer_defs = []
        self._layer_obj_map = {}
        self.refresh_layer_def_list()
        self.obj_list.clear()
        self.current_layer_label.setText("レイヤーを選択してください")
        self.obj_count_label.setText("")
        self.refresh_playblast_list()

    # -- プレイブラスト対象リスト ---------------------------------

    def refresh_playblast_list(self):
        self._layer_map = {}
        self.playblast_list.clear()
        layers = core.get_render_layers()
        for layer in layers:
            disp = core.strip_rs_prefix(layer)
            self._layer_map[disp] = layer
            self.playblast_list.addItem(disp)
        self.playblast_list.selectAll()

    # -- 出力先 ---------------------------------------------------

    def on_browse_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "出力先フォルダを選択")
        if folder:
            self.output_edit.setText(folder)

    def on_open_explorer(self):
        custom = self.output_edit.text().strip()
        folder = core.resolve_output_base(
            core.merge_template(self._current_template),
            custom_folder=(custom or None))
        if not folder:
            QtWidgets.QMessageBox.warning(
                self, "エラー", "出力先フォルダが設定されていません。")
            return
        core.open_in_explorer(folder)

    # -- プレイブラスト実行 ---------------------------------------

    def on_playblast(self):
        selected = [i.text() for i in self.playblast_list.selectedItems()]
        if not selected:
            QtWidgets.QMessageBox.information(
                self, "選択なし", "プレイブラストするレンダーレイヤーを選択してください。")
            return
        custom = self.output_edit.text().strip()
        base_folder = custom if custom else None
        template = self.collect_template()

        if self.backup_chk.isChecked():
            folder = core.resolve_output_base(template, custom_folder=base_folder)
            if folder and os.path.isdir(folder):
                targets = [core.strip_rs_prefix(self._layer_map.get(d, d)) for d in selected]
                dest = core.backup_output_folder(folder, target_layer_names=targets)
                if dest:
                    print("[PlayblastTool] バックアップ完了: {}".format(dest))

        node_names = [self._layer_map.get(d, d) for d in selected]
        core.run_playblast(node_names, base_folder=base_folder, template=template)


# ---------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------

_window = None


def main():
    global _window
    _delete_existing(MAIN_OBJECT_NAME)
    _window = PlayblastToolWindow()
    _window.show()
    _window.raise_()
    return _window


if __name__ == "__main__":
    main()
