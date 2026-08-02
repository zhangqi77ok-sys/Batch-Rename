# -*- coding: utf-8 -*-
"""批量重命名工具 (Batch Rename Tool)

只重命名文件/文件夹的名字，不修改文件内容。
支持：选目录 / 拖拽输入、作用范围过滤、关键词替换、正则替换、预览、冲突跳过、撤销。
"""
import os
import re
import json
import traceback

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
SCOPE_ALL = "全部（文件和文件夹）"
SCOPE_FILE_ONLY = "只文件"
SCOPE_DIR_ONLY = "只文件夹"
SCOPE_SUBDIR = "子文件夹（嵌套目录）"
SCOPE_FILE_IN_DIR = "文件夹下的文件"
SCOPE_OPTIONS = [SCOPE_ALL, SCOPE_FILE_ONLY, SCOPE_DIR_ONLY, SCOPE_SUBDIR, SCOPE_FILE_IN_DIR]

import fnmatch

# 查找替换的子类型
RULE_KEYWORD = "关键词替换"
RULE_REGEX = "正则替换"

# 重命名模式
MODE_REPLACE = "查找替换"
MODE_NUMBER = "序号"
MODE_CASE = "大小写"
MODE_EDIT = "删除/插入"
MODE_EXT = "统一扩展名"
MODE_OPTIONS = [MODE_REPLACE, MODE_NUMBER, MODE_CASE, MODE_EDIT, MODE_EXT]

# 大小写模式
CASE_UPPER = "全大写"
CASE_LOWER = "全小写"
CASE_TITLE = "首字母大写"
CASE_CAPITALIZE = "每词首字母大写"
CASE_OPTIONS = [CASE_UPPER, CASE_LOWER, CASE_TITLE, CASE_CAPITALIZE]

# 序号位置
POS_SUFFIX = "原名后"
POS_PREFIX = "原名前"
POS_REPLACE = "替换原名"
POS_OPTIONS = [POS_SUFFIX, POS_PREFIX, POS_REPLACE]

# 序号样式
SEQ_NUMBER = "数字 1,2,3"
SEQ_LOWER = "小写字母 a,b,c"
SEQ_UPPER = "大写字母 A,B,C"
SEQ_OPTIONS = [SEQ_NUMBER, SEQ_LOWER, SEQ_UPPER]

CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".batch_rename_config.json")


def natural_key(name):
    """自然排序键：把数字段当整数比较，使 file2 排在 file10 前。"""
    parts = re.split(r"(\d+)", name.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def index_to_letters(n):
    """1→a, 2→b, ..., 26→z, 27→aa（Excel 列式）。n<1 返回空。"""
    if n < 1:
        return ""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("a") + r) + s
    return s

# 预览状态
ST_RENAME = "将重命名"
ST_NOCHANGE = "无变化"
ST_CONFLICT = "冲突-跳过"
ST_DUP = "重复-跳过"
ST_DONE = "✓ 已改"
ST_FAIL = "✗ 失败"

UNDO_FILE = os.path.join(os.path.expanduser("~"), ".batch_rename_undo.json")

# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------
def collect_targets(roots, scope, recursive):
    """收集待处理的路径列表。

    roots: 用户选择/拖入的顶层路径（可以是文件或目录）。
    scope: SCOPE_* 之一。
    recursive: 是否递归进入子目录。

    返回去重后的 (绝对路径, is_dir) 元组列表（尚未按执行顺序排序）。
    收集阶段一并记录类型，避免 build_plan 二次 stat。
    """
    want_files = scope in (SCOPE_ALL, SCOPE_FILE_ONLY, SCOPE_FILE_IN_DIR)
    want_dirs = scope in (SCOPE_ALL, SCOPE_DIR_ONLY, SCOPE_SUBDIR)
    # 「子文件夹」「文件夹下的文件」= 只针对嵌套项（相对根目录深度 >= 2），
    # 不含根目录的直接子项。其余 scope 含直接子项。
    inner_only = scope in (SCOPE_SUBDIR, SCOPE_FILE_IN_DIR)

    result = []
    seen = set()

    def add(path, is_dir):
        ap = os.path.abspath(path)
        if ap not in seen:
            seen.add(ap)
            result.append((ap, is_dir))

    for root in roots:
        root = os.path.abspath(root)
        if not os.path.exists(root):
            continue

        # 直接拖入的是文件：作为直接目标处理（inner_only 的 scope 不含它）
        if os.path.isfile(root):
            if want_files and not inner_only:
                add(root, False)
            continue

        # root 是目录：作为容器，其自身永不改名，只处理内部的项
        for dirpath, dirnames, filenames in os.walk(root):
            # 相对根的深度：直接子项 depth==1，嵌套项 depth>=2
            rel = os.path.relpath(dirpath, root)
            base_depth = 0 if rel == "." else rel.count(os.sep) + 1
            nested = base_depth + 1 >= 2

            if want_dirs and (not inner_only or nested):
                for d in dirnames:
                    add(os.path.join(dirpath, d), True)
            if want_files and (not inner_only or nested):
                for f in filenames:
                    add(os.path.join(dirpath, f), False)
            if not recursive:
                dirnames[:] = []
                break

    return result


def _split_stem(old_name, is_file, include_ext):
    """拆分主名/扩展名。文件默认保留扩展名；include_ext 或非文件时整名为 stem。"""
    if is_file and not include_ext:
        return os.path.splitext(old_name)
    return old_name, ""


def apply_replace(stem, rule, find, repl, case_sensitive):
    """查找替换（关键词 / 正则）。正则非法抛 re.error。"""
    if not find:
        return stem
    if rule == RULE_KEYWORD:
        if case_sensitive:
            return stem.replace(find, repl)
        pattern = re.compile(re.escape(find), re.IGNORECASE)
        return pattern.sub(lambda m: repl, stem)
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.sub(find, repl, stem, flags=flags)


def apply_number(stem, index, opts, parent_name):
    """序号模式。index 为组内序号（已含起始值+步长计算后的值）。

    opts: dict(pad, prefix, suffix, position, use_parent, sep, style)
    style: SEQ_NUMBER/SEQ_LOWER/SEQ_UPPER。字母样式忽略补零。
    use_parent 时用 parent_name 作为序号前缀（如父文件夹 '1' → '1-001'）。
    """
    style = opts.get("style", SEQ_NUMBER)
    if style == SEQ_LOWER:
        num = index_to_letters(index)
    elif style == SEQ_UPPER:
        num = index_to_letters(index).upper()
    else:
        num = str(index).zfill(opts.get("pad", 0))
    seq = num
    if opts.get("use_parent") and parent_name:
        seq = f"{parent_name}{opts.get('sep', '-')}{num}"
    prefix = opts.get("prefix", "")
    suffix = opts.get("suffix", "")
    core = f"{prefix}{seq}{suffix}"
    pos = opts.get("position", POS_SUFFIX)
    if pos == POS_REPLACE:
        return core
    if pos == POS_PREFIX:
        return core + stem
    return stem + core  # POS_SUFFIX


def apply_case(stem, case_mode):
    if case_mode == CASE_UPPER:
        return stem.upper()
    if case_mode == CASE_LOWER:
        return stem.lower()
    if case_mode == CASE_TITLE:
        return stem[:1].upper() + stem[1:] if stem else stem
    if case_mode == CASE_CAPITALIZE:
        return stem.title()
    return stem


def apply_edit(stem, del_from, del_to, ins_pos, ins_text):
    """删除第 del_from~del_to 个字符（1-based，含端点），再在 ins_pos 处插入 ins_text。

    del_from/del_to 为 0 或 None 表示不删除；越界自动裁剪。ins_pos 为负或 None 表示不插入。
    """
    s = stem
    if del_from and del_to and del_from >= 1 and del_to >= del_from:
        i = del_from - 1
        j = min(del_to, len(s))
        if i < len(s):
            s = s[:i] + s[j:]
    if ins_text and ins_pos is not None and ins_pos >= 0:
        p = min(ins_pos, len(s))
        s = s[:p] + ins_text + s[p:]
    return s


def apply_ext(old_name, target_ext):
    """统一扩展名：把整名的扩展名换成 target_ext（含或不含前导点均可）。"""
    if not target_ext:
        return old_name
    ext = target_ext if target_ext.startswith(".") else "." + target_ext
    stem, _ = os.path.splitext(old_name)
    return stem + ext


def compute_new_name(old_name, is_file, mode, params, index=0, parent_name=""):
    """按 mode 分派计算新 basename。params 为该模式所需参数字典。"""
    include_ext = params.get("include_ext", False)

    if mode == MODE_EXT:
        # 统一扩展名作用于整名，不拆 stem
        return apply_ext(old_name, params.get("target_ext", ""))

    stem, ext = _split_stem(old_name, is_file, include_ext)

    if mode == MODE_REPLACE:
        new_stem = apply_replace(stem, params.get("rule", RULE_KEYWORD),
                                 params.get("find", ""), params.get("repl", ""),
                                 params.get("case_sensitive", False))
    elif mode == MODE_NUMBER:
        new_stem = apply_number(stem, index, params, parent_name)
    elif mode == MODE_CASE:
        new_stem = apply_case(stem, params.get("case_mode", CASE_LOWER))
    elif mode == MODE_EDIT:
        new_stem = apply_edit(stem, params.get("del_from", 0), params.get("del_to", 0),
                              params.get("ins_pos", None), params.get("ins_text", ""))
    else:
        new_stem = stem

    return new_stem + ext


