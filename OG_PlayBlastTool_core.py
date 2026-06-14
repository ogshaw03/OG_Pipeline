# -*- coding: utf-8 -*-
"""
OG_PlayBlastTool - コアロジック (Maya 2023+)

Render Setup レンダーレイヤー作成・プレイブラスト実行・テンプレート入出力・
出力先解決などの機能本体を提供する。UI は OG_PlayBlastTool_UI（PySide 版）が
本モジュールを import して使用する。本モジュールは UI を持たない。
"""

import maya.cmds as cmds
import maya.mel as mel
import os
import sys
import json
import subprocess

try:
    import maya.app.renderSetup.model.renderSetup as renderSetup
    import maya.app.renderSetup.model.renderLayer as renderLayerModel
    import maya.app.renderSetup.model.collection as collectionModel
    _RS_API_AVAILABLE = True
except ImportError:
    _RS_API_AVAILABLE = False


# ---------------------------------------------------------------
# Core Logic
# ---------------------------------------------------------------

def _get_or_create_layer(rs, layer_name):
    for rl in rs.getRenderLayers():
        if rl.name() == layer_name:
            return rl, False
    return rs.createRenderLayer(layer_name), True


def _get_or_create_collection(rl_obj, col_name):
    for c in rl_obj.getCollections():
        if c.name() == col_name:
            return c, False
    return rl_obj.createCollection(col_name), True


def _add_objects_to_collection(col_obj, obj_list):
    """コレクションにオブジェクトを追加する"""
    if not obj_list:
        return
    # パイプ階層のみ除去し、ネームスペースは保持する
    # 例: |group|ns:pSphere1 -> ns:pSphere1
    short_names = [obj.split("|")[-1] for obj in obj_list]
    try:
        selector = col_obj.getSelector()
        if hasattr(selector, "setPattern"):
            existing = selector.getPattern() if hasattr(selector, "getPattern") else ""
            existing_items = [x.strip() for x in existing.split(",") if x.strip()]
            for name in short_names:
                if name not in existing_items:
                    existing_items.append(name)
            selector.setPattern(", ".join(existing_items))
            print("[PlayblastTool] setPattern で追加: {}".format(short_names))
            return
        if hasattr(selector, "setCustomPatternString"):
            existing = selector.getCustomPatternString() if hasattr(
                selector, "getCustomPatternString") else ""
            existing_items = [x.strip() for x in existing.split() if x.strip()]
            for name in short_names:
                if name not in existing_items:
                    existing_items.append(name)
            selector.setCustomPatternString(" ".join(existing_items))
            print("[PlayblastTool] setCustomPatternString で追加: {}".format(short_names))
            return
        sets_nodes = cmds.ls(col_obj.name(), type="objectSet") or []
        if sets_nodes:
            cmds.sets(obj_list, addElement=sets_nodes[0])
            print("[PlayblastTool] cmds.sets で追加: {}".format(short_names))
            return
        prev_sel = cmds.ls(selection=True, long=True) or []
        try:
            cmds.select(obj_list, replace=True)
            mel.eval('renderSetup -sel addSelected "{}";'.format(col_obj.name()))
            print("[PlayblastTool] MEL で追加: {}".format(short_names))
        finally:
            cmds.select(prev_sel, replace=True) if prev_sel else cmds.select(clear=True)
    except Exception as e:
        cmds.warning("[PlayblastTool] コレクションへのオブジェクト追加エラー: {}".format(e))


def create_render_setup_layers(layer_defs, layer_objects_map=None):
    """
    Render Setup レンダーレイヤーとコレクションを作成する。

    Parameters
    ----------
    layer_defs       : list[dict]  {"name": str}
    layer_objects_map: dict or None  {layer_name: [obj, ...]}
                       None の場合はすべて空のコレクション
    """
    if not _RS_API_AVAILABLE:
        cmds.warning("Render Setup API が利用できません。")
        return []

    rs      = renderSetup.instance()
    results = []

    for defn in layer_defs:
        layer_name = defn["name"]
        result = {"layer": layer_name, "collection": "",
                  "status": "", "message": "", "added_objects": []}
        try:
            rl_obj, layer_created = _get_or_create_layer(rs, layer_name)
            result["status"]  = "created" if layer_created else "exists"
            result["message"] = "作成" if layer_created else "既存"

            col_name = layer_name + "_col"
            col_obj, col_created = _get_or_create_collection(rl_obj, col_name)
            result["collection"] = col_obj.name()
            result["message"] += " / コレクション{}".format("追加" if col_created else "既存")

            obj_list = (layer_objects_map or {}).get(layer_name, [])
            if obj_list:
                _add_objects_to_collection(col_obj, obj_list)
                result["added_objects"] = list(obj_list)
                result["message"] += " / {}個のオブジェクト追加".format(len(obj_list))
            else:
                result["message"] += " / オブジェクトなし"

        except Exception as e:
            result["status"]  = "error"
            result["message"] = str(e)
            cmds.warning("[PlayblastTool] エラー ({}): {}".format(layer_name, e))

        results.append(result)
        print("[PlayblastTool] {} -> {}".format(layer_name, result["message"]))

    return results


