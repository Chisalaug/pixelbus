"""YAML 规则的加载、校验和友好报错。

这个模块的价值不在"能读 YAML"，而在**读错的时候能说到点子上**：
带行号、带字段路径、带一句"你大概想写什么"。

校验一次跑完，把所有问题一起抛出来，不用改一个错再跑一遍。
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import yaml

from .capture import RegionError, RegionSpec, parse_region
from .predicates import PredicateSpecError, build_test

__all__ = [
    "RulesError",
    "Issue",
    "RuleSet",
    "RegionCfg",
    "SignalCfg",
    "StateCfg",
    "ActionCfg",
    "WhenExpr",
    "load_rules",
    "loads_rules",
    "parse_when",
    "ACTION_KINDS",
    "TOP_KEYS",
]

TOP_KEYS = {
    "version",
    "fps",
    "regions",
    "signals",
    "states",
    "actions",
    "server",
    "name",
    "description",
}

ACTION_KINDS = {"webhook", "press", "exec", "mqtt", "log", "beep"}


# --------------------------------------------------------------------------
# YAML 加载器：按 YAML 1.2 认布尔值
# --------------------------------------------------------------------------
#
# PyYAML 默认按 YAML 1.1 解析，会把 on / off / yes / no 全当成布尔。
# 而我们的契约里 `on:` 是动作挂载的 state 名 —— 一旦被解析成 True，
# 报错会变成"action 里不认识 [True]"，谁都看不懂。所以这里只认 true/false。

_BOOL_RE = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")


class _Loader(yaml.SafeLoader):
    pass


_Loader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver("tag:yaml.org,2002:bool", _BOOL_RE, list("tTfF"))


def _yaml_load(text: str) -> Any:
    return yaml.load(text, Loader=_Loader)  # noqa: S506 - 受控子类，只用 safe 的构造器


# --------------------------------------------------------------------------
# 报错
# --------------------------------------------------------------------------


@dataclass
class Issue:
    where: str
    msg: str
    hint: str = ""
    line: int | None = None
    severity: str = "error"

    def render(self, filename: str) -> str:
        loc = f"{filename}:{self.line}" if self.line else filename
        head = "  ✗" if self.severity == "error" else "  !"
        text = f"{head} {loc}  {self.where}\n      {self.msg}"
        if self.hint:
            text += f"\n      → {self.hint}"
        return text


class RulesError(ValueError):
    """一条或多条规则错误。``str(err)`` 直接能贴给人看。"""

    def __init__(self, issues: Sequence[Issue], path: str = "rules.yaml") -> None:
        self.issues = list(issues)
        self.path = os.path.basename(path) if path else "rules.yaml"
        super().__init__(self.format())

    def format(self) -> str:
        n = len(self.issues)
        lines = [f"{self.path} 有 {n} 处问题：", ""]
        lines += [i.render(self.path) for i in self.issues]
        lines += ["", '  YAML 契约见 README 的"YAML 全部字段"，或者直接抄 rules/example.yaml']
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 行号
# --------------------------------------------------------------------------


def _line_index(text: str) -> dict[tuple, int]:
    """把 YAML 组成一棵节点树，记下每个路径的行号（1 起）。

    用 ``yaml.compose`` 而不是 post-process 数据，是为了让 dict 保持干净。
    """
    index: dict[tuple, int] = {}
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return index
    if root is None:
        return index

    def walk(node: yaml.Node, path: tuple) -> None:
        index[path] = node.start_mark.line + 1
        if isinstance(node, yaml.MappingNode):
            for key_node, val_node in node.value:
                key = getattr(key_node, "value", None)
                walk(val_node, path + (key,))
        elif isinstance(node, yaml.SequenceNode):
            for i, val_node in enumerate(node.value):
                walk(val_node, path + (i,))

    walk(root, ())
    return index


class _Ctx:
    """收集 issue 的小工具，顺便把路径翻译成行号。"""

    def __init__(self, path: str, index: dict[tuple, int]) -> None:
        self.path = path
        self.index = index
        self.issues: list[Issue] = []

    def key(self, *path: Any) -> tuple:
        return tuple(path)

    def err(self, path: tuple, msg: str, hint: str = "") -> None:
        self.issues.append(Issue(_where(path), msg, hint, self._line(path), "error"))

    def warn(self, path: tuple, msg: str, hint: str = "") -> None:
        self.issues.append(Issue(_where(path), msg, hint, self._line(path), "warn"))

    def _line(self, path: tuple) -> int | None:
        for size in range(len(path), -1, -1):
            hit = self.index.get(path[:size])
            if hit is not None:
                return hit
        return None


def _where(path: tuple) -> str:
    parts: list[str] = []
    for p in path:
        if isinstance(p, int):
            parts.append(f"[{p}]")
        else:
            parts.append(p if not parts else f".{p}")
    return "".join(parts) if parts else "(根)"


# --------------------------------------------------------------------------
# when 表达式
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WhenExpr:
    """``when`` 的语法树。只有四种节点，够用且不会变成一门新语言。"""

    op: str  # sig / and / or / not
    name: str = ""
    kids: tuple["WhenExpr", ...] = ()

    def signals(self) -> set[str]:
        if self.op == "sig":
            return {self.name}
        return set().union(*(k.signals() for k in self.kids)) if self.kids else set()

    def __str__(self) -> str:
        if self.op == "sig":
            return self.name
        if self.op == "not":
            return f"!{self.kids[0]}"
        joiner = " & " if self.op == "and" else " | "
        parts = []
        for k in self.kids:
            text = str(k)
            parts.append(f"({text})" if k.op in ("and", "or") and k.op != self.op else text)
        return joiner.join(parts)


_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")


class WhenError(ValueError):
    def __init__(self, msg: str, hint: str = "") -> None:
        super().__init__(msg if not hint else f"{msg}（{hint}）")
        self.msg = msg
        self.hint = hint


def parse_when(value: Any) -> WhenExpr:
    """把 ``when`` 解析成语法树。

    支持三种写法：
    - 单个字符串：``alarm_on``
    - 列表（AND）：``[alarm_on, not_silenced]``
    - 表达式：``"a & !b"``、``"a | (b & c)"``
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise WhenError("when 是空的", "写上 signal 或 state 的名字")
        if not re.search(r"[&|!()]", text):
            if not _NAME_RE.fullmatch(text):
                raise WhenError(
                    f"when 里的名字 {text!r} 不合法",
                    "名字只能用字母、数字、下划线、点、短横线",
                )
            return WhenExpr("sig", text)
        return _WhenParser(text).parse()

    if isinstance(value, (list, tuple)):
        if not value:
            raise WhenError("when 列表是空的", "至少写一个名字")
        kids = tuple(parse_when(v) for v in value)
        return kids[0] if len(kids) == 1 else WhenExpr("and", kids=kids)

    raise WhenError(
        f"when 应该是字符串或列表，收到 {type(value).__name__}",
        '例如 when: alarm_on 或 when: "alarm_on & !silenced"',
    )


