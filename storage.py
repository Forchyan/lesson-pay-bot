"""Хранилище: занятия, отметки об оплате, цены по ученикам.

Работает в двух режимах:
  * облачная база Supabase  — через REST API, секретный ключ (SUPABASE_URL + SUPABASE_KEY);
  * локальный файл SQLite   — если облако не задано (тесты, резерв).

Интерфейс одинаковый в обоих режимах, остальной код этого не замечает.
Никаких внешних пакетов не требуется.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ical import normalize

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    key           TEXT PRIMARY KEY,
    uid           TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    start_utc     TEXT NOT NULL,
    end_utc       TEXT NOT NULL,
    all_day       INTEGER NOT NULL DEFAULT 0,
    paid          INTEGER NOT NULL DEFAULT 0,
    paid_at       TEXT,
    price         REAL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    manual        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_lessons_start ON lessons(start_utc);
CREATE INDEX IF NOT EXISTS idx_lessons_paid  ON lessons(paid);

CREATE TABLE IF NOT EXISTS prices (
    pattern    TEXT PRIMARY KEY,
    amount     REAL NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sent_summaries (
    week_start TEXT PRIMARY KEY,
    sent_at    TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class LessonRow:
    key: str
    uid: str
    summary: str
    start_utc: datetime
    end_utc: datetime
    all_day: bool
    paid: bool
    price: float | None
    manual: bool


@dataclass
class PriceRule:
    pattern: str
    amount: float
    label: str


# ----------------------------------------------------------------------
# Облачный backend (Supabase REST)
# ----------------------------------------------------------------------
class _Supabase:
    """Тонкая обёртка над PostgREST API Supabase."""

    def __init__(self, url: str, key: str) -> None:
        self.url = url.rstrip("/") + "/rest/v1/"
        self.key = key
        self._headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def _request(self, table: str, method: str, params: str = "", payload=None) -> list | dict:
        target = self.url + table + params
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(target, data=data, headers=self._headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise RuntimeError(f"Supabase {method} {table}: HTTP {resp.status}")
                raw = resp.read()
                return json.loads(raw) if raw else []
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"Supabase {method} {table}: HTTP {exc.code} {body}") from exc

    def select(self, table: str, params: str = "") -> list:
        result = self._request(table, "GET", params)
        return result if isinstance(result, list) else []

    def insert(self, table: str, rows: list[dict], on_conflict: str) -> None:
        if not rows:
            return
        params = f"?on_conflict={urllib.parse.quote(on_conflict)}"
        hdrs = dict(self._headers)
        hdrs["Prefer"] = "resolution=merge-duplicates,return=minimal"
        req = urllib.request.Request(
            self.url + table + params,
            data=json.dumps(rows).encode(),
            headers=hdrs,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise RuntimeError(f"Supabase POST {table}: HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"Supabase POST {table}: HTTP {exc.code} {body}") from exc

    def update(self, table: str, values: dict, filter_sql: str) -> None:
        params = "?" + filter_sql
        hdrs = dict(self._headers)
        hdrs["Prefer"] = "return=minimal"
        req = urllib.request.Request(
            self.url + table + params,
            data=json.dumps(values).encode(),
            headers=hdrs,
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise RuntimeError(f"Supabase PATCH {table}: HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"Supabase PATCH {table}: HTTP {exc.code} {body}") from exc

    def delete(self, table: str, filter_sql: str) -> None:
        req = urllib.request.Request(
            self.url + table + "?" + filter_sql,
            headers=self._headers,
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise RuntimeError(f"Supabase DELETE {table}: HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"Supabase DELETE {table}: HTTP {exc.code} {body}") from exc


class Storage:
    """Все операции с базой в одном классе.

    Если переданы supabase_url и supabase_key — работает с облаком, иначе
    с локальным SQLite-файлом.
    """

    def __init__(
        self,
        db_path: str = "",
        supabase_url: str = "",
        supabase_key: str = "",
    ) -> None:
        self.db_path = db_path
        self.conn = None
        self.supabase: _Supabase | None = None

        if supabase_url and supabase_key:
            self.supabase = _Supabase(supabase_url, supabase_key)
            return

        path = Path(db_path)
        if str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        conn.commit()
        self.conn = conn
        self.db_path = str(path)

    # ------------------------------------------------------------------
    # Служебные запросы, которые делает основной код напрямую
    # ------------------------------------------------------------------
    def all_lesson_keys(self) -> list[str]:
        if self.supabase:
            rows = self.supabase.select("lessons", "?select=key")
            return [row["key"] for row in rows]
        rows = self.conn.execute("SELECT key FROM lessons").fetchall()
        return [row["key"] for row in rows]

    def distinct_summaries(self) -> list[str]:
        if self.supabase:
            rows = self.supabase.select(
                "lessons", "?select=summary&summary=not.is.null&order=summary.asc"
            )
            seen: dict[str, int] = {}
            for row in rows:
                s = row.get("summary") or ""
                if s:
                    seen[s] = seen.get(s, 0) + 1
            return [k for k, v in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))]
        rows = self.conn.execute(
            "SELECT summary, COUNT(*) AS c FROM lessons WHERE summary <> ''"
            " GROUP BY summary ORDER BY c DESC, summary"
        ).fetchall()
        return [row["summary"] for row in rows]

    def week_span(self) -> tuple[datetime, datetime] | None:
        """Минимальный и максимальный start_utc среди занятий, или None."""
        if self.supabase:
            rows = self.supabase.select(
                "lessons", "?select=start_utc&order=start_utc.asc&limit=1"
            ) + self.supabase.select(
                "lessons", "?select=start_utc&order=start_utc.desc&limit=1"
            )
            if not rows:
                return None
            first = min(self._parse_utc(r["start_utc"]) for r in rows)
            last = max(self._parse_utc(r["start_utc"]) for r in rows)
            return first, last
        row = self.conn.execute(
            "SELECT MIN(start_utc) AS first, MAX(start_utc) AS last FROM lessons"
        ).fetchone()
        if not row or not row["first"]:
            return None
        return self._parse_utc(row["first"]), self._parse_utc(row["last"])

    @staticmethod
    def _parse_utc(value: str) -> datetime:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    # ------------------------------------------------------------------
    # Занятия
    # ------------------------------------------------------------------
    def upsert_lessons(self, occurrences) -> int:
        """Добавляет новые занятия из календаря и обновляет изменившиеся."""
        now = _now_iso()
        added = 0
        for occ in occurrences:
            start = occ.start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            end = occ.end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            lesson = {
                "key": occ.key,
                "uid": occ.uid,
                "summary": occ.summary,
                "start_utc": start,
                "end_utc": end,
                "all_day": int(occ.all_day),
                "paid": 0,
                "paid_at": None,
                "price": None,
                "first_seen": now,
                "last_seen": now,
                "manual": 0,
            }
            if self.supabase:
                existing = self.supabase.select(
                    "lessons", f"?select=key&key=eq.{urllib.parse.quote(occ.key)}"
                )
                if not existing:
                    self.supabase.insert("lessons", [lesson], "key")
                    added += 1
                else:
                    self.supabase.update(
                        "lessons",
                        {
                            "summary": occ.summary,
                            "start_utc": start,
                            "end_utc": end,
                            "all_day": int(occ.all_day),
                            "last_seen": now,
                        },
                        f"key=eq.{urllib.parse.quote(occ.key)}",
                    )
                continue
            existing = self.conn.execute(
                "SELECT key, summary, start_utc, paid, manual FROM lessons WHERE key = ?",
                (occ.key,),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO lessons (key, uid, summary, start_utc, end_utc, all_day,"
                    " paid, first_seen, last_seen, manual)"
                    " VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 0)",
                    (occ.key, occ.uid, occ.summary, start, end, int(occ.all_day), now, now),
                )
                added += 1
            else:
                self.conn.execute(
                    "UPDATE lessons SET summary = ?, start_utc = ?, end_utc = ?,"
                    " all_day = ?, last_seen = ? WHERE key = ?",
                    (occ.summary, start, end, int(occ.all_day), now, occ.key),
                )
        if not self.supabase:
            self.conn.commit()
        return added

    def lesson(self, key: str) -> LessonRow | None:
        if self.supabase:
            rows = self.supabase.select(
                "lessons", f"?select=*&key=eq.{urllib.parse.quote(key)}&limit=1"
            )
            return self._to_lesson(rows[0]) if rows else None
        row = self.conn.execute("SELECT * FROM lessons WHERE key = ?", (key,)).fetchone()
        return self._to_lesson(row) if row else None

    def lessons_between(self, start_utc: datetime, end_utc: datetime) -> list[LessonRow]:
        s = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        e = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        if self.supabase:
            rows = self.supabase.select(
                "lessons",
                f"?select=*&start_utc=gte.{urllib.parse.quote(s)}"
                f"&start_utc=lt.{urllib.parse.quote(e)}&order=start_utc.asc",
            )
            return [self._to_lesson(row) for row in rows]
        rows = self.conn.execute(
            "SELECT * FROM lessons WHERE start_utc >= ? AND start_utc < ?"
            " ORDER BY start_utc",
            (s, e),
        ).fetchall()
        return [self._to_lesson(row) for row in rows]

    def remove_lessons_before_hour(
        self, zone, min_hour: int, window_start_utc: datetime, window_end_utc: datetime
    ) -> int:
        """Удаляет занятия, начинающиеся раньше min_hour по местному времени."""
        if min_hour <= 0 or min_hour > 24:
            return 0
        s = window_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        e = window_end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        if self.supabase:
            rows = self.supabase.select(
                "lessons",
                f"?select=key,start_utc,manual&manual=eq.0"
                f"&start_utc=gte.{urllib.parse.quote(s)}"
                f"&start_utc=lt.{urllib.parse.quote(e)}",
            )
        else:
            rows = self.conn.execute(
                "SELECT key, start_utc, manual FROM lessons WHERE manual = 0"
                " AND start_utc >= ? AND start_utc < ?",
                (s, e),
            ).fetchall()
        doomed: list[str] = []
        for row in rows:
            start_utc = self._parse_utc(row["start_utc"])
            local = start_utc.astimezone(zone)
            if local.hour < min_hour:
                doomed.append(row["key"])
        if doomed:
            if self.supabase:
                for key in doomed:
                    self.supabase.delete("lessons", f"key=eq.{urllib.parse.quote(key)}")
            else:
                self.conn.executemany(
                    "DELETE FROM lessons WHERE key = ?", [(key,) for key in doomed]
                )
                self.conn.commit()
        return len(doomed)

    def set_paid(self, key: str, paid: bool) -> LessonRow | None:
        value = 1 if paid else 0
        paid_at = _now_iso() if paid else None
        if self.supabase:
            self.supabase.update(
                "lessons", {"paid": value, "paid_at": paid_at},
                f"key=eq.{urllib.parse.quote(key)}",
            )
            return self.lesson(key)
        self.conn.execute(
            "UPDATE lessons SET paid = ?, paid_at = ? WHERE key = ?",
            (value, paid_at, key),
        )
        self.conn.commit()
        return self.lesson(key)

    def set_price(self, key: str, price: float | None) -> None:
        if self.supabase:
            self.supabase.update(
                "lessons", {"price": price}, f"key=eq.{urllib.parse.quote(key)}"
            )
            return
        self.conn.execute("UPDATE lessons SET price = ? WHERE key = ?", (price, key))
        self.conn.commit()

    def mark_all_paid(self, keys: list[str], paid: bool = True) -> int:
        if not keys:
            return 0
        value = 1 if paid else 0
        paid_at = _now_iso() if paid else None
        if self.supabase:
            for key in keys:
                self.supabase.update(
                    "lessons", {"paid": value, "paid_at": paid_at},
                    f"key=eq.{urllib.parse.quote(key)}",
                )
            return len(keys)
        self.conn.executemany(
            "UPDATE lessons SET paid = ?, paid_at = ? WHERE key = ?",
            [(value, paid_at, key) for key in keys],
        )
        self.conn.commit()
        return len(keys)

    def add_manual_lesson(
        self, summary: str, start_utc: datetime, end_utc: datetime
    ) -> LessonRow:
        now = _now_iso()
        key = f"manual:{start_utc.strftime('%Y%m%dT%H%M%S')}:{normalize(summary)[:40]}"
        lesson = {
            "key": key,
            "uid": "",
            "summary": summary,
            "start_utc": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_utc": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "all_day": 0,
            "paid": 0,
            "paid_at": None,
            "price": None,
            "first_seen": now,
            "last_seen": now,
            "manual": 1,
        }
        if self.supabase:
            self.supabase.insert("lessons", [lesson], "key")
            return self.lesson(key)  # type: ignore[return-value]
        self.conn.execute(
            "INSERT OR REPLACE INTO lessons (key, uid, summary, start_utc, end_utc,"
            " all_day, paid, first_seen, last_seen, manual)"
            " VALUES (?, '', ?, ?, ?, 0, 0, ?, ?, 1)",
            (
                key,
                summary,
                start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                now,
                now,
            ),
        )
        self.conn.commit()
        return self.lesson(key)  # type: ignore[return-value]

    def delete_lesson(self, key: str) -> None:
        if self.supabase:
            self.supabase.delete("lessons", f"key=eq.{urllib.parse.quote(key)}")
            return
        self.conn.execute("DELETE FROM lessons WHERE key = ?", (key,))
        self.conn.commit()

    def rename_lesson(self, key: str, summary: str) -> None:
        if self.supabase:
            self.supabase.update(
                "lessons", {"summary": summary}, f"key=eq.{urllib.parse.quote(key)}"
            )
            return
        self.conn.execute("UPDATE lessons SET summary = ? WHERE key = ?", (summary, key))
        self.conn.commit()

    # ------------------------------------------------------------------
    # Цены по ученикам
    # ------------------------------------------------------------------
    def set_price_rule(self, pattern: str, amount: float, label: str = "") -> None:
        pattern_norm = normalize(pattern)
        row = {
            "pattern": pattern_norm,
            "amount": float(amount),
            "label": label or pattern,
            "updated_at": _now_iso(),
        }
        if self.supabase:
            self.supabase.insert("prices", [row], "pattern")
            return
        self.conn.execute(
            "INSERT INTO prices (pattern, amount, label, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(pattern) DO UPDATE SET amount = excluded.amount,"
            " label = excluded.label, updated_at = excluded.updated_at",
            (pattern_norm, float(amount), label or pattern, _now_iso()),
        )
        self.conn.commit()

    def delete_price_rule(self, pattern: str) -> bool:
        if self.supabase:
            self.supabase.delete("prices", f"pattern=eq.{urllib.parse.quote(normalize(pattern))}")
            return True
        cur = self.conn.execute("DELETE FROM prices WHERE pattern = ?", (normalize(pattern),))
        self.conn.commit()
        return cur.rowcount > 0

    def price_rules(self) -> list[PriceRule]:
        if self.supabase:
            rows = self.supabase.select("prices", "?select=pattern,amount,label&order=label.asc")
            out = [PriceRule(r["pattern"], r["amount"], r.get("label") or r["pattern"]) for r in rows]
            out.sort(key=lambda p: p.label)
            return out
        rows = self.conn.execute(
            "SELECT pattern, amount, label FROM prices ORDER BY length(pattern) DESC"
        ).fetchall()
        return [PriceRule(row["pattern"], row["amount"], row["label"]) for row in rows]

    def find_price(self, summary: str) -> tuple[float | None, str | None]:
        haystack = normalize(summary)
        exact = None
        for rule in self.price_rules():
            if rule.pattern == haystack:
                return rule.amount, rule.label
            if exact is None and rule.pattern and rule.pattern in haystack:
                exact = rule
        if exact is not None:
            return exact.amount, exact.label
        return None, None

    def lesson_price(self, lesson: LessonRow) -> tuple[float | None, str | None]:
        if lesson.price is not None:
            return lesson.price, None
        return self.find_price(lesson.summary)

    # ------------------------------------------------------------------
    # Настройки и отметки об отправленных итогах
    # ------------------------------------------------------------------
    def set_setting(self, key: str, value: str) -> None:
        if self.supabase:
            self.supabase.insert("settings", [{"key": key, "value": value}], "key")
            return
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        if self.supabase:
            rows = self.supabase.select(
                "settings", f"?select=value&key=eq.{urllib.parse.quote(key)}&limit=1"
            )
            return rows[0]["value"] if rows else default
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def summary_sent(self, week_start: str) -> bool:
        if self.supabase:
            rows = self.supabase.select(
                "sent_summaries",
                f"?select=week_start&week_start=eq.{urllib.parse.quote(week_start)}&limit=1",
            )
            return bool(rows)
        row = self.conn.execute(
            "SELECT 1 FROM sent_summaries WHERE week_start = ?", (week_start,)
        ).fetchone()
        return row is not None

    def mark_summary_sent(self, week_start: str) -> None:
        if self.supabase:
            self.supabase.insert(
                "sent_summaries", [{"week_start": week_start, "sent_at": _now_iso()}],
                "week_start",
            )
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO sent_summaries (week_start, sent_at) VALUES (?, ?)",
            (week_start, _now_iso()),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    def _to_lesson(self, row) -> LessonRow:
        return LessonRow(
            key=row["key"],
            uid=row["uid"],
            summary=row["summary"],
            start_utc=self._parse_utc(row["start_utc"]),
            end_utc=self._parse_utc(row["end_utc"]),
            all_day=bool(row["all_day"]),
            paid=bool(row["paid"]),
            price=row["price"],
            manual=bool(row["manual"]),
        )

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass