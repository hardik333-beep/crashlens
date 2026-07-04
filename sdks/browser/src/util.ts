// Small dependency-free helpers.

// RFC4122 v4 UUID. Uses the platform crypto where available, falling back to a
// Math.random source only when neither crypto.randomUUID nor getRandomValues
// exists (never in a modern browser).
export function uuid4(): string {
  const c: Crypto | undefined =
    typeof crypto !== "undefined" ? crypto : undefined;
  if (c && typeof c.randomUUID === "function") {
    return c.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (c && typeof c.getRandomValues === "function") {
    c.getRandomValues(bytes);
  } else {
    for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex: string[] = [];
  for (let i = 0; i < 16; i++) hex.push(bytes[i].toString(16).padStart(2, "0"));
  return (
    hex[0] +
    hex[1] +
    hex[2] +
    hex[3] +
    "-" +
    hex[4] +
    hex[5] +
    "-" +
    hex[6] +
    hex[7] +
    "-" +
    hex[8] +
    hex[9] +
    "-" +
    hex[10] +
    hex[11] +
    hex[12] +
    hex[13] +
    hex[14] +
    hex[15]
  );
}

// RFC3339 UTC timestamp, e.g. "2026-07-04T12:00:00.000Z".
export function nowIso(): string {
  return new Date().toISOString();
}

// Stringify an arbitrary value for a breadcrumb, capped to `max` characters.
export function stringifyArg(value: unknown, max = 200): string {
  let out: string;
  try {
    if (typeof value === "string") {
      out = value;
    } else if (value instanceof Error) {
      out = value.name + ": " + value.message;
    } else {
      const json = JSON.stringify(value);
      out = json === undefined ? String(value) : json;
    }
  } catch {
    out = String(value);
  }
  return out.length > max ? out.slice(0, max) : out;
}

// Run a function, swallowing any error so the SDK never throws into the host
// page (a Tier-1 requirement of this SDK).
export function guard<T>(fn: () => T): T | undefined {
  try {
    return fn();
  } catch {
    return undefined;
  }
}
