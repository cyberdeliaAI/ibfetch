use anyhow::{Context, Result, ensure};
use clap::{Args, Parser, Subcommand};
use ibfetch::{
    account::{Account, Provider},
    browser,
    catalog::{DEFAULT_LAYOUT, Filters, Group, Layout, Library, fold},
    downloads::{self, Event, Options, Source, State},
    storage,
};
use std::{
    io::{self, IsTerminal, Write},
    path::PathBuf,
    sync::Arc,
};
use tokio_util::sync::CancellationToken;
#[cfg(feature = "desktop")]
mod desktop;

#[derive(Parser)]
#[command(version, about, long_about=None)]
struct Cli {
    #[arg(long, global = true)]
    home: Option<PathBuf>,
    #[command(subcommand)]
    command: Option<Command>,
}
#[derive(Subcommand)]
enum Command {
    /// Launch the native desktop application.
    Desktop,
    /// Open ibfetch in your browser.
    #[command(alias = "browser")]
    Gui {
        #[arg(long, default_value_t = 8765)]
        port: u16,
        #[arg(long)]
        no_browser: bool,
    },
    /// Sign in to iBroadcast with a device code.
    Login {
        #[arg(long)]
        client_id: Option<String>,
    },
    /// Remove this app's saved iBroadcast sign-in.
    Logout,
    /// List album artists and album counts.
    Artists {
        patterns: Vec<String>,
        #[arg(long)]
        skip_the: bool,
        #[arg(long)]
        refresh: bool,
    },
    /// List the albums and tracks selected by your filters.
    List(Selection),
    /// Download the albums and tracks selected by your filters.
    Get(Get),
}
#[derive(Args)]
struct Selection {
    #[command(flatten)]
    filters: Filters,
    #[arg(long)]
    tracks: bool,
    #[arg(long)]
    refresh: bool,
}
#[derive(Args)]
struct Get {
    #[command(flatten)]
    selection: Selection,
    #[arg(short, long, default_value = "iBroadcast")]
    out: PathBuf,
    #[arg(short,long,default_value="orig",value_parser=["orig","96","128","192","256","320"])]
    quality: String,
    #[arg(long, value_enum)]
    source: Option<Source>,
    #[arg(short,long,default_value_t=3,value_parser=clap::value_parser!(u8).range(1..=8))]
    jobs: u8,
    #[arg(long,default_value=DEFAULT_LAYOUT)]
    layout: String,
    #[arg(short = 'n', long)]
    dry_run: bool,
    #[arg(long, requires = "dry_run")]
    json: bool,
    #[arg(long)]
    yes: bool,
    #[arg(long)]
    verify: bool,
    #[arg(long)]
    adopt_existing: bool,
    #[arg(long)]
    work_folder: Option<PathBuf>,
}
fn main() -> std::process::ExitCode {
    match run() {
        Ok(code) => std::process::ExitCode::from(code),
        Err(e) => {
            eprintln!("{}: {e}", ibfetch::APP_SLUG);
            std::process::ExitCode::FAILURE
        }
    }
}
fn run() -> Result<u8> {
    let cli =
        Cli::parse_from(std::env::args_os().filter(|arg| {
            !cfg!(target_os = "macos") || !arg.to_string_lossy().starts_with("-psn_")
        }));
    let home = storage::home_dir(cli.home.as_deref())?;
    let stop = CancellationToken::new();
    let ctrl = stop.clone();
    ctrlc::set_handler(move || ctrl.cancel()).context("Could not listen for Ctrl+C.")?;
    let command = cli.command.unwrap_or(if cfg!(feature = "desktop") {
        Command::Desktop
    } else {
        Command::Gui {
            port: 8765,
            no_browser: false,
        }
    });
    #[cfg(debug_assertions)]
    let provider = if let Ok(address) = std::env::var("IBFETCH_RUST_TEST_PROVIDER") {
        ensure!(
            cli.home.is_some(),
            "Fixtures require an explicit disposable --home."
        );
        Provider::with_endpoints(home, ibfetch::account::Endpoints::loopback(&address)?)?
    } else {
        Provider::new(home)?
    };
    #[cfg(not(debug_assertions))]
    let provider = Provider::new(home)?;
    match command {
        Command::Desktop => {
            #[cfg(feature = "desktop")]
            desktop::run(provider, stop)?;
            #[cfg(not(feature = "desktop"))]
            anyhow::bail!("This build has no desktop interface. Run ibfetch gui.");
        }
        Command::Gui { port, no_browser } => browser::run(provider, port, !no_browser, stop)?,
        Command::Login { client_id } => {
            provider.connect(client_id.as_deref(), &stop, |approval| {
                println!("Open {} and approve code {}.", approval.url, approval.code);
                let _ = browser::open_url(&approval.url);
            })?;
            println!("Signed in to iBroadcast.");
        }
        Command::Logout => {
            provider.logout(&stop)?;
            println!("Signed out. The cached library index was removed.");
        }
        Command::Artists {
            patterns,
            skip_the,
            refresh,
        } => {
            let (_, lib) = connected(&provider, refresh, &stop)?;
            for (name, count) in lib.artists() {
                if patterns.is_empty()
                    || patterns
                        .iter()
                        .any(|p| Library::matches_artist(p, &name, skip_the))
                {
                    println!("{count:>5}  {name}");
                }
            }
        }
        Command::List(selection) => {
            let (_, lib) = connected(&provider, selection.refresh, &stop)?;
            let groups = lib.select(&selection.filters, &stop)?;
            print_groups(&lib, &groups, selection.tracks);
            if groups.is_empty() {
                return Ok(1);
            }
        }
        Command::Get(args) => return get(provider, args, &stop),
    }
    Ok(0)
}
fn connected(
    provider: &Provider,
    refresh: bool,
    stop: &CancellationToken,
) -> Result<(Arc<Account>, Library)> {
    let account = provider
        .load()?
        .context("Sign in first with: ibfetch login")?;
    let library = account.library(refresh, stop)?;
    Ok((account, library))
}
fn print_groups(lib: &Library, groups: &[Group], tracks: bool) {
    for group in groups {
        let year = lib.year(group.id, group.tracks.first().copied());
        let year = if year == 0 {
            String::new()
        } else {
            format!(" ({year})")
        };
        let disc = if lib.multi_disc(group.id) {
            format!(" [disc {}]", lib.disc(group.id))
        } else {
            String::new()
        };
        println!(
            "{} — {}{year}{disc} [{} tracks, album {}]",
            lib.album_artist(group.id),
            lib.album_title(group.id),
            group.tracks.len(),
            group.id
        );
        if tracks {
            for &id in &group.tracks {
                println!(
                    "  {:02} {} ({}, track {id})",
                    lib.track_number(id),
                    lib.track_title(id),
                    lib.format(id)
                );
            }
        }
    }
    if groups.is_empty() {
        println!("Nothing matches these filters.");
    }
}
fn get(provider: Provider, args: Get, stop: &CancellationToken) -> Result<u8> {
    let source = Source::choose(args.source, &args.quality)?;
    let layout = Layout::new(&args.layout)?;
    let (account, lib) = connected(&provider, args.selection.refresh, stop)?;
    let groups = lib.select(&args.selection.filters, stop)?;
    if groups.is_empty() {
        if args.json {
            println!("[]");
        } else {
            println!("Nothing matches these filters.");
        }
        return Ok(1);
    }
    let jobs = lib.plan(&groups, &args.quality, &layout, stop)?;
    let opts = Options {
        out: args.out,
        work: args.work_folder,
        quality: args.quality,
        source,
        workers: args.jobs as usize,
        verify: args.verify,
        adopt: args.adopt_existing,
    };
    if args.dry_run {
        if args.json {
            let rows = jobs
                .iter()
                .map(|j| serde_json::json!({"track_id":j.track_id,"path":j.relative}))
                .collect::<Vec<_>>();
            println!("{}", serde_json::to_string(&rows)?);
        } else {
            for job in &jobs {
                println!("{}", opts.out.join(&job.relative).display());
            }
            println!("{} tracks would download.", jobs.len());
        }
        return Ok(0);
    }
    if jobs.len() > 1000 && !args.yes {
        ensure!(
            io::stdin().is_terminal(),
            "Pass --yes to download more than 1000 tracks non-interactively."
        );
        print!("Download {} tracks? [y/N] ", jobs.len());
        io::stdout().flush()?;
        let mut answer = String::new();
        io::stdin().read_line(&mut answer)?;
        if !matches!(fold(answer.trim()).as_str(), "yes" | "y") {
            return Ok(0);
        }
    }
    let summary = downloads::run(&account, &lib, &jobs, opts, stop, |event| {
        if let Event::Finished(_, path, result) = event {
            let name = match result.state {
                State::Saved => "Saved",
                State::Skipped => "Skipped",
                State::Failed => "Failed",
                State::Stopped => "Stopped",
            };
            let note = if result.note.is_empty() {
                String::new()
            } else {
                format!(" — {}", result.note)
            };
            println!("{name}: {}{note}", path.display());
        }
    })?;
    println!(
        "{} saved, {} skipped, {} failed, {} stopped.",
        summary.saved, summary.skipped, summary.failed, summary.stopped
    );
    Ok(u8::from(summary.failed + summary.stopped > 0))
}