# ---------------------------------------------------------------
# Templates / Output Format / Output Resolution
# ---------------------------------------------------------------

# 表示名 -> (playblast format, compression)
# format="image" は連番画像、それ以外は動画ファイル
PLAYBLAST_FORMATS = [
    ("PNG",             ("image", "png")),
    ("JPEG",            ("image", "jpg")),
    ("TARGA",           ("image", "tga")),
    ("TIFF",            ("image", "tif")),
    ("Maya IFF",        ("image", "iff")),
    ("BMP",             ("image", "bmp")),
    ("QuickTime (H.264)", ("qt",  "H.264")),
    ("AVI",             ("avi",   "")),
]

# 内部に持つ初期設定の名前。外部 JSON ファイルではなくコード内に保持し、
# テンプレートプルダウンに常に先頭表示される。
DEFAULT_TEMPLATE_NAME = "Default"


def default_template():
    """新しいデフォルトテンプレート（dict）を返す。"""
    return {
        "name":             "Default",
        "format":           "image",
        "compression":      "png",
        "show_ornaments":   False,
        "disable_panzoom":  True,
        "camera_mode":      "active",   # active / persp / render
        "frame_padding":    4,
        "scale_percent":    100,
        # 出力先: シーンファイルのフォルダから scene_parent_level 階層遡り、
        #         そこへ subfolders を多階層で連結する
        "output_spec": {
            "scene_parent_level": 0,
            "subfolders":         [],
        },
    }


def merge_template(data):
    """読み込んだ dict をデフォルトにマージして欠損キーを補う。"""
    tpl = default_template()
    if isinstance(data, dict):
        for k, v in data.items():
            if k == "output_spec" and isinstance(v, dict):
                spec = tpl["output_spec"]
                spec.update({sk: sv for sk, sv in v.items()})
                tpl["output_spec"] = spec
            else:
                tpl[k] = v
    # 型を正規化
    spec = tpl["output_spec"]
    try:
        spec["scene_parent_level"] = max(0, int(spec.get("scene_parent_level", 0)))
    except Exception:
        spec["scene_parent_level"] = 0
    subs = spec.get("subfolders", [])
    spec["subfolders"] = [str(s).strip() for s in subs if str(s).strip()]
    return tpl


# --- format ヘルパー -------------------------------------------------

def format_labels():
    return [f[0] for f in PLAYBLAST_FORMATS]


def format_from_label(label):
    for lbl, (fmt, comp) in PLAYBLAST_FORMATS:
        if lbl == label:
            return fmt, comp
    return "image", "png"


def label_from_format(fmt, comp):
    for lbl, (f, c) in PLAYBLAST_FORMATS:
        if f == fmt and c == comp:
            return lbl
    return "PNG"


# --- テンプレートファイル入出力 -------------------------------------

def get_templates_dir():
    """テンプレート JSON を保存するディレクトリ（プロジェクト非依存）。"""
    base = None
    try:
        base = cmds.internalVar(userAppDir=True)
    except Exception:
        base = None
    if not base:
        base = os.path.expanduser("~")
    path = os.path.join(base, "playblast_templates")
    if not os.path.isdir(path):
        try:
            os.makedirs(path)
        except Exception as e:
            cmds.warning("[PlayblastTool] テンプレートフォルダ作成エラー: {}".format(e))
    return path


def list_template_names():
    d = get_templates_dir()
    if not os.path.isdir(d):
        return []
    # "_" 始まりは内部用ファイル（_config.json など）として除外する
    names = [os.path.splitext(f)[0] for f in os.listdir(d)
             if f.lower().endswith(".json") and not f.startswith("_")]
    return sorted(names, key=lambda s: s.lower())


