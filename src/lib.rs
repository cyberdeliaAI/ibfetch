pub mod account;
pub mod browser;
pub mod catalog;
pub mod downloads;
pub mod model;
pub mod storage;
pub mod update;

pub const APP_NAME: &str = "ibfetch";
pub const APP_SLUG: &str = env!("CARGO_PKG_NAME");
pub const MAC_BUNDLE_ID: &str = "nl.cyberdelia.ibfetch";
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub const QUALITIES: &[&str] = &["orig", "320", "256", "192", "128", "96"];

pub fn random_hex(bytes: usize) -> anyhow::Result<String> {
    let mut data = vec![0; bytes];
    getrandom::fill(&mut data)
        .map_err(|_| anyhow::anyhow!("Could not generate a private session identifier."))?;
    Ok(data.iter().map(|b| format!("{b:02x}")).collect())
}
