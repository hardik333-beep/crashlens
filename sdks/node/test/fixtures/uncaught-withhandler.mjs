// Fixture: init the SDK AND register another uncaughtException listener. The
// SDK must capture but NOT force exit(1); the user's listener decides the exit
// (here: exit code 7 after a short delay to let the flush complete).
const sdk = await import(process.env.SDK_PATH);
sdk.init({ dsn: process.env.DSN });
process.on("uncaughtException", () => {
  setTimeout(() => process.exit(7), 500);
});
setTimeout(() => {
  throw new Error("boom-uncaught-withhandler");
}, 10);