def build_plan(targets, mode, params, filter_glob=""):
    """生成预览计划。

    targets: [(abs_path, is_dir), ...]（来自 collect_targets）。
    mode/params: 重命名模式与参数。
    filter_glob: 通配符过滤（对 basename 做 fnmatch），空则不过滤。

    序号模式下按父目录分组、组内按名称排序独立编号。
    返回 [dict(old_path, old_name, new_name, new_path, status), ...]
    正则非法时抛 re.error。
    """
    # 过滤
    if filter_glob:
        pat = filter_glob
        targets = [t for t in targets
                   if fnmatch.fnmatch(os.path.basename(t[0]).lower(), pat.lower())]

    # 序号模式需要组内序号：按父目录分组，组内按名称排序
    index_map = {}  # abs_path -> 组内序号值
    if mode == MODE_NUMBER:
        start = params.get("start", 1)
        step = params.get("step", 1)
        groups = {}
        for path, _ in targets:
            groups.setdefault(os.path.dirname(path), []).append(path)
        for parent, paths in groups.items():
            paths.sort(key=lambda p: natural_key(os.path.basename(p)))
            for i, p in enumerate(paths):
                index_map[p] = start + i * step

    # 第一趟：算新名。会被腾空的源集合（真改名的旧路径）用于放宽冲突判定：
    # 目标虽已存在，但若它是本批次某个将被改走的源，则不算真冲突（交换/循环改名）。
    rows = []
    vacated = set()  # 会被腾空的旧路径（归一）
    for old_path, is_dir in targets:
        old_name = os.path.basename(old_path)
        parent = os.path.dirname(old_path)
        parent_name = os.path.basename(parent)
        new_name = compute_new_name(
            old_name, not is_dir, mode, params,
            index=index_map.get(old_path, 0), parent_name=parent_name,
        )
        new_path = os.path.join(parent, new_name)
        rows.append((old_path, new_name, old_name, new_path))
        if new_name and new_name != old_name:
            vacated.add(os.path.normcase(old_path))

    plan = []
    target_seen = {}  # new_path(归一) -> True，本批次内重复判定
    for old_path, new_name, old_name, new_path in rows:
        status = ST_RENAME
        if not new_name or new_name == old_name:
            status = ST_NOCHANGE
        else:
            key = os.path.normcase(new_path)
            if key in target_seen:
                status = ST_DUP
            elif (os.path.exists(new_path)
                  and key != os.path.normcase(old_path)
                  and key not in vacated):
                # 已存在且不是本批次会被腾走的源 → 真冲突
                status = ST_CONFLICT
            else:
                target_seen[key] = True

        plan.append({
            "old_path": old_path,
            "old_name": old_name,
            "new_name": new_name,
            "new_path": new_path,
            "status": status,
        })

    return plan


def _unique_temp(path):
    """在同目录生成一个不存在的临时名。"""
    base = path + ".brtmp"
    cand = base
    i = 0
    while os.path.exists(cand):
        i += 1
        cand = f"{base}{i}"
    return cand


def execute_plan(plan):
    """执行计划。叶子优先（深→浅）落盘；同目录交换/循环改名用临时名破环。

    返回 (done_count, skipped_count, undo_records)
    undo_records: [(new_path, old_path), ...] 按执行顺序（含临时步骤）。
    """
    to_do = [p for p in plan if p["status"] == ST_RENAME]
    # 叶子优先：路径越深越先改。始终从列表最前的空闲项执行，保证深项优先。
    to_do.sort(key=lambda p: p["old_path"].count(os.sep), reverse=True)
    pending = [(p["old_path"], p["new_path"]) for p in to_do]

    done = 0
    skipped = 0
    undo = []

    def is_free(old, new):
        nc_new = os.path.normcase(new)
        return nc_new == os.path.normcase(old) or not os.path.exists(new)

    while pending:
        idx = next((k for k, (o, n) in enumerate(pending) if is_free(o, n)), None)
        if idx is None:
            # 剩余项互相占用（循环），把最前一项挪到临时名以破环
            old, new = pending[0]
            temp = _unique_temp(old)
            try:
                os.rename(old, temp)
                undo.append((temp, old))
                pending[0] = (temp, new)
            except OSError:
                skipped += 1
                pending.pop(0)
            continue
        old, new = pending.pop(idx)
        try:
            os.rename(old, new)
            undo.append((new, old))
            done += 1
        except OSError:
            skipped += 1

    if undo:
        _push_undo(undo)
    return done, skipped, undo


def undo_last():
    """恢复最近一次重命名（弹出撤销栈顶）。逆序改回。

    返回 (restored_count, failed_count)；无记录返回 (0, 0)。
    """
    stack = _load_stack()
    if not stack:
        return 0, 0
    records = stack.pop()
    restored = 0
    failed = 0
    for new_path, old_path in reversed(records):
        try:
            if os.path.exists(new_path):
                os.rename(new_path, old_path)
                restored += 1
        except OSError:
            failed += 1
    _save_stack(stack)
    return restored, failed


def undo_depth():
    """撤销栈里还有多少批可撤。"""
    return len(_load_stack())


def _push_undo(records):
    stack = _load_stack()
    stack.append(records)
    # 最多保留 20 批，防止无限增长
    if len(stack) > 20:
        stack = stack[-20:]
    _save_stack(stack)


