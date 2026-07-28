# -*- coding: utf-8 -*-
"""Keyframe Reducer — キーフレーム整理ツール（Maya 2024）。

Source の対象アトリビュートのキー時刻に合わせて、Target の対象アトリビュートのキーを
間引く（＝Source がキーを持たないフレームの Target キーを削除する）。値は焼いたまま
残るので、フルベイク後の状態からブロッキング時のキータイミングへ戻す用途に使える。

Target のアトリビュートは:
  - ALL    … キーのある全アトリビュート
  - SELECT … 位置 / 回転 / スケール のうちチェックしたものだけ
（Source 側の「対象アトリビュート」も同じ設定で判定してタイミングを集める）

起動（Maya の Script Editor / Python）:
    import KeyframeReducer
    KeyframeReducer.show()
"""

import maya.cmds as cmds
import maya.OpenMayaUI as omui

try:
    from PySide2 import QtWidgets, QtCore
    from shiboken2 import wrapInstance
except ImportError:                       # 将来の PySide6 対応
    from PySide6 import QtWidgets, QtCore
    from shiboken6 import wrapInstance


__version__ = "0.2.0"
WINDOW = "keyframeReducerWin"

# SELECT で選べるトランスフォーム種別（表示名, 内部キー, チャンネル）
_TYPES = [("位置", "translate", ["translateX", "translateY", "translateZ"]),
          ("回転", "rotate", ["rotateX", "rotateY", "rotateZ"]),
          ("スケール", "scale", ["scaleX", "scaleY", "scaleZ"])]
_CH = {k: ch for (_lbl, k, ch) in _TYPES}
_EPS = 1e-4


# --------------------------------------------------------------------------- #
# ロジック（UI 非依存・単体テスト可能）
# --------------------------------------------------------------------------- #
def _animated_plugs(node, mode, types):
    """node で対象となる 'node.attr'（キーを持つもの）のリストを返す。

    mode == "all"    … キーのある全 animatable アトリビュート
    mode == "select" … types（translate/rotate/scale）のチャンネルのうちキーがあるもの
    """
    plugs = []
    if mode == "all":
        for p in (cmds.listAnimatable(node) or []):
            try:
                if cmds.keyframe(p, q=True, keyframeCount=True):
                    plugs.append(p)
            except Exception:
                pass
    else:
        chans = []
        for t in types:
            chans += _CH.get(t, [])
        for ch in chans:
            p = "%s.%s" % (node, ch)
            if cmds.objExists(p):
                try:
                    if cmds.keyframe(p, q=True, keyframeCount=True):
                        plugs.append(p)
                except Exception:
                    pass
    return plugs


def _source_frames(sources, mode, types):
    """Source 群の対象アトリビュートのキー時刻の集合（丸め済み）。"""
    frames = set()
    for s in sources:
        for p in _animated_plugs(s, mode, types):
            for t in (cmds.keyframe(p, q=True) or []):
                frames.add(round(float(t), 4))
    return frames


def reduce_keys(sources, targets, mode, types):
    """Target のキーを Source のキー時刻に合わせて間引く。

    戻り値: dict(deleted=削除キー数, plugs=処理したアトリビュート数,
                skipped=Sourceと交差せず全削除回避したアトリビュート数)
    """
    src = _source_frames(sources, mode, types)
    if not src:
        raise RuntimeError("Source にキーが見つかりません（選択・対象アトリビュートを確認）。")

    deleted = 0
    plugs = 0
    skipped = 0
    cmds.undoInfo(openChunk=True)
    try:
        for tgt in targets:
            for p in _animated_plugs(tgt, mode, types):
                plugs += 1
                times = sorted(cmds.keyframe(p, q=True) or [])
                if not times:
                    continue
                kept = [t for t in times if round(float(t), 4) in src]
                if not kept:
                    # Source と1つも一致しない → 全削除は危険なのでスキップ
                    skipped += 1
                    continue
                lo = times[0] - 1.0
                hi = times[-1] + 1.0
                ranges = [(lo, kept[0] - _EPS)]
                for i in range(len(kept) - 1):
                    ranges.append((kept[i] + _EPS, kept[i + 1] - _EPS))
                ranges.append((kept[-1] + _EPS, hi))
                for (a, b) in ranges:
                    if b <= a:
                        continue
                    n = cmds.keyframe(p, q=True, time=(a, b), keyframeCount=True) or 0
                    if n:
                        cmds.cutKey(p, time=(a, b), clear=True)
                        deleted += n
    finally:
        cmds.undoInfo(closeChunk=True)
    return {"deleted": deleted, "plugs": plugs, "skipped": skipped}


