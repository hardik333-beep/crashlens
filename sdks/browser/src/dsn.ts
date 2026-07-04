// DSN parsing. A Crashlens DSN carries the ingest endpoint and the public key
// as the userinfo half of a URL:
//
//   http(s)://<public_key>@<host>[:port]/api/ingest/<project_id>/
//
// We parse the key out of the userinfo and rebuild the URL without it; what
// remains is exactly the POST target (docs/PROTOCOL.md section 1).

export interface ParsedDsn {
  url: string;
  key: string;
}

export function parseDsn(dsn: string): ParsedDsn {
  let parsed: URL;
  try {
    parsed = new URL(dsn);
  } catch {
    throw new Error("Crashlens: DSN is not a valid URL");
  }
  const key = parsed.username;
  if (!key) {
    throw new Error("Crashlens: DSN is missing the public key");
  }
  // Strip the userinfo so the remaining URL is the plain ingest endpoint.
  parsed.username = "";
  parsed.password = "";
  return { url: parsed.toString(), key };
}
