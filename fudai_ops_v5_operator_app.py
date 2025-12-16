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
from tkinter import ttk, messagebox
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import datetime as dt
import random

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

    # 选品相关
    filtered: List[Item] = None
    selected_ids: set[int] = None
    manual_added_ids: set[int] = None

    # 计算/模拟
    calc_ok: bool = False
    adapt_triggered: bool = False
    adapt_success: bool = False
    final_probs: Dict[int, float] = None
    sim_ok: bool = False
    sim_metrics: Optional[Tuple[float, float, float]] = None

    def __post_init__(self):
        if self.filtered is None: self.filtered = []
        if self.selected_ids is None: self.selected_ids = set()
        if self.manual_added_ids is None: self.manual_added_ids = set()
        if self.final_probs is None: self.final_probs = {}

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

def simulate_draws(cfg: Config, selected: List[Item], probs: Dict[int, float], n: int, seed: int = 20251213) -> Tuple[float, float, float]:
    rnd = random.Random(seed)
    by_level: Dict[str, List[Tuple[Item, float]]] = {k: [] for k in LEVELS}
    for it in selected:
        p = probs.get(it.id, 0.0)
        if p > 0:
            by_level[it.level].append((it, p))

    lvl_list, lvl_w = [], []
    lvl_items: Dict[str, Tuple[List[Item], List[float]]] = {}
    for lvl in LEVELS:
        s = sum(p for _, p in by_level[lvl])
        if s > 0:
            lvl_list.append(lvl)
            lvl_w.append(s)
            items = [it for it, _ in by_level[lvl]]
            w = [p for _, p in by_level[lvl]]
            sw = sum(w)
            w = [x / sw for x in w] if sw > 0 else [1 / len(w) for _ in w]
            lvl_items[lvl] = (items, w)

    revenue_per_draw = calc_p_eff(cfg.P, cfg.d, cfg.q)
    total_revenue = revenue_per_draw * n

    pity = 0
    total_cost = 0.0
    legend_hits = 0

    for _ in range(n):
        pity += 1
        if cfg.pity_on and cfg.X and cfg.X > 0 and pity >= cfg.X and "L" in lvl_items:
            lvl = "L"
        else:
            lvl = rnd.choices(lvl_list, weights=lvl_w, k=1)[0]
        items, w = lvl_items[lvl]
        it = rnd.choices(items, weights=w, k=1)[0]
        total_cost += it.cost
        if lvl == "L":
            legend_hits += 1
            pity = 0

    profit = total_revenue - total_cost
    profit_rate = profit / total_revenue if total_revenue > 0 else 0.0
    avg_profit = profit / n if n > 0 else 0.0
    legend_rate = legend_hits / n if n > 0 else 0.0
    return profit_rate, avg_profit, legend_rate

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
        self.settings = {
            'reminder_enabled': True,
            'reminder_time': '16:00',
            'notify_enabled': True,
            'notify_supervisor_on_force': True,
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
        for F in (PageBagList, PageConfig, PagePick, PageResult, PageSim):
            frame = F(container, self)
            self.frames[F.__name__] = frame
            frame.grid(row=0, column=0, sticky="nsew")

        self.show("PageBagList")
        self.after(1000, self._tick_minutely)

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

    # ---------- lifecycle ----------
    def prune_expired_from_selection(self, bag: BagState):
        now = dt.datetime.now()
        for it in self.catalog:
            if it.discount_end and it.discount_end <= now:
                it.discount_status = "无活动"
        # 折扣活动结束：从当前福袋已选池自动剔除（无论系统/手动）
        for it_id in list(bag.selected_ids):
            it = next((x for x in self.catalog if x.id == it_id), None)
            if it and it.discount_end and it.discount_end <= now:
                bag.selected_ids.remove(it_id)
                if it_id in bag.manual_added_ids:
                    bag.manual_added_ids.remove(it_id)

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
        if now.hour != 16 or now.minute != 0:
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
        bag.sim_ok = False
        bag.sim_metrics = None
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

        self.tree = ttk.Treeview(self, columns=("name", "price", "cost", "stock", "status"), show="headings", height=14, selectmode="extended")
        for c, t, w in [
            ("name", "游戏名", 360),
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
            if kw and kw not in it.name.lower():
                continue
            self.tree.insert("", "end", iid=str(it.id), values=(it.name, f"{it.price:.2f}", f"{it.cost:.2f}", str(it.stock), it.discount_status))
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
        self.geometry("520x260")
        self.resizable(False, False)

        ttk.Label(self, text="提醒与通知设置（仅交互展示，可后续接入真实通知）", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=12, pady=(12, 8))

        box = ttk.Frame(self, padding=12)
        box.pack(fill="both", expand=True)

        self.v_remind = tk.BooleanVar(value=bool(app.settings.get('reminder_enabled', True)))
        self.v_notify = tk.BooleanVar(value=bool(app.settings.get('notify_enabled', True)))
        self.v_force = tk.BooleanVar(value=bool(app.settings.get('notify_supervisor_on_force', True)))

        ttk.Checkbutton(box, text="开启每日提醒弹窗（默认 16:00）", variable=self.v_remind).pack(anchor="w", pady=4)
        ttk.Checkbutton(box, text="开启通知（占位：可对接企业微信/邮件）", variable=self.v_notify).pack(anchor="w", pady=4)
        ttk.Checkbutton(box, text="强制上线时通知上级（占位交互）", variable=self.v_force).pack(anchor="w", pady=4)

        ttk.Label(box, text="说明：此处仅提供交互入口与开关，具体通知通道可后续接入。", foreground="#6b7280").pack(anchor="w", pady=(10, 0))

        btns = ttk.Frame(self, padding=12)
        btns.pack(fill="x")
        ttk.Button(btns, text="保存", command=self.save).pack(side="left")
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="left", padx=8)

    def save(self):
        self.app.settings['reminder_enabled'] = bool(self.v_remind.get())
        self.app.settings['notify_enabled'] = bool(self.v_notify.get())
        self.app.settings['notify_supervisor_on_force'] = bool(self.v_force.get())
        messagebox.showinfo("已保存", "提醒/通知设置已保存（占位交互）。")
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

        ttk.Label(top, text="搜索：").pack(side="right", padx=(6, 4))
        self.v_search = tk.StringVar(value="")
        ent = ttk.Entry(top, textvariable=self.v_search, width=26)
        ent.pack(side="right")
        ent.bind("<KeyRelease>", lambda e: self.refresh())

        ttk.Label(top, text="状态：").pack(side="right", padx=(16, 4))
        self.v_status = tk.StringVar(value="全部")
        cb = ttk.Combobox(top, textvariable=self.v_status, values=["全部", "未上线", "已上线", "已下架"], width=10, state="readonly")
        cb.pack(side="right")
        self.v_status.trace_add("write", lambda *_: self.refresh())

        self.tree = ttk.Treeview(self, columns=("id","name","P","time","status"), show="headings", height=18)
        for c, t, w, a in [
            ("id", "福袋ID", 90, "center"),
            ("name", "福袋名称", 260, "w"),
            ("P", "单抽标价(元)", 110, "e"),
            ("time", "上下架时间", 260, "center"),
            ("status", "状态", 100, "center"),
        ]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a)
        self.tree.pack(fill="both", expand=True, pady=(0, 10))

        btns = ttk.Frame(self)
        btns.pack(fill="x")
        ttk.Button(btns, text="编辑️ 编辑", command=self.edit).pack(side="left")
        ttk.Button(btns, text="查看 查看配置（只读）", command=self.view).pack(side="left", padx=8)
        self.btn_del_down = ttk.Button(btns, text="删除️ 删除/⏹️ 下架", command=self.del_or_down)
        self.btn_del_down.pack(side="left", padx=8)

        self.hint = ttk.Label(self, text="提示：未上线可删除；已上线可下架；已下架可删除。", foreground="#9ca3af")
        self.hint.pack(anchor="w")

        self._view_mode = False

    def open_reminder_settings(self):
        ReminderSettingsDialog(self, self.app)

    def on_show(self):
        self.refresh()

    def _match(self, bag: BagState) -> bool:
        st = self.v_status.get()
        if st != "全部" and bag.status != st:
            return False
        kw = self.v_search.get().strip().lower()
        if kw:
            name = bag.cfg.bag_name if bag.cfg else ""
            if kw not in (name or "").lower() and kw not in bag.bag_id.lower():
                return False
        return True

    def refresh(self):
        self.app._auto_down_by_time()
        self.tree.delete(*self.tree.get_children())
        for bag_id, bag in sorted(self.app.bags.items(), key=lambda x: x[0]):
            if not self._match(bag):
                continue
            name = bag.cfg.bag_name if bag.cfg else "（未配置）"
            P = f"{bag.cfg.P:.2f}" if bag.cfg else "—"
            time_s = "—"
            if bag.cfg:
                time_s = f"{fmt_dt(bag.cfg.up_time)} ~ {fmt_dt(bag.cfg.down_time)}"
            self.tree.insert("", "end", iid=bag_id, values=(bag_id, name, P, time_s, bag.status))

    def _selected_bag_id(self) -> Optional[str]:
        sel = self.tree.selection()
        if not sel:
            return None
        return sel[0]

    def new_bag(self):
        bag_id = self.app._next_bag_id()
        bag = BagState(bag_id=bag_id, status="未上线")
        self.app.bags[bag_id] = bag
        self.app.current_bag_id = bag_id
        self.app.frames["PageConfig"].set_readonly(False)
        self.app.show("PageConfig")

    def edit(self):
        bag_id = self._selected_bag_id()
        if not bag_id:
            messagebox.showinfo("提示", "请先选择一个福袋。")
            return
        self.app.current_bag_id = bag_id
        self.app.frames["PageConfig"].set_readonly(False)
        self.app.show("PageConfig")

    def view(self):
        bag_id = self._selected_bag_id()
        if not bag_id:
            messagebox.showinfo("提示", "请先选择一个福袋。")
            return
        self.app.current_bag_id = bag_id
        self.app.frames["PageConfig"].set_readonly(True)
        self.app.show("PageConfig")

    def del_or_down(self):
        bag_id = self._selected_bag_id()
        if not bag_id:
            messagebox.showinfo("提示", "请先选择一个福袋。")
            return
        bag = self.app.bags[bag_id]
        if bag.status == "已上线":
            if messagebox.askyesno("确认下架", f"确认下架 {bag_id} 吗？"):
                bag.status = "已下架"
                self.refresh()
            return
        # 未上线/已下架可删除
        if messagebox.askyesno("确认删除", f"确认删除 {bag_id} 吗？删除后不可恢复。"):
            del self.app.bags[bag_id]
            if self.app.current_bag_id == bag_id:
                self.app.current_bag_id = None
            self.refresh()

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

        # vars
        self.v_name = tk.StringVar(value="")
        self.v_P = tk.StringVar(value="99")
        self.v_g = tk.StringVar(value="30")
        self.v_d = tk.StringVar(value="0.95")
        self.v_q = tk.StringVar(value="60")
        self.v_ratio = tk.StringVar(value="80")

        now = dt.datetime.now()
        self.v_up = tk.StringVar(value=fmt_dt(now + dt.timedelta(hours=1)))
        self.v_down = tk.StringVar(value=fmt_dt(now + dt.timedelta(days=7)))

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

        self.preview_tree = ttk.Treeview(preview_box, columns=("name", "stock", "cost", "prob", "src"), show="headings", height=7)
        for c, t, w, a in [
            ("name", "游戏名", 360, "w"),
            ("stock", "库存", 80, "center"),
            ("cost", "成本(元)", 90, "e"),
            ("prob", "最终概率(如有)", 120, "e"),
            ("src", "来源", 90, "center"),
        ]:
            self.preview_tree.heading(c, text=t)
            self.preview_tree.column(c, width=w, anchor=a)
        self.preview_tree.pack(fill="both", expand=True)

        self.preview_hint = ttk.Label(preview_box, text="提示：这里展示当前福袋已配置/已选择商品。若需要调整，请进入「商品选品页」。", foreground="#6b7280")
        self.preview_hint.pack(anchor="w", pady=(6, 0))

        self.btns = ttk.Frame(body)
        self.btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=12)

        self.btn_save = ttk.Button(self.btns, text="保存配置（返回列表）", command=self.on_save)
        self.btn_save.pack(side="left")
        self.btn_filter = ttk.Button(self.btns, text="筛选商品（去选品）", command=self.on_filter)
        self.btn_filter.pack(side="left", padx=8)
        self.btn_manual = ttk.Button(self.btns, text="手动添加商品", command=self.on_manual_add)
        self.btn_manual.pack(side="left", padx=8)
        ttk.Button(self.btns, text="返回列表", command=lambda: self.app.show("PageBagList")).pack(side="right")

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
            self.v_up.set(fmt_dt(cfg.up_time))
            self.v_down.set(fmt_dt(cfg.down_time))
            self.v_pity_on.set(bool(cfg.pity_on))
            self.v_X.set(str(cfg.X or ""))
            for k in LEVELS:
                self.v_p[k].set(str(int(round(cfg.p_k[k] * 100))))
                self.v_lo[k].set(str(cfg.cost_ranges[k].lo))
                self.v_hi[k].set(str(cfg.cost_ranges[k].hi))
        else:
            if not self.v_name.get():
                self.v_name.set(f"{dt.datetime.now():%Y%m%d} 福袋")

        self._apply_readonly_state()
        self.refresh_expected_cost()
        self.refresh_preview()

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
        self.btn_save.configure(state=("disabled" if self._readonly else "normal"))
        self.btn_filter.configure(state=("disabled" if self._readonly else "normal"))
        self.btn_manual.configure(state=("disabled" if self._readonly else "normal"))

    def _build_basic(self, parent):
        self._basic_entries = []
        ttk.Label(parent, text="基础配置（含上下架时间）", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 6))

        def row(label, var, hint=""):
            r = ttk.Frame(parent)
            r.pack(fill="x", pady=4)
            ttk.Label(r, text=label, width=20).pack(side="left")
            ent = ttk.Entry(r, textvariable=var, width=18)
            ent.pack(side="left")
            self._basic_entries.append(ent)
            if hint:
                ttk.Label(r, text=hint, foreground="#9ca3af").pack(side="left", padx=8)

        row("福袋名称（≤30字）", self.v_name)
        row("单抽标价 P（元）", self.v_P)
        row("目标利润率 g（%）", self.v_g, "如 30")
        row("十连折扣系数 d", self.v_d, "如 0.97=97折")
        row("十连抽占比 q（%）", self.v_q, "如 60")
        row("售价筛选比例（%）", self.v_ratio, "商品售价≥P×比例")
        row("上架时间", self.v_up, "YYYY-MM-DD HH:MM")
        row("下架时间", self.v_down, "需晚于上架时间")

        ttk.Label(parent, text="保底设置", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(12, 6))
        self.cb_pity = ttk.Checkbutton(parent, text="开启：累计 X 抽必出传说", variable=self.v_pity_on, command=self._toggle_x)
        self.cb_pity.pack(anchor="w")
        rx = ttk.Frame(parent)
        rx.pack(fill="x", pady=4)
        ttk.Label(rx, text="保底阈值 X", width=20).pack(side="left")
        self.ent_X = ttk.Entry(rx, textvariable=self.v_X, width=18)
        self.ent_X.pack(side="left")
        self._basic_entries.append(self.ent_X)

    def _toggle_x(self):
        self.ent_X.configure(state=("normal" if (self.v_pity_on.get() and not self._readonly) else "disabled"))

    def _build_levels(self, parent):
        self._range_entries = []
        ttk.Label(parent, text="等级与成本范围（数值区间）", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 6))
        ttk.Label(parent, text="系统预期成本=参考值（只读）。建议你填写的成本区间包含参考值。", foreground="#9ca3af").pack(anchor="w", pady=(0, 8))

        header = ttk.Frame(parent)
        header.pack(fill="x", pady=(0, 4))
        ttk.Label(header, text="等级", width=10).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="概率(%)", width=10).grid(row=0, column=1, sticky="w")
        ttk.Label(header, text="预期成本", width=10).grid(row=0, column=2, sticky="w")
        ttk.Label(header, text="下限", width=8).grid(row=0, column=3, sticky="w")
        ttk.Label(header, text="~", width=2).grid(row=0, column=4, sticky="w")
        ttk.Label(header, text="上限", width=8).grid(row=0, column=5, sticky="w")

        for lvl in LEVELS:
            r = ttk.Frame(parent)
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

        up_time = parse_dt(self.v_up.get(), "上架时间")
        down_time = parse_dt(self.v_down.get(), "下架时间")
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

        return Config(bag_name, P, g, d, q, ratio, up_time, down_time, pity_on, X, p_k, ranges)

    def refresh_expected_cost(self):
        try:
            cfg = self._build_config()
            mu = expected_cost_reference(cfg)
            for k in LEVELS:
                lo = int(self.v_lo[k].get() or 0)
                hi = int(self.v_hi[k].get() or 0)
                self.v_exp[k].set(f"{mu[k]:.0f}" if (lo <= mu[k] <= hi) else f"⚠ {mu[k]:.0f}")
        except Exception:
            return

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
                rows.append((it.name, it.stock, it.cost, p, src))

            if not rows:
                self.preview_hint.configure(text=f"{LEVEL_NAME.get(lvl, lvl)} 等级暂无已选商品。")
                return

            rows.sort(key=lambda x: -x[3])  # prob desc
            for name, stock, cost, p, src in rows[:80]:
                self.preview_tree.insert("", "end", values=(name, stock, f"{cost:.2f}", (f"{p*100:.4f}%" if p > 0 else "-"), src))
            self.preview_hint.configure(text=f"显示 {min(len(rows), 80)} 条（可在选品页调整；最终概率需计算后才有）。")
        except Exception:
            return

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
        bag.sim_ok = False
        bag.sim_metrics = None

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

        self.breadcrumb = ttk.Label(self, text="", foreground="#9ca3af")
        self.breadcrumb.pack(anchor="w")

        ttk.Label(self, text="商品选品（系统筛选 + 手动添加）", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", pady=(0, 10))

        top = ttk.Frame(self)
        top.pack(fill="x", pady=6)

        self.level_vars = {k: tk.BooleanVar(value=True) for k in LEVELS}
        lvf = ttk.Frame(top)
        lvf.pack(side="left")
        ttk.Label(lvf, text="等级：").pack(side="left")
        for k in LEVELS:
            ttk.Checkbutton(lvf, text=LEVEL_NAME[k], variable=self.level_vars[k], command=self.refresh).pack(side="left", padx=(0, 6))

        self.v_stock_filter = tk.StringVar(value="全部")
        ttk.Label(top, text="库存：").pack(side="left", padx=(16, 4))
        ttk.Combobox(top, textvariable=self.v_stock_filter, values=["全部", "正常库存", "低库存预警"], width=10, state="readonly").pack(side="left")
        self.v_stock_filter.trace_add("write", lambda *_: self.refresh())

        ttk.Label(top, text="搜索：").pack(side="right", padx=(6, 4))
        self.v_search = tk.StringVar(value="")
        ent = ttk.Entry(top, textvariable=self.v_search, width=24)
        ent.pack(side="right")
        ent.bind("<KeyRelease>", lambda e: self.refresh())

        self.tree = ttk.Treeview(self, columns=("checked", "name", "level", "stock", "price", "cost", "alarm", "disc", "src"),
                                 show="headings", height=18)
        for c, t, w, a in [
            ("checked", "选择", 60, "center"),
            ("name", "游戏名", 260, "w"),
            ("level", "等级", 90, "center"),
            ("stock", "库存", 90, "center"),
            ("price", "售价", 90, "e"),
            ("cost", "成本", 90, "e"),
            ("alarm", "库存报警值", 100, "center"),
            ("disc", "折扣活动状态", 150, "center"),
            ("src", "来源", 90, "center"),
        ]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a)
        self.tree.pack(fill="both", expand=True, pady=(8, 8))

        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<Double-1>", self.on_double_click_alarm)

        btns = ttk.Frame(self)
        btns.pack(fill="x", pady=8)
        ttk.Button(btns, text="全选", command=self.select_all).pack(side="left")
        ttk.Button(btns, text="取消全选", command=self.unselect_all).pack(side="left", padx=6)
        ttk.Button(btns, text="手动添加商品", command=self.manual_add).pack(side="left", padx=18)
        ttk.Button(btns, text="返回配置", command=self.back_config).pack(side="left")
        ttk.Button(btns, text="返回列表", command=self.back_list).pack(side="left", padx=6)

        self.btn_calc = ttk.Button(btns, text="开始计算", command=self.start_calc)
        self.btn_calc.pack(side="right")

        self.status = ttk.Label(self, text="", foreground="#9ca3af")
        self.status.pack(anchor="w")

    def on_show(self):
        bag = self.app.ensure_current()
        name = bag.cfg.bag_name if bag.cfg else ""
        self.breadcrumb.configure(text=f"福袋列表 → {bag.bag_id} {name} → 商品选品")
        self.refresh()

    def _is_low_stock(self, it: Item) -> bool:
        return it.stock < max(1, it.alarm)

    def _matches(self, it: Item) -> bool:
        if not self.level_vars.get(it.level, tk.BooleanVar(value=True)).get():
            return False
        kw = self.v_search.get().strip().lower()
        if kw and kw not in it.name.lower():
            return False
        sf = self.v_stock_filter.get()
        if sf == "正常库存" and self._is_low_stock(it):
            return False
        if sf == "低库存预警" and not self._is_low_stock(it):
            return False
        return True

    def refresh(self):
        bag = self.app.ensure_current()
        if not bag.cfg:
            self.app.show("PageConfig")
            return

        self.app.prune_expired_from_selection(bag)

        # 系统筛选商品
        shown = [it for it in bag.filtered if self._matches(it)]

        # 手动添加商品（永不被系统剔除）
        manual_items = [it for it in self.app.catalog if it.id in bag.manual_added_ids]
        for it in manual_items:
            # 允许按等级/搜索/库存筛选显示，但不因系统筛选规则而消失
            if self._matches(it) and it not in shown:
                shown.append(it)

        self.tree.delete(*self.tree.get_children())
        now = dt.datetime.now()
        for it in shown:
            checked = "☑️" if it.id in bag.selected_ids else "☐"
            stock = f"{it.stock}" if self._is_low_stock(it) else str(it.stock)

            disc = it.discount_status
            if it.discount_end and it.discount_end > now:
                remain = it.discount_end - now
                if remain.total_seconds() <= 24 * 3600:
                    disc = f"即将结束（{int(remain.total_seconds() // 3600)}h）"
            if it.discount_end and it.discount_end <= now:
                disc = "无活动"

            src = "手动添加" if it.id in bag.manual_added_ids else "系统筛选"

            self.tree.insert("", "end", iid=str(it.id),
                             values=(checked, it.name, LEVEL_BADGE[it.level], stock, f"{it.price:.2f}", f"{it.cost:.2f}", str(it.alarm), disc, src))

        self.status.configure(text=f"当前显示：{len(shown)} | 已勾选：{len(bag.selected_ids)}（活动结束商品会自动剔除）")

    def on_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        bag = self.app.ensure_current()
        it_id = int(row)
        if it_id in bag.selected_ids:
            bag.selected_ids.remove(it_id)
        else:
            bag.selected_ids.add(it_id)
        self.refresh()

    def on_double_click_alarm(self, event):
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row or col != "#7":
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
        bag.selected_ids |= ({it.id for it in bag.filtered} | set(bag.manual_added_ids))
        self.refresh()

    def unselect_all(self):
        bag = self.app.ensure_current()
        bag.selected_ids.clear()
        self.refresh()

    def manual_add(self):
        bag = self.app.ensure_current()
        dlg = ManualAddDialog(self, self.app, bag)
        self.wait_window(dlg)
        self.refresh()

    def back_config(self):
        bag = self.app.ensure_current()
        bag.calc_ok = False
        bag.adapt_triggered = False
        bag.adapt_success = False
        bag.final_probs = {}
        bag.sim_ok = False
        bag.sim_metrics = None
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

            bag.sim_ok = False
            bag.sim_metrics = None
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
        self.btn_sim = ttk.Button(btns, text="抽卡模拟", command=self.goto_sim)
        self.btn_sim.pack(side="left")
        ttk.Button(btns, text="查看公示规则", command=self.show_disclosure).pack(side="left", padx=6)
        ttk.Button(btns, text="返回选品", command=self.back_pick).pack(side="left", padx=6)
        ttk.Button(btns, text="返回配置", command=self.back_config).pack(side="left", padx=6)
        self.btn_online = ttk.Button(btns, text="确认上线", command=self.online)
        self.btn_online.pack(side="right")
        self.btn_force = ttk.Button(btns, text="强制上线（通知上级）", command=self.force_online)
        self.btn_force.pack(side="right", padx=8)

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
        c_target = calc_c_target(P_eff, cfg.g)
        ec = expected_cost_total(cfg, selected, probs) if probs else float("inf")
        gap = ec - c_target

        if bag.calc_ok:
            if bag.adapt_triggered and bag.adapt_success:
                self.banner.configure(bg="#1f6f3c", fg="white", text="✅ 配置通过！自适应调权完成（若触发），可进入模拟。")
            else:
                self.banner.configure(bg="#1f6f3c", fg="white", text="✅ 配置通过！可进入模拟。")
            self.btn_sim.configure(state="normal")
        else:
            self.banner.configure(bg="#8a1c1c", fg="white", text=f"❌ 配置未通过！期望成本超出盈利红线 {gap:.2f} 元，请返回调整。")
            # 允许模拟：即使配置未通过，也可查看模拟结果用于判断差距
            self.btn_sim.configure(state=("normal" if probs else "disabled"))

        # 上线按钮逻辑：
        # - 模拟通过：允许正常上线
        # - 模拟未通过但已模拟过：允许强制上线（通知上级，占位交互）
        if bag.sim_metrics:
            if bag.sim_ok:
                self.btn_online.configure(state="normal")
                self.btn_force.configure(state="disabled")
                self.note.configure(text="模拟已通过：允许上线。")
            else:
                self.btn_online.configure(state="disabled")
                self.btn_force.configure(state="normal")
                self.note.configure(text="模拟未通过：可选择强制上线（将通知上级审批）。")
        else:
            self.btn_online.configure(state="disabled")
            self.btn_force.configure(state="disabled")
            self.note.configure(text=("提示：请先进行一次「抽卡模拟」。" if bag.final_probs else "提示：请先在选品页完成计算。"))

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
        bag.sim_ok = False
        bag.sim_metrics = None
        messagebox.showinfo("已移除", "已移除商品，请返回选品页重新计算。")
        self.on_show()

    def back_pick(self):
        bag = self.app.ensure_current()
        bag.calc_ok = False
        bag.adapt_triggered = False
        bag.adapt_success = False
        bag.final_probs = {}
        bag.sim_ok = False
        bag.sim_metrics = None
        self.app.show("PagePick")

    def back_config(self):
        bag = self.app.ensure_current()
        bag.calc_ok = False
        bag.adapt_triggered = False
        bag.adapt_success = False
        bag.final_probs = {}
        bag.sim_ok = False
        bag.sim_metrics = None
        self.app.frames["PageConfig"].set_readonly(False)
        self.app.show("PageConfig")

    def goto_sim(self):
        bag = self.app.ensure_current()
        if not bag.final_probs or not bag.selected_ids:
            messagebox.showwarning("不可模拟", "缺少最终概率或选品结果，请先返回选品页进行计算。")
            return
        self.app.show("PageSim")

    def online(self):
        bag = self.app.ensure_current()
        if not (bag.calc_ok and bag.sim_ok):
            messagebox.showwarning("禁止上线", "必须先通过计算与模拟，才允许上线。")
            return
        # 上线：状态更新，回列表
        bag.status = "已上线"
        messagebox.showinfo("上线成功", " 上线成功！已返回福袋列表。")
        self.app.show("PageBagList")

    def force_online(self):
        bag = self.app.ensure_current()
        # 允许在模拟不通过时强制上线：只做交互占位 + 通知上级
        if not bag.sim_metrics:
            messagebox.showwarning("需要先模拟", "请先完成一次抽卡模拟，再决定是否强制上线。")
            return
        if bag.sim_ok:
            # 模拟通过直接走正常上线
            return self.online()

        if self.app.settings.get('notify_supervisor_on_force', True) and self.app.settings.get('notify_enabled', True):
            messagebox.showinfo("已通知上级", "模拟未通过，已发起强制上线申请并通知上级审批（占位交互）。")
        else:
            messagebox.showinfo("已记录", "模拟未通过，已记录强制上线申请（占位交互）。")

        bag.status = "待审批"
        self.app.show("PageBagList")

    def show_disclosure(self):
        bag = self.app.ensure_current()
        cfg = bag.cfg
        if not cfg or not bag.final_probs:
            messagebox.showwarning("提示", "请先完成计算。")
            return

        selected = [it for it in self.app.catalog if it.id in bag.selected_ids]
        probs = bag.final_probs

        win = tk.Toplevel(self)
        win.title("概率公示规则（可复制）")
        win.geometry("900x600")

        txt = tk.Text(win, wrap="word")
        txt.pack(fill="both", expand=True, padx=12, pady=12)

        lines = []
        lines.append(f"福袋ID：{bag.bag_id}")
        lines.append(f"福袋名称：{cfg.bag_name}")
        lines.append(f"单抽标价：{cfg.P:.2f} 元")
        lines.append(f"上下架时间：{fmt_dt(cfg.up_time)} ~ {fmt_dt(cfg.down_time)}")
        lines.append(f"十连折扣：d={cfg.d:.2f}，十连占比：q={cfg.q * 100:.0f}%")
        lines.append(f"售价筛选比例：商品售价 ≥ 单抽价×{cfg.price_ratio * 100:.0f}%")
        lines.append(f"保底规则：{'累计 ' + str(cfg.X) + ' 抽必出【传说】' if cfg.pity_on and cfg.X else '未开启'}")
        lines.append("\n概率公示（最终）：（以下概率可直接用于前端展示）\n")

        rows = []
        for it in selected:
            p = probs.get(it.id, 0.0)
            if p > 0:
                rows.append((LEVEL_NAME[it.level], it.name, p))
        rows.sort(key=lambda x: (x[0], -x[2], x[1]))
        for lvl, name, p in rows:
            lines.append(f"- {lvl}｜{name}：{p * 100:.6f}%")
        lines.append(f"\n概率总和：{sum(probs.values()) * 100:.4f}%（理论应≈100%）")

        txt.insert("1.0", "\n".join(lines))

        def copy_all():
            self.clipboard_clear()
            self.clipboard_append(txt.get("1.0", "end-1c"))
            messagebox.showinfo("已复制", "已复制到剪贴板。")

        btns = ttk.Frame(win, padding=12)
        btns.pack(fill="x")
        ttk.Button(btns, text="复制全部", command=copy_all).pack(side="left")
        ttk.Button(btns, text="关闭", command=win.destroy).pack(side="left", padx=8)

