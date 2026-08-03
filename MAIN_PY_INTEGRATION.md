# main.py integration patch

Every change below is an **addition** — either a new line or a small, localized
replacement. Nothing about existing routes, templates, or business logic changes
except where explicitly noted (the two `transactions`→`transactions_active` /
`savings_goals`→`savings_goals_active` swaps, and turning the hard `DELETE` in
`delete_transaction` into a soft delete).

Apply each block with your editor's find/replace, top to bottom.

---

## 1. Imports (Section 1)

```python
import database  # all persistence (DB file, schema, folders, backups) lives here now
import network   # NEW — internet connectivity detection
import sync      # NEW — sync engine (push/pull/conflict resolution/background loop)
import api as sync_api  # NEW — /sync/* JSON endpoints (Blueprint)
```

## 2. Register the sync Blueprint (Section 2, right after `app = Flask(__name__)` / config block)

```python
app.register_blueprint(sync_api.bp)
```

## 3. Start the background scheduler (Section 17, right after `database.init_db()`)

```python
database.init_db()
sync.start_background_scheduler(interval_seconds=300)  # NEW — startup + every 5 min
```

## 4. Login hook (Section 7 — `login()`)

Right after the existing `db.commit()` for `last_login`, before the `flash(...)`:

```python
            db.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.now().isoformat(), user["id"]))
            db.commit()
            sync.trigger_async_sync("login")  # NEW — fire-and-forget, never blocks the response
            flash(f"Welcome back, {user['full_name'].split(' ')[0]}!", "success")
```

## 5. Logout hook (Section 7 — `logout()`)

```python
@app.route("/logout", methods=["POST"])
def logout():
    sync.trigger_async_sync("logout")  # NEW — push any last local changes before clearing session
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))
```

## 6. New user gets sync metadata (Section 7 — `register()`)

Right after `user_id = cur.lastrowid`:

```python
        user_id = cur.lastrowid
        database.mark_created(db, "users", user_id)  # NEW
        db.commit()
```

## 7. Allowance (Section 9 — `add_allowance`)

```python
        db = get_db()
        cur = db.execute(
            "INSERT INTO transactions (user_id, type, amount, category, date, notes, recurring, created_at) "
            "VALUES (?, 'allowance', ?, ?, ?, ?, ?, ?)",
            (session["user_id"], amount, source, date, notes, recurring, datetime.now().isoformat())
        )
        database.mark_created(db, "transactions", cur.lastrowid)  # NEW
        db.commit()
```

## 8. Expense (Section 10 — `add_expense`)

Same pattern:

```python
        db = get_db()
        cur = db.execute(
            "INSERT INTO transactions (user_id, type, amount, category, date, notes, receipt, created_at) "
            "VALUES (?, 'expense', ?, ?, ?, ?, ?, ?)",
            (session["user_id"], amount, category, date, notes, receipt, datetime.now().isoformat())
        )
        database.mark_created(db, "transactions", cur.lastrowid)  # NEW
        db.commit()
```

## 9. Transactions list — read from the active view (Section 11 — `transactions()`)

Only the `FROM` clause changes:

```python
    sql = "SELECT * FROM transactions_active WHERE user_id = ?"   # was: FROM transactions
```

## 10. Edit transaction (Section 11 — `edit_transaction`)

```python
        else:
            db.execute("UPDATE transactions SET amount=?, category=?, date=?, notes=? WHERE id=? AND user_id=?",
                       (amount, category, date, notes, tx_id, session["user_id"]))
            database.mark_updated(db, "transactions", tx_id)  # NEW
            db.commit()
```

## 11. Delete transaction → soft delete (Section 11 — `delete_transaction`)

Replace the hard `DELETE` with a soft delete so the row survives for sync but still
disappears from every existing view exactly as before:

```python
@app.route("/transactions/<int:tx_id>/delete", methods=["POST"])
@login_required
def delete_transaction(tx_id):
    db = get_db()
    t = db.execute("SELECT id FROM transactions WHERE id=? AND user_id=?", (tx_id, session["user_id"])).fetchone()
    if t:
        database.mark_deleted(db, "transactions", tx_id)  # was: DELETE FROM transactions ...
        db.commit()
    flash("Transaction deleted.", "info")
    return redirect(url_for("transactions"))
```

## 12. Exports — read from the active view (Section 11 — `export_csv`, `export_print`)

Both currently do `SELECT * FROM transactions WHERE user_id=?` — change the table name
the same way as step 9: `FROM transactions_active`.

## 13. Dashboard / budget stats — read from the active view (Section 5 — `get_stats`, `get_budget_stats`)

In `get_stats()`'s inner `total()` helper and in `get_budget_stats()`, change:

```python
q = "SELECT COALESCE(SUM(amount),0) t FROM transactions WHERE user_id=? AND type=?"
```
to
```python
q = "SELECT COALESCE(SUM(amount),0) t FROM transactions_active WHERE user_id=? AND type=?"
```
(both occurrences of `FROM transactions` inside `get_budget_stats` — the sum and the count query).