def _load_stack():
    """读撤销栈。兼容旧的扁平格式（单批 [[new,old],...]）。"""
    try:
        with open(UNDO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not data:
        return []
    # 旧格式：元素是 [new, old] 的二元列表 → 包成单批
    if isinstance(data[0], list) and len(data[0]) == 2 and isinstance(data[0][0], str):
        return [data]
    return data


def _save_stack(stack):
    try:
        if not stack:
            if os.path.exists(UNDO_FILE):
                os.remove(UNDO_FILE)
            return
        with open(UNDO_FILE, "w", encoding="utf-8") as f:
            json.dump(stack, f, ensure_ascii=False)
    except OSError:
        pass


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
    except OSError:
        pass


def parse_dnd_paths(data):
    """解析 tkinterdnd2 DND_FILES 拖入的路径字符串。

    Windows 下多个路径以空格分隔，含空格的路径用 {} 包裹。
    """
    paths = []
    token = ""
    in_brace = False
    for ch in data:
        if ch == "{":
            in_brace = True
            token = ""
        elif ch == "}":
            in_brace = False
            paths.append(token)
            token = ""
        elif ch == " " and not in_brace:
            if token:
                paths.append(token)
                token = ""
        else:
            token += ch
    if token:
        paths.append(token)
    return [p for p in paths if p]


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _DND_OK = True
except Exception:
    _DND_OK = False
    TkinterDnD = None
    DND_FILES = None


_BaseTk = TkinterDnD.Tk if _DND_OK else tk.Tk

# 性能阈值
RENDER_LIMIT = 2000       # 预览最多渲染这么多行，超出只显示前 N
AUTO_PREVIEW_LIMIT = 5000  # targets 超过此数自动关闭实时预览，改手动
DEBOUNCE_MS = 300


class App(_BaseTk):
    def __init__(self):
        super().__init__()
        self.title("批量重命名工具")
        self._cfg = load_config()
        self.geometry(self._cfg.get("geometry", "900x720"))
        self.roots = []          # 用户选择/拖入的顶层路径
        self.plan = []           # 当前预览计划
        self._preview_job = None  # 防抖 after id
        self._compute_seq = 0     # 后台计算序号，丢弃过期结果
        self._busy = False
        self._build_ui()
        self._apply_config()
        self._refresh_sample()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _apply_config(self):
        """把上次保存的常用设置恢复到界面。"""
        c = self._cfg
        try:
            if "scope" in c: self.scope_var.set(c["scope"])
            if "recursive" in c: self.recursive_var.set(c["recursive"])
            if "filter" in c: self.filter_var.set(c["filter"])
            if "realtime" in c: self.realtime_var.set(c["realtime"])
            if "keep_ext" in c: self.keep_ext_var.set(c["keep_ext"])
            if "mode_tab" in c and 0 <= c["mode_tab"] < len(MODE_OPTIONS):
                self.nb.select(c["mode_tab"])
        except (tk.TclError, KeyError):
            pass

    def _on_close(self):
        try:
            save_config({
                "geometry": self.winfo_geometry(),
                "scope": self.scope_var.get(),
                "recursive": self.recursive_var.get(),
                "filter": self.filter_var.get(),
                "realtime": self.realtime_var.get(),
                "keep_ext": self.keep_ext_var.get(),
                "mode_tab": self.nb.index(self.nb.select()),
            })
        except tk.TclError:
            pass
        self.destroy()

    # -- UI 装配 ------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        # 顶部：输入区
        top = ttk.LabelFrame(self, text="1. 选择或拖入 文件/文件夹")
        top.pack(fill="x", **pad)

        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=6, pady=4)
        ttk.Button(btns, text="选择目录", command=self._pick_dir).pack(side="left", padx=3)
        ttk.Button(btns, text="选择文件", command=self._pick_files).pack(side="left", padx=3)
        ttk.Button(btns, text="清空", command=self._clear_roots).pack(side="left", padx=3)

        hint = "把文件/文件夹拖到下面，或用上面的按钮选择（目录默认折叠，点箭头展开）" \
            if _DND_OK else "用上面的按钮选择（未装 tkinterdnd2，拖拽不可用）"
        ttk.Label(top, text=hint, foreground="#666666").pack(anchor="w", padx=6)

        srcwrap = ttk.Frame(top)
        srcwrap.pack(fill="x", padx=6, pady=4)
        self.src = ttk.Treeview(srcwrap, show="tree", height=8, selectmode="browse")
        svsb = ttk.Scrollbar(srcwrap, orient="vertical", command=self.src.yview)
        self.src.configure(yscrollcommand=svsb.set)
        self.src.pack(side="left", fill="both", expand=True)
        svsb.pack(side="right", fill="y")
        # iid -> 绝对路径；用于懒加载时定位
        self._node_path = {}
        self._root_iids = set()  # 顶层根节点 iid，仅这些可右键移除
        self.src.bind("<<TreeviewOpen>>", self._on_expand)
        self.src.bind("<Button-3>", self._on_src_rclick)
        if _DND_OK:
            self.src.drop_target_register(DND_FILES)
            self.src.dnd_bind("<<Drop>>", self._on_drop)

        # 中部：公共选项 + 模式标签页
        mid = ttk.LabelFrame(self, text="2. 作用范围与重命名规则")
        mid.pack(fill="x", **pad)

        common = ttk.Frame(mid)
        common.pack(fill="x", padx=6, pady=4)
        ttk.Label(common, text="作用范围:").pack(side="left")
        self.scope_var = tk.StringVar(value=SCOPE_ALL)
        cb = ttk.Combobox(common, textvariable=self.scope_var, values=SCOPE_OPTIONS,
                          state="readonly", width=20)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._schedule_preview())
        self.recursive_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(common, text="递归子层", variable=self.recursive_var,
                        command=self._schedule_preview).pack(side="left", padx=8)
        ttk.Label(common, text="过滤(如 *.png):").pack(side="left", padx=(12, 0))
        self.filter_var = tk.StringVar()
        fe = ttk.Entry(common, textvariable=self.filter_var, width=14)
        fe.pack(side="left", padx=6)
        self.filter_var.trace_add("write", lambda *a: self._schedule_preview())

        self.nb = ttk.Notebook(mid)
        self.nb.pack(fill="x", padx=6, pady=4)
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._schedule_preview())
        self._build_tab_replace()
        self._build_tab_number()
        self._build_tab_case()
        self._build_tab_edit()
        self._build_tab_ext()

        # 实时示例：不依赖真实选择，用固定样例展示当前规则效果
        self.sample_lbl = ttk.Label(mid, text="", foreground="#0a6")
        self.sample_lbl.pack(anchor="w", padx=8, pady=(0, 4))

        # 操作按钮
        ops = ttk.Frame(self)
        ops.pack(fill="x", **pad)
        ttk.Button(ops, text="刷新预览", command=self._preview).pack(side="left", padx=3)
        self.exec_btn = ttk.Button(ops, text="执行", command=self._execute)
        self.exec_btn.pack(side="left", padx=3)
        ttk.Button(ops, text="撤销上一次", command=self._undo).pack(side="left", padx=3)
        self.realtime_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ops, text="实时预览", variable=self.realtime_var).pack(side="left", padx=8)
        self.only_changed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ops, text="只看会改的", variable=self.only_changed_var,
                        command=self._render_plan).pack(side="left", padx=8)
        self.status_lbl = ttk.Label(ops, text="")
        self.status_lbl.pack(side="left", padx=12)

        # 预览表
        table = ttk.LabelFrame(self, text="3. 预览（双击「新名」可手动编辑；执行前请核对）")
        table.pack(fill="both", expand=True, **pad)
        srow = ttk.Frame(table)
        srow.pack(fill="x", padx=6, pady=2)
        ttk.Label(srow, text="搜索:").pack(side="left")
        self.search_var = tk.StringVar()
        ttk.Entry(srow, textvariable=self.search_var, width=24).pack(side="left", padx=4)
        self.search_var.trace_add("write", lambda *a: self._render_plan())
        ttk.Label(srow, text="（匹配原名或新名）", foreground="#888").pack(side="left")
        twrap = ttk.Frame(table)
        twrap.pack(fill="both", expand=True)
        cols = ("old_name", "new_name", "status", "old_path")
        self.tree = ttk.Treeview(twrap, columns=cols, show="headings")
        for c, txt, w in [("old_name", "原名", 200), ("new_name", "新名", 200),
                          ("status", "状态", 90), ("old_path", "路径", 340)]:
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="w")
        vsb = ttk.Scrollbar(twrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("rename", foreground="#158000")
        self.tree.tag_configure("nochange", foreground="#888888")
        self.tree.tag_configure("skip", foreground="#c00000")
        self.tree.tag_configure("done", foreground="#0a6")
        self.tree.tag_configure("fail", foreground="#c00000")
        # iid -> plan 中的索引，供双击编辑定位
        self._row_index = {}
        self.tree.bind("<Double-1>", self._on_row_dblclick)

    # -- 模式标签页 ---------------------------------------------------------
    def _watch(self, var):
        """给变量挂上触发实时预览的监听。"""
        var.trace_add("write", lambda *a: self._schedule_preview())
        return var

    def _build_tab_replace(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=MODE_REPLACE)
        r1 = ttk.Frame(f); r1.pack(fill="x", padx=6, pady=4)
        self.rule_var = self._watch(tk.StringVar(value=RULE_KEYWORD))
        ttk.Radiobutton(r1, text=RULE_KEYWORD, variable=self.rule_var, value=RULE_KEYWORD).pack(side="left")
        ttk.Radiobutton(r1, text=RULE_REGEX, variable=self.rule_var, value=RULE_REGEX).pack(side="left", padx=6)
        self.case_var = self._watch(tk.BooleanVar(value=False))
        ttk.Checkbutton(r1, text="区分大小写", variable=self.case_var).pack(side="left", padx=10)
        self.keep_ext_var = self._watch(tk.BooleanVar(value=False))
        ttk.Checkbutton(r1, text="保留扩展名不动", variable=self.keep_ext_var).pack(side="left", padx=10)
        r2 = ttk.Frame(f); r2.pack(fill="x", padx=6, pady=4)
        ttk.Label(r2, text="查找:").pack(side="left")
        self.find_var = self._watch(tk.StringVar())
        ttk.Entry(r2, textvariable=self.find_var, width=28).pack(side="left", padx=6)
        ttk.Label(r2, text="替换为:").pack(side="left")
        self.repl_var = self._watch(tk.StringVar())
        ttk.Entry(r2, textvariable=self.repl_var, width=28).pack(side="left", padx=6)

    def _build_tab_number(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=MODE_NUMBER)
        r1 = ttk.Frame(f); r1.pack(fill="x", padx=6, pady=4)
        ttk.Label(r1, text="起始:").pack(side="left")
        self.num_start = self._watch(tk.IntVar(value=1))
        ttk.Spinbox(r1, from_=0, to=999999, textvariable=self.num_start, width=6).pack(side="left", padx=4)
        ttk.Label(r1, text="步长:").pack(side="left")
        self.num_step = self._watch(tk.IntVar(value=1))
        ttk.Spinbox(r1, from_=1, to=1000, textvariable=self.num_step, width=5).pack(side="left", padx=4)
        ttk.Label(r1, text="补零位数:").pack(side="left")
        self.num_pad = self._watch(tk.IntVar(value=3))
        ttk.Spinbox(r1, from_=0, to=10, textvariable=self.num_pad, width=4).pack(side="left", padx=4)
        ttk.Label(r1, text="样式:").pack(side="left", padx=(10, 0))
        self.num_style = self._watch(tk.StringVar(value=SEQ_NUMBER))
        ttk.Combobox(r1, textvariable=self.num_style, values=SEQ_OPTIONS,
                     state="readonly", width=14).pack(side="left", padx=4)
        r2 = ttk.Frame(f); r2.pack(fill="x", padx=6, pady=4)
        self.num_use_parent = self._watch(tk.BooleanVar(value=True))
        ttk.Checkbutton(r2, text="用父文件夹名作前缀", variable=self.num_use_parent).pack(side="left")
        ttk.Label(r2, text="分隔符:").pack(side="left", padx=(10, 0))
        self.num_sep = self._watch(tk.StringVar(value="-"))
        ttk.Entry(r2, textvariable=self.num_sep, width=4).pack(side="left", padx=4)
        r3 = ttk.Frame(f); r3.pack(fill="x", padx=6, pady=4)
        ttk.Label(r3, text="前缀:").pack(side="left")
        self.num_prefix = self._watch(tk.StringVar())
        ttk.Entry(r3, textvariable=self.num_prefix, width=10).pack(side="left", padx=4)
        ttk.Label(r3, text="后缀:").pack(side="left")
        self.num_suffix = self._watch(tk.StringVar())
        ttk.Entry(r3, textvariable=self.num_suffix, width=10).pack(side="left", padx=4)
        ttk.Label(r3, text="位置:").pack(side="left", padx=(10, 0))
        self.num_pos = self._watch(tk.StringVar(value=POS_SUFFIX))
        ttk.Combobox(r3, textvariable=self.num_pos, values=POS_OPTIONS,
                     state="readonly", width=8).pack(side="left", padx=4)

    def _build_tab_case(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=MODE_CASE)
        r = ttk.Frame(f); r.pack(fill="x", padx=6, pady=8)
        self.case_mode = self._watch(tk.StringVar(value=CASE_LOWER))
        for opt in CASE_OPTIONS:
            ttk.Radiobutton(r, text=opt, variable=self.case_mode, value=opt).pack(side="left", padx=6)

    def _build_tab_edit(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=MODE_EDIT)
        r1 = ttk.Frame(f); r1.pack(fill="x", padx=6, pady=4)
        ttk.Label(r1, text="删除第").pack(side="left")
        self.del_from = self._watch(tk.IntVar(value=0))
        ttk.Spinbox(r1, from_=0, to=999, textvariable=self.del_from, width=5).pack(side="left", padx=3)
        ttk.Label(r1, text="到第").pack(side="left")
        self.del_to = self._watch(tk.IntVar(value=0))
        ttk.Spinbox(r1, from_=0, to=999, textvariable=self.del_to, width=5).pack(side="left", padx=3)
        ttk.Label(r1, text="个字符（0=不删）").pack(side="left")
        r2 = ttk.Frame(f); r2.pack(fill="x", padx=6, pady=4)
        ttk.Label(r2, text="在位置").pack(side="left")
        self.ins_pos = self._watch(tk.IntVar(value=0))
        ttk.Spinbox(r2, from_=0, to=999, textvariable=self.ins_pos, width=5).pack(side="left", padx=3)
        ttk.Label(r2, text="插入:").pack(side="left")
        self.ins_text = self._watch(tk.StringVar())
        ttk.Entry(r2, textvariable=self.ins_text, width=16).pack(side="left", padx=4)
        ttk.Label(r2, text="（位置从 0 起，空则不插）").pack(side="left")

    def _build_tab_ext(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=MODE_EXT)
        r = ttk.Frame(f); r.pack(fill="x", padx=6, pady=8)
        ttk.Label(r, text="统一扩展名为:").pack(side="left")
        self.target_ext = self._watch(tk.StringVar())
        ttk.Entry(r, textvariable=self.target_ext, width=12).pack(side="left", padx=6)
        ttk.Label(r, text="如 jpg（只作用于文件）", foreground="#666").pack(side="left")

    # -- 输入处理（树形，懒加载）-------------------------------------------
    def _refresh_drop(self):
        self.src.delete(*self.src.get_children())
        self._node_path.clear()
        self._root_iids.clear()
        for r in self.roots:
            iid = self._insert_node("", r, top=True)
            self._root_iids.add(iid)

    def _insert_node(self, parent_iid, path, top=False):
        """插入一个节点。目录默认折叠，先放占位子节点以显示展开箭头。"""
        name = path if top else os.path.basename(path)
        is_dir = os.path.isdir(path)
        text = ("📁 " if is_dir else "📄 ") + name
        iid = self.src.insert(parent_iid, "end", text=text, open=False)
        self._node_path[iid] = path
        if is_dir:
            # 占位节点：真正展开时替换为实际内容
            self.src.insert(iid, "end", text="…")
        return iid

    def _on_expand(self, event):
        iid = self.src.focus()
        path = self._node_path.get(iid)
        if not path or not os.path.isdir(path):
            return
        children = self.src.get_children(iid)
        # 已加载过（首个子节点在 _node_path 里）则不重复加载
        if children and children[0] in self._node_path:
            return
        # 移除占位节点，加载实际内容
        self.src.delete(*children)
        try:
            entries = sorted(os.listdir(path),
                             key=lambda n: (not os.path.isdir(os.path.join(path, n)), natural_key(n)))
        except OSError:
            return
        for n in entries:
            self._insert_node(iid, os.path.join(path, n))

    def _add_roots(self, paths):
        for p in paths:
            p = os.path.abspath(p)
            if os.path.exists(p) and p not in self.roots:
                self.roots.append(p)
        self._refresh_drop()
        self._schedule_preview()

    def _pick_dir(self):
        d = filedialog.askdirectory()
        if d:
            self._add_roots([d])

    def _pick_files(self):
        fs = filedialog.askopenfilenames()
        if fs:
            self._add_roots(list(fs))

    def _clear_roots(self):
        self.roots = []
        self.plan = []
        self._refresh_drop()
        self.tree.delete(*self.tree.get_children())
        self.status_lbl.config(text="")

    def _on_drop(self, event):
        self._add_roots(parse_dnd_paths(event.data))

    # -- 预览/执行/撤销 -----------------------------------------------------
    def _current_mode(self):
        idx = self.nb.index(self.nb.select())
        return MODE_OPTIONS[idx]

    def _current_params(self):
        """收集当前模式的参数。IntVar 可能因空输入抛异常，统一兜底。"""
        def iv(var, default=0):
            try:
                return int(var.get())
            except (tk.TclError, ValueError):
                return default
        mode = self._current_mode()
        p = {}
        if mode == MODE_REPLACE:
            p = {"rule": self.rule_var.get(), "find": self.find_var.get(),
                 "repl": self.repl_var.get(), "case_sensitive": self.case_var.get(),
                 "include_ext": not self.keep_ext_var.get()}
        elif mode == MODE_NUMBER:
            p = {"start": iv(self.num_start, 1), "step": iv(self.num_step, 1),
                 "pad": iv(self.num_pad, 0), "use_parent": self.num_use_parent.get(),
                 "sep": self.num_sep.get(), "prefix": self.num_prefix.get(),
                 "suffix": self.num_suffix.get(), "position": self.num_pos.get(),
                 "style": self.num_style.get()}
        elif mode == MODE_CASE:
            p = {"case_mode": self.case_mode.get()}
        elif mode == MODE_EDIT:
            p = {"del_from": iv(self.del_from), "del_to": iv(self.del_to),
                 "ins_pos": iv(self.ins_pos), "ins_text": self.ins_text.get()}
        elif mode == MODE_EXT:
            p = {"target_ext": self.target_ext.get()}
        return mode, p

    def _schedule_preview(self):
        """参数变化时触发。示例即时刷新；实时预览开则防抖刷新表格。"""
        self._refresh_sample()
        if not getattr(self, "realtime_var", None) or not self.realtime_var.get():
            return
        if self._preview_job:
            self.after_cancel(self._preview_job)
        self._preview_job = self.after(DEBOUNCE_MS, self._preview)

    def _refresh_sample(self):
        """用固定样例展示当前规则效果，不依赖真实选择。"""
        if not hasattr(self, "sample_lbl"):
            return
        mode, params = self._current_params()
        sample = "sample.jpg"
        try:
            # 序号示例给个组内序号 1，父目录名用 folder 演示
            result = compute_new_name(sample, True, mode, params, index=1, parent_name="folder")
            self.sample_lbl.config(text=f"示例：{sample} → {result}", foreground="#0a6")
        except re.error as e:
            self.sample_lbl.config(text=f"示例：正则非法（{e}）", foreground="#c00")
        except Exception:
            self.sample_lbl.config(text="")

    def _preview(self):
        self._preview_job = None
        if not self.roots or self._busy:
            return
        mode, params = self._current_params()
        scope = self.scope_var.get()
        recursive = self.recursive_var.get()
        filter_glob = self.filter_var.get().strip()
        self._busy = True
        self._compute_seq += 1
        seq = self._compute_seq
        self.exec_btn.config(state="disabled")
        self.status_lbl.config(text="计算中…")

        def work():
            try:
                targets = collect_targets(self.roots, scope, recursive)
                plan = build_plan(targets, mode, params, filter_glob)
                err = None
            except re.error as e:
                targets, plan, err = [], [], f"正则非法：{e}"
            except Exception as e:
                targets, plan, err = [], [], str(e)
            self.after(0, lambda: self._on_computed(seq, plan, err))

        threading.Thread(target=work, daemon=True).start()

    def _on_computed(self, seq, plan, err):
        self._busy = False
        self.exec_btn.config(state="normal")
        if seq != self._compute_seq:
            return  # 过期结果，已有更新的计算
        if err:
            self.status_lbl.config(text=err)
            return
        self.plan = plan
        self._render_plan()

    @staticmethod
    def _status_tag(status):
        return {ST_RENAME: "rename", ST_NOCHANGE: "nochange",
                ST_CONFLICT: "skip", ST_DUP: "skip",
                ST_DONE: "done", ST_FAIL: "fail"}.get(status, "nochange")

    def _visible_indices(self):
        """按「只看会改的」+ 搜索词过滤，返回要显示的 plan 索引列表。"""
        only = self.only_changed_var.get()
        kw = self.search_var.get().strip().lower()
        out = []
        for i, p in enumerate(self.plan):
            if only and p["status"] not in (ST_RENAME, ST_DONE):
                continue
            if kw and kw not in p["old_name"].lower() and kw not in p["new_name"].lower():
                continue
            out.append(i)
        return out

    def _render_plan(self):
        # 打断上一次未完成的分批渲染
        self._render_seq = getattr(self, "_render_seq", 0) + 1
        self.tree.delete(*self.tree.get_children())
        self._row_index.clear()
        counts = {}
        for p in self.plan:
            counts[p["status"]] = counts.get(p["status"], 0) + 1
        self._visible = self._visible_indices()
        self._render_batch(0, self._render_seq)
        shown = len(self._visible)
        capped = f"（仅显示前 {RENDER_LIMIT}）" if shown > RENDER_LIMIT else ""
        parts = [f"{k} {v}" for k, v in counts.items()]
        self.status_lbl.config(
            text=f"共 {len(self.plan)} 项 | 显示 {min(shown, RENDER_LIMIT)}{capped} | " + " | ".join(parts)
        )

    def _render_batch(self, start, seq):
        """分批插入，每批 500 行。seq 过期则停止（有更新的渲染）。"""
        if seq != self._render_seq:
            return
        vis = self._visible
        end = min(start + 500, len(vis), RENDER_LIMIT)
        for k in range(start, end):
            i = vis[k]
            p = self.plan[i]
            iid = self.tree.insert("", "end",
                                   values=(p["old_name"], p["new_name"], p["status"], p["old_path"]),
                                   tags=(self._status_tag(p["status"]),))
            self._row_index[iid] = i
        if end < min(len(vis), RENDER_LIMIT):
            self.after(1, lambda: self._render_batch(end, seq))

    def _on_row_dblclick(self, event):
        """双击某行手动编辑「新名」。改后重算该行状态。"""
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        iid = self.tree.identify_row(event.y)
        i = self._row_index.get(iid)
        if i is None:
            return
        p = self.plan[i]
        from tkinter import simpledialog
        new_name = simpledialog.askstring("编辑新名", f"原名：{p['old_name']}", initialvalue=p["new_name"])
        if new_name is None:
            return
        parent = os.path.dirname(p["old_path"])
        p["new_name"] = new_name
        p["new_path"] = os.path.join(parent, new_name)
        # 重算该行状态（简单判定：无变化/冲突/将改）
        if not new_name or new_name == p["old_name"]:
            p["status"] = ST_NOCHANGE
        elif os.path.exists(p["new_path"]) and os.path.normcase(p["new_path"]) != os.path.normcase(p["old_path"]):
            p["status"] = ST_CONFLICT
        else:
            p["status"] = ST_RENAME
        self.tree.item(iid, values=(p["old_name"], p["new_name"], p["status"], p["old_path"]),
                       tags=(self._status_tag(p["status"]),))

    def _execute(self):
        if not self.plan:
            messagebox.showwarning("提示", "请先点「预览」生成计划")
            return
        to_do = [p for p in self.plan if p["status"] == ST_RENAME]
        if not to_do:
            messagebox.showinfo("提示", "没有需要重命名的项")
            return
        if not messagebox.askyesno("确认", f"确定重命名 {len(to_do)} 项？此操作可撤销。"):
            return
        # 记录哪些计划项真正成功了：执行后按 new_path 是否落地判断
        try:
            done, skipped, _ = execute_plan(self.plan)
        except Exception:
            messagebox.showerror("执行出错", traceback.format_exc())
            return
        # 保留结果：成功项标 ✓ 已改，并把原名/路径更新为新状态；未落地的标 ✗ 失败
        for p in to_do:
            if os.path.exists(p["new_path"]) and not os.path.exists(p["old_path"]):
                p["status"] = ST_DONE
                p["old_name"] = p["new_name"]
                p["old_path"] = p["new_path"]
            else:
                p["status"] = ST_FAIL
        self._render_plan()
        messagebox.showinfo("完成", f"成功 {done} 项，跳过 {skipped} 项。结果已在表中标记，可「撤销上一次」恢复。")
        self.status_lbl.config(text=f"已执行：成功 {done}，跳过 {skipped}。绿色 ✓ 为已改，可撤销。")

    def _undo(self):
        restored, failed = undo_last()
        if restored == 0 and failed == 0:
            messagebox.showinfo("提示", "没有可撤销的记录")
            return
        left = undo_depth()
        messagebox.showinfo("撤销完成", f"恢复 {restored} 项，失败 {failed} 项。剩余可撤销 {left} 批。")
        self.status_lbl.config(text=f"已撤销：恢复 {restored}，失败 {failed}。剩余可撤销 {left} 批。")

    # -- 输入树右键移除 -----------------------------------------------------
    def _on_src_rclick(self, event):
        iid = self.src.identify_row(event.y)
        if not iid or iid not in self._root_iids:
            return  # 只允许移除顶层根项
        path = self._node_path.get(iid)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=f"移除：{os.path.basename(path) or path}",
                         command=lambda: self._remove_root(path))
        menu.tk_popup(event.x_root, event.y_root)

    def _remove_root(self, path):
        self.roots = [r for r in self.roots if r != path]
        self._refresh_drop()
        self._schedule_preview()


if __name__ == "__main__":
    App().mainloop()

