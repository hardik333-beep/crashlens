// Fixture: init the SDK, then throw an uncaught exception with no other
// uncaughtException listener. The SDK's handler should capture, flush, print
// the error, and exit(1) (mirroring Node's default).
const sdk = await import(process.env.SDK_PATH);
sdk.init({ dsn: process.env.DSN });
setTimeout(() => {
  throw new Error("boom-uncaught-sole");
}, 10);
