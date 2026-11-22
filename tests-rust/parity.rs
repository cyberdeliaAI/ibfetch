use ibfetch::{
    account::{Endpoints, Provider},
    catalog::{DEFAULT_LAYOUT, Filters, Layout, Library},
    downloads::{self, Options, Source, State},
    storage,
};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::Duration,
};
use tempfile::TempDir;
use tiny_http::{Header, Response, Server};
use tokio_util::sync::CancellationToken;

fn fixture() -> Value {
    json!({"library":{
        "artists":{"map":{"name":0},"1":["Björk"],"2":["The 9 Lives"],"3":["Guest"]},
        "albums":{"map":{"name":0,"artist_id":1,"year":2,"disc":3,"trashed":4},
            "10":["Album",1,1998,1,false],"11":["Album",1,1998,2,false],"12":["Punctuation [Live]",2,0,1,false],
            "13":["Unknown Year",1,0,1,false],"14":["Trash",1,2020,1,true]},
        "tracks":{"map":{"title":0,"artist_id":1,"album_id":2,"track":3,"year":4,"file":5,"type":6,"length":7,"genre":8},
            "101":["Same",1,10,1,2001,"/orig/101","audio/flac",34,"Electronic"],
            "102":["Same",1,10,1,2001,"/orig/102","audio/flac",36,"Electronic"],
            "103":["Same [101]",1,10,1,2001,"/orig/103","audio/flac",36,"Electronic"],
            "104":["Guest",3,11,1,2001,"/orig/104","audio/flac",30,"Ambient"],
            "105":["Path / Thing",2,12,0,2010,"/orig/105","audio/mp3",36,"Rock"],
            "106":["Song",1,13,1,0,"/orig/106","audio/flac",36,"Rock"],
            "107":["Trashed",1,14,1,2020,"/orig/107","audio/flac",36,"Rock"]},
        "playlists":{"1":{"name":"Fixture playlist","tracks":[101,104,105]}},
        "tags":{"2":{"name":"Fixture tag","tracks":[101,106]}}
    }})
}
fn lib(owner: &str) -> Library {
    Library::parse(fixture(), owner.into(), owner.into(), true).unwrap()
}
fn plan(lib: &Library, tracking: u64, quality: &str) -> ibfetch::catalog::Job {
    let stop = CancellationToken::new();
    let groups = lib
        .select(
            &Filters {
                track_id: vec![tracking],
                ..Filters::default()
            },
            &stop,
        )
        .unwrap();
    lib.plan(
        &groups,
        quality,
        &Layout::new(DEFAULT_LAYOUT).unwrap(),
        &stop,
    )
    .unwrap()
    .remove(0)
}
fn tests_options(temp: &TempDir, source: Source) -> Options {
    let mut opts = Options {
        out: temp.path().join("out"),
        work: None,
        quality: "orig".into(),
        source,
        workers: 3,
        verify: false,
        adopt: false,
    };
    opts.validate().unwrap();
    opts
}
fn manifest(path: &Path) -> PathBuf {
    path.parent().unwrap().join(".ibfetch.json")
}
fn partial(path: &Path) -> PathBuf {
    let mut name = path.file_name().unwrap().to_owned();
    name.push(".part");
    path.with_file_name(name)
}
fn sha(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    format!("{:x}", Sha256::digest(data))
}
fn record(path: &Path, part: bool) -> Value {
    let data: Value = serde_json::from_slice(&fs::read(manifest(path)).unwrap()).unwrap();
    data[if part { "partials" } else { "files" }][path.file_name().unwrap().to_str().unwrap()]
        .clone()
}
fn request_count(seen: &Arc<Mutex<Vec<(String, String)>>>, path: &str) -> usize {
    seen.lock()
        .unwrap()
        .iter()
        .filter(|(p, _)| p.starts_with(path))
        .count()
}

