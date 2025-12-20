# fudai_ops_v5_operator_app.py
# ======================================================
# 福袋系统后台简化版（运营友好型·V5｜Python 单文件可运行）
# ======================================================
# ✅ 首页新增「福袋列表页」：列表→编辑配置→选品→计算→模拟→上线→返回列表
# ✅ 补全上下架时间配置（到期自动下架）
# ✅ 手动添加商品：带“确认添加” + 分配等级 + 成本区间校验 + 真正加入当前福袋已选池
# ✅ 保留 V3 全部能力：筛选/手动加商品、期望成本校验、自适应调权、保底、抽卡模拟、概率公示、活动结束自动剔除、库存报警、每日16:00提醒
#
# 运行：python fudai_ops_v5_operator_app.py
# 依赖：Python 标准库（tkinter）
#
# 说明：
# - 本文件内置“模拟商品库”（含折扣活动结束时间），用于演示全流程。
# - 接真实后台：替换 load_catalog_from_backend() 即可（保持 Item 字段即可）。

from __future__ import annotations
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import datetime as dt
import calendar as cal
import random
import json

# -------------------------------
# 常量
# -------------------------------
LEVELS = ["L", "E", "R", "U", "C"]
LEVEL_NAME = {"L": "传说", "E": "史诗", "R": "优秀", "U": "精良", "C": "普通"}
LEVEL_BADGE = {"L": " 传说", "E": " 史诗", "R": " 优秀", "U": " 精良", "C": " 普通"}

DEFAULT_PK = {"L": 1, "E": 5, "R": 15, "U": 30, "C": 49}  # 百分比（会自动凑100）
DEFAULT_RANGE = {"L": (50, 80), "E": (30, 55), "R": (18, 35), "U": (10, 20), "C": (4, 12)}  # 元（示例）
DEFAULT_ALARM = 50

# 内部“等级价值系数”（用于生成预期成本参考，不展示给运营）
_ALPHA = {"L": 3.0, "E": 2.0, "R": 1.5, "U": 1.2, "C": 1.0}

# -------------------------------
# 数据结构
# -------------------------------
@dataclass(frozen=True)
class LevelRange:
    lo: int
    hi: int

@dataclass(frozen=True)
class Config:
    bag_name: str
    P: float                  # 单抽标价（元）
    g: float                  # 目标利润率（0~1）
    d: float                  # 十连折扣系数（0~1）
    q: float                  # 十连抽占比（0~1）
    price_ratio: float        # 售价筛选比例（0~1）  商品售价 >= P*ratio
    up_time: dt.datetime      # 上架时间
    down_time: dt.datetime    # 下架时间
    pity_on: bool
    X: Optional[int]          # 保底阈值
    p_k: Dict[str, float]     # 等级概率（0~1）
    cost_ranges: Dict[str, LevelRange]  # 数值型成本范围（元）
    notify_person: str = "当前操作人"

@dataclass
class Item:
    id: int
    name: str
    price: float
    cost: float
    stock: int
    steam_lowest: float
    enabled: bool = True
    blacklisted: bool = False
    platform: str = "steam"
    is_dlc: bool = False

    # 折扣活动信息（用于“自动下架/提醒”）
    discount_status: str = "无活动"  # "活动中" / "无活动" / "即将结束"
    discount_end: Optional[dt.datetime] = None  # 活动结束时间（无活动为 None）

    # 运营设置：库存报警值（可在选品页编辑）
    alarm: int = DEFAULT_ALARM

    # 系统匹配：等级（基于成本范围自动匹配 或 手动分配）
    level: str = "C"

@dataclass
class BagState:
    bag_id: str
    status: str = "未上线"  # 未上线 / 已上线 / 已下架
    cfg: Optional[Config] = None
    bag_type: str = "原福袋"
    recommended: bool = False
    revenue: float = 0.0
    orders: int = 0
    total_cost: float = 0.0
    cover_path: str = ""
    bg_path: str = ""
    ad_path: str = ""
    distribution: str = ""
    remark: str = ""

    # 选品相关
    filtered: List[Item] = None
    selected_ids: set[int] = None
    manual_added_ids: set[int] = None

    # 计算/模拟
    calc_ok: bool = False
    adapt_triggered: bool = False
    adapt_success: bool = False
    final_probs: Dict[int, float] = None
    config_confirmed: bool = False

    def __post_init__(self):
        if self.filtered is None: self.filtered = []
        if self.selected_ids is None: self.selected_ids = set()
        if self.manual_added_ids is None: self.manual_added_ids = set()
        if self.final_probs is None: self.final_probs = {}


@dataclass
class MonthlyBagState:
    bag_name: str = ""
    P: float = 0.0
    d: float = 0.95
    up_time: Optional[dt.datetime] = None
    down_time: Optional[dt.datetime] = None
    cover_path: str = ""
    bg_path: str = ""
    ad_path: str = ""
    notify_person: str = "当前操作人"
    remark: str = ""
    items: List[Item] = None
    status: str = "未上线"

    def __post_init__(self):
        if self.items is None:
            self.items = []

# -------------------------------
# 内部计算（隐藏）
# -------------------------------
def calc_p_eff(P: float, d: float, q: float) -> float:
    return P * (1.0 - q + q * d)

def calc_c_target(P_eff: float, g: float) -> float:
    return P_eff * (1.0 - g)

def normalize_pk_percent(pk_percent: Dict[str, float]) -> Dict[str, float]:
    keys = ["L", "E", "R", "U", "C"]
    vals = [max(0.0, float(pk_percent.get(k, 0.0))) for k in keys]
    total = sum(vals)
    diff = 100.0 - total
    vals[-1] += diff
    if vals[-1] < 0:
        raise ValueError("等级概率总和超过 100%，请调小前四项。")
    return {k: v / 100.0 for k, v in zip(keys, vals)}

def expected_cost_reference(cfg: Config) -> Dict[str, float]:
    P_eff = calc_p_eff(cfg.P, cfg.d, cfg.q)
    C_target = calc_c_target(P_eff, cfg.g)
    denom = sum(cfg.p_k.get(k, 0.0) * _ALPHA[k] for k in LEVELS)
    denom = max(denom, 1e-9)
    t = C_target / denom
    return {k: _ALPHA[k] * t for k in LEVELS}

def assign_level_by_cost(cfg: Config, it: Item) -> Optional[str]:
    for lvl in LEVELS:
        r = cfg.cost_ranges[lvl]
        if r.lo <= it.cost <= r.hi:
            return lvl
    return None

def hard_pass(cfg: Config, it: Item) -> bool:
    if not it.enabled:
        return False
    if it.blacklisted:
        return False
    if it.stock <= 0:
        return False
    if it.platform.lower() != "steam":
        return False
    if it.is_dlc:
        return False
    if it.price < it.steam_lowest:
        return False
    if it.price < cfg.P * cfg.price_ratio:
        return False
    lvl = assign_level_by_cost(cfg, it)
    return lvl is not None

def apply_matching_level(cfg: Config, it: Item) -> None:
    lvl = assign_level_by_cost(cfg, it)
    it.level = lvl if lvl is not None else "C"

def build_final_probs(cfg: Config, selected: List[Item], beta: float) -> Dict[int, float]:
    by_level: Dict[str, List[Item]] = {k: [] for k in LEVELS}
    for it in selected:
        by_level[it.level].append(it)

    p_k = dict(cfg.p_k)
    for lvl in LEVELS:
        if p_k.get(lvl, 0.0) > 0 and len(by_level[lvl]) == 0:
            p_k["C"] = p_k.get("C", 0.0) + p_k[lvl]
            p_k[lvl] = 0.0

    probs: Dict[int, float] = {}
    for lvl, its in by_level.items():
        if not its:
            continue
        raw = [1.0 / (max(it.cost, 1e-9) ** beta) for it in its]
        s = sum(raw)
        if s <= 0:
            for it in its:
                probs[it.id] = p_k.get(lvl, 0.0) / len(its)
        else:
            for it, w in zip(its, raw):
                probs[it.id] = p_k.get(lvl, 0.0) * (w / s)
    return probs

def expected_cost_total(cfg: Config, selected: List[Item], probs: Dict[int, float]) -> float:
    ec_base = sum(probs.get(it.id, 0.0) * it.cost for it in selected)
    ec_pity = 0.0
    if cfg.pity_on and cfg.X and cfg.X > 0:
        legends = [it for it in selected if it.level == "L"]
        pL = sum(probs.get(it.id, 0.0) for it in legends)
        if legends and pL > 1e-12:
            e_cost_L = sum(probs.get(it.id, 0.0) * it.cost for it in legends) / pL
            ec_pity = e_cost_L / cfg.X
    return ec_base + ec_pity

def auto_adapt(cfg: Config, selected: List[Item], c_target: float,
              beta_max: float = 6.0, beta_step: float = 0.25) -> Tuple[bool, float, Dict[int, float]]:
    best_ok = False
    best_ec = float("inf")
    best_probs: Dict[int, float] = {}

    beta = 1.0
    while beta <= beta_max + 1e-12:
        probs = build_final_probs(cfg, selected, beta=beta)
        ec = expected_cost_total(cfg, selected, probs)
        if ec < best_ec:
            best_ec = ec
            best_probs = probs
            best_ok = ec <= c_target + 1e-12
        if ec <= c_target + 1e-12:
            return True, ec, probs
        beta += beta_step

    return best_ok, best_ec, best_probs

# -------------------------------
# 模拟商品库（接后台可替换）
# -------------------------------
def load_catalog_from_backend(seed: int = 20251212, total: int = 800) -> List[Item]:
    rnd = random.Random(seed)
    now = dt.datetime.now()
    items: List[Item] = []
    for i in range(1, total + 1):
        base_cost = rnd.choice([4, 6, 9, 12, 18, 25, 35, 50, 65])
        cost = base_cost * rnd.uniform(0.8, 1.25)
        steam_low = cost * rnd.uniform(1.2, 1.9)
        price = steam_low * rnd.uniform(0.9, 1.4)
        stock = 0 if rnd.random() < 0.02 else rnd.randint(10, 2000)

        discount_end = None
        discount_status = "无活动"
        if rnd.random() < 0.25:
            hours = rnd.randint(-6, 5 * 24)
            discount_end = now + dt.timedelta(hours=hours)
            if discount_end <= now:
                discount_status = "无活动"
            else:
                discount_status = "即将结束" if (discount_end - now).total_seconds() <= 24 * 3600 else "活动中"

        items.append(Item(
            id=i,
            name=f"游戏_{i:04d}",
            price=float(price),
            cost=float(cost),
            stock=int(stock),
            steam_lowest=float(steam_low),
            enabled=(rnd.random() > 0.01),
            blacklisted=(rnd.random() < 0.01),
            platform=("steam" if rnd.random() < 0.98 else "other"),
            is_dlc=(rnd.random() < 0.02),
            discount_status=discount_status,
            discount_end=discount_end,
            alarm=DEFAULT_ALARM,
        ))
    return items

# -------------------------------
# 时间解析
# -------------------------------
def parse_dt(s: str, field_name: str) -> dt.datetime:
    s = (s or "").strip()
    if not s:
        raise ValueError(f"{field_name} 不能为空，格式：YYYY-MM-DD HH:MM")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt)
        except Exception:
            pass
    raise ValueError(f"{field_name} 格式不正确，应为：YYYY-MM-DD HH:MM")

