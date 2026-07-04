import { describe, expect, it } from "vitest";
import { parseDsn } from "../src/dsn";

describe("parseDsn", () => {
  it("parses a standard https DSN and strips the key from the URL", () => {
    const { url, key } = parseDsn(
      "https://abc123def@errors.example.com/api/ingest/3fa85f64-5717-4562-b3fc-2c963f66afa6/",
    );
    expect(key).toBe("abc123def");
    expect(url).toBe(
      "https://errors.example.com/api/ingest/3fa85f64-5717-4562-b3fc-2c963f66afa6/",
    );
    expect(url).not.toContain("abc123def@");
  });

  it("preserves an explicit port", () => {
    const { url, key } = parseDsn(
      "http://pubkey@localhost:8000/api/ingest/proj-1/",
    );
    expect(key).toBe("pubkey");
    expect(url).toBe("http://localhost:8000/api/ingest/proj-1/");
  });

  it("keeps the http scheme distinct from https", () => {
    expect(parseDsn("http://k@h/api/ingest/p/").url).toMatch(/^http:\/\//);
    expect(parseDsn("https://k@h/api/ingest/p/").url).toMatch(/^https:\/\//);
  });

  it("throws when the key (userinfo) is missing", () => {
    expect(() =>
      parseDsn("https://errors.example.com/api/ingest/proj-1/"),
    ).toThrow(/missing the public key/);
  });

  it("throws on a non-URL string", () => {
    expect(() => parseDsn("not a url")).toThrow(/not a valid URL/);
  });
});
