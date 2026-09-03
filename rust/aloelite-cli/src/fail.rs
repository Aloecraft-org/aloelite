//! One error type for the command, and the top-level message mapping the
//! reference applies in `main`: every failure is one line, `aloelite: <why>`,
//! on stderr, and exit 1.

use aloelite_core::FsError;
use aloelite_store::StoreError;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Why a command stopped.
#[derive(Debug)]
pub enum Fail {
    /// A refusal the command itself phrased (already user-facing).
    Msg(String),
    Engine(FsError),
    Store(StoreError),
    Io(std::io::Error),
}

pub type Result<T> = std::result::Result<T, Fail>;

/// `Err(Fail::Msg(..))`, for the many one-line refusals.
pub fn fail<T>(msg: impl Into<String>) -> Result<T> {
    Err(Fail::Msg(msg.into()))
}

impl Fail {
    /// The line after `aloelite: `, with the reference's special cases.
    pub fn message(&self) -> String {
        match self {
            Fail::Msg(m) => m.clone(),
            Fail::Engine(e) => engine_message(e),
            Fail::Store(StoreError::Engine(e)) => engine_message(e),
            Fail::Store(e) => e.to_string(),
            Fail::Io(e) => e.to_string(),
        }
    }
}

// ---------------------------------------------------------------------------
// depth
// ---------------------------------------------------------------------------

fn engine_message(e: &FsError) -> String {
    match e {
        FsError::BadKey => "wrong PIN".to_owned(),
        FsError::EncryptionRequired { .. } => e.to_string(),
        FsError::ContainerExists { .. } => "already exists (use mkdir -p to tolerate)".to_owned(),
        other => {
            let code = other.code().unwrap_or(match other {
                FsError::Usage(_) => "usage",
                FsError::Sqlite(_) => "sqlite",
                _ => "internal",
            });
            format!("{code}: {other}")
        }
    }
}

impl From<FsError> for Fail {
    fn from(e: FsError) -> Self {
        Fail::Engine(e)
    }
}

impl From<StoreError> for Fail {
    fn from(e: StoreError) -> Self {
        Fail::Store(e)
    }
}

impl From<std::io::Error> for Fail {
    fn from(e: std::io::Error) -> Self {
        Fail::Io(e)
    }
}
