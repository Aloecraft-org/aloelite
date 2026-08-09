// @ts-check
// The closed error set, transcribed from config/mount-api.yaml (`errors:`).
// Every engine backend maps its native failures onto these codes; the UI
// switches on `code`, never on message text. Codes below the marker are
// adapter-level additions that exist only in the web client.
export const ERROR_CODES = Object.freeze({
  // -- mount-api.yaml closed set --------------------------------------------
  not_found: "path or id does not resolve",
  not_a_container: "operation needs a container, target is an entry",
  not_an_entry: "operation needs an entry, target is a container",
  nameless: "a name was required and was empty",
  would_cycle: "reparent would place a container under its own descendant",
  volume_mismatch: "operation would span volumes",
  not_empty: "container delete policy forbids removing non-empty",
  mount_invalid: "mount is unmounted, expired, or its mount point vanished",
  mount_point_archived: "mount point node is archived",
  lock_held: "an exclusive lock is held by another mount",
  lock_invalid: "the descriptor's lock has expired or its mount ended",
  corrupt: "an invariant the schema cannot enforce was found violated",
  unsupported: "operation not available in this build/target",
  container_exists: "container already exists",
  bad_key: "the supplied PIN did not unlock the volume",
  encryption_required: "an encrypted volume was mounted without a PIN, or vice versa",
  // -- web-client adapter additions -----------------------------------------
  read_only: "this volume was opened read-only",
  too_large: "file exceeds the size limit for in-browser handling",
  boot_failed: "the engine runtime failed to load",
  fs_error: "filesystem error", // unmapped FsError subclass; carries the message
});

export class EngineError extends Error {
  /**
   * @param {keyof typeof ERROR_CODES | string} code
   * @param {string} [message]
   */
  constructor(code, message) {
    super(message || ERROR_CODES[/** @type {keyof typeof ERROR_CODES} */ (code)] || code);
    this.name = "EngineError";
    this.code = code;
  }
}

/** @param {unknown} e @param {string} code */
export const isEngineError = (e, code) => e instanceof EngineError && e.code === code;
