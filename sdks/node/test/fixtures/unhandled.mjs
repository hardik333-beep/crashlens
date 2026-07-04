// Fixture: init the SDK, then create an unhandled promise rejection. The SDK
// must capture it WITHOUT altering process behaviour, so the process exits 0
// under its own control after the event has had time to send.
const sdk = await import(process.env.SDK_PATH);
sdk.init({ dsn: process.env.DSN });
Promise.reject(new Error("boom-unhandled-rejection"));
setTimeout(() => process.exit(0), 700);
