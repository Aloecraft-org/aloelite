//! At-rest encryption: the ENC-2 key ladder and the chunk-cipher seam.
//!
//! This module owns ALL cryptographic decisions. The rest of the engine
//! touches encryption only through [`Cipher`] — `encrypt_chunk` /
//! `decrypt_chunk` — so the operations and the streaming descriptor stay
//! byte-for-byte oblivious to whether a volume is encrypted. The identity
//! cipher is a no-op, so an unencrypted volume runs the exact same code
//! path and the whole conformance suite is unaffected.
//!
//! Every construction here is pinned to exact bytes by
//! `conformance/vectors/format-v1.json`. `encrypt_chunk_convergent` is the
//! one to check first in any port: convergent encryption has to be
//! byte-identical everywhere or the same plaintext lands at two pool
//! addresses and cross-implementation dedup silently stops.
//!
//! **Key ladder** (doc/ENCRYPTION.md). One user secret, layered so the volume
//! key is the fixed point that survives PIN rotation:
//!
//! ```text
//! H_v   = SHA256(volume_id || root_node_id)       derived, never stored
//! K_u   = Argon2id(PIN, salt=H_v)                  unlock secret; mount-only
//! K_v   = random(32)                               volume key; immutable
//! S_vk  = AEAD(K_u, N_wrap, K_v)                   wrapped_key on disk; the
//!                                                  Poly1305 tag rejects a wrong
//!                                                  PIN with no alternate secret
//! ```
//!
//! Per mount, a token stands in for the PIN so `K_u` is needed only once:
//!
//! ```text
//! T            = random(16)                        token; user-held, runtime
//! N_m          = random(16)                        mount nonce; on the mount row
//! session_kek  = HKDF(T, salt=N_m, info="aloelite-session")
//! mount_secret = AEAD(session_kek, N_sess, K_v)    memory-only
//! ```
//!
//! Chunks are encrypted under a domain-separated subkey, with a convergent
//! nonce so identical plaintext re-encrypts identically and the
//! content-addressed pool keeps deduplicating:
//!
//! ```text
//! K_chunk = HKDF(K_v, info="aloelite-chunk:" || volume_id)
//! N_c     = SHA256("aloelite-nc" || len_be64(pt) || pt)[:12]     convergent
//! (ct, tag) = ChaCha20Poly1305(K_chunk).encrypt(N_c, pt)
//! ```
//!
//! The chunk address (`content::chunk_hash`) is taken over the CIPHERTEXT
//! actually stored, so the pool's same-address ⇔ same-bytes invariant holds
//! across volumes with different keys. A different key (or random mode)
//! yields different ciphertext and a distinct address; nothing cross-volume
//! ever aliases.
//!
//! Randomness is an argument, never ambient: wrapping, random-mode nonces
//! and key generation take a caller-supplied `Rng + CryptoRng`. That is what
//! keeps this module free of platform code and lets every target supply
//! its own entropy (`ego_platform::entropy::SystemEntropy` in practice).

use chacha20poly1305::aead::{Aead, KeyInit};
use chacha20poly1305::{ChaCha20Poly1305, Key, Nonce};
use hkdf::Hkdf;
use rand_core::{CryptoRng, Rng};
use sha2::{Digest, Sha256};
use zeroize::Zeroizing;

// ---------------------------------------------------------------------------
// surface: constants that are format contract
// ---------------------------------------------------------------------------

/// ChaCha20-Poly1305 (IETF) nonce.
pub const NONCE_LEN: usize = 12;
/// Poly1305 tag.
pub const TAG_LEN: usize = 16;
/// Every key in the ladder.
pub const KEY_LEN: usize = 32;
/// The per-mount token `T`.
pub const TOKEN_LEN: usize = 16;
/// The per-mount nonce `N_m`.
pub const MOUNT_NONCE_LEN: usize = 16;

/// Argon2id work factors. Part of the format contract: every port must
/// stretch the PIN with these exact parameters or it derives a different
/// `K_u`. RFC 9106's second recommended profile.
pub const ARGON2_TIME_COST: u32 = 3;
pub const ARGON2_MEMORY_COST_KIB: u32 = 65_536;
pub const ARGON2_PARALLELISM: u32 = 4;
pub const ARGON2_KEY_LEN: usize = KEY_LEN;

const CHUNK_KEY_INFO_PREFIX: &[u8] = b"aloelite-chunk:";
const CONVERGENT_NONCE_DOMAIN: &[u8] = b"aloelite-nc";
const SESSION_KEK_INFO: &[u8] = b"aloelite-session";

// ---------------------------------------------------------------------------
// surface: types
// ---------------------------------------------------------------------------