def fmt_dt(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M")


# -------------------------------
# 统一的日期时间选择器（YYYY-MM-DD HH:MM）
# -------------------------------
def open_datetime_picker(owner: tk.Misc, var: tk.StringVar):
    top = tk.Toplevel(owner)
    top.title("选择时间")
    top.geometry("320x360")
    top.resizable(False, False)

    try:
        current_dt = parse_dt(var.get(), "时间")
    except Exception:
        current_dt = dt.datetime.now()
    current = current_dt.date()
    cur_h, cur_m = current_dt.hour, current_dt.minute

    state = {"year": current.year, "month": current.month}

    header = ttk.Frame(top)
    header.pack(fill="x", pady=4)
    lbl = ttk.Label(header, text="")
    lbl.pack(side="left", padx=8)

    btns: list[tk.Widget] = []
    grid = ttk.Frame(top)
    grid.pack(fill="both", expand=True, padx=6, pady=6)
    header_row = ttk.Frame(grid)
    header_row.pack(fill="x", pady=(0, 4))
    for w in ["一", "二", "三", "四", "五", "六", "日"]:
        ttk.Label(header_row, text=w, width=4, anchor="center").pack(side="left", expand=True)

    def render():
        lbl.configure(text=f"{state['year']}年{state['month']:02d}月")
        for btn in btns:
            btn.destroy()
        btns.clear()
        cal_mat = cal.monthcalendar(state["year"], state["month"])
        for week in cal_mat:
            row = ttk.Frame(grid)
            row.pack(fill="x")
            for d in week:
                txt = f"{d:02d}" if d else ""
                btn = ttk.Button(row, text=txt, width=4,
                                 command=(lambda day=d: choose_day(day)) if d else None)
                btn.pack(side="left", expand=True, padx=1, pady=1)
                btns.append(btn)

    def prev_month():
        if state["month"] == 1:
            state["month"] = 12
            state["year"] -= 1
        else:
            state["month"] -= 1
        render()

    def next_month():
        if state["month"] == 12:
            state["month"] = 1
            state["year"] += 1
        else:
            state["month"] += 1
        render()

    ttk.Button(header, text="<", command=prev_month).pack(side="left", padx=(8, 4))
    ttk.Button(header, text=">", command=next_month).pack(side="left")

    time_box = ttk.Frame(top, padding=6)
    time_box.pack(fill="x", pady=(4, 0))
    ttk.Label(time_box, text="时间：").pack(side="left")
    sp_hour = tk.Spinbox(time_box, from_=0, to=23, width=4, format="%02.0f")
    sp_min = tk.Spinbox(time_box, from_=0, to=59, width=4, format="%02.0f")
    sp_hour.delete(0, "end"); sp_hour.insert(0, f"{cur_h:02d}")
    sp_min.delete(0, "end"); sp_min.insert(0, f"{cur_m:02d}")
    sp_hour.pack(side="left", padx=(2, 4))
    ttk.Label(time_box, text=":").pack(side="left")
    sp_min.pack(side="left", padx=(4, 4))

    def choose_day(day: int):
        if day <= 0:
            return
        hh = int(sp_hour.get() or 0)
        mm = int(sp_min.get() or 0)
        val = dt.datetime(state["year"], state["month"], day, hh, mm).strftime("%Y-%m-%d %H:%M")
        var.set(val)
        top.destroy()

    render()

def _safe_kw(val: str, placeholder: str) -> str:
    val = (val or "").strip()
    return "" if val == placeholder else val

# -------------------------------
# GUI App：列表页 + 3页 + 1提醒弹窗
# -------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("福袋系统后台（运营友好型·V5）")
        self.geometry("1160x800")
        self.minsize(1060, 740)

        self.catalog: List[Item] = load_catalog_from_backend()

        # 多福袋
        self.bags: Dict[str, BagState] = {}
        self.monthly_bag: MonthlyBagState = MonthlyBagState()
        self.settings = {
            'reminder_enabled': True,
            'reminder_time': '16:00',
            'reminder_person': '默认运营',
            'reminder_phone': '13800000000',
            'notify_enabled': True,
            'notify_supervisor_on_force': True,
            'is_super_admin': False,
            'supervisor_name': '上级运营',
            'supervisor_phone': '13900000000',
        }
        self.current_bag_id: Optional[str] = None

        # 提醒相关
        self._remind_snooze_date: Optional[dt.date] = None
        self._remind_last_shown_date: Optional[dt.date] = None

        container = ttk.Frame(self, padding=12)
        container.pack(fill="both", expand=True)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        self.frames = {}
        for F in (PageBagList, PageConfig, PagePick, PageResult, PageMonthlyBag):
            frame = F(container, self)
            self.frames[F.__name__] = frame
            frame.grid(row=0, column=0, sticky="nsew")

        self.show("PageBagList")
        self.after(1000, self._tick_minutely)

    def apply_placeholder(self, entry: tk.Entry, var: tk.StringVar, placeholder: str):
        entry.configure(foreground="#9ca3af")
        var.set(placeholder)

        def focus_in(_):
            if var.get() == placeholder:
                var.set("")
                entry.configure(foreground="#111827")

        def focus_out(_):
            if not var.get().strip():
                var.set(placeholder)
                entry.configure(foreground="#9ca3af")

        entry.bind("<FocusIn>", focus_in)
        entry.bind("<FocusOut>", focus_out)

    # ---------- bag helpers ----------
    def _next_bag_id(self) -> str:
        n = len(self.bags) + 1
        while True:
            bid = f"FD{n:03d}"
            if bid not in self.bags:
                return bid
            n += 1

    def ensure_current(self) -> BagState:
        if not self.current_bag_id or self.current_bag_id not in self.bags:
            raise RuntimeError("缺少当前福袋，请返回列表重新进入。")
        return self.bags[self.current_bag_id]

    def show(self, name: str):
        self.frames[name].tkraise()
        self.frames[name].on_show()

    def _profit_rate(self, cfg: Config, selected: List[Item], probs: Dict[int, float]) -> float:
        if not cfg or not selected or not probs:
            return 0.0
        profit_val = self._profit_value(cfg, selected, probs)
        P_eff = calc_p_eff(cfg.P, cfg.d, cfg.q)
        return (profit_val / P_eff) if (P_eff and profit_val is not None) else 0.0

    def _profit_value(self, cfg: Config, selected: List[Item], probs: Dict[int, float]) -> Optional[float]:
        if not cfg or not selected or not probs:
            return None
        P_eff = calc_p_eff(cfg.P, cfg.d, cfg.q)
        ec = expected_cost_total(cfg, selected, probs)
        return P_eff - ec

    def build_config_snapshot(self, bag: BagState) -> str:
        cfg = bag.cfg
        if not cfg:
            return "缺少配置。"
        selected = [it for it in self.catalog if it.id in bag.selected_ids]
        probs = bag.final_probs or {}
        pr = self._profit_rate(cfg, selected, probs) if probs else None

        sys_ids = {it.id for it in bag.filtered}
        manual = bag.manual_added_ids
        sys_count = len(set(bag.selected_ids) & sys_ids)
        manual_count = len(set(bag.selected_ids) & manual)

        level_counts: Dict[str, int] = {k: 0 for k in LEVELS}
        for it in selected:
            level_counts[it.level] = level_counts.get(it.level, 0) + 1

        lines = []
        lines.append(f"福袋ID：{bag.bag_id}")
        lines.append(f"福袋名称：{cfg.bag_name}")
        lines.append(f"上下架时间：{fmt_dt(cfg.up_time)} ~ {fmt_dt(cfg.down_time)}")
        lines.append(f"单抽标价 P：{cfg.P:.2f} 元")
        lines.append(f"十连折扣系数 d：{cfg.d:.2f}")
        lines.append(f"十连抽占比 q：{cfg.q * 100:.0f}%")
        P_eff = calc_p_eff(cfg.P, cfg.d, cfg.q)
        lines.append(f"等效单抽收入 P_eff：{P_eff:.2f} 元")
        lines.append(f"目标利润率：{cfg.g * 100:.2f}%")
        lines.append(f"当前预计利润率：{(pr * 100):.2f}%" if pr is not None else "当前预计利润率：—（需计算）")
        lines.append(f"售价筛选比例：商品售价 ≥ 单抽价×{cfg.price_ratio * 100:.0f}%")
        lines.append(f"保底规则：{'累计 ' + str(cfg.X) + ' 抽必出【传说】' if cfg.pity_on and cfg.X else '未开启'}")
        lines.append(f"通知人：{getattr(cfg, 'notify_person', '当前操作人')}")
        lines.append("\n等级概率与成本区间：")
        for lvl in LEVELS:
            r = cfg.cost_ranges[lvl]
            lines.append(f"- {LEVEL_NAME[lvl]}：概率 {cfg.p_k[lvl] * 100:.2f}% ｜ 成本 {r.lo}~{r.hi}")

        lines.append("\n选品概览：")
        lines.append(f"- 已选商品总数：{len(selected)}")
        lines.append(f"- 其中系统筛选：{sys_count}，手动添加：{manual_count}")
        lvl_line = " / ".join([f"{LEVEL_NAME[k]}: {level_counts.get(k,0)}" for k in LEVELS])
        lines.append(f"- 等级分布：{lvl_line}")

        lines.append("\n自动行为：")
        lines.append("1) 达到下架时间自动下架")
        lines.append("2) 折扣结束商品将自动从已选池剔除")
        lines.append("3) 每日提醒按「提醒与通知设置」的提醒时间/对象弹窗")
        return "\n".join(lines)

    # ---------- lifecycle ----------
    def prune_expired_from_selection(self, bag: BagState):
        now = dt.datetime.now()
        for it in self.catalog:
            if it.discount_end and it.discount_end <= now:
                it.discount_status = "无活动"
        # 折扣活动结束：仅对系统筛选商品自动剔除，手动添加不受影响
        for it_id in list(bag.selected_ids):
            if it_id in bag.manual_added_ids:
                continue
            it = next((x for x in self.catalog if x.id == it_id), None)
            if it and it.discount_end and it.discount_end <= now:
                bag.selected_ids.remove(it_id)

    def _tick_minutely(self):
        try:
            self._auto_down_by_time()
            # 列表不需要每分钟刷新 UI；只做数据层维护
            for bag in self.bags.values():
                self.prune_expired_from_selection(bag)
            self._maybe_show_reminder()
        finally:
            self.after(60_000, self._tick_minutely)

    def _auto_down_by_time(self):
        now = dt.datetime.now()
        for bag in self.bags.values():
            if bag.cfg and bag.status == "已上线":
                if now >= bag.cfg.down_time:
                    bag.status = "已下架"

    def _items_expiring_by_tomorrow_23(self) -> List[Item]:
        now = dt.datetime.now()
        tomorrow = (now + dt.timedelta(days=1)).date()
        deadline = dt.datetime.combine(tomorrow, dt.time(23, 0))
        out = [it for it in self.catalog if it.discount_end and now < it.discount_end <= deadline]
        out.sort(key=lambda x: x.discount_end or dt.datetime.max)
        return out

    def _maybe_show_reminder(self):
        if not self.settings.get('reminder_enabled', True):
            return
        now = dt.datetime.now()
        try:
            hh, mm = map(int, str(self.settings.get('reminder_time', '16:00')).split(":"))
        except Exception:
            hh, mm = 16, 0
        if now.hour != hh or now.minute != mm:
            return
        today = now.date()
        if self._remind_snooze_date == today:
            return
        if self._remind_last_shown_date == today:
            return
        items = self._items_expiring_by_tomorrow_23()
        self._remind_last_shown_date = today
        if items:
            ReminderDialog(self, self, items[:10])

    def reset_downstream(self, bag: BagState, keep_config: bool = True):
        bag.filtered = []
        bag.selected_ids = set()
        bag.manual_added_ids = set()
        bag.calc_ok = False
        bag.adapt_triggered = False
        bag.adapt_success = False
        bag.final_probs = {}
        bag.config_confirmed = False
        if not keep_config:
            bag.cfg = None

# -------------------------------
# 弹窗：手动添加商品（真添加 + 成本区间校验）
# -------------------------------
class ManualAddDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, app: App, bag: BagState):
        super().__init__(parent)
        self.app = app
        self.bag = bag
        self.title("手动添加商品")
        self.geometry("760x460")
        self.resizable(False, False)

        ttk.Label(self, text="手动添加商品（真实添加：加入当前福袋已选池）", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=12, pady=(12, 6))

        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")

        self.v_search = tk.StringVar(value="")
        ttk.Label(top, text="游戏名称：").grid(row=0, column=0, sticky="w")
        ent = ttk.Entry(top, textvariable=self.v_search, width=28)
        ent.grid(row=0, column=1, sticky="w")
        ent.bind("<KeyRelease>", lambda e: self.refresh())

        ttk.Label(top, text="分配等级：").grid(row=0, column=2, sticky="e", padx=(16, 6))
        self.v_level = tk.StringVar(value="普通(C)")
        cb = ttk.Combobox(top, textvariable=self.v_level, values=[f"{LEVEL_NAME[l]}({l})" for l in LEVELS], width=12, state="readonly")
        cb.grid(row=0, column=3, sticky="w")
        cb.current(4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_cost_check())

        # 把「确认添加」放在分配等级旁，方便点选后立即操作
        self.btn_confirm = ttk.Button(top, text="确认添加到当前福袋", command=self.confirm)
        self.btn_confirm.grid(row=0, column=4, sticky="w", padx=(12, 0))

        ttk.Label(top, text="库存报警值：").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.v_alarm = tk.StringVar(value=str(DEFAULT_ALARM))
        ttk.Entry(top, textvariable=self.v_alarm, width=10).grid(row=1, column=1, sticky="w", pady=(10, 0))

        self.lbl_cost_check = ttk.Label(top, text="成本校验：—", foreground="#9ca3af")
        self.lbl_cost_check.grid(row=1, column=2, columnspan=2, sticky="w", padx=(16, 0), pady=(10, 0))

        ttk.Label(top, text="（可按 Ctrl/Shift 多选后点击旁边“确认添加”）", foreground="#6b7280").grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))

        self.tree = ttk.Treeview(self, columns=("id", "name", "price", "cost", "stock", "status"), show="headings", height=14, selectmode="extended")
        for c, t, w in [
            ("id", "ID", 70),
            ("name", "游戏名", 320),
            ("price", "售价", 90),
            ("cost", "成本", 90),
            ("stock", "库存", 80),
            ("status", "折扣状态", 110),
        ]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=("w" if c == "name" else "center"))
        self.tree.pack(fill="both", expand=True, padx=12, pady=(6, 8))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._refresh_cost_check())

        btns = ttk.Frame(self, padding=12)
        btns.pack(fill="x")
        ttk.Label(btns, text="选择商品和等级后，点击上方“确认添加”即可。", foreground="#6b7280").pack(side="left")
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=8)

        self.refresh()

    def _parse_level(self) -> str:
        s = self.v_level.get()
        if "(" in s and ")" in s:
            return s.split("(")[-1].split(")")[0]
        return "C"

    def refresh(self):
        kw = self.v_search.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        for it in self.app.catalog:
            if kw and kw not in it.name.lower() and kw not in f"{it.id}".lower():
                continue
            self.tree.insert("", "end", iid=str(it.id), values=(it.id, it.name, f"{it.price:.2f}", f"{it.cost:.2f}", str(it.stock), it.discount_status))
        self._refresh_cost_check()

    def _refresh_cost_check(self):
        sel = self.tree.selection()
        if not sel or not self.bag.cfg:
            self.lbl_cost_check.configure(text="成本校验：—", foreground="#9ca3af")
            return

        lvl = self._parse_level()
        r = self.bag.cfg.cost_ranges[lvl]

        results = []
        for iid in sel:
            it = next((x for x in self.app.catalog if x.id == int(iid)), None)
            if it:
                ok = (r.lo <= it.cost <= r.hi)
                results.append(ok)

        if not results:
            self.lbl_cost_check.configure(text="成本校验：—", foreground="#9ca3af")
            return

        ok_cnt = sum(1 for x in results if x)
        total = len(results)
        if total == 1:
            it_id = int(sel[0])
            it = next((x for x in self.app.catalog if x.id == it_id), None)
            msg = f"成本校验：{it.cost:.2f} 元，{LEVEL_NAME[lvl]}范围 {r.lo}~{r.hi}：{'是' if results[0] else '否（允许添加）'}"
        else:
            msg = f"成本校验：已选 {total} 个，符合 {ok_cnt}，不符合 {total - ok_cnt}（允许添加）"

        self.lbl_cost_check.configure(text=msg, foreground=("#1f6f3c" if ok_cnt == total else "#8a1c1c"))

    def confirm(self):
        if not self.bag.cfg:
            messagebox.showwarning("提示", "请先在配置页保存配置后再手动添加。")
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择一个或多个商品。")
            return
        try:
            alarm = int(float(self.v_alarm.get().strip()))
            if alarm <= 0:
                raise ValueError
        except Exception:
            messagebox.showerror("输入错误", "库存报警值请输入正整数。")
            return

        lvl = self._parse_level()
        r = self.bag.cfg.cost_ranges[lvl]
        added, off_range = [], []

        for iid in sel:
            it = next((x for x in self.app.catalog if x.id == int(iid)), None)
            if not it:
                continue
            it.level = lvl
            it.alarm = alarm

            self.bag.selected_ids.add(it.id)
            self.bag.manual_added_ids.add(it.id)

            if r.lo <= it.cost <= r.hi:
                added.append(it.name)
            else:
                off_range.append(it.name)

        if not added and not off_range:
            messagebox.showerror("异常", "未能添加所选商品，请重试。")
            return

        total = len(added) + len(off_range)
        lines = [f"成功添加：共 {total} 个 → {LEVEL_NAME[lvl]}"]
        if off_range:
            lines.append(f"其中 {len(off_range)} 个成本不在 {LEVEL_NAME[lvl]} 范围内，已允许添加（请注意风险）。")
        messagebox.showinfo("已添加", "\n".join(lines))
        self.bag.config_confirmed = False
        self.destroy()

# -------------------------------
# 提醒弹窗
# -------------------------------
class ReminderDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, app: App, items: List[Item]):
        super().__init__(parent)
        self.app = app
        self.title("福袋商品下架提醒")
        self.geometry("820x460")
        self.resizable(False, False)

        ttk.Label(self, text="【福袋商品下架提醒】", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=12, pady=(12, 6))

        txt = tk.Text(self, height=14, wrap="word")
        txt.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        lines = ["以下商品折扣活动将于明日23:00前结束，届时将自动从福袋中剔除：\n"]
        for i, it in enumerate(items, 1):
            end = it.discount_end.strftime("%m-%d %H:%M") if it.discount_end else "-"
            lines.append(f"{i}. {it.name}（库存：{it.stock}，成本：{it.cost:.0f}元，结束：{end}）")
        txt.insert("1.0", "\n".join(lines))
        txt.configure(state="disabled")

        bottom = ttk.Frame(self, padding=12)
        bottom.pack(fill="x")

        self.v_snooze = tk.BooleanVar(value=False)
        ttk.Checkbutton(bottom, text="今日不再提醒", variable=self.v_snooze).pack(side="left")

        ttk.Button(bottom, text="关闭", command=self.close).pack(side="right")

    def close(self):
        if self.v_snooze.get():
            self.app._remind_snooze_date = dt.datetime.now().date()
        self.destroy()

# -------------------------------
# Page 1：福袋列表（首页）
# -------------------------------

