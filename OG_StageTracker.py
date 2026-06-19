"""
OG_StageTracker — ショット工程トラッカー（Google Sheets 連携）

OG_Pipeline とは独立したツール。命名規則からショットと工程を判定し、
ショット×工程の表として一覧・管理する。Google Sheets と同期できる。

命名規則（既定）:
    sh###d00_lay_pri  → 工程 lay_pri
    sh###d00_lay_anm  → 工程 lay_anm
    sh###d00_anm_sec  → 工程 anm_sec
  すなわち  sh<番号>d<番号>_<工程>[_<バージョン>]  の形。
  ショットIDは sh### と shFS###（例 sh010d00 / shFS010d00）の両形式に対応。
  ※ 同じショット×工程に複数ある場合や打ち間違いデータがある場合は、
    更新日時が最新のファイルを参照する。
  工程の定義は設定ファイルで追加・変更できる。

設定ファイル（OG_Pipeline と同じ og_pipeline フォルダ内）:
    stage_tracker.json … シート ID / 認証 JSON パス / 工程定義 / 最後に使ったルート

Google Sheets 連携には gspread と google-auth が必要:
    pip install gspread google-auth
サービスアカウントの JSON 鍵を発行し、対象シートをそのアカウントに共有しておく。

Maya 内でも Maya 外（通常の Python）でも起動できる。Sheets 連携は外部実行が手軽。
"""

import os
import re
import sys
import json
import subprocess
from pathlib import Path

try:
    from PySide2.QtCore import Qt, QThread, Signal
    from PySide2.QtGui import QColor, QFont
    from PySide2.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
        QComboBox, QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
        QFileDialog, QLineEdit, QDialog, QFormLayout, QDialogButtonBox, QAbstractItemView
    )
except ImportError:
    from PySide6.QtCore import Qt, QThread, Signal
    from PySide6.QtGui import QColor, QFont
    from PySide6.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
        QComboBox, QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
        QFileDialog, QLineEdit, QDialog, QFormLayout, QDialogButtonBox, QAbstractItemView
    )

MAYA_EXTENSIONS = {".ma", ".mb"}

# 工程の既定定義（コード, 表示名）。順序がパイプライン進行順。
# ※ anm_pri はファイル名の打ち間違いデータのため、正規工程には含めない。
#   万一ファイルが存在しても「現工程」は更新日時が最新の工程で判定するので参照されない。
DEFAULT_STAGES = [
    {"code": "lay_pri", "label": "Layout / Primary"},
    {"code": "lay_anm", "label": "Layout / Anim"},
    {"code": "anm_sec", "label": "Anim / Secondary"},
]

# sh<digits>d<digits>_<stage>[_<version>] を名前のどこからでも拾う。
# ショットIDは sh### と shFS###（例 sh010d00 / shFS010d00）の両形式に対応。
# 先頭にプレフィックスが付く（例: EP01_sh010d00_lay_pri）場合も match させるため search で使う。
# stage は lay_pri / lay_anm / anm_sec のような「英字_英字」トークン。
SHOT_RE = re.compile(
    r"(sh(?:fs)?\d+d\d+)_([a-z]{2,6}_[a-z]{2,6})(?:_v?(\d+))?", re.IGNORECASE
)


# ═══════════════════════════════════════════════════════════════════════════════
#  設定（JSON）
# ═══════════════════════════════════════════════════════════════════════════════
def get_config_dir():
    """OG_Pipeline と同じ設定フォルダを使う（無ければホーム配下に作成）。"""
    try:
        import OG_Pipeline
        return OG_Pipeline.get_config_dir()
    except Exception:
        base = os.path.expanduser("~")
        d = os.path.join(base, "og_pipeline")
        if not os.path.isdir(d):
            try:
                os.makedirs(d)
            except Exception:
                pass
        return d


def _config_path():
    return os.path.join(get_config_dir(), "stage_tracker.json")


