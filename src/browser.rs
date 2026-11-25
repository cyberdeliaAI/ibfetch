use crate::{
    VERSION,
    account::Provider,
    catalog::{Filters, Group},
    model::Model,
    storage,
};
use anyhow::{Context, Result, bail, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    io::Read,
    net::SocketAddrV4,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tiny_http::{Header, Method, Request, Response, Server, StatusCode};
use tokio_util::sync::CancellationToken;

const TEMPLATE: &str = include_str!("../assets/browser.html");
const ICON: &[u8] = include_bytes!("../assets/ibfetch-icon.svg");
const LIGHT: &[u8] = include_bytes!("../assets/ibroadcast-light-compact-powered.svg");
const DARK: &[u8] = include_bytes!("../assets/ibroadcast-dark-compact-powered.svg");
const MAX_BODY: u64 = 64 * 1024 * 1024;

pub fn open_url(url: &str) -> Result<()> {
    ensure!(
        url.starts_with("https://") || url.starts_with("http://127.0.0.1:"),
        "Invalid browser link."
    );
    #[cfg(windows)]
    {
        windows_open_link(url)
    }
    #[cfg(not(windows))]
    open_external(url.as_ref())
}

#[cfg(windows)]
fn windows_open_link(url: &str) -> Result<()> {
    use windows_sys::Win32::{
        Foundation::RPC_E_CHANGED_MODE,
        System::Com::{COINIT_APARTMENTTHREADED, CoInitializeEx, CoUninitialize},
        UI::{
            Shell::{SEE_MASK_FLAG_NO_UI, SEE_MASK_NOASYNC, SHELLEXECUTEINFOW, ShellExecuteExW},
            WindowsAndMessaging::SW_SHOWNORMAL,
        },
    };

    ensure!(!url.contains('\0'), "Invalid browser link.");
    let target: Vec<u16> = url.encode_utf16().chain(Some(0)).collect();
    let mut request = SHELLEXECUTEINFOW {
        cbSize: std::mem::size_of::<SHELLEXECUTEINFOW>() as u32,
        fMask: SEE_MASK_FLAG_NO_UI | SEE_MASK_NOASYNC,
        lpVerb: windows_sys::core::w!("open"),
        lpFile: target.as_ptr(),
        nShow: SW_SHOWNORMAL,
        ..Default::default()
    };
    // The shell can delegate to a COM protocol handler. Respect an apartment
    // already established by the desktop UI and balance only our own init.
    let hr = unsafe { CoInitializeEx(std::ptr::null(), COINIT_APARTMENTTHREADED as u32) };
    ensure!(
        hr >= 0 || hr == RPC_E_CHANGED_MODE,
        "Could not initialize the Windows browser launcher."
    );
    // target remains alive until Windows has finished handing off the URL.
    let launched = unsafe { ShellExecuteExW(&mut request) } != 0;
    let error = (!launched).then(std::io::Error::last_os_error);
    if hr >= 0 {
        unsafe { CoUninitialize() };
    }
    if let Some(error) = error {
        bail!("Windows could not open the default browser ({error}).");
    }
    Ok(())
}

#[cfg(all(test, windows))]
#[test]
fn windows_reports_failed_shell_dispatch() {
    let temp = tempfile::tempdir().unwrap();
    let missing = temp.path().join("nonexistent.exe");
    assert!(
        windows_open_link(missing.to_str().unwrap())
            .unwrap_err()
            .to_string()
            .contains("Windows could not open")
    );
}

pub fn open_folder(path: &Path) -> Result<()> {
    let folder = storage::canonical(&storage::expand(path)?)?;
    ensure!(folder.is_dir(), "That folder does not exist.");
    open_external(folder.as_os_str())
}
fn open_external(value: &std::ffi::OsStr) -> Result<()> {
    let command = if cfg!(target_os = "macos") {
        "open"
    } else if cfg!(windows) {
        "explorer.exe"
    } else {
        "xdg-open"
    };
    let mut proc = std::process::Command::new(command)
        .arg(value)
        .spawn()
        .context("Could not open the system browser or file manager.")?;
    std::thread::spawn(move || {
        let _ = proc.wait();
    });
    Ok(())
}
#[cfg(feature = "desktop")]
pub fn choose_folder(start: &Path) -> Option<PathBuf> {
    let folder = storage::expand(start).unwrap_or_else(|_| start.to_owned());
    let dialog = rfd::FileDialog::new().set_title("Choose a folder");
    if folder.is_dir() {
        dialog.set_directory(folder).pick_folder()
    } else {
        dialog.pick_folder()
    }
}
#[cfg(not(feature = "desktop"))]
pub fn choose_folder(_: &Path) -> Option<PathBuf> {
    None
}

pub fn run(provider: Provider, port: u16, launch: bool, stop: CancellationToken) -> Result<()> {
    let listen = |p| {
        Server::http(SocketAddrV4::new(std::net::Ipv4Addr::LOCALHOST, p))
            .map_err(|_| anyhow::anyhow!("Could not bind a loopback browser port."))
    };
    let server = Arc::new(listen(port).or_else(|_| listen(0))?);
    let port = server
        .server_addr()
        .to_ip()
        .context("Local browser address unavailable.")?
        .port();
    let token = crate::random_hex(24)?;
    let url = format!("http://127.0.0.1:{port}/?t={token}");
    let app = Model::new(provider);
    app.updates.check(false);
    println!("ibfetch is running at\n  {url}\n\nKeep this window open. Press Ctrl+C to quit.");
    if launch {
        let _ = open_url(&url);
    }
    let page = Arc::new(Page::new(token, port)?);
    std::thread::scope(|threads| {
        for _ in 0..4 {
            let (server, page, app, stop) =
                (server.clone(), page.clone(), app.clone(), stop.clone());
            threads.spawn(move || {
                while !stop.is_cancelled() {
                    match server.recv_timeout(Duration::from_millis(150)) {
                        Ok(Some(request)) => handle(request, &page, &app, &stop),
                        Ok(None) => {}
                        Err(_) => break,
                    }
                }
            });
        }
        while !stop.is_cancelled() {
            std::thread::sleep(Duration::from_millis(100));
        }
        app.close();
    });
    for _ in 0..50 {
        if !app.busy() {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    Ok(())
}
struct Page {
    html: String,
    token: String,
    host: String,
    localhost: String,
    csp: String,
}
impl Page {
    fn new(token: String, port: u16) -> Result<Self> {
        let image = |data| format!("data:image/svg+xml;base64,{}", STANDARD.encode(data));
        let html = TEMPLATE
            .replace("__TOKEN__", &token)
            .replace("__VERSION__", VERSION)
            .replace("__IBFETCH_ICON__", &image(ICON))
            .replace("__POWERED_LIGHT__", &image(LIGHT))
            .replace("__POWERED_DARK__", &image(DARK));
        // Use the same CSP treatment as the original page. There are no remote assets.
        let hashes = html
            .split("<script>")
            .skip(1)
            .map(|s| {
                let (js, _) = s
                    .split_once("</script>")
                    .context("Invalid browser script.")?;
                Ok(format!(
                    "'sha256-{}'",
                    STANDARD.encode(Sha256::digest(js.as_bytes()))
                ))
            })
            .collect::<Result<Vec<_>>>()?
            .join(" ");
        let csp = format!(
            "default-src 'none'; script-src {hashes}; style-src 'unsafe-inline'; connect-src 'self'; img-src data:"
        );
        Ok(Self {
            html,
            token,
            host: format!("127.0.0.1:{port}"),
            localhost: format!("localhost:{port}"),
            csp,
        })
    }
    fn initial_url(&self, url: &str) -> bool {
        url.split_once('?')
            .is_some_and(|(p, q)| p == "/" && q == format!("t={}", self.token))
    }
}
fn header<'a>(request: &'a Request, key: &str) -> Option<&'a str> {
    let mut values = request
        .headers()
        .iter()
        .filter(|h| h.field.as_str().as_str().eq_ignore_ascii_case(key));
    let value = values.next()?.value.as_str();
    values.next().is_none().then_some(value)
}
fn reply(request: Request, status: u16, body: Vec<u8>, kind: &str, extra: &[(&str, &str)]) {
    let mut response = Response::from_data(body).with_status_code(StatusCode(status));
    for &(name, value) in [
        ("Content-Type", kind),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
    ]
    .iter()
    .chain(extra)
    {
        if let Ok(h) = Header::from_bytes(name, value) {
            response.add_header(h);
        }
    }
    let _ = request.respond(response);
}
fn json_reply(req: Request, status: u16, result: Value) {
    reply(
        req,
        status,
        result.to_string().into_bytes(),
        "application/json; charset=utf-8",
        &[],
    );
}
fn handle(mut req: Request, page: &Page, app: &Model, stop: &CancellationToken) {
    if !header(&req, "Host").is_some_and(|h| h == page.host || h == page.localhost) {
        return json_reply(req, 403, json!({"error":"Forbidden"}));
    }
    let path = req.url().split('?').next().unwrap_or("").to_owned();
    if req.method() == &Method::Get && path == "/" {
        let authorized = page.initial_url(req.url())
            || header(&req, "Cookie").is_some_and(|value| {
                value
                    .split(';')
                    .any(|c| c.trim().strip_prefix("ibfetch=") == Some(page.token.as_str()))
            });
        if !authorized {
            return reply(
                req,
                403,
                b"Open the link printed in your terminal.".to_vec(),
                "text/plain; charset=utf-8",
                &[],
            );
        }
        let settings = app.settings();
        let html = page
            .html
            .replace("__APPEARANCE__", &settings.appearance)
            .replace("__PALETTE__", &settings.palette);
        let cookie = format!("ibfetch={}; Path=/; HttpOnly; SameSite=Strict", page.token);
        return reply(
            req,
            200,
            html.into_bytes(),
            "text/html; charset=utf-8",
            &[
                ("Set-Cookie", &cookie),
                ("Content-Security-Policy", &page.csp),
            ],
        );
    }
    if header(&req, "X-Ibfetch-Token") != Some(&page.token) {
        return json_reply(req, 403, json!({"error":"Forbidden"}));
    }
    if req.method() == &Method::Get {
        return match path.as_str() {
            "/api/state" => {
                app.updates.check(false);
                let view = app.view();
                let library = if view.loading {
                    "loading"
                } else if view.library.is_some() {
                    "ready"
                } else if !view.error.is_empty() && view.signed_in {
                    "error"
                } else {
                    "none"
                };
                let user = view
                    .library
                    .as_ref()
                    .map(|l| json!({"name":l.name,"premium":l.premium}));
                json_reply(
                    req,
                    200,
                    json!({"signed_in":view.signed_in,"login":view.login,
                    "session":view.epoch,"library":{"state":library,"error":view.error,"version":view.revision,"counts":app.meta()["counts"]},
                    "settings":app.settings(),"user":user,"transfer":view.transfer,"can_retry":view.can_retry,"updates":app.updates.snapshot()}),
                );
            }
            "/api/meta" => json_reply(req, 200, app.meta().as_ref().clone()),
            _ => json_reply(req, 404, json!({"error":"Not found"})),
        };
    }
    if req.method() != &Method::Post {
        return json_reply(req, 405, json!({"error":"Method not allowed"}));
    }
    if req.body_length().is_some_and(|n| n as u64 > MAX_BODY) {
        return json_reply(req, 413, json!({"error":"Too much data."}));
    }
    let mut raw = Vec::new();
    if req
        .as_reader()
        .take(MAX_BODY + 1)
        .read_to_end(&mut raw)
        .is_err()
        || raw.len() as u64 > MAX_BODY
    {
        return json_reply(req, 413, json!({"error":"Too much or invalid data."}));
    }
    let body = if raw.is_empty() {
        json!({})
    } else {
        match serde_json::from_slice::<Value>(&raw) {
            Ok(body) if body.is_object() => body,
            _ => return json_reply(req, 400, json!({"error":"Bad request."})),
        }
    };
    match route(app, &path, &body, stop) {
        Ok(Some(value)) => json_reply(req, 200, value),
        Ok(None) => json_reply(req, 404, json!({"error":"Not found"})),
        Err(e) => json_reply(req, 400, json!({"error":e.to_string()})),
    }
}
fn route(app: &Model, path: &str, body: &Value, stop: &CancellationToken) -> Result<Option<Value>> {
    let txt = |key: &str| body[key].as_str().unwrap_or_default().trim();
    let one = |key: &str| {
        let s = txt(key);
        if s.is_empty() { vec![] } else { vec![s.into()] }
    };
    let value = match path {
        "/api/login" => {
            let rx = app.login()?;
            let mut result = None;
            for _ in 0..480 {
                if stop.is_cancelled() {
                    bail!("Stopped.");
                }
                match rx.recv_timeout(Duration::from_millis(100)) {
                    Ok(value) => {
                        result = Some(value?);
                        break;
                    }
                    Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
                    Err(_) => bail!("Sign-in was cancelled."),
                }
            }
            json!({"login":result.context("Sign-in did not respond. Try again.")?})
        }
        "/api/login/cancel" => {
            app.cancel_login(body["request_id"].as_str());
            json!({})
        }
        "/api/logout" => {
            app.logout()?;
            json!({})
        }
        "/api/library" => {
            app.load(body["refresh"] == true)?;
            json!({})
        }
        "/api/select" => {
            let artist_initial = if let Some(raw) = body["artist_initial"].as_str() {
                let mut chars = raw.chars();
                let initial = chars.next().context("Choose an artist group.")?;
                ensure!(
                    chars.next().is_none(),
                    "Choose an artist group from A–Z or #."
                );
                Some(initial)
            } else {
                None
            };
            let f = Filters {
                artist: one("artist"),
                track_artist: one("track_artist"),
                album: one("album"),
                genre: one("genre"),
                format: one("format"),
                tag: one("tag"),
                playlist: one("playlist"),
                year: one("year"),
                artist_initial,
                skip_the: body["skip_the"] == true,
                all: body["all"] == true,
                ..Filters::default()
            };
            json!({"albums":app.search(&f,stop)?})
        }
        "/api/album" => {
            let lib = app.library()?;
            let rows=body["tracks"].as_array().into_iter().flatten().filter_map(Value::as_u64)
                .filter(|&id|lib.has_track(id)).map(|id|json!({"id":id,"no":lib.track_number(id),"title":lib.track_title(id),
                    "artist":lib.track_artist(id),"length":lib.duration(id),"format":lib.format(id)})).collect::<Vec<_>>();
            json!({"tracks":rows})
        }
        "/api/download" => {
            let items = body["items"]
                .as_array()
                .context("The download list is empty.")?;
            let items = items
                .iter()
                .filter_map(|v| {
                    Some(Group {
                        id: v["album_id"].as_u64()?,
                        tracks: v["tracks"]
                            .as_array()?
                            .iter()
                            .filter_map(Value::as_u64)
                            .collect(),
                    })
                })
                .collect();
            let mut settings = app.settings();
            settings.out = txt("out").into();
            settings.work = txt("work").into();
            if !txt("layout").is_empty() {
                settings.layout = txt("layout").into();
            }
            if !txt("quality").is_empty() {
                settings.quality = txt("quality").into();
            }
            if !txt("source").is_empty() {
                settings.source = txt("source").into();
            }
            if let Some(n) = body["jobs"].as_u64() {
                settings.jobs = usize::try_from(n)?;
            }
            json!({"tracks":app.start(items,settings,body["verify"]==true,body["adopt_existing"]==true)?})
        }
        "/api/retry" => json!({"tracks":app.retry()?}),
        "/api/cancel" => {
            app.cancel_download();
            json!({})
        }
        "/api/choose-folder" => {
            let path = choose_folder(Path::new(txt("start")));
            json!({"available":path.is_some(),"path":path})
        }
        "/api/open-folder" => {
            open_folder(Path::new(txt("path")))?;
            json!({})
        }
        "/api/updates/check" => {
            app.updates.check(true);
            app.updates.snapshot()
        }
        "/api/updates/settings" => {
            let enabled = body["automatic"]
                .as_bool()
                .context("Choose whether to check automatically.")?;
            app.save_settings(|s| s.auto_updates = enabled)?;
            app.updates.check(false);
            app.updates.snapshot()
        }
        "/api/appearance" => {
            let (appearance, palette) = (txt("appearance"), txt("palette"));
            ensure!(
                matches!(appearance, "auto" | "dark" | "light")
                    && matches!(
                        palette,
                        "forest" | "ocean" | "warm" | "ibroadcast" | "amber"
                    ),
                "Choose a listed appearance and palette."
            );
            app.save_settings(|s| {
                s.appearance = appearance.into();
                s.palette = palette.into();
            })?;
            json!({})
        }
        "/api/quit" => {
            app.close();
            stop.cancel();
            json!({})
        }
        _ => return Ok(None),
    };
    Ok(Some(value))
}
