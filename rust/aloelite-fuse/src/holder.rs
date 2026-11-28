//! Who holds a mount, and whether they are provably gone (#15).
//!
//! A daemon killed with SIGKILL never unmounts, so its rw mount row blocked
//! the next daemon on the volume until the row's lease ran out. Each daemon
//! now records itself as the mount's holder, and the next one retires any
//! mount whose holder is a process on this same kernel and pid namespace
//! that no longer exists. Anything it cannot judge is left to the lease.
//!
//! The port of `aloelite/holder.py`; the format is fixed by mount-api.yaml
//! `mount.holder`:
//!
//! ```text
//! linux boot=<boot_id> pidns=<inode> pid=<pid> start=<starttime> host=<name>
//! ```

use std::collections::HashMap;

use aloelite_core::types::VolumeId;
use aloelite_core::{Db, ops};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------
//
// Entry points: identity, is_gone, retire_dead.
// Configurable: none; the format is the spec's.

/// This process as a holder, or `None` where there is no /proc to say so.
pub fn identity() -> Option<String> {
    let (boot, pidns) = here()?;
    let pid = std::process::id();
    let start = starttime(pid)?;
    let host = hostname().replace(' ', "_");
    let host = if host.is_empty() {
        "-".to_owned()
    } else {
        host
    };
    Some(format!(
        "{SCHEME} boot={boot} pidns={pidns} pid={pid} start={start} host={host}"
    ))
}

/// True only when `holder` names a process of this boot and pid namespace
/// that has exited, or whose pid now belongs to a later process. A holder
/// from another host, boot or namespace, or none at all, cannot be judged
/// from here and is never called gone.
pub fn is_gone(holder: Option<&str>) -> bool {
    let (Some(fields), Some(here)) = (holder.and_then(parse), here()) else {
        return false;
    };
    if (fields.get("boot"), fields.get("pidns")) != (Some(&here.0), Some(&here.1)) {
        return false;
    }
    let (Some(pid), Some(start)) = (
        fields.get("pid").and_then(|p| p.parse::<u32>().ok()),
        fields.get("start").and_then(|s| s.parse::<u64>().ok()),
    ) else {
        return false;
    };
    if pid == std::process::id() {
        return starttime(pid) != Some(start);
    }
    let Ok(raw) = libc::pid_t::try_from(pid) else {
        return false;
    };
    // SAFETY: signal 0 only probes for existence; nothing is delivered.
    if unsafe { libc::kill(raw, 0) } != 0
        && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
    {
        return true;
    }
    // it exists (or kill said EPERM): the same process only if it started
    // when the holder did
    starttime(pid).is_some_and(|now| now != start)
}

/// Unmount every mount on `volume` whose holder [`is_gone`]. Returns how
/// many. Unmount marks the row retired; its locks go invalid with it and
/// prune reclaims them later (ACC-10).
pub fn retire_dead(db: &mut Db, volume: &VolumeId) -> aloelite_core::Result<usize> {
    let mut retired = 0;
    for info in ops::list_mounts(db, Some(volume), false)? {
        if is_gone(info.holder.as_deref()) {
            ops::unmount(db, &info.id)?;
            retired += 1;
        }
    }
    Ok(retired)
}

// ---------------------------------------------------------------------------
// depth: /proc parsing
// ---------------------------------------------------------------------------

const SCHEME: &str = "linux";

fn here() -> Option<(String, String)> {
    let boot = std::fs::read_to_string("/proc/sys/kernel/random/boot_id").ok()?;
    let boot = boot.trim().to_owned();
    let ns = std::fs::read_link("/proc/self/ns/pid").ok()?; // "pid:[4026531836]"
    let ns = ns.to_string_lossy();
    let inode = ns.get(ns.find('[')? + 1..ns.rfind(']')?)?.to_owned();
    if boot.is_empty() || inode.is_empty() || !inode.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    Some((boot, inode))
}