class ReminderSettingsDialog(tk.Toplevel):
    """提醒/通知设置（交互占位：不一定真实发送通知）"""
    def __init__(self, parent: tk.Tk, app: 'App'):
        super().__init__(parent)
        self.app = app
        self.title("提醒与通知设置")
        self.geometry("620x520")
        self.resizable(False, False)

        ttk.Label(self, text="提醒设置（展示型功能，不触发真实通知）", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=14, pady=(12, 8))

        box = ttk.Frame(self, padding=14)
        box.pack(fill="both", expand=True)

        self.v_remind = tk.BooleanVar(value=bool(app.settings.get('reminder_enabled', True)))
        self.v_notify = tk.BooleanVar(value=bool(app.settings.get('notify_enabled', True)))
        self.v_force = tk.BooleanVar(value=bool(app.settings.get('notify_supervisor_on_force', True)))
        self.v_person = tk.StringVar(value=str(app.settings.get('reminder_person', "")))
        self.v_phone = tk.StringVar(value=str(app.settings.get('reminder_phone', "")))
        self.v_time = tk.StringVar(value=str(app.settings.get('reminder_time', "16:00")))
        self.v_start_day = tk.StringVar(value=str(app.settings.get('reminder_start_day', '今天')))
        self.v_end_day = tk.StringVar(value=str(app.settings.get('reminder_end_day', '明天')))
        self.v_start_time = tk.StringVar(value=str(app.settings.get('reminder_start_time', '00:00')))
        self.v_end_time = tk.StringVar(value=str(app.settings.get('reminder_end_time', '23:59')))

        ttk.Checkbutton(box, text="开启每日提醒弹窗（默认 16:00）", variable=self.v_remind).pack(anchor="w", pady=4)
        ttk.Checkbutton(box, text="开启通知（占位：可对接企业微信/邮件）", variable=self.v_notify).pack(anchor="w", pady=4)
        ttk.Checkbutton(box, text="强制上线时通知上级（占位交互）", variable=self.v_force).pack(anchor="w", pady=4)

        ttk.Label(box, text="统计时间范围（昨天/今天/明天 + 时间），仅用于展示：", foreground="#374151").pack(anchor="w", pady=(10, 4))
        self._build_range_picker(box)

        ttk.Label(box, text="统计对象说明：用于统计该时间范围内【折扣即将到期】或【库存为 0】的商品。", foreground="#6b7280", wraplength=560, justify="left").pack(anchor="w", pady=(8, 4))
        ttk.Label(box, text="结束时间需晚于开始时间；不合法时将提示“统计时间范围不合法”。", foreground="#6b7280").pack(anchor="w")

        frm = ttk.Frame(box)
        frm.pack(fill="x", pady=(12, 2))
        ttk.Label(frm, text="提醒人：", width=12).pack(side="left")
        ttk.Entry(frm, textvariable=self.v_person, width=20).pack(side="left")
        ttk.Label(frm, text="手机号：", width=8).pack(side="left")
        ttk.Entry(frm, textvariable=self.v_phone, width=16).pack(side="left")

        frm2 = ttk.Frame(box)
        frm2.pack(fill="x", pady=(2, 8))
        ttk.Label(frm2, text="提醒时间：", width=12).pack(side="left")
        ttk.Entry(frm2, textvariable=self.v_time, width=10).pack(side="left")
        ttk.Label(frm2, text="(HH:MM，例如 16:00)", foreground="#6b7280").pack(side="left", padx=(6, 0))
        ttk.Label(box, text="系统将在设定时间点生成提醒（当前仅用于配置展示）", foreground="#6b7280").pack(anchor="w", pady=(2, 8))

        sup = ttk.Frame(box)
        sup.pack(fill="x", pady=(4, 0))
        ttk.Label(sup, text="强制上线通知对象：", width=18).pack(side="left")
        sup_name = str(app.settings.get('supervisor_name', '上级运营'))
        sup_phone = str(app.settings.get('supervisor_phone', ''))
        ttk.Label(sup, text=f"{sup_name}（{sup_phone}）", foreground="#374151").pack(side="left")

        ttk.Label(box, text="提醒设置当前仅用于功能展示，不会触发真实通知；不影响统计数据、上线/下线或选品逻辑。", foreground="#6b7280", wraplength=560, justify="left").pack(anchor="w", pady=(10, 0))

        btns = ttk.Frame(self, padding=12)
        btns.pack(fill="x")
        ttk.Button(btns, text="保存", command=self.save).pack(side="left")
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="left", padx=8)

    def _build_range_picker(self, parent: ttk.Frame):
        days = ["昨天", "今天", "明天"]

        def row(title: str, v_day: tk.StringVar, v_time: tk.StringVar):
            r = ttk.Frame(parent)
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=title, width=10).pack(side="left")
            for d in days:
                ttk.Radiobutton(r, text=d, value=d, variable=v_day).pack(side="left", padx=(0, 6))
            ttk.Label(r, text="时间：").pack(side="left", padx=(10, 4))
            ttk.Entry(r, textvariable=v_time, width=8).pack(side="left")
            ttk.Label(r, text="(HH:MM)", foreground="#6b7280").pack(side="left")

        row("开始时间", self.v_start_day, self.v_start_time)
        row("结束时间", self.v_end_day, self.v_end_time)

    def _parse_day_time(self, day_flag: str, hhmm: str) -> dt.datetime:
        today = dt.datetime.now().date()
        base = today
        if day_flag == "昨天":
            base = today - dt.timedelta(days=1)
        elif day_flag == "明天":
            base = today + dt.timedelta(days=1)

        hhmm = hhmm.strip()
        if not hhmm:
            raise ValueError("时间不能为空，格式 HH:MM")
        try:
            h, m = hhmm.split(":")
            hour = int(h)
            minute = int(m)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except Exception:
            raise ValueError("时间格式错误，应为 HH:MM")

        return dt.datetime.combine(base, dt.time(hour, minute))

    def save(self):
        try:
            start = self._parse_day_time(self.v_start_day.get(), self.v_start_time.get())
            end = self._parse_day_time(self.v_end_day.get(), self.v_end_time.get())
            if end <= start:
                raise ValueError("统计时间范围不合法：结束时间需晚于开始时间")
        except Exception as e:
            messagebox.showerror("统计时间范围不合法", str(e))
            return

        self.app.settings['reminder_enabled'] = bool(self.v_remind.get())
        self.app.settings['notify_enabled'] = bool(self.v_notify.get())
        self.app.settings['notify_supervisor_on_force'] = bool(self.v_force.get())
        self.app.settings['reminder_person'] = self.v_person.get().strip() or "默认运营"
        self.app.settings['reminder_phone'] = self.v_phone.get().strip()
        self.app.settings['reminder_time'] = self.v_time.get().strip() or "16:00"
        self.app.settings['reminder_start_day'] = self.v_start_day.get()
        self.app.settings['reminder_end_day'] = self.v_end_day.get()
        self.app.settings['reminder_start_time'] = self.v_start_time.get().strip()
        self.app.settings['reminder_end_time'] = self.v_end_time.get().strip()
        self.app.settings['reminder_range'] = f"{start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}"
        messagebox.showinfo("已保存", "提醒设置已保存（当前仅用于配置展示，不会触发真实通知）。")
        self.destroy()

class ConfigPopup(tk.Toplevel):
    """参数配置页弹窗（只读），内含上线/强制上线操作。"""
    def __init__(self, parent: tk.Tk, app: App, bag: BagState, on_state_change=None):
        super().__init__(parent)
        self.app = app
        self.bag = bag
        self.on_state_change = on_state_change
        self.title("参数配置（只读）")
        self.geometry("960x760")
        self.resizable(True, True)

        ttk.Button(self, text="关闭", command=self.destroy).place(relx=1.0, y=8, x=-10, anchor="ne")

        wrapper = ttk.Frame(self, padding=12)
        wrapper.pack(fill="both", expand=True)
        wrapper.columnconfigure(0, weight=1)
        wrapper.columnconfigure(1, weight=1)

        cfg = bag.cfg
        selected = [it for it in app.catalog if it.id in bag.selected_ids]
        probs = bag.final_probs or {}
        pr = app._profit_rate(cfg, selected, probs) if probs else None
        P_eff = calc_p_eff(cfg.P, cfg.d, cfg.q)

        # 基础配置区域（类似参数配置页）
        base = ttk.Labelframe(wrapper, text="基础配置", padding=10)
        base.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))
        info = [
            ("福袋ID", bag.bag_id),
            ("福袋名称", cfg.bag_name),
            ("上下架时间", f"{fmt_dt(cfg.up_time)} ~ {fmt_dt(cfg.down_time)}"),
            ("单抽标价 P", f"{cfg.P:.2f} 元"),
            ("目标利润率 g", f"{cfg.g*100:.2f}%"),
            ("十连折扣系数 d", f"{cfg.d:.2f}"),
            ("十连占比 q", f"{cfg.q*100:.0f}%"),
            ("售价筛选比例", f"{cfg.price_ratio*100:.0f}%"),
            ("等效单抽收入 P_eff", f"{P_eff:.2f} 元"),
            ("当前预计利润率", f"{pr*100:.2f}%" if pr is not None else "—（需计算）"),
            ("保底规则", f"累计 {cfg.X} 抽必出【传说】" if cfg.pity_on and cfg.X else "未开启"),
            ("通知人", getattr(cfg, "notify_person", "当前操作人")),
        ]
        for i, (k, v) in enumerate(info):
            ttk.Label(base, text=f"{k}：", width=18).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Label(base, text=v).grid(row=i, column=1, sticky="w", pady=2)

        # 等级概率与成本区间（表格形式）
        lvl_frame = ttk.Labelframe(wrapper, text="等级概率与成本区间", padding=10)
        lvl_frame.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(0, 8))
        tree = ttk.Treeview(lvl_frame, columns=("prob", "range"), show="headings", height=8)
        tree.heading("prob", text="概率")
        tree.heading("range", text="成本区间")
        tree.column("prob", width=160, anchor="center")
        tree.column("range", width=200, anchor="center")
        for lvl in LEVELS:
            r = cfg.cost_ranges[lvl]
            tree.insert("", "end", values=(f"{LEVEL_NAME[lvl]}：{cfg.p_k[lvl]*100:.2f}%", f"{r.lo} ~ {r.hi}"))
        tree.pack(fill="both", expand=True)

        # 选品概览
        summary = ttk.Labelframe(wrapper, text="选品概览", padding=10)
        summary.grid(row=1, column=0, columnspan=2, sticky="nsew")
        summary.columnconfigure(0, weight=1)
        summary.columnconfigure(1, weight=1)
        sys_ids = {it.id for it in bag.filtered}
        manual = bag.manual_added_ids
        sys_count = len(set(bag.selected_ids) & sys_ids)
        manual_count = len(set(bag.selected_ids) & manual)
        level_counts: Dict[str, int] = {k: 0 for k in LEVELS}
        for it in selected:
            level_counts[it.level] = level_counts.get(it.level, 0) + 1
        ttk.Label(summary, text=f"已选商品：{len(selected)}（系统筛选 {sys_count} / 手动添加 {manual_count}）").grid(row=0, column=0, sticky="w", pady=2, padx=(0,8))
        lvl_line = " / ".join([f"{LEVEL_NAME[k]}: {level_counts.get(k,0)}" for k in LEVELS])
        ttk.Label(summary, text=f"等级分布：{lvl_line}").grid(row=0, column=1, sticky="w", pady=2)

        media = ttk.Frame(summary)
        media.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6,0))
        ttk.Label(media, text=f"封面图：{bag.cover_path or '未上传'}").pack(side="left", padx=(0,12))
        ttk.Label(media, text=f"背景图：{bag.bg_path or '未上传'}").pack(side="left", padx=(0,12))
        ttk.Label(media, text=f"广告图：{bag.ad_path or '未上传'}").pack(side="left", padx=(0,12))
        ttk.Label(media, text=f"备注：{bag.remark or '—'}").pack(side="left", padx=(0,12))

        # 按等级商品明细（只读）
        detail = ttk.Labelframe(wrapper, text="按等级查看已选商品（只读，仅用于核对）", padding=10)
        detail.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        detail.columnconfigure(0, weight=1)
        wrapper.rowconfigure(2, weight=1)

        tab = ttk.Frame(detail)
        tab.pack(anchor="w", pady=(0, 6))
        self.level_var = tk.StringVar(value="L")
        for lvl in LEVELS:
            ttk.Radiobutton(tab, text=LEVEL_NAME[lvl], value=lvl, variable=self.level_var, command=self._refresh_level_view).pack(side="left", padx=(0,6))

        cols = ("name", "level", "stock", "cost", "prob", "status")
        self.tree_detail = ttk.Treeview(detail, columns=cols, show="headings", height=7)
        headers = [
            ("name", "游戏名", 320, "w"),
            ("level", "等级", 80, "center"),
            ("stock", "库存", 80, "center"),
            ("cost", "成本", 90, "e"),
            ("prob", "最终概率", 110, "e"),
            ("status", "状态提示", 160, "center"),
        ]
        for c, t, w, a in headers:
            self.tree_detail.heading(c, text=t)
            self.tree_detail.column(c, width=w, anchor=a)
        self.tree_detail.pack(fill="both", expand=True)

        # 动作按钮
        btns = ttk.Frame(self, padding=12)
        btns.pack(fill="x")
        self.note = ttk.Label(btns, text="", foreground="#6b7280")
        self.note.pack(side="left")
        self.btn_online = ttk.Button(btns, text="确认上线", command=self._online)
        self.btn_online.pack(side="right")
        self.btn_force = ttk.Button(btns, text="强制上线", command=self._force_online)
        self.btn_force.pack(side="right", padx=8)

        self._refresh_level_view()
        self._update_action_state()

    def _refresh_level_view(self):
        if not hasattr(self, "tree_detail"):
            return
        bag = self.bag
        cfg = bag.cfg
        selected = [it for it in self.app.catalog if it.id in bag.selected_ids]
        probs = bag.final_probs or {}
        lvl = self.level_var.get() if hasattr(self, "level_var") else "L"
        now = dt.datetime.now()
        self.tree_detail.delete(*self.tree_detail.get_children())

        rows = []
        for it in selected:
            if it.level != lvl:
                continue
            p = probs.get(it.id, 0.0)
            stock_txt = str(it.stock)
            status = ""
            if it.stock < max(1, it.alarm):
                status = "库存低"
            if it.discount_end:
                if it.discount_end <= now:
                    status = (status + " / " if status else "") + "折扣已结束"
                elif (it.discount_end - now).total_seconds() <= 24*3600:
                    status = (status + " / " if status else "") + "折扣即将结束"
            rows.append((it.name, LEVEL_BADGE[it.level], stock_txt, f"{it.cost:.2f}", f"{p*100:.4f}%" if p>0 else "-", status or "正常"))

        rows.sort(key=lambda x: (-float(x[4].rstrip('%')) if x[4].endswith('%') else 0.0, x[0]))
        for row in rows[:120]:
            self.tree_detail.insert("", "end", values=row)

        if not rows:
            self.tree_detail.insert("", "end", values=("当前等级无商品", "-", "-", "-", "-", "-"))

    def _update_action_state(self):
        bag = self.bag
        cfg = bag.cfg
        selected = [it for it in self.app.catalog if it.id in bag.selected_ids]
        probs = bag.final_probs or {}
        profit_val = self.app._profit_value(cfg, selected, probs)

        super_admin = bool(self.app.settings.get('is_super_admin', False))

        if not probs or not selected or profit_val is None:
            self.btn_online.configure(state="disabled")
            self.btn_force.configure(state="disabled")
            self.note.configure(text="缺少最终概率或选品结果，需先计算。")
            return

        if profit_val >= -1e-12:
            self.btn_online.configure(state="normal")
            self.btn_force.configure(state="disabled")
            self.note.configure(text="预测利润为非负：可直接上线，无需强制上线。")
        else:
            self.btn_online.configure(state="disabled")
            if super_admin:
                self.btn_force.configure(state="normal")
                self.note.configure(text="预测利润为负：仅超级管理员可强制上线并记录审批。")
            else:
                self.btn_force.configure(state="disabled")
                self.note.configure(text="预测利润为负：普通运营不可上线，请联系超管处理。")

    def _online(self):
        bag = self.bag
        cfg = bag.cfg
        selected = [it for it in self.app.catalog if it.id in bag.selected_ids]
        probs = bag.final_probs or {}
        profit_val = self.app._profit_value(cfg, selected, probs)
        if profit_val is None:
            messagebox.showwarning("不允许上线", "缺少最终概率或选品结果，无法上线。")
            return
        if profit_val < -1e-12:
            messagebox.showwarning("不允许上线", "预测利润为负，需超级管理员执行强制上线。")
            return
        bag.status = "已上线"
        messagebox.showinfo("上线成功", "上线成功！已返回福袋列表。")
        if self.on_state_change:
            self.on_state_change()
        self.destroy()

    def _force_online(self):
        bag = self.bag
        super_admin = bool(self.app.settings.get('is_super_admin', False))
        if not super_admin:
            messagebox.showwarning("权限不足", "仅超级管理员可在预测利润为负时强制上线。")
            return
        if not bag.final_probs or not bag.selected_ids:
            messagebox.showwarning("需要先计算", "请先完成计算并生成最终概率。")
            return
        profit_val = self.app._profit_value(bag.cfg, [it for it in self.app.catalog if it.id in bag.selected_ids], bag.final_probs)
        if profit_val is None:
            messagebox.showwarning("需要先计算", "缺少预测利润，无法强制上线。")
            return
        if profit_val >= -1e-12:
            messagebox.showinfo("建议直接上线", "预测利润为非负，可直接上线，无需强制流程。")
            return
        sup_name = str(self.app.settings.get('supervisor_name', '上级运营'))
        sup_phone = str(self.app.settings.get('supervisor_phone', ''))
        messagebox.showinfo("已通知上级", f"已发起强制上线申请，将通知 {sup_name}（{sup_phone}）审批（占位交互）。")
        bag.status = "待审批"
        if self.on_state_change:
            self.on_state_change()
        self.destroy()

