#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OG_Pipeline ショットリスト — スタンドアロン版（Maya 不要）。

制作進行など Maya を持たない人向けの独立アプリ。登録プロジェクト、または任意の
ショットフォルダの親を対象に、各ショット/工程の最新動画を一覧・確認できる。
ダブルクリックで元解像度ビューア（フレームスクラブ／ブックマーク）も使える。

実行:
    python OG_ShotList.py

依存:
    - PySide2 もしくは PySide6（必須）
    - opencv-python（任意。mp4 の埋め込み再生に使用。無い場合は連番/外部再生）

プロジェクト設定（roots.json）は、本体ツールの［プロジェクト設定］→エクスポートで
書き出した JSON を、起動時のメニューから取り込めます（フォルダを直接開くことも可能）。
"""
import os
import sys

try:
    from PySide2.QtWidgets import (QApplication, QFileDialog, QInputDialog,
                                    QMessageBox)
except ImportError:
    from PySide6.QtWidgets import (QApplication, QFileDialog, QInputDialog,
                                   QMessageBox)

# 本体モジュール（Maya 非依存の部分だけを利用する）。同じフォルダに OG_Pipeline.py を置く。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import OG_Pipeline as ogp   # noqa: E402


_OPEN_FOLDER = "\U0001F4C2  フォルダを直接開く…"
_IMPORT_CFG = "⭳  プロジェクト設定(JSON)を取り込む…"


def _choose_project():
    """開くプロジェクト設定（dict）を返す。キャンセルなら None。

    登録プロジェクトの選択 / フォルダ直接指定 / 設定 JSON の取り込み、から選ぶ。
    """
    roots = ogp.load_roots()
    while True:
        items = [r["name"] for r in roots] + [_OPEN_FOLDER, _IMPORT_CFG]
        label = "プロジェクトを選択:" if roots else "登録がありません。操作を選んでください:"
        choice, ok = QInputDialog.getItem(
            None, "OG ショットリスト", label, items, 0, False)
        if not ok:
            return None
        if choice == _OPEN_FOLDER:
            d = QFileDialog.getExistingDirectory(
                None, "ショットフォルダの親を選択（この直下がショット）")
            if not d:
                continue
            return {"name": os.path.basename(d.rstrip("/\\")) or d, "path": d,
                    "shots_parent": d, "stage_subpath": "",
                    "subpath_label": "", "stages": []}
        if choice == _IMPORT_CFG:
            fp, _ = QFileDialog.getOpenFileName(
                None, "プロジェクト設定を取り込む", "", "JSON (*.json);;すべて (*.*)")
            if fp:
                try:
                    n = ogp.import_roots_file(fp)
                    QMessageBox.information(None, "取り込み", "%d 件を取り込みました。" % n)
                    roots = ogp.load_roots()
                except Exception as e:
                    QMessageBox.warning(None, "取り込み失敗", str(e))
            continue
        return ogp.find_root_entry(choice)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    try:
        app.setApplicationName("OG ショットリスト")
    except Exception:
        pass

    entry = _choose_project()
    if entry is None:
        return

    shots_parent = entry.get("shots_parent") or entry.get("path")
    if not shots_parent or not os.path.isdir(str(shots_parent)):
        QMessageBox.warning(None, "OG ショットリスト",
                            "ショットフォルダの親が見つかりません:\n%s" % shots_parent)
        return

    dlg = ogp.AllShotsDialog(
        shots_parent, parent=None,
        stage_subpath=entry.get("stage_subpath", ""),
        stages=entry.get("stages", []),
        subpath_label=entry.get("subpath_label", ""))
    dlg.setWindowTitle("OG ショットリスト — %s" % entry.get("name", ""))
    dlg.resize(1400, 860)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()

    # 最後のウィンドウを閉じたら終了（QApplication 既定）
    sys.exit(app.exec_() if hasattr(app, "exec_") else app.exec())


if __name__ == "__main__":
    main()
