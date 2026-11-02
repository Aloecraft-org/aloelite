// The Worker half of demo.html?opfs: one OPFS volume file, served over
// postMessage. aloelite-wasm/README.md's Worker, plus two things a page
// that reloads needs:
// - a queue for the requests the page sends while init and the pool open
//   are still awaiting, since a module Worker drops messages that arrive
//   before anything listens;
// - a retry on `busy`, because the previous page's Worker can still hold
//   the volume's Web Lock for a moment after a reload.
// If the open still fails, every request is answered with that error.
import init, { Pool, serve } from "../../pkg/aloelite_wasm.js";

const VOLUME_FILE = "explorer-demo.db";
const BUSY_RETRY_MS = 100;
const BUSY_GIVE_UP_MS = 5000;

const early = [];
self.onmessage = (event) => early.push(event);

try {
  await init();
  const pool = await Pool.install();
  const fs = await openWhenFree(pool, VOLUME_FILE);
  self.server = serve(fs, self); // replaces onmessage; kept so it is never collected
} catch (e) {
  const error = { code: e.code ?? "internal", message: String(e.message ?? e) };
  self.onmessage = ({ data }) => self.postMessage({ id: data?.id, error });
}
for (const event of early) self.onmessage(event);

async function openWhenFree(pool, name) {
  const deadline = Date.now() + BUSY_GIVE_UP_MS;
  for (;;) {
    try {
      return await pool.open(name);
    } catch (e) {
      if (e.code !== "busy" || Date.now() > deadline) throw e;
      await new Promise((r) => setTimeout(r, BUSY_RETRY_MS));
    }
  }
}