class PageBagList(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app

        ttk.Label(self, text="福袋列表（首页）", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", pady=(0, 10))

        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 8))
        ttk.Button(top, text="新建福袋", command=self.new_bag).pack(side="left")
        ttk.Button(top, text="提醒设置", command=self.open_reminder_settings).pack(side="left", padx=8)
        ttk.Button(top, text="设置月度福袋", command=self.open_monthly).pack(side="left", padx=8)

        # 筛选区
        filt = ttk.Frame(self)
        filt.pack(fill="x", pady=(6, 6))
        ttk.Label(filt, text="搜索：").pack(side="left", padx=(0, 4))
        self.v_search = tk.StringVar(value="")
        ent = ttk.Entry(filt, textvariable=self.v_search, width=22, justify="left")
        ent.pack(side="left")
        self.app.apply_placeholder(ent, self.v_search, "搜索 ID/名称")

        ttk.Label(filt, text="福袋类型：").pack(side="left", padx=(12, 4))
        self.v_type = tk.StringVar(value="请选择")
        cb_type = ttk.Combobox(filt, textvariable=self.v_type, values=["请选择", "原福袋", "其他类型"], width=10, state="readonly")
        cb_type.pack(side="left")

        ttk.Label(filt, text="开启状态：").pack(side="left", padx=(12, 4))
        self.v_status = tk.StringVar(value="请选择")
        cb_status = ttk.Combobox(filt, textvariable=self.v_status, values=["请选择", "开启", "关闭"], width=10, state="readonly")
        cb_status.pack(side="left")

        ttk.Label(filt, text="是否推荐：").pack(side="left", padx=(12, 4))
        self.v_rec = tk.StringVar(value="请选择")
        cb_rec = ttk.Combobox(filt, textvariable=self.v_rec, values=["请选择", "是", "否"], width=8, state="readonly")
        cb_rec.pack(side="left")

        ttk.Label(filt, text="统计时间：").pack(side="left", padx=(12, 4))
        self._start_placeholder = "开始时间（YYYY-MM-DD HH:MM）"
        self._end_placeholder = "结束时间（YYYY-MM-DD HH:MM）"
        self.v_start = tk.StringVar(value="")
        self.v_end = tk.StringVar(value="")
        ent_start = ttk.Entry(filt, textvariable=self.v_start, width=18, justify="left")
        ent_start.pack(side="left")
        self.app.apply_placeholder(ent_start, self.v_start, self._start_placeholder)
        ttk.Button(filt, text="选择", command=lambda: open_datetime_picker(self, self.v_start)).pack(side="left", padx=(2, 6))
        ttk.Label(filt, text="~").pack(side="left")
        ent_end = ttk.Entry(filt, textvariable=self.v_end, width=18, justify="left")
        ent_end.pack(side="left")
        self.app.apply_placeholder(ent_end, self.v_end, self._end_placeholder)
        ttk.Button(filt, text="选择", command=lambda: open_datetime_picker(self, self.v_end)).pack(side="left", padx=(2, 6))

        ttk.Button(filt, text="搜索", command=self._on_search).pack(side="left", padx=(12, 0))

        ttk.Label(self, text="统计时间用于计算成交额、订单数、成本、利润、利润率。未选择时使用默认区间。", foreground="#6b7280").pack(anchor="w", pady=(2, 6))

        self.tree = ttk.Treeview(self, columns=("id","name","type","P","rev","orders","cost","profit","prate","status","count","ops"), show="headings", height=18)
        for c, t, w, a in [
            ("id", "福袋ID", 90, "center"),
            ("name", "福袋名称", 200, "w"),
            ("type", "福袋类型", 90, "center"),
            ("P", "单抽标价(元)", 110, "e"),
            ("rev", "成交额", 100, "e"),
            ("orders", "订单数", 80, "center"),
            ("cost", "成本", 100, "e"),
            ("profit", "利润", 100, "e"),
            ("prate", "利润率", 90, "center"),
            ("status", "状态", 80, "center"),
            ("count", "商品个数", 90, "center"),
            ("ops", "管理操作", 230, "center"),
        ]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a)
        self.tree.pack(fill="both", expand=True, pady=(0, 10))
        self.tree.bind("<Button-1>", self.on_click)

        btns = ttk.Frame(self)
        btns.pack(fill="x")
        ttk.Label(btns, text="首页仅作为入口与数据总览（统计时间默认按产品定义）。", foreground="#6b7280").pack(anchor="w")

        self._view_mode = False

    def open_reminder_settings(self):
        ReminderSettingsDialog(self, self.app)

    def open_monthly(self):
        self.app.show("PageMonthlyBag")

    def on_show(self):
        self.refresh()

    def _match(self, bag: BagState) -> bool:
        kw = _safe_kw(self.v_search.get(), "搜索 ID/名称").lower()
        if kw:
            name = (bag.cfg.bag_name if bag.cfg else "") or ""
            if kw not in name.lower() and kw not in bag.bag_id.lower():
                return False

        type_v = self.v_type.get()
        if type_v != "请选择" and (bag.bag_type or "原福袋") != type_v:
            return False

        status_v = self.v_status.get()
        if status_v != "请选择":
            is_open = (bag.status == "已上线")
            if status_v == "开启" and not is_open:
                return False
            if status_v == "关闭" and is_open:
                return False

        rec_v = self.v_rec.get()
        if rec_v != "请选择":
            want = (rec_v == "是")
            if bag.recommended != want:
                return False
        return True

    def _validate_time_range(self) -> bool:
        start_raw = _safe_kw(self.v_start.get(), self._start_placeholder)
        end_raw = _safe_kw(self.v_end.get(), self._end_placeholder)
        if not start_raw or not end_raw:
            messagebox.showwarning("缺少统计时间", "请先选择统计开始和结束时间。")
            return False
        try:
            start_dt = parse_dt(start_raw, "开始时间")
            end_dt = parse_dt(end_raw, "结束时间")
        except Exception as e:
            messagebox.showerror("时间格式错误", str(e))
            return False
        if end_dt <= start_dt:
            messagebox.showerror("时间范围错误", "结束时间必须晚于开始时间。")
            return False
        self._last_time_range = (start_dt, end_dt)
        return True

    def _on_search(self):
        if not self._validate_time_range():
            return
        self.refresh()

    def refresh(self):
        self.app._auto_down_by_time()
        self.tree.delete(*self.tree.get_children())
        bags = sorted(self.app.bags.items(), key=lambda x: (0 if x[1].recommended else 1, x[0]))
        for bag_id, bag in bags:
            if not self._match(bag):
                continue
            name = bag.cfg.bag_name if bag.cfg else "（未配置）"
            btype = bag.bag_type or "原福袋"
            price = f"{bag.cfg.P:.2f}" if bag.cfg else "—"

            revenue = bag.revenue or 0.0
            orders = bag.orders or 0
            cost = bag.total_cost or 0.0
            profit_val = revenue - cost
            prate = (profit_val / revenue * 100.0) if revenue > 1e-9 else None

            status = "开启" if bag.status == "已上线" else "关闭"
            count = len(bag.selected_ids) if bag.selected_ids else 0
            ops_txt = "修改｜" + ("取消推荐" if bag.recommended else "推荐") + "｜详情｜" + ("下线" if bag.status == "已上线" else "上线") + "｜删除"

            self.tree.insert("", "end", iid=bag_id, values=(
                bag_id,
                name,
                btype,
                price,
                f"{revenue:.2f}",
                str(orders),
                f"{cost:.2f}",
                f"{profit_val:.2f}",
                (f"{prate:.2f}%" if prate is not None else "—"),
                status,
                str(count),
                ops_txt,
            ))

    def new_bag(self):
        bag_id = self.app._next_bag_id()
        bag = BagState(bag_id=bag_id, status="未上线")
        self.app.bags[bag_id] = bag
        self.app.current_bag_id = bag_id
        self.app.frames["PageConfig"].set_readonly(False)
        self.app.show("PageConfig")

    def on_click(self, event):
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if not row or col != "#12":
            return
        bbox = self.tree.bbox(row, column=col)
        if not bbox:
            return
        x_rel = event.x - bbox[0]
        width = max(bbox[2], 1)
        section = x_rel / width
        if section < 0.20:
            self._edit_bag(row)
        elif section < 0.40:
            self._toggle_recommend(row)
        elif section < 0.60:
            self._show_detail(row)
        elif section < 0.80:
            self._toggle_status(row)
        else:
            self._delete_bag(row)

    def _edit_bag(self, bag_id: str):
        self.app.current_bag_id = bag_id
        self.app.frames["PageConfig"].set_readonly(False)
        self.app.show("PageConfig")

    def _toggle_recommend(self, bag_id: str):
        bag = self.app.bags.get(bag_id)
        if not bag:
            return
        bag.recommended = not bag.recommended
        self.refresh()

    def _toggle_status(self, bag_id: str):
        bag = self.app.bags.get(bag_id)
        if not bag:
            return
        if not bag.cfg:
            messagebox.showwarning("缺少配置", "请先完成参数配置后再上线或下线。")
            return
        current = bag.status
        if current == "已上线":
            bag.status = "已下架"
            messagebox.showinfo("已下线", "已手动下线该福袋，立即生效，与时间配置无关。")
        else:
            if not bag.final_probs or not bag.selected_ids:
                messagebox.showwarning("禁止上线", "缺少最终概率或选品结果，请先完成选品与计算。")
                return
            profit_val = self.app._profit_value(bag.cfg, [it for it in self.app.catalog if it.id in bag.selected_ids], bag.final_probs)
            if profit_val is None:
                messagebox.showwarning("禁止上线", "缺少预测利润，无法判断上线风险。")
                return
            if profit_val < -1e-12:
                if not self.app.settings.get('is_super_admin', False):
                    messagebox.showwarning("需超管操作", "预测利润为负，普通运营不可上线，请联系超级管理员强制上线。")
                    return
                if self.app.settings.get('notify_supervisor_on_force', True) and self.app.settings.get('notify_enabled', True):
                    messagebox.showinfo("已通知上级", "预测利润为负，已记录强制上线并通知上级审批（占位交互）。")
                else:
                    messagebox.showinfo("已记录", "预测利润为负，已记录强制上线申请（占位交互）。")
                bag.status = "待审批"
            else:
                bag.status = "已上线"
                messagebox.showinfo("已上线", "已手动上线该福袋，立即生效，与时间配置无关。")
        self.refresh()

    def _show_detail(self, bag_id: str):
        bag = self.app.bags.get(bag_id)
        if not bag:
            return
        win = tk.Toplevel(self)
        win.title(f"{bag_id} 详情")
        win.geometry("420x280")
        win.resizable(False, False)
        rev = bag.revenue or 0.0
        orders = bag.orders or 0
        cost = bag.total_cost or 0.0
        profit_val = rev - cost
        prate = (profit_val / rev * 100.0) if rev > 1e-9 else None

        lines = [
            f"福袋ID：{bag.bag_id}",
            f"福袋名称：{bag.cfg.bag_name if bag.cfg else '（未配置）'}",
            f"福袋类型：{bag.bag_type or '原福袋'}",
            f"成交额：{rev:.2f}",
            f"订单数：{orders}",
            f"成本：{cost:.2f}",
            f"利润：{profit_val:.2f}",
            f"利润率：{prate:.2f}%" if prate is not None else "利润率：—",
            f"状态：{'开启' if bag.status == '已上线' else '关闭'}",
            f"商品个数：{len(bag.selected_ids) if bag.selected_ids else 0}",
        ]
        lbl = tk.Text(win, wrap="word", height=10)
        lbl.pack(fill="both", expand=True, padx=12, pady=12)
        lbl.insert("1.0", "\n".join(lines))
        lbl.configure(state="disabled")
        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=(0, 10))


    def _delete_bag(self, bag_id: str):
        bag = self.app.bags.get(bag_id)
        if not bag:
            return
        if not messagebox.askyesno("确认删除", f"确认删除 {bag_id} 吗？删除后不可恢复。"):
            return
        if self.app.current_bag_id == bag_id:
            self.app.current_bag_id = None
        del self.app.bags[bag_id]
        self.refresh()


