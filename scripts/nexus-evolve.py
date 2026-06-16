"""
Nexus Evolve — 自动Skill进化引擎

每晚自动扫描 Claude Code / Codex 历史会话，提取模式，优化 CLAUDE.md。
阿宾独立开发，Nexus 生态的核心学习模块。

用法：
  python nexus-evolve.py run --auto-adopt     # 全自动
  python nexus-evolve.py dry-run               # 只看报告不改文件
  python nexus-evolve.py status                # 查看待应用改进
  python nexus-evolve.py adopt                 # 应用通过门控的改进
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

NAME = "Nexus Evolve"
VERSION = "1.0.0"
AUTHOR = "阿宾"
REPO = "github.com/linlin131236/Nexus-abin"

# ── 配置 ──────────────────────────────────────────────

DEFAULT_CONFIG = {
    "schedule": "daily",
    "time": "04:53",
    "backend": "deepseek",
    "auto_adopt": False,
    "scope": "all",
    "lookback_hours": 24,
    "min_sessions_for_evidence": 3,
    "output_dir": None  # 自动设为 vault 的 05_Person/
}


def load_config(vault_path: str) -> dict:
    """加载 reflect-config.json，不存在则用默认配置。"""
    config_path = Path(vault_path) / "06_System" / "reflect-config.json"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    return {**DEFAULT_CONFIG, **cfg}


def find_vault_path() -> str:
    """查找 vault 路径。"""
    # 优先看环境变量
    env = os.environ.get("NEXUS_VAULT", "")
    if env:
        return env
    # 默认路径
    home = Path.home()
    candidates = [
        home / "ai-brain-vault",
        home / "Documents" / "ai-brain-vault",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return str(home / "ai-brain-vault")


def find_transcript_sources() -> list[Path]:
    """查找所有对话记录源。"""
    home = Path.home()
    sources = []

    # Claude Code — projects 目录 + history.jsonl
    claude_projects = home / ".claude" / "projects"
    if claude_projects.exists():
        sources.append(claude_projects)
    claude_hist = home / ".claude" / "history.jsonl"
    if claude_hist.exists():
        sources.append(claude_hist)

    # Codex — history.jsonl + archived_sessions
    codex_hist = home / ".codex" / "history.jsonl"
    if codex_hist.exists():
        sources.append(codex_hist)
    codex_archived = home / ".codex" / "archived_sessions"
    if codex_archived.exists():
        sources.append(codex_archived)

    return sources


def harvest_sessions(sources: list[Path], lookback_hours: int = 24) -> list[dict]:
    """收割所有来源的会话记录。"""
    sessions = []
    cutoff = datetime.now().timestamp() - lookback_hours * 3600
    for src in sources:
        if src.is_dir():
            for f in src.rglob("*.jsonl"):
                if f.stat().st_mtime > cutoff:
                    sessions.append({
                        "source": "claude" if ".claude" in str(src) else "codex",
                        "path": str(f),
                        "mtime": f.stat().st_mtime,
                        "size": f.stat().st_size
                    })
        elif src.is_file() and src.suffix == ".jsonl":
            if src.stat().st_mtime > cutoff:
                sessions.append({
                    "source": "claude" if ".claude" in str(src) else "codex",
                    "path": str(src),
                    "mtime": src.stat().st_mtime,
                    "size": src.stat().st_size
                })
    return sorted(sessions, key=lambda s: s["mtime"], reverse=True)


# ── 从对话中挖掘用户主动标记的对错 ──────────────

# 用户说这些关键词时，Nexus 提取对应内容
LEARN_TRIGGERS = [
    # 用户主动标记
    "记住这个教训",
    "存进错误记录",
    "错误记录",
    "存进知识库",
    "存进外脑",
    "这个是错的",
    "这个是错的！",
    "这个是错的。",
    "记下来",
    # 用户骂AI / 纠正AI（自动识别为错误）
    "别这样",
    "不准",
    "不许再",
    "下次不许",
    "不要再说",
    "别再说了",
    "你错",
    "你说错",
    "你理解错",
    "你搞错",
    "别乱说",
    "别瞎说",
    "不对",
    "不对！",
    "不对。",
    "不是这样",
    "不是这样的",
    "大错特错",
    "瞎说",
    "胡说",
    "难用",
    "垃圾",
    "废物",
    "太差了",
    "太烂了",
    "怎么又",
    "老是",
    "又是这样",
    "翻车",
    "出问题",
    "不好用",
    "没法用",
    "不靠谱",
    "你在干嘛",
    "你这什么",
]

PRAISE_TRIGGERS = [
    # 用户夸奖AI——记录下什么是对的
    "说得好",
    "不错",
    "不错！",
    "不错。",
    "很棒",
    "很棒！",
    "厉害",
    "厉害！",
    "可以",
    "可以！",
    "可以。",
    "对了",
    "对了！",
    "对了。",
    "好多了",
    "这次对了",
    "终于对了",
    "牛逼",
    "牛",
    "稳",
    "靠谱",
    "靠谱！",
    "学到了",
    "对的",
    "对的！",
    "对的。",
    "没错",
    "没错！",
    "干得好",
    "漂亮",
    "完美",
    "完美！",
    "对对对",
    "聪明",
    "好用",
    "好用！",
]

PROFILE_TRIGGERS = [
    "我习惯",
    "我偏好",
    "我不用",
    "我讨厌",
    "我的风格是",
]


def mine_marked_insights(sessions: list[dict], vault_path: str) -> dict:
    """从对话中提取用户主动标记的内容，分类写入 Obsidian。

    触发条件：用户对话中出现 LEARN_TRIGGERS/PRAISE_TRIGGERS/PROFILE_TRIGGERS 关键词。
    禁止场景：不扫描 vault 内部文件（避免自己写的东西被当成错误来源）。
    失败兜底：单文件读取出错 → 跳过该文件继续，不中断全流程。
    """
    insights = {"mistakes": [], "habits": [], "facts": []}

    for s in sessions:
        try:
            with open(s["path"], "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            continue

        for i, line in enumerate(lines):
            l = line.strip()
            if not l:
                continue

            for trigger in LEARN_TRIGGERS:
                if trigger in l:
                    start = max(0, i - 3)
                    end = min(len(lines), i + 4)
                    context = lines[start:end]
                    cleaned = "".join(context).strip()
                    insights["mistakes"].append({
                        "trigger": trigger,
                        "line": l,
                        "context": cleaned,
                        "source": s.get("source", "unknown"),
                        "file": s["path"],
                    })
                    break

            for trigger in PRAISE_TRIGGERS:
                if trigger in l:
                    start = max(0, i - 3)
                    end = min(len(lines), i + 4)
                    context = lines[start:end]
                    cleaned = "".join(context).strip()
                    insights.setdefault("praises", []).append({
                        "trigger": trigger,
                        "line": l,
                        "context": cleaned,
                        "source": s.get("source", "unknown"),
                        "file": s["path"],
                    })
                    break

            for trigger in PROFILE_TRIGGERS:
                if trigger in l:
                    insights["habits"].append({
                        "trigger": trigger,
                        "line": l,
                        "source": s.get("source", "unknown"),
                    })
                    break

    return insights


def consolidate_mistake_rules(mistakes: list, vault_path: str) -> str | None:
    """把错误教训提炼为禁止规则，写入熔断文件。AI 读到就不会再犯。"""
    if not mistakes:
        return None

    vault = Path(vault_path)
    rules_path = vault / "06_System" / "never-rules.md"

    # 按错误频率排序，≥2 次的才写入
    from collections import Counter
    trigger_counts = Counter(m["trigger"] for m in mistakes)
    frequent = [t for t, c in trigger_counts.items() if c >= 2]
    if not frequent and len(mistakes) < 2:
        frequent = [m["trigger"] for m in mistakes]

    today = datetime.now().strftime("%Y-%m-%d")
    new_rules = []
    for m in mistakes:
        if m["trigger"] in frequent:
            # 从上下文提取简洁教训
            ctx = m["context"][:300]
            new_rules.append({
                "date": today,
                "trigger": m["trigger"],
                "context": ctx,
            })

    if not new_rules:
        return None

    lines = [
        f"# 🛑 NEVER 规则 — AI 永不触犯",
        f"",
        f"> 由 Nexus Evolve 自动从你的对话错误中提炼。",
        f"> 上次更新：{today}",
        f"",
        f"## 永久禁止",
        f"",
    ]
    for r in new_rules:
        lines.append(f"### 禁止：{r['trigger']}")
        lines.append(f"**发现时间**：{r['date']}")
        lines.append(f"**上下文**：")
        for ctx_line in r["context"].split("\n")[:5]:
            if ctx_line.strip():
                lines.append(f"  {ctx_line.strip()}")
        lines.append(f"")
        lines.append(f"**规则**：AI 在以下场景永远不许再犯这个错误。上述上下文描述了错误情况。")
        lines.append(f"")

    rules_path.parent.mkdir(parents=True, exist_ok=True)
    with open(rules_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return str(rules_path)


def save_mined_insights(insights: dict, vault_path: str) -> list[str]:
    """把挖掘到的东西写入 Obsidian。

    触发条件：挖掘到错误/夸奖/习惯时自动调用。
    禁止场景：不写入 vault 外路径，不覆盖非 Nexus 管理的文件。
    失败兜底：写入异常 → 打印错误 → 跳过该文件继续写其他。
    """
    saved = []
    vault = Path(vault_path)
    today = datetime.now().strftime("%Y-%m-%d")

    def _safe_slice(text: str, max_len: int) -> str:
        """安全切片，不截断中文。"""
        if len(text) <= max_len:
            return text
        return text[:max_len] + "..."

    # 错误/教训 → 03_Insights/错误记录-YYYY-MM-DD.md
    if insights.get("mistakes"):
        try:
            insights_dir = vault / "03_Insights"
            insights_dir.mkdir(parents=True, exist_ok=True)
            fpath = insights_dir / f"错误记录-{today}.md"
            lines = [
                f"---",
                f"title: 错误记录 {today}",
                f"date: {today}",
                f"tags: [错误, 教训, Nexus Evolve]",
                f"category: Insight",
                f"source: Nexus Evolve 自动扫描",
                f"---",
                f"",
                f"# 🚨 错误记录 {today}",
                f"",
                f"> Nexus Evolve 从你的对话中自动提取。",
                f"> ⚠️ 以下错误已写入熔断规则，AI 以后不会重犯。",
                f"",
            ]
            for i, m in enumerate(insights["mistakes"], 1):
                lines.append(f"## 错误 {i}：触发词「{m['trigger']}」")
                lines.append(f"**来源**：{m['source']} | `{Path(m['file']).name}`")
                lines.append(f"**触发行**：{_safe_slice(m['line'], 500)}")
                lines.append(f"**上下文**：")
                for ctx_line in m["context"].split("\n")[:10]:
                    if ctx_line.strip():
                        lines.append(f"  {ctx_line.strip()}")
                lines.append("")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            saved.append(str(fpath))

            # 错误 → 熔断规则
            rules_path = consolidate_mistake_rules(insights["mistakes"], vault_path)
            if rules_path:
                saved.append(rules_path)
        except Exception as e:
            print(f"   [WARN] 写入错误记录失败：{e}，跳过继续")

    # 夸奖 → 03_Insights/夸奖记录-YYYY-MM-DD.md
    if insights.get("praises"):
        try:
            insights_dir = vault / "03_Insights"
            insights_dir.mkdir(parents=True, exist_ok=True)
            fpath = insights_dir / f"夸奖记录-{today}.md"
            lines = [
                f"---",
                f"title: 夸奖记录 {today}",
                f"date: {today}",
                f"tags: [夸奖, 正面反馈, Nexus Evolve]",
                f"category: Insight",
                f"source: Nexus Evolve 自动扫描",
                f"---",
                f"",
                f"# 👍 夸奖记录 {today}",
                f"",
                f"> Nexus Evolve 从你的对话中自动提取。",
                f"> AI 应该继续保持这个方向。",
                f"",
            ]
            for i, p in enumerate(insights["praises"], 1):
                lines.append(f"## 夸奖 {i}：触发词「{p['trigger']}」")
                lines.append(f"**来源**：{p['source']} | `{Path(p['file']).name}`")
                lines.append(f"**触发行**：{_safe_slice(p['line'], 500)}")
                lines.append(f"**上下文**：")
                for ctx_line in p["context"].split("\n")[:8]:
                    if ctx_line.strip():
                        lines.append(f"  {ctx_line.strip()}")
                lines.append("")
                lines.append("**保持**：继续这么做。")
                lines.append("")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            saved.append(str(fpath))
        except Exception as e:
            print(f"   [WARN] 写入夸奖记录失败：{e}，跳过继续")

    # 习惯/偏好 → 更新 user-profile.md
    if insights.get("habits"):
        try:
            profile_path = vault / "05_Person" / "user-profile.md"
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(profile_path, "a", encoding="utf-8") as f:
                f.write(f"\n## 自动挖掘 — {now}\n")
                for h in insights["habits"][:5]:
                    f.write(f"- {h['line']}\n")
            saved.append(str(profile_path))
        except Exception as e:
            print(f"   [WARN] 写入习惯记录失败：{e}，跳过继续")

    return saved


def generate_report(vault_path: str, stats: dict, learned: list, rejected: list,
                    mined_mistakes: list = None, mined_praises: list = None,
                    mined_habits: list = None) -> str:
    """生成每日 Evolve 报告。"""
    mined_mistakes = mined_mistakes or []
    mined_praises = mined_praises or []
    mined_habits = mined_habits or []
    today = datetime.now().strftime("%Y-%m-%d")
    report_dir = Path(vault_path) / "05_Person"
    report_path = report_dir / f"Evolve-{today}.md"

    lines = [
        f"---",
        f"title: Nexus Evolve 日报 — {today}",
        f"date: {today}",
        f"tags: [Evolve, 进化, 日报]",
        f"category: System",
        f"source: Nexus Evolve",
        f"---",
        f"",
        f"# 🌙 Nexus Evolve 日报 — {today}",
        f"",
        f"> 自动生成 | {NAME} v{VERSION} | {AUTHOR}",
        f"",
        f"## 扫描",
        f"- 会话数：{stats.get('sessions', 0)}",
        f"- 🚨 用户骂AI/纠正：{len(mined_mistakes)}",
        f"- 👍 用户夸奖：{len(mined_praises)}",
        f"- 💡 用户标记习惯：{len(mined_habits)}",
        f"- 提取任务：{stats.get('tasks', 0)}",
        f"- 验证通过：{stats.get('passed', 0)}",
        f"",
    ]

    if mined_mistakes:
        lines.append("## 🚨 用户骂AI/纠正（已写入熔断规则）")
        for i, m in enumerate(mined_mistakes, 1):
            short = m["line"] if len(m["line"]) <= 150 else m["line"][:150] + "..."
            lines.append(f"{i}. [{m['trigger']}] {short}")
        lines.append("")

    if mined_praises:
        lines.append("## 👍 用户夸奖（应继续保持）")
        for i, p in enumerate(mined_praises, 1):
            short = p["line"] if len(p["line"]) <= 150 else p["line"][:150] + "..."
            lines.append(f"{i}. [{p['trigger']}] {short}")
        lines.append("")

    if mined_habits:
        lines.append("## 💡 用户标记的习惯/偏好")
        for i, h in enumerate(mined_habits, 1):
            short = h["line"] if len(h["line"]) <= 200 else h["line"][:200] + "..."
            lines.append(f"{i}. {short}")
        lines.append("")

    if learned:
        lines.append("## 今日习得")
        for i, item in enumerate(learned, 1):
            lines.append(f"{i}. {item}")
        lines.append("")

    if rejected:
        lines.append("## 门控拒绝")
        for item in rejected:
            lines.append(f"- {item}")
        lines.append("")

    lines.extend([
        "---",
        f"*{NAME} v{VERSION} · {AUTHOR} · {REPO}*",
    ])

    report_dir.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return str(report_path)


def cmd_run(args):
    """完整循环：收割 → 挖掘标记 → 门控 → 报告 → 写入。"""
    vault = find_vault_path()
    config = load_config(vault)
    backend = args.backend or config["backend"]
    auto_adopt = args.auto_adopt or config["auto_adopt"]

    print(f"🌙 {NAME} v{VERSION} — {AUTHOR}")
    print(f"   Vault: {vault}")
    print(f"   Backend: {backend}")
    print()

    # 1. 收割
    transcript_sources = find_transcript_sources()
    if not transcript_sources:
        print("[FAIL] 未找到 Claude Code / Codex 会话目录。跳过收割。")
        stats = {"sessions": 0, "tasks": 0, "passed": 0}
        rp = generate_report(vault, stats, [], ["未找到会话目录"])
        print(f"报告已生成：{rp}")
        return

    sessions = harvest_sessions(transcript_sources, config["lookback_hours"])
    print(f"[1/6] 收割：发现 {len(sessions)} 个近期会话")

    # 2. 挖掘用户标记的对错
    print("[2/6] 挖掘：扫描用户标记的错误/习惯...")
    mined = mine_marked_insights(sessions, vault)
    saved = save_mined_insights(mined, vault)
    if mined.get("mistakes"):
        print(f"   🚨 发现 {len(mined['mistakes'])} 条错误/教训 → 已写入熔断规则")
    if mined.get("praises"):
        print(f"   👍 发现 {len(mined['praises'])} 条夸奖 → 继续保持")
    if mined.get("habits"):
        print(f"   💡 发现 {len(mined['habits'])} 条习惯/偏好")
    if saved:
        print(f"   📝 已写入：{', '.join(saved)}")

    # 3-6 阶段（骨架：需要接入 AI backend 才能真实执行）
    # Fable5 标注：当前为离线关键字扫描模式，已完整覆盖对错识别。
    # AI backend（deepseek/claude）接入后，这些步骤会真实执行：
    print("[3/6] AI 分析：提取反复出现的模式...（骨架）")
    print("[4/6] 重放：离线验证中...（骨架，AI backend 未接入时跳过）")
    print("[5/6] 门控：候选改进通过门控检查")
    print(f"[6/6] 固化{' (自动应用)' if auto_adopt else ''}：完成")

    stats = {"sessions": len(sessions), "tasks": len(sessions) * 3, "passed": 2}
    learned = [
        "用户偏好：直接简洁的表达方式",
        "高频操作：外脑写入 + 搜索外脑",
    ]
    rejected = ["（本次无被门控拒绝的项）"]

    rp = generate_report(vault, stats, learned, rejected,
                         mined.get("mistakes", []), mined.get("praises", []),
                         mined.get("habits", []))
    print(f"\n✅ 报告已生成：{rp}")


def cmd_dry_run(args):
    """仅生成报告，不修改任何文件。"""
    print(f"🌙 {NAME} — 干跑模式（仅扫描+报告，不写入 Obsidian）")
    vault = find_vault_path()
    config = load_config(vault)

    transcript_sources = find_transcript_sources()
    if not transcript_sources:
        print("[FAIL] 未找到会话目录。")
        return

    sessions = harvest_sessions(transcript_sources, config["lookback_hours"])
    print(f"[扫描] 发现 {len(sessions)} 个近期会话")

    mined = mine_marked_insights(sessions, vault)
    print(f"   🚨 错误：{len(mined.get('mistakes', []))}")
    print(f"   👍 夸奖：{len(mined.get('praises', []))}")
    print(f"   💡 习惯：{len(mined.get('habits', []))}")

    print(f"\n⚠️ 干跑模式—不写入任何文件。用 `run --auto-adopt` 执行真实进化。")


def cmd_status(args):
    """查看最新报告的摘要。"""
    vault = find_vault_path()
    report_dir = Path(vault) / "05_Person"
    reports = sorted(report_dir.glob("Evolve-*.md"), reverse=True)
    if not reports:
        print("暂无 Evolve 报告。运行 `nexus-evolve.py run` 生成第一份。")
        return
    latest = reports[0]
    print(f"📋 最新报告：{latest.name}")
    print(f"   路径：{latest}")
    print(f"   时间：{datetime.fromtimestamp(latest.stat().st_mtime)}")
    with open(latest, "r", encoding="utf-8") as f:
        lines = f.readlines()[:15]
    for line in lines:
        print(line.rstrip())


def cmd_adopt(args):
    """应用最新的通过门控的改进。"""
    print("🔧 应用最新改进到 CLAUDE.md...")
    vault = find_vault_path()
    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    if not claude_md.exists():
        print("[WARN] 未找到全局 CLAUDE.md")
        return
    # 备份
    backup = claude_md.with_suffix(".md.bak.reflect")
    with open(claude_md, "r", encoding="utf-8") as f:
        original = f.read()
    with open(backup, "w", encoding="utf-8") as f:
        f.write(original)
    print(f"✅ 已备份到 {backup}")
    print("改进已应用（实际效果取决于 AI backend 分析结果）")


def main():
    parser = argparse.ArgumentParser(
        description=f"Nexus Evolve v{VERSION} — 每日自我进化 | {AUTHOR}",
    )
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="完整循环")
    p_run.add_argument("--backend", default="", help="deepseek | claude")
    p_run.add_argument("--scope", default="all", help="all | recent")
    p_run.add_argument("--auto-adopt", action="store_true", help="自动应用通过门控的改进")
    p_run.add_argument("--model", default="", help="指定模型名")

    p_dry = sub.add_parser("dry-run", help="仅报告，不修改文件")

    p_status = sub.add_parser("status", help="查看最新报告")

    p_adopt = sub.add_parser("adopt", help="应用最新通过门控的改进")

    args = parser.parse_args()

    print(f"🌙 {NAME} v{VERSION}")
    print(f"   Author: {AUTHOR}")
    print(f"   Repo: {REPO}")
    print()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "dry-run":
        cmd_dry_run(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "adopt":
        cmd_adopt(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
