use crate::{
    APP_SLUG, VERSION,
    catalog::{Library, cancel, text},
    downloads::{Fatal, Retry},
    storage,
};
use anyhow::{Context, Result, anyhow, bail, ensure};
use flate2::{Compression, read::GzDecoder, write::GzEncoder};
use reqwest::{Client, RequestBuilder, Response, StatusCode, Url, redirect::Policy};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use std::{
    fs::File,
    future::Future,
    io::Read,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::runtime::{Builder, Runtime};
use tokio_util::sync::CancellationToken;

// Public iBroadcast device-flow registration for ibfetch.
const DEFAULT_CLIENT_ID: &str = "3c2ba6a3e69e11f1b50eb49691aa2236";
const BUILT_IN_CLIENT_ID: Option<&str> = option_env!("IBROADCAST_CLIENT_ID");
const VERIFY: &str = "https://oauth.ibroadcast.com/device";
const SCOPES: &str = "user.library:read user.account:read";
const LIBRARY_MAX: usize = 256 * 1024 * 1024;
pub const USER_AGENT: &str = concat!(env!("CARGO_PKG_NAME"), "/", env!("CARGO_PKG_VERSION"));

#[derive(Clone)]
pub struct Endpoints {
    oauth: String,
    api: String,
    library: String,
    stream: String,
    download: String,
}
impl Default for Endpoints {
    fn default() -> Self {
        Self {
            oauth: "https://oauth.ibroadcast.com".into(),
            api: "https://api.ibroadcast.com".into(),
            library: "https://library.ibroadcast.com".into(),
            stream: "https://streaming.ibroadcast.com".into(),
            download: "https://download.ibroadcast.com/".into(),
        }
    }
}
impl Endpoints {
    pub fn loopback(address: &str) -> Result<Self> {
        let url = Url::parse(address).context("Invalid fixture address.")?;
        ensure!(
            url.scheme() == "http"
                && url.host_str() == Some("127.0.0.1")
                && url.port().is_some()
                && url.username().is_empty()
                && url.password().is_none()
                && url.path() == "/"
                && url.query().is_none()
                && url.fragment().is_none(),
            "Fixtures must use a loopback test server."
        );
        let base = address.trim_end_matches('/');
        Ok(Self {
            oauth: format!("{base}/oauth"),
            api: format!("{base}/api"),
            library: format!("{base}/library"),
            stream: format!("{base}/stream"),
            download: format!("{base}/download"),
        })
    }
}

#[derive(Debug)]
pub enum HttpError {
    Network,
    Tls,
}
impl std::fmt::Display for HttpError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Network => "The connection to iBroadcast was interrupted.",
            Self::Tls => "Could not verify the secure connection to iBroadcast.",
        })
    }
}
impl std::error::Error for HttpError {}
fn http_error(err: reqwest::Error) -> anyhow::Error {
    use std::error::Error;
    let mut source: Option<&(dyn Error + 'static)> = Some(&err);
    while let Some(e) = source {
        let desc = e.to_string().to_lowercase();
        if [
            "certificate",
            "tls handshake",
            "invalid peer",
            "ssl routines",
            "trust evaluation",
            "secure connection",
        ]
        .iter()
        .any(|str| desc.contains(str))
        {
            return HttpError::Tls.into();
        }
        source = e.source();
    }
    HttpError::Network.into()
}
#[derive(Clone)]
pub struct Http {
    pub client: Client,
    bulk: Client,
    stream: Client,
    export: Client,
    runtime: Arc<Runtime>,
}
impl Http {
    pub fn new() -> Result<Self> {
        let client = |secs, gzip| {
            Client::builder()
                .redirect(Policy::none())
                .user_agent(USER_AGENT)
                .connect_timeout(Duration::from_secs(10))
                .read_timeout(Duration::from_secs(secs))
                .gzip(gzip)
                .build()
                .map_err(http_error)
        };
        let runtime = Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()?;
        Ok(Self {
            client: client(45, true)?,
            bulk: client(600, true)?,
            stream: client(120, false)?,
            export: client(300, false)?,
            runtime: Arc::new(runtime),
        })
    }
    pub fn run<T>(
        &self,
        stop: &CancellationToken,
        future: impl Future<Output = Result<T>>,
    ) -> Result<T> {
        self.runtime.block_on(async {
            tokio::select! {biased; _=stop.cancelled()=>Err(anyhow!("Stopped.")), value=future=>value}
        })
    }
    pub fn send(&self, stop: &CancellationToken, request: RequestBuilder) -> Result<Response> {
        self.run(stop, async { request.send().await.map_err(http_error) })
    }
    pub fn chunk(
        &self,
        stop: &CancellationToken,
        response: &mut Response,
    ) -> Result<Option<impl AsRef<[u8]> + use<>>> {
        self.run(stop, async { response.chunk().await.map_err(http_error) })
    }
    pub fn body(
        &self,
        stop: &CancellationToken,
        mut response: Response,
        limit: usize,
    ) -> Result<Vec<u8>> {
        self.run(stop, async {
            let mut data = Vec::new();
            while let Some(chunk) = response.chunk().await.map_err(http_error)? {
                ensure!(
                    data.len().saturating_add(chunk.len()) <= limit,
                    "Response exceeds the size limit."
                );
                data.extend_from_slice(&chunk);
            }
            Ok(data)
        })
    }
}
pub fn delay(stop: &CancellationToken, seconds: f64) -> Result<()> {
    let deadline = std::time::Instant::now() + Duration::from_secs_f64(seconds.max(0.));
    while let Some(left) = deadline.checked_duration_since(std::time::Instant::now()) {
        cancel(stop)?;
        std::thread::sleep(left.min(Duration::from_millis(100)));
    }
    cancel(stop)
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 128
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}
#[derive(Clone, Serialize, Deserialize)]
struct Tokens {
    client_id: String,
    access_token: String,
    #[serde(default)]
    refresh_token: String,
    expires_at: f64,
    #[serde(default)]
    scope: String,
}
impl Tokens {
    fn grant(data: &Value, id: &str, prior: &str) -> Result<Self> {
        let access = data["access_token"]
            .as_str()
            .filter(|s| !s.is_empty())
            .context("iBroadcast returned no access token.")?;
        Ok(Self {
            client_id: id.into(),
            access_token: access.into(),
            refresh_token: data["refresh_token"]
                .as_str()
                .filter(|s| !s.is_empty())
                .unwrap_or(prior)
                .into(),
            expires_at: now() + positive(&data["expires_in"], 3600.),
            scope: text(&data["scope"]),
        })
    }
}
fn positive(value: &Value, default: f64) -> f64 {
    value
        .as_f64()
        .or_else(|| value.as_str()?.parse().ok())
        .filter(|n| n.is_finite() && *n > 0.)
        .unwrap_or(default)
}
#[derive(Clone, Serialize)]
pub struct Approval {
    pub code: String,
    pub url: String,
    pub plain_url: String,
}
fn verification(value: &Value) -> Option<String> {
    let url = Url::parse(value.as_str()?).ok()?;
    (url.scheme() == "https"
        && url.host_str() == Some("oauth.ibroadcast.com")
        && url.port().is_none()
        && url.path() == "/device"
        && url.username().is_empty()
        && url.password().is_none()
        && url.fragment().is_none())
    .then(|| url.into())
}
#[derive(Clone)]
pub struct Provider {
    pub http: Http,
    endpoints: Endpoints,
    home: PathBuf,
}
impl Provider {
    pub fn new(home: PathBuf) -> Result<Self> {
        Self::with_endpoints(home, Endpoints::default())
    }
    pub fn with_endpoints(home: PathBuf, endpoints: Endpoints) -> Result<Self> {
        Ok(Self {
            http: Http::new()?,
            endpoints,
            home,
        })
    }
    pub fn home(&self) -> &Path {
        &self.home
    }
    fn path(&self, name: &str) -> PathBuf {
        self.home.join(name)
    }
    fn oauth<T: DeserializeOwned>(
        &self,
        stop: &CancellationToken,
        form: &[(&str, &str)],
        path: &str,
    ) -> Result<(StatusCode, T)> {
        let response = self.http.send(
            stop,
            self.http
                .client
                .post(format!("{}/{}", self.endpoints.oauth, path))
                .form(form),
        )?;
        let status = response.status();
        let data = self.http.body(stop, response, 64 * 1024)?;
        Ok((
            status,
            serde_json::from_slice(&data)
                .map_err(|_| anyhow!("iBroadcast returned an invalid sign-in response."))?,
        ))
    }
    pub fn load(&self) -> Result<Option<Arc<Account>>> {
        let value = storage::read_json::<Tokens>(&self.path("tokens.json"))
            .map_err(|_| anyhow!("Stored sign-in is unreadable. Sign out and sign in again."))?;
        value
            .map(|tokens| {
                ensure!(
                    !tokens.access_token.is_empty() && valid_id(&tokens.client_id),
                    "Invalid stored sign-in."
                );
                Ok(Arc::new(Account {
                    provider: self.clone(),
                    tokens: Mutex::new(Some(tokens)),
                }))
            })
            .transpose()
    }
    pub fn connect(
        &self,
        client_id: Option<&str>,
        stop: &CancellationToken,
        show: impl FnOnce(Approval),
    ) -> Result<Arc<Account>> {
        let account = self.prepare_sign_in(client_id, stop, show)?;
        account.persist_sign_in(stop)?;
        Ok(account)
    }
    pub(crate) fn prepare_sign_in(
        &self,
        client_id: Option<&str>,
        stop: &CancellationToken,
        show: impl FnOnce(Approval),
    ) -> Result<Arc<Account>> {
        let runtime_client_id = std::env::var("IBROADCAST_CLIENT_ID").ok();
        let id = client_id
            .or(runtime_client_id
                .as_deref()
                .filter(|value| !value.is_empty()))
            .or(BUILT_IN_CLIENT_ID.filter(|value| !value.is_empty()))
            .unwrap_or(DEFAULT_CLIENT_ID);
        ensure!(valid_id(id), "Invalid iBroadcast client ID.");
        let response = self.http.send(
            stop,
            self.http
                .client
                .get(format!("{}/device/code", self.endpoints.oauth))
                .query(&[("client_id", id), ("scope", SCOPES)]),
        )?;
        let code = response.status();
        ensure!(
            code.is_success(),
            "iBroadcast refused the sign-in request (HTTP {code})."
        );
        let data: Value = serde_json::from_slice(&self.http.body(stop, response, 64 * 1024)?)
            .map_err(|_| anyhow!("iBroadcast returned an invalid device code."))?;
        let device = data["device_code"]
            .as_str()
            .filter(|s| !s.is_empty())
            .context("Missing device code.")?;
        let user = data["user_code"]
            .as_str()
            .filter(|s| !s.is_empty() && s.len() <= 64 && !s.chars().any(char::is_control))
            .context("Missing sign-in code.")?;
        let plain = verification(&data["verification_uri"]).unwrap_or_else(|| VERIFY.into());
        let url = verification(&data["verification_uri_complete"]).unwrap_or_else(|| plain.clone());
        show(Approval {
            code: user.into(),
            url,
            plain_url: plain,
        });
        let started = now();
        let ttl = positive(&data["expires_in"], 900.).min(3600.);
        let mut interval = positive(&data["interval"], 5.).clamp(1., 60.);
        while now() - started < ttl {
            delay(stop, interval.min((ttl - (now() - started)).max(0.)))?;
            if now() - started >= ttl {
                break;
            }
            let (status, reply): (StatusCode, Value) = self.oauth(
                stop,
                &[
                    ("grant_type", "device_code"),
                    ("client_id", id),
                    ("device_code", device),
                ],
                "token",
            )?;
            if status.is_success() {
                let tokens = Tokens::grant(&reply, id, "")?;
                cancel(stop)?;
                return Ok(Arc::new(Account {
                    provider: self.clone(),
                    tokens: Mutex::new(Some(tokens)),
                }));
            }
            match reply["error"].as_str() {
                Some("authorization_pending") => {}
                Some("slow_down") => interval += 5.,
                Some("access_denied") => bail!("iBroadcast sign-in was declined."),
                Some("expired_token") => break,
                _ => bail!("Could not complete iBroadcast sign-in (HTTP {status})."),
            }
        }
        bail!("The sign-in code expired. Start again.")
    }
    pub fn logout(&self, stop: &CancellationToken) -> Result<()> {
        let _lock =
            storage::target_lock(&self.path("session-locks"), &self.path("tokens.json"), stop)?;
        let path = self.path("tokens.json");
        let tokens = storage::read_json::<Tokens>(&path).ok().flatten();
        storage::remove_file(&path)?;
        for name in ["library-cache.json.gz", "library-cache.meta.json"] {
            storage::remove_file(&self.path(name))?;
        }
        drop(_lock);
        if let Some(token) = tokens.filter(|t| !t.refresh_token.is_empty()) {
            let _ = self.oauth::<Value>(
                stop,
                &[
                    ("client_id", &token.client_id),
                    ("refresh_token", &token.refresh_token),
                ],
                "revoke",
            );
        }
        Ok(())
    }
}

#[derive(Default, Serialize, Deserialize)]
struct Cache {
    user_id: String,
    lastmodified: String,
    #[serde(default)]
    expires: Value,
    fetched_at: f64,
}
pub struct Account {
    provider: Provider,
    tokens: Mutex<Option<Tokens>>,
}
impl Account {
    pub fn http(&self) -> &Http {
        &self.provider.http
    }
    pub fn home(&self) -> &Path {
        self.provider.home()
    }
    pub fn retire(&self) {
        if let Ok(mut state) = self.tokens.lock() {
            *state = None;
        }
    }
    fn current(&self) -> Result<Tokens> {
        self.tokens
            .lock()
            .map_err(|_| anyhow!("Sign-in unavailable."))?
            .clone()
            .context("Signed out.")
    }
    pub(crate) fn persist_sign_in(&self, stop: &CancellationToken) -> Result<()> {
        let path = self.provider.path("tokens.json");
        let _lock = storage::target_lock(&self.provider.path("session-locks"), &path, stop)?;
        cancel(stop)?;
        storage::write_json(&path, &self.current()?)
    }
    fn check_saved_session(&self) -> Result<()> {
        let current = self.current()?;
        let disk = storage::read_json::<Tokens>(&self.provider.path("tokens.json"))
            .map_err(|_| anyhow!("Stored sign-in is unreadable."))?
            .context("Signed out.")?;
        ensure!(
            current.client_id == disk.client_id && current.access_token == disk.access_token,
            "The saved sign-in changed. Reopen ibfetch."
        );
        Ok(())
    }
    fn refresh(&self, old: &Tokens, stop: &CancellationToken) -> Result<Tokens> {
        let path = self.provider.path("tokens.json");
        let _lock = storage::target_lock(&self.provider.path("session-locks"), &path, stop)?;
        let current = self.current()?;
        if current.access_token != old.access_token {
            return Ok(current);
        }
        let stored = storage::read_json::<Tokens>(&path)
            .map_err(|_| anyhow!("Stored sign-in is unreadable."))?
            .context("Signed out.")?;
        ensure!(
            stored.client_id == current.client_id,
            "The saved sign-in changed. Reopen ibfetch."
        );
        if stored.access_token != current.access_token {
            // Another process might have logged into a different account. Never
            // switch an active batch to those credentials.
            bail!("The saved sign-in changed. Reopen ibfetch.");
        }
        ensure!(
            !current.refresh_token.is_empty(),
            "Sign in again to renew this session."
        );
        let (status, data): (StatusCode, Value) = self.provider.oauth(
            &CancellationToken::new(),
            &[
                ("grant_type", "refresh_token"),
                ("client_id", &current.client_id),
                ("refresh_token", &current.refresh_token),
            ],
            "token",
        )?;
        if matches!(
            data["error"].as_str(),
            Some("invalid_grant" | "invalid_token")
        ) {
            return Err(Fatal("iBroadcast rejected this session. Sign in again.".into()).into());
        }
        ensure!(
            status.is_success(),
            "Sign-in renewal is temporarily unavailable. The saved sign-in was kept."
        );
        let next = Tokens::grant(&data, &current.client_id, &current.refresh_token)?;
        let mut state = self
            .tokens
            .lock()
            .map_err(|_| anyhow!("Sign-in unavailable."))?;
        ensure!(state.is_some(), "Signed out.");
        storage::write_json(&path, &next)?;
        *state = Some(next.clone());
        cancel(stop)?;
        Ok(next)
    }
    fn authorized(
        &self,
        stop: &CancellationToken,
        build: impl Fn(&str) -> Result<RequestBuilder>,
    ) -> Result<Response> {
        cancel(stop)?;
        let mut token = self.current()?;
        if !token.refresh_token.is_empty() && token.expires_at - now() < 60. {
            token = self.refresh(&token, stop)?;
        }
        for attempt in 0..2 {
            let response = self.http().send(stop, build(&token.access_token)?)?;
            if !matches!(response.status().as_u16(), 401 | 403) || attempt == 1 {
                return Ok(response);
            }
            drop(response);
            token = self.refresh(&token, stop)?;
        }
        unreachable!()
    }
    fn api(&self, mode: &str, stop: &CancellationToken) -> Result<(Value, Vec<u8>)> {
        let bulk = mode == "library";
        let (client, url, limit) = if bulk {
            (
                &self.http().bulk,
                self.provider.endpoints.library.clone(),
                LIBRARY_MAX,
            )
        } else {
            (
                &self.http().client,
                format!("{}/status", self.provider.endpoints.api),
                4 * 1024 * 1024,
            )
        };
        let body = json!({"mode":mode,"client":APP_SLUG,"version":VERSION,"device_name":APP_SLUG,"user_agent":USER_AGENT});
        for (attempt, wait) in [0., 2., 6.].into_iter().enumerate() {
            delay(stop, wait)?;
            let response = match self.authorized(stop, |token| {
                Ok(client.post(&url).bearer_auth(token).json(&body))
            }) {
                Ok(response) => response,
                Err(e)
                    if attempt < 2
                        && matches!(e.downcast_ref::<HttpError>(), Some(HttpError::Network)) =>
                {
                    continue;
                }
                Err(e) => return Err(e),
            };
            let status = response.status();
            if attempt < 2 && (status == StatusCode::TOO_MANY_REQUESTS || status.is_server_error())
            {
                continue;
            }
            ensure!(status.is_success(), "iBroadcast returned HTTP {status}.");
            let raw = match self.http().body(stop, response, limit) {
                Ok(raw) => raw,
                Err(e)
                    if attempt < 2
                        && matches!(e.downcast_ref::<HttpError>(), Some(HttpError::Network)) =>
                {
                    continue;
                }
                Err(e) => return Err(e),
            };
            let data: Value = serde_json::from_slice(&raw)
                .map_err(|_| anyhow!("Invalid iBroadcast library response."))?;
            if data["authenticated"] == false && attempt < 2 {
                let stale = self.current()?;
                self.refresh(&stale, stop)?;
                continue;
            }
            ensure!(
                data["authenticated"] != false && data["result"] != false,
                "iBroadcast refused the {mode} request."
            );
            return Ok((data, raw));
        }
        bail!("iBroadcast did not answer.")
    }
    pub fn library(&self, refresh: bool, stop: &CancellationToken) -> Result<Library> {
        let (status, _) = self.api("status", stop)?;
        let owner = text(&status["user"]["id"]);
        let owner = if owner.trim().is_empty() {
            text(&status["user"]["user_id"])
        } else {
            owner
        };
        let owner = owner.trim().to_owned();
        ensure!(
            !owner.is_empty(),
            "iBroadcast did not return an account ID."
        );
        let username = text(&status["user"]["username"]);
        let premium = status["user"]["premium"] == true
            || status["user"]["premium"] == 1
            || status["user"]["premium"] == "1";
        let revision = text(&status["status"]["lastmodified"]);
        let cache = self.provider.path("library-cache.json.gz");
        let stamp = self.provider.path("library-cache.meta.json");
        {
            let _lock = storage::target_lock(
                &self.provider.path("session-locks"),
                &self.provider.path("tokens.json"),
                stop,
            )?;
            self.check_saved_session()?;
            if !refresh
                && !revision.is_empty()
                && let Ok(Some(meta)) = storage::read_json::<Cache>(&stamp)
                && meta.user_id == owner
                && meta.lastmodified == revision
                && (positive(&meta.expires, 0.) - now() > 3600.
                    || positive(&meta.expires, 0.) == 0. && now() - meta.fetched_at < 21600.)
                && let Ok(file) = File::open(&cache)
            {
                let mut data = Vec::new();
                if GzDecoder::new(file)
                    .take(LIBRARY_MAX as u64 + 1)
                    .read_to_end(&mut data)
                    .is_ok()
                    && data.len() <= LIBRARY_MAX
                    && let Ok(value) = serde_json::from_slice(&data)
                    && let Ok(lib) = Library::parse(value, owner.clone(), username.clone(), premium)
                {
                    cancel(stop)?;
                    self.current()?;
                    return Ok(lib);
                }
            }
        }
        let (data, raw) = self.api("library", stop)?;
        let fetched = text(&data["status"]["lastmodified"]);
        let library = Library::parse(data, owner.clone(), username, premium)?;
        let _lock = storage::target_lock(
            &self.provider.path("session-locks"),
            &self.provider.path("tokens.json"),
            stop,
        )?;
        self.check_saved_session()?;
        cancel(stop)?;
        storage::remove_file(&stamp)?;
        storage::write_private(&cache, |file| {
            use std::io::Write;
            let mut zip = GzEncoder::new(file, Compression::new(5));
            for part in raw.chunks(256 * 1024) {
                cancel(stop)?;
                zip.write_all(part)?;
            }
            zip.finish()?;
            cancel(stop)?;
            self.check_saved_session()?;
            Ok(())
        })?;
        cancel(stop)?;
        self.check_saved_session()?;
        storage::write_json(
            &stamp,
            &Cache {
                user_id: owner,
                lastmodified: if fetched.is_empty() {
                    revision
                } else {
                    fetched
                },
                expires: json!(library.expires),
                fetched_at: now(),
            },
        )?;
        Ok(library)
    }
    pub fn original(&self, owner: &str, id: u64, stop: &CancellationToken) -> Result<Response> {
        let request = |token: &str| {
            Ok(self
                .http()
                .export
                .post(&self.provider.endpoints.download)
                .form(&[
                    ("_user_id", owner),
                    ("_token", token),
                    ("mode", "downloadlibrary"),
                    ("container_type", "track"),
                    ("ids", &id.to_string()),
                    ("end_marker", "1"),
                ]))
        };
        let response = self.authorized(stop, request)?;
        let code = response.status();
        if code == StatusCode::UNAUTHORIZED || code == StatusCode::FORBIDDEN {
            return Err(Fatal("iBroadcast rejected the session. Sign in again.".into()).into());
        }
        if code == StatusCode::TOO_MANY_REQUESTS || code.is_server_error() {
            return Err(Retry("The download service is temporarily unavailable.").into());
        }
        // This service may reject authorization in a JSON response with HTTP 200.
        if !is_json(&response) {
            return Ok(response);
        }
        let body = self.http().body(stop, response, 16 * 1024)?;
        if serde_json::from_slice::<Value>(&body).is_ok_and(|v| v["authenticated"] == false) {
            let token = self.refresh(&self.current()?, stop)?;
            let response = self.http().send(stop, request(&token.access_token)?)?;
            if is_json(&response) {
                let body = self.http().body(stop, response, 16 * 1024)?;
                if serde_json::from_slice::<Value>(&body).is_ok_and(|v| v["authenticated"] == false)
                {
                    return Err(
                        Fatal("iBroadcast rejected the session. Sign in again.".into()).into(),
                    );
                }
                bail!("iBroadcast returned a non-audio response.");
            }
            return Ok(response);
        }
        bail!("iBroadcast refused the original file (HTTP {code}).")
    }
    pub fn stream(
        &self,
        owner: &str,
        expires: &str,
        id: u64,
        path: &str,
        offset: u64,
        stop: &CancellationToken,
    ) -> Result<Response> {
        ensure!(
            path.starts_with('/')
                && !path.starts_with("//")
                && !path.contains(['?', '#', '\\'])
                && !path.split('/').any(|p| matches!(p, "." | "..")),
            "Invalid iBroadcast stream path."
        );
        let url = Url::parse(&format!("{}{}", self.provider.endpoints.stream, path))
            .map_err(|_| anyhow!("Invalid iBroadcast stream path."))?;
        self.authorized(stop, |token| {
            let mut request = self
                .http()
                .stream
                .get(url.clone())
                .header("Accept-Encoding", "identity")
                .query(&[
                    ("Signature", token),
                    ("file_id", &id.to_string()),
                    ("user_id", owner),
                    ("platform", "ibfetch"),
                    ("version", VERSION),
                ]);
            if !expires.is_empty() {
                request = request.query(&[("Expires", expires)]);
            }
            if offset > 0 {
                request = request.header("Range", format!("bytes={offset}-"));
            }
            Ok(request)
        })
    }
}
fn is_json(r: &Response) -> bool {
    r.headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|x| x.to_str().ok())
        .and_then(|x| x.split(';').next())
        .is_some_and(|x| x.trim() == "application/json" || x.trim().ends_with("+json"))
}
