# KeyframeReducer 引継ぎメモ

別セッション（ローカル）で `KeyframeReducer.py` の開発を続けるための引継ぎ資料。
本体の OG_Pipeline とは**独立した別ツール**（ただし今は同じリポジトリに同梱）。

- リポジトリ: `ogshaw03/OG_Pipeline`（branch `main` / 開発ブランチ `claude/dazzling-mendel-eg3zpk`）
- ファイル: リポジトリ直下 **`KeyframeReducer.py`**（単一ファイル）
- 現行バージョン: **`__version__ = "0.3.0"`**
- 対象: **Maya 2024**（PySide2 / shiboken2、Python 3.10）。import は PySide6 フォールバックあり。

---

## 1. ツールの目的・全体像

アニメのキーフレーム整理ツール。2つの機能を持つ。

### A. キー間引き（Reduce）
Source の対象アトリビュートのキー時刻に合わせて、Target の対象アトリビュートのキーを
**間引く**（Source がキーを持たないフレームの Target キーを削除）。値は焼いたまま残る
＝フルベイク後からブロッキングのキータイミングへ戻す用途。

### B. ロケータ経由フルベイク（親子反転・2段階）
- **①** 選択物の位置にロケータ作成 → 選択物を親にペアレントコンストレイン →
  ロケータをフルベイク → 親子反転（ロケータが親）。**ここで停止**（オブジェクトは
  ロケータ駆動のまま、オブジェクトのベイクはしない）。
- **②** 選択オブジェクトをフルベイクし、駆動中の parentConstraint を削除。
- ①のあとにロケータ側を編集 → ②で確定、というワークフロー用に2段分割済み。

---

## 2. 起動方法

- **Script Editor に全文貼り付けて実行**（推奨）: ファイル末尾が無条件 `show()` なので
  そのまま起動する（`__name__=="__main__"` には依存しない）。
- import 利用: `import KeyframeReducer; KeyframeReducer.show()`
  （その場合は末尾の `show()` 行をコメントアウト）。

---

## 3. UI 構成（`KeyframeReducer(QtWidgets.QDialog)`）

```
Source  [Set] [__________ 入力欄]
Target  [Set] [__________ 入力欄]
┌ Target アトリビュート ─────────────┐
│ (o) ALL   ( ) SELECT               │
│ [x]位置 [x]回転 [x]スケール         │  ← SELECT のときだけ有効
└───────────────────────────────────┘
[ Apply ]                              ← 間引き実行
───────────────────────────────────
[① ロケータ作成〜親子反転（フルベイク前）]
[② オブジェクトをフルベイク]
状態ラベル
```

- **Set ボタン**: `cmds.ls(selection=True)` をカンマ区切りで入力欄へ（複数対応・手入力可）。
- ALL/SELECT は QButtonGroup。SELECT の時だけ `_attrWidget` を enable。

---

## 4. 主要関数（UI 非依存ロジックは単体テスト可能）

| 関数 | 役割 |
|---|---|
| `_animated_plugs(node, mode, types)` | 対象 `node.attr`（キーのあるもの）。ALL=`listAnimatable`でキー有、SELECT=位置/回転/スケールのチャンネルでキー有 |
| `_source_frames(sources, mode, types)` | Source 群の対象アトリビュートのキー時刻の**集合**（`round(t,4)`） |
| `reduce_keys(sources, targets, mode, types)` | Target を間引く。戻り=`{deleted, plugs, skipped}`。**区間削除**で高速化。Sourceと一致ゼロのアトリビュートは全削除回避で skip |
| `_BAKE_ATTRS` | `translateX..rotateZ`（**位置+回転のみ**。スケール未対応） |
| `_playback_range()` | タイムラインの再生 min/max |
| `_bake_transform(node,s,e)` | `bakeResults`（sampleBy=1, simulation=True, preserveOutsideKeys, disableImplicitControl, sparseAnimCurveBake=False） |
| `setup_locator_reverse(objs)` | ①。ロケータ作成→`matchTransform`(pos+rot)→`parentConstraint(obj,loc)`→loc ベイク→pc1 削除→`cutKey(obj,_BAKE_ATTRS)`→`parentConstraint(loc,obj)`。戻り=作成ロケータ名 |
| `bake_objects(objs)` | ②。obj をベイク→`listRelatives(type=parentConstraint)` を削除。戻り=`{baked, constraints}` |

