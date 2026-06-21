# OG_Pipeline

Maya 向けのパイプラインツール集。

- **OG_Pipeline** … Maya シーンオープナー / ショットブラウザ
- **OG_StageTracker** … ショット工程トラッカー（Deadline レンダ状態連携）

---

## OG_Pipeline 起動コード

### 1. ファイルを配置
`OG_Pipeline.py` を **Maya 標準の scripts フォルダ**に保存します。
このフォルダは Maya 起動時に自動で `sys.path` に入るため、パス指定は不要です。

- Windows : `<ドキュメント>/maya/scripts/` または `/maya/<version>/scripts/`
- macOS : `~/Library/Preferences/Autodesk/maya/scripts/`

### 2. 起動（Maya スクリプトエディタ / シェルフボタン）

```python
import importlib, OG_Pipeline
importlib.reload(OG_Pipeline)
OG_Pipeline.main()
```

- 既にウィンドウが開いている場合は前面に出ます（多重起動防止）。
- Maya 既存の `QApplication` を使うため、新規に `QApplication` は作成しません。

### 3. 初回設定
- ツールバーの **`＋ 追加`** でプロジェクトルートを登録（または **`⭳ インポート`** で JSON を取り込み）。
- **`★ 次回も使用`** を押すと、そのルートが次回起動時に自動で選択されます。

### スタンドアロン起動（Maya 外での確認用）
PySide2 または PySide6 が入った環境で:

```bash
python OG_Pipeline.py
```

---

## OG_StageTracker 起動コード

`OG_StageTracker.py` を `OG_Pipeline.py` と同じフォルダに置きます。

### Maya 内 / スタンドアロン共通

```python
import importlib, OG_StageTracker
importlib.reload(OG_StageTracker)
OG_StageTracker.main()
```

### スタンドアロン（コマンド実行）

```bash
python OG_StageTracker.py
```

- 初回は **`＋ ルート追加`** でスキャンするフォルダを登録。
- **`⟳ Deadline`** でレンダ状態を取得（**`⚙ 設定`** で方式・ホスト等を設定）。

---

## 構成ファイル

| ファイル | 内容 |
|---|---|
| `OG_Pipeline.py` | シーンオープナー / ショットブラウザ |
| `OG_StageTracker.py` | 工程トラッカー（Deadline 連携） |
| `OG_PlayBlastTool_UI.py` / `OG_PlayBlastTool_core.py` | Playblast ツール |
