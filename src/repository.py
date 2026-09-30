"""SQLite 表结构、迁移与事务访问。

所有跨表写操作（封锁登记/变更 + 计划改期 + 审计 + 幂等记录）均在同一个
``BEGIN IMMEDIATE`` 事务内提交：任一语句失败即回滚，不会留下“半套结果”。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER REFERENCES records(id) ON DELETE CASCADE,
                    blockade_id INTEGER REFERENCES blockades(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS blockades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    target TEXT NOT NULL,
                    start_hour INTEGER NOT NULL,
                    end_hour INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    key TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_code INTEGER NOT NULL,
                    response_body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            # 旧库先补齐列，再建立引用这些列的索引。
            self._migrate(connection)
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_audit_blockade ON audit_events(blockade_id, id);
                CREATE INDEX IF NOT EXISTS idx_blockades_status ON blockades(status);
                """
            )

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(records)").fetchall()}
        if "schedule_info" not in columns:
            connection.execute("ALTER TABLE records ADD COLUMN schedule_info TEXT")
        audit_columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(audit_events)").fetchall()}
        if "blockade_id" not in audit_columns:
            connection.execute("ALTER TABLE audit_events ADD COLUMN blockade_id INTEGER")
        # 旧表 record_id 为 NOT NULL，封锁审计事件需要 NULL，重建表放开约束。
        record_id_col = audit_columns.get("record_id")
        if record_id_col is not None and int(record_id_col["notnull"]) == 1:
            connection.executescript(
                """
                CREATE TABLE audit_events_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER REFERENCES records(id) ON DELETE CASCADE,
                    blockade_id INTEGER,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO audit_events_new(id, record_id, blockade_id, action, actor_id, version, details, created_at)
                SELECT id, record_id, blockade_id, action, actor_id, version, details, created_at FROM audit_events;
                DROP TABLE audit_events;
                ALTER TABLE audit_events_new RENAME TO audit_events;
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        info = item.get("schedule_info")
        item["schedule_info"] = json.loads(info) if info else None
        return item

    @staticmethod
    def _blockade_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    # ----- 事务入口 -----

    def transaction(self) -> sqlite3.Connection:
        """返回已执行 BEGIN IMMEDIATE 的连接；调用方负责 commit/rollback。"""
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    @staticmethod
    def commit(connection: sqlite3.Connection) -> None:
        connection.commit()

    @staticmethod
    def rollback(connection: sqlite3.Connection) -> None:
        connection.rollback()

    # ----- 幂等 -----

    def find_idempotent(self, connection: sqlite3.Connection, key: str, request_hash: str) -> Optional[Dict[str, Any]]:
        row = connection.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise Conflict("幂等键已用于不同请求")
        return {
            "replayed": True,
            "response_code": int(row["response_code"]),
            "response_body": json.loads(row["response_body"]),
        }

    def store_idempotent(
        self,
        connection: sqlite3.Connection,
        key: str,
        scope: str,
        request_hash: str,
        response_code: int,
        response_body: Dict[str, Any],
        now: str,
    ) -> None:
        connection.execute(
            "INSERT INTO idempotency(key,scope,request_hash,response_code,response_body,created_at) VALUES(?,?,?,?,?,?)",
            (key, scope, request_hash, response_code, json.dumps(response_body, ensure_ascii=False, sort_keys=True), now),
        )

    # ----- 靠泊计划 -----

    def create(
        self,
        connection: sqlite3.Connection,
        reference: str,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
        now: str,
    ) -> Dict[str, Any]:
        try:
            cursor = connection.execute(
                "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        record_id = int(cursor.lastrowid)
        self.insert_audit(
            connection,
            record_id=record_id,
            blockade_id=None,
            action="created",
            actor_id=actor_id,
            version=1,
            details={"state": state},
            now=now,
        )
        return self._row(connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone())

    def get(self, record_id: int, connection: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
        own = connection is None
        connection = connection or self._connect()
        try:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        finally:
            if own:
                connection.close()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(
        self, state: Optional[str] = None, limit: int = 100, connection: Optional[sqlite3.Connection] = None
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        own = connection is None
        connection = connection or self._connect()
        try:
            if state:
                rows = connection.execute(
                    "SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            if own:
                connection.close()
        return [self._row(row) for row in rows]

    def insert_audit(
        self,
        connection: sqlite3.Connection,
        record_id: Optional[int],
        blockade_id: Optional[int],
        action: str,
        actor_id: str,
        version: int,
        details: Dict[str, Any],
        now: str,
    ) -> None:
        connection.execute(
            "INSERT INTO audit_events(record_id,blockade_id,action,actor_id,version,details,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (record_id, blockade_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )

    def mutate(
        self,
        record_id: int,
        expected_version: int,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
        action: str,
        details: Dict[str, Any],
        connection: Optional[sqlite3.Connection] = None,
        schedule_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        own = connection is None
        connection = connection or self.transaction()
        try:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                if own:
                    connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                if own:
                    connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            return self._apply_record_change(
                connection, record_id, int(row["version"]), state, payload, schedule_info, actor_id, action, details
            )
        except Exception:
            if own:
                connection.rollback()
            raise
        finally:
            if own:
                connection.close()

    def _apply_record_change(
        self,
        connection: sqlite3.Connection,
        record_id: int,
        current_version: int,
        state: str,
        payload: Dict[str, Any],
        schedule_info: Optional[Dict[str, Any]],
        actor_id: str,
        action: str,
        details: Dict[str, Any],
    ) -> Dict[str, Any]:
        now = _now()
        version = current_version + 1
        info_json = json.dumps(schedule_info, ensure_ascii=False, sort_keys=True) if schedule_info is not None else None
        connection.execute(
            "UPDATE records SET state=?,version=?,payload=?,schedule_info=?,updated_by=?,updated_at=? WHERE id=?",
            (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), info_json, actor_id, now, record_id),
        )
        self.insert_audit(
            connection,
            record_id=record_id,
            blockade_id=(details or {}).get("blockade_id"),
            action=action,
            actor_id=actor_id,
            version=version,
            details=details,
            now=now,
        )
        result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return self._row(result)

    def force_record_change(
        self,
        connection: sqlite3.Connection,
        record: Dict[str, Any],
        state: str,
        payload: Dict[str, Any],
        schedule_info: Optional[Dict[str, Any]],
        actor_id: str,
        action: str,
        details: Dict[str, Any],
    ) -> Dict[str, Any]:
        """封锁联动改期使用：同一事务内推进版本，不做乐观锁检查。"""
        return self._apply_record_change(
            connection,
            int(record["id"]),
            int(record["version"]),
            state,
            payload,
            schedule_info,
            actor_id,
            action,
            details,
        )

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        return self._timeline("record_id", record_id)

    def _timeline(self, column: str, value: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE %s=? ORDER BY id" % column, (value,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    # ----- 封锁 -----

    def create_blockade(
        self,
        connection: sqlite3.Connection,
        fields: Dict[str, Any],
        actor_id: str,
        now: str,
    ) -> Dict[str, Any]:
        cursor = connection.execute(
            "INSERT INTO blockades(kind,target,start_hour,end_hour,status,reason,version,created_by,updated_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                fields["kind"],
                fields["target"],
                fields["start_hour"],
                fields["end_hour"],
                "active",
                fields.get("reason", ""),
                1,
                actor_id,
                actor_id,
                now,
                now,
            ),
        )
        blockade_id = int(cursor.lastrowid)
        self.insert_audit(
            connection,
            record_id=None,
            blockade_id=blockade_id,
            action="blockade_registered",
            actor_id=actor_id,
            version=1,
            details={
                "kind": fields["kind"],
                "target": fields["target"],
                "start_hour": fields["start_hour"],
                "end_hour": fields["end_hour"],
                "reason": fields.get("reason", ""),
            },
            now=now,
        )
        return self.get_blockade(blockade_id, connection)

    def get_blockade(self, blockade_id: int, connection: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
        own = connection is None
        connection = connection or self._connect()
        try:
            row = connection.execute("SELECT * FROM blockades WHERE id=?", (blockade_id,)).fetchone()
        finally:
            if own:
                connection.close()
        if row is None:
            raise NotFound("封锁不存在")
        return self._blockade_row(row)

    def list_blockades(
        self, status: Optional[str] = None, kind: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM blockades%s ORDER BY id DESC LIMIT ?" % where, tuple(params)
            ).fetchall()
        return [self._blockade_row(row) for row in rows]

    def active_blockades(self, connection: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
        own = connection is None
        connection = connection or self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM blockades WHERE status='active' ORDER BY id"
            ).fetchall()
        finally:
            if own:
                connection.close()
        return [self._blockade_row(row) for row in rows]

    def update_blockade(
        self,
        connection: sqlite3.Connection,
        blockade: Dict[str, Any],
        start_hour: int,
        end_hour: int,
        actor_id: str,
        action: str,
        details: Dict[str, Any],
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = _now()
        new_status = status or blockade["status"]
        new_version = int(blockade["version"]) + 1
        connection.execute(
            "UPDATE blockades SET start_hour=?,end_hour=?,status=?,version=?,updated_by=?,updated_at=? WHERE id=?",
            (start_hour, end_hour, new_status, new_version, actor_id, now, blockade["id"]),
        )
        self.insert_audit(
            connection,
            record_id=None,
            blockade_id=int(blockade["id"]),
            action=action,
            actor_id=actor_id,
            version=new_version,
            details=details,
            now=now,
        )
        return self.get_blockade(int(blockade["id"]), connection)

    def blockade_timeline(self, blockade_id: int) -> List[Dict[str, Any]]:
        self.get_blockade(blockade_id)
        return self._timeline("blockade_id", blockade_id)

    # ----- 统计 -----

    def stats(self) -> Dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
            blockade_rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM blockades GROUP BY status"
            ).fetchall()
        return {
            "records": {str(row["state"]): int(row["total"]) for row in rows},
            "blockades": {str(row["status"]): int(row["total"]) for row in blockade_rows},
        }

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
