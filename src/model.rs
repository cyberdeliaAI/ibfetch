use crate::{
    account::{Account, Approval, Provider},
    catalog::{DEFAULT_LAYOUT, Filters, Group, Layout, Library},
    downloads::{self, Event, Options, Source, State},
    storage, update,
};
use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    path::PathBuf,
    sync::{Arc, Mutex, mpsc},
    time::{Duration, Instant},
};
use tokio_util::sync::CancellationToken;

#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub out: String,
    pub work: String,
    pub layout: String,
    pub quality: String,
    pub source: String,
    pub jobs: usize,
    pub appearance: String,
    pub palette: String,
    pub auto_updates: bool,
    #[serde(flatten)]
    other: BTreeMap<String, Value>,
}
impl Default for Settings {
    fn default() -> Self {
        Self {
            out: storage::user_home()
                .unwrap_or_default()
                .join("Music/iBroadcast")
                .to_string_lossy()
                .into(),
            work: String::new(),
            layout: DEFAULT_LAYOUT.into(),
            quality: "orig".into(),
            source: "download".into(),
            jobs: 3,
            appearance: "auto".into(),
            palette: "forest".into(),
            auto_updates: true,
            other: BTreeMap::new(),
        }
    }
}
impl Settings {
    pub fn options(&self, verify: bool, adopt: bool) -> Result<(Layout, Options)> {
        ensure!(!self.out.trim().is_empty(), "Choose the download folder.");
        ensure!(
            matches!(self.source.as_str(), "download" | "stream"),
            "Invalid original file source."
        );
        let source = if self.quality == "orig" && self.source == "download" {
            Source::Download
        } else {
            Source::Stream
        };
        let out = storage::expand(&PathBuf::from(self.out.trim()))?;
        ensure!(
            out.is_absolute(),
            "Use a full path for the download folder."
        );
        let mut opts = Options {
            out,
            work: (!self.work.trim().is_empty()).then(|| PathBuf::from(self.work.trim())),
            source,
            quality: self.quality.clone(),
            workers: self.jobs,
            verify,
            adopt,
        };
        opts.validate()?;
        Ok((Layout::new(self.layout.trim())?, opts))
    }
    pub fn loaded(home: &std::path::Path) -> Self {
        // Keep valid preferences if one field has an older or unknown type.
        let mut defaults = serde_json::to_value(Self::default()).unwrap();
        if let Ok(Some(Value::Object(stored))) = storage::read_json(&home.join("app-settings.json"))
        {
            for (k, v) in stored {
                let valid = match k.as_str() {
                    "out" | "layout" => v.as_str().is_some_and(|s| !s.trim().is_empty()),
                    "work" => v.is_string(),
                    "quality" => v.as_str().is_some_and(|s| crate::QUALITIES.contains(&s)),
                    "jobs" => v.as_u64().is_some_and(|n| (1..=8).contains(&n)),
                    "source" => v
                        .as_str()
                        .is_some_and(|s| matches!(s, "download" | "stream")),
                    "appearance" => v
                        .as_str()
                        .is_some_and(|s| matches!(s, "auto" | "light" | "dark")),
                    "palette" => v.as_str().is_some_and(|s| {
                        matches!(s, "forest" | "ocean" | "warm" | "ibroadcast" | "amber")
                    }),
                    "auto_updates" => v.is_boolean(),
                    _ => true,
                };
                if valid {
                    defaults[&k] = v;
                }
            }
        }
        serde_json::from_value(defaults).unwrap_or_default()
    }
}
#[derive(Clone, Serialize)]
pub struct Album {
    pub id: u64,
    pub artist: String,
    pub title: String,
    pub year: u64,
    pub disc: u64,
    pub tracks: Vec<u64>,
    pub formats: String,
    pub length: u64,
    pub track_lengths: BTreeMap<u64, u64>,
}
pub fn albums(library: &Library, groups: Vec<Group>) -> Vec<Album> {
    groups
        .into_iter()
        .map(|g| {
            let track_lengths: BTreeMap<_, _> = g
                .tracks
                .iter()
                .map(|&id| (id, library.duration(id)))
                .collect();
            let formats = g
                .tracks
                .iter()
                .map(|&id| library.format(id))
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect::<Vec<_>>()
                .join("/");
            Album {
                id: g.id,
                artist: library.album_artist(g.id),
                title: library.album_title(g.id),
                year: library.year(g.id, g.tracks.first().copied()),
                disc: if library.multi_disc(g.id) {
                    library.disc(g.id)
                } else {
                    0
                },
                tracks: g.tracks,
                formats,
                length: track_lengths.values().sum(),
                track_lengths,
            }
        })
        .collect()
}
#[derive(Clone)]
pub struct View {
    pub epoch: u64,
    pub revision: u64,
    pub signed_in: bool,
    pub loading: bool,
    pub signing_out: bool,
    pub login: Value,
    pub error: String,
    pub library: Option<Arc<Library>>,
    pub metadata: Arc<Value>,
    pub transfer: Option<Value>,
    pub can_retry: bool,
}
struct Login {
    cancel: CancellationToken,
    id: String,
    view: Value,
}
struct Active {
    name: PathBuf,
    received: u64,
    expected: Option<u64>,
}
const SPEED_WINDOW: Duration = Duration::from_secs(8);
struct Transfer {
    stop: CancellationToken,
    state: &'static str,
    total: usize,
    start: Instant,
    elapsed: Option<f64>,
    out: String,
    source: &'static str,
    done: usize,
    ok: usize,
    skip: usize,
    fail: usize,
    bytes: u64,
    active: BTreeMap<usize, Active>,
    // Bytes received in this run and when, for the speed over the last few seconds.
    moved: u64,
    samples: VecDeque<(Instant, u64)>,
    recent: VecDeque<Value>,
    failures: VecDeque<Value>,
    existing: VecDeque<Value>,
    notes: VecDeque<Value>,
    existing_count: usize,
    notes_count: usize,
}
impl Transfer {
    fn new(total: usize, opts: &Options) -> Self {
        Self {
            stop: CancellationToken::new(),
            state: "planning",
            total,
            start: Instant::now(),
            elapsed: None,
            out: opts.out.to_string_lossy().into(),
            source: if opts.source == Source::Stream {
                "stream"
            } else {
                "download"
            },
            done: 0,
            ok: 0,
            skip: 0,
            fail: 0,
            bytes: 0,
            active: BTreeMap::new(),
            moved: 0,
            samples: VecDeque::new(),
            recent: VecDeque::new(),
            failures: VecDeque::new(),
            existing: VecDeque::new(),
            notes: VecDeque::new(),
            existing_count: 0,
            notes_count: 0,
        }
    }
    fn busy(&self) -> bool {
        self.elapsed.is_none()
    }
    fn running(&mut self, total: usize, now: Instant) {
        self.total = total;
        self.state = "running";
        self.samples.clear();
        self.samples.push_back((now, self.moved));
    }
    fn speed(&self, now: Instant) -> f64 {
        if self.state != "running" {
            return 0.;
        }
        let recent = |at: &Instant| now.saturating_duration_since(*at) <= SPEED_WINDOW;
        let Some(&(since, base)) = self.samples.iter().find(|(at, _)| recent(at)) else {
            return 0.;
        };
        let seconds = now.saturating_duration_since(since).as_secs_f64();
        if seconds > 0.5 {
            (self.moved - base) as f64 / seconds
        } else {
            0.
        }
    }
    fn snapshot(&self, now: Instant) -> Value {
        let elapsed = self
            .elapsed
            .unwrap_or_else(|| now.saturating_duration_since(self.start).as_secs_f64());
        let active: Vec<_> = self
            .active
            .values()
            .map(|a| json!({"name":a.name,"received":a.received,"expected":a.expected}))
            .collect();
        let bytes = self.bytes + self.active.values().map(|a| a.received).sum::<u64>();
        let eta = (self.state == "running" && self.done >= 3)
            .then(|| elapsed / self.done as f64 * (self.total.saturating_sub(self.done)) as f64);
        json!({"state":self.state,"total":self.total,"done":self.done,"ok":self.ok,"skip":self.skip,"fail":self.fail,
            "bytes":bytes,"elapsed":elapsed,"speed":self.speed(now),"eta":eta,"out":self.out,"source":self.source,"active":active,
            "recent":self.recent,"failures":self.failures,"existing":self.existing,"notes":self.notes,
            "existing_count":self.existing_count,"notes_count":self.notes_count})
    }
    fn record(&mut self, e: Event, now: Instant) {
        fn last(xs: &mut VecDeque<Value>, new: Value) {
            if xs.len() == 200 {
                xs.pop_front();
            }
            xs.push_back(new);
        }
        match e {
            Event::Started(id, name) => {
                let file = Active {
                    name,
                    received: 0,
                    expected: None,
                };
                self.active.insert(id, file);
            }
            Event::Progress(id, at) => {
                if let Some(file) = self.active.get_mut(&id) {
                    self.moved += at.chunk_bytes;
                    file.received = at.received;
                    file.expected = at.total;
                    if self
                        .samples
                        .back()
                        .is_none_or(|(at, _)| now.saturating_duration_since(*at).as_millis() >= 250)
                    {
                        self.samples.push_back((now, self.moved));
                    }
                    while self.samples.len() > 2
                        && now.saturating_duration_since(self.samples[0].0) > SPEED_WINDOW
                    {
                        self.samples.pop_front();
                    }
                }
            }
            Event::Finished(id, path, result) => {
                self.active.remove(&id);
                if result.state == State::Stopped {
                    return;
                }
                let summary = json!({"name":path,"note":result.note});
                let state = match result.state {
                    State::Saved => {
                        self.ok += 1;
                        if !result.note.is_empty() {
                            self.notes_count += 1;
                            last(&mut self.notes, summary);
                        }
                        "ok"
                    }
                    State::Skipped => {
                        self.skip += 1;
                        if !result.note.is_empty() {
                            self.existing_count += 1;
                            last(&mut self.existing, summary);
                        }
                        "skip"
                    }
                    State::Failed => {
                        self.fail += 1;
                        last(&mut self.failures, summary);
                        "fail"
                    }
                    State::Stopped => unreachable!(),
                };
                self.done += 1;
                self.bytes += result.bytes;
                if self.recent.len() == 12 {
                    self.recent.pop_back();
                }
                self.recent.push_front(
                    json!({"name":path,"state":state,"bytes":result.bytes,"note":result.note}),
                );
            }
        }
    }
}
#[derive(Clone)]
struct Batch {
    items: Vec<Group>,
    settings: Settings,
    verify: bool,
}
struct Data {
    epoch: u64,
    revision: u64,
    account: Option<Arc<Account>>,
    library: Option<Arc<Library>>,
    meta: Arc<Value>,
    loading: Option<CancellationToken>,
    login: Option<Login>,
    signing_out: bool,
    closed: bool,
    error: String,
    transfer: Option<Transfer>,
    last: Option<Batch>,
}
#[derive(Clone)]
pub struct Model {
    pub provider: Provider,
    pub updates: update::Checker,
    settings: Arc<Mutex<Settings>>,
    data: Arc<Mutex<Data>>,
}
impl Model {
    pub fn new(provider: Provider) -> Self {
        let settings = Settings::loaded(provider.home());
        let updates =
            update::Checker::new(provider.home().join("updates.json"), settings.auto_updates);
        let (account, error) = match provider.load() {
            Ok(a) => (a, String::new()),
            Err(e) => (None, e.to_string()),
        };
        let this = Self {
            provider,
            updates,
            settings: Arc::new(Mutex::new(settings)),
            data: Arc::new(Mutex::new(Data {
                epoch: 0,
                revision: 0,
                account,
                library: None,
                meta: Arc::new(Value::Null),
                loading: None,
                login: None,
                signing_out: false,
                closed: false,
                error,
                transfer: None,
                last: None,
            })),
        };
        if this.view().signed_in {
            let _ = this.load(false);
        }
        this
    }
    pub fn settings(&self) -> Settings {
        self.settings.lock().unwrap().clone()
    }
    pub fn save_settings(&self, edit: impl FnOnce(&mut Settings)) -> Result<()> {
        let mut current = self.settings.lock().unwrap();
        let mut copy = current.clone();
        edit(&mut copy);
        storage::write_json(&self.provider.home().join("app-settings.json"), &copy)?;
        self.updates.enable(copy.auto_updates);
        *current = copy;
        Ok(())
    }
    pub fn view(&self) -> View {
        let d = self.data.lock().unwrap();
        View {
            epoch: d.epoch,
            revision: d.revision,
            signed_in: d.account.is_some(),
            loading: d.loading.is_some(),
            signing_out: d.signing_out,
            login: d.login.as_ref().map(|l| l.view.clone()).unwrap_or_default(),
            error: d.error.clone(),
            library: d.library.clone(),
            metadata: d.meta.clone(),
            transfer: d.transfer.as_ref().map(|t| t.snapshot(Instant::now())),
            can_retry: d.last.is_some(),
        }
    }
    pub fn meta(&self) -> Arc<Value> {
        self.data.lock().unwrap().meta.clone()
    }
    pub fn library(&self) -> Result<Arc<Library>> {
        let d = self.data.lock().unwrap();
        d.library
            .clone()
            .context("Wait until the library is ready.")
    }
    pub fn load(&self, refresh: bool) -> Result<()> {
        let (epoch, account, stop) = {
            let mut d = self.data.lock().unwrap();
            ensure!(
                !d.closed && !d.signing_out && d.loading.is_none(),
                "The library is already loading or the app is closing."
            );
            ensure!(
                !d.transfer.as_ref().is_some_and(Transfer::busy),
                "Stop the download before refreshing the library."
            );
            let account = d.account.clone().context("Sign in first.")?;
            let stop = CancellationToken::new();
            d.loading = Some(stop.clone());
            d.error.clear();
            (d.epoch, account, stop)
        };
        let this = self.clone();
        std::thread::spawn(move || {
            let result = account.library(refresh, &stop).map(|lib| {
                let meta = lib.metadata();
                (Arc::new(lib), Arc::new(meta))
            });
            let mut d = this.data.lock().unwrap();
            if d.closed || d.epoch != epoch || stop.is_cancelled() {
                return;
            }
            d.loading = None;
            match result {
                Ok((lib, meta)) => {
                    d.library = Some(lib);
                    d.meta = meta;
                    d.error.clear();
                    d.revision += 1;
                }
                Err(e) => {
                    d.error = e.to_string();
                }
            }
        });
        Ok(())
    }
    pub fn login(&self) -> Result<mpsc::Receiver<Result<Value>>> {
        let (send, receive) = mpsc::sync_channel(1);
        let (epoch, cancel, id) = {
            let mut d = self.data.lock().unwrap();
            ensure!(
                !d.closed && !d.signing_out && d.account.is_none(),
                "Sign out before signing in again."
            );
            if let Some(in_progress) = &d.login
                && in_progress.view["state"] != "error"
            {
                bail!("Sign-in is already in progress.");
            }
            d.epoch += 1;
            d.error.clear();
            let id = crate::random_hex(16)?;
            let cancel = CancellationToken::new();
            d.login = Some(Login {
                cancel: cancel.clone(),
                id: id.clone(),
                view: json!({"state":"starting","request_id":id}),
            });
            (d.epoch, cancel, id)
        };
        let this = self.clone();
        std::thread::spawn(move || {
            let result=this.provider.prepare_sign_in(None,&cancel,|approval:Approval|{
                let mut d=this.data.lock().unwrap();
                if d.epoch!=epoch||d.closed||cancel.is_cancelled(){return;}
                let value=json!({"state":"waiting","code":approval.code,"url":approval.url,"plain_url":approval.plain_url,"request_id":id});
                if let Some(login)=&mut d.login{login.view=value.clone();}
                let _=send.try_send(Ok(value));
            });
            {
                let mut d = this.data.lock().unwrap();
                if d.epoch != epoch || d.closed || cancel.is_cancelled() {
                    return;
                }
                match result.and_then(|account| {
                    account.persist_sign_in(&cancel)?;
                    Ok(account)
                }) {
                    Ok(account) => {
                        d.account = Some(account);
                        d.login = None;
                    }
                    Err(e) => {
                        let message = e.to_string();
                        if let Some(login) = &mut d.login {
                            login.view = json!({"state":"error","message":message,"request_id":id});
                        }
                        let _ = send.try_send(Err(e));
                        return;
                    }
                }
            }
            let _ = this.load(false);
        });
        Ok(receive)
    }
    pub fn cancel_login(&self, id: Option<&str>) {
        let mut d = self.data.lock().unwrap();
        if let Some(login) = &d.login
            && id.is_none_or(|id| id == login.id)
        {
            login.cancel.cancel();
            d.login = None;
            d.epoch += 1;
        }
    }
    pub fn logout(&self) -> Result<()> {
        {
            let mut d = self.data.lock().unwrap();
            ensure!(
                !d.closed && !d.signing_out,
                "The app is closing or signing out."
            );
            ensure!(
                !d.transfer.as_ref().is_some_and(Transfer::busy),
                "Stop the download before signing out."
            );
            d.signing_out = true;
            d.epoch += 1;
            if let Some(l) = d.login.take() {
                l.cancel.cancel();
            }
            if let Some(l) = d.loading.take() {
                l.cancel();
            }
            if let Some(a) = d.account.take() {
                a.retire();
            }
            d.library = None;
            d.meta = Arc::new(Value::Null);
            d.transfer = None;
            d.last = None;
            d.error.clear();
        }
        let result = self.provider.logout(&CancellationToken::new());
        let mut d = self.data.lock().unwrap();
        d.signing_out = false;
        if let Err(e) = &result {
            d.error = e.to_string();
        }
        result
    }
    pub fn search(&self, filters: &Filters, stop: &CancellationToken) -> Result<Vec<Album>> {
        let lib = self.library()?;
        Ok(albums(&lib, lib.select(filters, stop)?))
    }
    pub fn start(
        &self,
        items: Vec<Group>,
        settings: Settings,
        verify: bool,
        adopt: bool,
    ) -> Result<usize> {
        let (layout, opts) = settings.options(verify, adopt)?;
        let (epoch, account, lib, items, stop, total) = {
            let mut d = self.data.lock().unwrap();
            ensure!(
                !d.closed && !d.signing_out && d.loading.is_none(),
                "The app is busy. Try again shortly."
            );
            ensure!(
                !d.transfer.as_ref().is_some_and(Transfer::busy),
                "Another download is active."
            );
            let account = d.account.clone().context("Sign in first.")?;
            let lib = d
                .library
                .clone()
                .context("Wait until the library is ready.")?;
            ensure!(
                lib.premium || opts.quality == "128",
                "Choose MP3 128 kbps, or sign into an iBroadcast Premium account."
            );
            let total = items.iter().map(|g| g.tracks.len()).sum();
            ensure!(total > 0, "The download list is empty.");
            let transfer = Transfer::new(total, &opts);
            let stop = transfer.stop.clone();
            d.transfer = Some(transfer);
            d.last = Some(Batch {
                items: items.clone(),
                settings: settings.clone(),
                verify,
            });
            (d.epoch, account, lib, items, stop, total)
        };
        if let Err(e) = self.save_settings(|saved| {
            saved.out = settings.out;
            saved.work = settings.work;
            saved.layout = settings.layout;
            saved.source = settings.source;
            saved.quality = settings.quality;
            saved.jobs = settings.jobs;
        }) {
            let mut d = self.data.lock().unwrap();
            d.transfer = None;
            return Err(e);
        }
        let this = self.clone();
        std::thread::spawn(move || {
            let result = (|| {
                let items = lib.validate_items(&items);
                ensure!(!items.is_empty(), "The download list is empty.");
                let jobs = lib.plan(&items, &opts.quality, &layout, &stop)?;
                {
                    let mut d = this.data.lock().unwrap();
                    if d.closed || d.epoch != epoch || stop.is_cancelled() {
                        bail!("Stopped.");
                    }
                    if let Some(t) = &mut d.transfer {
                        t.running(jobs.len(), Instant::now());
                    }
                }
                downloads::run(&account, &lib, &jobs, opts, &stop, |event| {
                    let mut d = this.data.lock().unwrap();
                    if let Some(t) = &mut d.transfer {
                        t.record(event, Instant::now());
                    }
                })
            })();
            let mut d = this.data.lock().unwrap();
            if d.epoch != epoch {
                return;
            }
            if let Some(t) = &mut d.transfer {
                if let Err(error) = result
                    && !stop.is_cancelled()
                {
                    t.record(
                        Event::Finished(
                            usize::MAX,
                            PathBuf::from("Download preparation"),
                            downloads::Finished {
                                state: State::Failed,
                                bytes: 0,
                                note: error.to_string(),
                            },
                        ),
                        Instant::now(),
                    );
                }
                t.active.clear();
                t.elapsed = Some(t.start.elapsed().as_secs_f64());
                t.state = if stop.is_cancelled() {
                    "stopped"
                } else {
                    "done"
                };
            }
        });
        Ok(total)
    }
    pub fn retry(&self) -> Result<usize> {
        let last = self
            .data
            .lock()
            .unwrap()
            .last
            .clone()
            .context("There is no download to retry.")?;
        self.start(last.items, last.settings, last.verify, false)
    }
    pub fn busy(&self) -> bool {
        self.data
            .lock()
            .unwrap()
            .transfer
            .as_ref()
            .is_some_and(Transfer::busy)
    }
    pub fn cancel_download(&self) {
        if let Some(t) = &self.data.lock().unwrap().transfer {
            t.stop.cancel();
        }
    }
    pub fn close(&self) {
        let mut d = self.data.lock().unwrap();
        d.closed = true;
        if let Some(login) = &d.login {
            login.cancel.cancel();
        }
        if let Some(load) = &d.loading {
            load.cancel();
        }
        if let Some(transfer) = &d.transfer {
            transfer.stop.cancel();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn transfer() -> Transfer {
        Transfer::new(
            4,
            &Options {
                out: PathBuf::from("out"),
                work: None,
                source: Source::Stream,
                quality: "orig".into(),
                workers: 1,
                verify: false,
                adopt: false,
            },
        )
    }

    fn report(file: usize, received: u64, resumed: u64, chunk_bytes: u64) -> Event {
        let at = downloads::Progress {
            received,
            resumed,
            chunk_bytes,
            total: None,
        };
        Event::Progress(file, at)
    }

    #[test]
    fn speed_covers_the_last_seconds_and_ignores_a_resumed_offset() {
        let start = Instant::now();
        let at = |seconds: u64| start + Duration::from_secs(seconds);
        let mut t = transfer();
        t.running(4, start);
        t.record(Event::Started(0, PathBuf::from("a.flac")), start);
        // 50 MB were on disk from an earlier run; every report says so.
        for second in 1..=4 {
            t.record(
                report(0, 50_000_000 + second * 9_000_000, 50_000_000, 9_000_000),
                at(second),
            );
        }
        for second in 5..=20 {
            let bytes = 86_000_000 + (second - 4) * 1_000_000;
            t.record(report(0, bytes, 50_000_000, 1_000_000), at(second));
        }
        let view = t.snapshot(at(20));
        assert_eq!(view["bytes"], 102_000_000u64);
        assert_eq!(t.moved, 52_000_000);
        assert!((view["speed"].as_f64().unwrap() - 1_000_000.).abs() < 1.);
        assert_eq!(t.snapshot(at(29))["speed"], 0.);
        t.state = "done";
        assert_eq!(t.snapshot(at(20))["speed"], 0.);
    }

    #[test]
    fn every_received_byte_counts_once_from_the_first_block() {
        let start = Instant::now();
        let mut t = transfer();
        t.running(4, start);
        // A small file arrives in one block; the archive route then repeats its size.
        t.record(Event::Started(0, PathBuf::from("small.flac")), start);
        t.record(report(0, 4096, 0, 4096), start);
        t.record(report(0, 4096, 0, 0), start);
        assert_eq!(t.moved, 4096);
        // Four bytes were saved earlier; the first report already holds five new ones.
        t.record(Event::Started(1, PathBuf::from("resumed.flac")), start);
        t.record(report(1, 9, 4, 5), start);
        assert_eq!(t.moved, 4096 + 5);
        // A failed attempt starts again from nothing: those bytes travel twice.
        t.record(Event::Started(2, PathBuf::from("again.flac")), start);
        for (received, chunk_bytes) in [(3000, 3000), (1000, 1000), (3000, 2000)] {
            t.record(report(2, received, 0, chunk_bytes), start);
        }
        assert_eq!(t.moved, 4096 + 5 + 3000 + 1000 + 2000);
        // A retry that continues what this run saved adds only the new bytes.
        t.record(Event::Started(3, PathBuf::from("continued.flac")), start);
        t.record(report(3, 5000, 0, 5000), start);
        t.record(report(3, 6000, 5000, 1000), start);
        assert_eq!(t.moved, 4096 + 5 + 6000 + 6000);
    }

    #[test]
    fn speed_counts_the_full_response_when_a_resume_starts_over() {
        let start = Instant::now();
        let end = start + Duration::from_secs(4);
        let mut t = transfer();
        t.running(1, start);
        t.record(Event::Started(0, PathBuf::from("restart.flac")), start);
        t.record(report(0, 4096, 0, 4096), start + Duration::from_secs(1));
        t.record(report(0, 8192, 0, 8192), end);

        let view = t.snapshot(end);
        assert_eq!(view["bytes"], 8192);
        assert_eq!(view["speed"], 3072.0); // (4096 + 8192) bytes in four seconds
    }
}