# --------------------------------------------------------------------------- #
# ロケータ経由フルベイク（親子反転）
# --------------------------------------------------------------------------- #
_BAKE_ATTRS = ["translateX", "translateY", "translateZ",
               "rotateX", "rotateY", "rotateZ"]


def _playback_range():
    return (cmds.playbackOptions(q=True, min=True),
            cmds.playbackOptions(q=True, max=True))


def bake_via_locator(objs):
    """各オブジェクトについて次を一括実行する:
      1. オブジェクトの位置にロケータ作成
      2. オブジェクトを親にペアレントコンストレイン（ロケータがオブジェクトに追従）
      3. ロケータをフルベイク（毎フレーム）
      4. 親子反転（ロケータを親、オブジェクトを子）
      5. オブジェクトをフルベイク
    戻り値: 作成したロケータ名のリスト。
    """
    start, end = _playback_range()
    created = []
    cmds.undoInfo(openChunk=True)
    try:
        for obj in objs:
            short = obj.split("|")[-1].split(":")[-1]
            loc = cmds.spaceLocator(name="%s_bakeLoc" % short)[0]
            # 1. 位置（＋回転）を合わせる
            cmds.matchTransform(loc, obj, position=True, rotation=True)
            # 2. オブジェクトが親 → ロケータが追従
            pc1 = cmds.parentConstraint(obj, loc, maintainOffset=False)[0]
            # 3. ロケータをフルベイク
            cmds.bakeResults(loc, time=(start, end), attribute=_BAKE_ATTRS,
                             sampleBy=1, simulation=True,
                             preserveOutsideKeys=True,
                             disableImplicitControl=True,
                             sparseAnimCurveBake=False)
            cmds.delete(pc1)
            # 4. 親子反転（オブジェクトの既存キーを外してから、ロケータを親に）
            cmds.cutKey(obj, attribute=_BAKE_ATTRS, clear=True)
            pc2 = cmds.parentConstraint(loc, obj, maintainOffset=False)[0]
            # 5. オブジェクトをフルベイク
            cmds.bakeResults(obj, time=(start, end), attribute=_BAKE_ATTRS,
                             sampleBy=1, simulation=True,
                             preserveOutsideKeys=True,
                             disableImplicitControl=True,
                             sparseAnimCurveBake=False)
            cmds.delete(pc2)
            created.append(loc)
    finally:
        cmds.undoInfo(closeChunk=True)
    return created


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def _maya_main_window():
    ptr = omui.MQtUtil.mainWindow()
    if ptr is not None:
        return wrapInstance(int(ptr), QtWidgets.QWidget)
    return None


