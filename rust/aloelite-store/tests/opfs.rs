//! The OPFS pool, which exists only in a browser and only from a Dedicated
//! Worker — so this file is configured to run there.
#![cfg(all(target_arch = "wasm32", target_os = "unknown"))]

use aloelite_core::crypto::EncMode;
use aloelite_core::ops::{self, MountOptions};
use aloelite_store::opfs::{OpfsConfig, Pool};
use wasm_bindgen_test::wasm_bindgen_test;

wasm_bindgen_test::wasm_bindgen_test_configure!(run_in_dedicated_worker);

fn test_pool() -> OpfsConfig {
    OpfsConfig {
        directory: "aloelite-store-tests".to_owned(),
        vfs_name: "aloelite-opfs-test".to_owned(),
        ..Default::default()
    }
}

#[wasm_bindgen_test]
async fn a_volume_in_opfs_survives_close_and_reopen() {
    let pool = Pool::install(&test_pool()).await.unwrap();
    let _ = pool.delete("reopen.db");
    let volume = {
        let mut db = pool.open("reopen.db").unwrap();
        let volume = ops::create_volume(&mut db, Some("v"), 64, None, EncMode::Convergent).unwrap();
        let mount = ops::mount(&mut db, &volume.id, &MountOptions::default()).unwrap();
        ops::create_entry(&mut db, &mount, "/hello.txt", Some(b"hello")).unwrap();
        ops::unmount(&mut db, &mount).unwrap();
        db.close().unwrap();
        volume
    };
    assert!(pool.exists("reopen.db").unwrap());
    let mut db = pool.open("reopen.db").unwrap();
    let mount = ops::mount(&mut db, &volume.id, &MountOptions::default()).unwrap();
    assert_eq!(
        ops::read_all(&mut db, &mount, "/hello.txt").unwrap(),
        b"hello"
    );
    db.close().unwrap();

    let bytes = pool.export("reopen.db").unwrap();
    assert!(
        bytes.starts_with(b"SQLite format 3\0"),
        "export is a whole SQLite file"
    );
    assert!(pool.delete("reopen.db").unwrap());
    assert!(!pool.exists("reopen.db").unwrap());
}

#[wasm_bindgen_test]
async fn an_exported_database_imports_under_a_new_name() {
    let pool = Pool::install(&test_pool()).await.unwrap();
    let _ = pool.delete("source.db");
    let _ = pool.delete("copy.db");
    let mut db = pool.open("source.db").unwrap();
    let volume = ops::create_volume(&mut db, Some("v"), 64, None, EncMode::Convergent).unwrap();
    let mount = ops::mount(&mut db, &volume.id, &MountOptions::default()).unwrap();
    ops::create_entry(&mut db, &mount, "/f", Some(b"bytes")).unwrap();
    ops::unmount(&mut db, &mount).unwrap();
    db.close().unwrap();

    let bytes = pool.export("source.db").unwrap();
    pool.import("copy.db", &bytes).unwrap();
    let mut copy = pool.open("copy.db").unwrap();
    let mount = ops::mount(&mut copy, &volume.id, &MountOptions::default()).unwrap();
    assert_eq!(ops::read_all(&mut copy, &mount, "/f").unwrap(), b"bytes");
    copy.close().unwrap();
    let _ = pool.delete("source.db");
    let _ = pool.delete("copy.db");
}
