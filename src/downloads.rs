use crate::{
    QUALITIES,
    account::{Account, HttpError, delay},
    catalog::{Job, Library, cancel},
    storage,
};
use anyhow::{Context, Result, anyhow, bail, ensure};
use reqwest::{
    Response,
    header::{CONTENT_LENGTH, CONTENT_RANGE, CONTENT_TYPE, ETAG, HeaderMap, LAST_MODIFIED},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Component, Path, PathBuf},
    sync::{
        Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    thread,
};
use tokio_util::sync::CancellationToken;

const CHUNK: usize = 256 * 1024;
const ZIP_TAIL: usize = 64 * 1024;
const MANIFEST: &str = ".ibfetch.json";

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, clap::ValueEnum)]
pub enum Source {
    #[default]
    Download,
    Stream,
}
impl Source {
    fn name(self) -> &'static str {
        match self {
            Self::Download => "download",
            Self::Stream => "stream",
        }
    }
    pub fn choose(raw: Option<Self>, quality: &str) -> Result<Self> {
        ensure!(QUALITIES.contains(&quality), "Invalid download quality.");
        let source = raw.unwrap_or(if quality == "orig" {
            Self::Download
        } else {
            Self::Stream
        });
        ensure!(
            source == Self::Stream || quality == "orig",
            "The download service only delivers original files."
        );
        Ok(source)
    }
}
#[derive(Clone)]
pub struct Options {
    pub out: PathBuf,
    pub work: Option<PathBuf>,
    pub quality: String,
    pub source: Source,
    pub workers: usize,
    pub verify: bool,
    pub adopt: bool,
}
impl Options {
    pub fn validate(&mut self) -> Result<()> {
        Source::choose(Some(self.source), &self.quality)?;
        ensure!(
            (1..=8).contains(&self.workers),
            "Choose between 1 and 8 parallel downloads."
        );
        self.out = storage::canonical(&storage::expand(&self.out)?)?;
        if let Some(work) = &mut self.work {
            *work = storage::expand(work)?;
            ensure!(
                work.is_absolute(),
                "Use an absolute path for the work folder."
            );
            *work = storage::canonical(work)?;
            ensure!(
                !work.starts_with(&self.out) && !self.out.starts_with(&work),
                "Use a work folder outside the download folder."
            );
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum State {
    Saved,
    Skipped,
    Failed,
    Stopped,
}
#[derive(Debug, Clone, Serialize)]
pub struct Finished {
    pub state: State,
    pub bytes: u64,
    pub note: String,
}
impl Finished {
    fn saved(bytes: u64, note: &str) -> Self {
        Self {
            state: State::Saved,
            bytes,
            note: note.into(),
        }
    }
    fn skipped(note: impl Into<String>) -> Self {
        Self {
            state: State::Skipped,
            bytes: 0,
            note: note.into(),
        }
    }
}
/// File progress after a write. `received` includes the `resumed` bytes already on disk
/// before this attempt. `chunk_bytes` counts only the audio bytes written by this report.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Progress {
    pub received: u64,
    pub resumed: u64,
    pub chunk_bytes: u64,
    pub total: Option<u64>,
}
#[derive(Clone)]
pub enum Event {
    Started(usize, PathBuf),
    Progress(usize, Progress),
    Finished(usize, PathBuf, Finished),
}
#[derive(Default, Clone, Debug, Serialize)]
pub struct Summary {
    pub saved: usize,
    pub skipped: usize,
    pub failed: usize,
    pub stopped: usize,
    pub bytes: u64,
    pub fatal: Option<String>,
}
#[derive(Debug)]
pub(crate) struct Fatal(pub String);
impl std::fmt::Display for Fatal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for Fatal {}
fn disk<T>(result: Result<T>) -> Result<T> {
    result.map_err(|e| {
        if e.downcast_ref::<std::io::Error>().is_some() {
            Fatal(format!("Local filesystem: {e}")).into()
        } else {
            e
        }
    })
}
pub fn run(
    account: &Account,
    library: &Library,
    jobs: &[Job],
    mut options: Options,
    stop: &CancellationToken,
    emit: impl Fn(Event) + Sync,
) -> Result<Summary> {
    options.validate()?;
    ensure!(
        library.premium || options.quality == "128",
        "This quality requires iBroadcast Premium. Choose MP3 128 kbps."
    );
    let cursor = AtomicUsize::new(0);
    let summary = Mutex::new(Summary::default());
    let mut parent = Summary::default();
    let workers = options.workers.min(jobs.len());
    thread::scope(|scope| {
        for _ in 0..workers {
            let shared = &summary;
            let cursor = &cursor;
            let options = &options;
            let emit = &emit;
            scope.spawn(move || {
                while !stop.is_cancelled() {
                    let i = cursor.fetch_add(1, Ordering::Relaxed);
                    let Some(job) = jobs.get(i) else { break };
                    emit(Event::Started(i, job.relative.clone()));
                    let result = transfer(account, library, job, options, stop, &|progress| {
                        emit(Event::Progress(i, progress))
                    });
                    let mut s = shared.lock().expect("summary mutex");
                    let result = match result {
                        Ok(value) => value,
                        Err(err) => {
                            let state = if stop.is_cancelled() {
                                State::Stopped
                            } else {
                                State::Failed
                            };
                            if state == State::Failed
                                && (err.is::<Fatal>()
                                    || matches!(
                                        err.downcast_ref::<HttpError>(),
                                        Some(HttpError::Tls)
                                    ))
                            {
                                s.fatal.get_or_insert_with(|| err.to_string());
                                stop.cancel();
                            }
                            Finished {
                                state,
                                bytes: 0,
                                note: err.to_string(),
                            }
                        }
                    };
                    match result.state {
                        State::Saved => {
                            s.saved += 1;
                            s.bytes += result.bytes;
                        }
                        State::Skipped => s.skipped += 1,
                        State::Failed => s.failed += 1,
                        State::Stopped => s.stopped += 1,
                    }
                    drop(s);
                    emit(Event::Finished(i, job.relative.clone(), result));
                }
            });
        }
    });
    std::mem::swap(&mut parent, &mut *summary.lock().expect("summary mutex"));
    parent.stopped = jobs
        .len()
        .saturating_sub(parent.saved + parent.skipped + parent.failed);
    Ok(parent)
}

#[derive(Clone, PartialEq, Eq, Deserialize, Serialize)]
struct Identity {
    version: u32,
    user_id: String,
    track_id: u64,
    quality: String,
    source: String,
    #[serde(default = "stream")]
    route: String,
}
fn stream() -> String {
    "stream".into()
}
impl Identity {
    fn matches(&self, old: &Identity) -> bool {
        old.version == 1
            && self.user_id == old.user_id
            && self.track_id == old.track_id
            && self.quality == old.quality
    }
    fn partial(&self, old: &Identity) -> bool {
        self.matches(old) && self.source == old.source
    }
}
#[derive(Deserialize, Serialize)]
struct Manifest {
    version: u32,
    files: BTreeMap<String, Value>,
    partials: BTreeMap<String, Value>,
    #[serde(flatten)]
    extra: BTreeMap<String, Value>,
}
impl Default for Manifest {
    fn default() -> Self {
        Self {
            version: 1,
            files: BTreeMap::new(),
            partials: BTreeMap::new(),
            extra: BTreeMap::new(),
        }
    }
}
#[derive(Clone, Copy)]
enum Section {
    Final,
    Part,
}
impl Manifest {
    fn section(&mut self, section: Section) -> &mut BTreeMap<String, Value> {
        match section {
            Section::Final => &mut self.files,
            Section::Part => &mut self.partials,
        }
    }
}
struct Records<'a> {
    target: &'a Path,
    locks: &'a Path,
    stop: &'a CancellationToken,
}
impl<'a> Records<'a> {
    fn edit<T>(&self, action: impl FnOnce(&mut Manifest, &str) -> Result<(T, bool)>) -> Result<T> {
        let folder = self.target.parent().context("File has no parent folder.")?;
        let name = self
            .target
            .file_name()
            .and_then(|p| p.to_str())
            .context("The target filename is invalid.")?;
        let path = folder.join(MANIFEST);
        let _lock = disk(storage::target_lock(
            &self.locks.join("manifests"),
            &path,
            self.stop,
        ))?;
        disk(storage::exists(&path).map_err(Into::into))?;
        let mut data = storage::read_json::<Manifest>(&path)
            .map_err(|_| {
                anyhow!(
                    "Invalid download records in {}. The manifest was preserved.",
                    folder.display()
                )
            })?
            .unwrap_or_default();
        ensure!(
            data.version == 1
                && data
                    .files
                    .values()
                    .chain(data.partials.values())
                    .all(Value::is_object),
            "Unknown or damaged download records in {}. The manifest was preserved.",
            folder.display()
        );
        let mut imported = Vec::new();
        for possible in [
            side(self.target, ".ibfetch.json")?,
            side(self.target, ".part.ibfetch.json")?,
        ] {
            if !disk(storage::exists(&possible).map_err(Into::into))? {
                continue;
            }
            let bytes = disk(fs::read(&possible).map_err(Into::into))?;
            let Ok(v) = serde_json::from_slice::<Value>(&bytes) else {
                continue;
            };
            let Some(id) = identity(&v) else { continue };
            if id.version != 1 || id.track_id == 0 || !QUALITIES.contains(&id.quality.as_str()) {
                continue;
            }
            let filename = possible.file_name().unwrap().to_string_lossy();
            let key = filename.strip_suffix(".ibfetch.json").unwrap();
            let is_part = key.ends_with(".part") && (v["completed"] == true || v["size"].is_null());
            if (!is_part || v["completed"] == true) && !valid_receipt(&v) {
                continue;
            }
            let (section, key) = if is_part {
                (Section::Part, key.trim_end_matches(".part"))
            } else {
                (Section::Final, key)
            };
            let map = data.section(section);
            ensure!(
                map.get(key).is_none_or(|old| *old == v),
                "Conflicting legacy download record for {name}. Both records were kept."
            );
            if key == name {
                map.insert(key.into(), v);
                imported.push((possible, bytes));
            }
        }
        let (result, changed) = action(&mut data, name)?;
        if changed || !imported.is_empty() {
            if data.files.is_empty() && data.partials.is_empty() && data.extra.is_empty() {
                disk(storage::remove_file(&path).map_err(Into::into))?;
            } else {
                disk(storage::write_json(&path, &data))?;
                storage::hide_manifest(&path);
            }
            for (legacy, bytes) in imported {
                if fs::read(&legacy).is_ok_and(|s| s == bytes) {
                    disk(storage::remove_file(&legacy).map_err(Into::into))?;
                }
            }
        }
        Ok(result)
    }
    fn read(&self) -> Result<(Value, Value)> {
        self.edit(|m, n| {
            Ok((
                (
                    m.files.get(n).cloned().unwrap_or_default(),
                    m.partials.get(n).cloned().unwrap_or_default(),
                ),
                false,
            ))
        })
    }
    fn put(&self, section: Section, val: Value) -> Result<()> {
        self.edit(|m, n| {
            m.section(section).insert(n.into(), val);
            Ok(((), true))
        })
    }
    fn remove(&self, section: Section) -> Result<()> {
        self.edit(|m, n| Ok(((), m.section(section).remove(n).is_some())))
    }
    fn complete(&self, receipt: &Value, target: storage::Baseline) -> Result<()> {
        let mut record = receipt.clone();
        if let Some(map) = record.as_object_mut() {
            map.remove("completed");
        }
        record["size"] = json!(target.size);
        record["mtime_ns"] = json!(target.mtime_ns);
        if record["adopted"].is_null() {
            record["adopted"] = json!(false);
        }
        self.edit(|m, n| {
            m.partials.remove(n);
            m.files.insert(n.into(), record);
            Ok(((), true))
        })
    }
}
fn identity(v: &Value) -> Option<Identity> {
    serde_json::from_value(v["identity"].clone()).ok()
}
fn same(id: &Identity, v: &Value) -> bool {
    identity(v).is_some_and(|old| id.matches(&old))
}
fn valid_receipt(v: &Value) -> bool {
    v["size"].as_u64().is_some_and(|size| size > 0)
        && v["sha256"].as_str().is_some_and(|s| {
            s.len() == 64
                && s.bytes()
                    .all(|x| x.is_ascii_digit() || (b'a'..=b'f').contains(&x))
        })
}
fn matches_stats(record: &Value, s: storage::Baseline) -> bool {
    record["size"].as_u64() == Some(s.size) && record["mtime_ns"].as_i64() == Some(s.mtime_ns)
}
fn note(v: &Value) -> &str {
    v["note"].as_str().unwrap_or_default()
}
fn notes(first: &str, next: &str) -> String {
    [first, next]
        .into_iter()
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}
fn side(path: &Path, suffix: &str) -> Result<PathBuf> {
    let mut name = path.file_name().context("Missing filename.")?.to_owned();
    name.push(suffix);
    Ok(path.with_file_name(name))
}
fn done_receipt(id: &Identity, size: u64, digest: &str, note: &str) -> Value {
    let mut value = json!({"identity":id,"completed":true,"size":size,"sha256":digest});
    if !note.is_empty() {
        value["note"] = json!(note);
    }
    value
}
fn existing(
    records: &Records,
    target: &Path,
    part: &Path,
    id: &Identity,
    options: &Options,
) -> Result<Option<Finished>> {
    if !disk(storage::exists(target).map_err(Into::into))? {
        return Ok(None);
    }
    let (saved, pending) = records.read()?;
    let initial = match storage::baseline(target) {
        Ok(stats) if stats.size > 0 => stats,
        _ => {
            return Ok(Some(Finished::skipped(
                "Unverified: existing target is empty or is not a regular file. Preserved.",
            )));
        }
    };
    if !saved["identity"].is_null() && !same(id, &saved) {
        return Ok(Some(Finished::skipped(
            "Unverified: existing file belongs to another track, account or quality. Preserved.",
        )));
    }
    let current = same(id, &saved) && matches_stats(&saved, initial) && valid_receipt(&saved);
    if pending["completed"] == true
        && same(id, &pending)
        && valid_receipt(&pending)
        && !disk(storage::exists(part).map_err(Into::into))?
        && pending["size"].as_u64() == Some(initial.size)
    {
        if current
            && saved["sha256"] == pending["sha256"]
            && saved["identity"] == pending["identity"]
            && note(&saved) == note(&pending)
        {
            records.remove(Section::Part)?;
        } else if pending["sha256"] == disk(storage::file_hash(target, records.stop))?
            && storage::baseline(target)? == initial
        {
            records.complete(&pending, initial)?;
            return Ok(Some(Finished::skipped(notes(
                "Recovered an interrupted completion.",
                note(&pending),
            ))));
        }
    }
    if current && !options.verify {
        return Ok(Some(Finished::skipped(note(&saved))));
    }
    if same(id, &saved) && valid_receipt(&saved) && options.verify {
        let actual = disk(storage::file_hash(target, records.stop))?;
        if saved["sha256"] == actual && storage::baseline(target)? == initial {
            let mut verified = saved.clone();
            verified["size"] = json!(initial.size);
            verified["mtime_ns"] = json!(initial.mtime_ns);
            records.put(Section::Final, verified)?;
            return Ok(Some(Finished::skipped(note(&saved))));
        }
        return Ok(Some(Finished::skipped(
            "Modified: checksum or file size changed. Preserved.",
        )));
    }
    if options.adopt && saved["identity"].is_null() && pending.is_null() {
        let actual = disk(storage::file_hash(target, records.stop))?;
        ensure!(
            storage::baseline(target)? == initial,
            "File changed while being checked. Preserved."
        );
        let mut receipt = done_receipt(id, initial.size, &actual, "");
        receipt["adopted"] = json!(true);
        records.complete(&receipt, initial)?;
        return Ok(Some(Finished::skipped(
            "Adopted the existing file as a local baseline.",
        )));
    }
    Ok(Some(Finished::skipped(if same(id, &saved) {
        "Modified: existing file changed locally. Preserved."
    } else {
        "Unverified: existing file preserved. Use --adopt-existing to trust its current contents."
    })))
}
fn prepare_dirs(target: &Path, root: &Path) -> Result<()> {
    let dest = target.parent().context("Invalid target folder.")?;
    ensure!(
        dest.starts_with(root),
        "The target is outside the selected folder."
    );
    disk(fs::create_dir_all(root).map_err(Into::into))?;
    // Never follow a link from inside the destination when traversing a layout.
    let mut folder = root.to_owned();
    for component in dest.strip_prefix(root)?.components() {
        let Component::Normal(name) = component else {
            bail!("Invalid folder component.");
        };
        folder.push(name);
        match fs::create_dir(&folder) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(e) => return disk(Err(e.into())),
        }
        ensure!(
            fs::symlink_metadata(&folder)?.is_dir(),
            "A target folder is not a regular directory."
        );
    }
    Ok(())
}
pub fn transfer(
    account: &Account,
    library: &Library,
    job: &Job,
    opts: &Options,
    stop: &CancellationToken,
    progress: &dyn Fn(Progress),
) -> Result<Finished> {
    ensure!(
        job.relative
            .components()
            .all(|c| matches!(c, Component::Normal(_))),
        "Invalid relative path."
    );
    let target = opts.out.join(&job.relative);
    let locks = account.home().join("locks");
    let _lock = disk(storage::target_lock(&locks, &target, stop))?;
    let id = Identity {
        version: 1,
        user_id: library.owner.clone(),
        track_id: job.track_id,
        quality: opts.quality.clone(),
        source: job.source_path.clone(),
        route: opts.source.name().into(),
    };
    let part = side(&target, ".part")?;
    let records = Records {
        target: &target,
        locks: &locks,
        stop,
    };
    prepare_dirs(&target, &opts.out)?;
    if let Some(work) = &opts.work {
        let root = work.join(&storage::path_digest(&opts.out)?[..16]);
        let local = root.join(&job.relative);
        let local_part = side(&local, ".part")?;
        let lr = Records {
            target: &local,
            locks: &locks,
            stop,
        };
        prepare_dirs(&local, &root)?;
        if let Some(mut outcome) = existing(&records, &target, &part, &id, opts)? {
            if disk(storage::exists(&local).map_err(Into::into))? {
                let (saved, _) = records.read()?;
                let (source, _) = lr.read()?;
                if same(&id, &saved)
                    && same(&id, &source)
                    && saved["sha256"] == source["sha256"]
                    && valid_receipt(&source)
                    && matches_stats(&saved, storage::baseline(&target)?)
                {
                    outcome.note = notes(&outcome.note, &remove_unchanged(&local, &lr, &source)?);
                } else {
                    outcome.note = notes(&outcome.note, "The work-folder copy was preserved.");
                }
            }
            return Ok(outcome);
        }
        ensure!(
            !disk(storage::exists(&part).map_err(Into::into))?,
            "A partial exists at the output path. Finish it without a work folder first."
        );
        let work_options = Options {
            adopt: false,
            ..opts.clone()
        };
        let result = if let Some(value) = existing(&lr, &local, &local_part, &id, &work_options)? {
            value
        } else {
            retrieve(account, library, job, &lr, &local_part, &id, progress)?
        };
        let (record, _) = lr.read()?;
        ensure!(
            same(&id, &record) && valid_receipt(&record),
            "The work folder contains an unverified file. Preserved."
        );
        let source = storage::baseline(&local)?;
        ensure!(
            matches_stats(&record, source),
            "The work-folder copy changed. Preserved."
        );
        if result.state == State::Skipped && opts.verify {
            ensure!(
                record["sha256"] == disk(storage::file_hash(&local, stop))?,
                "The work-folder copy failed verification. Preserved."
            );
        }
        let folder = target.parent().context("Invalid output folder.")?;
        let mut stage = disk(
            tempfile::Builder::new()
                .prefix(".ibfetch-move-")
                .tempfile_in(folder)
                .map_err(Into::into),
        )?;
        let mut input = disk(File::open(&local).map_err(Into::into))?;
        let mut hash = Sha256::new();
        let (size, digest) = copy_local(&mut input, stage.as_file_mut(), &mut hash, stop)?;
        ensure!(
            size == source.size
                && record["sha256"] == digest
                && storage::baseline(&local)? == source,
            "The work-folder copy changed during the move. Both records were preserved."
        );
        disk(stage.as_file().sync_all().map_err(Into::into))?;
        let mut receipt = record.clone();
        receipt["completed"] = json!(true);
        records.put(Section::Part, receipt.clone())?;
        cancel(stop)?;
        // Close the staged handle before the platform's no-replace rename.
        let staged = stage.into_temp_path();
        disk(storage::publish_audio(&staged, &target))?;
        let renamed = storage::baseline(&target)?;
        ensure!(renamed.size == size, "The moved file changed. Preserved.");
        records.complete(&receipt, renamed)?;
        let cleanup = if storage::baseline(&local)? == source {
            disk(storage::remove_file(&local).map_err(Into::into))?;
            lr.remove(Section::Final)?;
            String::new()
        } else {
            "The changed work-folder copy was preserved.".into()
        };
        return Ok(Finished::saved(size, &notes(note(&record), &cleanup)));
    }
    if let Some(value) = existing(&records, &target, &part, &id, opts)? {
        return Ok(value);
    }
    retrieve(account, library, job, &records, &part, &id, progress)
}
fn remove_unchanged(local: &Path, records: &Records, record: &Value) -> Result<String> {
    let initial = storage::baseline(local)?;
    if !matches_stats(record, initial)
        || record["sha256"] != disk(storage::file_hash(local, records.stop))?
        || storage::baseline(local)? != initial
    {
        return Ok("The changed work-folder copy was preserved.".into());
    }
    disk(storage::remove_file(local).map_err(Into::into))?;
    records.remove(Section::Final)?;
    Ok(String::new())
}
fn copy_local(
    input: &mut File,
    output: &mut File,
    hash: &mut Sha256,
    stop: &CancellationToken,
) -> Result<(u64, String)> {
    let mut len = 0;
    let mut buffer = vec![0; CHUNK];
    loop {
        cancel(stop)?;
        let n = disk(input.read(&mut buffer).map_err(Into::into))?;
        if n == 0 {
            return Ok((len, format!("{:x}", hash.clone().finalize())));
        }
        disk(output.write_all(&buffer[..n]).map_err(Into::into))?;
        hash.update(&buffer[..n]);
        len += n as u64;
    }
}
fn finish(
    records: &Records,
    part: &Path,
    id: &Identity,
    hash: Sha256,
    n: u64,
    info: &str,
) -> Result<Finished> {
    cancel(records.stop)?;
    ensure!(n > 0, "iBroadcast returned an empty audio file.");
    let digest = format!("{:x}", hash.finalize());
    let receipt = done_receipt(id, n, &digest, info);
    records.put(Section::Part, receipt.clone())?;
    disk(storage::publish_audio(part, records.target))?;
    let after = storage::baseline(records.target)?;
    ensure!(
        after.size == n,
        "File changed during completion. Preserved."
    );
    records.complete(&receipt, after)?;
    Ok(Finished::saved(n, info))
}
fn retryable(err: &anyhow::Error) -> bool {
    matches!(err.downcast_ref::<HttpError>(), Some(HttpError::Network))
        || err.downcast_ref::<ZipError>().is_some_and(|e| e.retry)
        || err.is::<Retry>()
}
struct ZipError {
    message: &'static str,
    retry: bool,
}
impl std::fmt::Debug for ZipError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.message)
    }
}
impl std::fmt::Display for ZipError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.message)
    }
}
impl std::error::Error for ZipError {}
fn invalid_zip(message: &'static str, retry: bool) -> anyhow::Error {
    ZipError { message, retry }.into()
}
#[derive(Debug)]
pub(crate) struct Retry(pub &'static str);
impl std::fmt::Display for Retry {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.0)
    }
}
impl std::error::Error for Retry {}
fn retrieve(
    account: &Account,
    lib: &Library,
    job: &Job,
    recs: &Records,
    part: &Path,
    id: &Identity,
    progress: &dyn Fn(Progress),
) -> Result<Finished> {
    let stop = recs.stop;
    let use_service = id.route == "download";
    if disk(storage::exists(part).map_err(Into::into))? {
        let (_, old) = recs.read()?;
        let old_id = identity(&old)
            .filter(|previous| id.partial(previous))
            .context("Partial file has no matching record. It was preserved.")?;
        if use_service || old_id.route != "stream" {
            disk(storage::remove_file(part).map_err(Into::into))?;
            recs.remove(Section::Part)?;
        }
    }
    let mut id = id.clone();
    let mut fallback = String::new();
    if use_service {
        let mut failure = String::new();
        for (attempt, wait) in [0., 3., 6.].into_iter().enumerate() {
            delay(stop, wait)?;
            let result = (|| -> Result<(Sha256, u64)> {
                let mut response = account.original(&lib.owner, job.track_id, stop)?;
                if response.status().as_u16() == 429 || response.status().is_server_error() {
                    return Err(Retry("The download service is temporarily unavailable.").into());
                }
                require_audio(&response)?;
                recs.put(Section::Part, json!({"identity":id}))?;
                let mut file = disk(File::create(part).map_err(Into::into))?;
                let mut hash = Sha256::new();
                let ext = recs
                    .target
                    .extension()
                    .and_then(|s| s.to_str())
                    .unwrap_or("");
                let n = copy_archive(
                    || {
                        Ok(account
                            .http()
                            .chunk(stop, &mut response)?
                            .map(|chunk| chunk.as_ref().to_vec()))
                    },
                    &mut file,
                    &mut hash,
                    ext,
                    stop,
                    progress,
                )?;
                disk(file.sync_all().map_err(Into::into))?;
                Ok((hash, n))
            })();
            match result {
                Ok((hash, n)) => return finish(recs, part, &id, hash, n, ""),
                Err(e) => {
                    cancel(stop)?;
                    if e.is::<Fatal>()
                        || matches!(e.downcast_ref::<HttpError>(), Some(HttpError::Tls))
                    {
                        return Err(e);
                    }
                    failure = e.to_string();
                    if attempt == 2 || !retryable(&e) {
                        break;
                    }
                }
            }
        }
        fallback = format!(
            "Fallback: {failure} Saved the file as uploaded, without iBroadcast's tag edits."
        );
        id.route = "stream".into();
        disk(storage::remove_file(part).map_err(Into::into))?;
        recs.remove(Section::Part)?;
    }
    let path = job
        .stream
        .as_deref()
        .context("No stream path in the library. Refresh the library.")?;
    let mut reason = String::new();
    let mut saved_hash: Option<(u64, Sha256)> = None;
    for (attempt, wait) in [0., 3., 6., 9.].into_iter().enumerate() {
        delay(stop, wait)?;
        let result = (|| -> Result<(Sha256, u64)> {
            let offset = if disk(storage::exists(part).map_err(Into::into))? {
                storage::baseline(part)?.size
            } else {
                0
            };
            let mut r =
                account.stream(&lib.owner, &lib.expires, job.track_id, path, offset, stop)?;
            let code = r.status().as_u16();
            if code == 429 || code >= 500 {
                return Err(Retry("The streaming service is temporarily unavailable.").into());
            }
            if code == 416 && offset > 0 {
                let (_, old) = recs.read()?;
                let total = header(r.headers(), CONTENT_RANGE)
                    .and_then(|s| s.strip_prefix("bytes */"))
                    .and_then(|s| s.parse().ok());
                ensure!(
                    total == Some(offset),
                    "The server did not confirm the partial file is complete."
                );
                validate_resume(&old, r.headers(), offset)?;
                let hash = if let Some((len, hash)) = &saved_hash
                    && *len == offset
                {
                    hash.clone()
                } else {
                    disk(storage::hasher(part, stop))?
                };
                return Ok((hash, offset));
            }
            require_audio(&r)?;
            ensure!(
                matches!(code, 200 | 206),
                "Unexpected HTTP response while downloading."
            );
            let mut length = content_length(r.headers())?;
            let mut total = length;
            let append = code == 206 && offset > 0;
            let previous = if append { offset } else { 0 };
            if code == 206 {
                let (start, end, size) = parse_range(
                    header(r.headers(), CONTENT_RANGE).context("Missing byte range.")?,
                )?;
                ensure!(
                    start == offset && end >= start && end < size,
                    "Unexpected byte range. The partial file was preserved."
                );
                ensure!(
                    length.is_none_or(|n| n == end - start + 1),
                    "Incorrect byte range length."
                );
                if append {
                    validate_resume(&recs.read()?.1, r.headers(), size)?;
                }
                length = Some(end - start + 1);
                total = Some(size);
            }
            let mut hash = if append {
                if let Some((len, hash)) = &saved_hash
                    && *len == previous
                {
                    hash.clone()
                } else {
                    disk(storage::hasher(part, stop))?
                }
            } else {
                Sha256::new()
            };
            let mut file = disk(
                OpenOptions::new()
                    .write(true)
                    .create(true)
                    .append(append)
                    .truncate(!append)
                    .open(part)
                    .map_err(Into::into),
            )?;
            let mut row = json!({"identity":id,"total":total.unwrap_or(0)});
            if let Some(etag) = header(r.headers(), ETAG).filter(|v| !v.starts_with("W/")) {
                row["etag"] = json!(etag);
            } else if let Some(modified) = header(r.headers(), LAST_MODIFIED) {
                row["last_modified"] = json!(modified);
            }
            recs.put(Section::Part, row)?;
            let mut received = 0;
            loop {
                match account.http().chunk(stop, &mut r) {
                    Ok(Some(chunk)) => {
                        let chunk: &[u8] = chunk.as_ref();
                        if length.is_some_and(|n| received + chunk.len() as u64 > n) {
                            // Continue from the exact previous boundary after a malformed response.
                            disk(file.set_len(previous).map_err(Into::into))?;
                            saved_hash = None;
                            bail!("Received more data than the server's advertised byte range.");
                        }
                        disk(file.write_all(chunk).map_err(Into::into))?;
                        received += chunk.len() as u64;
                        hash.update(chunk);
                        progress(Progress {
                            received: previous + received,
                            resumed: previous,
                            chunk_bytes: chunk.len() as u64,
                            total,
                        });
                    }
                    Ok(None) => break,
                    Err(e) => {
                        saved_hash = Some((previous + received, hash));
                        return Err(e);
                    }
                }
            }
            if length.is_some_and(|n| n != received)
                || total.is_some_and(|n| previous + received < n)
            {
                saved_hash = Some((previous + received, hash));
                return Err(Retry("The server returned only part of the file.").into());
            }
            disk(file.sync_all().map_err(Into::into))?;
            Ok((hash, previous + received))
        })();
        match result {
            Ok((hash, n)) => return finish(recs, part, &id, hash, n, &fallback),
            Err(e) if attempt < 3 && retryable(&e) => reason = e.to_string(),
            Err(e) => return Err(e),
        }
    }
    bail!("{reason}")
}
fn require_audio(r: &Response) -> Result<()> {
    let code = r.status().as_u16();
    if matches!(code, 401 | 403) {
        return Err(Fatal("iBroadcast rejected the session. Sign in again.".into()).into());
    }
    ensure!(r.status().is_success(), "iBroadcast returned HTTP {code}.");
    let mime = header(r.headers(), CONTENT_TYPE)
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_lowercase();
    ensure!(
        !(mime.starts_with("text/")
            || mime.ends_with("+json")
            || matches!(
                mime.as_str(),
                "application/json" | "application/xml" | "application/xhtml+xml"
            )),
        "iBroadcast returned a non-audio response."
    );
    Ok(())
}
fn header(headers: &HeaderMap, key: reqwest::header::HeaderName) -> Option<&str> {
    headers.get(key)?.to_str().ok()
}
fn content_length(headers: &HeaderMap) -> Result<Option<u64>> {
    if let Some(raw) = headers.get(CONTENT_LENGTH) {
        return Ok(Some(
            raw.to_str()?.parse().context("Invalid Content-Length.")?,
        ));
    }
    Ok(None)
}
fn parse_range(value: &str) -> Result<(u64, u64, u64)> {
    let (range, total) = value
        .strip_prefix("bytes ")
        .and_then(|x| x.split_once('/'))
        .context("Invalid Content-Range.")?;
    let (first, last) = range.split_once('-').context("Invalid Content-Range.")?;
    Ok((first.parse()?, last.parse()?, total.parse()?))
}
fn validate_resume(old: &Value, headers: &HeaderMap, total: u64) -> Result<()> {
    ensure!(
        old["total"].as_u64() == Some(total) && total > 0,
        "The original file size is unknown or changed. The partial file was preserved."
    );
    for (key, h) in [("etag", ETAG), ("last_modified", LAST_MODIFIED)] {
        if let Some(old) = old[key].as_str().filter(|v| !v.is_empty()) {
            ensure!(
                header(headers, h) == Some(old),
                "The remote file changed or no longer reports its validator. Partial preserved."
            );
        }
    }
    Ok(())
}

