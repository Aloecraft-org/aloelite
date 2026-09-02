//! The memory-image model over `MemStore`: the portable shape, so this file
//! runs natively and in the browser without change.

use std::sync::Arc;

use aloelite_core::crypto::EncMode;
use aloelite_core::ops::{self, MountOptions};
use aloelite_store::image::Image;
use ego_platform::blobs::{BlobStore, MemStore};

#[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
wasm_bindgen_test::wasm_bindgen_test_configure!(run_in_browser);

/// An async test: `#[test]` over `block_on` natively, `#[wasm_bindgen_test]`
/// on an `async fn` in the browser.
macro_rules! store_test {
    (async fn $name:ident() $body:block) => {
        #[cfg(not(all(target_arch = "wasm32", target_os = "unknown")))]
        #[test]
        fn $name() {
            futures::executor::block_on(async $body)
        }
        #[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
        #[wasm_bindgen_test::wasm_bindgen_test]
        async fn $name() $body
    };
}

fn mem() -> Arc<dyn BlobStore> {
    Arc::new(MemStore::new())
}

store_test!(
    async fn a_fresh_key_is_an_empty_store_and_a_checkpoint_is_one_atomic_blob() {
        let store = mem();
        let mut image = Image::open(store.clone(), "vol").await.unwrap();
        let volume =
            ops::create_volume(image.db(), Some("v"), 64, None, EncMode::Convergent).unwrap();
        let mount = ops::mount(image.db(), &volume.id, &MountOptions::default()).unwrap();
        ops::create_entry(image.db(), &mount, "/hello.txt", Some(b"hello")).unwrap();
        assert!(
            store.get("vol").await.unwrap().is_none(),
            "nothing reaches the store before the first checkpoint"
        );

        image.checkpoint().await.unwrap();
        let blob = store
            .get("vol")
            .await
            .unwrap()
            .expect("the checkpoint wrote the blob");
        assert!(
            blob.starts_with(b"SQLite format 3\0"),
            "the blob is a whole SQLite file"
        );

        ops::create_entry(image.db(), &mount, "/later.txt", Some(b"x")).unwrap();
        ops::unmount(image.db(), &mount).unwrap();
        image.close().await.unwrap(); // close checkpoints first

        let mut again = Image::open(store.clone(), "vol").await.unwrap();
        let mount = ops::mount(again.db(), &volume.id, &MountOptions::default()).unwrap();
        assert_eq!(
            ops::read_all(again.db(), &mount, "/hello.txt").unwrap(),
            b"hello"
        );
        assert_eq!(
            ops::read_all(again.db(), &mount, "/later.txt").unwrap(),
            b"x"
        );
    }
);

store_test!(
    async fn losing_the_process_loses_only_what_came_after_the_last_checkpoint() {
        let store = mem();
        let volume = {
            let mut image = Image::open(store.clone(), "vol").await.unwrap();
            let volume =
                ops::create_volume(image.db(), Some("v"), 64, None, EncMode::Convergent).unwrap();
            let mount = ops::mount(image.db(), &volume.id, &MountOptions::default()).unwrap();
            ops::create_entry(image.db(), &mount, "/kept.txt", Some(b"kept")).unwrap();
            ops::unmount(image.db(), &mount).unwrap();
            image.checkpoint().await.unwrap();
            let mount = ops::mount(image.db(), &volume.id, &MountOptions::default()).unwrap();
            ops::create_entry(image.db(), &mount, "/lost.txt", Some(b"lost")).unwrap();
            volume
            // dropped without close: the crash
        };
        let mut again = Image::open(store, "vol").await.unwrap();
        let mount = ops::mount(again.db(), &volume.id, &MountOptions::default()).unwrap();
        assert_eq!(
            ops::read_all(again.db(), &mount, "/kept.txt").unwrap(),
            b"kept"
        );
        assert!(!ops::exists(again.db(), &mount, "/lost.txt").unwrap());
    }
);

store_test!(
    async fn an_empty_blob_reads_as_a_fresh_store() {
        let store = mem();
        store.put("vol", Vec::new()).await.unwrap();
        let mut image = Image::open(store, "vol").await.unwrap();
        assert!(ops::list_volumes(image.db()).unwrap().is_empty());
        assert_eq!(image.key(), "vol");
    }
);

store_test!(
    async fn two_keys_are_two_independent_stores() {
        let store = mem();
        let mut a = Image::open(store.clone(), "a").await.unwrap();
        let b = Image::open(store.clone(), "b").await.unwrap();
        ops::create_volume(a.db(), Some("only-in-a"), 64, None, EncMode::Convergent).unwrap();
        a.close().await.unwrap();
        b.close().await.unwrap();
        let mut b = Image::open(store, "b").await.unwrap();
        assert!(ops::list_volumes(b.db()).unwrap().is_empty());
    }
);
