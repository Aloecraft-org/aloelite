//! The `aloelite-fuse` command line: the same flags as the Python entry
//! point (`aloelite/fuse.py`), parsed by hand — the crate has no argument
//! library, and the surface is small and stable.
//!
//! The one deliberate difference from the reference: a bare `--pin` with no
//! value is an error here rather than an interactive prompt. The CLI has no
//! spec (doc/RUST_PORT.md; D-7 leaves the CLI contract open), so this
//! narrows rather than reimplements terminal echo control; `--pin-file` and
//! `--pin-env` cover the non-interactive cases the reference documents.

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------
//
// Entry point: main() -> process exit code. USAGE is the help text. On Linux
// it parses args, resolves the PIN and volume, and calls daemon::serve;
// elsewhere it reports that FUSE is Linux-only.

/// Help text, also printed on a parse error.
pub const USAGE: &str = "\
aloelite-fuse — mount an Aloelite volume as a FUSE filesystem (Linux)

USAGE:
    aloelite-fuse -f FILE [-v NAME] [options] MOUNTPOINT

OPTIONS:
    -f, --file FILE     the .fs/.sqlite file (default: $ALOELITE_FILE)
    -v, --volume NAME   volume name (optional if the file has exactly one)
        --create        create the volume if it does not exist (needs -v)
        --ro            mount read-only
        --allow-other   let other UIDs reach the mount
        --pin SECRET    PIN for an encrypted volume (prefer --pin-file/-env)
        --pin-file PATH file whose contents are the PIN (trailing \\n stripped)
        --pin-env VAR   environment variable holding the PIN
        --debug         verbose logging
        --version       print version and exit
    -h, --help          print this help and exit
";

#[cfg(target_os = "linux")]
pub fn main() -> i32 {
    use crate::daemon::{self, Options};

    let args: Vec<String> = std::env::args().skip(1).collect();
    let parsed = match Parsed::from_args(&args) {
        Ok(Outcome::Run(p)) => p,
        Ok(Outcome::Help) => {
            print!("{USAGE}");
            return 0;
        }
        Ok(Outcome::Version) => {
            println!("aloelite-fuse {}", env!("CARGO_PKG_VERSION"));
            return 0;
        }
        Err(msg) => {
            eprintln!("aloelite-fuse: {msg}\n\n{USAGE}");
            return 2;
        }
    };

    let pin = match parsed.resolve_pin() {
        Ok(pin) => pin,
        Err(msg) => {
            eprintln!("aloelite-fuse: {msg}");
            return 1;
        }
    };

    let volume = match parsed.resolve_volume() {
        Ok(v) => v,
        Err(msg) => {
            eprintln!("aloelite-fuse: {msg}");
            return 1;
        }
    };

    let opts = Options {
        file: std::path::Path::new(&parsed.file),
        volume: &volume,
        mountpoint: std::path::Path::new(&parsed.mountpoint),
        pin: pin.as_deref(),
        access: if parsed.ro {
            aloelite_core::types::Access::Ro
        } else {
            aloelite_core::types::Access::Rw
        },
        create: parsed.create,
        allow_other: parsed.allow_other,
    };

    if parsed.debug {
        // SAFETY: single-threaded startup, before any thread reads the env.
        unsafe { std::env::set_var("RUST_LOG", "debug") };
    }
    ego_platform::logging::init();

    match daemon::serve(&opts) {
        Ok(()) => 0,
        Err(daemon::DaemonError::Engine(aloelite_core::FsError::BadKey)) => {
            eprintln!("aloelite-fuse: wrong PIN for volume {:?}", parsed.volume);
            1
        }
        Err(e) => {
            eprintln!("aloelite-fuse: {e}");
            1
        }
    }
}

#[cfg(not(target_os = "linux"))]
pub fn main() -> i32 {
    eprintln!("aloelite-fuse is Linux-only (there is no FUSE kernel interface on this platform)");
    1
}

// ---------------------------------------------------------------------------
// depth: argument parsing and resolution (portable, unit-tested)
// ---------------------------------------------------------------------------

/// What parsing produced.
pub enum Outcome {
    Run(Parsed),
    Help,
    Version,
}

/// The parsed command line, before PIN and volume resolution.
#[derive(Debug, Default, PartialEq)]
pub struct Parsed {
    pub file: String,
    pub volume: Option<String>,
    pub mountpoint: String,
    pub create: bool,
    pub ro: bool,
    pub allow_other: bool,
    pub debug: bool,
    pub pin: Option<String>,
    pub pin_file: Option<String>,
    pub pin_env: Option<String>,
}