Also update the dashboard's "recent transactions" query and the reports page query the
same way (any remaining raw `FROM transactions` that reads rows for display).

## 14. Budget update (Section 12 — `budget()`)

```python
        db.execute(f"UPDATE users SET {column}=? WHERE id=?", (amount, session["user_id"]))
        database.mark_updated(db, "users", session["user_id"])  # NEW
        db.commit()
```

## 15. Savings goals list — read from the active view (Section 13 — `savings()`)

```python
    goals = db.execute("SELECT * FROM savings_goals_active WHERE user_id=? ORDER BY created_at DESC", (session["user_id"],)).fetchall()
```

## 16. Add savings goal (Section 13 — `add_goal`)

```python
        cur = db.execute(
            "INSERT INTO savings_goals (user_id, goal_name, goal_amount, current_saved, deadline, created_at) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            (session["user_id"], goal_name, goal_amount, deadline, datetime.now().isoformat())
        )
        database.mark_created(db, "savings_goals", cur.lastrowid)  # NEW
        db.commit()
```

## 17. Savings transfer (Section 13 — `add_savings_tx`)

```python
    cur = db.execute(
        "INSERT INTO transactions (user_id, type, amount, category, date, notes, goal_id, created_at) "
        "VALUES (?, 'savings', ?, ?, ?, '', ?, ?)",
        (session["user_id"], amount, "Savings Transfer", datetime.now().strftime("%Y-%m-%d"), goal_id, datetime.now().isoformat())
    )
    database.mark_created(db, "transactions", cur.lastrowid)  # NEW
    if goal_id:
        db.execute("UPDATE savings_goals SET current_saved = current_saved + ? WHERE id=? AND user_id=?",
                   (amount, goal_id, session["user_id"]))
        database.mark_updated(db, "savings_goals", goal_id)  # NEW
    db.commit()
```

## 18. Settings routes (Section 15) — one line each, after every UPDATE + before commit

Every settings route follows the identical pattern. Add
`database.mark_updated(db, "users", session["user_id"])` immediately after each
`db.execute("UPDATE users SET ...")` and before `db.commit()`, in:

- `update_profile` (full_name/username/bio)
- `update_picture` (profile_pic, both the "remove" branch and the upload branch)
- `update_cover_photo` (cover_photo, both branches)
- `change_password` (password_hash) — safe to sync; only the *hash* travels, and
  hashes are already excluded from the cloud `users` table per `supabase_schema.sql`,
  so in practice this call is a no-op for the cloud but still keeps `updated_at`
  correct locally. You can skip this one specifically if you'd rather not touch it.
- `update_theme`, `update_notifications`, `update_session_timeout`

Example (`update_theme`):

```python
    db.execute("UPDATE users SET theme=? WHERE id=?", (theme, session["user_id"]))
    database.mark_updated(db, "users", session["user_id"])  # NEW
    db.commit()
```

## 19. Sync status badge (Section 6 — `APP_SHELL_HEAD`, in the top-bar `<div class="d-flex gap-2">`)

```html
    <div class="d-flex gap-2">
      <span id="sync-badge" class="icon-btn" title="Sync status" style="cursor:pointer" onclick="manualSync()">
        <span id="sync-icon">🟢</span>
      </span>
      <a href="{{ url_for('reports') }}" class="icon-btn" title="Notifications">
```

And a small script — add to `APP_SHELL_TAIL` (or anywhere already inside `{% block extra_scripts %}`):

```html
<script>
async function refreshSyncBadge(){
  try{
    const r = await fetch("{{ url_for('sync_api.status') }}");
    const d = await r.json();
    document.getElementById('sync-icon').textContent = d.icon;
    document.getElementById('sync-badge').title =
      d.label + (d.pending_count ? ` (${d.pending_count} pending)` : '');
  }catch(e){ /* stay silent — badge just won't update this tick */ }
}
async function manualSync(){
  document.getElementById('sync-icon').textContent = '🔄';
  await fetch("{{ url_for('sync_api.manual_sync') }}", {method: 'POST'});
  refreshSyncBadge();
}
refreshSyncBadge();
setInterval(refreshSyncBadge, 20000);
</script>
```

This gives every logged-in page (the shell is shared) the 🟢/🔴/🔄/✅/⚠ indicator and
a tap-to-sync button, with zero new templates.

## 20. `.env` additions

```
SUPABASE_URL=https://YOUR-PROJECT.supabase.co
SUPABASE_SERVICE_KEY=your-service-role-key
```
(Project Settings -> API in the Supabase dashboard. Use the **service_role** key, not
`anon` — see the RLS note in `supabase_schema.sql`. Never commit `.env`.)

## 21. `requirements.txt` additions

```
requests
cryptography   # optional — only needed if you use cloud.save_encrypted_credentials()
```

---

### That's the entire integration surface
21 small, mechanical edits, all additive or one-line. No template is restructured,
no route is removed, no existing query result changes shape — the only user-visible
differences are the new sync badge/button and that deleted transactions now persist
in the database (invisible to the UI, same as before).
