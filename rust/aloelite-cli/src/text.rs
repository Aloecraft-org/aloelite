//! Small text helpers the listings share: the minute-resolution timestamp,
//! the pluraliser, and Python's dict syntax for `stat`'s metadata line.

use std::collections::BTreeMap;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// `YYYY-MM-DD HH:MM` in UTC for a nanosecond timestamp, `-` for none. The
/// reference prints the local minute; the contract records the difference.
pub fn minute_utc(ns: Option<i64>) -> String {
    let Some(ns) = ns else {
        return "-".to_owned();
    };
    let secs = ns.div_euclid(1_000_000_000);
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    format!(
        "{y:04}-{m:02}-{d:02} {:02}:{:02}",
        rem / 3600,
        (rem % 3600) / 60
    )
}

/// `1 file`, `2 files`, `3 directories`.
pub fn n(count: usize, noun: &str, plural: Option<&str>) -> String {
    if count == 1 {
        format!("{count} {noun}")
    } else {
        format!(
            "{count} {}",
            plural.map_or_else(|| format!("{noun}s"), str::to_owned)
        )
    }
}

/// `{'k': 'v', ...}` — what the reference prints for a metadata map.
pub fn py_dict(map: &BTreeMap<String, String>) -> String {
    let items: Vec<String> = map
        .iter()
        .map(|(k, v)| format!("{}: {}", py_str(k), py_str(v)))
        .collect();
    format!("{{{}}}", items.join(", "))
}

// ---------------------------------------------------------------------------
// depth
// ---------------------------------------------------------------------------

fn py_str(s: &str) -> String {
    let escaped = s.replace('\\', "\\\\").replace('\'', "\\'");
    format!("'{escaped}'")
}

/// Days since 1970-01-01 → (year, month, day). Howard Hinnant's algorithm.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timestamps_render_the_utc_minute() {
        assert_eq!(minute_utc(None), "-");
        assert_eq!(minute_utc(Some(0)), "1970-01-01 00:00");
        // 2026-09-03 00:41:33 UTC
        assert_eq!(
            minute_utc(Some(1_788_396_093_000_000_000)),
            "2026-09-03 00:41"
        );
        // a leap day
        assert_eq!(
            minute_utc(Some(951_782_400_000_000_000)),
            "2000-02-29 00:00"
        );
    }

    #[test]
    fn pluralising_and_dict_syntax() {
        assert_eq!(n(1, "file", None), "1 file");
        assert_eq!(n(2, "file", None), "2 files");
        assert_eq!(n(0, "directory", Some("directories")), "0 directories");
        let m = BTreeMap::from([
            ("k".to_owned(), "v".to_owned()),
            ("a".to_owned(), "it's".to_owned()),
        ]);
        assert_eq!(py_dict(&m), "{'a': 'it\\'s', 'k': 'v'}");
    }
}
