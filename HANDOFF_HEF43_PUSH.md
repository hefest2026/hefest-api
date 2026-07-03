# HEF-43 push delivery — handoff (in progress, interrupted at 99% usage)

## Goal
Wire actual Expo push sending into the **Python** notification worker inside
`hefest-api` (NOT the planned separate C++ `hefest-worker` repo — that repo
doesn't exist; user confirmed the real worker is this Python one at
`hefest-api/hefest/worker/`).

Context: mobile app (hefest-mobile) already registers/unregisters Expo push
tokens via `/devices/register` `/devices/unregister` (HEF-45, done). The
`NotificationJob` outbox + worker already deliver **email** end-to-end
(`hefest/worker/consumer.py`, `mailer.py`, `templates.py`). Nothing sent an
actual Expo push yet — that's this task.

## Design decision (important, don't relitigate)
Push is a **best-effort side channel**, NOT part of the job's at-least-once/
retry contract. Rationale: the outbox's retry logic exists to guarantee
*email* delivery; if push-sending were allowed to fail/retry the job, a push
hiccup would cause a duplicate **email** resend. So:
- Push is attempted only **after** the email send succeeds, right before
  `mark_completed`.
- Any exception from push (rendering, DB token lookup, HTTP to Expo) is
  caught broadly and logged (`logger.exception`) — never raised, never
  affects `mark_completed`/`mark_retry`/`mark_failed`.
- `Pusher.send_to_tokens` itself also never raises — transport errors are
  logged and swallowed inside `pusher.py`.

Push is skipped for `EmailVerify` jobs (account-scoped, no `event`, and in
the mobile app's own flow no device token is registered before sign-in
anyway).

## Files created (done)
- `hefest/worker/push_templates.py` — pure `render(event_type, event, payload) -> PushContent(title, body)`. Mirrors `templates.py` minus `EmailVerify`. Same event types: `RegistrationConfirmed`, `RegistrationWaitlisted`, `WaitlistPromoted`, `RegistrationCancelled`, `EventCancelled`. Raises `PermanentError` for unknown type / `event is None` (unused in practice since consumer checks `event is None` first, but kept for parity).
- `hefest/worker/pusher.py` — `Pusher` class, parallel to `Mailer`:
  - `__init__(settings)`: builds `httpx.AsyncClient` with optional Bearer header from `settings.expo_access_token`.
  - `async def send_to_tokens(tokens, content, data)`: batches ≤100 tokens per Expo request, POSTs to `settings.expo_push_url`, parses per-ticket response, deletes `Device` rows whose ticket error is `DeviceNotRegistered`. Never raises — `httpx.HTTPError`/`ValueError` caught and logged.
  - `async def aclose()`.

## Files edited (done)
- `hefest/config.py` — added `expo_push_url` (default `https://exp.host/--/api/v2/push/send`), `expo_access_token` (default `""`), `expo_push_timeout` (default `10`).
- `.env.example` — added the three `HEFEST_EXPO_*` vars.
- `hefest/worker/recipients.py` — added `async def load_push_tokens(user) -> list[str]` (queries `Device.filter(user=user).only("expo_push_token")`, returns token strings). **Note:** had to avoid `.values_list(..., flat=True)` — `ty check` flags its return type as `list[tuple[Any,...]]` not `list[str]`; used `.only()` + list comprehension instead.
- `hefest/worker/consumer.py`:
  - imports `push_templates`, `Pusher` (TYPE_CHECKING).
  - new `async def _send_push(job, recipient, pusher)`: returns early if `recipient.event is None`; else loads tokens, renders `PushContent`, calls `pusher.send_to_tokens(tokens, content, {"event_id": str(recipient.event.id)})`; wraps everything in `except Exception: logger.exception(...)`.
  - `_process_one` now takes `pusher: Pusher` param; calls `await _send_push(job, recipient, pusher)` right before the final `await _finalize(mark_completed, job, worker_id)` (i.e. only on the success path, after email sent).
  - `_bounded_process`, `_drain`, `run` all now thread `pusher: Pusher` through as an extra positional param (same position as `mailer`, right after).
- `hefest/worker/__main__.py` — constructs `pusher = Pusher(settings)`, passes to `consumer.run(worker_id, mailer, pusher, heartbeat, stop)`, and calls `await pusher.aclose()` in the shutdown `finally` block (alongside `mailer.aclose()`).

## Tests — status
- `hefest/worker/pusher.py` and `push_templates.py`: **no dedicated unit tests written yet.** Should add `tests/test_worker_pusher.py` (mirror `tests/test_worker_mailer.py` style: fake settings dataclass, monkeypatch `httpx.AsyncClient` or use `respx`/a fake transport) and `tests/test_worker_push_templates.py` (mirror `tests/test_worker_templates.py` — pure function, `SimpleNamespace` stubs, one test per event type + unknown-type/`event=None` PermanentError cases).
- `tests/test_worker_consumer.py`: **updated and passing** (`uv run pytest tests/test_worker_consumer.py -q` → 23 passed, then grew after adding 2 more push-specific tests — re-run to confirm count). Changes made:
  - `recipient_ok` fixture now also mocks `consumer.recipients.load_push_tokens` → `[]` (so existing tests don't hit real DB and don't trigger push).
  - Added `test_success_sends_push_to_registered_tokens` — asserts `pusher.send_to_tokens` called with correct tokens/content/data when tokens exist. Uses `SimpleNamespace(id="e1")` for `recipient.event` (bare `object()` breaks because consumer code does `recipient.event.id`).
  - Added `test_push_failure_does_not_affect_job_outcome` — pusher raises, asserts `mark_completed` still called, no retry/fail.
  - All other `_process_one(...)` and `_drain(...)` call sites updated to pass an extra `AsyncMock()` positional arg for `pusher` (one was missed by the first `sed` pass — `test_unexpected_error_propagates` used `_job()` inline instead of a `job` variable — fixed manually).
