//! The PIN, from the three standard flags in precedence order — `--pin`
//! (a value, or bare to prompt), `--pin-file`, `--pin-env` — the same rules
//! as `aloelite/pin.py`. The prompt reads the controlling terminal with echo
//! off, like ssh and sudo, so a piped stdin still allows an interactive PIN;
//! only a process with no terminal at all (cron, CI, WASI) cannot ask, and
//! is told to use a file or a variable instead.

use crate::fail::{Result, fail};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// `--pin` as parsed: bare, or with a value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PinArg {
    Prompt,
    Value(String),
}

/// Resolve the PIN. `Ok(None)` when no flag was given. `confirm` asks a
/// prompted PIN twice (volume creation).
pub fn read_pin(
    pin: Option<&PinArg>,
    pin_file: Option<&str>,
    pin_env: Option<&str>,
    confirm: bool,
) -> Result<Option<Vec<u8>>> {
    match pin {
        Some(PinArg::Prompt) => return prompt(confirm).map(Some),
        Some(PinArg::Value(v)) => return Ok(Some(v.clone().into_bytes())),
        None => {}
    }
    if let Some(path) = pin_file {
        let expanded = expand_home(path);
        let raw = std::fs::read(&expanded).map_err(|e| {
            crate::fail::Fail::Msg(format!("cannot read --pin-file {expanded:?}: {e}"))
        })?;
        let end = raw.iter().rposition(|b| *b != b'\n').map_or(0, |i| i + 1);
        return Ok(Some(raw[..end].to_vec()));
    }
    if let Some(var) = pin_env {
        return match std::env::var(var) {
            Ok(v) => Ok(Some(v.into_bytes())),
            Err(_) => fail(format!("environment variable {var:?} is not set")),
        };
    }
    Ok(None)
}

/// Whether a prompt is possible here at all.
pub fn tty_available() -> bool {
    tty::available()
}

/// Ask for the PIN on the controlling terminal, echo off; twice if
/// `confirm`, refusing a mismatch.
pub fn prompt(confirm: bool) -> Result<Vec<u8>> {
    if !tty_available() {
        return fail(
            "--pin with no value requires a controlling terminal to prompt; \
             use --pin-file or --pin-env in non-interactive contexts",
        );
    }
    let first = tty::read_secret("PIN: ")?;
    if confirm && tty::read_secret("Confirm PIN: ")? != first {
        return fail("PINs did not match");
    }
    Ok(first)
}

// ---------------------------------------------------------------------------
// depth
// ---------------------------------------------------------------------------

fn expand_home(path: &str) -> String {
    if let Some(rest) = path.strip_prefix("~/")
        && let Ok(home) = std::env::var("HOME")
    {
        return format!("{home}/{rest}");
    }
    path.to_owned()
}

#[cfg(unix)]
mod tty {
    use std::fs::OpenOptions;
    use std::io::{BufRead, BufReader, Write};
    use std::os::unix::io::AsRawFd;

    use crate::fail::{Fail, Result};

    pub fn available() -> bool {
        OpenOptions::new().read(true).open("/dev/tty").is_ok()
    }

    /// One line from /dev/tty with echo off; the trailing newline is
    /// stripped and echoed back so the cursor moves on.
    pub fn read_secret(label: &str) -> Result<Vec<u8>> {
        let mut tty = OpenOptions::new().read(true).write(true).open("/dev/tty")?;
        tty.write_all(label.as_bytes())?;
        tty.flush()?;
        let fd = tty.as_raw_fd();
        // SAFETY: termios is a plain C struct; the fd is open for the whole
        // call, and the original attributes are restored on every path.
        let mut original: libc::termios = unsafe { std::mem::zeroed() };
        if unsafe { libc::tcgetattr(fd, &mut original) } != 0 {
            return Err(Fail::Io(std::io::Error::last_os_error()));
        }
        let mut quiet = original;
        quiet.c_lflag &= !libc::ECHO;
        unsafe { libc::tcsetattr(fd, libc::TCSANOW, &quiet) };
        let mut line = Vec::new();
        let read = BufReader::new(&tty).read_until(b'\n', &mut line);
        unsafe { libc::tcsetattr(fd, libc::TCSANOW, &original) };
        let _ = tty.write_all(b"\n");
        read?;
        if line.ends_with(b"\n") {
            line.pop();
        }
        if line.ends_with(b"\r") {
            line.pop();
        }
        Ok(line)
    }
}

#[cfg(not(unix))]
mod tty {
    use crate::fail::{Result, fail};

    pub fn available() -> bool {
        false
    }

    pub fn read_secret(_label: &str) -> Result<Vec<u8>> {
        fail("no terminal to prompt on this platform; use --pin-file or --pin-env")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn precedence_and_sources() {
        let dir = std::env::temp_dir();
        let path = dir.join(format!("aloelite-cli-pin-{}", std::process::id()));
        std::fs::write(&path, b"secret\n\n").unwrap();
        let file = path.to_string_lossy().into_owned();
        // value beats file beats env
        let v = PinArg::Value("abc".to_owned());
        assert_eq!(
            read_pin(Some(&v), Some(&file), None, false)
                .unwrap()
                .unwrap(),
            b"abc"
        );
        assert_eq!(
            read_pin(None, Some(&file), None, false).unwrap().unwrap(),
            b"secret"
        );
        assert!(read_pin(None, None, Some("ALOELITE_UNSET_VAR_XYZ"), false).is_err());
        assert!(read_pin(None, None, None, false).unwrap().is_none());
        std::fs::remove_file(&path).ok();
    }
}
