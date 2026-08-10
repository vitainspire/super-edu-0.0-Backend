"""An in-memory stand-in for the postgrest client, enough for the query
shapes the substitute automation actually issues.

Not a general Supabase emulator — it supports eq/neq/in_/gte/lte filtering,
select, insert, upsert (with on_conflict), delete and maybe_single, because
that's the whole surface admin_queries and timetable_resolution use. Anything
outside that raises rather than quietly returning [], so a query the fake
can't model shows up as a test error instead of a passing test that proved
nothing.
"""


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store, table):
        self._store = store
        self._table = table
        self._filters = []          # (op, column, value)
        self._mode = "select"
        self._payload = None
        self._on_conflict = None
        self._single = False
        self._order = None

    # ── filters ──
    def eq(self, column, value):
        self._filters.append(("eq", column, value))
        return self

    def neq(self, column, value):
        self._filters.append(("neq", column, value))
        return self

    def in_(self, column, values):
        self._filters.append(("in", column, list(values)))
        return self

    def gte(self, column, value):
        self._filters.append(("gte", column, value))
        return self

    def lte(self, column, value):
        self._filters.append(("lte", column, value))
        return self

    def is_(self, column, value):
        self._filters.append(("is", column, None if value == "null" else value))
        return self

    # ── modifiers ──
    def select(self, *_columns):
        # Column projection is ignored: every caller reads keys it asked for,
        # so returning whole rows can't make a passing test that would fail
        # against real postgrest.
        self._mode = "select"
        return self

    def order(self, column, desc=False):
        self._order = (column, desc)
        return self

    def limit(self, _n):
        return self

    def maybe_single(self):
        self._single = True
        return self

    # ── writes ──
    def insert(self, payload):
        self._mode = "insert"
        self._payload = payload if isinstance(payload, list) else [payload]
        return self

    def upsert(self, payload, on_conflict=None):
        self._mode = "upsert"
        self._payload = payload if isinstance(payload, list) else [payload]
        self._on_conflict = on_conflict
        return self

    def update(self, payload):
        self._mode = "update"
        self._payload = payload
        return self

    def delete(self):
        self._mode = "delete"
        return self

    # ── execution ──
    def _matches(self, row):
        for op, column, value in self._filters:
            actual = row.get(column)
            if op == "eq" and actual != value:
                return False
            if op == "neq" and actual == value:
                return False
            if op == "in" and actual not in value:
                return False
            if op == "gte" and not (actual is not None and actual >= value):
                return False
            if op == "lte" and not (actual is not None and actual <= value):
                return False
            if op == "is" and actual is not value:
                return False
        return True

    def execute(self):
        rows = self._store.setdefault(self._table, [])

        if self._mode == "select":
            hits = [dict(r) for r in rows if self._matches(r)]
            if self._order:
                column, desc = self._order
                hits.sort(key=lambda r: (r.get(column) is None, r.get(column)), reverse=desc)
            if self._single:
                if len(hits) > 1:
                    raise AssertionError(f"maybe_single matched {len(hits)} rows in {self._table}")
                # Real postgrest returns a response whose .data is None on no
                # match; the calling code guards for exactly that.
                return _Result(hits[0] if hits else None)
            return _Result(hits)

        if self._mode == "insert":
            rows.extend(dict(p) for p in self._payload)
            return _Result([dict(p) for p in self._payload])

        if self._mode == "upsert":
            keys = [k.strip() for k in self._on_conflict.split(",")] if self._on_conflict else ["id"]
            for incoming in self._payload:
                existing = next(
                    (r for r in rows if all(r.get(k) == incoming.get(k) for k in keys)), None,
                )
                if existing:
                    existing.update(incoming)
                else:
                    rows.append(dict(incoming))
            return _Result([dict(p) for p in self._payload])

        if self._mode == "update":
            touched = [r for r in rows if self._matches(r)]
            for r in touched:
                r.update(self._payload)
            return _Result([dict(r) for r in touched])

        if self._mode == "delete":
            removed = [r for r in rows if self._matches(r)]
            self._store[self._table] = [r for r in rows if not self._matches(r)]
            return _Result([dict(r) for r in removed])

        raise NotImplementedError(self._mode)


class FakeClient:
    """`store` is {table_name: [row_dict, ...]} in raw snake_case DB shape."""

    def __init__(self, store=None):
        self.store = store if store is not None else {}

    def table(self, name):
        return _Query(self.store, name)

    def rows(self, name):
        return self.store.setdefault(name, [])