- `tests/test_worker_integration.py`: **IN PROGRESS, NOT FINISHED.** This is where the session was cut off.
  - Added a `StubPusher` class (mirrors `StubMailer`) right after `StubMailer` in the "Test doubles" section — but its `send_to_tokens` currently just raises `AssertionError` unconditionally on call, on the assumption these integration-test users never have registered `Device` rows so `_send_push` returns early before reaching the pusher. **This assumption has NOT been verified** — check the `_create_student`/`_create_organizer` helpers in that file to confirm no `Device` rows are ever created for integration-test users. If they might be, `StubPusher` needs an actual no-op record-based implementation instead (like `StubMailer.sent`), not an assertion-raiser.
  - **Still TODO in this file:** the three call sites at (approximately) lines 281, 399, 524 —
    ```python
    await consumer._process_one(claimed, worker_id, cast(Any, mailer))
    ```
    — each needs a `StubPusher()` instance added as the 4th arg:
    ```python
    await consumer._process_one(claimed, worker_id, cast(Any, mailer), cast(Any, pusher))
    ```
    Need to instantiate `pusher = StubPusher()` near each `mailer = StubMailer()` line (3 places: ~275, ~387, ~519).
  - After that, run `uv run pytest tests/test_worker_integration.py -q` (needs Docker for testcontainers Postgres — may skip if unavailable, that's fine, just confirm it doesn't error at collection/signature level).

## Remaining checklist (in order)
1. Finish `test_worker_integration.py`: add `pusher = StubPusher()` at the 3 call sites, pass as 4th arg. Verify the "no Device rows for test users" assumption; adjust `StubPusher` to be a recording no-op instead of an assertion-raiser if needed (safer default regardless — an assertion-raiser is a footgun for future test authors).
2. Write `tests/test_worker_pusher.py` — unit tests for `Pusher`:
   - Successful batch send (mock `httpx.AsyncClient.post`), verify request body shape (`to`/`title`/`body`/`data` per message).
   - `DeviceNotRegistered` ticket → verify `Device.filter(expo_push_token__in=[...]).delete()` called (will need real or mocked Tortoise `Device` — check how `test_services_device.py` sets up DB access, likely needs the `db` fixture / integration marker, OR mock `Device.filter` entirely like the mailer test mocks SMTP).
   - Non-`DeviceNotRegistered` error ticket → logged, not deleted, no raise.
   - `httpx.HTTPError` on POST → logged, no raise, no tokens deleted.
   - >100 tokens → verify batching (multiple POSTs).
   - Bearer header only sent when `expo_access_token` set.
3. Write `tests/test_worker_push_templates.py` — mirror `test_worker_templates.py` structure exactly, one test per event type, plus `unknown event_type` and `event=None` → `PermanentError`.
4. Run full suite: `cd hefest-api && uv run pytest -q` (non-integration) and with Docker if available for integration tests.
5. Run `uv run ty check hefest/worker/ hefest/config.py hefest/worker/__main__.py` and `uv run ruff check hefest/ tests/` + `uv run ruff format --check hefest/ tests/` — last known state: **ty and ruff both clean** as of the last run before this handoff (checked on `pusher.py`, `push_templates.py`, `consumer.py`, `recipients.py`, `__main__.py`, `config.py` — but NOT yet re-checked after the `test_worker_consumer.py` edits or the in-progress `test_worker_integration.py` edits).
6. Update `hefest-api/CLAUDE.md` or the Jira HEF-43 ticket status if the user wants it marked done on the backend side (currently only the mobile half was previously marked; this session's work is the backend/worker half).
7. **Not done, out of scope unless asked:** no commit was made for any of this backend work yet. `hefest-api` is on branch `feat/organizer-dashboard-settings` (per earlier session state) with pre-existing uncommitted changes (`.env.example`, `config.py`, `sso.py`, `registration.py`, `registration_service.py`) from before this task started — **do not blindly `git add -A`**; review `git status`/`git diff` first and stage only the HEF-43-related files (`hefest/worker/pusher.py`, `hefest/worker/push_templates.py`, `hefest/worker/consumer.py`, `hefest/worker/recipients.py`, `hefest/worker/__main__.py`, `hefest/config.py`, `.env.example`, `tests/test_worker_consumer.py`, `tests/test_worker_integration.py`, plus the two new test files once written) separately from whatever unrelated pre-existing diff is sitting in that branch.

## Quick resume command
```bash
cd /home/hexchap/Projects/HefestProject/hefest-api
git status --short   # see what's dirty; don't assume, re-check
uv run pytest tests/test_worker_consumer.py tests/test_worker_integration.py -q
```
