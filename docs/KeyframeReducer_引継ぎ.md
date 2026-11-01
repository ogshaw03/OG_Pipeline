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
