"""Background job functions run by the arq worker (see ``app/worker.py``).

* ``process_event`` -- consumes ingested envelopes, fingerprints them into
  Issues, and stores the event rows.
* ``retention`` -- partition maintenance and retention enforcement cron jobs.
"""