class _WhenParser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0

    def parse(self) -> WhenExpr:
        expr = self._or()
        self._skip()
        if self.i < len(self.text):
            raise WhenError(
                f"when 在 {self.text[self.i:]!r} 这里看不懂",
                "可用运算：&（与）、|（或）、!（非）、括号",
            )
        return expr

    def _skip(self) -> None:
        while self.i < len(self.text) and self.text[self.i].isspace():
            self.i += 1

    def _peek(self) -> str:
        self._skip()
        return self.text[self.i] if self.i < len(self.text) else ""

    def _or(self) -> WhenExpr:
        left = self._and()
        kids = [left]
        while self._peek() == "|":
            self.i += 1
            kids.append(self._and())
        return kids[0] if len(kids) == 1 else WhenExpr("or", kids=tuple(kids))

    def _and(self) -> WhenExpr:
        left = self._unary()
        kids = [left]
        while self._peek() == "&":
            self.i += 1
            kids.append(self._unary())
        return kids[0] if len(kids) == 1 else WhenExpr("and", kids=tuple(kids))

    def _unary(self) -> WhenExpr:
        ch = self._peek()
        if ch == "!":
            self.i += 1
            return WhenExpr("not", kids=(self._unary(),))
        if ch == "(":
            self.i += 1
            inner = self._or()
            if self._peek() != ")":
                raise WhenError("when 里的括号没闭合", '补一个 ")"')
            self.i += 1
            return inner
        return self._name()

    def _name(self) -> WhenExpr:
        self._skip()
        match = _NAME_RE.match(self.text, self.i)
        if not match:
            raise WhenError(
                f"when 在位置 {self.i} 期望一个名字，实际是 {self.text[self.i:self.i + 12]!r}",
                "名字只能用字母、数字、下划线、点、短横线",
            )
        self.i = match.end()
        return WhenExpr("sig", match.group(0))


