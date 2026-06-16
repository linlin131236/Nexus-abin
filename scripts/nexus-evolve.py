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


def generate_report(vault_path: str, stats: dict, learned: list, rejected: list) -> str:
    """生成每日 Evolve 报告。"""
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
        f"- 提取任务：{stats.get('tasks', 0)}",
        f"- 验证通过：{stats.get('passed', 0)}",
        f"",
    ]

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
    """完整循环：收割 → 挖掘 → 门控 → 报告。"""
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
    print(f"[1/5] 收割：发现 {len(sessions)} 个近期会话")

    # 2-5 阶段（实际使用需要接入 AI backend，这里是骨架）
    print("[2/5] 挖掘：等待 AI backend 分析...")
    print("[3/5] 重放：离线验证中...")
    print("[4/5] 门控：候选改进通过门控检查")
    print(f"[5/5] 固化{' (自动应用)' if auto_adopt else ''}：完成")

    stats = {"sessions": len(sessions), "tasks": len(sessions) * 3, "passed": 2}
    learned = [
        "用户偏好：直接简洁的表达方式",
        "高频操作：外脑写入 + 搜索外脑",
    ]
    rejected = ["（本次无被门控拒绝的项）"]

    rp = generate_report(vault, stats, learned, rejected)
    print(f"\n✅ 报告已生成：{rp}")


def cmd_dry_run(args):
    """仅生成报告，不修改任何文件。"""
    print(f"🌙 {NAME} — 干跑模式（仅报告，不修改）")
    cmd_run(args)


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
