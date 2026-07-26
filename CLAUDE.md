# OG_Pipeline 開発メモ（重要ルール）

## スタンドアロン ショットリストとの同期（必須）

`OG_ShotList.py` は Maya を持たない人（制作進行など）向けの独立アプリで、
本体 `OG_Pipeline.py` の **`AllShotsDialog`（および付随クラス `GridVideoCell` /
`VideoViewerDialog` / `BookmarkSlider` / `IdleReleaseMonitor` / 動画フレーム
キャッシュ・`pick_folder_media` 等）をそのまま import して再利用**している。

したがって **本体のショットリスト（`AllShotsDialog` まわり）に変更を加えたら、
スタンドアロンにも自動で反映される** ——ただし次を必ず守ること:

1. **`AllShotsDialog` と付随クラス・関数は Maya 非依存を保つ。**
   - `maya.cmds` はモジュール先頭で import しない（関数内で遅延 import し、失敗時は
     フォールバックする）。例: `maya_scene_fps()` は Maya 外で `24` を返す。
   - 親ウィンドウ（メインウィンドウ）が居る前提のコードを書かない。必要なら
     `hasattr(parent, "...")` でガードし、スタンドアロン時の代替動作を用意する
     （例: `_drill_to` はブラウザが無ければ `open_file_external` で開く）。
2. **ショットリストに機能追加/変更をしたら、`OG_ShotList.py` でも壊れないか確認する。**
   - 最低限: `python -m py_compile OG_Pipeline.py OG_ShotList.py`
   - できれば PySide 環境で `python OG_ShotList.py` を起動して確認。
3. スタンドアロン固有の起動フロー（プロジェクト選択／フォルダ直接指定／設定JSON
   取り込み）は `OG_ShotList.py` 側にあるので、必要に応じてそちらも更新する。
4. 手順マニュアルは `docs/OG_ShotList_スタンドアロン手順.md`。UI や操作が変わったら更新する。

> まとめ: ショットリスト＝共有コード。Maya 依存を持ち込まない限り、変更は
> スタンドアロンへ自動反映される。Maya 依存を足すときは必ずフォールバックを用意する。

## 配布・自動更新（install.py / GitHub ホットアップデート）

- 配布物はリポジトリ直下の **`install.py`**（エンドユーザーが Maya にドラッグ）。
  install.py は GitHub から **`OG_Pipeline.py` 1ファイル**を SHA 固定 URL で取得して
  Maya のユーザースクリプトに上書きし、シェルフボタン（左＝メイン起動／右クリック＝
  メイン・ショットリスト・更新）を追加する。更新はメインUIの「⟳ 更新」または
  シェルフ右クリック→「GitHub から更新」（`update_from_github` → `_run_github_update`
  → `_reopen_after_update`、evalDeferred 3段）。
- **リリース時は `OG_Pipeline.py` の `__version__` を必ず上げる**（install.py が
  before→after 表示に使う。ヘッダー/タイトルにも出る）。
- `_GH_OWNER` / `_GH_REPO` / `_GH_BRANCH` は **install.py と OG_Pipeline.py で同一値**に保つ。
- Maya 用の実体は `OG_Pipeline.py` 単一ファイル（main＋AllShotsDialog＋open_shot_list を
  内包）。`OG_ShotList.py` は Maya 非依存の独立起動用で install.py の取得対象外。

## 参考
- UI 各部の呼称: `docs/UI_NAMING.md`
- メイン起動: `OG_Pipeline.main()` ／ ショットリスト単独（Maya内）: `OG_Pipeline.open_shot_list()`
  ／ 完全スタンドアロン（Maya外）: `OG_ShotList.py`
