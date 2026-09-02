//! `conformance/vectors/format-v1.json` against `aloelite_core::{content, crypto}`.
//!
//! Fixed inputs to exact bytes: content addressing, chunking, and the ENC-2
//! key ladder. The same file `tests/test_format_vectors.py` reads. These are
//! agreement vectors generated from the Python reference — they say what
//! Aloelite does today, and a port that matches them reads and writes files
//! the reference can open. `encrypt_chunk_convergent` is the one that
//! matters most: convergent encryption must be byte-identical everywhere or
//! cross-implementation dedup silently stops working.

use aloelite_conformance::vectors::FORMAT_V1;
use aloelite_core::content::{chunk_hash, split_chunks};
use aloelite_core::crypto::{self, Cipher, EncMode};
use ego_platform::entropy::SystemEntropy;
use serde_json::Value;

#[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
wasm_bindgen_test::wasm_bindgen_test_configure!(run_in_browser);

macro_rules! conformance_test {
    ($(#[$m:meta])* fn $name:ident() $body:block) => {
        $(#[$m])*
        #[cfg_attr(not(all(target_arch = "wasm32", target_os = "unknown")), test)]
        #[cfg_attr(
            all(target_arch = "wasm32", target_os = "unknown"),
            wasm_bindgen_test::wasm_bindgen_test
        )]
        fn $name() $body
    };
}

fn vectors() -> Value {
    let v: Value = serde_json::from_str(FORMAT_V1).expect("format-v1.json parses");
    assert_eq!(v["format"], "aloelite-format-vectors", "wrong vector file");
    assert_eq!(v["version"], 1, "vector schema moved; update this runner");
    v
}

fn hex_of(v: &Value, what: &str) -> Vec<u8> {
    let s = v
        .as_str()
        .unwrap_or_else(|| panic!("{what}: expected a hex string, got {v}"));
    hex::decode(s).unwrap_or_else(|e| panic!("{what}: bad hex {s:?}: {e}"))
}

fn key32(v: &Value, what: &str) -> [u8; 32] {
    hex_of(v, what)
        .try_into()
        .unwrap_or_else(|b: Vec<u8>| panic!("{what}: expected 32 bytes, got {}", b.len()))
}

/// The platform CSPRNG. The ladder's wrap/seal/key functions demand
/// `CryptoRng`, and ego-platform's `SeededEntropy` deliberately is not one —
/// determinism must never masquerade as entropy, and the type system holds
/// that line here too. Nothing in these tests asserts a random byte: wrap
/// hands back the nonce it drew, so round trips need only the same key.
fn rng() -> SystemEntropy {
    SystemEntropy
}

conformance_test! {
    fn constants_match_the_format() {
        let c = &vectors()["constants"];
        assert_eq!(c["nonce_len"], crypto::NONCE_LEN);
        assert_eq!(c["tag_len"], crypto::TAG_LEN);
        assert_eq!(c["token_len"], crypto::TOKEN_LEN);
        assert_eq!(c["mount_nonce_len"], crypto::MOUNT_NONCE_LEN);
        assert_eq!(c["argon2_time_cost"], crypto::ARGON2_TIME_COST);
        assert_eq!(c["argon2_memory_cost_kib"], crypto::ARGON2_MEMORY_COST_KIB);
        assert_eq!(c["argon2_parallelism"], crypto::ARGON2_PARALLELISM);
        assert_eq!(c["argon2_key_len"], crypto::ARGON2_KEY_LEN);
    }
}

conformance_test! {
    fn chunk_addresses() {
        let v = vectors();
        for case in v["chunk_address"].as_array().expect("chunk_address") {
            let bytes = hex_of(&case["bytes"], "bytes");
            assert_eq!(chunk_hash(&bytes), case["chunk_hash"], "{}", case["name"]);
        }
    }
}

conformance_test! {
    fn chunk_splitting() {
        let v = vectors();
        for case in v["chunk_split"].as_array().expect("chunk_split") {
            let size = case["size"].as_u64().unwrap() as usize;
            let chunk_size = case["chunk_size"].as_u64().unwrap() as usize;
            let data = vec![b'z'; size];
            let got: Vec<u64> = split_chunks(&data, chunk_size).iter().map(|c| c.len() as u64).collect();
            let want: Vec<u64> = case["lengths"].as_array().unwrap().iter().map(|l| l.as_u64().unwrap()).collect();
            assert_eq!(got, want, "size={size} chunk_size={chunk_size}");
        }
    }
}

conformance_test! {
    fn volume_hash() {
        let v = vectors();
        for case in v["volume_hash"].as_array().expect("volume_hash") {
            let got = crypto::volume_hash(case["volume_id"].as_str().unwrap(), case["root_node_id"].as_str().unwrap());
            assert_eq!(hex::encode(got), case["h_v"]);
        }
    }
}

conformance_test! {
    fn unlock_key_is_argon2id_at_the_pinned_factors() {
        // 64 MiB, three passes, four lanes — per case. Slow by design; in a
        // browser this is also the first real measurement of what a mount
        // costs the Worker.
        let v = vectors();
        for case in v["unlock_key"].as_array().expect("unlock_key") {
            let pin = case["pin_utf8"].as_str().unwrap().as_bytes();
            let h_v = key32(&case["h_v"], "h_v");
            let got = crypto::derive_unlock_key(pin, &h_v);
            assert_eq!(hex::encode(*got), case["k_u"], "pin {:?}", case["pin_utf8"]);
        }
    }
}

conformance_test! {
    fn chunk_subkey() {
        let v = vectors();
        for case in v["chunk_key"].as_array().expect("chunk_key") {
            let vk = key32(&case["volume_key"], "volume_key");
            let got = crypto::chunk_key(&vk, case["volume_id"].as_str().unwrap());
            assert_eq!(hex::encode(*got), case["k_chunk"]);
        }
    }
}