impl Parsed {
    /// Parse `args` (without the program name). `Err` carries a message.
    pub fn from_args(args: &[String]) -> Result<Outcome, String> {
        let mut p = Parsed {
            file: std::env::var("ALOELITE_FILE").unwrap_or_default(),
            ..Default::default()
        };
        let mut mountpoint: Option<String> = None;
        let mut it = args.iter();
        while let Some(arg) = it.next() {
            let mut value = |flag: &str| -> Result<String, String> {
                it.next()
                    .cloned()
                    .ok_or_else(|| format!("{flag} needs a value"))
            };
            match arg.as_str() {
                "-h" | "--help" => return Ok(Outcome::Help),
                "--version" => return Ok(Outcome::Version),
                "-f" | "--file" => p.file = value("-f/--file")?,
                "-v" | "--volume" => p.volume = Some(value("-v/--volume")?),
                "--create" => p.create = true,
                "--ro" => p.ro = true,
                "--allow-other" => p.allow_other = true,
                "--debug" => p.debug = true,
                "--pin" => p.pin = Some(value("--pin")?),
                "--pin-file" => p.pin_file = Some(value("--pin-file")?),
                "--pin-env" => p.pin_env = Some(value("--pin-env")?),
                other if other.starts_with('-') && other != "-" => {
                    return Err(format!("unknown option {other}"));
                }
                _ => {
                    if mountpoint.replace(arg.clone()).is_some() {
                        return Err("more than one mountpoint given".to_owned());
                    }
                }
            }
        }
        if p.file.is_empty() {
            return Err("no file: pass -f or set ALOELITE_FILE".to_owned());
        }
        p.mountpoint = mountpoint.ok_or("no mountpoint given")?;
        if p.create && p.volume.is_none() {
            return Err("--create needs an explicit -v NAME".to_owned());
        }
        Ok(Outcome::Run(p))
    }

    /// The PIN bytes, from the three sources in precedence order.
    pub fn resolve_pin(&self) -> Result<Option<Vec<u8>>, String> {
        if let Some(pin) = &self.pin {
            return Ok(Some(pin.clone().into_bytes()));
        }
        if let Some(path) = &self.pin_file {
            let raw =
                std::fs::read(path).map_err(|e| format!("cannot read --pin-file {path:?}: {e}"))?;
            let end = raw.iter().rposition(|b| *b != b'\n').map_or(0, |i| i + 1);
            return Ok(Some(raw[..end].to_vec()));
        }
        if let Some(var) = &self.pin_env {
            let val = std::env::var(var)
                .map_err(|_| format!("environment variable {var:?} is not set"))?;
            return Ok(Some(val.into_bytes()));
        }
        Ok(None)
    }

    /// The volume name to mount: the flag if given, else the file's only
    /// volume. Needs the engine only in the "pick the one" case.
    #[cfg(target_os = "linux")]
    pub fn resolve_volume(&self) -> Result<String, String> {
        if let Some(v) = &self.volume {
            return Ok(v.clone());
        }
        if !std::path::Path::new(&self.file).exists() {
            return Err(format!("{}: no such file", self.file));
        }
        let mut db = aloelite_store::file::open_existing(&self.file)
            .map_err(|e| format!("cannot open {}: {e}", self.file))?;
        let names: Vec<String> = aloelite_core::ops::list_volumes(&mut db)
            .map_err(|e| format!("cannot list volumes: {e}"))?
            .into_iter()
            .map(|v| v.name.unwrap_or_else(|| v.id.as_str().to_owned()))
            .collect();
        match names.len() {
            1 => Ok(names.into_iter().next().unwrap()),
            0 => Err(format!(
                "{} contains no volumes (pass --create with -v NAME)",
                self.file
            )),
            _ => Err(format!(
                "multiple volumes; pick one with -v: {}",
                names.join(", ")
            )),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Parsed, String> {
        match Parsed::from_args(&args.iter().map(|s| s.to_string()).collect::<Vec<_>>())? {
            Outcome::Run(p) => Ok(p),
            _ => Err("not a run".to_owned()),
        }
    }

    #[test]
    fn a_minimal_command_line_parses() {
        let p = parse(&["-f", "vol.fs", "/mnt/x"]).unwrap();
        assert_eq!(p.file, "vol.fs");
        assert_eq!(p.mountpoint, "/mnt/x");
        assert!(!p.create && !p.ro && !p.allow_other);
    }

    #[test]
    fn flags_and_volume_and_pin_sources_parse() {
        let p = parse(&[
            "-f",
            "v.fs",
            "-v",
            "data",
            "--create",
            "--ro",
            "--allow-other",
            "--pin-env",
            "P",
            "/mnt/y",
        ])
        .unwrap();
        assert_eq!(p.volume.as_deref(), Some("data"));
        assert!(p.create && p.ro && p.allow_other);
        assert_eq!(p.pin_env.as_deref(), Some("P"));
    }

    #[test]
    fn missing_pieces_are_errors() {
        assert!(parse(&["/mnt/x"]).is_err(), "no file");
        assert!(parse(&["-f", "v.fs"]).is_err(), "no mountpoint");
        assert!(
            parse(&["-f", "v.fs", "--create", "/mnt/x"]).is_err(),
            "create needs -v"
        );
        assert!(
            parse(&["-f", "v.fs", "--bogus", "/mnt/x"]).is_err(),
            "unknown flag"
        );
        assert!(
            parse(&["-f", "v.fs", "/a", "/b"]).is_err(),
            "two mountpoints"
        );
    }

    #[test]
    fn pin_precedence_and_file_newline_strip() {
        let dir = std::env::temp_dir();
        let path = dir.join(format!("aloe-pin-{}", std::process::id()));
        std::fs::write(&path, b"secret\n\n").unwrap();
        let p = Parsed {
            pin_file: Some(path.to_string_lossy().into_owned()),
            ..Default::default()
        };
        assert_eq!(p.resolve_pin().unwrap().as_deref(), Some(&b"secret"[..]));
        std::fs::remove_file(&path).ok();

        let explicit = Parsed {
            pin: Some("abc".to_owned()),
            pin_env: Some("UNSET_VAR_XYZ".to_owned()),
            ..Default::default()
        };
        assert_eq!(
            explicit.resolve_pin().unwrap().as_deref(),
            Some(&b"abc"[..])
        );
    }
}
