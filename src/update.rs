use crate::{VERSION, storage};
use anyhow::{Result, ensure};
use reqwest::{Client, redirect::Policy};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

pub const RELEASES: &str = "https://github.com/cyberdeliaAI/ibfetch/releases";
const LATEST: &str = "https://api.github.com/repos/cyberdeliaAI/ibfetch/releases/latest";
const LIMIT: usize = 1024 * 1024;

#[derive(Clone, Default, Deserialize, Serialize)]
struct Release {
    version: String,
    #[serde(default)]
    name: String,
    #[serde(default)]
    notes: String,
}
#[derive(Default, Deserialize, Serialize)]
struct Cache {
    #[serde(default)]
    attempted_at: f64,
    #[serde(default)]
    checked_at: Option<f64>,
    #[serde(default)]
    latest: Option<Release>,
    #[serde(default)]
    error: bool,
}
struct State {
    cache: Cache,
    automatic: bool,
    checking: bool,
}
#[derive(Clone)]
pub struct Checker {
    state: Arc<Mutex<State>>,
    path: PathBuf,
}
impl Checker {
    pub fn new(path: PathBuf, automatic: bool) -> Self {
        let cache = storage::read_json(&path).ok().flatten().unwrap_or_default();
        Self {
            state: Arc::new(Mutex::new(State {
                cache,
                automatic,
                checking: false,
            })),
            path,
        }
    }
    pub fn enable(&self, automatic: bool) {
        self.state.lock().unwrap().automatic = automatic;
    }
    pub fn check(&self, manual: bool) {
        let mut state = self.state.lock().unwrap();
        if state.checking
            || !manual && (!state.automatic || now() - state.cache.attempted_at < 86400.)
        {
            return;
        }
        state.checking = true;
        state.cache.attempted_at = now();
        drop(state);
        let this = self.clone();
        std::thread::spawn(move || {
            let result = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map(|rt| rt.block_on(latest()));
            let mut state = this.state.lock().unwrap();
            state.checking = false;
            match result {
                Ok(Ok(release)) => {
                    state.cache.latest = Some(release);
                    state.cache.checked_at = Some(now());
                    state.cache.error = false;
                }
                _ => state.cache.error = true,
            }
            let _ = storage::write_json(&this.path, &state.cache);
        });
    }
    pub fn snapshot(&self) -> Value {
        let s = self.state.lock().unwrap();
        let release = s
            .cache
            .latest
            .as_ref()
            .filter(|r| stable(&r.version).is_some());
        let available = release.is_some_and(|r| newer_stable_release(VERSION, &r.version));
        let url = if available {
            format!("{RELEASES}/tag/v{}", release.unwrap().version)
        } else {
            format!("{RELEASES}/latest")
        };
        json!({"current":VERSION,"latest":release.map(|r|&r.version),"available":available,
            "url":url,"notes":release.map(|r|r.notes.as_str()).unwrap_or_default(),
            "name":release.map(|r|r.name.as_str()).unwrap_or_default(),
            "automatic":s.automatic,"checking":s.checking,"checked_at":s.cache.checked_at,"error":s.cache.error})
    }
}
fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
fn stable(value: &str) -> Option<(u32, u32, u32)> {
    let mut components = value.split('.');
    let mut part = || {
        let n = components.next()?;
        if n.len() > 6
            || n.is_empty()
            || n.len() > 1 && n.starts_with('0')
            || !n.bytes().all(|b| b.is_ascii_digit())
        {
            return None;
        }
        n.parse().ok()
    };
    let version = (part()?, part()?, part()?);
    components.next().is_none().then_some(version)
}
fn newer_stable_release(current: &str, candidate: &str) -> bool {
    let (base, prerelease) = current
        .split_once('-')
        .map_or((current, false), |(base, _)| (base, true));
    stable(base)
        .zip(stable(candidate))
        .is_some_and(|(base, candidate)| candidate > base || (prerelease && candidate == base))
}

async fn latest() -> Result<Release> {
    let client = Client::builder()
        .no_proxy()
        .redirect(Policy::none())
        .timeout(Duration::from_secs(8))
        .user_agent(crate::account::USER_AGENT)
        .build()?;
    let mut response = client
        .get(LATEST)
        .header("Accept", "application/vnd.github+json")
        .send()
        .await?;
    ensure!(response.status().is_success(), "Could not check releases.");
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await? {
        ensure!(
            bytes.len() + chunk.len() <= LIMIT,
            "Release response is too large."
        );
        bytes.extend(chunk);
    }
    let data: Value = serde_json::from_slice(&bytes)?;
    ensure!(
        data["draft"] == false && data["prerelease"] == false,
        "Unsupported release."
    );
    let version = data["tag_name"]
        .as_str()
        .and_then(|s| s.strip_prefix('v'))
        .unwrap_or_default();
    ensure!(stable(version).is_some(), "Invalid release version.");
    let bound = |k: &str, n| {
        data[k]
            .as_str()
            .unwrap_or_default()
            .chars()
            .take(n)
            .collect()
    };
    Ok(Release {
        version: version.into(),
        name: bound("name", 200),
        notes: bound("body", 50000),
    })
}

#[cfg(test)]
mod tests {
    use super::newer_stable_release;

    #[test]
    fn preview_can_upgrade_to_same_stable_version() {
        assert!(newer_stable_release("2.0.0-alpha.1", "2.0.0"));
        assert!(newer_stable_release("2.0.0", "2.0.1"));
        assert!(!newer_stable_release("2.0.0", "2.0.0"));
        assert!(!newer_stable_release("2.0.0-alpha.1", "1.7.1"));
        assert!(!newer_stable_release("2.0.0-alpha.1", "2.0.0-beta.1"));
    }
}
