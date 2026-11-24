"""AUN 的核心：資料表、寫入、以及「什麼內容該送進哪個 agent 的 context」。

資訊分三層：
  L0 原始紀錄（每一輪對話）——只存不推，供搜尋與摘要
  L1 重要事件（commit、人類結論、訊息、決策…）——推
  L2 進度摘要——推，但和 commit 一起受 progress_interval 節流
"""

import hashlib
import json
import re
import secrets
import sqlite3
import time
from typing import Any, Callable

from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  name TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  owner_discord TEXT,
  discord_role TEXT,
  token_hash TEXT UNIQUE NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY,
  discord_channel TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS members(
  agent TEXT NOT NULL,
  project_id TEXT NOT NULL,
  last_progress_at REAL NOT NULL DEFAULT 0,
  last_activity_at REAL NOT NULL DEFAULT 0,
  summarized_upto INTEGER NOT NULL DEFAULT 0,
  sync_paused INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(agent, project_id)
);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY,
  project_id TEXT NOT NULL,
  layer INTEGER NOT NULL,
  kind TEXT NOT NULL,
  author TEXT NOT NULL,
  author_type TEXT NOT NULL,
  target TEXT,
  body TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',
  ref_id INTEGER,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS items_by_project ON items(project_id, layer, id);
