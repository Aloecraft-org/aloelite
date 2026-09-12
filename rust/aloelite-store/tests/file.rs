//! The file model on a real filesystem. Not the browser, by definition.
#![cfg(not(all(target_arch = "wasm32", target_os = "unknown")))]

use aloelite_core::crypto::EncMode;
use aloelite_core::ops::{self, MountOptions};
use aloelite_store::file;

fn scratch(name: &str) -> std::path::PathBuf {
    let mut p = std::env::temp_dir();
    p.push(format!("aloelite-store-{name}-{}.fs", std::process::id()));
    let _ = std::fs::remove_file(&p);
    p
}

#[test]
fn a_volume_survives_close_and_reopen_by_path() {
    let path = scratch("reopen");
    let volume = {
        let mut db = file::open(&path).unwrap();
        let volume = ops::create_volume(&mut db, Some("v"), 64, None, EncMode::Convergent).unwrap();
        let mount = ops::mount(&mut db, &volume.id, &MountOptions::default()).unwrap();
        ops::create_entry(&mut db, &mount, "/hello.txt", Some(b"hello")).unwrap();
        ops::unmount(&mut db, &mount).unwrap();
        db.close().unwrap();
        volume
    };
    let mut db = file::open_existing(&path).unwrap();
    let mount = ops::mount(&mut db, &volume.id, &MountOptions::default()).unwrap();
    assert_eq!(
        ops::read_all(&mut db, &mount, "/hello.txt").unwrap(),
        b"hello"
    );
    db.close().unwrap();
    let _ = std::fs::remove_file(&path);
}

#[test]
fn open_existing_refuses_a_missing_file_instead_of_creating_one() {
    let path = scratch("missing");
    assert!(file::open_existing(&path).is_err());
    assert!(
        !path.exists(),
        "a refused open must not leave a file behind"
    );
}