#[test]
fn named_filters_initials_disc_year_and_collision_paths_match_python() {
    let library = lib("demo");
    let stop = CancellationToken::new();
    let group = library
        .select(
            &Filters {
                artist_initial: Some('#'),
                skip_the: true,
                ..Filters::default()
            },
            &stop,
        )
        .unwrap();
    assert_eq!(
        group
            .iter()
            .flat_map(|g| &g.tracks)
            .copied()
            .collect::<Vec<_>>(),
        [105]
    );
    let group = library
        .select(
            &Filters {
                artist: vec!["bjork".into()],
                year: vec!["1998".into()],
                tag: vec!["fixture*".into()],
                playlist: vec!["fixture*".into()],
                ..Filters::default()
            },
            &stop,
        )
        .unwrap();
    assert_eq!(group[0].tracks, [101]);
    assert_eq!(
        plan(&library, 101, "orig").relative,
        Path::new("Björk/Album (1998)/1-01 Same [101-2].flac")
    );
    assert_eq!(
        plan(&library, 102, "orig").relative,
        Path::new("Björk/Album (1998)/1-01 Same [102].flac")
    );
    assert_eq!(
        plan(&library, 104, "orig").relative,
        Path::new("Björk/Album (1998)/2-01 Guest.flac")
    );
    assert_eq!(
        plan(&library, 106, "orig").relative,
        Path::new("Björk/Unknown Year/01 Song.flac")
    );
    assert!(
        plan(&library, 105, "192")
            .relative
            .to_string_lossy()
            .ends_with("Path _ Thing [192kbps].mp3")
    );
    let groups = library
        .select(
            &Filters {
                album: vec!["Punctuation [Live]".into()],
                ..Filters::default()
            },
            &stop,
        )
        .unwrap();
    assert_eq!(groups[0].tracks, [105]);
}