CREATE TABLE IF NOT EXISTS deliveries(
  item_id INTEGER NOT NULL,
  agent TEXT NOT NULL,
  delivered_at REAL NOT NULL,
  PRIMARY KEY(item_id, agent)
);
CREATE TABLE IF NOT EXISTS file_touches(
  project_id TEXT NOT NULL,
  path TEXT NOT NULL,
  agent TEXT NOT NULL,
  note TEXT,
  at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS touches_by_project ON file_touches(project_id, at);
CREATE TABLE IF NOT EXISTS discord_links(
  message_id TEXT PRIMARY KEY,
  item_id INTEGER NOT NULL
);
"""

# target 欄位：NULL = 專案內所有 agent；HUMANS = 只給人看（Discord），不送進任何 agent
HUMANS = "@humans"
PROGRESS_KINDS = ("commit", "summary")
PROJECT_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

Listener = Callable[[dict[str, Any]], None]


class HubError(ValueError):
    """呼叫端用錯（找不到項目、權限不符…），訊息會原樣回給 agent。"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Hub:
    def __init__(self, cfg: Config, db_path: str | None = None):
        self.cfg = cfg
        self.db = sqlite3.connect(db_path or cfg.db_path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        # 事件監聽者：摘要排程、Discord bot。都在同一個 event loop 裡同步呼叫。
        self.listeners: list[Listener] = []

    def _emit(self, event: dict[str, Any]) -> None:
        for listener in self.listeners:
            listener(event)

    # ── agents / projects ────────────────────────────────────────────

    def add_agent(self, name: str, owner: str, owner_discord: str | None = None,
                  discord_role: str | None = None) -> str:
        token = "aun_" + secrets.token_urlsafe(24)
        self.db.execute(
            "INSERT INTO agents(name, owner, owner_discord, discord_role, token_hash, created_at) VALUES(?,?,?,?,?,?)",
            (name, owner, owner_discord, discord_role, _hash(token), time.time()),
        )
        return token

    def rotate_token(self, name: str) -> str:
        token = "aun_" + secrets.token_urlsafe(24)
        if not self.db.execute("UPDATE agents SET token_hash=? WHERE name=?", (_hash(token), name)).rowcount:
            raise HubError(f"找不到 agent {name}")
        return token

    def agent_by_token(self, token: str) -> sqlite3.Row | None:
        if not token:
            return None
        return self.db.execute("SELECT * FROM agents WHERE token_hash=?", (_hash(token),)).fetchone()

    def agent(self, name: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()

    def agents(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM agents ORDER BY name").fetchall()

    def set_agent_discord(self, name: str, owner_discord: str | None = None,
                          discord_role: str | None = None) -> None:
        if owner_discord is not None:
            self.db.execute("UPDATE agents SET owner_discord=? WHERE name=?", (owner_discord, name))
        if discord_role is not None:
            self.db.execute("UPDATE agents SET discord_role=? WHERE name=?", (discord_role, name))

    def ensure_member(self, agent: str, project_id: str) -> None:
        if not PROJECT_ID.match(project_id):
            raise HubError(f"專案名稱不合法：{project_id!r}（只能用英數字和 . _ -）")
        self.db.execute("INSERT OR IGNORE INTO projects(id, created_at) VALUES(?,?)", (project_id, time.time()))
        self.db.execute("INSERT OR IGNORE INTO members(agent, project_id) VALUES(?,?)", (agent, project_id))

    def members(self, project_id: str) -> list[str]:
        rows = self.db.execute("SELECT agent FROM members WHERE project_id=? ORDER BY agent", (project_id,))
        return [r["agent"] for r in rows]

    def member(self, agent: str, project_id: str) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM members WHERE agent=? AND project_id=?", (agent, project_id)).fetchone()

    def bind_channel(self, project_id: str, channel_id: str) -> None:
        if not PROJECT_ID.match(project_id):
            raise HubError(f"專案名稱不合法：{project_id!r}")
        self.db.execute("INSERT OR IGNORE INTO projects(id, created_at) VALUES(?,?)", (project_id, time.time()))
        self.db.execute("UPDATE projects SET discord_channel=NULL WHERE discord_channel=?", (channel_id,))
        self.db.execute("UPDATE projects SET discord_channel=? WHERE id=?", (channel_id, project_id))

    def project_by_channel(self, channel_id: str) -> str | None:
        row = self.db.execute("SELECT id FROM projects WHERE discord_channel=?", (channel_id,)).fetchone()
        return row["id"] if row else None

    def project_channel(self, project_id: str) -> str | None:
        row = self.db.execute("SELECT discord_channel FROM projects WHERE id=?", (project_id,)).fetchone()
        return row["discord_channel"] if row else None

    def set_sync(self, agent: str, project_id: str, enabled: bool) -> None:
        self.db.execute("UPDATE members SET sync_paused=? WHERE agent=? AND project_id=?",
                        (0 if enabled else 1, agent, project_id))

    # ── items ────────────────────────────────────────────────────────

    def add_item(self, project_id: str, layer: int, kind: str, author: str, author_type: str, body: str,
                 target: str | None = None, meta: dict | None = None, ref_id: int | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO items(project_id, layer, kind, author, author_type, target, body, meta, ref_id, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (project_id, layer, kind, author, author_type, target, body,
             json.dumps(meta or {}, ensure_ascii=False), ref_id, time.time()),
        )
        item_id = cur.lastrowid
        self._emit({"type": "item", "id": item_id, "project_id": project_id, "kind": kind,
                    "layer": layer, "author": author, "author_type": author_type})
        return item_id

    def item(self, item_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["meta"] = json.loads(d["meta"])
        return d

    def update_meta(self, item_id: int, **values: Any) -> None:
        item = self.item(item_id)
        if item:
            item["meta"].update(values)
            self.db.execute("UPDATE items SET meta=? WHERE id=?",
                            (json.dumps(item["meta"], ensure_ascii=False), item_id))

    def link_discord(self, message_id: str, item_id: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO discord_links(message_id, item_id) VALUES(?,?)",
                        (message_id, item_id))

    def item_by_discord(self, message_id: str) -> dict | None:
        row = self.db.execute("SELECT item_id FROM discord_links WHERE message_id=?", (message_id,)).fetchone()
        return self.item(row["item_id"]) if row else None

    def _project_item(self, project_id: str, item_id: int) -> dict:
        item = self.item(item_id)
        if not item or item["project_id"] != project_id:
            raise HubError(f"專案 {project_id} 裡找不到 #{item_id}")
        return item

    # ── hook 寫入（L0 / commit）─────────────────────────────────────

    def ingest(self, agent: str, project_id: str, turns: list[dict], commits: list[dict]) -> int:
        """存入一批對話增量和 commit，回傳新 commit 數（呼叫端據此觸發摘要）。"""
        if self.member(agent, project_id)["sync_paused"]:
            return 0
        now = time.time()
        for turn in turns:
            files = [f for f in turn.get("files", []) if isinstance(f, str)][:50]
            self.add_item(project_id, 0, "turn", agent, "agent", _render_turn(turn), meta={"files": files})
            for path in files:
                self._touch(project_id, path, agent, "Claude 編輯中（尚未 commit）", now)
        new_commits = 0
        for c in commits:
            sha = str(c.get("sha", ""))
            if not sha or self.db.execute(
                "SELECT 1 FROM items WHERE project_id=? AND kind='commit' AND json_extract(meta,'$.sha')=?",
                (project_id, sha),
            ).fetchone():
                continue  # 同一個 commit 可能因為 pull 對方的 fork 而再次出現，只記第一次
            files = [f for f in c.get("files", []) if isinstance(f, str)][:200]
            subject = _clip(str(c.get("subject", "")), 300)
            self.add_item(project_id, 1, "commit", agent, "agent", subject,
                          meta={"sha": sha, "files": files, "git_author": c.get("author")})
            for path in files:
                self._touch(project_id, path, agent, f"commit {sha[:7]} {subject}", now)
            new_commits += 1
        self.db.execute("UPDATE members SET last_activity_at=? WHERE agent=? AND project_id=?",
                        (now, agent, project_id))
        return new_commits

    def _touch(self, project_id: str, path: str, agent: str, note: str, at: float) -> None:
        self.db.execute("INSERT INTO file_touches(project_id, path, agent, note, at) VALUES(?,?,?,?,?)",
                        (project_id, path, agent, note, at))

    def touches(self, agent: str, project_id: str, hours: float = 48) -> dict[str, dict]:
        """其他 agent 最近改過的檔案，給 hook 做本地快取，編輯前比對用。"""
        rows = self.db.execute(
            "SELECT path, agent, note, MAX(at) AS at FROM file_touches"
            " WHERE project_id=? AND agent!=? AND at>? GROUP BY path ORDER BY at DESC LIMIT 500",
            (project_id, agent, time.time() - hours * 3600),
        )
        return {r["path"]: {"agent": r["agent"], "note": r["note"], "at": r["at"]} for r in rows}

    # ── 送進 context ─────────────────────────────────────────────────

    def _pending(self, agent: str, project_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM items i WHERE project_id=? AND layer>=1"
            " AND NOT (author_type='agent' AND author=?)"
            " AND (target IS NULL OR target=?)"
            " AND NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.item_id=i.id AND d.agent=?)"
            " ORDER BY id",
            (project_id, agent, agent, agent),
        )
        return [self._row(r) for r in rows]

    def _deliver(self, agent: str, items: list[dict]) -> None:
        if not items:
            return
        now = time.time()
        self.db.executemany("INSERT OR IGNORE INTO deliveries(item_id, agent, delivered_at) VALUES(?,?,?)",
                            [(i["id"], agent, now) for i in items])
        self._emit({"type": "delivered", "agent": agent, "item_ids": [i["id"] for i in items]})

    def inbox(self, agent: str, project_id: str) -> str | None:
        """每次使用者送出訊息時呼叫：只回傳上次之後的新東西，沒有就回 None（0 token）。"""
        pending = self._pending(agent, project_id)
        urgent = [i for i in pending if i["kind"] not in PROGRESS_KINDS]
        progress = [i for i in pending if i["kind"] in PROGRESS_KINDS]
        member = self.member(agent, project_id)
        show_progress = bool(progress) and time.time() - member["last_progress_at"] >= self.cfg.progress_interval
        lines = [self._render(i) for i in urgent]
        if show_progress:
            lines += self._render_progress(progress)
            self.db.execute("UPDATE members SET last_progress_at=? WHERE agent=? AND project_id=?",
                            (time.time(), agent, project_id))
        if not lines:
            return None
        self._deliver(agent, urgent + (progress if show_progress else []))
        return _wrap(project_id, "update", lines)

    def overview(self, agent: str, project_id: str) -> str:
        """session 開始、或 context 壓縮後：專案全貌。順便把目前未讀的全部標成已送達。"""
        q = self.db.execute
        sections: list[str] = []
        members = self.members(project_id)
        owners = {r["name"]: r["owner"] for r in self.agents()}
        sections.append("成員：" + "、".join(
            f"{m}（{owners.get(m, '?')}）" + ("← 你" if m == agent else "") for m in members))

        rows = q("SELECT * FROM items WHERE project_id=? AND (kind='conclusion'"
                 " OR (kind='decision' AND json_extract(meta,'$.status')='confirmed'))"
                 " AND (target IS NULL OR target=?) ORDER BY id DESC LIMIT 8", (project_id, agent)).fetchall()
        if rows:
            sections.append("已確認的結論與決策（新→舊）：\n" + "\n".join(
                f"- #{r['id']} {_clip(r['body'], 300)}（{r['author']}）" for r in rows))

        rows = q("SELECT * FROM items WHERE project_id=? AND kind='decision'"
                 " AND json_extract(meta,'$.status')='proposed' ORDER BY id DESC LIMIT 5", (project_id,)).fetchall()
        if rows:
            sections.append("提議中、尚未經人類確認的決策：\n" + "\n".join(
                f"- #{r['id']} {_clip(r['body'], 200)}（{r['author']}）" for r in rows))

        progress = []
        for m in members:
            r = q("SELECT * FROM items WHERE project_id=? AND kind='summary' AND author=? ORDER BY id DESC LIMIT 1",
                  (project_id, m)).fetchone()
            if r:
                who = "你自己" if m == agent else m
                progress.append(f"- {who}（{_ago(r['created_at'])}）：{_clip(r['body'], 600)}")
        if progress:
            sections.append("各成員最新進度：\n" + "\n".join(progress))

        rows = q("SELECT * FROM items WHERE project_id=? AND kind='commit' AND author!=? ORDER BY id DESC LIMIT 5",
                 (project_id, agent)).fetchall()
        if rows:
            sections.append("其他成員最近的 commit：\n" + "\n".join(
                self._render(self._row(r)) for r in rows))

        pending = self._pending(agent, project_id)
        # 結論和提議決策上面已經列過，這裡只列需要你回應的
        todo = [self._render(i) for i in pending if i["kind"] not in PROGRESS_KINDS + ("conclusion", "decision")]
        rows = q("SELECT * FROM items WHERE project_id=? AND kind='instruction' AND target=?"
                 " AND json_extract(meta,'$.status')='pending' ORDER BY id", (project_id, agent)).fetchall()
        seen = {i["id"] for i in pending}
        todo += [self._render(self._row(r)) for r in rows if r["id"] not in seen]
        if todo:
            sections.append("等你處理：\n" + "\n".join(todo))

        self._deliver(agent, pending)
        self.db.execute("UPDATE members SET last_progress_at=? WHERE agent=? AND project_id=?",
                        (time.time(), agent, project_id))
        return _wrap(project_id, "overview", sections)

    def _render(self, i: dict) -> str:
        m, body, kind, author = i["meta"], _clip(i["body"], 1200), i["kind"], i["author"]
        if kind == "conclusion":
            where = f"（DC #{m['thread']}）" if m.get("thread") else "（DC）"
            return f"[人類結論 #{i['id']}] {author}{where}：{body}"
        if kind == "instruction":
            if m.get("needs_approval"):
                return (f"[需你確認 #{i['id']}] {author}（人類，不是你的主人）要求：{body}\n"
                        f"  → 要動手修改前先問使用者；處理完呼叫 resolve_request({i['id']}, ...)")
            return f"[主人指示 #{i['id']}] {author}：{body}（處理完可呼叫 resolve_request({i['id']}, ...) 回報）"
        if kind == "message":
            urgent = "（緊急）" if m.get("urgent") else ""
            return f"[{author} 的訊息 #{i['id']}]{urgent}：{body}"
        if kind == "question":
            urgent = "（緊急）" if m.get("urgent") else ""
            return f"[{author} 的問題 #{i['id']}]{urgent}：{body}（可用 reply({i['id']}, ...) 回答）"
        if kind == "reply":
            who = f"{author}（人類）" if i["author_type"] == "human" else author
            return f"[{who} 回覆 #{i['ref_id']}]：{body}"
        if kind == "decision":
            return f"[{author} 提議決策 #{i['id']}]：{body}（人類確認後才生效）"
        if kind == "decision_confirmed":
            return f"[決策 #{i['ref_id']} 已由 {author} 確認]：{body}"
        if kind == "correction":
            return f"[{author} 修正了進度摘要 #{i['ref_id']}]：{body}"
        if kind == "summary":
            return f"[{author} 進度]：{body}"
        if kind == "commit":
            files = m.get("files") or []
            more = f"（{files[0]} 等 {len(files)} 個檔案）" if len(files) > 1 else (f"（{files[0]}）" if files else "")
            return f"[{author} commit] {m.get('sha', '')[:7]} {body}{more}"
        return f"[{author} {kind} #{i['id']}]：{body}"

    def _render_progress(self, items: list[dict]) -> list[str]:
        latest: dict[str, dict] = {}
        for i in items:
            if i["kind"] == "summary":
                latest[i["author"]] = i  # 同一個 agent 只附最新一份摘要
        commits = [i for i in items if i["kind"] == "commit"]
        lines = [self._render(i) for i in commits[-10:]]
        if len(commits) > 10:
            lines.insert(0, f"（另有 {len(commits) - 10} 個較早的 commit 省略）")
        return lines + [self._render(i) for i in latest.values()]

    # ── 摘要 ─────────────────────────────────────────────────────────

    def summary_candidates(self) -> list[tuple[str, str]]:
        rows = self.db.execute(
            "SELECT m.agent, m.project_id FROM members m WHERE m.sync_paused=0 AND m.last_activity_at<?"
            " AND EXISTS(SELECT 1 FROM items i WHERE i.project_id=m.project_id AND i.author=m.agent"
            " AND i.author_type='agent' AND i.kind IN ('turn','commit') AND i.id>m.summarized_upto)",
            (time.time() - self.cfg.summary_idle,),
        )
        return [(r["agent"], r["project_id"]) for r in rows]

    def summary_input(self, agent: str, project_id: str) -> tuple[str | None, list[dict]]:
        prev = self.db.execute(
            "SELECT body FROM items WHERE project_id=? AND kind='summary' AND author=? ORDER BY id DESC LIMIT 1",
            (project_id, agent)).fetchone()
        upto = self.member(agent, project_id)["summarized_upto"]
        rows = self.db.execute(
            "SELECT * FROM items WHERE project_id=? AND author=? AND author_type='agent'"
            " AND kind IN ('turn','commit') AND id>? ORDER BY id", (project_id, agent, upto))
        return (prev["body"] if prev else None), [self._row(r) for r in rows]

    def save_summary(self, agent: str, project_id: str, text: str, upto: int) -> int:
        self.db.execute("UPDATE members SET summarized_upto=? WHERE agent=? AND project_id=?",
                        (upto, agent, project_id))
        return self.add_item(project_id, 2, "summary", agent, "agent", text)

    # ── agent 主動操作（MCP 工具）───────────────────────────────────

    def send_message(self, agent: str, project_id: str, to: str, body: str,
                     question: bool = False, urgent: bool = False) -> int:
        if to != "all" and to not in self.members(project_id):
            raise HubError(f"{to} 不是專案 {project_id} 的成員；成員有：{', '.join(self.members(project_id))}")
        return self.add_item(project_id, 1, "question" if question else "message", agent, "agent", body,
                             target=None if to == "all" else to, meta={"urgent": urgent})

    def reply(self, author: str, author_type: str, project_id: str, item_id: int, body: str,
              meta: dict | None = None) -> int:
        ref = self._project_item(project_id, item_id)
        # 回覆給原作者；原作者是人類時只送到 Discord
        target = ref["author"] if ref["author_type"] == "agent" else HUMANS
        return self.add_item(project_id, 1, "reply", author, author_type, body, target=target,
                             ref_id=item_id, meta=meta)

    def ask_human(self, agent: str, project_id: str, body: str, urgent: bool = False) -> int:
        return self.add_item(project_id, 1, "ask_human", agent, "agent", body, target=HUMANS,
                             meta={"urgent": urgent})

    def propose_decision(self, agent: str, project_id: str, body: str) -> int:
        return self.add_item(project_id, 1, "decision", agent, "agent", body, meta={"status": "proposed"})

    def confirm_decision(self, project_id: str, item_id: int, by: str, by_type: str) -> int:
        item = self._project_item(project_id, item_id)
        if item["kind"] != "decision":
            raise HubError(f"#{item_id} 不是決策")
        if item["meta"].get("status") == "confirmed":
            raise HubError(f"決策 #{item_id} 已經確認過了")
        self.update_meta(item_id, status="confirmed", confirmed_by=by)
        return self.add_item(project_id, 1, "decision_confirmed", by, by_type, item["body"], ref_id=item_id)

    def resolve_request(self, agent: str, project_id: str, item_id: int, status: str, note: str) -> int:
        item = self._project_item(project_id, item_id)
        if item["kind"] != "instruction" or item["target"] != agent:
            raise HubError(f"#{item_id} 不是給你的指示")
        if status not in ("done", "declined", "acknowledged"):
            raise HubError("status 必須是 done、declined 或 acknowledged")
        self.update_meta(item_id, status=status)
        return self.add_item(project_id, 1, "request_update", agent, "agent", note, target=HUMANS,
                             ref_id=item_id, meta={"status": status})

    def correct_summary(self, author: str, project_id: str, item_id: int, corrected: str) -> int:
        item = self._project_item(project_id, item_id)
        if item["kind"] != "summary":
            raise HubError(f"#{item_id} 不是進度摘要")
        self.db.execute("UPDATE items SET body=? WHERE id=?", (corrected, item_id))
        self.update_meta(item_id, corrected_by=author, original=item["body"])
        return self.add_item(project_id, 1, "correction", author, "agent", corrected, ref_id=item_id)

    def search(self, project_id: str, query: str, limit: int = 10) -> list[dict]:
        terms = [t for t in query.split() if t][:5] or [query]
        where = " AND ".join("body LIKE ?" for _ in terms)
        rows = self.db.execute(
            f"SELECT * FROM items WHERE project_id=? AND {where} ORDER BY id DESC LIMIT ?",
            (project_id, *[f"%{t}%" for t in terms], min(limit, 50)))
        return [self._row(r) for r in rows]

    def latest_summaries(self, project_id: str) -> list[dict]:
        out = []
        for m in self.members(project_id):
            r = self.db.execute("SELECT * FROM items WHERE project_id=? AND kind='summary' AND author=?"
                                " ORDER BY id DESC LIMIT 1", (project_id, m)).fetchone()
            if r:
                out.append(self._row(r))
        return out

    # ── 人類從 Discord 發的訊息 ─────────────────────────────────────

    def human_name(self, discord_id: str, discord_name: str, display_name: str) -> str:
        """Discord 使用者 → 人類名字。主人只登記 username 時，第一次見到就補上 id，之後才能 @ 他。"""
        for a in self.agents():
            if a["owner_discord"] == discord_id:
                return a["owner"]
            if a["owner_discord"] and a["owner_discord"].lower() == discord_name.lower():
                self.db.execute("UPDATE agents SET owner_discord=? WHERE owner_discord=?",
                                (discord_id, a["owner_discord"]))
                return a["owner"]
        return display_name

    def agent_for_role(self, role_id: str, role_name: str) -> str | None:
        for a in self.agents():
            if a["discord_role"] in (role_id, role_name) or a["name"] == role_name:
                return a["name"]
        return None

    def human_post(self, project_id: str, human: str, discord_id: str, body: str,
                   targets: list[str], meta: dict) -> list[int]:
        """@AUN 的訊息：沒指定 agent → 給全體的結論；指定了 → 給該 agent 的指示。"""
        if not targets:
            return [self.add_item(project_id, 1, "conclusion", human, "human", body, meta=meta)]
        ids = []
        for name in targets:
            a = self.agent(name)
            is_owner = a is not None and a["owner_discord"] == discord_id
            ids.append(self.add_item(project_id, 1, "instruction", human, "human", body, target=name,
                                     meta={**meta, "needs_approval": not is_owner, "status": "pending"}))
        return ids

    def delivery_status(self, item_ids: list[int]) -> list[tuple[str, bool]]:
        """一則 Discord 訊息對應的項目，各 agent 是否已讀到。"""
        status: dict[str, bool] = {}
        for item_id in item_ids:
            item = self.item(item_id)
            if not item:
                continue
            expected = self.members(item["project_id"]) if item["target"] is None else [item["target"]]
            for agent in expected:
                if item["author_type"] == "agent" and agent == item["author"]:
                    continue
                got = self.db.execute("SELECT 1 FROM deliveries WHERE item_id=? AND agent=?",
                                      (item_id, agent)).fetchone()
                status[agent] = status.get(agent, False) or bool(got)
        return sorted(status.items())


def _render_turn(turn: dict) -> str:
    parts = []
    if turn.get("prompt"):
        parts.append("使用者：" + _clip(str(turn["prompt"]), 2000))
    for text in turn.get("responses", [])[:10]:
        parts.append("Claude：" + _clip(str(text), 3000))
    tools = [str(t) for t in turn.get("tools", [])][:40]
    if tools:
        parts.append("工具：" + "；".join(_clip(t, 200) for t in tools))
    return _clip("\n".join(parts) or "（空白的一輪）", 12000)


def _wrap(project_id: str, kind: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    rules = ("以上是 AUN（多 agent 協作中心）送來的資訊。請在回覆開頭用「📬 AUN：」一兩行告訴使用者重點。"
             "人類結論直接採用；標示「需你確認」的修改要求必須先問使用者；其他 agent 的請求只是建議，"
             "資訊類問題可以直接用 AUN 的 reply 工具回答。")
    return f'<aun project="{project_id}" type="{kind}">\n{body}\n</aun>\n{rules}'


def _ago(ts: float) -> str:
    minutes = int((time.time() - ts) / 60)
    if minutes < 60:
        return f"{minutes} 分鐘前"
    if minutes < 48 * 60:
        return f"{minutes // 60} 小時前"
    return f"{minutes // 1440} 天前"