class PageMonthlyBag(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app

        ttk.Label(self, text="月度福袋设置", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", pady=(0, 10))
        ttk.Button(self, text="返回列表", command=lambda: self.app.show("PageBagList")).place(relx=1.0, y=6, x=-10, anchor="ne")

        container = ttk.Frame(self)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.columnconfigure(1, weight=1)

        # vars
        self.v_name = tk.StringVar(value="")
        self.v_P = tk.StringVar(value="")
        self.v_d = tk.StringVar(value="0.95")
        self.v_up = tk.StringVar(value="")
        self.v_down = tk.StringVar(value="")
        self.v_cover = tk.StringVar(value="")
        self.v_bg = tk.StringVar(value="")
        self.v_ad = tk.StringVar(value="")
        self.v_notify = tk.StringVar(value="当前操作人")
        self.v_remark = tk.StringVar(value="")

        left = ttk.Frame(container)
        right = ttk.Frame(container)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        self._build_basic(left)
        self._build_media(right)

        preview = ttk.Labelframe(container, text="等级商品预览区（导入后展示）", padding=8)
        preview.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        container.rowconfigure(1, weight=1)

        tab_bar = ttk.Frame(preview)
        tab_bar.pack(anchor="w", pady=(0, 6))
        self.preview_level = tk.StringVar(value="L")
        for lvl in LEVELS:
            ttk.Radiobutton(tab_bar, text=LEVEL_NAME[lvl], value=lvl, variable=self.preview_level,
                            command=self.refresh_preview).pack(side="left", padx=(0, 8))

        cols = ("id", "name", "level", "stock", "price", "cost", "steam", "disc", "src")
        self.tree_preview = ttk.Treeview(preview, columns=cols, show="headings", height=10)
        for c, t, w, a in [
            ("id", "ID", 80, "center"),
            ("name", "游戏名", 200, "w"),
            ("level", "等级", 80, "center"),
            ("stock", "库存", 80, "center"),
            ("price", "售价", 90, "e"),
            ("cost", "成本", 90, "e"),
            ("steam", "Steam史低", 100, "e"),
            ("disc", "折扣状态", 120, "center"),
            ("src", "来源", 90, "center"),
        ]:
            self.tree_preview.heading(c, text=t)
            self.tree_preview.column(c, width=w, anchor=a)
        self.tree_preview.pack(fill="both", expand=True)

        self.lbl_hint = ttk.Label(preview, text="请先导入。", foreground="#6b7280")
        self.lbl_hint.pack(anchor="w", pady=(4, 0))

        actions = ttk.Frame(self, padding=8)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="导入", command=self._import_file).pack(side="left")
        self.btn_online = ttk.Button(actions, text="上线", command=self._online)
        self.btn_online.pack(side="left", padx=8)
        self.lbl_status = ttk.Label(actions, text="", foreground="#6b7280")
        self.lbl_status.pack(side="left", padx=8)

    def on_show(self):
        self._load_state()
        self.refresh_preview()

    def _build_basic(self, parent):
        box = ttk.Labelframe(parent, text="基础设置", padding=8)
        box.pack(fill="x", pady=(0, 10))

        def row(label, var, hint="", with_picker=False):
            r = ttk.Frame(box)
            r.pack(fill="x", pady=4)
            ttk.Label(r, text=label, width=14).pack(side="left")
            ent = ttk.Entry(r, textvariable=var, width=20)
            ent.pack(side="left")
            if with_picker:
                ttk.Button(r, text="选择时间", command=lambda v=var: open_datetime_picker(self, v)).pack(side="left", padx=4)
            if hint:
                ttk.Label(r, text=hint, foreground="#9ca3af").pack(side="left", padx=8)

        row("福袋名称", self.v_name)
        row("单抽标价 P", self.v_P)
        row("十连折扣系数 d", self.v_d, "如 0.97=97折")
        row("上架时间", self.v_up, with_picker=True)
        row("下架时间", self.v_down, "需晚于上架", with_picker=True)

        media_box = ttk.Labelframe(parent, text="素材与通知", padding=8)
        media_box.pack(fill="x")
        size_hint = {"封面图": "尺寸：290×261", "背景图": "尺寸：290×220"}
        for label, var in [("封面图", self.v_cover), ("背景图", self.v_bg), ("广告图", self.v_ad)]:
            r = ttk.Frame(media_box)
            r.pack(fill="x", pady=3)
            ttk.Label(r, text=f"{label}：", width=10).pack(side="left")
            ttk.Entry(r, textvariable=var).pack(side="left", fill="x", expand=True)
            ttk.Button(r, text="上传", command=lambda v=var: self._upload_file(v)).pack(side="left", padx=6)
            if label in size_hint:
                ttk.Label(r, text=size_hint[label], foreground="#6b7280").pack(side="left", padx=6)

        row_notify = ttk.Frame(media_box)
        row_notify.pack(fill="x", pady=3)
        ttk.Label(row_notify, text="通知人：", width=10).pack(side="left")
        ttk.Entry(row_notify, textvariable=self.v_notify, width=22).pack(side="left")

        row_remark = ttk.Frame(media_box)
        row_remark.pack(fill="x", pady=3)
        ttk.Label(row_remark, text="备注：", width=10).pack(side="left")
        ttk.Entry(row_remark, textvariable=self.v_remark).pack(side="left", fill="x", expand=True)

    def _build_media(self, parent):
        box = ttk.Labelframe(parent, text="关键配置（概要）", padding=8)
        box.pack(fill="x")
        ttk.Label(box, text="在此页面仅做基础设置与导入，详细概率/成本规则沿用主流程。", foreground="#6b7280").pack(anchor="w")

    def _assign_level_default(self, cost: float) -> str:
        for lvl in LEVELS:
            lo, hi = DEFAULT_RANGE[lvl]
            if lo <= cost <= hi:
                return lvl
        return "C"

    def _load_state(self):
        state = self.app.monthly_bag
        self.v_name.set(state.bag_name or "")
        self.v_P.set(f"{state.P}" if state.P else "")
        self.v_d.set(f"{state.d:.2f}" if state.d else "")
        self.v_up.set(fmt_dt(state.up_time) if state.up_time else "")
        self.v_down.set(fmt_dt(state.down_time) if state.down_time else "")
        self.v_cover.set(state.cover_path or "")
        self.v_bg.set(state.bg_path or "")
        self.v_ad.set(state.ad_path or "")
        self.v_notify.set(state.notify_person or "当前操作人")
        self.v_remark.set(state.remark or "")
        self.lbl_status.configure(text=f"当前状态：{state.status}")

    def _save_state(self) -> MonthlyBagState:
        name = self.v_name.get().strip()
        if not name:
            raise ValueError("福袋名称不能为空")
        P = float(self.v_P.get())
        if P <= 0:
            raise ValueError("单抽标价需大于0")
        d = float(self.v_d.get())
        if not (0.80 <= d <= 1.00):
            raise ValueError("十连折扣系数需在0.80~1.00之间")
        up = parse_dt(self.v_up.get(), "上架时间")
        down = parse_dt(self.v_down.get(), "下架时间")
        if down <= up:
            raise ValueError("下架时间必须晚于上架时间")

        state = self.app.monthly_bag
        state.bag_name = name
        state.P = P
        state.d = d
        state.up_time = up
        state.down_time = down
        state.cover_path = self.v_cover.get().strip()
        state.bg_path = self.v_bg.get().strip()
        state.ad_path = self.v_ad.get().strip()
        state.notify_person = self.v_notify.get().strip() or "当前操作人"
        state.remark = self.v_remark.get().strip()
        return state

    def _import_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("Excel 文件", "*.xlsx"), ("JSON 文件", "*.json"), ("CSV 文件", "*.csv"), ("所有文件", "*.*")]
        )
        if not path:
            return
        try:
            items: List[Item] = []
            payload = []
            lower = path.lower()
            if lower.endswith(".json"):
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, dict):
                    payload = payload.get("items", [])
            elif lower.endswith(".xlsx"):
                try:
                    import openpyxl  # type: ignore
                except Exception:
                    messagebox.showerror("导入失败", "请先安装 openpyxl 以支持 Excel 导入（pip install openpyxl）。")
                    return
                wb = openpyxl.load_workbook(path, data_only=True)
                ws = wb.active
                rows_iter = list(ws.iter_rows(values_only=True))
                if not rows_iter:
                    payload = []
                else:
                    header = [str(c or "").strip().lower() for c in rows_iter[0]]
                    data_rows = rows_iter[1:]
                    for row in data_rows:
                        if not any(row):
                            continue
                        rec: Dict[str, object] = {}
                        for key, cell in zip(header, row):
                            if key:
                                rec[key] = cell
                        payload.append(rec)
            else:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        parts = [p.strip() for p in line.split(",")]
                        if len(parts) < 5:
                            continue
                        payload.append({
                            "id": parts[0],
                            "name": parts[1],
                            "price": parts[2],
                            "cost": parts[3],
                            "stock": parts[4],
                        })

            now = dt.datetime.now()
            for row in payload or []:
                try:
                    it = Item(
                        id=int(row.get("id", 0)),
                        name=str(row.get("name", "")) or f"导入商品{len(items)+1}",
                        price=float(row.get("price", 0.0)),
                        cost=float(row.get("cost", 0.0)),
                        stock=int(row.get("stock", 0)),
                        steam_lowest=float(row.get("steam_lowest", row.get("steam_low", row.get("steam_lowest_price", 0.0)))),
                        discount_status=str(row.get("discount_status", "无活动")),
                        discount_end=None,
                    )
                    lvl = row.get("level") or self._assign_level_default(it.cost)
                    it.level = lvl if lvl in LEVELS else self._assign_level_default(it.cost)
                    disc_end_raw = row.get("discount_end") or row.get("discount_end_at")
                    if disc_end_raw:
                        try:
                            it.discount_end = dt.datetime.fromisoformat(str(disc_end_raw))
                        except Exception:
                            it.discount_end = None
                    if it.discount_end and it.discount_end <= now:
                        it.discount_status = "无活动"
                    setattr(it, "source", row.get("source", "import"))
                    items.append(it)
                except Exception:
                    continue

            if not items:
                messagebox.showwarning("导入失败", "文件中未解析到有效商品。")
                return

            state = self.app.monthly_bag
            state.items = items
            self.refresh_preview()
            messagebox.showinfo("导入成功", f"成功导入 {len(items)} 个商品。")
        except Exception as e:
            messagebox.showerror("导入失败", str(e))

    def refresh_preview(self):
        self.tree_preview.delete(*self.tree_preview.get_children())
        items = self.app.monthly_bag.items or []
        if not items:
            self.lbl_hint.configure(text="请先导入。")
            return
        lvl = self.preview_level.get()
        rows = [it for it in items if it.level == lvl]
        if not rows:
            self.lbl_hint.configure(text=f"{LEVEL_NAME.get(lvl, lvl)} 暂无导入商品。")
            return
        now = dt.datetime.now()
        rows.sort(key=lambda x: x.id)
        for it in rows:
            disc = it.discount_status or "无活动"
            if it.discount_end and it.discount_end > now:
                remain = it.discount_end - now
                if remain.total_seconds() <= 24 * 3600:
                    disc = f"即将结束({int(remain.total_seconds()//3600)}h)"
            src = getattr(it, "source", "import")
            self.tree_preview.insert("", "end", values=(it.id, it.name, LEVEL_BADGE[it.level], it.stock,
                                                          f"{it.price:.2f}", f"{it.cost:.2f}", f"{it.steam_lowest:.2f}", disc, src))
        self.lbl_hint.configure(text=f"当前等级展示 {len(rows)} 条。")

    def _online(self):
        try:
            state = self._save_state()
        except Exception as e:
            messagebox.showerror("输入错误", str(e))
            return
        if not state.items:
            messagebox.showwarning("无法上线", "请先导入商品后再上线。")
            return
        state.status = "已上线"
        messagebox.showinfo("上线成功", "月度福袋已上线，返回首页。")
        self.app.show("PageBagList")

    def _upload_file(self, var: tk.StringVar):
        path = filedialog.askopenfilename()
        if not path:
            return
        var.set(path)