type Seen = Arc<Mutex<Vec<(String, String)>>>;
struct Mock {
    address: String,
    seen: Seen,
    stop: Arc<AtomicBool>,
    handle: Option<thread::JoinHandle<()>>,
}
impl Mock {
    fn start(
        handler: impl Fn(&str, &str, &str) -> (u16, Vec<u8>, Vec<(&'static str, String)>)
        + Send
        + 'static,
    ) -> Self {
        let srv = Server::http("127.0.0.1:0").unwrap();
        let port = srv.server_addr().to_ip().unwrap().port();
        let seen: Seen = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let (running, log) = (stop.clone(), seen.clone());
        let handle = thread::spawn(move || {
            while !running.load(Ordering::Relaxed) {
                let Some(mut incoming) = srv.recv_timeout(Duration::from_millis(50)).unwrap()
                else {
                    continue;
                };
                let p = incoming.url().to_owned();
                let range = incoming
                    .headers()
                    .iter()
                    .find(|h| h.field.equiv("Range"))
                    .map(|h| h.value.as_str().to_owned())
                    .unwrap_or_default();
                let mut body = String::new();
                let _ = incoming.as_reader().read_to_string(&mut body);
                log.lock().unwrap().push((
                    p.split('?').next().unwrap_or_default().into(),
                    range.clone(),
                ));
                let (status, data, headers) = handler(&p, &range, &body);
                let mut response = Response::from_data(data).with_status_code(status);
                for (k, v) in headers {
                    response.add_header(Header::from_bytes(k.as_bytes(), v.as_bytes()).unwrap());
                }
                let _ = incoming.respond(response);
            }
        });
        Self {
            address: format!("http://127.0.0.1:{port}"),
            seen,
            stop,
            handle: Some(handle),
        }
    }
    fn provider(&self, root: &Path) -> Provider {
        Provider::with_endpoints(
            root.join("home"),
            Endpoints::loopback(&self.address).unwrap(),
        )
        .unwrap()
    }
}
impl Drop for Mock {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        self.handle.take().unwrap().join().unwrap();
    }
}
fn store_tokens(root: &Path) {
    storage::write_json(
        &root.join("home/tokens.json"),
        &json!({"client_id":"local-fixture","access_token":"example-access",
        "refresh_token":"example-refresh","expires_at":4102444800u64,"scope":""}),
    )
    .unwrap();
}
fn media(data: Vec<u8>) -> (u16, Vec<u8>, Vec<(&'static str, String)>) {
    (200, data, vec![("Content-Type", "audio/flac".into())])
}
fn json_response(data: Value) -> (u16, Vec<u8>, Vec<(&'static str, String)>) {
    (
        200,
        data.to_string().into_bytes(),
        vec![("Content-Type", "application/json".into())],
    )
}

fn archive(data: &[u8], descriptor: bool) -> Vec<u8> {
    let filename = b"never/use/this/path.flac";
    let mut out = Vec::new();
    let crc = crc32fast::hash(data);
    let size = data.len() as u32;
    let flags = if descriptor { 8 } else { 0 };
    fn a(buf: &mut Vec<u8>, n: u16) {
        buf.extend(n.to_le_bytes());
    }
    fn b(buf: &mut Vec<u8>, n: u32) {
        buf.extend(n.to_le_bytes());
    }
    b(&mut out, 0x04034b50);
    a(&mut out, 20);
    a(&mut out, flags);
    out.extend([0; 6]);
    for n in [crc, size, size] {
        b(&mut out, if descriptor { 0 } else { n });
    }
    a(&mut out, filename.len() as u16);
    a(&mut out, 0);
    out.extend(filename);
    out.extend(data);
    if descriptor {
        for n in [0x08074b50, crc, size, size] {
            b(&mut out, n);
        }
    }
    let dir = out.len() as u32;
    b(&mut out, 0x02014b50);
    a(&mut out, 20);
    a(&mut out, 20);
    a(&mut out, flags);
    out.extend([0; 6]);
    for n in [crc, size, size] {
        b(&mut out, n);
    }
    a(&mut out, filename.len() as u16);
    out.extend([0; 16]);
    out.extend(filename);
    let dsize = out.len() as u32 - dir;
    b(&mut out, 0x06054b50);
    out.extend([0; 4]);
    a(&mut out, 1);
    a(&mut out, 1);
    b(&mut out, dsize);
    b(&mut out, dir);
    a(&mut out, 0);
    out.extend(b"EOF\n");
    out
}

#[test]
fn download_service_writes_only_verified_audio_and_cross_route_skip() {
    for descriptor in [false, true] {
        let temp = tempfile::tempdir().unwrap();
        store_tokens(temp.path());
        let audio = vec![0x52; 256 * 1024 + 321];
        let zip = archive(&audio, descriptor);
        let server = Mock::start(move |path, _, form| {
            assert_eq!(path, "/download");
            assert!(
                form.contains("mode=downloadlibrary")
                    && form.contains("ids=106")
                    && form.contains("end_marker=1")
            );
            (
                200,
                zip.clone(),
                vec![("Content-Type", "application/zip".into())],
            )
        });
        let account = server.provider(temp.path()).load().unwrap().unwrap();
        let lib = lib("one");
        let job = plan(&lib, 106, "orig");
        let mut opts = tests_options(&temp, Source::Download);
        let stop = CancellationToken::new();
        let out = opts.out.join(&job.relative);
        let reports = std::cell::RefCell::new(Vec::new());
        let done = downloads::transfer(&account, &lib, &job, &opts, &stop, &|at| {
            reports.borrow_mut().push(at);
        })
        .unwrap();
        // Every byte counts as received in this run, from the first block on.
        let reports = reports.into_inner();
        assert!(reports[0].received > 0 && reports.iter().all(|at| at.resumed == 0));
        assert_eq!(
            reports.iter().map(|at| at.chunk_bytes).sum::<u64>(),
            audio.len() as u64
        );
        assert_eq!(reports.last().unwrap().received, audio.len() as u64);
        assert_eq!(done.state, State::Saved);
        assert_eq!(fs::read(&out).unwrap(), audio);
        assert_eq!(record(&out, false)["sha256"], sha(&audio));
        assert!(record(&out, true).is_null());
        assert!(!partial(&out).exists());
        assert!(!opts.out.join("never").exists());
        opts.source = Source::Stream;
        assert_eq!(
            downloads::transfer(&account, &lib, &job, &opts, &stop, &|_| {})
                .unwrap()
                .state,
            State::Skipped
        );
        assert_eq!(request_count(&server.seen, "/download"), 1);
    }
}

#[test]
fn stream_resume_reads_python_manifest_and_rejects_a_changed_validator() {
    for changed in [false, true] {
        let temp = tempfile::tempdir().unwrap();
        store_tokens(temp.path());
        let server = Mock::start(move |path, range, _| {
            assert!(path.starts_with("/stream/orig/106?"));
            assert_eq!(range, "bytes=4-");
            (
                206,
                b"music".to_vec(),
                vec![
                    ("Content-Range", "bytes 4-8/9".into()),
                    (
                        "ETag",
                        if changed {
                            "\"different\""
                        } else {
                            "\"expected\""
                        }
                        .into(),
                    ),
                    ("Content-Type", "audio/flac".into()),
                ],
            )
        });
        let lib = lib("one");
        let job = plan(&lib, 106, "orig");
        let opts = tests_options(&temp, Source::Stream);
        let stop = CancellationToken::new();
        let out = opts.out.join(&job.relative);
        let key = out.file_name().unwrap().to_str().unwrap();
        fs::create_dir_all(out.parent().unwrap()).unwrap();
        fs::write(partial(&out), b"some").unwrap();
        storage::write_json(&manifest(&out),&json!({"version":1,"files":{},"partials":{(key):{
            "identity":{"version":1,"user_id":"one","track_id":106,"quality":"orig","source":"/orig/106","route":"stream"},
            "total":9,"etag":"\"expected\""
        }}})).unwrap();
        let account = server.provider(temp.path()).load().unwrap().unwrap();
        let reports = std::cell::RefCell::new(Vec::new());
        let result = downloads::transfer(&account, &lib, &job, &opts, &stop, &|at| {
            reports.borrow_mut().push(at);
        });
        let reports = reports.into_inner();
        if changed {
            assert!(reports.is_empty());
            assert!(result.unwrap_err().to_string().contains("validator"));
            assert_eq!(fs::read(partial(&out)).unwrap(), b"some");
            assert!(!out.exists());
        } else {
            // The four bytes saved earlier are told apart from the five received now.
            assert!(reports.iter().all(|at| at.resumed == 4 && at.received > 4));
            assert_eq!(reports.iter().map(|at| at.chunk_bytes).sum::<u64>(), 5);
            assert_eq!(reports.last().unwrap().received, 9);
            assert_eq!(result.unwrap().state, State::Saved);
            assert_eq!(fs::read(&out).unwrap(), b"somemusic");
            assert_eq!(record(&out, false)["sha256"], sha(b"somemusic"));
        }
    }
}

#[test]
fn stream_counts_both_responses_when_the_server_ignores_the_resume_range() {
    let temp = tempfile::tempdir().unwrap();
    store_tokens(temp.path());
    let server = Mock::start(|_, range, _| {
        if range.is_empty() {
            (
                206,
                vec![0x41; 4096],
                vec![
                    ("Content-Type", "audio/flac".into()),
                    ("Content-Range", "bytes 0-4095/8192".into()),
                ],
            )
        } else {
            media(vec![0x42; 8192]) // HTTP 200 with the entire replacement file
        }
    });
    let account = server.provider(temp.path()).load().unwrap().unwrap();
    let library = lib("one");
    let job = plan(&library, 106, "orig");
    let opts = tests_options(&temp, Source::Stream);
    let traffic = std::cell::Cell::new(0);
    let done = downloads::transfer(
        &account,
        &library,
        &job,
        &opts,
        &CancellationToken::new(),
        &|at| {
            traffic.update(|n| n + at.chunk_bytes);
        },
    )
    .unwrap();

    let seen = server.seen.lock().unwrap();
    let ranges: Vec<_> = seen.iter().map(|(_, range)| range.as_str()).collect();
    assert_eq!(ranges, ["", "bytes=4096-"]);
    assert_eq!(traffic.get(), 4096 + 8192);
    assert_eq!(done.state, State::Saved);
    assert_eq!(done.bytes, 8192);
    assert_eq!(
        fs::read(opts.out.join(&job.relative)).unwrap(),
        vec![0x42; 8192]
    );
}

#[test]
fn fallback_keeps_provenance_and_does_not_replace_existing_audio() {
    let temp = tempfile::tempdir().unwrap();
    store_tokens(temp.path());
    let server = Mock::start(|path, _, _| {
        if path == "/download" {
            return media(b"invalid or unexpected ZIP".repeat(2));
        }
        assert!(path.starts_with("/stream/orig/106?"));
        media(b"original tags".to_vec())
    });
    let account = server.provider(temp.path()).load().unwrap().unwrap();
    let lib = lib("one");
    let job = plan(&lib, 106, "orig");
    let opts = tests_options(&temp, Source::Download);
    let stop = CancellationToken::new();
    let out = opts.out.join(&job.relative);
    let result = downloads::transfer(&account, &lib, &job, &opts, &stop, &|_| {}).unwrap();
    assert_eq!(result.state, State::Saved);
    assert!(result.note.contains("Fallback:"));
    assert_eq!(record(&out, false)["identity"]["route"], "stream");
    fs::write(&out, b"edited tags").unwrap();
    let next = downloads::transfer(&account, &lib, &job, &opts, &stop, &|_| {}).unwrap();
    assert_eq!(next.state, State::Skipped);
    assert!(next.note.contains("Modified"));
    assert_eq!(fs::read(&out).unwrap(), b"edited tags");
    assert_eq!(request_count(&server.seen, "/stream"), 1);
}
#[test]
fn independent_workers_preserve_shared_manifest_and_work_output() {
    let temp = tempfile::tempdir().unwrap();
    store_tokens(temp.path());
    let server = Mock::start(|_, _, _| media(b"audio fixture".to_vec()));
    let account = server.provider(temp.path()).load().unwrap().unwrap();
    let lib = lib("one");
    let jobs = [
        plan(&lib, 101, "orig"),
        plan(&lib, 102, "orig"),
        plan(&lib, 104, "orig"),
    ];
    let mut opts = tests_options(&temp, Source::Stream);
    opts.work = Some(temp.path().join("work"));
    let result = downloads::run(
        &account,
        &lib,
        &jobs,
        opts.clone(),
        &CancellationToken::new(),
        |_| {},
    )
    .unwrap();
    assert_eq!(result.saved, 3, "{result:?}");
    let p = opts.out.join(&jobs[0].relative);
    let m: Value = serde_json::from_slice(&fs::read(manifest(&p)).unwrap()).unwrap();
    assert_eq!(m["files"].as_object().unwrap().len(), 3);
    assert!(m["partials"].as_object().unwrap().is_empty());
    let root = opts
        .work
        .unwrap()
        .join(&storage::path_digest(&opts.out).unwrap()[..16]);
    for job in jobs {
        assert!(!root.join(&job.relative).exists());
    }
}
#[test]
fn old_completion_receipt_recovers_without_network() {
    let temp = tempfile::tempdir().unwrap();
    store_tokens(temp.path());
    let server = Mock::start(|_, _, _| panic!("Recovery must not contact the provider"));
    let account = server.provider(temp.path()).load().unwrap().unwrap();
    let lib = lib("one");
    let job = plan(&lib, 106, "orig");
    let opts = tests_options(&temp, Source::Stream);
    let out = opts.out.join(&job.relative);
    let key = out.file_name().unwrap().to_str().unwrap();
    fs::create_dir_all(out.parent().unwrap()).unwrap();
    fs::write(&out, b"expected bytes").unwrap();
    storage::write_json(&manifest(&out),&json!({"version":1,"files":{},"partials":{(key):{
        "identity":{"version":1,"user_id":"one","track_id":106,"quality":"orig","source":"/orig/106","route":"stream"},
        "completed":true,"size":14,"sha256":sha(b"expected bytes"),"note":"Original fallback note"
    }}})).unwrap();
    let result = downloads::transfer(
        &account,
        &lib,
        &job,
        &opts,
        &CancellationToken::new(),
        &|_| {},
    )
    .unwrap();
    assert_eq!(result.state, State::Skipped);
    assert!(result.note.contains("Recovered"));
    assert_eq!(record(&out, false)["note"], "Original fallback note");
    assert!(record(&out, true).is_null());
    assert!(server.seen.lock().unwrap().is_empty());
}
#[test]
fn malformed_manifest_and_other_account_are_preserved() {
    let temp = tempfile::tempdir().unwrap();
    store_tokens(temp.path());
    let server = Mock::start(|_, _, _| panic!("Unrelated files must never be downloaded over"));
    let account = server.provider(temp.path()).load().unwrap().unwrap();
    let lib = lib("one");
    let job = plan(&lib, 106, "orig");
    let mut opts = tests_options(&temp, Source::Stream);
    opts.adopt = true;
    let out = opts.out.join(&job.relative);
    let key = out.file_name().unwrap().to_str().unwrap();
    fs::create_dir_all(out.parent().unwrap()).unwrap();
    fs::write(&out, b"unrelated").unwrap();
    let bad = b"{\"version\":999,\"files\":{},\"partials\":{}}";
    fs::write(manifest(&out), bad).unwrap();
    assert!(
        downloads::transfer(
            &account,
            &lib,
            &job,
            &opts,
            &CancellationToken::new(),
            &|_| {}
        )
        .is_err()
    );
    assert_eq!(fs::read(manifest(&out)).unwrap(), bad);
    storage::write_json(
        &manifest(&out),
        &json!({"version":1,"files":{(key):{"identity":{
        "version":1,"user_id":"other","track_id":106,"quality":"orig","source":"/orig/106"
    }}},"partials":{}}),
    )
    .unwrap();
    assert_eq!(
        downloads::transfer(
            &account,
            &lib,
            &job,
            &opts,
            &CancellationToken::new(),
            &|_| {}
        )
        .unwrap()
        .state,
        State::Skipped
    );
    assert_eq!(fs::read(&out).unwrap(), b"unrelated");
    assert!(server.seen.lock().unwrap().is_empty());
}
#[test]
fn no_replace_publication_preserves_the_other_file() {
    let dir = tempfile::tempdir().unwrap();
    let a = dir.path().join("a");
    let b = dir.path().join("b");
    fs::write(&a, b"new").unwrap();
    fs::write(&b, b"existing").unwrap();
    assert!(storage::publish_audio(&a, &b).is_err());
    assert_eq!(fs::read(&b).unwrap(), b"existing");
    assert_eq!(fs::read(&a).unwrap(), b"new");
}
#[test]
fn library_cache_matches_account_even_when_revision_is_identical() {
    let temp = tempfile::tempdir().unwrap();
    store_tokens(temp.path());
    let current = Arc::new(AtomicBool::new(false));
    let switched = current.clone();
    let server = Mock::start(move |path, _, _| {
        if path == "/api/status" {
            let name = if switched.load(Ordering::Relaxed) {
                "second"
            } else {
                "first"
            };
            return json_response(
                json!({"user":{"id":name,"username":name,"premium":true},"status":{"lastmodified":"same"}}),
            );
        }
        if path == "/library" {
            let mut data = fixture();
            if switched.load(Ordering::Relaxed) {
                data["library"]["albums"]["13"][0] = json!("Second Library");
            }
            return json_response(data);
        }
        panic!("Unexpected mock route");
    });
    let account = server.provider(temp.path()).load().unwrap().unwrap();
    let stop = CancellationToken::new();
    let first = account.library(false, &stop).unwrap();
    assert_eq!(first.owner, "first");
    let repeated = account.library(false, &stop).unwrap();
    assert_eq!(repeated.owner, "first");
    current.store(true, Ordering::Relaxed);
    let second = account.library(false, &stop).unwrap();
    assert_eq!(second.owner, "second");
    assert_eq!(second.album_title(13), "Second Library");
    assert_eq!(request_count(&server.seen, "/library"), 2);
}
#[test]
fn cancelled_device_login_cannot_save_even_if_provider_approves() {
    let temp = tempfile::tempdir().unwrap();
    let server = Mock::start(|path, _, _| {
        if path.starts_with("/oauth/device/code") {
            assert!(
                path.contains("client_id=fixture-client"),
                "unexpected fixture client ID"
            );
            return json_response(
                json!({"device_code":"mock-device","user_code":"DEVICE","interval":1,
                "verification_uri":"https://oauth.ibroadcast.com/device"}),
            );
        }
        panic!("Cancelled fixture must not poll for tokens");
    });
    let provider = server.provider(temp.path());
    let stop = CancellationToken::new();
    let result = provider.connect(Some("fixture-client"), &stop, |view| {
        assert_eq!(view.code, "DEVICE");
        stop.cancel();
    });
    assert!(result.is_err());
    assert!(provider.load().unwrap().is_none());
}

#[test]
fn browser_contract_requires_host_and_token_and_downloads_through_rust() {
    use std::{
        io::{BufRead, BufReader, Read, Write},
        net::TcpStream,
        process::{Child, Command, Stdio},
        time::Instant,
    };
    // The fixture endpoint is accepted by debug binaries only.
    if !cfg!(debug_assertions) {
        return;
    }
    struct Process(Child);
    impl Drop for Process {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let tmp = tempfile::tempdir().unwrap();
    store_tokens(tmp.path());
    storage::write_json(
        &tmp.path().join("home/app-settings.json"),
        &json!({"auto_updates":false}),
    )
    .unwrap();
    let server = Mock::start(|path, _, _| {
        if path == "/api/status" {
            return json_response(
                json!({"user":{"id":"one","username":"First Fixture","premium":true},"status":{"lastmodified":"same"}}),
            );
        }
        if path == "/library" {
            return json_response(fixture());
        }
        if path.starts_with("/stream/orig/105") {
            return media(b"synthetic mp3 payload".to_vec());
        }
        panic!("Unexpected mock route");
    });
    let mut child = Process(
        Command::new(env!("CARGO_BIN_EXE_ibfetch"))
            .arg("--home")
            .arg(tmp.path().join("home"))
            .args(["gui", "--port", "0", "--no-browser"])
            .env("IBFETCH_RUST_TEST_PROVIDER", &server.address)
            .stdout(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let stdout = child.0.stdout.take().unwrap();
    let (tx, rx) = std::sync::mpsc::channel();
    thread::spawn(move || {
        let mut lines = BufReader::new(stdout).lines();
        let _ = lines.next();
        let _ = tx.send(lines.next().unwrap().unwrap());
        for _ in lines {}
    });
    let url = rx.recv_timeout(Duration::from_secs(10)).unwrap();
    let (addr, token) = url
        .trim()
        .strip_prefix("http://")
        .unwrap()
        .split_once("/?t=")
        .unwrap();
    let request = |method: &str, path: &str, host: &str, auth: bool, body: &str| {
        let mut conn = TcpStream::connect(addr).unwrap();
        conn.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        let auth = if auth {
            format!("X-Ibfetch-Token: {token}\r\n")
        } else {
            String::new()
        };
        write!(conn,"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n{auth}Content-Length: {}\r\n\r\n{body}",body.len()).unwrap();
        let mut full = String::new();
        conn.read_to_string(&mut full).unwrap();
        full
    };
    let call = |method: &str, path: &str, body: &Value| {
        let resp = request(method, path, addr, true, &body.to_string());
        assert!(resp.starts_with("HTTP/1.1 200"), "{resp}");
        let (_, value) = resp.split_once("\r\n\r\n").unwrap();
        serde_json::from_str::<Value>(value).unwrap()
    };
    let page = request("GET", &format!("/?t={token}"), addr, false, "");
    assert!(
        page.starts_with("HTTP/1.1 200")
            && page.contains("Content-Security-Policy:")
            && page.contains("SameSite=Strict")
    );
    assert!(page.contains("select") && page.contains("id=\"updateSettings\""));
    assert!(request("GET", "/api/state", addr, false, "").starts_with("HTTP/1.1 403"));
    assert!(request("GET", "/api/state", "example.invalid", true, "").starts_with("HTTP/1.1 403"));
    let until = Instant::now() + Duration::from_secs(10);
    loop {
        let s = call("GET", "/api/state", &Value::Null);
        if s["library"]["state"] == "ready" {
            assert_eq!(s["user"]["name"], "First Fixture");
            break;
        }
        assert!(Instant::now() < until, "{s}");
        thread::sleep(Duration::from_millis(30));
    }
    let albums = call(
        "POST",
        "/api/select",
        &json!({"artist_initial":"#","skip_the":true}),
    );
    let chosen = &albums["albums"][0];
    assert_eq!(chosen["title"], "Punctuation [Live]");
    let out = tmp.path().join("browser-output");
    assert_eq!(
        call(
            "POST",
            "/api/download",
            &json!({"items":[{"album_id":chosen["id"],"tracks":chosen["tracks"]}],
        "out":out,"source":"stream","quality":"orig"})
        )["tracks"],
        1
    );
    let until = Instant::now() + Duration::from_secs(10);
    loop {
        let t = call("GET", "/api/state", &Value::Null)["transfer"].clone();
        if t["state"] == "done" {
            assert_eq!(t["ok"], 1, "{t}");
            break;
        }
        assert!(Instant::now() < until, "{t}");
        thread::sleep(Duration::from_millis(30));
    }
    assert_eq!(
        fs::read(out.join(plan(&lib("one"), 105, "orig").relative)).unwrap(),
        b"synthetic mp3 payload"
    );
    call("POST", "/api/quit", &json!({}));
    let until = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(status) = child.0.try_wait().unwrap() {
            assert!(status.success());
            break;
        }
        assert!(Instant::now() < until, "Browser server did not exit.");
        thread::sleep(Duration::from_millis(30));
    }
}