UI ハンドラ: `_set_source/_set_target/_set_from_selection`, `_parse`, `_apply`,
`_selected_transforms`（selection から transform 系のみ）, `_setup_reverse`, `_bake_objects`, `_warn`。

---

## 5. 設計上の決定・前提（重要）

- **間引きのタイミング基準**: Source の対象アトリビュート**全体のキー時刻の集合（union）**を
  1つの基準にして、Target の対象アトリビュートを一律に間引く（ブロッキングのフレームに揃える）。
  → *要望があれば「アトリビュート別の厳密一致」へ変更可*。
- **SELECT の対象**: 位置/回転/スケールの3種のみ。**ALL** はキーのある全 animatable アトリビュート。
- **ロケータベイクは translate+rotate のみ**（parentConstraint 準拠）。`matchTransform` も pos+rot。
  → *スケールも含めたい場合は scaleConstraint + scale チャンネル追加*。
- **ベイク範囲 = タイムラインの再生範囲**（min/max）。
  → *アニメ範囲/指定範囲オプションは未実装*。
- **親子反転前に `cutKey`** でオブジェクトの translate/rotate を除去 → pairBlend の二重変換を回避。
- **①終了時点**: オブジェクトはロケータ駆動（live constraint）のまま。**②**で焼き＋拘束削除。
- 全操作 `undoInfo(openChunk/closeChunk)` で **Undo 一括**。

---

## 6. 未対応・次の候補（オープン項目）

1. **スケール対応**（ロケータベイク/親子反転に scale を含める）。
2. **間引きのアトリビュート別タイミング一致**オプション。
3. **ベイク範囲**の選択（アニメ範囲 / 手動指定）。
4. **配布方法**: 現状は貼り付け起動のみ。OG_Pipeline と同様の install.py ドラッグ導入＋
   シェルフ＋GitHub 更新を付けるか未定（付けるなら owner/repo/表示名/アイコンを決める）。
5. **配置**: 今は OG_Pipeline リポジトリ同梱。**別リポジトリに分離**するか未定。
6. ②の対象判定は「選択 transform の子の parentConstraint を削除」。pointConstraint/
   orientConstraint 等は対象外（①は parentConstraint のみ作成するため現状問題なし）。

---

## 7. 確認/テスト手順

- 構文: `python -m py_compile KeyframeReducer.py`
- Maya 動作:
  - 間引き: Source=疎キー / Target=フルベイク → Apply → Target が Source 時刻のみに。
  - ①→（必要ならロケータ編集）→② の順で、ロケータに動きが乗り、最後にオブジェクトへ焼き戻し＋拘束削除。
  - いずれも Undo 1回で戻る。

---

## 8. 直近コミット（参考）

- `ロケータ経由を2段階ボタンに分割`（v0.2.0→0.3.0）
- `ロケータ経由フルベイク（親子反転）ボタンを追加`（v0.1.0→0.2.0）
- `Script Editor 貼り付け実行で自動起動`（無条件 show）
- `新ツール: Keyframe Reducer（Maya2024・キーフレーム間引き）`

> 注: このツールは OG_Pipeline の `CLAUDE.md` ルール（AllShotsDialog 同期 / install.py 等）の
> 対象外。あくまで同リポジトリに同梱している独立ファイル。

---

## 9. 現行コード全文（`KeyframeReducer.py` v0.3.0）

> このメモ作成時点のスナップショット。最新は必ず `git pull` して `KeyframeReducer.py` を参照すること
> （このブロックとファイルがズレた場合はファイルが正）。

