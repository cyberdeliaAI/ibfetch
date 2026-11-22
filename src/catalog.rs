use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::{BTreeMap, HashMap, HashSet, VecDeque},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};
use tokio_util::sync::CancellationToken;
use unicode_casefold::UnicodeCaseFold;
use unicode_normalization::{UnicodeNormalization, char::canonical_combining_class};

pub const DEFAULT_LAYOUT: &str = "{albumartist}/{album} ({year})/{disctrack} {title}";
pub const FIELDS: &[&str] = &[
    "albumartist",
    "album",
    "year",
    "disc",
    "track",
    "disctrack",
    "title",
    "artist",
    "genre",
    "format",
];

pub(crate) fn text(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        other => other.to_string(),
    }
}
pub(crate) fn number(value: &Value) -> u64 {
    value
        .as_u64()
        .or_else(|| value.as_str()?.parse().ok())
        .or_else(|| {
            value
                .as_f64()
                .or_else(|| value.as_str()?.parse().ok())
                .map(|n| n as u64)
        })
        .unwrap_or(0)
}
fn truth(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().unwrap_or(0.) != 0.,
        Value::String(s) => !s.is_empty(),
        Value::Array(x) => !x.is_empty(),
        Value::Object(x) => !x.is_empty(),
    }
}
fn spacing(value: &str) -> String {
    value.split_whitespace().collect::<Vec<_>>().join(" ")
}
pub fn fold(value: &str) -> String {
    let stripped: String = value
        .nfkd()
        .filter(|c| canonical_combining_class(*c) == 0)
        .collect();
    spacing(&stripped.as_str().case_fold().collect::<String>())
}
pub fn artist_initial(value: &str, skip_the: bool) -> char {
    let decomposed: String = value
        .nfkd()
        .filter(|c| !('\u{0300}'..='\u{036f}').contains(c))
        .collect::<String>()
        .to_lowercase();
    let value = spacing(&decomposed);
    let value = if skip_the {
        value.strip_prefix("the ").unwrap_or(&value)
    } else {
        &value
    };
    value
        .chars()
        .next()
        .filter(char::is_ascii_alphabetic)
        .map_or('#', |c| c.to_ascii_uppercase())
}
fn wildcard(pattern: &[char], value: &str) -> bool {
    let value: Vec<char> = value.chars().collect();
    let (mut p, mut v, mut star, mut retry) = (0, 0, None, 0);
    while v < value.len() {
        match pattern.get(p) {
            Some(&c) if c == '?' || c == value[v] && c != '*' => {
                p += 1;
                v += 1;
            }
            Some('*') => {
                star = Some(p);
                p += 1;
                retry = v;
            }
            _ => match star {
                Some(s) => {
                    p = s + 1;
                    retry += 1;
                    v = retry;
                }
                None => return false,
            },
        }
    }
    pattern[p..].iter().all(|&c| c == '*')
}
struct Patterns(Vec<Vec<char>>);
impl Patterns {
    fn new(values: &[String]) -> Self {
        Self(values.iter().map(|s| fold(s).chars().collect()).collect())
    }
    fn active(&self) -> bool {
        !self.0.is_empty()
    }
    fn matches(&self, value: &str, skip_the: bool) -> bool {
        if !self.active() {
            return true;
        }
        let value = fold(value);
        self.0.iter().any(|p| {
            wildcard(p, &value)
                || skip_the && value.strip_prefix("the ").is_some_and(|s| wildcard(p, s))
        })
    }
}

#[derive(Clone, Default, Deserialize, Serialize, clap::Args)]
#[serde(default)]
pub struct Filters {
    #[arg(short, long)]
    pub artist: Vec<String>,
    #[arg(short = 'r', long)]
    pub track_artist: Vec<String>,
    #[arg(short = 'A', long)]
    pub album: Vec<String>,
    #[arg(short, long)]
    pub genre: Vec<String>,
    #[arg(short, long)]
    pub year: Vec<String>,
    #[arg(short, long)]
    pub format: Vec<String>,
    #[arg(short, long)]
    pub tag: Vec<String>,
    #[arg(short, long)]
    pub playlist: Vec<String>,
    #[arg(long)]
    pub album_id: Vec<u64>,
    #[arg(long)]
    pub track_id: Vec<u64>,
    #[arg(long)]
    pub artist_initial: Option<char>,
    #[arg(long)]
    pub skip_the: bool,
    #[arg(long)]
    pub all: bool,
}
fn year_range(raw: &str) -> Result<(u64, u64)> {
    fn year(s: &str) -> Option<u64> {
        let s = s.trim();
        (s.len() == 4 && s.bytes().all(|b| b.is_ascii_digit()))
            .then(|| s.parse().ok())
            .flatten()
    }
    if let Some((low, high)) = raw.split_once('-') {
        return year(low)
            .zip(if high.trim().is_empty() {
                Some(9999)
            } else {
                year(high)
            })
            .context("Enter a year or a range such as 1990-1999.");
    }
    year(raw)
        .map(|y| (y, y))
        .context("Enter a four-digit year.")
}

