#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OG_Pipeline ショットリスト — スタンドアロン版（Maya 不要）。

制作進行など Maya を持たない人向けの独立アプリ。各ショット/工程の最新動画を
一覧・確認できる。ダブルクリックで元解像度ビューア（フレームスクラブ／ブックマーク）。

プロジェクトの設定・切替は「ショットリストを開いてから」画面上部のプロジェクトバーで
行う（プルダウンで選択／📂フォルダ直接／⭳設定JSON取込）。

実行:
    python OG_ShotList.py

依存:
    - PySide2 もしくは PySide6（必須）
    - opencv-python（任意。mp4 の埋め込み再生に使用。無い場合は連番/外部再生）

同じフォルダに OG_Pipeline.py を置くこと。
"""
import os
import sys

try:
    from PySide2.QtWidgets import QApplication
except ImportError:
    from PySide6.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import OG_Pipeline as ogp   # noqa: E402


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    try:
        app.setApplicationName("OG ショットリスト")
    except Exception:
        pass

    # 初期表示は「次回も使用」プロジェクト（無ければ単一）。無ければ空で開き、
    # 画面上部のプロジェクトバーから選択/フォルダ指定/設定取込を行う。
    roots = ogp.load_roots()
    name = ogp.get_startup_root()
    entry = ogp.find_root_entry(name) if name else None
    if entry is None and len(roots) == 1:
        entry = roots[0]

    if entry:
        dlg = ogp.AllShotsDialog(
            entry.get("shots_parent") or entry.get("path"), parent=None,
            stage_subpath=entry.get("stage_subpath", ""),
            stages=entry.get("stages", []),
            subpath_label=entry.get("subpath_label", ""),
            standalone=True)
        dlg.setWindowTitle("OG ショットリスト — %s" % entry.get("name", ""))
        try:
            dlg._reload_project_combo(select_name=entry.get("name"))
        except Exception:
            pass
    else:
        dlg = ogp.AllShotsDialog("", parent=None, standalone=True)
        dlg.setWindowTitle("OG ショットリスト")

    dlg.resize(1400, 860)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()

    # 最後のウィンドウを閉じたら終了（QApplication 既定）
    sys.exit(app.exec_() if hasattr(app, "exec_") else app.exec())


if __name__ == "__main__":
    main()