```python
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


__version__ = "0.3.0"
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


def _bake_transform(node, start, end):
    cmds.bakeResults(node, time=(start, end), attribute=_BAKE_ATTRS,
                     sampleBy=1, simulation=True,
                     preserveOutsideKeys=True,
                     disableImplicitControl=True,
                     sparseAnimCurveBake=False)


def setup_locator_reverse(objs):
    """フルベイク前までを一括実行する（ボタン①）:
      1. オブジェクトの位置にロケータ作成
      2. オブジェクトを親にペアレントコンストレイン（ロケータが追従）
      3. ロケータをフルベイク（毎フレーム）
      4. 親子反転（ロケータを親、オブジェクトを子）
    ※ ここでは オブジェクトのフルベイクはしない（ロケータが駆動する状態で終わる）。
    戻り値: 作成したロケータ名のリスト。
    """
    start, end = _playback_range()
    created = []
    cmds.undoInfo(openChunk=True)
    try:
        for obj in objs:
            short = obj.split("|")[-1].split(":")[-1]
            loc = cmds.spaceLocator(name="%s_bakeLoc" % short)[0]
            cmds.matchTransform(loc, obj, position=True, rotation=True)
            pc1 = cmds.parentConstraint(obj, loc, maintainOffset=False)[0]
            _bake_transform(loc, start, end)          # 3. ロケータをベイク
            cmds.delete(pc1)
            cmds.cutKey(obj, attribute=_BAKE_ATTRS, clear=True)
            cmds.parentConstraint(loc, obj, maintainOffset=False)  # 4. 親子反転
            created.append(loc)
    finally:
        cmds.undoInfo(closeChunk=True)
    return created


def bake_objects(objs):
    """選択オブジェクトをフルベイクし、駆動している parentConstraint を削除する（ボタン②）。
    戻り値: dict(baked=ベイクした数, constraints=削除した拘束数)。
    """
    start, end = _playback_range()
    baked = 0
    removed = 0
    cmds.undoInfo(openChunk=True)
    try:
        for obj in objs:
            _bake_transform(obj, start, end)
            baked += 1
            cons = cmds.listRelatives(obj, type="parentConstraint",
                                      fullPath=True) or []
            if cons:
                cmds.delete(cons)
                removed += len(cons)
    finally:
        cmds.undoInfo(closeChunk=True)
    return {"baked": baked, "constraints": removed}


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

        # ── ロケータ経由（2段階） ───
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken)
        lay.addWidget(line)
        self._setupBtn = QtWidgets.QPushButton("① ロケータ作成〜親子反転（フルベイク前）")
        self._setupBtn.setMinimumHeight(28)
        self._setupBtn.setToolTip(
            "選択物の位置にロケータ作成 → 選択物を親にペアレントコンストレイン → "
            "ロケータをフルベイク → 親子反転。オブジェクトのフルベイクはしない"
            "（ロケータが駆動する状態で止める・複数可・Undo一括）")
        self._setupBtn.clicked.connect(self._setup_reverse)
        lay.addWidget(self._setupBtn)

        self._bakeObjBtn = QtWidgets.QPushButton("② オブジェクトをフルベイク")
        self._bakeObjBtn.setMinimumHeight(28)
        self._bakeObjBtn.setToolTip(
            "選択オブジェクトをフルベイクし、駆動している parentConstraint を削除する"
            "（①のあと、必要ならロケータを編集してから押す・複数可・Undo一括）")
        self._bakeObjBtn.clicked.connect(self._bake_objects)
        lay.addWidget(self._bakeObjBtn)

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

    def _selected_transforms(self):
        sel = cmds.ls(selection=True, long=True) or []
        return [x for x in sel
                if "transform" in (cmds.nodeType(x, inherited=True) or [])]

    def _setup_reverse(self):
        objs = self._selected_transforms()
        if not objs:
            self._warn("ビューポートでオブジェクト（トランスフォーム）を選択してください。")
            return
        try:
            locs = setup_locator_reverse(objs)
        except Exception as e:
            self._warn(str(e))
            return
        try:
            cmds.select(objs, replace=True)
        except Exception:
            pass
        self._status.setText(
            "✓ ①完了: %d 個をロケータ駆動に（ロケータ %d 作成）。必要なら編集して②へ。"
            % (len(objs), len(locs)))

    def _bake_objects(self):
        objs = self._selected_transforms()
        if not objs:
            self._warn("ビューポートでオブジェクト（トランスフォーム）を選択してください。")
            return
        try:
            res = bake_objects(objs)
        except Exception as e:
            self._warn(str(e))
            return
        self._status.setText(
            "✓ ②完了: %d 個をフルベイク（拘束 %d 削除）"
            % (res["baked"], res["constraints"]))

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
```
