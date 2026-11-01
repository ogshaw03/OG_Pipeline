# OG_Pipeline（パイプラインツール本体）引継ぎメモ

別セッション（ローカル）で **OG_Pipeline** の開発を続けるための引継ぎ資料。
本体は `OG_Pipeline.py` 1ファイル（約 7,400 行）。巨大なため本書はコード全文ではなく
**クラス／関数マップ・データ構造・設計決定・注意点**をまとめる。コード実体は repo を
`git pull` して参照（本書とズレたらコードが正）。

- リポジトリ: `ogshaw03/OG_Pipeline`（branch `main` / 開発 `claude/dazzling-mendel-eg3zpk`）
- 本体: リポジトリ直下 **`OG_Pipeline.py`**（現行 `__version__ = "1.0.2"`）
- 対象: **Maya**（PySide2 / PySide6 両対応、import フォールバックあり）。
  一部機能（ショットリスト）は **Maya 非依存**で、スタンドアロン `OG_ShotList.py` が再利用。

> **最優先ルールは `CLAUDE.md`（リポジトリ直下）**。本書と矛盾したら `CLAUDE.md` が優先。

---

## 0. リポジトリ構成（同梱物）

| ファイル | 役割 | install.py 配布対象 |
|---|---|---|
| **`OG_Pipeline.py`** | 本体。メインUI＋ショットリスト（`AllShotsDialog`）＋`open_shot_list()` を内包 | ✅（SHA固定で取得） |
| `OG_ShotList.py` | Maya 非依存のスタンドアロン起動用（`AllShotsDialog` を import 再利用） | ❌（制作進行に別配布／exe化） |
| `install.py` | エンドユーザーが Maya にドラッグ → GitHub から本体取得＋シェルフ登録＋ホット更新 | 自身が配布物 |
| `OG_PL_icon.png` | シェルフアイコン（**ユーザー所有の実体 9144 bytes**。勝手に上書き禁止） | ✅ |
| `CLAUDE.md` | 開発ルール（ショットリスト同期／install.py／バージョン上げ） | — |
| `docs/` | 手順書・命名・引継ぎ（本書を含む） | — |
| `KeyframeReducer.py` | **別ツール**（キー間引き）。パイプラインとは無関係 | ❌ |
| `OG_PlayBlastTool_UI.py` / `OG_PlayBlastTool_core.py` | **姉妹ツール**（Render Setup レイヤー別プレイブラスト）。独立 | ❌ |
| `OG_StageTracker.py` | **姉妹ツール**（ショット工程トラッカー・Deadline連携）。独立 | ❌ |

> パイプラインの開発対象は基本 `OG_Pipeline.py` / `OG_ShotList.py` / `install.py` /
> `OG_PL_icon.png` と `docs/`。姉妹ツール3種はこのメモの範囲外（別途管理）。

---

## 1. 起動フロー

- **メイン起動（Maya 内）**: `OG_Pipeline.main()` → `OGPipelineWindow`。
- **ショットリスト単独（Maya 内）**: `OG_Pipeline.open_shot_list()` → `AllShotsDialog(standalone=False)`。
- **完全スタンドアロン（Maya 外）**: `python OG_ShotList.py` → `AllShotsDialog(standalone=True)`。
- **配布／自動更新**: `install.py` を Maya ビューポートにドラッグ →
  GitHub から `OG_Pipeline.py` を **SHA 固定 URL** で取得して `scripts` に上書き →
  シェルフボタン作成（左クリック＝メイン／右クリック＝メニュー: メイン・ショットリスト・更新）。
  更新は本体UIの「⟳ 更新」またはシェルフ右クリック →「GitHub から更新」
  （`update_from_github` → `_run_github_update` → `_reopen_after_update`、`evalDeferred` 3段）。

**リリース手順（必須）**:
1. `OG_Pipeline.py` の `__version__` を上げる（install.py の before→after 表示・ヘッダーに出る）。
2. `_GH_OWNER` / `_GH_REPO` / `_GH_BRANCH` は **install.py と OG_Pipeline.py で同一値**に保つ。
3. `python -m py_compile OG_Pipeline.py OG_ShotList.py` で最低限の構文確認。
4. `main`（または配布ブランチ）へ push。SHA 固定取得なので push 後に反映。

---

## 2. 設定・データ構造（保存先）

Maya のバージョンに依存しない通常ファイル。`get_config_dir()`:
Maya 内は `<userAppDir>/og_pipeline/`、Maya 外はホーム配下。

- **`roots.json`** … 登録プロジェクト（ルート）一覧。
- **`_config.json`** … 起動時に自動適用するルート名・各種設定。

**ルート（プロジェクト）エントリの主なキー**（`_normalize_entries` / `_normalize_stages` が正規化）:

