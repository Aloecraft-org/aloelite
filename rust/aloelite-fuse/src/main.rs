//! `aloelite-fuse`: mount an Aloelite volume as a FUSE filesystem.
//! All logic is in [`aloelite_fuse::cli`]; this is the entry point.

fn main() {
    std::process::exit(aloelite_fuse::cli::main());
}
