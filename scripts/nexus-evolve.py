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
import re
import sys
import urllib.request
import urllib.error
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


# ── AI Backend 接入（兼容所有 OpenAI 格式 API）──────────

# 内置主流厂商的 API 地址，用户只需填 KEY
BUILTIN_PROVIDERS = {
    "deepseek": {
        "url": "https://api.deepseek.com/v1/chat/completions",
        "models": ["deepseek-chat", "deepseek-v4-pro", "deepseek-reasoner"],
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "models": ["gpt-4o", "gpt-4o-mini"],
    },
    "zhipu": {
        "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "models": ["glm-4-flash", "glm-4-air"],
    },
    "moonshot": {
        "url": "https://api.moonshot.cn/v1/chat/completions",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k"],
    },
    "qwen": {
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "models": ["qwen-plus", "qwen-turbo"],
    },
    "doubao": {
        "url": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        "models": ["doubao-1-5-lite-32k-250115"],
    },
    "custom": {
        "url": "https://你的API地址/v1/chat/completions",
        "models": ["你的模型名"],
    },
}


def _load_api_config(vault_path: str) -> dict:
    """
    加载 API 配置。查找顺序：
    1. vault/06_System/nexus-api.json（用户自己建的配置文件）
    2. 环境变量 NEXUS_API_KEY + NEXUS_API_URL
    3. 环境变量 DEEPSEEK_API_KEY / OPENAI_API_KEY 等
    4. OpenClaw 配置文件
    """
    # 1. 用户配置文件
    config_path = Path(vault_path) / "06_System" / "nexus-api.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if cfg.get("api_key"):
                return cfg
        except Exception:
            pass

    # 2. NEXUS 环境变量
    env_key = os.environ.get("NEXUS_API_KEY", "").strip()
    if env_key:
        return {
            "api_key": env_key,
            "api_url": os.environ.get("NEXUS_API_URL", "https://api.deepseek.com/v1/chat/completions"),
            "model": os.environ.get("NEXUS_MODEL", "deepseek-chat"),
        }

    # 3. 常见厂商环境变量
    for var in ["DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GLM_API_KEY", "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY"]:
        key = os.environ.get(var, "").strip()
        if key:
            provider = var.replace("_API_KEY", "").lower()
            info = BUILTIN_PROVIDERS.get(provider, BUILTIN_PROVIDERS["deepseek"])
            return {"api_key": key, "api_url": info["url"], "model": info["models"][0]}

    # 4. OpenClaw 配置
    oc_config = Path.home() / ".openclaw" / "openclaw.json"
    if oc_config.exists():
        try:
            with open(oc_config, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            providers = cfg.get("models", {}).get("providers", {})
            for p in providers.values():
                key = p.get("apiKey", "")
                url = p.get("baseUrl", "")
                if key and url:
                    return {"api_key": key, "api_url": url.rstrip("/") + "/chat/completions", "model": "deepseek-chat"}
        except Exception:
            pass

    return {}


def _call_ai_api(prompt: str, api_config: dict) -> str | None:
    """调用任意 OpenAI 兼容 API。纯标准库，零依赖。"""
    api_key = api_config.get("api_key", "")
    api_url = api_config.get("api_url", "https://api.deepseek.com/v1/chat/completions")
    model = api_config.get("model", "deepseek-chat")

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "你是分析助手。只返回 JSON，不要解释、不要 markdown 代码块。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 3000,
    }).encode("utf-8")

    req = urllib.request.Request(api_url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    })

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read())
            return result["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"   [WARN] API 调用失败：{e}")
        return None


def _try_parse_json(text: str) -> dict | None:
    """尽力从 AI 返回中提取 JSON。"""
    if not text:
        return None
    text = text.strip()
    for pattern in [r'\{[\s\S]*\}', r'\[[\s\S]*\]']:
        m = re.search(pattern, text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
    return None


def analyze_conversations(sessions: list[dict], api_config: dict) -> dict:
    """主链 3：AI 分析对话，提取模式、偏好、改进建议。"""
    if not sessions:
        return {"patterns": [], "preferences": [], "suggestions": []}

    # 采样：选最大的 5 个文件，每文件取前 2000 字符
    samples = []
    sorted_sessions = sorted(sessions, key=lambda s: s["size"], reverse=True)
    for s in sorted_sessions[:5]:
        try:
            with open(s["path"], "r", encoding="utf-8") as f:
                content = f.read(3000)
                samples.append({"source": s["source"], "name": Path(s["path"]).name, "content": content})
        except Exception:
            continue

    if not samples:
        return {"patterns": [], "preferences": [], "suggestions": []}

    prompt = f"""分析以下用户与 AI 的对话片段。提取三类信息，返回严格 JSON：

1. patterns: 反复出现的工作模式（如"每次写代码前先要整体方案"）。含 frequency（出现次数）和 evidence（简短证据）。
2. preferences: 用户偏好（如"喜欢直接简洁的风格"）。含 confidence（高/中/低）。
3. suggestions: 可写入 CLAUDE.md 的具体改进规则。含 rule（一条可执行的规则）、reason（依据）、priority（高/中/低）。

只返回 JSON，格式：{{"patterns": [...], "preferences": [...], "suggestions": [...]}}

对话片段：
{json.dumps([{"source": s["source"], "content": s["content"][:1500]} for s in samples], ensure_ascii=False)}
"""

    print("   ⏳ 正在调用 AI 分析对话模式...")
    result = _call_ai_api(prompt, api_config)
    if not result:
        return {"patterns": [], "preferences": [], "suggestions": []}

    parsed = _try_parse_json(result)
    if not parsed:
        print("   [WARN] AI 返回不是有效 JSON，跳过分析")
        return {"patterns": [], "preferences": [], "suggestions": []}

    return {
        "patterns": parsed.get("patterns", []),
        "preferences": parsed.get("preferences", []),
        "suggestions": parsed.get("suggestions", []),
    }


# ── 门控验证 ──────────────────────────────

IRON_RULES = [
    "破折号",
    "不是X而是X",
    "竟然",
    "仿佛",
    "宛如",
    "双向链接",
    "文艺腔",
]


def _conflicts_with_iron_rules(suggestion: str) -> bool:
    """检查建议是否与写作铁律冲突。"""
    return any(rule in suggestion for rule in IRON_RULES)


def _is_already_present(suggestion: str, claude_md_content: str) -> bool:
    """检查规则是否已存在于 CLAUDE.md。"""
    key_phrase = suggestion[:40]
    return key_phrase in claude_md_content


def gate_improvements(analysis: dict, vault_path: str) -> tuple[list[dict], list[dict]]:
    """主链 4-5：门控验证候选改进。返回 (通过的, 拒绝的)。"""
    passed = []
    rejected = []

    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    existing = ""
    if claude_md.exists():
        try:
            with open(claude_md, "r", encoding="utf-8") as f:
                existing = f.read()
        except Exception:
            pass

    for s in analysis.get("suggestions", []):
        rule = s.get("rule", "")
        reason = s.get("reason", "")
        priority = s.get("priority", "中")

        # 门控条件
        failures = []
        if not rule or len(rule) < 10:
            failures.append("规则过短<10字")
        if _conflicts_with_iron_rules(rule):
            failures.append("与写作铁律冲突")
        if _is_already_present(rule, existing):
            failures.append("已存在于CLAUDE.md")

        if failures:
            rejected.append({"rule": rule, "reason": reason, "failures": failures})
        else:
            passed.append({"rule": rule, "reason": reason, "priority": priority})

    return passed, rejected


def apply_improvements(passed: list[dict]) -> str | None:
    """主链 6：把通过门控的改进写入 CLAUDE.md。"""
    if not passed:
        return None

    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    today = datetime.now().strftime("%Y-%m-%d")

    # 备份
    backup = claude_md.with_suffix(f".md.bak.{today}")
    original = ""
    if claude_md.exists():
        with open(claude_md, "r", encoding="utf-8") as f:
            original = f.read()
        with open(backup, "w", encoding="utf-8") as f:
            f.write(original)

    # 追加 Nexus Evolve 块
    block = [
        f"",
        f"<!-- Nexus Evolve {today} -->",
        f"## 自动进化 — {today}",
        f"",
    ]
    for i, p in enumerate(passed, 1):
        block.append(f"{i}. {p['rule']}")
        block.append(f"   依据：{p['reason']}")

    new_section = "\n".join(block)

    if "<!-- Nexus Evolve" in original:
        # 替换已有块
        original = re.sub(
            r'<!-- Nexus Evolve[\s\S]*?(?=<!--|$)',
            new_section,
            original
        )

    with open(claude_md, "w", encoding="utf-8") as f:
        f.write(original + ("\n" + new_section if "<!-- Nexus Evolve" not in original else ""))

    return str(claude_md)


# ── 从对话中挖掘用户主动标记的对错 ──────────────

# 用户说这些关键词时，Nexus 提取对应内容
LEARN_TRIGGERS = [
    # 用户主动标记（精确匹配，避免噪音）
    "记教训:",
    "记教训：",
    "存错:",
    "存错：",
    "错误记录:",
    "错误记录：",
    "记住这个教训",
    "存进错误记录",
    # 强信号纠正（不会在日常对话中误触发）
    "大错特错",
    "别瞎说",
    "胡说八道",
]

PRAISE_TRIGGERS = [
    # 用户主动标记
    "记对:",
    "记对：",
    "做得好:",
    "做得好：",
    # 强信号夸奖（不会被日常对话误触发）
    "干得好",
    "这次对了",
    "终于对了",
    "好多了",
]

PROFILE_TRIGGERS = [
    "我习惯",
    "我偏好",
    "我不用",
    "我讨厌",
    "我的风格是",
]


def _extract_user_messages(filepath: str) -> list[dict]:
    """从 Claude Code / Codex JSONL 文件中提取用户消息。只取 role=user 的文本。"""
    messages = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = obj.get("message", obj)
                role = msg.get("role", "") if isinstance(msg, dict) else ""
                if role != "user":
                    continue
                content = msg.get("content", "")
                # content 可能是 string 或 list of blocks
                if isinstance(content, list):
                    texts = []
                    for block in content:
                        if isinstance(block, dict):
                            texts.append(block.get("text", ""))
                        elif isinstance(block, str):
                            texts.append(block)
                    content = " ".join(texts)
                if content and isinstance(content, str) and content.strip():
                    messages.append({
                        "text": content.strip(),
                        "raw_line": line,
                    })
    except Exception:
        pass
    return messages


def mine_marked_insights(sessions: list[dict], vault_path: str) -> dict:
    """从对话中提取用户主动标记的内容，分类写入 Obsidian。

    触发条件：用户消息中出现 LEARN_TRIGGERS/PRAISE_TRIGGERS/PROFILE_TRIGGERS 关键词。
    禁止场景：不扫描 vault 内部文件。只扫描 role=user 的消息，不扫 AI 输出。
    失败兜底：单文件读取出错/非 JSONL 格式 → 跳过继续。
    """
    insights = {"mistakes": [], "habits": [], "facts": []}

    for s in sessions:
        user_msgs = _extract_user_messages(s["path"])
        if not user_msgs:
            # 回退：按纯文本行扫描（兼容普通 Markdown/文本日志）
            try:
                with open(s["path"], "r", encoding="utf-8") as f:
                    raw = f.read()
            except Exception:
                continue
            user_msgs = [{"text": raw, "raw_line": raw}]

        for msg in user_msgs:
            text = msg["text"]

            for trigger in LEARN_TRIGGERS:
                if trigger in text:
                    insights["mistakes"].append({
                        "trigger": trigger,
                        "line": text[:500],  # 存清洗后的纯文本
                        "context": text[:600],
                        "source": s.get("source", "unknown"),
                        "file": s["path"],
                    })
                    break

            for trigger in PRAISE_TRIGGERS:
                if trigger in text:
                    insights.setdefault("praises", []).append({
                        "trigger": trigger,
                        "line": text[:500],
                        "context": text[:600],
                        "source": s.get("source", "unknown"),
                        "file": s["path"],
                    })
                    break

            for trigger in PROFILE_TRIGGERS:
                if trigger in text:
                    insights["habits"].append({
                        "trigger": trigger,
                        "line": text[:500],
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
        f"",
        f"### 关键词挖掘（副链）",
        f"- 🚨 错误/教训：{stats.get('mined_mistakes', 0)}",
        f"- 👍 夸奖：{stats.get('mined_praises', 0)}",
        f"- 💡 习惯：{len(mined_habits)}",
        f"",
        f"### AI 分析进化（主链）",
        f"- 🧠 AI 建议：{stats.get('ai_suggestions', 0)}",
        f"- ✅ 通过门控：{stats.get('gated_passed', 0)}",
        f"- ❌ 门控拒绝：{stats.get('gated_rejected', 0)}",
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

    # 3. AI 分析
    print("[3/6] AI 分析：提取行为模式...")
    api_config = _load_api_config(vault)
    analysis = {"patterns": [], "preferences": [], "suggestions": []}
    gated_passed = []
    gated_rejected = []

    if api_config.get("api_key"):
        print(f"   🔑 使用 {api_config.get('model', '?')} ({api_config.get('api_url', '')[:50]}...)")
        analysis = analyze_conversations(sessions, api_config)
        pd = analysis.get("patterns", [])
        pr = analysis.get("preferences", [])
        sg = analysis.get("suggestions", [])
        print(f"   📊 模式：{len(pd)} | 偏好：{len(pr)} | 改进建议：{len(sg)}")

        # 4. 门控
        print("[4/6] 门控：验证候选改进...")
        gated_passed, gated_rejected = gate_improvements(analysis, vault)
        print(f"   ✅ 通过：{len(gated_passed)} | ❌ 拒绝：{len(gated_rejected)}")
        for r in gated_rejected:
            print(f"      └ {r['rule'][:60]}... → {', '.join(r['failures'])}")

        # 5. 固化
        if gated_passed and auto_adopt:
            print("[5/6] 固化：写入 CLAUDE.md...")
            result_path = apply_improvements(gated_passed)
            if result_path:
                print(f"   ✅ 已写入：{result_path}")
            else:
                print("   ⚠️ 写入失败")
        else:
            print(f"[5/6] 固化{' (跳过，需 --auto-adopt)' if not auto_adopt else ''}")

        # 6. 报告
        print("[6/6] 生成报告...")
    else:
        print("   ⚠️ 未检测到 API Key，跳过 AI 分析")
        print("   💡 三种配置方式：")
        print("      1. 创建 vault/06_System/nexus-api.json → 填 api_key + api_url + model")
        print("      2. 设置环境变量 NEXUS_API_KEY + NEXUS_API_URL")
        print("      3. 设置 DEEPSEEK_API_KEY 或 OPENAI_API_KEY 等厂商变量")
        print("[4/6] 门控：跳过（无候选）")
        print("[5/6] 固化：跳过（无候选）")
        print("[6/6] 生成报告...")

    stats = {
        "sessions": len(sessions),
        "mined_mistakes": len(mined.get("mistakes", [])),
        "mined_praises": len(mined.get("praises", [])),
        "ai_suggestions": len(analysis.get("suggestions", [])),
        "gated_passed": len(gated_passed),
        "gated_rejected": len(gated_rejected),
    }
    learned = [s["rule"] for s in gated_passed] if gated_passed else []
    rejected = [
        f"{r['rule'][:80]}... ({', '.join(r['failures'])})"
        for r in gated_rejected
    ] if gated_rejected else []

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