conformance_test! {
    fn convergent_encryption_is_byte_identical() {
        let v = vectors();
        let inputs = &v["inputs"];
        let vk = key32(&inputs["volume_key"], "volume_key");
        let cipher = Cipher::for_volume(&vk, inputs["volume_id"].as_str().unwrap(), EncMode::Convergent);
        assert!(cipher.dedup() && cipher.encrypts());
        for case in v["encrypt_chunk_convergent"].as_array().expect("cases") {
            let name = case["name"].as_str().unwrap();
            let pt = hex_of(&case["plaintext"], "plaintext");
            let sealed = cipher.encrypt_chunk(&pt, &mut rng());
            assert_eq!(hex::encode(&sealed.nonce), case["n_c"], "{name}: convergent nonce drifted");
            assert_eq!(hex::encode(&sealed.ciphertext), case["ciphertext"], "{name}: ciphertext drifted");
            assert_eq!(hex::encode(&sealed.tag), case["tag"], "{name}: tag drifted");
            // the pool address is over the CIPHERTEXT actually stored
            assert_eq!(chunk_hash(&sealed.ciphertext), case["chunk_hash"], "{name}: pool address");
            // and it round-trips
            let back = cipher.decrypt_chunk(&sealed.ciphertext, &sealed.nonce, &sealed.tag).expect("decrypts");
            assert_eq!(back, pt, "{name}: round trip");
        }
    }
}

conformance_test! {
    fn convergent_encryption_is_stable_across_cipher_instances() {
        let v = vectors();
        let inputs = &v["inputs"];
        let vk = key32(&inputs["volume_key"], "volume_key");
        let vid = inputs["volume_id"].as_str().unwrap();
        let a = Cipher::for_volume(&vk, vid, EncMode::Convergent).encrypt_chunk(b"same bytes", &mut rng());
        let b = Cipher::for_volume(&vk, vid, EncMode::Convergent).encrypt_chunk(b"same bytes", &mut rng());
        assert_eq!(a, b);
        // and random mode is NOT stable, which is its whole point
        let r1 = Cipher::for_volume(&vk, vid, EncMode::Random).encrypt_chunk(b"same bytes", &mut rng());
        let r2 = Cipher::for_volume(&vk, vid, EncMode::Random).encrypt_chunk(b"same bytes", &mut rng());
        assert_ne!(r1.nonce, r2.nonce);
        assert_ne!(r1.ciphertext, r2.ciphertext);
    }
}

conformance_test! {
    fn unwrap_volume_key() {
        let v = vectors();
        for case in v["unwrap_volume_key"].as_array().expect("unwrap_volume_key") {
            let k_u = key32(&case["k_u"], "k_u");
            let got = crypto::unwrap_volume_key(&k_u, &hex_of(&case["wrapped_key"], "wrapped_key"), &hex_of(&case["wrap_nonce"], "wrap_nonce"))
                .expect("the vectors' K_u opens the vectors' S_vk");
            assert_eq!(hex::encode(*got), case["volume_key"]);
        }
    }
}

conformance_test! {
    fn unwrap_rejects_a_wrong_key() {
        let v = vectors();
        for case in v["unwrap_volume_key"].as_array().expect("unwrap_volume_key") {
            let mut wrong = key32(&case["k_u"], "k_u");
            wrong[0] ^= 0x01;
            let r = crypto::unwrap_volume_key(&wrong, &hex_of(&case["wrapped_key"], "wrapped_key"), &hex_of(&case["wrap_nonce"], "wrap_nonce"));
            assert_eq!(r.err(), Some(crypto::BadKey), "a one-bit-wrong K_u must be refused, not produce garbage");
        }
    }
}

conformance_test! {
    fn wrap_then_unwrap_round_trips_and_a_wrong_pin_is_refused() {
        // wrap has no vector (fresh nonce every call); pin its contract with
        // unwrap instead: what we seal, we open, and only with the same key.
        let k_u = [0x42u8; 32];
        let k_v = [0x24u8; 32];
        let (wrapped, nonce) = crypto::wrap_volume_key(&k_u, &k_v, &mut rng());
        assert_eq!(wrapped.len(), 32 + crypto::TAG_LEN);
        assert_eq!(*crypto::unwrap_volume_key(&k_u, &wrapped, &nonce).unwrap(), k_v);
        assert_eq!(crypto::unwrap_volume_key(&[0u8; 32], &wrapped, &nonce).err(), Some(crypto::BadKey));
    }
}

conformance_test! {
    fn session_kek() {
        let v = vectors();
        for case in v["session_kek"].as_array().expect("session_kek") {
            let got = crypto::session_kek(&hex_of(&case["token"], "token"), &hex_of(&case["mount_nonce"], "mount_nonce"));
            assert_eq!(hex::encode(*got), case["kek"]);
        }
    }
}

conformance_test! {
    fn mount_secret_seals_and_opens_only_with_the_same_token() {
        let v = vectors();
        let inputs = &v["inputs"];
        let token = hex_of(&inputs["token"], "token");
        let n_m = hex_of(&inputs["mount_nonce"], "mount_nonce");
        let k_v = key32(&inputs["volume_key"], "volume_key");
        let (secret, n_sess) = crypto::seal_mount_secret(&token, &n_m, &k_v, &mut rng());
        assert_eq!(*crypto::open_mount_secret(&token, &n_m, &secret, &n_sess).unwrap(), k_v);
        let mut other = token.clone();
        other[3] ^= 0x80;
        assert_eq!(crypto::open_mount_secret(&other, &n_m, &secret, &n_sess).err(), Some(crypto::BadKey));
    }
}
