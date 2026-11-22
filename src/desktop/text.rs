//! Wording and number formats shared with the bundled browser page.
use ibfetch::catalog::fold;
use std::{cmp::Ordering, path::Path};

/// A whole number with thousands separators, like `290,576`.
pub fn num(n: u64) -> String {
    let digits = n.to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}
pub fn plural(n: u64, word: &str) -> String {
    format!("{} {word}{}", num(n), if n == 1 { "" } else { "s" })
}
/// A track or album length: `35:19`, or `1:19:32` from one hour.
pub fn clock(seconds: u64) -> String {
    let (h, m, s) = (seconds / 3600, seconds % 3600 / 60, seconds % 60);
    if h > 0 {
        format!("{h}:{m:02}:{s:02}")
    } else {
        format!("{m}:{s:02}")
    }
}
/// A rough duration in words: `27 h 11 min`, `55 s`.
pub fn span(seconds: f64) -> String {
    let seconds = seconds.max(0.).round() as u64;
    let (h, m) = (seconds / 3600, seconds % 3600 / 60);
    if h > 0 {
        format!("{} h {m} min", num(h))
    } else if m > 0 {
        format!("{m} min")
    } else {
        format!("{seconds} s")
    }
}
/// A byte count: `250 MB`, `8.9 MB`, `512 B`.
pub fn size(bytes: f64) -> String {
    let mut value = bytes.max(0.);
    let mut unit = 0;
    const UNITS: [&str; 5] = ["B", "KB", "MB", "GB", "TB"];
    while value >= 1024. && unit < UNITS.len() - 1 {
        value /= 1024.;
        unit += 1;
    }
    if unit == 0 {
        format!("{} B", value.round() as u64)
    } else if value < 10. {
        format!("{value:.1} {}", UNITS[unit])
    } else {
        format!("{value:.0} {}", UNITS[unit])
    }
}
/// The file name of a relative download path.
pub fn base(path: &str) -> &str {
    Path::new(path)
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or(path)
}

/// A sort key that ignores case and accents and orders digit runs by value,
/// so `Vol. 2` comes before `Vol. 10`.
#[derive(PartialEq, Eq)]
pub struct Natural(String);
impl Natural {
    pub fn new(value: &str) -> Self {
        Self(fold(value))
    }
}
impl Ord for Natural {
    fn cmp(&self, other: &Self) -> Ordering {
        let (mut a, mut b) = (self.0.as_str(), other.0.as_str());
        loop {
            let (Some(x), Some(y)) = (a.chars().next(), b.chars().next()) else {
                return a.len().cmp(&b.len());
            };
            if x.is_ascii_digit() && y.is_ascii_digit() {
                let digits = |s: &str| s.bytes().take_while(u8::is_ascii_digit).count();
                let (m, n) = (digits(a), digits(b));
                let (p, q) = (
                    a[..m].trim_start_matches('0'),
                    b[..n].trim_start_matches('0'),
                );
                let order = p.len().cmp(&q.len()).then_with(|| p.cmp(q));
                if order != Ordering::Equal {
                    return order;
                }
                (a, b) = (&a[m..], &b[n..]);
            } else if x != y {
                return x.cmp(&y);
            } else {
                (a, b) = (&a[x.len_utf8()..], &b[y.len_utf8()..]);
            }
        }
    }
}
impl PartialOrd for Natural {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_match_the_browser_page() {
        assert_eq!(num(0), "0");
        assert_eq!(num(999), "999");
        assert_eq!(num(1000), "1,000");
        assert_eq!(num(290_576), "290,576");
        assert_eq!(num(1_234_567), "1,234,567");
        assert_eq!(plural(1, "album"), "1 album");
        assert_eq!(plural(21_375, "album"), "21,375 albums");
        assert_eq!(clock(2119), "35:19");
        assert_eq!(clock(4772), "1:19:32");
        assert_eq!(clock(5), "0:05");
        assert_eq!(span(97_860.), "27 h 11 min");
        assert_eq!(span(125.), "2 min");
        assert_eq!(span(54.6), "55 s");
        assert_eq!(size(512.), "512 B");
        assert_eq!(size(250. * 1024. * 1024.), "250 MB");
        assert_eq!(size(8.9 * 1024. * 1024.), "8.9 MB");
        assert_eq!(size(1.2 * 1024. * 1024. * 1024.), "1.2 GB");
        assert_eq!(
            base("Björk/Post (1995)/01 Army of Me.flac"),
            "01 Army of Me.flac"
        );
        assert_eq!(base("plain.mp3"), "plain.mp3");
    }

    #[test]
    fn natural_order_ignores_case_and_compares_numbers_by_value() {
        let mut names = vec!["Vol. 10", "vol. 2", "Älbum", "album 1", "Vol. 02 b", "B"];
        names.sort_by_key(|name| Natural::new(name));
        assert_eq!(
            names,
            ["Älbum", "album 1", "B", "vol. 2", "Vol. 02 b", "Vol. 10"]
        );
    }
}
