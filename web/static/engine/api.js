// @ts-check
// The engine adapter contract: the ONLY surface the viewer UI is allowed to
// talk to. It is a transcription of config/mount-api.yaml's session /
// structural / read groups into a JS shape, so that the Pyodide backend that
// implements it today can be swapped for the TS/wasm or aloelite-rs engine
// later by replacing one factory — the UI must never import a backend
// directly, never touch Pyodide objects, and never see a Python string.
//
// Backends: ./pyodide-engine.js (real), ./mock-engine.js (UI dev + tests).
// Pick one with `createEngine()` below (`?engine=mock` opts into the mock).
//
// All methods reject with EngineError (errors.js) using the closed code set.

/**
 * @typedef {Object} VolumeDesc
 * @property {string} id          uuid7, opaque
 * @property {string|null} name
 * @property {boolean} encrypted  whether mount() needs a pin
 * @property {string}  encMode    'none' | 'convergent' | 'random'
 * @property {number}  createdAt  unix epoch ms
 */

/**
 * @typedef {Object} Entry       one row of a directory listing
 * @property {string} name
 * @property {'container'|'entry'} type
 * @property {string} node       uuid7 of the node
 */

/**
 * @typedef {Object} NodeStat
 * @property {string} id
 * @property {'container'|'entry'} type
 * @property {string} name
 * @property {number|null} size  bytes; null for containers
 * @property {number} createdAt  unix epoch ms
 * @property {number} modifiedAt unix epoch ms
 * @property {Object<string,string>} metadata  NODE-6 shallow map
 */

/**
 * @typedef {Object} EngineMount  a live session on one volume
 * @property {boolean} readOnly
 * @property {string}  volumeId
 * @property {(path: string) => Promise<Entry[]>} list  containers first, then by name
 * @property {(path: string) => Promise<NodeStat>} stat
 * @property {(path: string) => Promise<Uint8Array>} readFile
 * @property {(path: string, bytes: Uint8Array) => Promise<void>} putFile  rejects read_only
 * @property {(path: string) => Promise<void>} mkdir  parents created; rejects read_only
 * @property {(path: string, opts?: {recursive?: boolean}) => Promise<void>} remove  rejects read_only
 * @property {() => Promise<void>} unmount
 */

/**
 * @typedef {Object} EngineSession  an open .fs file (one connection)
 * @property {string} fileName
 * @property {VolumeDesc[]} volumes
 * @property {(volumeId: string, opts?: {pin?: string, readOnly?: boolean}) => Promise<EngineMount>} mount
 * @property {() => Promise<Uint8Array>} exportBytes  consistent single-file snapshot of the whole .fs
 * @property {() => Promise<void>} close
 */

/**
 * @typedef {Object} Engine
 * @property {(onProgress?: (stage: string, fraction: number) => void) => Promise<void>} boot
 *           idempotent; heavy (runtime download) — call only after the user commits
 * @property {(fileName: string, bytes: Uint8Array) => Promise<EngineSession>} openVolumeFile
 * @property {(volumeName: string, pin?: string|null) => Promise<EngineSession>} createScratch
 */

/**
 * Resolve the backend for this page load.
 * @returns {Promise<Engine>}
 */
export async function createEngine() {
  const params = new URLSearchParams(location.search);
  if (params.get("engine") === "mock") {
    const { createMockEngine } = await import("./mock-engine.js");
    return createMockEngine();
  }
  const { createPyodideEngine } = await import("./pyodide-engine.js");
  return createPyodideEngine();
}
