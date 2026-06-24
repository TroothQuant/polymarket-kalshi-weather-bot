import sqlite3, sys
db = sys.argv[1]
con = sqlite3.connect(db)
cols = [r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()]
if "order_id" in cols:
    print(f"order_id already present in {db} — no-op (idempotent)")
else:
    con.execute("ALTER TABLE trades ADD COLUMN order_id VARCHAR")
    con.execute("CREATE INDEX IF NOT EXISTS ix_trades_order_id ON trades(order_id)")
    con.commit()
    print(f"ALTER applied: added order_id to trades in {db}")
cols2 = [r[1] for r in con.execute("PRAGMA table_info(trades)").fetchall()]
assert "order_id" in cols2, "order_id MISSING after migration!"
print("VERIFY order_id present:", "order_id" in cols2)
con.close()