# -------------------------------
# Page 2：参数配置页（支持只读模式）
# -------------------------------
class PageConfig(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app
        self._readonly = False

        header = ttk.Frame(self)
        header.pack(fill="x", pady=(0, 8))
        self.lbl_title = ttk.Label(header, text="参数配置", font=("Microsoft YaHei UI", 16, "bold"))
        self.lbl_title.pack(side="left")
        self.lbl_mode = ttk.Label(header, text="", foreground="#9ca3af")
        self.lbl_mode.pack(side="left", padx=10)

        ttk.Label(self, text="提示：配置页不展示已选商品，商品请在「选品页/结果页」查看。", foreground="#9ca3af").pack(anchor="w", pady=(0, 8))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))

        self.btn_save_top = ttk.Button(self, text="保存并返回列表", command=self.on_save)
        self.btn_save_top.place(relx=1.0, y=10, x=-10, anchor="ne")
        self.btn_back_top = ttk.Button(self, text="返回列表", command=lambda: self.app.show("PageBagList"))
        self.btn_back_top.place(relx=1.0, y=40, x=-10, anchor="ne")

        # vars
        self.v_name = tk.StringVar(value="")
        self.v_P = tk.StringVar(value="99")
        self.v_g = tk.StringVar(value="30")
        self.v_d = tk.StringVar(value="0.95")
        self.v_q = tk.StringVar(value="60")
        self.v_ratio = tk.StringVar(value="80")
        self.v_type = tk.StringVar(value="原福袋")
        self.v_cover = tk.StringVar(value="")
        self.v_bg = tk.StringVar(value="")
        self.v_ad = tk.StringVar(value="")
        self.v_remark = tk.StringVar(value="")
        self.v_notify_person = tk.StringVar(value="当前操作人")
        self.v_my_cost = tk.StringVar(value="—")
        self.v_pred_profit = tk.StringVar(value="—")

        now = dt.datetime.now()
        self.v_up_date = tk.StringVar(value=f"{(now + dt.timedelta(hours=1)):%Y-%m-%d %H:%M}")
        self.v_down_date = tk.StringVar(value=f"{(now + dt.timedelta(days=7)):%Y-%m-%d %H:%M}")

        self.v_pity_on = tk.BooleanVar(value=True)
        self.v_X = tk.StringVar(value="100")

        self.v_p = {k: tk.StringVar(value=str(DEFAULT_PK[k])) for k in LEVELS}
        self.v_lo = {k: tk.StringVar(value=str(DEFAULT_RANGE[k][0])) for k in LEVELS}
        self.v_hi = {k: tk.StringVar(value=str(DEFAULT_RANGE[k][1])) for k in LEVELS}
        self.v_exp = {k: tk.StringVar(value="—") for k in LEVELS}

        self._build_basic(left)
        self._build_levels(right)

        preview_box = ttk.Labelframe(body, text="等级商品预览区（可查看已配置商品）", padding=10)
        preview_box.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        body.rowconfigure(1, weight=1)

        tabs = ttk.Frame(preview_box)
        tabs.pack(anchor="w", pady=(0, 6))
        self.preview_level = tk.StringVar(value="L")
        for lvl in LEVELS:
            ttk.Radiobutton(tabs, text=LEVEL_NAME[lvl], value=lvl, variable=self.preview_level, command=self.refresh_preview).pack(side="left", padx=(0, 8))

        cols_prev = ("name", "stock", "price", "steam_lowest", "cost", "discount", "prob", "src")
        self.preview_tree = ttk.Treeview(preview_box, columns=cols_prev, show="headings", height=7)
        self.preview_headings = {
            "name": "游戏名",
            "stock": "库存",
            "price": "售价",
            "steam_lowest": "Steam史低",
            "cost": "成本(元)",
            "discount": "折扣状态",
            "prob": "最终概率(如有)",
            "src": "来源",
        }
        for c, w, a, cmd in [
            ("name", 260, "w", None),
            ("stock", 80, "center", None),
            ("price", 90, "e", lambda col="price": self._sort_preview(col)),
            ("steam_lowest", 90, "e", None),
            ("cost", 90, "e", lambda col="cost": self._sort_preview(col)),
            ("discount", 110, "center", lambda col="discount": self._sort_preview(col)),
            ("prob", 120, "e", None),
            ("src", 90, "center", None),
        ]:
            label = self.preview_headings[c]
            if cmd:
                self.preview_tree.heading(c, text=label, command=cmd)
            else:
                self.preview_tree.heading(c, text=label)
            self.preview_tree.column(c, width=w, anchor=a)
        self.preview_sort = ("discount", True)
        self.preview_tree.pack(fill="both", expand=True)

        self.preview_hint = ttk.Label(preview_box, text="提示：这里展示当前福袋已配置/已选择商品。若需要调整，请进入「商品选品页」。", foreground="#6b7280")
        self.preview_hint.pack(anchor="w", pady=(6, 0))

        self.btns = ttk.Frame(body)
        self.btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=12)

        self.btn_filter = ttk.Button(self.btns, text="筛选商品（去选品）", command=self.on_filter)
        self.btn_filter.pack(side="right", padx=8)

        for var in [self.v_P, self.v_g, self.v_d, self.v_q]:
            var.trace_add("write", lambda *_: self.refresh_expected_cost())
        for k in LEVELS:
            self.v_p[k].trace_add("write", lambda *_: self.refresh_expected_cost())

    def set_readonly(self, ro: bool):
        self._readonly = bool(ro)

    def on_show(self):
        bag = self.app.ensure_current()
        self.lbl_title.configure(text=f"参数配置｜{bag.bag_id}")
        self.lbl_mode.configure(text=("只读模式" if self._readonly else "编辑模式"))

        # load existing cfg if any
        if bag.cfg:
            cfg = bag.cfg
            self.v_name.set(cfg.bag_name)
            self.v_P.set(str(cfg.P))
            self.v_g.set(str(int(round(cfg.g * 100))))
            self.v_d.set(f"{cfg.d:.2f}")
            self.v_q.set(str(int(round(cfg.q * 100))))
            self.v_ratio.set(str(int(round(cfg.price_ratio * 100))))
            self.v_type.set(bag.bag_type or "原福袋")
            self.v_up_date.set(cfg.up_time.strftime("%Y-%m-%d %H:%M"))
            self.v_down_date.set(cfg.down_time.strftime("%Y-%m-%d %H:%M"))
            self.v_pity_on.set(bool(cfg.pity_on))
            self.v_notify_person.set(getattr(cfg, "notify_person", "当前操作人") or "当前操作人")
            self.v_X.set(str(cfg.X or ""))
            for k in LEVELS:
                self.v_p[k].set(str(int(round(cfg.p_k[k] * 100))))
                self.v_lo[k].set(str(cfg.cost_ranges[k].lo))
                self.v_hi[k].set(str(cfg.cost_ranges[k].hi))
            self.v_cover.set(bag.cover_path)
            self.v_bg.set(bag.bg_path)
            self.v_ad.set(bag.ad_path)
            self.v_remark.set(bag.remark)
        else:
            if not self.v_name.get():
                self.v_name.set(f"{dt.datetime.now():%Y%m%d} 福袋")
            self.v_type.set(bag.bag_type or "原福袋")
            self.v_cover.set(bag.cover_path)
            self.v_bg.set(bag.bg_path)
            self.v_ad.set(bag.ad_path)
            self.v_remark.set(bag.remark)

        self._apply_readonly_state()
        self.refresh_expected_cost()
        self.refresh_pred_profit()
        self.refresh_preview()
        # 手工上下线控制已迁移到首页管理操作

    def _apply_readonly_state(self):
        # entries state
        state = "disabled" if self._readonly else "normal"

        for w in self._basic_entries:
            w.configure(state=state)
        for w in self._range_entries:
            w.configure(state=state)

        # pity controls
        self.cb_pity.configure(state=state)
        self.ent_X.configure(state=("disabled" if (self._readonly or not self.v_pity_on.get()) else "normal"))

        # buttons
        self.btn_save_top.configure(state=("disabled" if self._readonly else "normal"))
        self.btn_back_top.configure(state="normal")
        self.btn_filter.configure(state=("disabled" if self._readonly else "normal"))
        self._refresh_manual_controls()

    def _refresh_manual_controls(self):
        # 旧版遗留：当前页面已无手动控制区，避免空调用报错
        return

    def _build_basic(self, parent):
        self._basic_entries = []
        base = ttk.Labelframe(parent, text="基础配置", padding=8)
        base.pack(fill="x", pady=(0, 8))

        def row(container, label, var, hint="", extra=None):
            r = ttk.Frame(container)
            r.pack(fill="x", pady=4)
            ttk.Label(r, text=label, width=18).pack(side="left")
            ent = ttk.Entry(r, textvariable=var, width=18)
            ent.pack(side="left")
            self._basic_entries.append(ent)
            if extra:
                extra(r)
            if hint:
                ttk.Label(r, text=hint, foreground="#9ca3af").pack(side="left", padx=8)

        row(base, "福袋名称（≤30字）", self.v_name)
        row(base, "福袋类型", self.v_type)
        row(base, "单抽标价 P（元）", self.v_P)
        row(base, "十连折扣系数 d", self.v_d, "如 0.97=97折")

        ttk.Frame(base, height=4).pack(fill="x")
        ttk.Label(base, text="预测利润率（只读）", foreground="#111827").pack(anchor="w")
        ttk.Label(base, textvariable=self.v_pred_profit, foreground="#111827").pack(anchor="w", pady=(2, 0))

        # 上下架时间：单行输入框 + 日历
        def date_row(label, var, hint=""):
            r = ttk.Frame(base)
            r.pack(fill="x", pady=4)
            ttk.Label(r, text=label, width=18).pack(side="left")
            ent = ttk.Entry(r, textvariable=var, width=18)
            ent.pack(side="left")
            self._basic_entries.append(ent)
            ttk.Button(r, text="选择时间", command=lambda v=var: open_datetime_picker(self, v)).pack(side="left", padx=4)
            if hint:
                ttk.Label(r, text=hint, foreground="#9ca3af").pack(side="left", padx=8)

        date_row("上架时间", self.v_up_date, "")
        date_row("下架时间", self.v_down_date, "需晚于上架")

        media = ttk.Labelframe(parent, text="素材与分销", padding=8)
        media.pack(fill="x", pady=(8, 0))
        size_hint = {"封面图": "尺寸：290×261", "背景图": "尺寸：290×220"}
        for label, var in [("封面图", self.v_cover), ("背景图", self.v_bg), ("广告图", self.v_ad)]:
            row = ttk.Frame(media)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=f"{label}：", width=10).pack(side="left")
            ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)
            ttk.Button(row, text="上传", command=lambda v=var: self._upload_file(v)).pack(side="left", padx=6)
            if label in size_hint:
                ttk.Label(row, text=size_hint[label], foreground="#6b7280").pack(side="left", padx=6)

        row_notify = ttk.Frame(media)
        row_notify.pack(fill="x", pady=3)
        ttk.Label(row_notify, text="通知人：", width=10).pack(side="left")
        cb_notify = ttk.Combobox(row_notify, textvariable=self.v_notify_person,
                                 values=["当前操作人", "运营A", "运营B", "运营C"], state="readonly", width=14)
        cb_notify.pack(side="left")

        row_remark = ttk.Frame(media)
        row_remark.pack(fill="x", pady=3)
        ttk.Label(row_remark, text="备注：", width=10).pack(side="left")
        ttk.Entry(row_remark, textvariable=self.v_remark).pack(side="left", fill="x", expand=True)

    def _toggle_x(self):
        self.ent_X.configure(state=("normal" if (self.v_pity_on.get() and not self._readonly) else "disabled"))

    def _upload_file(self, var: tk.StringVar):
        path = filedialog.askopenfilename(title="选择文件")
        if path:
            var.set(path)

    def _build_levels(self, parent):
        self._range_entries = []
        box = ttk.Labelframe(parent, text="关键配置", padding=8)
        box.pack(fill="both", expand=True)

        top = ttk.Frame(box)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="十连抽占比 q（%）", width=18).pack(side="left")
        ent_q = ttk.Entry(top, textvariable=self.v_q, width=10)
        ent_q.pack(side="left")
        self._range_entries.append(ent_q)
        ttk.Label(top, text="售价筛选比例（%）", width=16).pack(side="left", padx=(16, 4))
        ent_ratio = ttk.Entry(top, textvariable=self.v_ratio, width=10)
        ent_ratio.pack(side="left")
        self._range_entries.append(ent_ratio)

        ttk.Label(box, text="目标利润率 g（%）", width=18).pack(anchor="w", pady=(6, 2))
        ent_g = ttk.Entry(box, textvariable=self.v_g, width=12)
        ent_g.pack(anchor="w")
        self._range_entries.append(ent_g)

        ttk.Label(box, text="等级与成本范围（数值区间）", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(10, 4))

        header = ttk.Frame(box)
        header.pack(fill="x", pady=(0, 4))
        ttk.Label(header, text="等级", width=10).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="概率(%)", width=10).grid(row=0, column=1, sticky="w")
        ttk.Label(header, text="预期成本", width=10).grid(row=0, column=2, sticky="w")
        ttk.Label(header, text="下限", width=8).grid(row=0, column=3, sticky="w")
        ttk.Label(header, text="~", width=2).grid(row=0, column=4, sticky="w")
        ttk.Label(header, text="上限", width=8).grid(row=0, column=5, sticky="w")

        for lvl in LEVELS:
            r = ttk.Frame(box)
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=LEVEL_NAME[lvl], width=10).grid(row=0, column=0, sticky="w")
            e_p = ttk.Entry(r, textvariable=self.v_p[lvl], width=8)
            e_p.grid(row=0, column=1, sticky="w")
            self._range_entries.append(e_p)

            ttk.Label(r, textvariable=self.v_exp[lvl], width=10).grid(row=0, column=2, sticky="w")

            e_lo = ttk.Entry(r, textvariable=self.v_lo[lvl], width=6)
            e_hi = ttk.Entry(r, textvariable=self.v_hi[lvl], width=6)
            e_lo.grid(row=0, column=3, sticky="w")
            ttk.Label(r, text="~").grid(row=0, column=4, sticky="w")
            e_hi.grid(row=0, column=5, sticky="w")
            self._range_entries.extend([e_lo, e_hi])

        ttk.Label(box, text="库存筛选：库存需大于0；低于报警值在选品页提示。", foreground="#6b7280").pack(anchor="w", pady=(6,0))

        ttk.Label(box, text="保底设置", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(10, 4))
        self.cb_pity = ttk.Checkbutton(box, text="开启：累计 X 抽必出传说", variable=self.v_pity_on, command=self._toggle_x)
        self.cb_pity.pack(anchor="w")
        rx = ttk.Frame(box)
        rx.pack(fill="x", pady=4)
        ttk.Label(rx, text="保底阈值 X", width=18).pack(side="left")
        self.ent_X = ttk.Entry(rx, textvariable=self.v_X, width=18)
        self.ent_X.pack(side="left")
        self._range_entries.append(self.ent_X)

        ttk.Label(box, text="我的预期成本", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(10, 4))
        ttk.Label(box, textvariable=self.v_my_cost, foreground="#111827").pack(anchor="w")

        # 人工上下线控制已迁移到首页管理操作

    def _parse_float(self, s: str, name: str, lo=None, hi=None) -> float:
        v = float(str(s).strip())
        if lo is not None and v < lo:
            raise ValueError(f"{name} 不能小于 {lo}")
        if hi is not None and v > hi:
            raise ValueError(f"{name} 不能大于 {hi}")
        return v

    def _parse_int(self, s: str, name: str, lo=None, hi=None) -> int:
        v = int(float(str(s).strip()))
        if lo is not None and v < lo:
            raise ValueError(f"{name} 不能小于 {lo}")
        if hi is not None and v > hi:
            raise ValueError(f"{name} 不能大于 {hi}")
        return v


    def _build_config(self) -> Config:
        bag_name = self.v_name.get().strip()[:30]
        if not bag_name:
            raise ValueError("福袋名称不能为空")

        P = self._parse_float(self.v_P.get(), "单抽标价 P", lo=0.01)
        g = self._parse_float(self.v_g.get(), "目标利润率 g(%)", lo=0.0, hi=95.0) / 100.0
        d = self._parse_float(self.v_d.get(), "十连折扣系数 d", lo=0.80, hi=1.00)
        q = self._parse_float(self.v_q.get(), "十连抽占比 q(%)", lo=0.0, hi=100.0) / 100.0
        ratio = self._parse_float(self.v_ratio.get(), "售价筛选比例(%)", lo=0.0, hi=200.0) / 100.0

        up_time = parse_dt(self.v_up_date.get(), "上架时间")
        down_time = parse_dt(self.v_down_date.get(), "下架时间")
        if down_time <= up_time:
            raise ValueError("下架时间必须晚于上架时间")

        pk_percent = {k: self._parse_float(self.v_p[k].get(), f"{LEVEL_NAME[k]}概率(%)", lo=0.0, hi=100.0) for k in LEVELS}
        p_k = normalize_pk_percent(pk_percent)

        ranges = {}
        for k in LEVELS:
            lo = self._parse_int(self.v_lo[k].get(), f"{LEVEL_NAME[k]}成本下限", lo=0, hi=10_000_000)
            hi = self._parse_int(self.v_hi[k].get(), f"{LEVEL_NAME[k]}成本上限", lo=0, hi=10_000_000)
            if lo > hi:
                raise ValueError(f"{LEVEL_NAME[k]}成本范围不合法：下限不能大于上限")
            ranges[k] = LevelRange(lo=lo, hi=hi)

        pity_on = bool(self.v_pity_on.get())
        X = None
        if pity_on:
            X = self._parse_int(self.v_X.get(), "保底阈值 X", lo=1, hi=1_000_000)

        notify_person = self.v_notify_person.get().strip() or "当前操作人"

        return Config(bag_name, P, g, d, q, ratio, up_time, down_time, pity_on, X, p_k, ranges, notify_person)

    def refresh_expected_cost(self):
        try:
            cfg = self._build_config()
            mu = expected_cost_reference(cfg)
            for k in LEVELS:
                lo = int(self.v_lo[k].get() or 0)
                hi = int(self.v_hi[k].get() or 0)
                self.v_exp[k].set(f"{mu[k]:.0f}" if (lo <= mu[k] <= hi) else f"⚠ {mu[k]:.0f}")
            P_eff = calc_p_eff(cfg.P, cfg.d, cfg.q)
            c_target = calc_c_target(P_eff, cfg.g)
            self.v_my_cost.set(f"{c_target:.2f} 元（目标成本线）")
        except Exception:
            self.v_my_cost.set("—")
            return

    def refresh_pred_profit(self):
        try:
            bag = self.app.ensure_current()
            if not bag.cfg or not bag.final_probs or not bag.selected_ids:
                self.v_pred_profit.set("—")
                return
            cfg = bag.cfg
            selected = [it for it in self.app.catalog if it.id in bag.selected_ids]
            if not selected:
                self.v_pred_profit.set("—")
                return
            P_eff = calc_p_eff(cfg.P, cfg.d, cfg.q)
            if P_eff <= 0:
                self.v_pred_profit.set("—")
                return
            ec = expected_cost_total(cfg, selected, bag.final_probs)
            rate = (P_eff - ec) / P_eff
            self.v_pred_profit.set(f"{rate*100:.2f}%")
        except Exception:
            self.v_pred_profit.set("—")

    def refresh_preview(self):
        """在配置页展示当前福袋已配置/已选择商品（按等级过滤）。"""
        try:
            bag = self.app.ensure_current()
            self.preview_tree.delete(*self.preview_tree.get_children())
            lvl = self.preview_level.get() if hasattr(self, 'preview_level') else 'C'
            ids = set(bag.selected_ids) if bag else set()
            if not ids:
                self.preview_hint.configure(text="当前福袋还没有已选商品。你可以先去「筛选商品（去选品）」或「手动添加商品」。")
                return

            rows = []
            for it in self.app.catalog:
                if it.id not in ids:
                    continue
                if it.level != lvl:
                    continue
                p = bag.final_probs.get(it.id, 0.0) if bag.final_probs else 0.0
                src = ("手动添加" if it.id in bag.manual_added_ids else "系统筛选")
                disc = it.discount_status
                now = dt.datetime.now()
                if it.discount_end and it.discount_end <= now:
                    disc = "无活动"
                elif it.discount_end and it.discount_end > now:
                    remain = it.discount_end - now
                    if remain.total_seconds() <= 24 * 3600:
                        disc = f"即将结束（{int(remain.total_seconds()//3600)}h）"
                rows.append((it, p, src, disc))

            if not rows:
                self.preview_hint.configure(text=f"{LEVEL_NAME.get(lvl, lvl)} 等级暂无已选商品。")
                return

            sort_col, asc = self.preview_sort
            if sort_col == "discount":
                def disc_key(item):
                    it = item[0]
                    if it.discount_end and it.discount_end > dt.datetime.now():
                        return (0, it.discount_end)
                    return (1, dt.datetime.max)
                rows.sort(key=disc_key, reverse=not asc)
            elif sort_col in {"cost", "price"}:
                rows.sort(key=lambda item: getattr(item[0], sort_col, 0.0), reverse=not asc)

            arrow = "↑" if asc else "↓"
            for col, base in self.preview_headings.items():
                label = base + (f" {arrow}" if col == sort_col else "")
                cmd = (lambda c=col: self._sort_preview(c)) if col in {"discount", "cost", "price"} else None
                if cmd:
                    self.preview_tree.heading(col, text=label, command=cmd)
                else:
                    self.preview_tree.heading(col, text=label)

            for it, p, src, disc in rows[:80]:
                p_txt = f"{p*100:.4f}%" if p > 0 else "-"
                self.preview_tree.insert("", "end", values=(
                    it.name,
                    it.stock,
                    f"{it.price:.2f}",
                    f"{it.steam_lowest:.2f}",
                    f"{it.cost:.2f}",
                    disc,
                    p_txt,
                    src,
                ))
            self.preview_hint.configure(text=f"显示 {min(len(rows), 80)} 条（可在选品页调整；最终概率需计算后才有）。")
        except Exception:
            return

    def _sort_preview(self, col: str):
        cur_col, asc = self.preview_sort
        if cur_col == col:
            self.preview_sort = (col, not asc)
        else:
            self.preview_sort = (col, True)
        self.refresh_preview()

    def on_save(self):
        if self._readonly:
            self.app.show("PageBagList")
            return
        try:
            cfg = self._build_config()
        except Exception as e:
            messagebox.showerror("输入错误", str(e))
            return
        bag = self.app.ensure_current()
        bag.cfg = cfg
        bag.bag_type = self.v_type.get().strip() or "原福袋"
        bag.cover_path = self.v_cover.get().strip()
        bag.bg_path = self.v_bg.get().strip()
        bag.ad_path = self.v_ad.get().strip()
        bag.remark = self.v_remark.get().strip()
        messagebox.showinfo("已保存", "配置已保存，已返回福袋列表。")
        self.app.show("PageBagList")

    def on_manual_add(self):
        if self._readonly:
            return
        bag = self.app.ensure_current()
        if not bag.cfg:
            # 先保存一次配置（便于成本范围校验）
            try:
                bag.cfg = self._build_config()
            except Exception as e:
                messagebox.showerror("输入错误", str(e))
                return
        dlg = ManualAddDialog(self, self.app, bag)
        self.wait_window(dlg)
        self.refresh_preview()

    def on_filter(self):
        if self._readonly:
            return
        bag = self.app.ensure_current()
        try:
            cfg = self._build_config()
        except Exception as e:
            messagebox.showerror("输入错误", str(e))
            return

        bag.cfg = cfg
        bag.calc_ok = False
        bag.adapt_triggered = False
        bag.adapt_success = False
        bag.final_probs = {}
        bag.config_confirmed = False

        out = []
        for it in self.app.catalog:
            if hard_pass(cfg, it):
                apply_matching_level(cfg, it)
                out.append(it)
        bag.filtered = out
        # 保留手动添加
        bag.selected_ids = {it.id for it in out} | set(bag.manual_added_ids)

        if not out and not bag.manual_added_ids:
            messagebox.showerror("无可选商品", "筛选结果为空，请调整成本范围/售价比例/概率等。")
            return

        self.app.show("PagePick")