# --------------------------------------------------------------------------
# 结构
# --------------------------------------------------------------------------


@dataclass
class RegionCfg:
    name: str
    spec: RegionSpec


@dataclass
class SignalCfg:
    name: str
    region: str
    test: dict
    mode: str = "all"
    cooldown: float = 0.0
    values: bool = False

    def build(self, *, validate_only: bool = False, base_dir: str | None = None):
        from .predicates import build_test

        return build_test(self.test, validate_only=validate_only, mode=self.mode, base_dir=base_dir)


@dataclass
class StateCfg:
    name: str
    when: WhenExpr
    stable_frames: int = 1
    on_change_only: bool = True


@dataclass
class ActionCfg:
    on: tuple[str, ...]
    do: dict[str, Any]
    when: str = "appear"  # appear / disappear / change / value / any
    text: str = ""
    cooldown: float = 0.0
    repeat: int = 1
    raw: dict = field(default_factory=dict)


@dataclass
class ServerCfg:
    host: str = "127.0.0.1"
    port: int = 8787
    lock_port: int = 8788


@dataclass
class RuleSet:
    version: int
    fps: float
    regions: dict[str, RegionCfg]
    signals: dict[str, SignalCfg]
    states: dict[str, StateCfg]
    actions: list[ActionCfg]
    server: ServerCfg
    path: str = "rules.yaml"
    text: str = ""
    warnings: list[Issue] = field(default_factory=list)
    name: str = ""

    def signal_names(self) -> list[str]:
        return list(self.signals)

    def state_names(self) -> list[str]:
        return list(self.states)

    def signature(self) -> str:
        """热重载用：内容没变就别重建引擎。"""
        return self.text


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------


def load_rules(path: str) -> RuleSet:
    with open(path, "r", encoding="utf-8") as fp:
        text = fp.read()
    rules = loads_rules(text, path=path)
    rules.text = text
    return rules


def loads_rules(text: str, path: str = "rules.yaml") -> RuleSet:
    index = _line_index(text)
    ctx = _Ctx(path, index)

    try:
        raw = _yaml_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        issue = Issue(
            where="(YAML)",
            msg=str(getattr(exc, "problem", exc)).splitlines()[0],
            hint=_yaml_hint(str(getattr(exc, "problem", ""))),
            line=(mark.line + 1) if mark is not None else None,
        )
        raise RulesError([issue], path) from None

    if raw is None:
        raise RulesError([Issue("(根)", "文件是空的", "抄一份 rules/example.yaml 开始改", 1)], path)
    if not isinstance(raw, Mapping):
        raise RulesError(
            [Issue("(根)", f"顶层应该是字典，实际是 {type(raw).__name__}", "最外层写 version/fps/regions/...", 1)],
            path,
        )

    _check_top_keys(raw, ctx)

    version = raw.get("version")
    if version != 1:
        ctx.err(
            ("version",),
            f"version 必须是 1，实际是 {version!r}",
            "现在只有一套契约，写 version: 1",
        )

    fps = raw.get("fps", 10)
    if isinstance(fps, bool) or not isinstance(fps, (int, float)):
        ctx.err(("fps",), f"fps 应该是数字，实际是 {fps!r}", "例如 fps: 10")
        fps = 10
    elif not (0.2 <= float(fps) <= 60):
        ctx.err(("fps",), f"fps={fps} 超出合理范围（0.2 ~ 60）", "10 帧够用；30 帧以上 CPU 会很吃力")
        fps = 10

    server = _parse_server(raw.get("server"), ctx)

    regions = _parse_regions(raw.get("regions"), ctx)
    signals = _parse_signals(raw.get("signals"), regions, ctx)
    states = _parse_states(raw.get("states"), signals, ctx)
    actions = _parse_actions(raw.get("actions"), states, ctx)

    name = raw.get("name")
    if name is not None and not isinstance(name, str):
        ctx.err(("name",), "name 应该是字符串", '例如 name: "HIS 危急值看板"')

    errors = [i for i in ctx.issues if i.severity == "error"]
    if errors:
        raise RulesError(errors, path)

    return RuleSet(
        version=int(version or 1),
        fps=float(fps),
        regions=regions,
        signals=signals,
        states=states,
        actions=actions,
        server=server,
        path=path,
        text=text,
        warnings=[i for i in ctx.issues if i.severity == "warn"],
        name=name or "",
    )


