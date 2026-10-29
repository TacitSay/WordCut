# -*- coding: utf-8 -*-
"""Export the OpenClaw session transcript (SQLite) into resumable artifacts.

Outputs (into E:\WordCut\docs\):
  1. openclaw-transcript-v4.jsonl  -- faithful OpenClaw transcript v4, one event per line,
     decompressing zstd-stored events. Keeps id/parentId chain so any agent can continue.
  2. openclaw-dialogue-numbered.json -- condensed dialogue with stable [Un] numbering so a
     successor agent can cite and continue the conversation.
  3. openclaw-dialogue-numbered.md -- the same, human readable.
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DB = r"C:\Users\YWW\.openclaw\agents\main\agent\openclaw-agent.sqlite"
SID = "8e3e8969-2d12-42bb-a861-b5e181673d41"
OUT = Path(r"E:\WordCut\docs")
TZ = timezone(timedelta(hours=8))

# Automated continuation / status prompts injected by the harness, not user intent.
AUTO_PREFIXES = (
    "Continue the current task from the existing transcript",
    "Refresh this session\u2019s progress card",
    "Refresh this session's progress card",
)


def load_events():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        "select seq, event_json, event_zstd from transcript_events "
        "where session_id=? order by seq",
        (SID,),
    ).fetchall()
    dec = None
    try:
        from compression import zstd as _zstd  # Python 3.14+

        dec = ("codec", _zstd)
    except ImportError:
        pass
    if dec is None:
        try:
            import zstandard as _zstd

            dec = ("module", _zstd.ZstdDecompressor())
        except ImportError:  # pragma: no cover
            dec = None
    events, failed = [], 0
    for seq, raw, blob in rows:
        if raw is not None:
            text = raw
        elif blob is not None and dec is not None:
            try:
                kind, impl = dec
                if kind == "codec":
                    text = impl.decompress(blob).decode("utf-8")
                else:
                    text = impl.decompress(blob, max_output_size=8 * 1024 * 1024).decode("utf-8")
            except Exception:
                failed += 1
                continue
        else:
            failed += 1
            continue
        try:
            ev = json.loads(text)
        except Exception:
            failed += 1
            continue
        ev.setdefault("__seq", seq)
        events.append(ev)
    return events, len(rows), failed


def flatten(message):
    """Return (text, tool_calls, tool_results)."""
    content = message.get("content")
    text, calls, results = [], [], []
    if isinstance(content, str):
        text.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text.append(block.get("text", ""))
            elif kind == "toolCall":
                calls.append(block)
            elif kind == "toolResult":
                results.append(block)
    return "".join(text).strip(), calls, results


def is_auto(message, text):
    """Harness-injected prompts: excluded from user turn numbering.

    - unowned internal system prompts (continuation / progress-card requests)
    - exec completion notifications pasted back as user messages
    """
    if any(text.startswith(p) for p in AUTO_PREFIXES):
        return True
    oc = message.get("__openclaw") or {}
    if oc.get("senderIsOwner") is False:
        return True
    if (message.get("provenance") or {}).get("kind") == "internal_system":
        return True
    return False


def typed(s):
    dt = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ)
    return dt.strftime("%m-%d %H:%M")


def main():
    events, total, failed = load_events()
    print(f"db rows={total} decoded={len(events)} failed={failed}")

    # ---------- 1. faithful v4 jsonl ----------
    v4 = [e for e in events if e.get("type") != "session" or True]
    path_v4 = OUT / "openclaw-transcript-v4.jsonl"
    with path_v4.open("w", encoding="utf-8", newline="\n") as fh:
        for ev in v4:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    print(f"wrote {path_v4} ({len(v4)} events)")

    # ---------- 2. numbered dialogue ----------
    msgs = [e for e in events if e.get("type") == "message"]
    turns = []      # user-visible turns
    pending_tools = []  # assistant text produced alongside tool calls, folded into the turn
    auto_counter = 0
    tool_total = 0
    for ev in msgs:
        m = ev.get("message") or {}
        role = m.get("role")
        text, calls, results = flatten(m)
        if role == "user":
            if not text:
                continue
            if is_auto(m, text):
                auto_counter += 1
                continue
            turns.append({
                "no": f"U{len(turns) + 1}",
                "role": "user",
                "at": typed(ev["timestamp"]) if ev.get("timestamp") else "",
                "seq": ev.get("__seq"),
                "text": text,
                "notes": [],
            })
            pending_tools = []
        elif role == "assistant":
            tool_total += len(calls)
            if text and turns:
                turns[-1].setdefault("_said", [])
                if text in turns[-1]["_said"]:
                    text = ""  # duplicate emission of the same assistant text
                else:
                    turns[-1]["_said"].append(text)
            if text:
                if turns and turns[-1]["role"] == "user":
                    turns.append({
                        "no": f"A{len(turns)}",
                        "role": "assistant",
                        "at": typed(ev["timestamp"]) if ev.get("timestamp") else "",
                        "seq": ev.get("__seq"),
                        "text": text,
                        "tools": [],
                    })
                else:
                    turns[-1]["text"] += "\n\n" + text
            if calls and turns:
                for c in calls:
                    args = c.get("arguments")
                    if isinstance(args, (dict, list)):
                        args = json.dumps(args, ensure_ascii=False)
                    turns[-1].setdefault("tools", []).append(
                        {"name": c.get("name"), "args": (args or "")[:200]}
                    )

    for t in turns:
        t.pop("_said", None)

    doc = {
        "format": "openclaw-dialogue-numbered",
        "version": 1,
        "source": {
            "sqlite": DB,
            "session_id": SID,
            "events_total": total,
            "events_decoded": len(events),
            "messages": len(msgs),
            "tool_calls": tool_total,
        },
        "usage": (
            "Cite turns as [U n] (user) / [A n] (assistant). Reserved numbers: "
            f"auto-continuation prompts skipped ({auto_counter}). "
            "To continue the project, read state.json first."
        ),
        "turns": turns,
    }
    path_json = OUT / "openclaw-dialogue-numbered.json"
    path_json.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {path_json} ({len(turns)} turns, skipped {auto_counter} auto prompts)")

    lines = [
        "# OpenClaw 会话对话记录（带编号）",
        "",
        f"- 会话：`{SID}`",
        f"- 事件：{total} 条（解码 {len(events)}），消息 {len(msgs)} 条，工具调用 {tool_total} 次",
        f"- 编号：`[U n]` = 用户第 n 轮，`[A n]` = 其后助手回复；已跳过 {auto_counter} 条自动续跑提示",
        "",
    ]
    for t in turns:
        lines.append(f"### [{t['no']}] {'用户' if t['role']=='user' else '助手'} · {t['at']} (seq {t['seq']})")
        lines.append("")
        lines.append(t["text"])
        if t.get("tools"):
            names = []
            for c in t["tools"]:
                if c["name"] not in names:
                    names.append(c["name"])
            lines.append("")
            lines.append(f"> 工具调用 {len(t['tools'])} 次（{', '.join(names)}）")
        lines.append("")
    path_md = OUT / "openclaw-dialogue-numbered.md"
    path_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {path_md}")

    print("\nlast turns:")
    for t in turns[-6:]:
        print(f"  [{t['no']}] {t['text'][:80]!r}")


if __name__ == "__main__":
    main()