/// A 32-byte key that zeroes itself on drop.
pub type Key32 = Zeroizing<[u8; KEY_LEN]>;

/// The AEAD refused: the tag did not verify. For the unlock path this is
/// "wrong PIN"; for the session path, "wrong token"; for a chunk, tampered
/// or mis-keyed data. Maps onto the spec's `bad_key`. Deliberately carries
/// nothing else — which of the three it was is not the cipher's to say.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("bad key: authentication failed")]
pub struct BadKey;

/// ENC-3's closed set, as stored in `volume.enc_mode`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EncMode {
    /// Plaintext; the identity cipher.
    None,
    /// Deterministic nonce; equal plaintext deduplicates.
    Convergent,
    /// Random nonce; no dedup, no equality leakage.
    Random,
}

impl EncMode {
    /// The column value.
    pub fn as_str(self) -> &'static str {
        match self {
            EncMode::None => "none",
            EncMode::Convergent => "convergent",
            EncMode::Random => "random",
        }
    }

    /// From the column value; `None` for anything outside the closed set.
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "none" => Some(EncMode::None),
            "convergent" => Some(EncMode::Convergent),
            "random" => Some(EncMode::Random),
            _ => None,
        }
    }
}

/// What `encrypt_chunk` hands back, in the shape the pool row stores:
/// length-preserving ciphertext (so the stored plaintext `length` still
/// governs offset math), the nonce, and the tag. The identity cipher
/// stores empty nonce and tag — the columns are `NOT NULL` and an empty
/// blob satisfies that.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Sealed {
    pub ciphertext: Vec<u8>,
    pub nonce: Vec<u8>,
    pub tag: Vec<u8>,
}

/// The seam. Exactly three shapes, one per [`EncMode`]; an enum rather than
/// a trait because the set is closed by ENC-3 and an enum needs no object
/// safety to take a generic rng.
#[derive(Debug, Clone)]
pub enum Cipher {
    /// `enc_mode = none`.
    Identity,
    /// `enc_mode = convergent | random`, over the domain-separated `K_chunk`.
    Chunk { key: Key32, convergent: bool },
}

// ---------------------------------------------------------------------------
// surface: the cipher
// ---------------------------------------------------------------------------

impl Cipher {
    /// The no-op cipher for a plaintext volume.
    pub fn identity() -> Self {
        Cipher::Identity
    }

    /// The cipher for an encrypted volume: derives `K_chunk` from the
    /// volume key and id. `EncMode::None` yields the identity cipher, so a
    /// caller can construct from the volume row without branching.
    pub fn for_volume(volume_key: &[u8; KEY_LEN], volume_id: &str, mode: EncMode) -> Self {
        match mode {
            EncMode::None => Cipher::Identity,
            EncMode::Convergent | EncMode::Random => Cipher::Chunk {
                key: chunk_key(volume_key, volume_id),
                convergent: mode == EncMode::Convergent,
            },
        }
    }

    /// Whether identical plaintext addresses to the same pool row. True for
    /// identity and convergent; false for random, where the per-chunk nonce
    /// is folded into the address so equal plaintext stores distinctly.
    pub fn dedup(&self) -> bool {
        match self {
            Cipher::Identity => true,
            Cipher::Chunk { convergent, .. } => *convergent,
        }
    }

    /// Whether this cipher actually protects chunks. The mount precondition
    /// compares it against the volume's `enc_mode` so an unkeyed connection
    /// can never read an encrypted volume as garbage, nor write plaintext
    /// into one (ENC-3).
    pub fn encrypts(&self) -> bool {
        matches!(self, Cipher::Chunk { .. })
    }

    /// Seal one chunk. `rng` is consumed only in random mode; identity and
    /// convergent are deterministic and ignore it.
    pub fn encrypt_chunk<R: Rng + CryptoRng + ?Sized>(
        &self,
        plaintext: &[u8],
        rng: &mut R,
    ) -> Sealed {
        match self {
            Cipher::Identity => Sealed {
                ciphertext: plaintext.to_vec(),
                nonce: Vec::new(),
                tag: Vec::new(),
            },
            Cipher::Chunk { key, convergent } => {
                let nonce: [u8; NONCE_LEN] = if *convergent {
                    convergent_nonce(plaintext)
                } else {
                    let mut n = [0u8; NONCE_LEN];
                    rng.fill_bytes(&mut n);
                    n
                };
                let mut sealed = aead(key)
                    .encrypt(Nonce::from_slice(&nonce), plaintext)
                    .expect("ChaCha20-Poly1305 encryption cannot fail on in-memory input");
                let tag = sealed.split_off(sealed.len() - TAG_LEN);
                Sealed {
                    ciphertext: sealed,
                    nonce: nonce.to_vec(),
                    tag,
                }
            }
        }
    }