def _yaml_hint(problem: str) -> str:
    low = problem.lower()
    if "tab" in low or "\\t" in problem:
        return "YAML 不吃 Tab，缩进改成空格（通常是 2 个）"
    if "mapping" in low or "colon" in low:
        return "键值之间要有一个空格：rect: [0, 0, 1, 1]"
    if "found character" in low or "'<'" in low:
        return "值里有特殊字符时用引号包起来"
    return "检查缩进和冒号后的空格"


def _check_top_keys(raw: Mapping, ctx: _Ctx) -> None:
    for key in raw:
        if key not in TOP_KEYS:
            guess = difflib.get_close_matches(str(key), sorted(TOP_KEYS), n=1, cutoff=0.4)
            ctx.err(
                (key,),
                f"顶层字段 {key!r} 不认识",
                f"应该是 {TOP_KEYS} 里的一个" + (f"；是不是想写 {guess[0]!r}？" if guess else ""),
            )


def _parse_server(raw: Any, ctx: _Ctx) -> ServerCfg:
    cfg = ServerCfg()
    if raw is None:
        return cfg
    if not isinstance(raw, Mapping):
        ctx.err(("server",), "server 应该是字典", "例如 server: {port: 8787}")
        return cfg

    for key in raw:
        if key not in {"host", "port", "lock_port"}:
            ctx.err(("server", key), f"server 里不认识 {key!r}", "可用：host / port / lock_port")

    host = raw.get("host", cfg.host)
    if not isinstance(host, str) or not host:
        ctx.err(("server", "host"), "host 应该是非空字符串", '本地用 "127.0.0.1"')
    else:
        cfg.host = host

    for key, attr in (("port", "port"), ("lock_port", "lock_port")):
        if key not in raw:
            continue
        val = raw[key]
        if isinstance(val, bool) or not isinstance(val, int):
            ctx.err(("server", key), f"{key} 应该是整数，实际是 {val!r}", "例如 8787")
            continue
        if not (1024 <= val <= 65535):
            ctx.err(("server", key), f"{key}={val} 不在 1024~65535", "换一个高位端口，别用系统保留的")
            continue
        setattr(cfg, attr, val)

    if cfg.port == cfg.lock_port:
        ctx.err(("server", "lock_port"), "port 和 lock_port 不能相同", "锁端口默认就是 port+1")
        cfg.lock_port = cfg.port + 1
    return cfg


def _parse_regions(raw: Any, ctx: _Ctx) -> dict[str, RegionCfg]:
    if raw is None:
        ctx.err(("regions",), "缺少 regions", "至少定义一个要看的区域")
        return {}
    if not isinstance(raw, Mapping) or not raw:
        ctx.err(("regions",), "regions 应该是非空字典", "例如 regions: {alarm: {monitor: primary, rect: [...]}}")
        return {}

    out: dict[str, RegionCfg] = {}
    for name, body in raw.items():
        if not isinstance(name, str) or not name:
            ctx.err(("regions", name), f"区域名 {name!r} 不合法", "用英文字母打头的名字，方便在 signal 里引用")
            continue
        try:
            spec = parse_region(name, body)
        except RegionError as exc:
            ctx.err(("regions", name), exc.message, exc.hint)
            continue
        out[name] = RegionCfg(name=name, spec=spec)
    return out


