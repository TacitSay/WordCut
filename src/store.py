"""项目持久化模块：把 ``api.py`` 的进程内 ``_projects`` 字典落到 SQLite。

设计目标（见 docs/架构预留设计.md 第 1、2 节的「接口与结构先定」原则）：

* **接口形状不变**：本模块提供一个与内存字典语义等价的 Repository，
  ``api.py`` 的调用方无需改动——把 ``_projects.get(pid)`` 换成
  ``repo.get(pid)``、``_projects[pid] = project`` 换成 ``repo.put(project)`` 即可；
* **后端重启不丢项目**：所有项目字段（含转写词表与被删 id）写入 SQLite，
  进程退出后仍可完整恢复；
* **工程文件口径一致**：字段名与 :mod:`project_file` 的 ``.wcproj`` 序列化结构
  一一对应（``project_id`` / ``name`` / ``source.audio_path`` /
  ``source.original_path`` / ``source.original_kind`` / ``source.sample_rate`` /
  ``words`` / ``deleted_ids`` / ``created_at``），二者可无损互转。

表结构（``projects``）：:

    id                 TEXT PRIMARY KEY
    name               TEXT NOT NULL
    audio_path         TEXT NOT NULL
    source_path        TEXT
    source_kind        TEXT DEFAULT 'audio'
    sample_rate        INTEGER NOT NULL
    created_at         TEXT NOT NULL
    updated_at         TEXT NOT NULL
    words_json         TEXT NOT NULL DEFAULT '[]'   -- JSON 数组
    deleted_ids_json   TEXT NOT NULL DEFAULT '[]'   -- JSON 数组

线程模型：

* **每线程独立连接**（:class:`threading.local`）。``sqlite3`` 的连接对象默认
  禁止跨线程使用，FastAPI 的同步接口跑在线程池里，因此按线程懒创建连接；
* **写操作串行化**（:class:`threading.Lock`）：UPSERT / UPDATE / DELETE 全部在
  锁内执行，避免 ``database is locked`` 与并发覆盖；
* 读操作（``get`` / ``list_all`` / ``exists``）不加写锁：WAL 模式下读不阻塞写，
  且读到的永远是一个完整快照（单条 SQL 的原子性）。

数据一致性：

* 写入前用 ``json.dumps(..., ensure_ascii=False)`` 序列化 ``words`` /
  ``deleted_ids``，中文词文本不转义，DB 内容可直接肉眼检查；
* 读取时容错：JSON 解析失败或类型不符时回退为空列表 / 空集合，
  单条坏数据绝不阻塞整个服务；
* ``deleted_ids`` 读出来一定是 :class:`set`——``api.py`` 依赖
  ``.update()`` / ``.difference_update()`` / ``&=`` 等集合运算（不变量 4：
  只增删引用，不重排 Token）。

遵守的不变量（见 docs/不变量.md）：

* 不变量 1：源音频只读。本模块只存路径字符串，从不打开、改写源文件；
* 不变量 2：时间单位为秒（float）。本模块原样透传 ``words[*].start/end/dur``，
  不做任何单位换算；
* 不变量 4：``words`` 按词序原样保存，``token.id`` 仍由
  :func:`pipeline.load_tokens` 由词序生成，本模块从不重排词表。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

__all__ = [
    "DB_FILENAME",
    "DEFAULT_DB_PATH",
    "Repository",
    "ProjectRepository",
]

#: 默认数据库文件名（放在项目根目录，与 ``uploads/`` / ``outputs/`` 平级）。
DB_FILENAME: str = "wordcut.db"

#: 默认数据库路径：``<项目根>/wordcut.db``。
#: 本文件位于 ``src/``，因此上一级的上一级即项目根（``E:\WordCut``）。
DEFAULT_DB_PATH: Path = Path(__file__).resolve().parent.parent / DB_FILENAME

#: SQLite 忙等超时（秒）：写锁之外偶发的短事务冲突靠它自愈。
_BUSY_TIMEOUT: float = 10.0

#: 建表语句：字段与 ``api.py`` 的内存项目字典 / ``project_file.py`` 的工程文档一一对应。
_CREATE_PROJECTS_SQL: str = """
CREATE TABLE IF NOT EXISTS projects (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    audio_path        TEXT NOT NULL,
    source_path       TEXT,
    source_kind       TEXT DEFAULT 'audio',
    sample_rate       INTEGER NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    words_json        TEXT NOT NULL DEFAULT '[]',
    deleted_ids_json  TEXT NOT NULL DEFAULT '[]'
)
"""

#: 列表接口按创建时间倒序，建索引避免全表排序。
_CREATE_INDEX_SQL: str = (
    "CREATE INDEX IF NOT EXISTS idx_projects_created_at "
    "ON projects (created_at DESC)"
)

#: UPSERT：存在即整行更新，不存在即插入（``api.py`` 里 ``_projects[pid] = project``
#: 的两种语义——新建与「加载工程后覆盖」——都由这一条语句覆盖）。
_UPSERT_SQL: str = """
INSERT INTO projects (
    id, name, audio_path, source_path, source_kind,
    sample_rate, created_at, updated_at, words_json, deleted_ids_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    name             = excluded.name,
    audio_path       = excluded.audio_path,
    source_path      = excluded.source_path,
    source_kind      = excluded.source_kind,
    sample_rate      = excluded.sample_rate,
    created_at       = excluded.created_at,
    updated_at       = excluded.updated_at,
    words_json       = excluded.words_json,
    deleted_ids_json = excluded.deleted_ids_json
"""

#: 查询列顺序与 :func:`_row_to_project` 的取值一一对应（写全列名，不用 ``SELECT *``）。
_SELECT_COLUMNS: str = (
    "id, name, audio_path, source_path, source_kind, "
    "sample_rate, created_at, updated_at, words_json, deleted_ids_json"
)


# ----------------------------------------------------------------------
# 内部工具：时间与 JSON 容错
# ----------------------------------------------------------------------
def _now_iso() -> str:
    """当前 UTC 时间的 ISO-8601 字符串（秒级精度，与 ``api._now_iso`` 同口径）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    """把任意值安全地序列化成 JSON 文本（中文不转义，不可序列化的对象转成字符串）。"""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - 兜底，正常路径不会走到
        return "[]"


def _loads_words(raw: Any) -> List[Dict[str, Any]]:
    """把 ``words_json`` 解析成词 dict 列表；任何异常都回退为空列表。

    容错范围：``NULL`` / 空串 / 非法 JSON / 解析出来不是数组。
    数组内的元素保持原样（非 dict 元素由下游 ``pipeline.load_tokens`` 校验），
    本层不做结构改写，保证「存什么读什么」。
    """
    if raw is None:
        return []
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return []
    if not isinstance(raw, str):
        return []
    text = raw.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return list(parsed)


def _loads_ids(raw: Any) -> Set[str]:
    """把 ``deleted_ids_json`` 解析成 id 集合；任何异常都回退为空集合。

    强约束：返回值一定是 :class:`set`（``api.py`` 依赖
    ``.update()`` / ``.difference_update()``），元素一律转成 ``str``。
    """
    if raw is None:
        return set()
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return set()
    if not isinstance(raw, str):
        return set()
    text = raw.strip()
    if not text:
        return set()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return set()
    if not isinstance(parsed, list):
        return set()
    return {str(item) for item in parsed}


def _as_int(value: Any, default: int = 0) -> int:
    """把值转成 int；``None`` 或非法值用 ``default``（避免一行坏数据炸掉整个列表）。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    """把值转成 str；``None`` 用 ``default``（``NOT NULL`` 列不允许写 ``None``）。"""
    if value is None:
        return default
    return str(value)


def _row_to_project(row: sqlite3.Row) -> Dict[str, Any]:
    """把一行 ``projects`` 记录还原成与 ``api.py`` 内存结构一致的字典。

    返回值字段：``id`` / ``name`` / ``audio_path`` / ``source_path`` /
    ``source_kind`` / ``sample_rate`` / ``words``（list）/ ``deleted_ids``（set）/
    ``created_at``，另附 ``updated_at``（列表排序 / 调试用，下游按需取用）。
    """
    return {
        "id": _as_str(row["id"]),
        "name": _as_str(row["name"]),
        "audio_path": _as_str(row["audio_path"]),
        "source_path": row["source_path"],
        "source_kind": _as_str(row["source_kind"], "audio"),
        "sample_rate": _as_int(row["sample_rate"]),
        "words": _loads_words(row["words_json"]),
        "deleted_ids": _loads_ids(row["deleted_ids_json"]),
        "created_at": _as_str(row["created_at"]),
        "updated_at": _as_str(row["updated_at"]),
    }


# ----------------------------------------------------------------------
# Repository
# ----------------------------------------------------------------------
class Repository:
    """项目表（``projects``）的读写仓库，语义等价于 ``api._projects`` 内存字典。

    用法::

        repo = Repository(Path("E:/WordCut/wordcut.db"))
        repo.put({"id": "abc", "name": "示例", "audio_path": "E:/...wav",
                  "source_path": None, "source_kind": "audio",
                  "sample_rate": 16000, "words": [], "deleted_ids": set(),
                  "created_at": "2024-01-01T00:00:00+00:00"})
        project = repo.get("abc")       # words 是 list，deleted_ids 是 set
        repo.update_deleted_ids("abc", {"t00003"})
        repo.list_all()                 # 按 created_at 倒序

    并发约定：一个进程内共享一个 :class:`Repository` 实例即可（连接按线程自动
    分发），写方法内部持有实例级锁，调用方无需再加锁。
    """

    def __init__(self, db_path: Path) -> None:
        """打开（或创建）数据库，自动建表并开启 WAL。

        参数:
            db_path: SQLite 文件路径。父目录不存在时自动创建。
                传入 ``":memory:"`` 时使用内存库（仅测试用，跨线程各自独立）。

        副作用:
            主线程连接上执行 ``PRAGMA journal_mode=WAL``（持久化到库文件，
            其余线程的新连接自动沿用）与 ``PRAGMA synchronous=NORMAL``
            （WAL 下的推荐设置：断电只丢最后一个事务，性能明显更好）。
        """
        self._db_path: Path = Path(db_path)
        self._is_memory: bool = str(db_path).strip() == ":memory:"
        if not self._is_memory:
            parent = self._db_path.parent
            if str(parent) and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)

        #: 每线程独立连接（``sqlite3`` 连接对象默认禁止跨线程使用）。
        self._local: threading.local = threading.local()
        #: 写操作串行化（UPSERT / UPDATE / DELETE）。
        self._write_lock: threading.Lock = threading.Lock()
        #: 已创建的全部连接，供 :meth:`close` 逐线程清理（用 list + 自身锁保护）。
        self._connections: List[sqlite3.Connection] = []
        self._connections_lock: threading.Lock = threading.Lock()
        self._closed: bool = False

        # 取一次连接即可完成建表 / 建索引 / 开 WAL（_connection 内部幂等初始化）。
        self._connection()

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    def _connection(self) -> sqlite3.Connection:
        """取当前线程的连接；首次调用时创建并做连接级 PRAGMA。

        异常:
            RuntimeError: 仓库已 :meth:`close` 后再取连接（明确报错，避免
                用半关闭的句柄写出难排查的 ``ProgrammingError``）。
        """
        if self._closed:
            raise RuntimeError("Repository 已关闭，不能再执行数据库操作")

        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn

        try:
            conn = sqlite3.connect(
                str(self._db_path),
                timeout=_BUSY_TIMEOUT,
                check_same_thread=True,  # 每线程一个连接，本线程专属
            )
        except sqlite3.Error as exc:  # pragma: no cover - 磁盘权限等环境问题
            raise RuntimeError(f"打开 SQLite 数据库失败：{self._db_path}（{exc}）") from exc

        conn.row_factory = sqlite3.Row
        # busy_timeout 与 synchronous 是连接级设置，每个新连接都要设一遍。
        conn.execute(f"PRAGMA busy_timeout = {int(_BUSY_TIMEOUT * 1000)}")
        conn.execute("PRAGMA synchronous = NORMAL")

        self._local.conn = conn
        with self._connections_lock:
            self._connections.append(conn)
        # 内存库 / 新建库 / 迁移过来的老库都可能缺表：每个新连接都幂等补一次。
        self._init_schema(conn)
        return conn

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        """开启 WAL 并建表 / 建索引（幂等，可重复执行）。

        ``journal_mode=WAL`` 写在库文件头里，只需成功设置一次；重复设置无副作用。
        内存库不支持 WAL，会返回 ``"memory"``，直接忽略。

        注意：本方法**不加写锁**。它只在「当前线程第一次取连接」时执行，
        而写方法会先持锁再取连接；若这里再抢同一把非重入锁就会自锁。
        建表语句自带 ``IF NOT EXISTS``，并发重复执行是安全的。
        """
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error:  # pragma: no cover - 只读目录 / 不支持 WAL 的 FS
            pass
        try:
            conn.execute(_CREATE_PROJECTS_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.commit()
        except sqlite3.Error as exc:  # pragma: no cover
            conn.rollback()
            raise RuntimeError(f"初始化 projects 表失败：{exc}") from exc

    # ------------------------------------------------------------------
    # 内部序列化
    # ------------------------------------------------------------------
    @staticmethod
    def _row_values(project: Dict[str, Any], created_at: str, now: str) -> tuple:
        """把项目字典摊平成 UPSERT 的参数元组（列顺序见 :data:`_UPSERT_SQL`）。"""
        source_path = project.get("source_path")
        source_kind = project.get("source_kind") or "audio"
        return (
            _as_str(project.get("id")),
            _as_str(project.get("name")),
            _as_str(project.get("audio_path")),
            None if source_path in (None, "") else str(source_path),
            _as_str(source_kind, "audio"),
            _as_int(project.get("sample_rate")),
            created_at,
            now,
            _dumps(project.get("words") or []),
            _dumps(sorted(str(item) for item in (project.get("deleted_ids") or ()))),
        )

    def _existing_created_at(self, pid: str) -> Optional[str]:
        """读已有记录的 ``created_at``（用于 ``put`` 未提供该字段时保持稳定）。"""
        row = self._connection().execute(
            "SELECT created_at FROM projects WHERE id = ?", (pid,)
        ).fetchone()
        return None if row is None else _as_str(row["created_at"])

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def get(self, pid: str) -> Optional[Dict[str, Any]]:
        """按 id 取项目；不存在时返回 ``None``（对应 ``_projects.get(pid)``）。

        返回的是**新构造**的字典：``words`` 为 ``list``、``deleted_ids`` 为
        ``set``，与库里彻底解耦——调用方随意原地修改（``deleted_ids.update(...)``）
        都不会污染数据库，改完须显式调 :meth:`put` / :meth:`update_deleted_ids`
        才会落盘。
        """
        try:
            row = self._connection().execute(
                f"SELECT {_SELECT_COLUMNS} FROM projects WHERE id = ?", (str(pid),)
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return _row_to_project(row)

    def list_all(self) -> List[Dict[str, Any]]:
        """列出全部项目，按 ``created_at`` 倒序（最新在前）。

        与 ``api.list_projects`` 的排序口径一致；``created_at`` 为 ISO-8601
        字符串，字典序即时间序（同一时刻创建的记录再按 id 稳定排序）。
        """
        try:
            rows = self._connection().execute(
                f"SELECT {_SELECT_COLUMNS} FROM projects "
                "ORDER BY created_at DESC, id DESC"
            ).fetchall()
        except sqlite3.Error:
            return []
        return [_row_to_project(row) for row in rows]

    def exists(self, pid: str) -> bool:
        """判断项目是否存在（只查 ``1``，不读整行，比 :meth:`get` 便宜）。"""
        try:
            row = self._connection().execute(
                "SELECT 1 FROM projects WHERE id = ? LIMIT 1", (str(pid),)
            ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def put(self, project: Dict[str, Any]) -> None:
        """插入或整行更新一个项目（``_projects[pid] = project`` 的持久化等价物）。

        * 新 id：插入，``created_at`` 取项目自带的（缺失则用当前时间）；
        * 已存在：整行覆盖（含 ``created_at``），只是把 ``updated_at`` 刷成当前时间。
          「加载工程覆盖同名项目」这类场景因此能完整还原工程里的创建时间。

        参数:
            project: 与 ``api.py`` 内存结构一致的项目字典，至少含 ``id``。
                ``words`` 允许是任意可 JSON 序列化序列；``deleted_ids``
                允许是 set / list / tuple（内部统一转成去重后的有序数组）。

        异常:
            ValueError: ``id`` 为空——没有主键就无法定位项目，直接拒绝。
        """
        pid = _as_str(project.get("id"))
        if not pid:
            raise ValueError("put(project) 要求 project['id'] 非空")

        now = _now_iso()
        created_at = _as_str(project.get("created_at"))
        if not created_at:
            created_at = self._existing_created_at(pid) or now

        values = self._row_values(project, created_at, now)
        with self._write_lock:
            conn = self._connection()
            try:
                conn.execute(_UPSERT_SQL, values)
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise RuntimeError(f"写入项目失败：{pid}（{exc}）") from exc

    def delete(self, pid: str) -> None:
        """删除一个项目；id 不存在时是幂等空操作（对应 ``_projects.pop(pid, None)``）。

        注意：本方法只删数据库记录，不删 ``uploads/`` / ``outputs/`` 下的实体文件
        （那些文件由调用方按自己的生命周期决定是否清理）。
        """
        with self._write_lock:
            conn = self._connection()
            try:
                conn.execute("DELETE FROM projects WHERE id = ?", (str(pid),))
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise RuntimeError(f"删除项目失败：{pid}（{exc}）") from exc

    def update_words(self, pid: str, words: List[Dict[str, Any]]) -> None:
        """只更新词表（转写完成后调用），不动 ``deleted_ids`` 与其他字段。

        参数:
            pid: 项目 id。
            words: faster-whisper 的 word dict 列表（按词序，不重排，不变量 4）。
        """
        payload = _dumps(list(words or []))
        with self._write_lock:
            conn = self._connection()
            try:
                conn.execute(
                    "UPDATE projects SET words_json = ?, updated_at = ? WHERE id = ?",
                    (payload, _now_iso(), str(pid)),
                )
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise RuntimeError(f"更新词表失败：{pid}（{exc}）") from exc

    def update_deleted_ids(self, pid: str, ids: Iterable[str]) -> None:
        """只更新被删 id 集合（删除 / 恢复接口的高频写路径）。

        参数:
            pid: 项目 id。
            ids: 被删 Token id 集合（set / list / tuple 均可）。内部转成 ``str``
                并排序后存储，保证同样的集合写出同样的 JSON（便于 diff 与排查）。
        """
        payload = _dumps(sorted({str(item) for item in (ids or ())}))
        with self._write_lock:
            conn = self._connection()
            try:
                conn.execute(
                    "UPDATE projects SET deleted_ids_json = ?, updated_at = ? "
                    "WHERE id = ?",
                    (payload, _now_iso(), str(pid)),
                )
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise RuntimeError(f"更新被删 id 失败：{pid}（{exc}）") from exc

    # ------------------------------------------------------------------
    # 迁移 / 关闭
    # ------------------------------------------------------------------
    def migrate_from_dict(self, old: Dict[str, Any]) -> int:
        """把旧的 ``_projects`` 内存字典一次性写入数据库，返回写入条数。

        * ``old`` 形如 ``{project_id: project_dict}``；
        * 逐条 UPSERT，单条失败只跳过该条（记下计数），不中断整批迁移；
        * 幂等：重复调用不会产生重复行，只是把 ``updated_at`` 刷新一遍；
        * 传入空字典 / ``None`` 时返回 ``0``。

        参数:
            old: 旧的内存项目字典。

        返回:
            成功写入（或更新）的项目条数。
        """
        if not old:
            return 0

        written = 0
        for key, project in dict(old).items():
            if not isinstance(project, dict):
                # 允许「字典里缺 id」：用外层 key 补上，保持与内存字典同构。
                continue
            item: Dict[str, Any] = dict(project)
            if not _as_str(item.get("id")):
                item["id"] = str(key)
            try:
                self.put(item)
            except (ValueError, RuntimeError):
                continue
            written += 1
        return written

    def close(self) -> None:
        """关闭所有已创建的连接（幂等；关闭后本实例不可再用）。

        逐个连接 ``commit`` + ``close``：跨线程关闭由 ``check_same_thread``
        保护，因此对非当前线程的连接做「尽力而为」处理，失败也不抛错。
        每个线程自己关闭最稳妥；进程退出时未关闭的连接由 SQLite 回收。
        """
        if self._closed:
            return
        self._closed = True

        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()

        for conn in connections:
            try:
                conn.commit()
                conn.close()
            except (sqlite3.Error, sqlite3.ProgrammingError):
                # 其他线程持有的连接：本线程关不掉属正常，留给该线程 / 进程退出。
                continue

        # 清掉本线程的缓存引用，避免 close 之后再被误用。
        try:
            delattr(self._local, "conn")
        except AttributeError:
            pass


#: 语义化别名，便于调用方写 ``ProjectRepository(...)`` 读起来更明确。
ProjectRepository = Repository