def template_display_names():
    """プルダウン表示用の一覧。常に先頭に内部デフォルトを含める。"""
    names = [n for n in list_template_names() if n != DEFAULT_TEMPLATE_NAME]
    return [DEFAULT_TEMPLATE_NAME] + names


def is_default_template(name):
    return name == DEFAULT_TEMPLATE_NAME


def load_template_or_default(name):
    """名前からテンプレートを取得する。'Default' は外部ファイルではなく
    内部の初期設定 (default_template) を返す。"""
    if is_default_template(name):
        return default_template()
    return load_template(name)


def template_path(name):
    return os.path.join(get_templates_dir(), "{}.json".format(name))


def load_template(name):
    """名前からテンプレートを読み込む。失敗時はデフォルトを返す。"""
    p = template_path(name)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return merge_template(data)
    except Exception as e:
        cmds.warning("[PlayblastTool] テンプレート読込エラー ({}): {}".format(name, e))
        return default_template()


def save_template(data):
    """テンプレートを JSON として保存する。data['name'] をファイル名に使う。"""
    tpl = merge_template(data)
    name = tpl.get("name", "").strip()
    if not name:
        raise ValueError("テンプレート名が空です。")
    if is_default_template(name):
        raise ValueError("'{}' は内部初期設定の予約名です。別の名前を付けてください。".format(name))
    p = template_path(name)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(tpl, fh, ensure_ascii=False, indent=2)
    print("[PlayblastTool] テンプレート保存: {}".format(p))
    return p


def delete_template(name):
    p = template_path(name)
    if os.path.isfile(p):
        os.remove(p)
        print("[PlayblastTool] テンプレート削除: {}".format(p))
        return True
    return False


# --- 起動時テンプレート（テンプレートフォルダ内 _config.json に永続保存） -------
# Maya の環境設定 (optionVar) ではなく通常ファイルに保存する。バージョン非依存・
# prefs リセット耐性・設定直後の即時書き込み（クラッシュ耐性）が得られる。

def _config_path():
    return os.path.join(get_templates_dir(), "_config.json")