# -------------------------------
# Page 5：抽卡模拟页
# -------------------------------
class PageSim(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app

        self.breadcrumb = ttk.Label(self, text="", foreground="#9ca3af")
        self.breadcrumb.pack(anchor="w")

        ttk.Label(self, text="抽卡模拟（极简工具）", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", pady=(0, 10))

        top = ttk.Frame(self)
        top.pack(fill="x", pady=8)
        ttk.Label(top, text="模拟抽数（5万~30万）", width=22).pack(side="left")
        self.v_n = tk.StringVar(value="100000")
        ttk.Entry(top, textvariable=self.v_n, width=12).pack(side="left")
        self.btn_run = ttk.Button(top, text="开始模拟", command=self.run_sim)
        self.btn_run.pack(side="left", padx=10)
        self.lbl = ttk.Label(top, text="", foreground="#9ca3af")
        self.lbl.pack(side="left")

        cards = ttk.Frame(self)
        cards.pack(fill="x", pady=10)
        self.card_pr = self._card(cards, "利润率", 0)
        self.card_ap = self._card(cards, "平均每抽利润", 1)
        self.card_lr = self._card(cards, "传说出货率", 2)

        btns = ttk.Frame(self)
        btns.pack(fill="x", pady=12)
        ttk.Button(btns, text="重新模拟", command=self.run_sim).pack(side="left")
        ttk.Button(btns, text="返回结果页", command=lambda: self.app.show("PageResult")).pack(side="left", padx=6)
        self.btn_confirm = ttk.Button(btns, text="确认上线（达标激活）", command=self.confirm_online)
        self.btn_confirm.pack(side="right")
        self.btn_force = ttk.Button(btns, text="强制上线（通知上级）", command=self.force_online)
        self.btn_force.pack(side="right", padx=8)

        self.note = ttk.Label(self, text="", foreground="#9ca3af")
        self.note.pack(anchor="w")

    def _card(self, parent, title: str, idx: int):
        lf = ttk.Labelframe(parent, text=title, padding=12)
        lf.grid(row=0, column=idx, sticky="ew", padx=(0 if idx == 0 else 8, 0))
        parent.columnconfigure(idx, weight=1)
        v = tk.StringVar(value="—")
        lab = ttk.Label(lf, textvariable=v, font=("Microsoft YaHei UI", 14, "bold"))
        lab.pack(anchor="center")
        return (v, lab)

    def on_show(self):
        bag = self.app.ensure_current()
        cfg = bag.cfg
        if cfg:
            self.breadcrumb.configure(text=f"福袋列表 → {bag.bag_id} {cfg.bag_name} → 抽卡模拟")
        self.lbl.configure(text="")
        self.note.configure(text="")
        self.btn_confirm.configure(state="disabled")
        self.btn_force.configure(state="disabled")
        for v, _ in (self.card_pr, self.card_ap, self.card_lr):
            v.set("—")

    def _parse_n(self) -> int:
        n = int(float(self.v_n.get().strip()))
        n = max(50_000, min(300_000, n))
        self.v_n.set(str(n))
        return n

    def run_sim(self):
        bag = self.app.ensure_current()
        if not bag.final_probs or not bag.selected_ids:
            messagebox.showwarning("不可模拟", "缺少最终概率或选品结果，请先返回选品页计算。")
            return
        try:
            n = self._parse_n()
        except Exception:
            messagebox.showerror("输入错误", "模拟抽数请输入数字。")
            return

        self.lbl.configure(text="模拟中...")
        self.btn_run.configure(state="disabled")
        self.update_idletasks()
        self.after(60, lambda: self._do_sim(n))

    def _do_sim(self, n: int):
        bag = self.app.ensure_current()
        try:
            cfg = bag.cfg
            assert cfg is not None
            selected = [it for it in self.app.catalog if it.id in bag.selected_ids]
            probs = bag.final_probs
            if not selected or not probs:
                raise ValueError("缺少最终概率，请返回重新计算。")

            pr, ap, lr = simulate_draws(cfg, selected, probs, n=n)
            ok = pr >= cfg.g - 1e-12
            bag.sim_ok = bool(ok)
            bag.sim_metrics = (pr, ap, lr)

            good, bad = "#1f6f3c", "#8a1c1c"
            pr_v, pr_lab = self.card_pr
            ap_v, ap_lab = self.card_ap
            lr_v, lr_lab = self.card_lr

            pr_v.set(f"{pr * 100:.2f}%（目标 {cfg.g * 100:.0f}%）")
            ap_v.set(f"{ap:.2f} 元")
            lr_v.set(f"{lr * 100:.2f}%")

            pr_lab.configure(foreground=(good if ok else bad))
            ap_lab.configure(foreground=(good if ok else bad))
            lr_lab.configure(foreground=good)

            if ok:
                self.note.configure(text="✅ 模拟利润率达标：允许上线。返回结果页点击「确认上线」。")
                self.btn_confirm.configure(state="normal")
                self.btn_force.configure(state="disabled")
            else:
                self.note.configure(text="❌ 模拟利润率不达标：禁止上线，请返回调整。")
                self.btn_confirm.configure(state="disabled")
                self.btn_force.configure(state="disabled")
        except Exception as e:
            messagebox.showerror("模拟失败", str(e))
        finally:
            self.lbl.configure(text="")
            self.btn_run.configure(state="normal")

    def confirm_online(self):
        bag = self.app.ensure_current()
        if not bag.sim_ok:
            messagebox.showwarning("禁止上线", "模拟未通过，不允许上线。")
            return
        # 直接上线并回列表
        bag.status = "已上线"
        messagebox.showinfo("上线成功", " 上线成功！已返回福袋列表。")
        self.app.show("PageBagList")

    def force_online(self):
        bag = self.app.ensure_current()
        if not bag.sim_metrics:
            messagebox.showwarning("需要先模拟", "请先完成一次抽卡模拟，再决定是否强制上线。")
            return
        if bag.sim_ok:
            return self.confirm_online()

        if self.app.settings.get('notify_supervisor_on_force', True) and self.app.settings.get('notify_enabled', True):
            messagebox.showinfo("已通知上级", "模拟未通过，已发起强制上线申请并通知上级审批（占位交互）。")
        else:
            messagebox.showinfo("已记录", "模拟未通过，已记录强制上线申请（占位交互）。")

        bag.status = "待审批"
        self.app.show("PageBagList")

# -------------------------------
# main
# -------------------------------
def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