    /// Open one chunk. `BadKey` on a tag failure.
    pub fn decrypt_chunk(
        &self,
        ciphertext: &[u8],
        nonce: &[u8],
        tag: &[u8],
    ) -> Result<Vec<u8>, BadKey> {
        match self {
            Cipher::Identity => Ok(ciphertext.to_vec()),
            Cipher::Chunk { key, .. } => {
                if nonce.len() != NONCE_LEN || tag.len() != TAG_LEN {
                    return Err(BadKey);
                }
                let mut sealed = Vec::with_capacity(ciphertext.len() + TAG_LEN);
                sealed.extend_from_slice(ciphertext);
                sealed.extend_from_slice(tag);
                aead(key)
                    .decrypt(Nonce::from_slice(nonce), sealed.as_slice())
                    .map_err(|_| BadKey)
            }
        }
    }
}

// ---------------------------------------------------------------------------
// surface: the ladder (pure; no DB). Ports reproduce these exactly.
// ---------------------------------------------------------------------------

/// `H_v = SHA256(volume_id || root_node_id)`. The Argon2id salt; derived on
/// mount from two immutable fields, never stored.
pub fn volume_hash(volume_id: &str, root_node_id: &str) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(volume_id.as_bytes());
    h.update(root_node_id.as_bytes());
    h.finalize().into()
}

/// `K_u = Argon2id(PIN, salt=H_v)` with the fixed format work factors.
pub fn derive_unlock_key(pin: &[u8], h_v: &[u8; 32]) -> Key32 {
    let params = argon2::Params::new(
        ARGON2_MEMORY_COST_KIB,
        ARGON2_TIME_COST,
        ARGON2_PARALLELISM,
        Some(ARGON2_KEY_LEN),
    )
    .expect("the format's Argon2id parameters are valid");
    // The 64 MiB of Argon2 memory is allocated here rather than by argon2's
    // `alloc` feature: that feature enables `password-hash`, whose default
    // brings a second `rand_core` (0.6) into every binary for nothing this
    // module uses. `block_count()` is `m_cost` rounded to the algorithm's
    // 4×lanes granularity — 65536 exactly, at the format's factors.
    let block_count = params.block_count();
    let kdf = argon2::Argon2::new(argon2::Algorithm::Argon2id, argon2::Version::V0x13, params);
    let mut blocks = vec![argon2::Block::default(); block_count];
    let mut out = Zeroizing::new([0u8; KEY_LEN]);
    kdf.hash_password_into_with_memory(pin, h_v, out.as_mut(), &mut blocks)
        .expect("32-byte salt and output are within Argon2id's limits");
    out
}

/// `K_chunk = HKDF(K_v, info="aloelite-chunk:" || volume_id)`. Exposed
/// because the vectors pin it separately from the ciphertext, so a port
/// that disagrees can tell derivation from AEAD.
pub fn chunk_key(volume_key: &[u8; KEY_LEN], volume_id: &str) -> Key32 {
    let mut info = Vec::with_capacity(CHUNK_KEY_INFO_PREFIX.len() + volume_id.len());
    info.extend_from_slice(CHUNK_KEY_INFO_PREFIX);
    info.extend_from_slice(volume_id.as_bytes());
    hkdf_sha256(None, volume_key, &info)
}

/// `S_vk = AEAD(K_u, N_wrap, K_v)` → `(wrapped_key = ct || tag, wrap_nonce)`.
/// A fresh random nonce every call, which is why there is no vector for it;
/// [`unwrap_volume_key`] covers the same ladder deterministically.
pub fn wrap_volume_key<R: Rng + CryptoRng + ?Sized>(
    unlock_key: &[u8; KEY_LEN],
    volume_key: &[u8; KEY_LEN],
    rng: &mut R,
) -> (Vec<u8>, [u8; NONCE_LEN]) {
    aead_wrap(unlock_key, volume_key, rng)
}

/// `K_v = open(K_u, S_vk)`. `BadKey` ⇒ wrong PIN: the Poly1305 tag is the
/// only thing standing between a guess and the volume, by design.
pub fn unwrap_volume_key(
    unlock_key: &[u8; KEY_LEN],
    wrapped_key: &[u8],
    wrap_nonce: &[u8],
) -> Result<Key32, BadKey> {
    aead_unwrap(unlock_key, wrapped_key, wrap_nonce)
}

/// `HKDF(T, salt=N_m, info="aloelite-session")`. HKDF rather than Argon2
/// because `T` is already high-entropy — there is nothing to stretch.
pub fn session_kek(token: &[u8], mount_nonce: &[u8]) -> Key32 {
    hkdf_sha256(Some(mount_nonce), token, SESSION_KEK_INFO)
}

