"""Worker error taxonomy — transient vs permanent failures.

Every processing failure is classified as one of two base types:

- ``TransientError`` — the failure may resolve on retry (network hiccup, SMTP
  timeout). The consumer retries with exponential backoff.
- ``PermanentError`` — the failure cannot be resolved by retrying (missing
  recipient, unknown event type). The consumer parks the job as ``failed``.

Extension points for downstream tasks
--------------------------------------
Task 5 (mailer) defines ``TransientSendError(TransientError)`` and
``PermanentSendError(PermanentError)`` for SMTP-specific classifications.
Task 7 (consumer) catches ``PermanentError`` and ``TransientError`` at the
top of the dispatch loop to route jobs to the correct finalizer.
"""

from __future__ import annotations


class WorkerError(Exception):
    """Base for worker delivery-processing errors."""


class TransientError(WorkerError):
    """A failure that should be retried with backoff."""


class PermanentError(WorkerError):
    """A failure that must not be retried — park the job as failed."""


class RecipientNotFound(PermanentError):
    """The job's user or event no longer exists."""