# -------------------------------
# Page 3：商品选品页
# -------------------------------
class PagePick(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app

        self.sel_page = 1
        self.sel_page_size = tk.StringVar(value="10")
        self.cand_page = 1
        self.cand_page_size = tk.StringVar(value="10")
        self.sel_sort_key = None
        self.sel_sort_desc = False
        self.cand_sort_key = None
        self.cand_sort_desc = False

        self.v_stock_threshold = tk.StringVar(value="")

        self.breadcrumb = ttk.Label(self, text="", foreground="#9ca3af")
        self.breadcrumb.pack(anchor="w")
        ttk.Button(self, text="返回参数配置页面", command=self.back_config_only).place(relx=1.0, y=5, x=-10, anchor="ne")

        ttk.Label(self, text="商品选品（系统筛选 + 手动添加）", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", pady=(0, 10))

        top = ttk.Frame(self)
        top.pack(fill="x", pady=6)

        self.level_vars = {k: tk.BooleanVar(value=True) for k in LEVELS}
        lvf = ttk.Frame(top)
        lvf.pack(side="left")
        ttk.Label(lvf, text="等级：").pack(side="left")
        for k in LEVELS:
            ttk.Checkbutton(lvf, text=LEVEL_NAME[k], variable=self.level_vars[k], command=self.refresh).pack(side="left", padx=(0, 6))

        ttk.Label(top, text="库存阈值≥").pack(side="left", padx=(16, 4))
        ent_stock = ttk.Entry(top, textvariable=self.v_stock_threshold, width=10, justify="left")
        ent_stock.pack(side="left")
        self.app.apply_placeholder(ent_stock, self.v_stock_threshold, "库存N")
        ent_stock.bind("<KeyRelease>", lambda e: self.refresh())

        ttk.Label(top, text="搜索：").pack(side="right", padx=(6, 4))
        self.v_search = tk.StringVar(value="")
        ent = ttk.Entry(top, textvariable=self.v_search, width=24, justify="left")
        ent.pack(side="right")
        self.app.apply_placeholder(ent, self.v_search, "搜索 ID/名称")
        ent.bind("<KeyRelease>", lambda e: self.refresh())

        # 上层：已选择商品区（独立分页）
        selected_box = ttk.Labelframe(self, text="已选择商品", padding=8)
        selected_box.pack(fill="both", expand=True, pady=(6, 6))

        sel_cols = [
            ("checked", "选择", 60, "center"),
            ("id", "ID", 70, "center"),
            ("name", "游戏名", 240, "w"),
            ("level", "等级", 90, "center"),
            ("stock", "库存", 90, "center"),
            ("price", "售价", 90, "e"),
            ("steam_low", "Steam史低", 100, "e"),
            ("cost", "成本", 90, "e"),
            ("disc", "折扣活动状态", 150, "center"),
            ("src", "来源", 90, "center"),
        ]
        self.sel_heading_labels = {c: t for c, t, *_ in sel_cols}

        self.tree_selected = ttk.Treeview(selected_box, columns=[c for c, *_ in sel_cols], show="headings", height=7)
        for c, t, w, a in sel_cols:
            cmd = None
            if c in ("price", "cost", "disc"):
                cmd = lambda col=c: self._toggle_sort("sel", col)
            if cmd:
                self.tree_selected.heading(c, text=t, command=cmd)
            else:
                self.tree_selected.heading(c, text=t)
            self.tree_selected.column(c, width=w, anchor=a)
        self.tree_selected.pack(fill="both", expand=True)
        self.tree_selected.bind("<Button-1>", self.on_click_selected)

        sel_pg = ttk.Frame(selected_box, padding=(0, 6))
        sel_pg.pack(fill="x")
        self.sel_info = ttk.Label(sel_pg, text="", foreground="#6b7280")
        self.sel_info.pack(side="left")
        ttk.Label(sel_pg, text="每页：").pack(side="right")
        cb_sel_size = ttk.Combobox(sel_pg, textvariable=self.sel_page_size, values=["10", "20", "50"], width=4, state="readonly")
        cb_sel_size.pack(side="right", padx=(0, 8))
        cb_sel_size.bind("<<ComboboxSelected>>", lambda e: self._change_sel_size())
        ttk.Button(sel_pg, text="下一页", command=lambda: self._turn_page("sel", 1)).pack(side="right", padx=4)
        ttk.Button(sel_pg, text="上一页", command=lambda: self._turn_page("sel", -1)).pack(side="right")

        # 下层：候选商品区（独立分页）
        cand_box = ttk.Labelframe(self, text="候选商品", padding=8)
        cand_box.pack(fill="both", expand=True, pady=(6, 6))

        cand_cols = [
            ("checked", "选择", 60, "center"),
            ("id", "ID", 70, "center"),
            ("name", "游戏名", 230, "w"),
            ("level", "等级", 90, "center"),
            ("stock", "库存", 90, "center"),
            ("price", "售价", 90, "e"),
            ("steam_low", "Steam史低", 100, "e"),
            ("cost", "成本", 90, "e"),
            ("alarm", "库存报警值", 100, "center"),
            ("disc", "折扣活动状态", 150, "center"),
            ("src", "来源", 90, "center"),
        ]
        self.cand_heading_labels = {c: t for c, t, *_ in cand_cols}

        self.tree = ttk.Treeview(cand_box, columns=[c for c, *_ in cand_cols], show="headings", height=10)
        for c, t, w, a in cand_cols:
            cmd = None
            if c in ("price", "cost", "disc"):
                cmd = lambda col=c: self._toggle_sort("cand", col)
            if cmd:
                self.tree.heading(c, text=t, command=cmd)
            else:
                self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a)
        self.tree.pack(fill="both", expand=True)

        self.tree.bind("<Button-1>", self.on_click_candidate)
        self.tree.bind("<Double-1>", self.on_double_click_alarm)

        cand_pg = ttk.Frame(cand_box, padding=(0, 6))
        cand_pg.pack(fill="x")
        self.cand_info = ttk.Label(cand_pg, text="", foreground="#6b7280")
        self.cand_info.pack(side="left")
        ttk.Label(cand_pg, text="每页：").pack(side="right")
        cb_cand_size = ttk.Combobox(cand_pg, textvariable=self.cand_page_size, values=["10", "20", "50"], width=4, state="readonly")
        cb_cand_size.pack(side="right", padx=(0, 8))
        cb_cand_size.bind("<<ComboboxSelected>>", lambda e: self._change_cand_size())
        ttk.Button(cand_pg, text="下一页", command=lambda: self._turn_page("cand", 1)).pack(side="right", padx=4)
        ttk.Button(cand_pg, text="上一页", command=lambda: self._turn_page("cand", -1)).pack(side="right")

        btns = ttk.Frame(self)
        btns.pack(fill="x", pady=8)
        ttk.Button(btns, text="全选", command=self.select_all).pack(side="left")
        ttk.Button(btns, text="取消全选", command=self.unselect_all).pack(side="left", padx=6)
        ttk.Button(btns, text="手动添加商品", command=self.manual_add).pack(side="left", padx=18)

        self.btn_calc = ttk.Button(btns, text="开始计算", command=self.start_calc)
        self.btn_calc.pack(side="right")

        self.status = ttk.Label(self, text="", foreground="#9ca3af")
        self.status.pack(anchor="w")

    def on_show(self):
        bag = self.app.ensure_current()
        name = bag.cfg.bag_name if bag.cfg else ""
        self.breadcrumb.configure(text=f"福袋列表 → {bag.bag_id} {name} → 商品选品")
        self.sel_page = 1
        self.cand_page = 1
        self.refresh()

    def _is_low_stock(self, it: Item) -> bool:
        return it.stock < max(1, it.alarm)

    def _stock_ok(self, it: Item) -> bool:
        try:
            threshold = int(float(self.v_stock_threshold.get().strip()))
            if threshold < 0:
                return True
            return it.stock >= threshold
        except Exception:
            return True

    def _is_expired_for_candidate(self, it: Item, now: dt.datetime) -> bool:
        """系统筛选商品折扣到期后不在候选区展示，手动添加保留。"""
        if it.discount_end and it.discount_end <= now and it.id not in getattr(self.app.ensure_current(), "manual_added_ids", set()):
            return True
        return False

    def _toggle_sort(self, which: str, key: str):
        if which == "sel":
            if self.sel_sort_key == key:
                self.sel_sort_desc = not self.sel_sort_desc
            else:
                self.sel_sort_key = key
                self.sel_sort_desc = False
        else:
            if self.cand_sort_key == key:
                self.cand_sort_desc = not self.cand_sort_desc
            else:
                self.cand_sort_key = key
                self.cand_sort_desc = False
        self.refresh()

    def _disc_text(self, it: Item, now: dt.datetime) -> str:
        disc = it.discount_status
        if it.discount_end and it.discount_end > now:
            remain = it.discount_end - now
            if remain.total_seconds() <= 24 * 3600:
                disc = f"即将结束（{int(remain.total_seconds() // 3600)}h）"
        if it.discount_end and it.discount_end <= now:
            disc = "无活动"
        return disc

    def _disc_sort_key(self, it: Item, now: dt.datetime):
        if it.discount_end and it.discount_end > now:
            return (0, it.discount_end)
        return (1, dt.datetime.max)

    def _apply_sort(self, which: str, items: List[Item], now: dt.datetime) -> List[Item]:
        key = self.sel_sort_key if which == "sel" else self.cand_sort_key
        desc = self.sel_sort_desc if which == "sel" else self.cand_sort_desc
        if not key:
            return items

        def _k(it: Item):
            if key == "price":
                return it.price
            if key == "cost":
                return it.cost
            if key == "disc":
                return self._disc_sort_key(it, now)
            return 0

        return sorted(items, key=_k, reverse=desc)

    def _refresh_sort_indicators(self):
        def _apply(tree, labels, key, desc, sortable):
            for col, base in labels.items():
                text = base
                if col in sortable:
                    if key == col:
                        text = f"{base} {'↓' if desc else '↑'}"
                    else:
                        text = f"{base} ⇅"
                tree.heading(col, text=text)

        _apply(self.tree_selected, self.sel_heading_labels, self.sel_sort_key, self.sel_sort_desc, {"price", "cost", "disc"})
        _apply(self.tree, self.cand_heading_labels, self.cand_sort_key, self.cand_sort_desc, {"price", "cost", "disc"})

    def _matches(self, it: Item) -> bool:
        if not self.level_vars.get(it.level, tk.BooleanVar(value=True)).get():
            return False
        kw = _safe_kw(self.v_search.get(), "搜索 ID/名称").lower()
        if kw and kw not in it.name.lower() and kw not in f"{it.id}".lower():
            return False
        if not self._stock_ok(it):
            return False
        return True

    def refresh(self):
        bag = self.app.ensure_current()
        if not bag.cfg:
            self.app.show("PageConfig")
            return

        self.app.prune_expired_from_selection(bag)
        now = dt.datetime.now()

        # 已选列表（受搜索/等级/库存筛选影响）
        selected_items = [it for it in self.app.catalog if it.id in bag.selected_ids and self._matches(it)]
        selected_items = self._apply_sort("sel", selected_items, now)
        sel_size = max(1, int(self.sel_page_size.get() or 10))
        sel_slice, sel_total, sel_pages, self.sel_page = self._paginate(selected_items, self.sel_page, sel_size)

        self.tree_selected.delete(*self.tree_selected.get_children())
        sel_rows = []
        for it in sel_slice:
            stock = f"{it.stock}" if self._is_low_stock(it) else str(it.stock)
            disc = self._disc_text(it, now)
            src = "手动添加" if it.id in bag.manual_added_ids else "系统筛选"
            sel_rows.append(("☑️", it.id, it.name, LEVEL_BADGE[it.level], stock, it.price, it.steam_lowest, it.cost, disc, src))

        for checked, it_id, name, lvl, stock, price, steam_low, cost, disc, src in sel_rows:
            self.tree_selected.insert("", "end", iid=str(it_id),
                                      values=(checked, it_id, name, lvl, stock, f"{price:.2f}", f"{steam_low:.2f}", f"{cost:.2f}", disc, src))

        self.sel_info.configure(text=f"共 {sel_total} 条，{sel_pages} 页，当前第 {self.sel_page} 页")

        # 候选列表（系统筛选 + 手动添加），系统筛选的过期商品不再展示，避免“全选后立即被剔除”
        shown = []
        for it in bag.filtered:
            if it.id in bag.selected_ids:
                continue
            if self._is_expired_for_candidate(it, now):
                continue
            if self._matches(it):
                shown.append(it)

        manual_items = [it for it in self.app.catalog if it.id in bag.manual_added_ids]
        for it in manual_items:
            if it.id in bag.selected_ids:
                continue
            if it.discount_end and it.discount_end <= now:
                # 手动添加允许保留，但不自动加入候选区；若需要重新勾选，可先延长折扣时间
                continue
            if self._matches(it) and it not in shown:
                shown.append(it)

        shown = self._apply_sort("cand", shown, now)
        cand_size = max(1, int(self.cand_page_size.get() or 10))
        cand_slice, cand_total, cand_pages, self.cand_page = self._paginate(shown, self.cand_page, cand_size)

        self.tree.delete(*self.tree.get_children())
        cand_rows = []
        for it in cand_slice:
            checked = "☑️" if it.id in bag.selected_ids else "☐"
            stock = f"{it.stock}" if self._is_low_stock(it) else str(it.stock)

            disc = self._disc_text(it, now)

            src = "手动添加" if it.id in bag.manual_added_ids else "系统筛选"

            cand_rows.append((checked, it.id, it.name, LEVEL_BADGE[it.level], stock, it.price, it.steam_lowest, it.cost, it.alarm, disc, src))

        for checked, it_id, name, lvl, stock, price, steam_low, cost, alarm, disc, src in cand_rows:
            self.tree.insert("", "end", iid=str(it_id),
                             values=(checked, it_id, name, lvl, stock, f"{price:.2f}", f"{steam_low:.2f}", f"{cost:.2f}", str(alarm), disc, src))

        self.cand_info.configure(text=f"共 {cand_total} 条，{cand_pages} 页，当前第 {self.cand_page} 页")
        self.status.configure(text=f"候选显示：{len(cand_slice)} / {cand_total}｜已勾选：{len(bag.selected_ids)}（系统筛选商品折扣结束会自动剔除，手动添加不受影响）")
        self._refresh_sort_indicators()

    def _paginate(self, items: List[Item], page: int, size: int) -> Tuple[List[Item], int, int, int]:
        total = len(items)
        size = max(1, size)
        pages = max(1, (total + size - 1) // size)
        page = min(max(1, page), pages)
        start = (page - 1) * size
        end = start + size
        return items[start:end], total, pages, page

    def _turn_page(self, which: str, delta: int):
        if which == "sel":
            self.sel_page += delta
        else:
            self.cand_page += delta
        self.refresh()

    def _change_sel_size(self):
        self.sel_page = 1
        self.refresh()

    def _change_cand_size(self):
        self.cand_page = 1
        self.refresh()

    def on_click_selected(self, event):
        row = self.tree_selected.identify_row(event.y)
        if not row:
            return
        bag = self.app.ensure_current()
        it_id = int(row)
        if it_id in bag.selected_ids:
            bag.selected_ids.remove(it_id)
            if it_id in bag.manual_added_ids:
                bag.manual_added_ids.remove(it_id)
        bag.config_confirmed = False
        self.refresh()

    def on_click_candidate(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        bag = self.app.ensure_current()
        it_id = int(row)
        if it_id in bag.selected_ids:
            bag.selected_ids.remove(it_id)
        else:
            bag.selected_ids.add(it_id)
        bag.config_confirmed = False
        self.refresh()

    def on_double_click_alarm(self, event):
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row or col != "#9":
            return
        bag = self.app.ensure_current()
        it_id = int(row)
        it = next((x for x in self.app.catalog if x.id == it_id), None)
        if not it:
            return

        win = tk.Toplevel(self)
        win.title("修改库存报警值")
        win.geometry("320x150")
        win.resizable(False, False)
        ttk.Label(win, text=f"{it.name}", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=12, pady=(12, 6))
        box = ttk.Frame(win, padding=12)
        box.pack(fill="x")
        ttk.Label(box, text="库存报警值：").pack(side="left")
        v_alarm = tk.StringVar(value=str(it.alarm))
        ttk.Entry(box, textvariable=v_alarm, width=10).pack(side="left")

        def save():
            try:
                v = int(float(v_alarm.get().strip()))
                if v <= 0:
                    raise ValueError
                it.alarm = v
                win.destroy()
                self.refresh()
            except Exception:
                messagebox.showerror("输入错误", "库存报警值请输入正整数。")

        btns = ttk.Frame(win, padding=12)
        btns.pack(fill="x")
        ttk.Button(btns, text="保存", command=save).pack(side="left")
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="left", padx=8)

    def select_all(self):
        bag = self.app.ensure_current()
        now = dt.datetime.now()
        # 仅勾选当前候选视图中可见且未过期的系统商品 + 未过期的手动添加商品
        candidates: List[Item] = []
        for it in bag.filtered:
            if it.id in bag.selected_ids:
                continue
            if self._is_expired_for_candidate(it, now):
                continue
            if self._matches(it):
                candidates.append(it)
        for it in self.app.catalog:
            if it.id in bag.manual_added_ids and it.id not in bag.selected_ids:
                if it.discount_end and it.discount_end <= now:
                    continue
                if self._matches(it):
                    candidates.append(it)

        bag.selected_ids |= {it.id for it in candidates}
        bag.config_confirmed = False
        self.refresh()

    def unselect_all(self):
        bag = self.app.ensure_current()
        bag.selected_ids.clear()
        bag.config_confirmed = False
        self.refresh()

    def manual_add(self):
        bag = self.app.ensure_current()
        dlg = ManualAddDialog(self, self.app, bag)
        self.wait_window(dlg)
        self.refresh()

    def back_config_only(self):
        # 仅返回配置页，不重置筛选/选中/计算结果
        self.app.frames["PageConfig"].set_readonly(False)
        self.app.show("PageConfig")

    def back_config(self):
        bag = self.app.ensure_current()
        bag.calc_ok = False
        bag.adapt_triggered = False
        bag.adapt_success = False
        bag.final_probs = {}
        bag.config_confirmed = False
        self.app.frames["PageConfig"].set_readonly(False)
        self.app.show("PageConfig")

    def back_list(self):
        self.app.show("PageBagList")

    def start_calc(self):
        bag = self.app.ensure_current()
        if not bag.cfg:
            messagebox.showerror("异常", "缺少配置，请返回配置页。")
            return
        if not bag.selected_ids:
            messagebox.showwarning("提示", "请至少勾选一个商品。")
            return
        self.btn_calc.configure(state="disabled")
        self.status.configure(text="计算中...")
        self.update_idletasks()
        self.after(120, self._do_calc)

    def _do_calc(self):
        bag = self.app.ensure_current()
        try:
            cfg = bag.cfg
            assert cfg is not None
            self.app.prune_expired_from_selection(bag)
            selected = [it for it in self.app.catalog if it.id in bag.selected_ids]
            if not selected:
                raise ValueError("勾选商品为空（可能被自动剔除），请重新勾选。")

            P_eff = calc_p_eff(cfg.P, cfg.d, cfg.q)
            c_target = calc_c_target(P_eff, cfg.g)

            probs = build_final_probs(cfg, selected, beta=1.0)
            ec = expected_cost_total(cfg, selected, probs)

            if ec <= c_target + 1e-12:
                bag.calc_ok = True
                bag.adapt_triggered = False
                bag.adapt_success = False
                bag.final_probs = probs
            else:
                bag.adapt_triggered = True
                ok, _, probs2 = auto_adapt(cfg, selected, c_target)
                bag.adapt_success = bool(ok)
                bag.calc_ok = bool(ok)
                bag.final_probs = probs2
            bag.config_confirmed = False
            self.app.show("PageResult")
        except Exception as e:
            messagebox.showerror("计算失败", str(e))
        finally:
            self.btn_calc.configure(state="normal")
            self.refresh()

# -------------------------------
# Page 4：结果与操作页
# -------------------------------
class PageResult(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app

        self.breadcrumb = ttk.Label(self, text="", foreground="#9ca3af")
        self.breadcrumb.pack(anchor="w")
        ttk.Button(self, text="返回商品选品页", command=self.back_pick_only).place(relx=1.0, y=5, x=-10, anchor="ne")

        ttk.Label(self, text="结果与操作（最终决策）", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", pady=(0, 10))
        self.banner = tk.Label(self, text="", anchor="w", padx=12, pady=10, font=("Microsoft YaHei UI", 11, "bold"))
        self.banner.pack(fill="x", pady=(0, 10))

        self.tree = ttk.Treeview(self, columns=("name", "level", "stock", "cost", "prob", "remain", "op"), show="headings", height=16)
        for c, t, w, a in [
            ("name", "游戏名", 280, "w"),
            ("level", "等级", 90, "center"),
            ("stock", "库存", 80, "center"),
            ("cost", "成本(元)", 90, "e"),
            ("prob", "最终概率", 110, "e"),
            ("remain", "折扣活动剩余时间", 170, "center"),
            ("op", "操作", 80, "center"),
        ]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a)
        self.tree.pack(fill="both", expand=True, pady=(8, 8))
        self.tree.bind("<Button-1>", self.on_click)

        btns = ttk.Frame(self)
        btns.pack(fill="x", pady=10)
        ttk.Button(btns, text="查看当前配置", command=self.show_config_snapshot).pack(side="right", padx=6)

        self.note = ttk.Label(self, text="", foreground="#9ca3af")
        self.note.pack(anchor="w")

    def on_show(self):
        bag = self.app.ensure_current()
        cfg = bag.cfg
        if not cfg:
            self.app.show("PageConfig")
            return

        self.breadcrumb.configure(text=f"福袋列表 → {bag.bag_id} {cfg.bag_name} → 结果与操作")

        self.app.prune_expired_from_selection(bag)
        selected = [it for it in self.app.catalog if it.id in bag.selected_ids]
        probs = bag.final_probs or {}

        self.tree.delete(*self.tree.get_children())
        now = dt.datetime.now()
        rows = []
        for it in selected:
            p = probs.get(it.id, 0.0)
            if p <= 0:
                continue
            stock = f"{it.stock}" if it.stock < max(1, it.alarm) else str(it.stock)
            remain = "无活动"
            if it.discount_end and it.discount_end > now:
                td = it.discount_end - now
                remain = f"{td.days}天{(td.seconds // 3600)}小时"
            op = "❌ 移除"
            rows.append((it.name, LEVEL_BADGE[it.level], stock, it.cost, p, remain, op, it.id))

        rows.sort(key=lambda x: (-x[4], x[0]))
        for name, lvl, stock, cost, p, remain, op, it_id in rows:
            self.tree.insert("", "end", iid=str(it_id), values=(name, lvl, stock, f"{cost:.2f}", f"{p * 100:.4f}%", remain, op))

        P_eff = calc_p_eff(cfg.P, cfg.d, cfg.q)
        profit_val = self.app._profit_value(cfg, selected, probs) if probs else None

        if not probs or profit_val is None:
            self.banner.configure(bg="#8a1c1c", fg="white", text="❌ 缺少最终概率，请返回选品页完成计算。")
            self.note.configure(text="需先完成计算后再决定上线。")
        else:
            profit_rate = (profit_val / P_eff) if P_eff > 0 else 0.0
            if profit_val >= -1e-12:
                self.banner.configure(bg="#1f6f3c", fg="white", text=f"✅ 预测利润 {profit_val:.2f} 元（利润率 {profit_rate * 100:.2f}% ，目标利润率仅供参考 {cfg.g * 100:.0f}%）")
                self.note.configure(text="预测利润为非负：可在「查看当前配置」中直接上线。")
            else:
                self.banner.configure(bg="#8a1c1c", fg="white", text=f"⚠️ 预测利润为负：{profit_val:.2f} 元（利润率 {profit_rate * 100:.2f}%）")
                self.note.configure(text="预测利润为负：普通运营不可上线，仅可请超管在弹窗中强制上线。")

    def on_click(self, event):
        # 点击“移除”
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if col != "#7":
            return
        bag = self.app.ensure_current()
        it_id = int(row)
        if it_id in bag.selected_ids:
            bag.selected_ids.remove(it_id)
            if it_id in bag.manual_added_ids:
                bag.manual_added_ids.remove(it_id)
        # 移除后自动重新计算
        bag.calc_ok = False
        bag.adapt_triggered = False
        bag.adapt_success = False
        bag.final_probs = {}
        bag.config_confirmed = False
        messagebox.showinfo("已移除", "已移除商品，已返回选品页，请重新计算。")
        self.app.show("PagePick")

    def online(self):
        bag = self.app.ensure_current()
        if not bag.final_probs or not bag.selected_ids:
            messagebox.showwarning("禁止上线", "缺少最终概率或选品结果，请返回选品页计算后再上线。")
            return
        profit_val = self.app._profit_value(bag.cfg, [it for it in self.app.catalog if it.id in bag.selected_ids], bag.final_probs)
        if profit_val is None:
            messagebox.showwarning("禁止上线", "缺少预测利润，无法上线。")
            return
        if profit_val < -1e-12:
            messagebox.showwarning("禁止上线", "预测利润为负，仅超级管理员可强制上线。")
            return
        # 上线：状态更新，回列表
        bag.status = "已上线"
        messagebox.showinfo("上线成功", " 上线成功！已返回福袋列表。")
        self.app.show("PageBagList")

    def force_online(self):
        bag = self.app.ensure_current()
        super_admin = bool(self.app.settings.get('is_super_admin', False))
        if not super_admin:
            messagebox.showwarning("权限不足", "仅超级管理员可在预测利润为负时强制上线。")
            return
        if not bag.final_probs or not bag.selected_ids:
            messagebox.showwarning("需要先计算", "请先在选品页完成计算并生成最终概率。")
            return
        profit_val = self.app._profit_value(bag.cfg, [it for it in self.app.catalog if it.id in bag.selected_ids], bag.final_probs)
        if profit_val is None:
            messagebox.showwarning("需要先计算", "缺少预测利润，无法强制上线。")
            return
        if profit_val >= -1e-12:
            messagebox.showinfo("建议直接上线", "预测利润为非负，可直接上线，无需强制流程。")
            return

        if self.app.settings.get('notify_supervisor_on_force', True) and self.app.settings.get('notify_enabled', True):
            messagebox.showinfo("已通知上级", "预测利润为负，已发起强制上线申请并通知上级审批（占位交互）。")
        else:
            messagebox.showinfo("已记录", "预测利润为负，已记录强制上线申请（占位交互）。")

        bag.status = "待审批"
        self.app.show("PageBagList")

    def show_config_snapshot(self):
        bag = self.app.ensure_current()
        cfg = bag.cfg
        if not cfg:
            messagebox.showwarning("提示", "请先完成配置。")
            return

        ConfigPopup(self, self.app, bag, on_state_change=self.on_state_change)

    def on_state_change(self):
        if self.app.ensure_current().status in ("已上线", "待审批"):
            self.app.show("PageBagList")
        else:
            self.on_show()

    def back_pick_only(self):
        # 返回选品页，不重置已选商品或概率结果
        self.app.show("PagePick")

# -------------------------------
# main
# -------------------------------
def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