/// `mount_secret = AEAD(session_kek(T, N_m), N_sess, K_v)`. Memory-only;
/// `T + N_m + mount_secret` reconstruct `K_v` without the PIN.
pub fn seal_mount_secret<R: Rng + CryptoRng + ?Sized>(
    token: &[u8],
    mount_nonce: &[u8],
    volume_key: &[u8; KEY_LEN],
    rng: &mut R,
) -> (Vec<u8>, [u8; NONCE_LEN]) {
    aead_wrap(&session_kek(token, mount_nonce), volume_key, rng)
}

/// `K_v` from the session triple. `BadKey` ⇒ wrong token.
pub fn open_mount_secret(
    token: &[u8],
    mount_nonce: &[u8],
    mount_secret: &[u8],
    sess_nonce: &[u8],
) -> Result<Key32, BadKey> {
    aead_unwrap(&session_kek(token, mount_nonce), mount_secret, sess_nonce)
}

/// A fresh `K_v`.
pub fn new_volume_key<R: Rng + CryptoRng + ?Sized>(rng: &mut R) -> Key32 {
    let mut k = Zeroizing::new([0u8; KEY_LEN]);
    rng.fill_bytes(k.as_mut());
    k
}

/// A fresh `T`.
pub fn new_token<R: Rng + CryptoRng + ?Sized>(rng: &mut R) -> [u8; TOKEN_LEN] {
    let mut t = [0u8; TOKEN_LEN];
    rng.fill_bytes(&mut t);
    t
}

/// A fresh `N_m`.
pub fn new_mount_nonce<R: Rng + CryptoRng + ?Sized>(rng: &mut R) -> [u8; MOUNT_NONCE_LEN] {
    let mut n = [0u8; MOUNT_NONCE_LEN];
    rng.fill_bytes(&mut n);
    n
}

// ---------------------------------------------------------------------------
// depth: primitives
// ---------------------------------------------------------------------------

/// `N_c = SHA256("aloelite-nc" || len_be64(pt) || pt)[:12]`.
fn convergent_nonce(plaintext: &[u8]) -> [u8; NONCE_LEN] {
    let mut h = Sha256::new();
    h.update(CONVERGENT_NONCE_DOMAIN);
    h.update((plaintext.len() as u64).to_be_bytes());
    h.update(plaintext);
    let digest = h.finalize();
    let mut n = [0u8; NONCE_LEN];
    n.copy_from_slice(&digest[..NONCE_LEN]);
    n
}

/// HKDF-SHA256, 32-byte output. `None` salt is RFC 5869's zero-filled salt,
/// which is what the reference (`cryptography`) does with `salt=None`.
fn hkdf_sha256(salt: Option<&[u8]>, ikm: &[u8], info: &[u8]) -> Key32 {
    let hk = Hkdf::<Sha256>::new(salt, ikm);
    let mut okm = Zeroizing::new([0u8; KEY_LEN]);
    hk.expand(info, okm.as_mut())
        .expect("32 bytes is within HKDF-SHA256's 255*32 limit");
    okm
}

fn aead(key: &[u8; KEY_LEN]) -> ChaCha20Poly1305 {
    ChaCha20Poly1305::new(Key::from_slice(key))
}

/// Seal `plaintext` under `key` with a fresh nonce → `(ct || tag, nonce)`.
fn aead_wrap<R: Rng + CryptoRng + ?Sized>(
    key: &[u8; KEY_LEN],
    plaintext: &[u8],
    rng: &mut R,
) -> (Vec<u8>, [u8; NONCE_LEN]) {
    let mut nonce = [0u8; NONCE_LEN];
    rng.fill_bytes(&mut nonce);
    let sealed = aead(key)
        .encrypt(Nonce::from_slice(&nonce), plaintext)
        .expect("ChaCha20-Poly1305 encryption cannot fail on in-memory input");
    (sealed, nonce)
}

/// Open `ct || tag` under `key`. `BadKey` on a tag failure — this is what
/// rejects a wrong PIN or token, with no alternate secret reachable.
fn aead_unwrap(key: &[u8; KEY_LEN], wrapped: &[u8], nonce: &[u8]) -> Result<Key32, BadKey> {
    if nonce.len() != NONCE_LEN {
        return Err(BadKey);
    }
    let opened = aead(key)
        .decrypt(Nonce::from_slice(nonce), wrapped)
        .map_err(|_| BadKey)?;
    let mut k = Zeroizing::new([0u8; KEY_LEN]);
    if opened.len() != KEY_LEN {
        return Err(BadKey);
    }
    k.copy_from_slice(&opened);
    Ok(k)
}