fn u16_at(data: &[u8], at: usize) -> usize {
    u16::from_le_bytes([data[at], data[at + 1]]) as usize
}
fn u32_at(data: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(data[at..at + 4].try_into().unwrap())
}
fn alias(ext: &str) -> &str {
    match ext {
        "aif" => "aiff",
        "wave" => "wav",
        "oga" => "ogg",
        "mp4" => "m4a",
        e => e,
    }
}
fn need(
    buf: &mut Vec<u8>,
    bytes: usize,
    read: &mut impl FnMut() -> Result<Option<Vec<u8>>>,
    stop: &CancellationToken,
) -> Result<()> {
    while buf.len() < bytes {
        cancel(stop)?;
        let chunk =
            read()?.ok_or_else(|| invalid_zip("The download service's ZIP ended early.", true))?;
        buf.extend(chunk);
    }
    Ok(())
}
fn copy_archive(
    mut read: impl FnMut() -> Result<Option<Vec<u8>>>,
    output: &mut impl Write,
    hash: &mut Sha256,
    ext: &str,
    stop: &CancellationToken,
    progress: &dyn Fn(Progress),
) -> Result<u64> {
    let mut data = Vec::new();
    need(&mut data, 30, &mut read, stop)?;
    if !data.starts_with(b"PK\x03\x04") {
        return Err(invalid_zip(
            "The service did not return a ZIP archive.",
            false,
        ));
    }
    let flags = u16_at(&data, 6);
    if flags & 1 != 0 || u16_at(&data, 8) != 0 {
        return Err(invalid_zip(
            "The service returned an encrypted or compressed ZIP entry.",
            false,
        ));
    }
    let local_crc = u32_at(&data, 14);
    let local_length = u32_at(&data, 18);
    let local_uncompressed = u32_at(&data, 22);
    let name_len = u16_at(&data, 26);
    let offset = 30 + name_len + u16_at(&data, 28);
    need(&mut data, offset, &mut read, stop)?;
    let name = data[30..30 + name_len].to_vec();
    let filename = String::from_utf8_lossy(&name);
    let ext_zip = filename
        .rsplit(['/', '\\'])
        .next()
        .and_then(|n| n.rsplit_once('.'))
        .map(|(_, e)| e.to_ascii_lowercase())
        .unwrap_or_default();
    if alias(&ext_zip) != alias(&ext.to_ascii_lowercase()) {
        return Err(invalid_zip(
            "The ZIP entry has a different audio format.",
            false,
        ));
    }
    data.drain(..offset);
    let mut out = 0u64;
    let mut crc = crc32fast::Hasher::new();
    let mut write = |bytes: &[u8]| -> Result<()> {
        cancel(stop)?;
        if !bytes.is_empty() {
            disk(output.write_all(bytes).map_err(Into::into))?;
            hash.update(bytes);
            crc.update(bytes);
            out += bytes.len() as u64;
            progress(Progress {
                received: out,
                resumed: 0,
                chunk_bytes: bytes.len() as u64,
                total: None,
            });
        }
        Ok(())
    };
    loop {
        cancel(stop)?;
        if data.len() > ZIP_TAIL {
            let first = data.len() - ZIP_TAIL;
            write(&data[..first])?;
            data.drain(..first);
        }
        match read()? {
            Some(bytes) => data.extend(bytes),
            None => break,
        }
    }
    drop(write);
    if !data.ends_with(b"EOF\n") {
        return Err(invalid_zip(
            "The download service stopped before its end marker.",
            true,
        ));
    }
    data.truncate(data.len() - 4);
    let Some(end) = data.windows(4).rposition(|s| s == b"PK\x05\x06") else {
        return Err(invalid_zip(
            "The ZIP archive has no central directory.",
            true,
        ));
    };
    if data.len() - end < 22 {
        return Err(invalid_zip("Incomplete ZIP directory.", true));
    }
    if end + 22 + u16_at(&data, end + 20) != data.len()
        || u16_at(&data, end + 4) != 0
        || u16_at(&data, end + 6) != 0
        || u16_at(&data, end + 8) != 1
        || u16_at(&data, end + 10) != 1
    {
        return Err(invalid_zip(
            "The service returned an unexpected ZIP layout.",
            false,
        ));
    }
    let dsize = u32_at(&data, end + 12) as usize;
    let dpos = u32_at(&data, end + 16) as u64;
    let Some(dir) = dpos
        .checked_sub(offset as u64 + out)
        .and_then(|v| usize::try_from(v).ok())
    else {
        return Err(invalid_zip(
            "The ZIP directory points outside the archive.",
            false,
        ));
    };
    if dsize < 46
        || dir.checked_add(dsize) != Some(end)
        || &data[dir..dir + 4] != b"PK\x01\x02"
        || u16_at(&data, dir + 10) != 0
        || u16_at(&data, dir + 8) & 1 != 0
        || u32_at(&data, dir + 42) != 0
    {
        return Err(invalid_zip(
            "The ZIP directory does not match its file.",
            false,
        ));
    }
    let expected_crc = u32_at(&data, dir + 16);
    let compressed = u32_at(&data, dir + 20);
    let original = u32_at(&data, dir + 24);
    let n = u16_at(&data, dir + 28);
    if compressed != original
        || n != name.len()
        || dir + 46 + n > end
        || data[dir + 46..dir + 46 + n] != name
    {
        return Err(invalid_zip(
            "The ZIP directory does not match its file.",
            false,
        ));
    }
    let mut data_end = dir;
    if flags & 8 != 0 {
        let descriptor = [16, 12].into_iter().find(|&n| {
            dir >= n
                && (n == 12 || &data[dir - 16..dir - 12] == b"PK\x07\x08")
                && [
                    u32_at(&data, dir - 12),
                    u32_at(&data, dir - 8),
                    u32_at(&data, dir - 4),
                ] == [expected_crc, compressed, original]
        });
        data_end -= descriptor
            .ok_or_else(|| invalid_zip("The ZIP data descriptor does not match.", true))?;
    } else if [local_crc, local_length, local_uncompressed] != [expected_crc, compressed, original]
    {
        return Err(invalid_zip(
            "The ZIP header does not match its directory.",
            true,
        ));
    }
    let rest = &data[..data_end];
    out += rest.len() as u64;
    crc.update(rest);
    if out != u64::from(original) || out == 0 || crc.finalize() != expected_crc {
        return Err(invalid_zip(
            "The ZIP failed its size or CRC-32 check.",
            true,
        ));
    }
    disk(output.write_all(rest).map_err(Into::into))?;
    hash.update(rest);
    progress(Progress {
        received: out,
        resumed: 0,
        chunk_bytes: rest.len() as u64,
        total: Some(out),
    });
    Ok(out)
}