def _read_config():
    try:
        with open(_config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_config(data):
    try:
        with open(_config_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception as e:
        cmds.warning("[PlayblastTool] 設定保存エラー: {}".format(e))


def get_startup_template():
    """次回起動時に自動適用するテンプレート名を返す。未設定なら None。"""
    name = _read_config().get("startup_template")
    return name or None


def set_startup_template(name):
    cfg = _read_config()
    cfg["startup_template"] = name
    _write_config(cfg)


def clear_startup_template():
    cfg = _read_config()
    if "startup_template" in cfg:
        cfg.pop("startup_template", None)
        _write_config(cfg)


def resolve_output_base(template, custom_folder=None):
    """
    テンプレートの output_spec とシーンファイルパスから出力先ベースを解決する。

    custom_folder が指定されている場合はそれを優先する。
    解決できない場合（シーン未保存など）は None。
    """
    if custom_folder:
        return custom_folder

    scene_folder = get_scene_folder()
    if not scene_folder:
        return None

    spec  = (template or {}).get("output_spec", {})
    level = int(spec.get("scene_parent_level", 0) or 0)

    base = scene_folder
    for _ in range(level):
        parent = os.path.dirname(base)
        if parent == base:
            break
        base = parent

    for sub in spec.get("subfolders", []):
        sub = str(sub).strip()
        if sub:
            base = os.path.join(base, sub)

    return base


# ---------------------------------------------------------------
# Playblast
# ---------------------------------------------------------------

def get_render_layers():
    layers = cmds.ls(type="renderLayer")
    return [l for l in layers if not cmds.referenceQuery(l, isNodeReferenced=True)]


def strip_rs_prefix(name):
    return name[3:] if name.startswith("rs_") else name


def get_scene_folder():
    scene_path = cmds.file(query=True, sceneName=True)
    return os.path.dirname(scene_path) if scene_path else None


def _play_notification_sound():
    """
    プレイブラスト完了時に通知音を鳴らす。
    Windows: winsound.MessageBeep
    macOS  : afplay でシステムサウンドを再生
    Linux  : print (端末ベル)
    """
    try:
        if sys.platform.startswith("win"):
            import winsound
            winsound.MessageBeep(winsound.MB_OK)
        elif sys.platform == "darwin":
            subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
        else:
            sys.stdout.write("\a")
            sys.stdout.flush()
    except Exception as e:
        cmds.warning("[PlayblastTool] 通知音エラー: {}".format(e))


def open_in_explorer(folder_path):
    target = folder_path
    while target and not os.path.isdir(target):
        parent = os.path.dirname(target)
        if parent == target:
            target = None
            break
        target = parent
    if not target:
        cmds.warning("開けるフォルダが見つかりませんでした: {}".format(folder_path))
        return
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", os.path.normpath(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except Exception as e:
        cmds.warning("フォルダを開けませんでした: {}".format(str(e)))


def _get_render_camera():
    """
    レンダリング設定で指定されているカメラ形状ノードを返す。
    取得できない場合は None。
    """
    try:
        # renderable フラグが立っているカメラを探す
        all_cams = cmds.ls(type="camera")
        renderable = [c for c in all_cams if cmds.getAttr(c + ".renderable")]
        return renderable[0] if renderable else None
    except Exception:
        return None


def _get_persp_camera():
    """パースカメラ (perspShape) を返す。存在しない場合は None。"""
    persp = cmds.ls("perspShape", type="camera")
    if persp:
        return persp[0]
    try:
        shapes = cmds.listRelatives("persp", shapes=True, type="camera") or []
        return shapes[0] if shapes else None
    except Exception:
        return None




def _get_active_camera():
    """
    現在アクティブなビューパネルのカメラ形状ノードを返す。
    フォーカス取得が失敗した場合は全モデルパネルの先頭カメラを返す。
    """
    # フォーカス中パネルから取得
    try:
        panel = cmds.getPanel(withFocus=True)
        if panel and cmds.getPanel(typeOf=panel) == "modelPanel":
            cam_transform = cmds.modelEditor(panel, query=True, camera=True)
            shapes = cmds.listRelatives(cam_transform, shapes=True, type="camera") or []
            if shapes:
                return shapes[0]
    except Exception:
        pass
    # 全モデルパネルから先頭を取得
    try:
        model_panels = cmds.getPanel(type="modelPanel") or []
        for p in model_panels:
            cam_transform = cmds.modelEditor(p, query=True, camera=True)
            shapes = cmds.listRelatives(cam_transform, shapes=True, type="camera") or []
            if shapes:
                return shapes[0]
    except Exception:
        pass
    persp = cmds.ls("perspShape", type="camera")
    return persp[0] if persp else None


# アクティブカメラ選択時にビューのカメラをキャッシュする（UI から設定される）。
_cached_active_camera = None


def _camera_from_mode(mode):
    """カメラモード文字列 (active / persp / render) からカメラ形状ノードを返す。
    UI に依存しないため PySide 版からも利用できる。"""
    if mode == "persp":
        return _get_persp_camera()
    if mode == "render":
        cam = _get_render_camera()
        if cam:
            return cam
        cmds.warning("[PlayblastTool] レンダリング設定カメラが見つかりません。パースカメラを使用します。")
        return _get_persp_camera()
    # active / その他
    cam = _cached_active_camera or _get_active_camera()
    return cam or _get_persp_camera()


def backup_output_folder(base_folder, target_layer_names=None):
    """
    base_folder 直下のレイヤーフォルダを
    base_folder/old/YYYYMMDD[_NNN]/ にコピーする。

    target_layer_names が指定されている場合は、その名前と一致する
    フォルダのみをバックアップする（関係ないフォルダを除外）。

    - old フォルダが存在しない場合は作成する
    - 今日の日付フォルダが既にある場合は _001, _002 ... とナンバリングする
    - base_folder が存在しない場合は何もしない

    Returns
    -------
    str  バックアップ先フォルダのパス（何もコピーしなかった場合は None）
    """
    import shutil
    import datetime

    if not base_folder or not os.path.isdir(base_folder):
        return None

    # old フォルダ
    old_root = os.path.join(base_folder, "old")

    # 今日の日付文字列
    today = datetime.date.today().strftime("%Y%m%d")

    # ナンバリング: YYYYMMDD が既存なら YYYYMMDD_001, _002 ...
    dest_name = today
    dest_path = os.path.join(old_root, dest_name)
    if os.path.exists(dest_path):
        idx = 1
        while True:
            dest_name = "{}_{}".format(today, str(idx).zfill(3))
            dest_path = os.path.join(old_root, dest_name)
            if not os.path.exists(dest_path):
                break
            idx += 1

    # base_folder 直下のサブフォルダを収集
    # target_layer_names が指定されている場合はその名前のフォルダのみ対象
    subdirs = []
    for entry in os.listdir(base_folder):
        if entry == "old":
            continue
        full = os.path.join(base_folder, entry)
        if not os.path.isdir(full):
            continue
        if target_layer_names is not None:
            if entry not in target_layer_names:
                continue
        subdirs.append(full)

    if not subdirs:
        return None

    # コピー実行
    os.makedirs(dest_path, exist_ok=True)
    for src_dir in subdirs:
        dst = os.path.join(dest_path, os.path.basename(src_dir))
        shutil.copytree(src_dir, dst)
        print("[PlayblastTool] バックアップ: {} -> {}".format(src_dir, dst))

    return dest_path


def _disable_camera_overlays(camera, disable_panzoom=False):
    """
    指定カメラシェイプの displayResolution / displaySafeAction /
    displayGateMask / displayFilmGate / overscan を無効化し元の値を返す。
    panZoomEnabled はトランスフォームノードに対して操作する。

    Parameters
    ----------
    camera : str  カメラシェイプノード名
    """
    saved = {}
    if not camera:
        return saved

    # シェイプノードのアトリビュート
    for attr in ("displayResolution", "displaySafeAction",
                 "displayGateMask", "displayFilmGate"):
        try:
            val = cmds.getAttr("{}.{}".format(camera, attr))
            saved[attr] = val
            cmds.setAttr("{}.{}".format(camera, attr), False)
        except Exception as e:
            cmds.warning("[PlayblastTool] {}.{}: {}".format(camera, attr, e))

    # overscan を 1.0 にリセット
    try:
        val = cmds.getAttr("{}.overscan".format(camera))
        saved["overscan"] = val
        cmds.setAttr("{}.overscan".format(camera), 1.0)
    except Exception as e:
        cmds.warning("[PlayblastTool] {}.overscan: {}".format(camera, e))

    # panZoomEnabled はトランスフォームノードのアトリビュート
    if disable_panzoom:
        parents = cmds.listRelatives(camera, parent=True) or []
        if parents:
            try:
                val = cmds.getAttr("{}.panZoomEnabled".format(parents[0]))
                saved["__panZoom_transform__"] = parents[0]
                saved["panZoomEnabled"] = val
                cmds.setAttr("{}.panZoomEnabled".format(parents[0]), False)
            except Exception as e:
                cmds.warning("[PlayblastTool] panZoomEnabled {}: {}".format(parents[0], e))

    print("[PlayblastTool] オーバーレイ無効化: {}".format(camera))
    return saved


def _restore_camera_overlays(camera, saved):
    """_disable_camera_overlays で保存した値を元に戻す。"""
    if not camera or not saved:
        return

    transform = saved.get("__panZoom_transform__")
    for attr, val in saved.items():
        if attr == "__panZoom_transform__":
            continue
        if attr == "panZoomEnabled" and transform:
            try:
                cmds.setAttr("{}.panZoomEnabled".format(transform), val)
            except Exception as e:
                cmds.warning("[PlayblastTool] panZoomEnabled 復元エラー: {}".format(e))
        else:
            try:
                cmds.setAttr("{}.{}".format(camera, attr), val)
            except Exception as e:
                cmds.warning("[PlayblastTool] {}.{} 復元エラー: {}".format(camera, attr, e))

    print("[PlayblastTool] オーバーレイ復元完了: {}".format(camera))


def run_playblast(layer_names, base_folder=None, template=None):
    if template is None:
        template = default_template()

    output_base = resolve_output_base(template, custom_folder=base_folder)
    if not output_base:
        cmds.confirmDialog(title="エラー",
                           message="出力先フォルダが設定されていません。\n"
                                   "シーンが未保存の場合は出力先を直接指定してください。",
                           button=["OK"], defaultButton="OK")
        return

    # テンプレートから書き出し設定を取得
    pb_format   = template.get("format", "image")
    pb_comp     = template.get("compression", "png")
    show_orn    = bool(template.get("show_ornaments", False))
    frame_pad   = int(template.get("frame_padding", 4) or 4)
    scale_pct   = int(template.get("scale_percent", 100) or 100)

    current_layer = cmds.editRenderLayerGlobals(query=True, currentRenderLayer=True)

    # プレイブラスト前にカメラのオーバーレイを無効化（設定はテンプレートに従う）
    disable_panzoom = bool(template.get("disable_panzoom", True))

    # アクティブカメラ選択かどうかを判定（テンプレートのカメラモードを使用）
    camera_mode = template.get("camera_mode", "active")
    use_active  = (camera_mode == "active")

    if use_active:
        # アクティブカメラモード: カメラ指定なしでアクティブビューをそのまま使う
        # オーバーレイ無効化対象はアクティブビューのカメラ
        camera = _get_active_camera()
        cam_saved = _disable_camera_overlays(camera, disable_panzoom=disable_panzoom) if camera else {}
        prev_cam = None
        panel    = None
    else:
        # レンダリング設定 / パースカメラ: 指定カメラにビューを切り替える
        camera = _camera_from_mode(camera_mode)
        cam_saved = _disable_camera_overlays(camera, disable_panzoom=disable_panzoom) if camera else {}
        prev_cam = None
        panel    = None
        try:
            panel = cmds.getPanel(withFocus=True)
            if panel and cmds.getPanel(typeOf=panel) == "modelPanel" and camera:
                cam_transform  = (cmds.listRelatives(camera, parent=True) or [camera])[0]
                prev_cam_shape = cmds.modelEditor(panel, query=True, camera=True)
                prev_cam = (cmds.listRelatives(prev_cam_shape, parent=True) or [prev_cam_shape])[0]
                if prev_cam != cam_transform:
                    cmds.lookThru(panel, cam_transform)
                else:
                    prev_cam = None   # 変更不要
        except Exception as e:
            cmds.warning("[PlayblastTool] カメラ切り替えエラー: {}".format(e))
            prev_cam = None

    errors = []
    try:
        for layer in layer_names:
            try:
                cmds.editRenderLayerGlobals(currentRenderLayer=layer)
                folder_name   = strip_rs_prefix(layer)
                output_folder = os.path.join(output_base, folder_name)
                if not os.path.exists(output_folder):
                    os.makedirs(output_folder)
                output_path = os.path.join(output_folder, folder_name)
                width  = cmds.getAttr("defaultResolution.width")
                height = cmds.getAttr("defaultResolution.height")
                is_movie = (pb_format != "image")
                pb_kwargs = dict(
                    format=pb_format,
                    filename=output_path,
                    widthHeight=[width, height],
                    percent=scale_pct,
                    forceOverwrite=True,
                    viewer=False,
                    showOrnaments=show_orn,
                )
                if is_movie:
                    # 動画は1ファイル出力。framePadding は無関係、品質を指定
                    pb_kwargs["quality"] = 100
                else:
                    pb_kwargs["framePadding"] = frame_pad
                if pb_comp:
                    pb_kwargs["compression"] = pb_comp
                cmds.playblast(**pb_kwargs)
                print("[PlayblastTool] 完了 (cam={}, fmt={}/{}): {} -> {}".format(
                    camera, pb_format, pb_comp or "-", layer, output_folder))
            except Exception as e:
                msg = "レイヤー '{}' でエラー: {}".format(layer, str(e))
                if pb_format != "image":
                    msg += "（{} 形式はこの環境/Mayaバージョンで利用できない可能性があります）".format(pb_format)
                cmds.warning(msg)
                errors.append(msg)
    finally:
        # エラーが発生しても必ずカメラ・設定・レンダーレイヤーを元に戻す
        if camera and cam_saved:
            _restore_camera_overlays(camera, cam_saved)
        if prev_cam:
            try:
                cmds.lookThru(panel, prev_cam)
            except Exception:
                pass
        cmds.editRenderLayerGlobals(currentRenderLayer=current_layer)

    _play_notification_sound()
    if errors:
        cmds.confirmDialog(title="プレイブラスト完了（一部エラーあり）",
                           message="\n".join(errors), button=["OK"], defaultButton="OK")
    else:
        cmds.confirmDialog(title="プレイブラスト完了",
                           message="{} 個のレイヤーが完了しました。\n出力先: {}".format(
                               len(layer_names), output_base),
                           button=["OK"], defaultButton="OK")