```
{
  "name":          "プロジェクト表示名",
  "path":          "ルートパス",
  "shots_parent":  "ショットフォルダの親（この直下＝各ショット）",
  "stage_subpath": "ショット内で工程コンテナへ潜るサブパス（任意）",
  "subpath_label": "サブパスの表示名（任意）",
  "stages": [
    {
      "name":        "工程名（＝基準トークン）",
      "rename_from": "リネーム元トークン（明示時は厳密一致・大文字小文字も）",
      "rename_to":   "リネーム先トークン（空なら name を使用）",
      "take":        "テイク初期値（数字入りのときだけ初期化）",
      "local":       "ローカル版初期値（同上）",
      "order":       工程の並び順,
      ...
    }, ...
  ]
}
```

- 設定の入出力: `load_roots/save_roots/add_root/remove_root/find_root_entry/
  import_roots_file/export_roots_file`、起動ルート `get/set/clear_startup_root`。
- その他設定アクセサ: `get/set_export_method`, `get/set_auto_export_on_save`,
  `get/set_auto_export_interval_min`, `get/set_grid_cols`, `get/set_list_rows`,
  `get/set_manual_export_method`, `get/set_pending_cv2_uninstall`。

---

## 3. 命名規則・工程リネーム（重要ロジック）

「ベースネーム方式」は**廃止済み**。現在は**現在開いているシーン名を基準にトークン置換**する方式。

- `apply_stage_rename(stem, stage, all_stages) -> (new, replaced)`
  - `rename_from` が**明示**されていれば、それを唯一の基準に**厳密一致**で置換（大文字小文字も厳密）。
    見つからなければ **置換しない**（`replaced=False`）。呼び出し側 `_save_to_stage` は
    置換できなかったとき **警告して中断**（取り違え保存の防止）。
  - `rename_from` 未設定時のみ、他工程のトークン（`rename_to`/`name`）を**自動検出**して
    最長一致で1回だけ置換。
  - テイク/ローカルは欄に**数字が入っている工程のときだけ**その値で初期化（空欄は据え置き）。
- バージョントークン: `make_token_re` / `_parse_version_prefix` / `version_token_re` /
  `_normalize_token_str` / `bump_version_token` / `set_version_token` / `_clamp_digits`。
  採番接頭辞は各工程のテイク/ローカル欄から導出（`_t##` 固定ではない汎用）。
- 工程関連: `shot_stage_list` / `shot_stage_scene_list` / `resolve_stage_dir` /
  `stage_container` / `expand_stage_bases(_named)` / `_stage_rank` / `stage_has_scene` /
  `stage_latest_scene_mtime` / `stage_color`。

---

## 4. 保存系フロー（`OGPipelineWindow`）

| メソッド | 挙動 |
|---|---|
| `_save_scene_as` | **Maya 純正の「Save Scene As」画面**を開く（`mel.eval("SaveSceneAs")`） |
| `_save_new_scene` + `_prompt_save_name` | **現在開いているシーン**を、命名規則の名前を**初期値に入れたテキストフィールド**で保存（ユーザー変更可）。※旧「空シーンが保存される地雷仕様」は廃止 |
| `_save_to_stage` / `_save_renamed` | 対象工程へリネーム保存。`apply_stage_rename` が置換できないと警告・中断 |
| `_version_up_save` + `_next_version_path` | **VERSION UP**: 末尾番号をインクリメント（工程設定とは独立に動く） |
| `_take_up_save` | テイクを1つ上げて保存 |
| `_playblast_current` / `_playblast` | プレイブラスト出力（`VIDEO_SUBDIR = "Pipeline_Movie"` へ） |
| `_on_scene_saved` / `_register_save_job` / `_kill_save_job` | シーン保存監視（自動エクスポート連携） |

---

## 5. クラス・マップ（`OG_Pipeline.py`）

| クラス | 役割 |
|---|---|
| `Cv2VideoThread` | OpenCV で mp4 をデコード再生するスレッド |
| `FrameDecodeThread` | ビューア/グリッド用のフレーム先読みデコードスレッド |
| `ScanThread` | シーンファイル検索スレッド |
| `VideoPlayer` | 内蔵プレイヤー（mp4/連番を QLabel 描画）。`resume()` は先に `_stop_all()` してから再開（二重スレッド暴走の修正済み） |
| `IdleReleaseMonitor` | アイドル時にファイルロック解放。**QTimer で `QCursor.pos()` をポーリング**（旧: グローバル app イベントフィルタ→起動クラッシュのため置換済み） |
| `GridVideoCell` | グリッドの動画セル（メモリ再生／`_resume_mem` で現フレームから再開） |
| `ClickableRow` | リスト行 |
| `SafeComboBox` | マルチモニタでポップアップが別画面に出る問題を修正（同一スクリーンのコンボ直下へ再配置） |
| `BookmarkSlider` | ビューアのタイムスライダ（ブックマークを縦線表示） |
| `VideoViewerDialog` | ダブルクリックで開く**元解像度ビューア**。Space=再生/停止, ←→=フレーム, ↑↓=ブックマーク送り, F=ブックマークトグル |
| `AllShotsDialog` | **ショットリスト本体（共有コード・Maya 非依存）**。`standalone` 引数でスタンドアロン対応。グリッド3〜7列、工程バッジ、絞り込み、並び替え、⏸再生停止 |
| `DetailPanel` | 右サイドバー（工程別最新動画）。`_stage_for_media` でバッジに工程設定を反映 |
| `ColumnBrowser` | Finder ライクな Miller カラムのフォルダブラウザ |
| `ReferenceEditDialog` | .ma リファレンス編集 |
| `ProjectSettingsDialog` | プロジェクト設定（工程の追加/順序↑↓/削除、プロジェクト削除、設定JSONエクスポート） |
| `SettingsDialog` | 全体設定（エクスポート方式・自動エクスポート・グリッド列数など） |
| `OGPipelineWindow` | **メインウィンドウ**（ヘッダー/ルートバー/ツールバー/ファイルパネル/ムービーバー/詳細パネル） |