def _parse_signals(raw: Any, regions: dict[str, RegionCfg], ctx: _Ctx) -> dict[str, SignalCfg]:
    if raw is None:
        ctx.err(("signals",), "缺少 signals", "至少定义一条判据")
        return {}
    if not isinstance(raw, Mapping) or not raw:
        ctx.err(("signals",), "signals 应该是非空字典", "例如 signals: {red_alarm: {region: alarm, test: {...}}}")
        return {}

    out: dict[str, SignalCfg] = {}
    used_regions: dict[str, str] = {}
    for name, body in raw.items():
        if not isinstance(name, str) or not name:
            ctx.err(("signals", name), f"signal 名 {name!r} 不合法")
            continue
        if not isinstance(body, Mapping):
            ctx.err(
                ("signals", name),
                f"signal {name!r} 应该是字典，实际是 {type(body).__name__}",
                "写成 {region: alarm, test: {color_ratio: {...}}}",
            )
            continue

        for key in body:
            if key not in {"region", "test", "mode", "cooldown", "values"}:
                ctx.err(("signals", name, key), f"signal 里不认识 {key!r}", "可用：region / test / mode / cooldown / values")

        region = body.get("region")
        if region is None:
            ctx.err(("signals", name, "region"), f"signal {name!r} 没写 region", "指向 regions 里的一个名字")
        elif not isinstance(region, str):
            ctx.err(("signals", name, "region"), "region 应该是字符串", '例如 region: alarm')
        elif region not in regions:
            opts = difflib.get_close_matches(region, list(regions), n=1, cutoff=0.4)
            known = ("已定义：" + ", ".join(sorted(regions))) if regions else "当前没有任何 region 通过校验，先修上面的 regions"
            ctx.err(
                ("signals", name, "region"),
                f"region={region!r} 在 regions 里不存在",
                known + (f"；是不是想写 {opts[0]!r}？" if opts else ""),
            )
        else:
            used_regions.setdefault(region, name)

        mode = body.get("mode", "all")
        if mode not in ("all", "any"):
            ctx.err(("signals", name, "mode"), f"mode={mode!r} 只能是 all 或 any", "默认 all（判据全过），any 是任一命中")
            mode = "all"

        test = body.get("test")
        if test is None:
            ctx.err(("signals", name, "test"), f"signal {name!r} 没写 test", "test 里放判据")
            continue
        try:
            build_test(test, validate_only=True, mode=mode)
        except PredicateSpecError as exc:
            ctx.err(("signals", name, "test"), exc.message, exc.hint)

        cooldown = body.get("cooldown", 0)
        if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)):
            ctx.err(("signals", name, "cooldown"), f"cooldown 应该是秒数，实际是 {cooldown!r}", "例如 cooldown: 2.0")
            cooldown = 0.0
        elif not (0 <= float(cooldown) <= 3600):
            ctx.err(("signals", name, "cooldown"), f"cooldown={cooldown} 不合理（0~3600 秒）", "2~10 秒够用")
            cooldown = 0.0

        values = body.get("values", False)
        if not isinstance(values, bool):
            ctx.err(("signals", name, "values"), "values 应该是 true/false", "开启后每秒发一次 value 事件")
            values = False

        out[name] = SignalCfg(
            name=name,
            region=region or "",
            test=dict(test) if isinstance(test, Mapping) else {},
            mode=mode,
            cooldown=float(cooldown),
            values=values,
        )
    for region in regions:
        if region not in used_regions:
            ctx.warn(("regions", region), f"区域 {region!r} 定义了但没有任何 signal 用它", "删掉或者给 signal 用上，能省一次抓屏")
    return out


