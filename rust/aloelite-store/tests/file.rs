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

#[test]
fn a_native_wal_file_opens_as_a_memory_image_and_a_snapshot_reopens_natively() {
    use aloelite_core::Db;
    use aloelite_core::types::MountState;
    use ego_platform::entropy::SystemEntropy;

    let path = scratch("wal-image");
    let volume = {
        let mut db = file::open(&path).unwrap();
        let mode: String = db
            .connection()
            .query_row("PRAGMA journal_mode", [], |r| r.get(0))
            .unwrap();
        assert_eq!(mode, "wal", "the native file is in WAL mode");
        let volume = ops::create_volume(&mut db, Some("v"), 64, None, EncMode::Convergent).unwrap();
        let mount = ops::mount(&mut db, &volume.id, &MountOptions::default()).unwrap();
        ops::create_entry(&mut db, &mount, "/hello.txt", Some(b"hello")).unwrap();
        ops::unmount(&mut db, &mount).unwrap();
        db.close().unwrap(); // the last connection checkpoints the WAL
        volume
    };
    let bytes = std::fs::read(&path).unwrap();
    assert_eq!((bytes[18], bytes[19]), (2, 2), "the header says WAL");

    let clock = aloelite_store::clock::system_clock();
    let mut db = Db::open_image(&bytes, clock, SystemEntropy).unwrap();
    let mount = ops::mount(&mut db, &volume.id, &MountOptions::default()).unwrap();
    assert_eq!(
        ops::mount_info(&mut db, &mount).unwrap().state,
        MountState::Active
    );
    assert_eq!(
        ops::read_all(&mut db, &mount, "/hello.txt").unwrap(),
        b"hello"
    );
    ops::create_entry(&mut db, &mount, "/later.txt", Some(b"x")).unwrap();
    ops::unmount(&mut db, &mount).unwrap();
    let snapshot = db.snapshot().unwrap();
    db.close().unwrap();

    let back = scratch("wal-image-back");
    std::fs::write(&back, &snapshot).unwrap();
    let mut db = file::open_existing(&back).unwrap();
    let mount = ops::mount(&mut db, &volume.id, &MountOptions::default()).unwrap();
    assert_eq!(ops::read_all(&mut db, &mount, "/later.txt").unwrap(), b"x");
    db.close().unwrap();
    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_file(&back);
}

#[test]
fn bytes_that_are_not_a_database_fail_as_sqlite_not_as_a_too_old_host() {
    use aloelite_core::{Db, FsError};
    use ego_platform::entropy::SystemEntropy;

    let junk = vec![0x5a_u8; 4096];
    let err = Db::open_image(&junk, aloelite_store::clock::system_clock(), SystemEntropy)
        .err()
        .expect("junk is refused");
    assert!(matches!(err, FsError::Sqlite(_)), "got {err:?}");
}