/// Field 22 of /proc/<pid>/stat: clock ticks after boot when the process
/// started. The command name (field 2) may hold spaces and parentheses, so
/// count from the last ')'.
fn starttime(pid: u32) -> Option<u64> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let rest = stat.get(stat.rfind(')')? + 2..)?;
    rest.split_whitespace().nth(19)?.parse().ok() // nth(0) is field 3
}

fn hostname() -> String {
    let mut buf = [0u8; 256];
    // SAFETY: the buffer is valid for its whole length, and gethostname
    // writes at most that many bytes.
    if unsafe { libc::gethostname(buf.as_mut_ptr().cast(), buf.len()) } != 0 {
        return String::new();
    }
    let end = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
    String::from_utf8_lossy(&buf[..end]).into_owned()
}

fn parse(holder: &str) -> Option<HashMap<String, String>> {
    let (scheme, rest) = holder.split_once(' ')?;
    if scheme != SCHEME {
        return None;
    }
    Some(
        rest.split_whitespace()
            .filter_map(|t| t.split_once('='))
            .map(|(k, v)| (k.to_owned(), v.to_owned()))
            .collect(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn exited_holder() -> String {
        let mut child = std::process::Command::new("sleep")
            .arg("30")
            .spawn()
            .unwrap();
        let pid = child.id();
        let start = starttime(pid).unwrap();
        child.kill().unwrap();
        child.wait().unwrap();
        let (boot, pidns) = here().unwrap();
        format!("linux boot={boot} pidns={pidns} pid={pid} start={start} host=x")
    }

    #[test]
    fn retire_dead_frees_the_subtree_and_spares_the_living() {
        use aloelite_core::crypto::EncMode;
        use aloelite_core::ops::MountOptions;

        let dir = std::env::temp_dir().join(format!("aloelite-holder-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut db = aloelite_store::file::open(dir.join("h.fs")).unwrap();
        let vol = ops::create_volume(&mut db, Some("v"), 1 << 20, None, EncMode::Convergent)
            .unwrap()
            .id;
        let dead = exited_holder();
        let opts = |holder| MountOptions {
            ttl_ms: Some(300_000),
            holder,
            ..MountOptions::default()
        };
        ops::mount(&mut db, &vol, &opts(Some(&dead))).unwrap();
        assert_eq!(retire_dead(&mut db, &vol).unwrap(), 1);
        let me = identity().unwrap();
        let live = ops::mount(&mut db, &vol, &opts(Some(&me))).unwrap();
        let rows = ops::list_mounts(&mut db, Some(&vol), false).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].holder.as_deref(), Some(me.as_str()));
        assert_eq!(retire_dead(&mut db, &vol).unwrap(), 0);
        ops::unmount(&mut db, &live).unwrap();
        db.close().unwrap();
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn this_process_is_not_gone() {
        let me = identity().expect("linux has /proc");
        assert!(me.starts_with("linux boot="), "{me}");
        assert!(!is_gone(Some(&me)));
    }

    #[test]
    fn an_exited_process_is_gone() {
        assert!(is_gone(Some(&exited_holder())));
    }

    #[test]
    fn a_reused_pid_is_gone_and_what_cannot_be_judged_is_not() {
        let (boot, pidns) = here().unwrap();
        let pid = std::process::id();
        let start = starttime(pid).unwrap();
        let reused = format!(
            "linux boot={boot} pidns={pidns} pid={pid} start={} host=x",
            start + 1
        );
        assert!(is_gone(Some(&reused)));
        for unjudgeable in [
            None,
            Some("".to_owned()),
            Some("windows pid=1".to_owned()),
            Some(format!(
                "linux boot=other pidns={pidns} pid=1 start=1 host=x"
            )),
            Some(format!("linux boot={boot} pidns=1 pid=1 start=1 host=x")),
            Some(format!("linux boot={boot} pidns={pidns} host=x")),
        ] {
            assert!(!is_gone(unjudgeable.as_deref()), "{unjudgeable:?}");
        }
    }
}