def _parse_states(raw: Any, signals: dict[str, SignalCfg], ctx: _Ctx) -> dict[str, StateCfg]:
    if raw is None:
        ctx.err(("states",), "缺少 states", "至少定义一个组合状态")
        return {}
    if not isinstance(raw, Mapping) or not raw:
        ctx.err(("states",), "states 应该是非空字典", "例如 states: {alarm_on: {when: red_alarm, stable_frames: 3}}")
        return {}

    out: dict[str, StateCfg] = {}
    for name, body in raw.items():
        if not isinstance(name, str) or not name:
            ctx.err(("states", name), f"state 名 {name!r} 不合法")
            continue
        if not isinstance(body, Mapping):
            ctx.err(("states", name), f"state {name!r} 应该是字典", "写成 {when: red_alarm, stable_frames: 3}")
            continue

        for key in body:
            if key not in {"when", "stable_frames", "on_change_only"}:
                ctx.err(("states", name, key), f"state 里不认识 {key!r}", "可用：when / stable_frames")

        when_raw = body.get("when")
        if when_raw is None:
            ctx.err(("states", name, "when"), f"state {name!r} 没写 when", "when 指向 signal 或其它 state")
            continue
        try:
            expr = parse_when(when_raw)
        except WhenError as exc:
            ctx.err(("states", name, "when"), exc.msg, exc.hint)
            continue

        unknown = expr.signals() - set(signals) - set(out)
        if unknown:
            opts = difflib.get_close_matches(sorted(unknown)[0], list(signals), n=1, cutoff=0.4)
            ctx.err(
                ("states", name, "when"),
                f"when 里引用了不存在的信号：{sorted(unknown)}",
                "已定义 signal：" + ", ".join(sorted(signals)) + (f"；是不是想写 {opts[0]!r}？" if opts else ""),
            )

        stable = body.get("stable_frames", 1)
        if isinstance(stable, bool) or not isinstance(stable, int):
            ctx.err(("states", name, "stable_frames"), f"stable_frames 应该是整数，实际是 {stable!r}", "3 帧 @10fps = 0.3 秒")
            stable = 1
        elif not (1 <= stable <= 300):
            ctx.err(("states", name, "stable_frames"), f"stable_frames={stable} 不合理（1~300）", "3~10 比较常用")
            stable = max(1, min(300, stable))

        on_change = body.get("on_change_only", True)
        if not isinstance(on_change, bool):
            ctx.err(("states", name, "on_change_only"), "on_change_only 应该是 true/false")
            on_change = True

        out[name] = StateCfg(name=name, when=expr, stable_frames=stable, on_change_only=on_change)
    return out


