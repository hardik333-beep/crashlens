"""Redis token-bucket rate limiter for the ingest hot path.

Keyed per DSN key id, and called ONLY AFTER the key has been authenticated, so
the limit is a hard per-principal project gate that never degrades to per-IP (a
limiter keyed before auth would see only the client IP and let a single abusive
key hide behind rotating IPs, or let many honest clients behind one NAT starve
each other; see the ``ratelimit-key-before-auth`` rule).

ATOMICITY: the bucket state (token count + last-refill timestamp) lives in one
Redis hash mutated by a single Lua script. The whole read-refill-decide-write
cycle therefore runs atomically on the Redis server, so two concurrent ingest
requests for the same key can never both read the same stale token count and
double-spend it. No WATCH loop, no client-side race.

FLAGGED DEFAULTS (governor review requested): capacity 120 tokens, refill 60
tokens/minute (1 token/second). A client may burst up to 120 events, then
sustain 1 event/second. Both constants are named here so they are trivial to
retune.
"""

import math
from dataclasses import dataclass

# --- FLAGGED product defaults -------------------------------------------------
BUCKET_CAPACITY = 120
REFILL_PER_MINUTE = 60
REFILL_PER_SECOND = REFILL_PER_MINUTE / 60.0

# Redis key namespace for the per-DSN-key buckets.
KEY_PREFIX = "ingest:ratelimit:"

# Idle buckets expire so Redis does not accumulate a key per one-off sender.
# BUCKET_CAPACITY / REFILL_PER_SECOND is the time for an empty bucket to refill
# to capacity; a comfortable margin past it guarantees any expired bucket was
# already back to full, so expiry never wrongly penalises a returning client.
TTL_SECONDS = int(BUCKET_CAPACITY / REFILL_PER_SECOND) + 60

# Atomic token bucket. ``now`` is passed in by the caller so the arithmetic is
# deterministic and unit-testable; the entire read-modify-write still executes
# atomically inside Redis. Token count is returned as a STRING because Redis
# coerces a Lua number reply to an integer, which would drop the fractional part
# the Retry-After calculation depends on.
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * refill)

local allowed = 0
if tokens >= requested then
  tokens = tokens - requested
  allowed = 1
end

redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', key, ttl)
return {allowed, tostring(tokens)}
"""


@dataclass
class RateLimitDecision:
    """The outcome of a token-bucket check."""

    allowed: bool
    retry_after: int  # whole seconds until a token is available (0 when allowed)


def _as_float(value: object) -> float:
    if isinstance(value, bytes | bytearray):
        return float(value.decode())
    return float(value)  # type: ignore[arg-type]


def retry_after_seconds(
    tokens_available: float, refill_per_second: float = REFILL_PER_SECOND
) -> int:
    """Return whole seconds until one token has accrued, given the current level.

    A denied request has ``tokens_available < 1``. The wait is the time to accrue
    the ``1 - tokens_available`` deficit at ``refill_per_second``, rounded UP so
    the client never retries a shade too early, and floored at 1 so a ``429``
    never advertises an immediate retry.
    """
    deficit = 1.0 - tokens_available
    if deficit <= 0:
        return 0
    return max(1, math.ceil(deficit / refill_per_second))


async def check_rate_limit(
    redis_client: object, key_id: object, now: float
) -> RateLimitDecision:
    """Consume one token for ``key_id`` and return the decision.

    ``redis_client`` is any ``redis.asyncio.Redis`` (the shared arq pool is one,
    so ingest reuses a single connection pool for both the limiter and the
    enqueue). ``now`` is the current wall-clock time in seconds.
    """
    bucket_key = f"{KEY_PREFIX}{key_id}"
    allowed_raw, tokens_raw = await redis_client.eval(  # type: ignore[attr-defined]
        _TOKEN_BUCKET_LUA,
        1,
        bucket_key,
        BUCKET_CAPACITY,
        REFILL_PER_SECOND,
        now,
        1,
        TTL_SECONDS,
    )
    if int(_as_float(allowed_raw)) == 1:
        return RateLimitDecision(allowed=True, retry_after=0)
    tokens = _as_float(tokens_raw)
    return RateLimitDecision(
        allowed=False, retry_after=retry_after_seconds(tokens)
    )