def load_settings():
    try:
        with open(_config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data):
    try:
        with open(_config_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[OG_StageTracker] 設定保存エラー:", e)


def get_stages():
    """工程定義を返す（設定で上書き可能、無ければ既定）。"""
    stages = load_settings().get("stages")
    if isinstance(stages, list) and stages:
        out = []
        for s in stages:
            if isinstance(s, dict) and s.get("code"):
                out.append({"code": str(s["code"]).lower(),
                            "label": str(s.get("label", s["code"]))})
        if out:
            return out
    return list(DEFAULT_STAGES)


def load_project_roots():
    """OG_Pipeline に登録済みのルート一覧を再利用する。"""
    try:
        import OG_Pipeline
        return OG_Pipeline.load_roots()
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════════════
#  命名パース／集計（UI 非依存・単体テスト可能）
# ═══════════════════════════════════════════════════════════════════════════════
def parse_scene_name(filename):
    """ファイル名から {'shot','stage','version'} を返す。該当しなければ None。"""
    stem = Path(filename).stem
    m = SHOT_RE.search(stem)
    if not m:
        return None
    return {
        "shot": m.group(1).lower(),
        "stage": m.group(2).lower(),
        "version": int(m.group(3)) if m.group(3) else 0,
    }


def scan_shots(root, stage_order):
    """ルート配下を走査し、ショット×工程の最新版テーブルを構築する。

    戻り値:
        shots: {shot: {stage: {'version','path','mtime'}}}
        stages_seen: 出現した工程コードの順序付きリスト（既知→未知）
        stats: {'total','matched','samples'} 診断用
    """
    shots = {}
    seen = set()
    total = 0
    matched = 0
    samples = []           # 命名規則に一致しなかったファイル名の例
    root = Path(root)
    if not root.exists():
        return shots, list(stage_order), {"total": 0, "matched": 0, "samples": []}
    for path in root.rglob("*"):
        if path.suffix.lower() not in MAYA_EXTENSIONS:
            continue
        total += 1
        info = parse_scene_name(path.name)
        if not info:
            if len(samples) < 5:
                samples.append(path.name)
            continue
        matched += 1
        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = 0.0
        seen.add(info["stage"])
        entry = shots.setdefault(info["shot"], {})
        cur = entry.get(info["stage"])
        # 同じショット×工程に複数ある場合は「最新の更新日時」のものを採用する
        if cur is None or mtime >= cur["mtime"]:
            entry[info["stage"]] = {
                "version": info["version"],
                "path": str(path),
                "mtime": mtime,
            }
    # 既知の工程順 → そのあとに未知の工程
    ordered = [s for s in stage_order if s in seen]
    ordered += sorted(s for s in seen if s not in stage_order)
    stats = {"total": total, "matched": matched, "samples": samples}
    return shots, ordered, stats


def current_stage(shot_stages, stage_order=None):
    """そのショットの「現工程」を返す。無ければ None。

    更新日時が最新のファイルを持つ工程を現工程とする。
    （打ち間違いデータ等、古いファイルの工程を現工程にしないため、
      パイプライン順ではなく更新日時を優先する）
    更新日時が同じ場合はパイプライン順で後ろの工程を優先する。
    """
    order = stage_order or []
    best, best_m = None, None
    for code, cell in shot_stages.items():
        m = cell.get("mtime", 0.0) if isinstance(cell, dict) else 0.0
        if best is None or m > best_m:
            best, best_m = code, m
        elif m == best_m and order:
            bi = order.index(best) if best in order else -1
            ci = order.index(code) if code in order else -1
            if ci > bi:
                best = code
    return best


def build_table_rows(shots, stage_order):
    """Google Sheets / 表示用の行データを作る。

    header: ["Shot", <各工程>, "Current", "Render"]
    各工程セルは最新バージョン文字列（無ければ ""）。
    """
    header = ["Shot"] + list(stage_order) + ["Current", "Render"]
    rows = []
    for shot in sorted(shots.keys()):
        st = shots[shot]
        row = [shot]
        for s in stage_order:
            row.append(f"v{st[s]['version']:03d}" if s in st else "")
        row.append(current_stage(st, stage_order) or "")
        row.append("")  # Render 状態（Sheets/Deadline 側で埋める想定）
        rows.append(row)
    return header, rows


# ═══════════════════════════════════════════════════════════════════════════════
#  Google Sheets 連携（gspread）
# ═══════════════════════════════════════════════════════════════════════════════
class SheetSync:
    def __init__(self, sheet_id, worksheet, cred_path):
        self.sheet_id = sheet_id
        self.worksheet = worksheet or "Sheet1"
        self.cred_path = cred_path

    def _open(self):
        try:
            import gspread
        except ImportError:
            raise RuntimeError(
                "gspread が見つかりません。`pip install gspread google-auth` を実行してください。"
            )
        if not self.sheet_id:
            raise RuntimeError("シート ID が未設定です。")
        if not self.cred_path or not os.path.isfile(self.cred_path):
            raise RuntimeError("サービスアカウント JSON のパスが正しくありません。")
        try:
            from google.oauth2.service_account import Credentials
            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            creds = Credentials.from_service_account_file(self.cred_path, scopes=scopes)
            gc = gspread.authorize(creds)
        except Exception:
            gc = gspread.service_account(filename=self.cred_path)
        return gc.open_by_key(self.sheet_id)

    def push(self, header, rows):
        """ヘッダー＋行を worksheet に書き出す（全置換）。戻り値: 書き込み行数。"""
        sh = self._open()
        try:
            ws = sh.worksheet(self.worksheet)
        except Exception:
            ws = sh.add_worksheet(title=self.worksheet,
                                  rows=max(100, len(rows) + 10),
                                  cols=max(20, len(header) + 2))
        ws.clear()
        data = [header] + rows
        try:
            ws.update(range_name="A1", values=data)   # 新しめの gspread
        except TypeError:
            ws.update("A1", data)                     # 旧 gspread 互換
        return len(rows)

    def pull_records(self):
        """worksheet を辞書のリストで取得（Render 列の取り込みなどに使用）。"""
        sh = self._open()
        ws = sh.worksheet(self.worksheet)
        return ws.get_all_records()


# ═══════════════════════════════════════════════════════════════════════════════
#  Deadline 連携（Web Service REST / deadlinecommand CLI）
# ═══════════════════════════════════════════════════════════════════════════════
# 表示用の状態優先度（上ほど優先して表示＝「今レンダ中」を前面に出す）
RENDER_PRIORITY = ["Rendering", "Queued", "Pending", "Failed", "Suspended", "Completed", "Unknown"]

# Deadline の Stat 整数 → ラベル（環境差があるためチャンク数も併用して判定する）
_STAT_LABELS = {1: "Queued", 2: "Suspended", 3: "Completed", 4: "Failed",
                5: "Pending", 6: "Pending"}


def _shot_of_jobname(name):
    """ジョブ名から sh###d00 / shFS###d00 を抽出してショット ID にする。無ければ None。"""
    m = re.search(r"sh(?:fs)?\d+d\d+", str(name), re.IGNORECASE)
    return m.group(0).lower() if m else None


def _job_status_from_rest(job):
    """REST のジョブ辞書から {name, status, progress} を作る（チャンク数優先）。"""
    props = job.get("Props", {}) if isinstance(job, dict) else {}
    name = props.get("Name") or job.get("Name") or job.get("JobName") or ""

    def ci(key):
        try:
            return int(job.get(key, 0) or 0)
        except Exception:
            return 0

    rc, cc, qc = ci("RenderingChunks"), ci("CompletedChunks"), ci("QueuedChunks")
    fc, pc, sc = ci("FailedChunks"), ci("PendingChunks"), ci("SuspendedChunks")
    total = rc + cc + qc + fc + pc + sc
    progress = (cc / total * 100.0) if total else 0.0

    if rc > 0:
        status = "Rendering"
    elif total > 0 and cc == total:
        status = "Completed"
    elif fc > 0 and qc == 0 and rc == 0 and pc == 0:
        status = "Failed"
    elif qc > 0:
        status = "Queued"
    elif pc > 0:
        status = "Pending"
    elif sc > 0:
        status = "Suspended"
    else:
        status = _STAT_LABELS.get(job.get("Stat"), "Unknown")
    return {"name": name, "status": status, "progress": progress}


def _normalize_cli_status(raw):
    s = str(raw).strip().lower()
    if "render" in s:
        return "Rendering"
    if "queue" in s or "active" in s:
        return "Queued"
    if "pend" in s:
        return "Pending"
    if "fail" in s:
        return "Failed"
    if "suspend" in s:
        return "Suspended"
    if "complete" in s or "done" in s:
        return "Completed"
    return "Unknown"


class DeadlineClient:
    """Deadline からジョブ状態を取得する。mode='webservice' か 'cli'。"""

    def __init__(self, mode="webservice", host="", port="8082", cmd_path=""):
        self.mode = mode or "webservice"
        self.host = host
        self.port = str(port or "8082")
        self.cmd_path = cmd_path

    def get_jobs(self):
        """[{name, status, progress}, ...] を返す。"""
        if self.mode == "cli":
            return self._jobs_via_cli()
        return self._jobs_via_webservice()

    # --- Web Service (REST) ---
    def _jobs_via_webservice(self):
        import urllib.request
        if not self.host:
            raise RuntimeError("Deadline Web Service のホストが未設定です。")
        url = "http://{}:{}/api/jobs".format(self.host, self.port)
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            raise RuntimeError("Deadline Web Service へ接続できません: {}".format(e))
        if isinstance(data, dict):
            data = data.get("Jobs") or data.get("jobs") or []
        return [_job_status_from_rest(j) for j in data if isinstance(j, dict)]

    # --- deadlinecommand (CLI) ---
    def _resolve_cmd(self):
        if self.cmd_path and os.path.isfile(self.cmd_path):
            return self.cmd_path
        exe = "deadlinecommand.exe" if sys.platform.startswith("win") else "deadlinecommand"
        base = os.environ.get("DEADLINE_PATH", "")
        if base:
            cand = os.path.join(base, exe)
            if os.path.isfile(cand):
                return cand
        return exe  # PATH に通っている前提

    def _jobs_via_cli(self):
        cmd = self._resolve_cmd()
        try:
            out = subprocess.check_output([cmd, "-GetJobs"],
                                          stderr=subprocess.STDOUT, timeout=30)
            out = out.decode("utf-8", "replace")
        except Exception as e:
            raise RuntimeError("deadlinecommand の実行に失敗しました: {}".format(e))
        return self._parse_cli_jobs(out)

    @staticmethod
    def _parse_cli_jobs(text):
        """deadlinecommand -GetJobs 出力を防御的にパースする（環境差に強く）。"""
        jobs = []
        cur = {}

        def flush():
            if cur:
                name = cur.get("name") or cur.get("jobname") or ""
                if name:
                    jobs.append({
                        "name": name,
                        "status": _normalize_cli_status(cur.get("status")
                                                        or cur.get("jobstatus") or ""),
                        "progress": _to_float(cur.get("progress")
                                              or cur.get("taskprogress") or 0),
                    })

        for line in text.splitlines():
            if "=" not in line:
                if line.strip() == "":
                    flush()
                    cur = {}
                continue
            key, val = line.split("=", 1)
            k = key.strip().lower().replace(" ", "")
            # 新しいジョブの開始（id 行）が来たら前のジョブを確定
            if k in ("jobid", "id") and cur:
                flush()
                cur = {}
            cur[k] = val.strip()
        flush()
        return jobs


def _to_float(v):
    try:
        return float(str(v).replace("%", "").strip())
    except Exception:
        return 0.0


def deadline_status_by_shot(jobs):
    """ジョブ一覧をショット ID 単位に集約し、最も注目すべき状態を返す。

    戻り値: {shot: {'status','progress','jobs'}}
    """
    by_shot = {}
    for job in jobs:
        shot = _shot_of_jobname(job.get("name", ""))
        if not shot:
            continue
        rec = by_shot.setdefault(shot, {"status": "Unknown", "progress": 0.0, "jobs": 0})
        rec["jobs"] += 1
        # 優先度の高い（=レンダ中寄りの）状態を採用
        cur_i = RENDER_PRIORITY.index(rec["status"]) if rec["status"] in RENDER_PRIORITY else len(RENDER_PRIORITY)
        new_i = RENDER_PRIORITY.index(job["status"]) if job["status"] in RENDER_PRIORITY else len(RENDER_PRIORITY)
        if new_i < cur_i:
            rec["status"] = job["status"]
            rec["progress"] = job.get("progress", 0.0)
    return by_shot


class DeadlineWorker(QThread):
    done = Signal(object)     # {shot: {...}}
    failed = Signal(str)

    def __init__(self, client):
        super().__init__()
        self.client = client

    def run(self):
        try:
            jobs = self.client.get_jobs()
            self.done.emit(deadline_status_by_shot(jobs))
        except Exception as e:
            self.failed.emit(str(e))


# ═══════════════════════════════════════════════════════════════════════════════
#  バックグラウンド・スキャン
# ═══════════════════════════════════════════════════════════════════════════════
class ScanWorker(QThread):
    done = Signal(object, object, object)   # (shots, stage_order, stats)
    failed = Signal(str)

    def __init__(self, root, stage_order):
        super().__init__()
        self.root = root
        self.stage_order = stage_order

    def run(self):
        try:
            shots, ordered, stats = scan_shots(self.root, self.stage_order)
            self.done.emit(shots, ordered, stats)
        except Exception as e:
            self.failed.emit(str(e))


# ═══════════════════════════════════════════════════════════════════════════════
#  設定ダイアログ（Google Sheets ＋ Deadline）
# ═══════════════════════════════════════════════════════════════════════════════
class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("設定 — Google Sheets / Deadline")
        self.setMinimumWidth(520)
        form = QFormLayout(self)

        form.addRow(QLabel("■ Google Sheets"))
        self.sheetId = QLineEdit(settings.get("sheet_id", ""))
        self.sheetId.setPlaceholderText("スプレッドシート URL の /d/ と /edit の間の ID")
        form.addRow("シート ID:", self.sheetId)

        self.worksheet = QLineEdit(settings.get("worksheet", "Sheet1"))
        form.addRow("ワークシート名:", self.worksheet)

        cred_row = QHBoxLayout()
        self.cred = QLineEdit(settings.get("cred_path", ""))
        self.cred.setPlaceholderText("サービスアカウントの JSON 鍵ファイル")
        browse = QPushButton("参照…")
        browse.clicked.connect(self._browse)
        cred_row.addWidget(self.cred, 1)
        cred_row.addWidget(browse)
        form.addRow("認証 JSON:", cred_row)

        form.addRow(QLabel(""))
        form.addRow(QLabel("■ Deadline"))
        self.dlMode = QComboBox()
        self.dlMode.addItems(["webservice", "cli"])
        i = self.dlMode.findText(settings.get("deadline_mode", "webservice"))
        if i >= 0:
            self.dlMode.setCurrentIndex(i)
        form.addRow("方式:", self.dlMode)

        self.dlHost = QLineEdit(settings.get("deadline_host", ""))
        self.dlHost.setPlaceholderText("Web Service(RCS) のホスト名/IP")
        form.addRow("ホスト:", self.dlHost)

        self.dlPort = QLineEdit(str(settings.get("deadline_port", "8082")))
        form.addRow("ポート:", self.dlPort)

        cmd_row = QHBoxLayout()
        self.dlCmd = QLineEdit(settings.get("deadline_cmd", ""))
        self.dlCmd.setPlaceholderText("deadlinecommand のパス（CLI 方式・未指定なら自動探索）")
        cbrowse = QPushButton("参照…")
        cbrowse.clicked.connect(self._browse_cmd)
        cmd_row.addWidget(self.dlCmd, 1)
        cmd_row.addWidget(cbrowse)
        form.addRow("deadlinecommand:", cmd_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse(self):
        fp, _ = QFileDialog.getOpenFileName(
            self, "サービスアカウント JSON を選択", str(Path.home()), "JSON Files (*.json)"
        )
        if fp:
            self.cred.setText(fp)

    def _browse_cmd(self):
        fp, _ = QFileDialog.getOpenFileName(self, "deadlinecommand を選択", str(Path.home()))
        if fp:
            self.dlCmd.setText(fp)

    def values(self):
        return {
            "sheet_id": self.sheetId.text().strip(),
            "worksheet": self.worksheet.text().strip() or "Sheet1",
            "cred_path": self.cred.text().strip(),
            "deadline_mode": self.dlMode.currentText(),
            "deadline_host": self.dlHost.text().strip(),
            "deadline_port": self.dlPort.text().strip() or "8082",
            "deadline_cmd": self.dlCmd.text().strip(),
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  メインウィンドウ
# ═══════════════════════════════════════════════════════════════════════════════
STYLE = """
QWidget { background:#0f1117; color:#c8ccd4; font-family:"Consolas",monospace; font-size:12px; }
QPushButton { background:#1a1f2e; color:#c8ccd4; border:1px solid #2a3045; border-radius:3px; padding:6px 14px; }
QPushButton:hover { border-color:#4a9eff; color:#4a9eff; }
QComboBox { background:#1a1f2e; border:1px solid #2a3045; border-radius:3px; padding:4px 8px; min-width:220px; }
QTableWidget { background:#0f1117; gridline-color:#1e2435; border:none; }
QHeaderView::section { background:#141824; color:#e8a838; border:none; border-right:1px solid #1e2435; padding:6px 8px; }
QTableWidget::item:selected { background:#2a2010; color:#e8a838; }
QLineEdit { background:#1a1f2e; border:1px solid #2a3045; border-radius:3px; padding:5px 8px; }
"""


class OGStageTracker(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Window)
        self.setWindowTitle("OG_StageTracker — Shot / Stage")
        self.setMinimumSize(900, 560)
        self.resize(1100, 680)

        self.settings = load_settings()
        self.stages = get_stages()
        self.stage_order = [s["code"] for s in self.stages]
        self.stage_labels = {s["code"]: s["label"] for s in self.stages}
        self._shots = {}
        self._ordered_stages = list(self.stage_order)
        self._scan = None

        self.setStyleSheet(STYLE)
        self._build_ui()
        self._reload_roots()

    # ── UI ──────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("PROJECT:"))
        self.rootCombo = QComboBox()
        bar.addWidget(self.rootCombo)

        self.scanBtn = QPushButton("↻  SCAN")
        self.scanBtn.clicked.connect(self._scan_now)
        bar.addWidget(self.scanBtn)

        bar.addStretch()

        self.pushBtn = QPushButton("⭱  PUSH → Sheets")
        self.pushBtn.clicked.connect(self._push_sheets)
        bar.addWidget(self.pushBtn)

        self.pullBtn = QPushButton("⭳  PULL Render")
        self.pullBtn.setToolTip("Google Sheets の Render 列を取り込んで表示")
        self.pullBtn.clicked.connect(self._pull_render)
        bar.addWidget(self.pullBtn)

        self.deadlineBtn = QPushButton("⟳  Deadline")
        self.deadlineBtn.setToolTip("Deadline からレンダ状態を取得して Render 列に反映")
        self.deadlineBtn.clicked.connect(self._fetch_deadline)
        bar.addWidget(self.deadlineBtn)

        self.cfgBtn = QPushButton("⚙  設定")
        self.cfgBtn.clicked.connect(self._edit_settings)
        bar.addWidget(self.cfgBtn)
        root.addLayout(bar)

        self.table = QTableWidget()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self._on_cell_double)
        root.addWidget(self.table, 1)

        self.status = QLabel("準備完了")
        self.status.setStyleSheet("color:#3a4055;")
        root.addWidget(self.status)

    def _reload_roots(self):
        self.rootCombo.clear()
        roots = load_project_roots()
        for r in roots:
            self.rootCombo.addItem(r["name"], r["path"])
        last = self.settings.get("last_root")
        if last:
            i = self.rootCombo.findText(last)
            if i >= 0:
                self.rootCombo.setCurrentIndex(i)
        if self.rootCombo.count() == 0:
            self.status.setText(
                "プロジェクトルート未登録 — まず OG_Pipeline でルートを登録してください"
            )

    def _active_root(self):
        i = self.rootCombo.currentIndex()
        return self.rootCombo.itemData(i) if i >= 0 else None

    # ── スキャン ──────────────────────────────────────────
    def _scan_now(self):
        root = self._active_root()
        if not root:
            self.status.setText("ルートが選択されていません")
            return
        self.settings["last_root"] = self.rootCombo.currentText()
        save_settings(self.settings)
        self.scanBtn.setEnabled(False)
        self.status.setText(f"スキャン中: {root}")
        self._scan = ScanWorker(root, self.stage_order)
        self._scan.done.connect(self._on_scan_done)
        self._scan.failed.connect(self._on_scan_failed)
        self._scan.start()

    def _on_scan_failed(self, msg):
        self.scanBtn.setEnabled(True)
        self.status.setText(f"⚠  スキャン失敗: {msg}")

    def _on_scan_done(self, shots, ordered, stats):
        self.scanBtn.setEnabled(True)
        self._shots = shots
        self._ordered_stages = ordered or list(self.stage_order)
        self._populate_table()

        total = stats.get("total", 0)
        matched = stats.get("matched", 0)
        if not shots:
            if total == 0:
                self.status.setText(
                    "⚠  このルート配下に Maya ファイル(.ma/.mb)が見つかりませんでした"
                )
            else:
                ex = "  /  例: " + ", ".join(stats.get("samples", [])[:3]) if stats.get("samples") else ""
                self.status.setText(
                    f"⚠  Maya {total} 件中、命名規則(sh###d00_工程)に一致 0 件。"
                    f"このプロジェクトの命名が違う可能性があります{ex}"
                )
        else:
            self.status.setText(
                f"✓  {len(shots)} ショット  |  Maya {total} 件中 {matched} 件一致  "
                f"|  工程: {', '.join(self._ordered_stages)}"
            )

    def _populate_table(self):
        cols = ["Shot"] + self._ordered_stages + ["Current", "Render"]
        self.table.clear()
        self.table.setColumnCount(len(cols))
        labels = ["Shot"] + [self.stage_labels.get(s, s) for s in self._ordered_stages] + ["Current", "Render"]
        self.table.setHorizontalHeaderLabels(labels)
        self.table.setRowCount(len(self._shots))

        for r, shot in enumerate(sorted(self._shots.keys())):
            st = self._shots[shot]
            cur = current_stage(st, self._ordered_stages)

            it = QTableWidgetItem(shot)
            it.setForeground(QColor("#e8c87a"))
            self.table.setItem(r, 0, it)

            for c, s in enumerate(self._ordered_stages, start=1):
                cell = st.get(s)
                if cell:
                    item = QTableWidgetItem(f"v{cell['version']:03d}")
                    item.setForeground(QColor("#3dcfb8"))
                    # 採用した「最新更新日時」のファイルのパスと日時をツールチップに
                    import datetime
                    try:
                        mstr = datetime.datetime.fromtimestamp(cell["mtime"]).strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        mstr = "-"
                    item.setToolTip(f"{cell['path']}\n更新: {mstr}（最新更新日時を採用）")
                    item.setData(Qt.UserRole, cell["path"])
                    if s == cur:
                        f = QFont(); f.setBold(True); item.setFont(f)
                        item.setForeground(QColor("#e8a838"))
                else:
                    item = QTableWidgetItem("")
                self.table.setItem(r, c, item)

            cur_item = QTableWidgetItem(cur or "")
            cur_item.setForeground(QColor("#e8a838"))
            self.table.setItem(r, len(self._ordered_stages) + 1, cur_item)

            self.table.setItem(r, len(self._ordered_stages) + 2, QTableWidgetItem(""))

        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)

    def _on_cell_double(self, row, col):
        item = self.table.item(row, col)
        path = item.data(Qt.UserRole) if item else None
        if not path:
            return
        try:
            import OG_Pipeline
            OG_Pipeline.reveal_in_explorer(path)
        except Exception:
            pass

    # ── Render 列ヘルパー ────────────────────────────────
    def _render_col(self):
        return len(self._ordered_stages) + 2

    @staticmethod
    def _render_color(text):
        low = str(text).lower()
        if "rend" in low:
            return QColor("#e8a838")      # レンダ中
        if "queue" in low or "active" in low or "pend" in low:
            return QColor("#4a9eff")      # 待機/キュー
        if "fail" in low or "error" in low:
            return QColor("#ff6b6b")      # 失敗
        if "done" in low or "complete" in low:
            return QColor("#3dcfb8")      # 完了
        if "suspend" in low:
            return QColor("#7a8190")      # 中断
        return QColor("#9aa3b0")

    def _set_render_cell(self, row, text):
        item = QTableWidgetItem(text)
        if text:
            item.setForeground(self._render_color(text))
        self.table.setItem(row, self._render_col(), item)

    def _shot_at_row(self, row):
        it = self.table.item(row, 0)
        return it.text().lower() if it else None

    # ── 設定 ─────────────────────────────────────────────
    def _edit_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec():
            self.settings.update(dlg.values())
            save_settings(self.settings)
            self.status.setText("✓  設定を保存しました")

    # ── Google Sheets ────────────────────────────────────
    def _sync(self):
        return SheetSync(
            self.settings.get("sheet_id", ""),
            self.settings.get("worksheet", "Sheet1"),
            self.settings.get("cred_path", ""),
        )

    def _push_sheets(self):
        if not self._shots:
            self.status.setText("先に SCAN を実行してください")
            return
        header, rows = build_table_rows(self._shots, self._ordered_stages)
        try:
            n = self._sync().push(header, rows)
        except Exception as e:
            QMessageBox.warning(self, "Sheets へ書き出し失敗", str(e))
            return
        self.status.setText(f"✓  Google Sheets に {n} 行を書き出しました")

    def _pull_render(self):
        """シートの Render 列（Shot をキー）を取り込んで表に反映する。"""
        try:
            records = self._sync().pull_records()
        except Exception as e:
            QMessageBox.warning(self, "Sheets 取り込み失敗", str(e))
            return
        render_by_shot = {}
        for rec in records:
            shot = str(rec.get("Shot", "")).lower()
            if shot:
                render_by_shot[shot] = str(rec.get("Render", ""))
        for r in range(self.table.rowCount()):
            shot = self._shot_at_row(r)
            if shot is None:
                continue
            self._set_render_cell(r, render_by_shot.get(shot, ""))
        self.status.setText("✓  Render 状態を取り込みました")

    # ── Deadline ─────────────────────────────────────────
    def _fetch_deadline(self):
        """Deadline からレンダ状態を取得して Render 列へ反映する。"""
        if self.table.rowCount() == 0:
            self.status.setText("先に SCAN を実行してください")
            return
        mode = self.settings.get("deadline_mode", "webservice")
        if mode == "webservice" and not self.settings.get("deadline_host"):
            QMessageBox.information(
                self, "Deadline 設定",
                "Deadline の接続先が未設定です。[⚙ 設定] で方式とホスト等を設定してください。",
            )
            return
        client = DeadlineClient(
            mode=mode,
            host=self.settings.get("deadline_host", ""),
            port=self.settings.get("deadline_port", "8082"),
            cmd_path=self.settings.get("deadline_cmd", ""),
        )
        self.deadlineBtn.setEnabled(False)
        self.status.setText("Deadline からレンダ状態を取得中…")
        self._dl = DeadlineWorker(client)
        self._dl.done.connect(self._on_deadline_done)
        self._dl.failed.connect(self._on_deadline_failed)
        self._dl.start()

    def _on_deadline_failed(self, msg):
        self.deadlineBtn.setEnabled(True)
        self.status.setText(f"⚠  Deadline 取得失敗: {msg}")

    def _on_deadline_done(self, by_shot):
        self.deadlineBtn.setEnabled(True)
        rendering = 0
        for r in range(self.table.rowCount()):
            shot = self._shot_at_row(r)
            rec = by_shot.get(shot) if shot else None
            if not rec:
                self._set_render_cell(r, "")
                continue
            status = rec.get("status", "")
            prog = rec.get("progress", 0.0)
            text = status
            if status == "Rendering":
                rendering += 1
                if prog:
                    text = f"Rendering {prog:.0f}%"
            self._set_render_cell(r, text)
        self.status.setText(
            f"✓  Deadline 反映: レンダ中 {rendering} カット / {len(by_shot)} カットにジョブあり"
        )


def _get_maya_main_window():
    """Maya メインウィンドウを QWidget として取得（親に設定して GC で閉じるのを防ぐ）。"""
    try:
        import OG_Pipeline
        w = OG_Pipeline._get_maya_main_window()
        if w is not None:
            return w
    except Exception:
        pass
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


# 生成したウィンドウへの参照を保持し、ガベージコレクションによる即時クローズを防ぐ
_TRACKER_WINDOW = None


def main():
    """
    Maya 内: 既存の QApplication を使い、Maya メインウィンドウを親にして表示する。
    （親付け＋モジュール参照保持をしないと、一瞬で閉じてしまう）
    スタンドアロン: QApplication を新規作成してイベントループを回す。
    """
    global _TRACKER_WINDOW

    app = QApplication.instance()
    standalone = app is None
    if standalone:
        app = QApplication(sys.argv)

    # 多重起動を防ぐ: 既存があれば閉じてから作り直す
    if _TRACKER_WINDOW is not None:
        try:
            _TRACKER_WINDOW.close()
            _TRACKER_WINDOW.deleteLater()
        except Exception:
            pass
        _TRACKER_WINDOW = None

    win = OGStageTracker(parent=_get_maya_main_window())
    _TRACKER_WINDOW = win
    win.show()
    win.raise_()
    win.activateWindow()

    if standalone:
        sys.exit(app.exec_() if hasattr(app, "exec_") else app.exec())
    return win


if __name__ == "__main__":
    main()