def _parse_actions(raw: Any, states: dict[str, StateCfg], ctx: _Ctx) -> list[ActionCfg]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        ctx.err(("actions",), "actions 应该是列表", "每一项写成 {on: alarm_on, do: {webhook: ...}}")
        return []

    out: list[ActionCfg] = []
    for i, item in enumerate(raw):
        path = ("actions", i)
        if not isinstance(item, Mapping):
            ctx.err(path, f"第 {i + 1} 条 action 应该是字典，实际是 {type(item).__name__}")
            continue
        extra = set(item) - {"on", "do", "when", "text", "repeat", "cooldown"}
        if extra:
            ctx.err(path, f"action 里不认识 {sorted(extra)}", "可用：on / do / when / text")

        on = item.get("on")
        targets: tuple[str, ...]
        if isinstance(on, str):
            targets = (on,)
        elif isinstance(on, (list, tuple)) and on and all(isinstance(v, str) for v in on):
            targets = tuple(on)
        else:
            ctx.err(path, f"on 应该是 state 名字或名字列表，实际是 {on!r}", "例如 on: alarm_on")
            continue

        for target in targets:
            if target not in states:
                opts = difflib.get_close_matches(target, list(states), n=1, cutoff=0.4)
                ctx.err(
                    path,
                    f"on={target!r} 不是已定义的 state",
                    "已定义 state：" + ", ".join(sorted(states)) + (f"；是不是想写 {opts[0]!r}？" if opts else ""),
                )

        do = item.get("do")
        if not isinstance(do, Mapping) or not do:
            ctx.err(path, "do 应该是非空字典", "例如 do: {webhook: \"https://...\", text: \"...\"}")
            continue

        # 契约里 text 是写在 do 里的（短信/日志正文），不是独立的动作类型
        kinds = set(do) - {"text"}
        unknown = kinds - ACTION_KINDS
        if unknown:
            guess = difflib.get_close_matches(sorted(unknown)[0], sorted(ACTION_KINDS), n=1, cutoff=0.4)
            ctx.err(
                path,
                f"do 里有不认识的动作用法：{sorted(unknown)}",
                "可用：" + ", ".join(sorted(ACTION_KINDS)) + (f"；是不是想写 {guess[0]!r}？" if guess else ""),
            )

        _check_do(path, do, ctx)

        when = item.get("when", "appear")
        if when not in ("appear", "disappear", "change", "value", "any"):
            ctx.err(path, f"when={when!r} 不合法", "可选：appear / disappear / change / value / any")
            when = "appear"

        cooldown = item.get("cooldown", 0)
        try:
            cooldown = float(cooldown)
            if cooldown < 0:
                raise ValueError
        except (TypeError, ValueError):
            ctx.err(path, f"cooldown 应该是 ≥0 的秒数，实际是 {cooldown!r}", "例如 cooldown: 30")
            cooldown = 0.0

        repeat = item.get("repeat", 1)
        try:
            repeat = int(repeat)
            if repeat < 1:
                raise ValueError
        except (TypeError, ValueError):
            ctx.err(path, f"repeat 应该是 ≥1 的整数，实际是 {repeat!r}", "默认 1；>1 表示状态保持期间重复触发 N 次")
            repeat = 1

        out.append(
            ActionCfg(
                on=targets,
                do=dict(do),
                when=str(when),
                text=str(item.get("text") or do.get("text") or ""),
                cooldown=cooldown,
                repeat=repeat,
                raw=dict(item),
            )
        )
    return out


def _check_do(path: tuple, do: Mapping, ctx: _Ctx) -> None:
    """每个动作的关键字段先挑出来，别等到跑起来才 TypeError。"""
    if "webhook" in do:
        url = do["webhook"]
        if not isinstance(url, str) or not re.match(r"^https?://", url):
            ctx.err(path, f"webhook 需要 http(s) 地址，实际是 {url!r}", '例如 "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"')
    if "press" in do:
        keys = do["press"]
        if isinstance(keys, str):
            if not keys.strip():
                ctx.err(path, "press 的按键是空的", '例如 "ctrl+s" 或 ["tab", "enter"]')
        elif isinstance(keys, (list, tuple)):
            if not keys or not all(isinstance(k, str) and k.strip() for k in keys):
                ctx.err(path, "press 的按键列表里有空值或非字符串", '例如 press: ["ctrl", "s"]')
        else:
            ctx.err(path, f"press 应该是字符串或列表，实际是 {keys!r}", '例如 press: "ctrl+s"')
    if "exec" in do:
        cmd = do["exec"]
        if isinstance(cmd, str):
            if not cmd.strip():
                ctx.err(path, "exec 的命令是空的")
        elif isinstance(cmd, (list, tuple)):
            if not cmd or not all(isinstance(c, (str, int, float)) for c in cmd):
                ctx.err(path, "exec 的命令列表必须是字符串", '例如 exec: ["python", "say.py", "文本"]')
        else:
            ctx.err(path, f"exec 应该是字符串或列表，实际是 {cmd!r}")
    if "mqtt" in do:
        body = do["mqtt"]
        if isinstance(body, str):
            if not body.strip():
                ctx.err(path, "mqtt 的 topic 是空的")
        elif isinstance(body, Mapping):
            if not body.get("topic"):
                ctx.err(path, "mqtt 少了 topic", '例如 mqtt: {topic: "pixelbus/alarm", payload: "on"}')
        else:
            ctx.err(path, "mqtt 应该是 topic 字符串或字典")
    for key in ("text", "payload"):
        if key in do and not isinstance(do[key], str):
            ctx.err(path, f"do.{key} 应该是字符串，实际是 {do[key]!r}", "需要嵌套结构就先序列化成 JSON 字符串")