class KeyframeReducer(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super(KeyframeReducer, self).__init__(parent)
        self.setObjectName(WINDOW)
        self.setWindowTitle("Keyframe Reducer  v%s" % __version__)
        self.setWindowFlags(QtCore.Qt.Window)
        self.setMinimumWidth(420)
        self._build_ui()

    # ── UI 構築 ──
    def _build_ui(self):
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        lay.addWidget(self._make_obj_row("Source", "_srcEdit", self._set_source))
        lay.addWidget(self._make_obj_row("Target", "_tgtEdit", self._set_target))

        # 対象アトリビュート ALL / SELECT
        mode_box = QtWidgets.QGroupBox("Target アトリビュート")
        mv = QtWidgets.QVBoxLayout(mode_box)
        row = QtWidgets.QHBoxLayout()
        self._allRadio = QtWidgets.QRadioButton("ALL")
        self._selRadio = QtWidgets.QRadioButton("SELECT")
        self._allRadio.setChecked(True)
        self._modeGroup = QtWidgets.QButtonGroup(self)
        self._modeGroup.addButton(self._allRadio)
        self._modeGroup.addButton(self._selRadio)
        row.addWidget(self._allRadio)
        row.addWidget(self._selRadio)
        row.addStretch(1)
        mv.addLayout(row)

        # SELECT 用チェック（位置/回転/スケール）
        self._attrWidget = QtWidgets.QWidget()
        aw = QtWidgets.QHBoxLayout(self._attrWidget)
        aw.setContentsMargins(0, 0, 0, 0)
        self._checks = {}
        for label, key, _ch in _TYPES:
            cb = QtWidgets.QCheckBox(label)
            cb.setChecked(True)
            self._checks[key] = cb
            aw.addWidget(cb)
        aw.addStretch(1)
        mv.addWidget(self._attrWidget)
        lay.addWidget(mode_box)

        self._allRadio.toggled.connect(self._update_attr_enabled)
        self._update_attr_enabled()

        # Apply
        self._applyBtn = QtWidgets.QPushButton("Apply")
        self._applyBtn.setMinimumHeight(30)
        self._applyBtn.clicked.connect(self._apply)
        lay.addWidget(self._applyBtn)

        # ── ロケータ経由フルベイク（親子反転）───
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken)
        lay.addWidget(line)
        self._bakeLocBtn = QtWidgets.QPushButton("ロケータ経由でフルベイク（親子反転）")
        self._bakeLocBtn.setMinimumHeight(28)
        self._bakeLocBtn.setToolTip(
            "選択物の位置にロケータ作成 → 選択物を親にペアレントコンストレイン → "
            "ロケータをフルベイク → 親子反転 → オブジェクトをフルベイク（複数可・Undo一括）")
        self._bakeLocBtn.clicked.connect(self._bake_locator)
        lay.addWidget(self._bakeLocBtn)

        self._status = QtWidgets.QLabel("")
        self._status.setStyleSheet("color: #999;")
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

    def _make_obj_row(self, label, edit_attr, set_slot):
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        lab = QtWidgets.QLabel(label)
        lab.setFixedWidth(52)
        h.addWidget(lab)
        btn = QtWidgets.QPushButton("Set")
        btn.setFixedWidth(52)
        btn.setToolTip("ビューポートで選択中のオブジェクトをセット（複数可）")
        btn.clicked.connect(set_slot)
        h.addWidget(btn)
        edit = QtWidgets.QLineEdit()
        edit.setPlaceholderText("オブジェクト名（Set で自動入力・手入力/複数可）")
        setattr(self, edit_attr, edit)
        h.addWidget(edit, 1)
        return w

    def _update_attr_enabled(self):
        self._attrWidget.setEnabled(self._selRadio.isChecked())

    # ── 操作 ──
    def _set_source(self):
        self._set_from_selection(self._srcEdit)

    def _set_target(self):
        self._set_from_selection(self._tgtEdit)

    def _set_from_selection(self, edit):
        sel = cmds.ls(selection=True) or []
        if not sel:
            self._status.setText("▲ ビューポートで対象を選択してから Set を押してください。")
            return
        edit.setText(", ".join(sel))
        self._status.setText("セット: %d 個" % len(sel))

    @staticmethod
    def _parse(edit):
        raw = edit.text().replace(",", " ").split()
        return [x for x in (s.strip() for s in raw) if x]

    def _apply(self):
        sources = self._parse(self._srcEdit)
        targets = self._parse(self._tgtEdit)
        if not sources:
            self._warn("Source が未設定です。")
            return
        if not targets:
            self._warn("Target が未設定です。")
            return
        missing = [n for n in sources + targets if not cmds.objExists(n)]
        if missing:
            self._warn("存在しないオブジェクト:\n%s" % ", ".join(missing))
            return

        if self._selRadio.isChecked():
            mode = "select"
            types = [k for k, cb in self._checks.items() if cb.isChecked()]
            if not types:
                self._warn("SELECT では 位置/回転/スケール を1つ以上選んでください。")
                return
        else:
            mode = "all"
            types = []

        try:
            res = reduce_keys(sources, targets, mode, types)
        except Exception as e:
            self._warn(str(e))
            return

        msg = ("完了: %d キー削除（対象アトリビュート %d）"
               % (res["deleted"], res["plugs"]))
        if res["skipped"]:
            msg += " ／ Source と一致せずスキップ %d" % res["skipped"]
        self._status.setText("✓ " + msg)

    def _bake_locator(self):
        sel = cmds.ls(selection=True, long=True) or []
        objs = [x for x in sel
                if "transform" in (cmds.nodeType(x, inherited=True) or [])]
        if not objs:
            self._warn("ビューポートでオブジェクト（トランスフォーム）を選択してください。")
            return
        try:
            locs = bake_via_locator(objs)
        except Exception as e:
            self._warn(str(e))
            return
        try:
            cmds.select(objs, replace=True)
        except Exception:
            pass
        self._status.setText(
            "✓ ロケータ経由フルベイク完了: %d 個（ロケータ %d 作成）"
            % (len(objs), len(locs)))

    def _warn(self, text):
        self._status.setText("▲ " + text)
        QtWidgets.QMessageBox.warning(self, "Keyframe Reducer", text)


def show():
    """ツールウィンドウを開く（既存があれば閉じてから）。"""
    parent = _maya_main_window()
    for w in (QtWidgets.QApplication.topLevelWidgets() if QtWidgets.QApplication.instance() else []):
        try:
            if w.objectName() == WINDOW:
                w.close()
                w.deleteLater()
        except Exception:
            pass
    win = KeyframeReducer(parent=parent)
    win.show()
    win.raise_()
    win.activateWindow()
    return win


# このファイルの内容をそのまま Maya の Script Editor に貼り付けて実行すると起動する。
# （Maya の実行方法によっては __name__ が "__main__" にならないため、無条件で起動する）
# ※ import して使いたい場合はこの行をコメントアウトしてください。
show()