#[derive(Default)]
struct Table {
    rows: BTreeMap<u64, Value>,
    fields: HashMap<String, usize>,
}
impl Table {
    fn take(raw: Value) -> Self {
        let Value::Object(mut rows) = raw else {
            return Self::default();
        };
        let fields = match rows.remove("map") {
            Some(Value::Object(fields)) => fields
                .into_iter()
                .filter_map(|(k, v)| Some((k, usize::try_from(v.as_u64()?).ok()?)))
                .collect(),
            _ => HashMap::new(),
        };
        let rows = rows
            .into_iter()
            .filter_map(|(id, row)| {
                if !(row.is_array() || row.is_object()) {
                    return None;
                }
                Some((id.parse::<u64>().ok()?, row))
            })
            .collect();
        Self { rows, fields }
    }
    fn get(&self, id: u64, field: &str) -> &Value {
        match self.rows.get(&id) {
            Some(Value::Array(row)) => self
                .fields
                .get(field)
                .and_then(|i| row.get(*i))
                .unwrap_or(&Value::Null),
            Some(Value::Object(row)) => row.get(field).unwrap_or(&Value::Null),
            _ => &Value::Null,
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Group {
    pub id: u64,
    pub tracks: Vec<u64>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Job {
    pub track_id: u64,
    pub relative: PathBuf,
    pub stream: Option<String>,
    pub source_path: String,
}
struct Plan {
    directories: Option<HashMap<String, HashSet<u64>>>,
    paths: VecDeque<(String, Arc<HashMap<u64, PathBuf>>)>,
}
pub struct Library {
    artists: Table,
    albums: Table,
    tracks: Table,
    playlists: Table,
    tags: Table,
    discs: HashMap<(u64, String), HashSet<u64>>,
    members: BTreeMap<u64, Vec<u64>>,
    plan: Mutex<Plan>,
    pub owner: String,
    pub name: String,
    pub premium: bool,
    pub expires: String,
}
impl Library {
    pub fn parse(mut response: Value, owner: String, name: String, premium: bool) -> Result<Self> {
        ensure!(
            !owner.trim().is_empty(),
            "iBroadcast did not identify the account."
        );
        let data = if response.get("library").is_some() {
            &mut response["library"]
        } else {
            &mut response
        };
        ensure!(
            ["artists", "albums", "tracks"]
                .iter()
                .all(|k| data[*k].is_object()),
            "Incomplete iBroadcast library."
        );
        let albums = Table::take(data["albums"].take());
        let tracks = Table::take(data["tracks"].take());
        let mut discs = HashMap::<_, HashSet<_>>::new();
        let mut members = BTreeMap::<_, Vec<_>>::new();
        for &id in albums.rows.keys() {
            if truth(albums.get(id, "trashed")) {
                continue;
            }
            discs
                .entry((
                    number(albums.get(id, "artist_id")),
                    fold(&text(albums.get(id, "name"))),
                ))
                .or_default()
                .insert(number(albums.get(id, "disc")).max(1));
        }
        for &id in tracks.rows.keys() {
            let album = number(tracks.get(id, "album_id"));
            if !truth(tracks.get(id, "trashed")) && !truth(albums.get(album, "trashed")) {
                members.entry(album).or_default().push(id);
            }
        }
        Ok(Self {
            artists: Table::take(data["artists"].take()),
            albums,
            tracks,
            playlists: Table::take(data["playlists"].take()),
            tags: Table::take(data["tags"].take()),
            discs,
            members,
            plan: Mutex::new(Plan {
                directories: None,
                paths: VecDeque::new(),
            }),
            owner,
            name,
            premium,
            expires: text(&data["expires"]),
        })
    }
    pub fn track_count(&self) -> usize {
        self.members.values().map(Vec::len).sum()
    }
    pub fn album_count(&self) -> usize {
        self.members.len()
    }
    pub fn album_artist(&self, id: u64) -> String {
        fallback(
            text(
                self.artists
                    .get(number(self.albums.get(id, "artist_id")), "name"),
            ),
            "Unknown Artist",
        )
    }
    pub fn album_title(&self, id: u64) -> String {
        fallback(text(self.albums.get(id, "name")), "Unknown Album")
    }
    pub fn year(&self, album: u64, track: Option<u64>) -> u64 {
        let year = number(self.albums.get(album, "year"));
        if year == 0 {
            track.map_or(0, |id| number(self.tracks.get(id, "year")))
        } else {
            year
        }
    }
    pub fn disc(&self, album: u64) -> u64 {
        number(self.albums.get(album, "disc")).max(1)
    }
    pub fn multi_disc(&self, album: u64) -> bool {
        self.disc(album) > 1
            || self
                .discs
                .get(&(
                    number(self.albums.get(album, "artist_id")),
                    fold(&text(self.albums.get(album, "name"))),
                ))
                .is_some_and(|discs| discs.len() > 1)
    }
    pub fn track_title(&self, track: u64) -> String {
        fallback(
            text(self.tracks.get(track, "title")),
            &format!("Track {track}"),
        )
    }
    pub fn track_number(&self, track: u64) -> u64 {
        number(self.tracks.get(track, "track"))
    }
    pub fn track_artist(&self, track: u64) -> String {
        text(
            self.artists
                .get(number(self.tracks.get(track, "artist_id")), "name"),
        )
    }
    pub fn duration(&self, track: u64) -> u64 {
        number(self.tracks.get(track, "length"))
    }
    fn genres(&self, track: u64) -> Vec<String> {
        std::iter::once(self.tracks.get(track, "genre"))
            .chain(
                self.tracks
                    .get(track, "genres_additional")
                    .as_array()
                    .into_iter()
                    .flatten(),
            )
            .map(text)
            .filter(|g| !g.trim().is_empty())
            .collect()
    }
    fn mime(&self, track: u64) -> String {
        text(self.tracks.get(track, "type")).trim().to_lowercase()
    }
    pub fn format(&self, track: u64) -> String {
        let mime = self.mime(track);
        let ext = mime_extension(&mime).map_or_else(|| tail_mime(&mime), str::to_owned);
        match ext.as_str() {
            "aiff" => "AIFF".into(),
            "" => "?".into(),
            other => other.to_uppercase(),
        }
    }
    fn extension(&self, track: u64, quality: &str) -> String {
        if quality != "orig" {
            return "mp3".into();
        }
        let mime = self.mime(track);
        let ext = mime_extension(&mime).map_or_else(
            || {
                tail_mime(&mime)
                    .chars()
                    .filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
                    .collect()
            },
            str::to_owned,
        );
        fallback(ext, "audio")
    }
    pub fn artists(&self) -> Vec<(String, usize)> {
        let mut counts = HashMap::new();
        for &a in self.members.keys() {
            *counts.entry(self.album_artist(a)).or_insert(0) += 1;
        }
        let mut counts: Vec<_> = counts.into_iter().collect();
        counts.sort_by_cached_key(|(s, _)| (fold(s), s.clone()));
        counts
    }
    pub fn metadata(&self) -> Value {
        let mut artists = BTreeMap::<String, (usize, usize)>::new();
        let mut genres = BTreeMap::<String, usize>::new();
        let mut formats = BTreeMap::<String, usize>::new();
        for (&album, ids) in &self.members {
            let stats = artists.entry(self.album_artist(album)).or_default();
            stats.0 += 1;
            stats.1 += ids.len();
            for &id in ids {
                let genre = text(self.tracks.get(id, "genre"));
                if !genre.trim().is_empty() {
                    *genres.entry(genre.trim().into()).or_default() += 1;
                }
                *formats.entry(self.format(id)).or_default() += 1;
            }
        }
        let mut artists: Vec<_> = artists.into_iter().collect();
        artists.sort_by_cached_key(|(s, _)| fold(s));
        let mut genres: Vec<_> = genres.into_keys().collect();
        genres.sort_by_cached_key(|s| fold(s));
        let mut formats: Vec<_> = formats.into_iter().collect();
        formats.sort_by_key(|(_, n)| std::cmp::Reverse(*n));
        serde_json::json!({"artists":artists.iter().map(|(name,(a,t))|serde_json::json!([name,a,t])).collect::<Vec<_>>(),
            "genres":genres,"formats":formats.into_iter().map(|(n,_)|n).collect::<Vec<_>>(),
            "tags":self.named_values(false),"playlists":self.named_values(true),
            "counts":{"tracks":self.track_count(),"albums":self.album_count(),"artists":artists.len()}})
    }
    pub fn validate_items(&self, items: &[Group]) -> Vec<Group> {
        let mut picked = BTreeMap::<u64, HashSet<u64>>::new();
        for group in items {
            if let Some(ids) = self.members.get(&group.id) {
                picked.entry(group.id).or_default().extend(
                    group
                        .tracks
                        .iter()
                        .copied()
                        .filter(|id| ids.binary_search(id).is_ok()),
                );
            }
        }
        let mut sorted: Vec<_> = picked
            .into_iter()
            .filter(|(_, ts)| !ts.is_empty())
            .map(|(id, tracks)| {
                let mut tracks: Vec<_> = tracks.into_iter().collect();
                tracks.sort_by_cached_key(|&id| {
                    let n = self.track_number(id);
                    (
                        if n == 0 { 9999 } else { n },
                        fold(&text(self.tracks.get(id, "title"))),
                        id,
                    )
                });
                Group { id, tracks }
            })
            .collect();
        sorted.sort_by_cached_key(|g| {
            (
                fold(&self.album_artist(g.id)),
                fold(&self.album_title(g.id)),
                self.year(g.id, None),
                self.disc(g.id),
                g.id,
            )
        });
        sorted
    }
    pub fn has_track(&self, id: u64) -> bool {
        self.tracks.rows.contains_key(&id)
    }
    pub fn matches_artist(pattern: &str, value: &str, skip_the: bool) -> bool {
        Patterns::new(&[pattern.into()]).matches(value, skip_the)
    }
    pub fn named_values(&self, playlist: bool) -> Vec<String> {
        let table = if playlist {
            &self.playlists
        } else {
            &self.tags
        };
        let mut values: Vec<_> = table
            .rows
            .keys()
            .map(|id| text(table.get(*id, "name")))
            .filter(|s| !s.is_empty())
            .collect();
        values.sort_by_cached_key(|s| (fold(s), s.clone()));
        values.dedup();
        values
    }
    pub fn select(&self, f: &Filters, stop: &CancellationToken) -> Result<Vec<Group>> {
        let (artists, albums, track_artists, genres, formats) = (
            Patterns::new(&f.artist),
            Patterns::new(&f.album),
            Patterns::new(&f.track_artist),
            Patterns::new(&f.genre),
            Patterns::new(&f.format),
        );
        let years = f
            .year
            .iter()
            .map(|s| year_range(s))
            .collect::<Result<Vec<_>>>()?;
        ensure!(
            f.artist_initial
                .is_none_or(|c| c == '#' || c.is_ascii_uppercase()),
            "Artist group must be A–Z or #."
        );
        let mut named = None::<HashSet<u64>>;
        for (table, values, label) in [
            (&self.tags, &f.tag, "tag"),
            (&self.playlists, &f.playlist, "playlist"),
        ] {
            if values.is_empty() {
                continue;
            }
            let pats = Patterns::new(values);
            let mut allowed = HashSet::new();
            let mut found = false;
            for id in table
                .rows
                .keys()
                .copied()
                .filter(|id| pats.matches(&text(table.get(*id, "name")), false))
            {
                found = true;
                if let Some(tracks) = table.get(id, "tracks").as_array() {
                    allowed.extend(tracks.iter().map(number));
                }
            }
            ensure!(found, "No {label} matches that filter.");
            named = Some(named.map_or_else(
                || allowed.clone(),
                |prev| prev.intersection(&allowed).copied().collect(),
            ));
        }
        ensure!(
            f.all
                || f.artist_initial.is_some()
                || named.is_some()
                || !years.is_empty()
                || !f.album_id.is_empty()
                || !f.track_id.is_empty()
                || [&artists, &albums, &track_artists, &genres, &formats]
                    .iter()
                    .any(|p| p.active()),
            "Add a filter or use --all to select the whole library."
        );
        let album_ids: HashSet<_> = f.album_id.iter().copied().collect();
        let track_ids: HashSet<_> = f.track_id.iter().copied().collect();
        let mut groups = Vec::new();
        for (&album, members) in &self.members {
            cancel(stop)?;
            if !album_ids.is_empty() && !album_ids.contains(&album) {
                continue;
            }
            if artists.active() || f.artist_initial.is_some() {
                let name = self.album_artist(album);
                if !artists.matches(&name, f.skip_the)
                    || f.artist_initial
                        .is_some_and(|c| artist_initial(&name, f.skip_the) != c)
                {
                    continue;
                }
            }
            if albums.active() && !albums.matches(&self.album_title(album), f.skip_the) {
                continue;
            }
            let mut tracks = Vec::new();
            for &track in members {
                if (!track_ids.is_empty() && !track_ids.contains(&track))
                    || named.as_ref().is_some_and(|ids| !ids.contains(&track))
                {
                    continue;
                }
                if track_artists.active()
                    && !track_artists.matches(&self.track_artist(track), f.skip_the)
                {
                    continue;
                }
                if !years.is_empty()
                    && !years
                        .iter()
                        .any(|&(low, high)| (low..=high).contains(&self.year(album, Some(track))))
                {
                    continue;
                }
                if genres.active() && !self.genres(track).iter().any(|g| genres.matches(g, false)) {
                    continue;
                }
                if formats.active()
                    && !formats.matches(&self.format(track), false)
                    && !formats.matches(&self.mime(track), false)
                {
                    continue;
                }
                tracks.push(track);
            }
            if !tracks.is_empty() {
                tracks.sort_by_cached_key(|&id| {
                    let n = self.track_number(id);
                    (
                        if n == 0 { 9999 } else { n },
                        fold(&text(self.tracks.get(id, "title"))),
                        id,
                    )
                });
                groups.push(Group { id: album, tracks });
            }
        }
        groups.sort_by_cached_key(|g| {
            (
                fold(&self.album_artist(g.id)),
                fold(&self.album_title(g.id)),
                self.year(g.id, None),
                self.disc(g.id),
                g.id,
            )
        });
        Ok(groups)
    }
    pub fn plan(
        &self,
        groups: &[Group],
        quality: &str,
        layout: &Layout,
        stop: &CancellationToken,
    ) -> Result<Vec<Job>> {
        ensure!(
            crate::QUALITIES.contains(&quality),
            "Invalid download quality."
        );
        let mut cache = self
            .plan
            .lock()
            .map_err(|_| anyhow::anyhow!("Download planner unavailable."))?;
        let parents = if layout.template == DEFAULT_LAYOUT {
            if cache.directories.is_none() {
                let mut dirs = HashMap::<_, HashSet<_>>::new();
                for (&album, members) in &self.members {
                    cancel(stop)?;
                    let mut years = HashSet::new();
                    for &id in members {
                        if years.insert(self.year(album, Some(id))) {
                            let dir = layout.relative(self, album, id, "audio")?;
                            dirs.entry(path_key(dir.parent().unwrap_or(Path::new(""))))
                                .or_default()
                                .insert(album);
                        }
                    }
                }
                cache.directories = Some(dirs);
            }
            let mut parents = HashSet::new();
            for g in groups {
                for &id in &g.tracks {
                    cancel(stop)?;
                    let rel = layout.relative(self, g.id, id, "audio")?;
                    parents.insert(path_key(rel.parent().unwrap_or(Path::new(""))));
                }
            }
            Some(parents)
        } else {
            None
        };
        let mut ordered = parents.iter().flatten().collect::<Vec<_>>();
        ordered.sort_unstable();
        let key = format!("{quality}\0{}\0{:?}", layout.template, ordered);
        let paths = if let Some((_, paths)) = cache.paths.iter().find(|(k, _)| *k == key) {
            paths.clone()
        } else {
            let albums: Vec<u64> = if let Some(parents) = &parents {
                parents
                    .iter()
                    .filter_map(|p| cache.directories.as_ref()?.get(p))
                    .flatten()
                    .copied()
                    .collect::<HashSet<_>>()
                    .into_iter()
                    .collect()
            } else {
                self.members.keys().copied().collect()
            };
            let mut candidates = BTreeMap::new();
            let mut counts = HashMap::<_, usize>::new();
            for album in albums {
                for &id in &self.members[&album] {
                    cancel(stop)?;
                    let mut rel = layout.relative(self, album, id, &self.extension(id, quality))?;
                    if parents.as_ref().is_some_and(|p| {
                        !p.contains(&path_key(rel.parent().unwrap_or(Path::new(""))))
                    }) {
                        continue;
                    }
                    if quality != "orig" {
                        rel = with_suffix(&rel, &format!("{quality}kbps"));
                    }
                    *counts.entry(path_key(&rel)).or_default() += 1;
                    candidates.insert(id, rel);
                }
            }
            let mut seen = HashSet::new();
            for (&id, rel) in &mut candidates {
                cancel(stop)?;
                if counts[&path_key(rel)] > 1 {
                    let mut attempt = 1;
                    loop {
                        let tag = if attempt == 1 {
                            id.to_string()
                        } else {
                            format!("{id}-{attempt}")
                        };
                        let candidate = with_suffix(rel, &tag);
                        let k = path_key(&candidate);
                        if !counts.contains_key(&k) && !seen.contains(&k) {
                            *rel = candidate;
                            break;
                        }
                        attempt += 1;
                    }
                }
                seen.insert(path_key(rel));
            }
            let paths = Arc::new(candidates.into_iter().collect());
            if cache.paths.len() >= 4 {
                cache.paths.pop_front();
            }
            cache.paths.push_back((key, Arc::clone(&paths)));
            paths
        };
        drop(cache);
        groups
            .iter()
            .flat_map(|g| g.tracks.iter())
            .map(|&id| {
                cancel(stop)?;
                let relative = paths
                    .get(&id)
                    .cloned()
                    .context("Selection contains an unknown track.")?;
                let source_path = text(self.tracks.get(id, "file"));
                let stream = source_path
                    .strip_prefix('/')
                    .filter(|s| !s.starts_with('/') && !s.contains(['?', '#', '\\']))
                    .map(|s| {
                        if let Some((q, tail)) = s.split_once('/')
                            && crate::QUALITIES.contains(&q)
                        {
                            return format!("/{quality}/{tail}");
                        }
                        source_path.clone()
                    });
                Ok(Job {
                    track_id: id,
                    relative,
                    stream,
                    source_path,
                })
            })
            .collect()
    }
}
pub fn cancel(stop: &CancellationToken) -> Result<()> {
    ensure!(!stop.is_cancelled(), "Stopped.");
    Ok(())
}
fn fallback(value: String, default: &str) -> String {
    if value.is_empty() {
        default.into()
    } else {
        value
    }
}
fn tail_mime(mime: &str) -> String {
    mime.rsplit('/').next().unwrap_or("").replace("x-", "")
}
fn mime_extension(mime: &str) -> Option<&'static str> {
    Some(match mime {
        "audio/mpeg" | "audio/mp3" => "mp3",
        "audio/flac" | "audio/x-flac" => "flac",
        "audio/mp4" | "audio/x-m4a" | "audio/m4a" => "m4a",
        "audio/aac" => "aac",
        "audio/ogg" | "audio/vorbis" => "ogg",
        "audio/opus" => "opus",
        "audio/wav" | "audio/x-wav" | "audio/wave" => "wav",
        "audio/aiff" | "audio/x-aiff" => "aiff",
        "audio/dsf" | "audio/x-dsf" => "dsf",
        "audio/x-ms-wma" => "wma",
        "audio/x-wavpack" => "wv",
        "audio/ape" | "audio/x-ape" => "ape",
        _ => return None,
    })
}
pub fn safe_name(s: &str, limit: usize) -> String {
    let substituted: String = s
        .chars()
        .map(|c| {
            if c <= '\u{001f}' || c == '\u{007f}' || "\\/:*?\"<>|".contains(c) {
                '_'
            } else {
                c
            }
        })
        .collect();
    let mut s = spacing(&substituted);
    if s.len() > limit {
        let mut bound = limit;
        while !s.is_char_boundary(bound) {
            bound -= 1;
        }
        s.truncate(bound);
        s = s.trim_end().into();
    }
    s = s.trim_end_matches(['.', ' ']).into();
    if s.starts_with('.') {
        s.replace_range(..1, "_");
    }
    let first = s.split('.').next().unwrap_or("").to_ascii_lowercase();
    if matches!(first.as_str(), "con" | "prn" | "aux" | "nul")
        || (first.len() == 4
            && ["com", "lpt"].iter().any(|p| first.starts_with(p))
            && (b'1'..=b'9').contains(&first.as_bytes()[3]))
    {
        s.push('_');
    }
    fallback(s, "_")
}
fn with_suffix(path: &Path, suffix: &str) -> PathBuf {
    path.with_file_name(format!(
        "{} [{}].{}",
        path.file_stem().unwrap_or_default().to_string_lossy(),
        suffix,
        path.extension().unwrap_or_default().to_string_lossy()
    ))
}
fn path_key(path: &Path) -> String {
    fold(&path.to_string_lossy())
}
#[derive(Clone)]
pub struct Layout {
    pub template: String,
    tokens: Vec<Part>,
}
#[derive(Clone)]
enum Part {
    Literal(String),
    Field(usize),
}
impl Layout {
    pub fn new(template: &str) -> Result<Self> {
        ensure!(
            !template.is_empty() && template.len() <= 2048,
            "Enter a folder/filename layout up to 2048 bytes."
        );
        let template = template.replace('\\', "/").trim_matches('/').to_owned();
        ensure!(!template.is_empty(), "Layout must name a file.");
        let mut rest = template.as_str();
        let mut tokens = Vec::new();
        while let Some(i) = rest.find(['{', '}']) {
            if i > 0 {
                tokens.push(Part::Literal(rest[..i].into()));
            }
            let field = &rest[i..];
            if field.starts_with("{{") || field.starts_with("}}") {
                tokens.push(Part::Literal(field[..1].into()));
                rest = &field[2..];
                continue;
            }
            ensure!(field.starts_with('{'), "Unmatched closing brace in layout.");
            let end = field.find('}').context("Unclosed field in layout.")?;
            let key = &field[1..end];
            let Some(index) = FIELDS.iter().position(|k| *k == key) else {
                bail!("Unsupported layout field {{{key}}}.");
            };
            tokens.push(Part::Field(index));
            rest = &field[end + 1..];
        }
        if !rest.is_empty() {
            tokens.push(Part::Literal(rest.into()));
        }
        Ok(Self { template, tokens })
    }
    fn relative(&self, lib: &Library, album: u64, id: u64, ext: &str) -> Result<PathBuf> {
        let n = lib.track_number(id);
        let disc = lib.disc(album);
        let tn = if n > 0 {
            format!("{n:02}")
        } else {
            String::new()
        };
        let artist = lib.album_artist(album);
        let year = lib.year(album, Some(id));
        let values = [
            artist.clone(),
            lib.album_title(album),
            if year > 0 {
                year.to_string()
            } else {
                String::new()
            },
            disc.to_string(),
            tn.clone(),
            if !tn.is_empty() && lib.multi_disc(album) {
                format!("{disc}-{tn}")
            } else {
                tn
            },
            lib.track_title(id),
            fallback(lib.track_artist(id), &artist),
            lib.genres(id)
                .into_iter()
                .next()
                .unwrap_or_else(|| "Unknown Genre".into()),
            lib.format(id),
        ];
        let mut result = String::new();
        for token in &self.tokens {
            match token {
                Part::Literal(s) => result.push_str(s),
                Part::Field(i) if !values[*i].is_empty() => {
                    result.push_str(&safe_name(&values[*i], 400))
                }
                Part::Field(_) => {}
            }
        }
        let mut parts: Vec<String> = result.split('/').map(clean_component).collect();
        let last = parts.last_mut().context("Layout must name a file.")?;
        *last = format!(
            "{}.{}",
            safe_name(last, 150usize.saturating_sub(ext.len() + 1)),
            ext
        );
        Ok(parts.into_iter().collect())
    }
}
fn clean_component(part: &str) -> String {
    let mut result = String::new();
    let mut chars = part.chars().peekable();
    while let Some(c) = chars.next() {
        if matches!(c, '(' | '[') {
            let close = if c == '(' { ')' } else { ']' };
            let mut space = String::new();
            while chars.peek().is_some_and(|c| c.is_whitespace()) {
                space.push(chars.next().unwrap_or_default());
            }
            if chars.peek() == Some(&close) {
                chars.next();
                result.truncate(result.trim_end().len());
                continue;
            }
            result.push(c);
            result.push_str(&space);
        } else {
            result.push(c);
        }
    }
    let leading = result.trim_start();
    if let Some(after) = leading.strip_prefix(['-', '.'])
        && after.starts_with(char::is_whitespace)
    {
        return safe_name(after.trim_start(), 150);
    }
    safe_name(&result, 150)
}