---

## 6. 動画・フレームキャッシュ（パフォーマンス）

- 共有メモリフレームキャッシュ＋LRU: `_frame_key` / `_img_bytes` / `_frame_cache_get` /
  `_frame_cache_put` / `_FRAME_CACHE`。
- デコード並列数の上限: `_DECODE_MAX_ACTIVE = 3`（`_pump_decode_queue` / `request_video_frames` /
  `_on_decode_done` / `_drop_decode_thread` / `stop_all_decodes`）。
- 連番は **`Pipeline_Movie` 配下のみ**走査（`find_latest_sequence_under` を限定）。
- グリッドの可視セル更新は **90ms デバウンス**（`_schedule_update_visible`）＝スクロールのカクつき対策。
- OpenCV は任意依存（`_try_import_cv2` / `install_opencv` / `uninstall_opencv` /
  `_cleanup_cv2_leftovers` / `_cv2_startup`）。無い場合は連番/外部再生にフォールバック。
- `maya_scene_fps(default=24.0)`: Maya 外では 24 を返す（**Maya 非依存の肝**）。

---

## 7. 設計決定・注意点（必読の地雷メモ）

1. **ショットリスト＝共有コード**。`AllShotsDialog` と付随（`GridVideoCell`/`VideoViewerDialog`/
   `BookmarkSlider`/`IdleReleaseMonitor`/フレームキャッシュ/`pick_folder_media` 等）は
   **Maya 非依存を保つ**。`maya.cmds` はモジュール先頭で import せず関数内で遅延 import＋フォールバック。
   親ウィンドウ前提のコードは `hasattr(parent, "...")` でガード。変更後は必ず
   `python -m py_compile OG_Pipeline.py OG_ShotList.py`、可能なら `python OG_ShotList.py` で確認。
2. **`OG_PL_icon.png` はユーザー所有の実体（9144 bytes, main 上）。勝手に生成・上書きしない。**
   アイコンを触るときは、まず main の既存ファイルがあるか確認して**それを使う**。
3. **GitHub 取得は SHA 固定 URL**（CDN キャッシュ回避）。ブランチ名取得だと古いものを掴むことがある。
4. **リリース時は必ず `__version__` を上げる**（§1）。GH 定数3つは install.py と同期。
5. **工程リネームは置換できなければ中断・警告**（誤保存防止）。`rename_from` は大文字小文字厳密。
6. マルチモニタ時のコンボは `SafeComboBox` を使う（素の QComboBox だとポップアップが別画面に出る）。
7. `IdleReleaseMonitor` はアプリ全体のイベントフィルタを**使わない**（起動クラッシュの原因だった）。

---

## 8. 未対応・次の候補（オープン項目）

1. 配布経路: 本体は public repo の raw 取得前提（install.py）。private のままだと raw が 404 になる。
2. スタンドアロン ショットリストの exe 配布（PyInstaller、`--hidden-import OG_Pipeline --paths .`）。
   手順は `docs/OG_ShotList_スタンドアロン手順.md`。
3. 工程設定が無いプロジェクトでも VERSION UP は末尾番号で動く（工程非依存）。要件変化時は要確認。
4. 姉妹ツール（PlayBlastTool / StageTracker）との連携・統合は未定。

---

## 9. 確認/テスト手順

- 構文: `python -m py_compile OG_Pipeline.py OG_ShotList.py`
- スタンドアロン: `python OG_ShotList.py`（PySide2/6 と任意で opencv-python）。
- Maya: `OG_Pipeline.main()` でメイン、`OG_Pipeline.open_shot_list()` でショットリスト単独。
- 保存系: SAVE AS が Maya 純正画面／SAVE NEW SCENE が現シーンを命名規則初期値で保存／
  工程保存が置換不能時に中断警告／VERSION UP が末尾採番、を確認。

---

## 10. 参考ドキュメント

- `CLAUDE.md` … 開発ルール（最優先）
- `docs/UI_NAMING.md` … UI 各部の呼称
- `docs/OG_ShotList_スタンドアロン手順.md` … 制作進行向けの導入/起動/exe化手順
- `README.md` … ツール集の概要（OG_Pipeline / OG_StageTracker）

> まとめ: 本体は `OG_Pipeline.py` 1ファイルに集約。ショットリストは共有コードで Maya 非依存を保ち、
> スタンドアロンへ自動反映。配布は install.py の SHA 固定取得＋シェルフ＋ホット更新。
> リリースは `__version__` を上げて push、GH 定数は install.py と同期、アイコンは既存実体を尊重。
