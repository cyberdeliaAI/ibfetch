use anyhow::{Result, anyhow};
use eframe::egui::{
    self, Align, Align2, Color32, CursorIcon, Layout, Margin, Rect, RichText, Sense, Stroke, Vec2,
    pos2, vec2,
};
use ibfetch::{
    APP_NAME, MAC_BUNDLE_ID, QUALITIES, VERSION,
    account::Provider,
    browser,
    catalog::{FIELDS, Filters, Group, artist_initial, fold},
    model::{Album, Model, Settings, View},
};
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    path::Path,
    sync::mpsc::{self, Receiver},
    time::{Duration, Instant},
};
use tokio_util::sync::CancellationToken;

mod art;
mod notes;
mod rows;
mod text;
mod theme;
mod transfer;
use rows::{
    Columns, RowAction, RowState, album_row, artist_row, letter_button, queue_row, track_list,
};
use text::{Natural, num, plural, span};
use theme::{Palette, fit, paint};

const ICON: &[u8] = include_bytes!("../assets/ibfetch-icon.png");
const TESTS: &str = "https://github.com/cyberdeliaAI/ibfetch/actions/workflows/tests.yml";
const LICENSE: &str = "https://github.com/cyberdeliaAI/ibfetch/blob/main/LICENSE";
const HINT: &str = "* matches anything, so A* finds every name starting with A.";
const ROW: f32 = 34.;
const TRACK: f32 = 22.;

pub fn run(provider: Provider, stop: CancellationToken) -> Result<()> {
    let mut viewport = egui::ViewportBuilder::default()
        .with_inner_size([1320., 850.])
        .with_min_inner_size([620., 560.])
        .with_title(APP_NAME)
        .with_app_id(MAC_BUNDLE_ID);
    if let Ok(icon) = eframe::icon_data::from_png_bytes(ICON) {
        viewport = viewport.with_icon(icon);
    }
    let path = provider.home().join("desktop-window.ron");
    let model = Model::new(provider);
    model.updates.check(false);
    let native = eframe::NativeOptions {
        viewport,
        persistence_path: Some(path),
        ..Default::default()
    };
    eframe::run_native(
        APP_NAME,
        native,
        Box::new(move |cc| Ok(Box::new(Desktop::new(model, stop, &cc.egui_ctx)))),
    )
    .map_err(|e| anyhow!("Could not open the desktop app: {e}"))
}
#[derive(Default)]
struct SearchFields {
    artist: String,
    album: String,
    track_artist: String,
    year: String,
    genre: String,
    format: String,
    tag: String,
    playlist: String,
    skip_the: bool,
}
impl SearchFields {
    fn any(&self) -> bool {
        [
            &self.artist,
            &self.album,
            &self.track_artist,
            &self.year,
            &self.genre,
            &self.format,
            &self.tag,
            &self.playlist,
        ]
        .iter()
        .any(|value| !value.trim().is_empty())
    }
    fn filters(&self, initial: Option<char>, all: bool) -> Filters {
        let one = |s: &str| {
            if s.trim().is_empty() {
                vec![]
            } else {
                vec![s.trim().into()]
            }
        };
        Filters {
            artist: one(&self.artist),
            album: one(&self.album),
            track_artist: one(&self.track_artist),
            year: one(&self.year),
            genre: one(&self.genre),
            format: one(&self.format),
            tag: one(&self.tag),
            playlist: one(&self.playlist),
            skip_the: self.skip_the,
            artist_initial: initial,
            all,
            ..Filters::default()
        }
    }
}
#[derive(Clone, Copy, PartialEq)]
enum Order {
    Artist,
    Album,
    Year,
    Tracks,
    Format,
    Length,
}
impl Order {
    const ALL: [Self; 6] = [
        Self::Artist,
        Self::Album,
        Self::Year,
        Self::Tracks,
        Self::Format,
        Self::Length,
    ];
    fn label(self) -> &'static str {
        match self {
            Self::Artist => "Artist",
            Self::Album => "Album",
            Self::Year => "Year",
            Self::Tracks => "Tracks",
            Self::Format => "Format",
            Self::Length => "Length",
        }
    }
    fn numeric(self) -> bool {
        matches!(self, Self::Year | Self::Tracks | Self::Length)
    }
}
#[derive(PartialEq, Eq, PartialOrd, Ord)]
enum Primary {
    Number(u64),
    Text(Natural),
}
/// The chosen column first, then artist, title, disc and id, always ascending.
#[derive(PartialEq, Eq)]
struct RowKey {
    primary: Primary,
    descending: bool,
    rest: (Natural, Natural, u64, u64),
}
impl Ord for RowKey {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        let first = self.primary.cmp(&other.primary);
        if self.descending {
            first.reverse()
        } else {
            first
        }
        .then_with(|| self.rest.cmp(&other.rest))
    }
}
impl PartialOrd for RowKey {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}
fn order_albums(albums: &mut [Album], column: Order, descending: bool) {
    albums.sort_by_cached_key(|a| RowKey {
        primary: match column {
            Order::Artist => Primary::Text(Natural::new(&a.artist)),
            Order::Album => Primary::Text(Natural::new(&a.title)),
            Order::Format => Primary::Text(Natural::new(&a.formats)),
            Order::Year => Primary::Number(a.year),
            Order::Tracks => Primary::Number(a.tracks.len() as u64),
            Order::Length => Primary::Number(a.length),
        },
        descending,
        rest: (
            Natural::new(&a.artist),
            Natural::new(&a.title),
            a.disc,
            a.id,
        ),
    });
}

struct Queued {
    id: u64,
    artist: String,
    title: String,
    year: u64,
    disc: u64,
    lengths: BTreeMap<u64, u64>,
}
/// The download list, in the order albums were added.
#[derive(Default)]
struct Queue {
    items: Vec<Queued>,
    index: HashMap<u64, usize>,
    tracks: u64,
    length: u64,
}
impl Queue {
    fn contains(&self, id: u64) -> bool {
        self.index.contains_key(&id)
    }
    /// Returns whether the album is new to the list, and how many tracks it added.
    fn add(&mut self, album: &Album) -> (bool, u64) {
        let before = self.tracks;
        let new = !self.index.contains_key(&album.id);
        let slot = *self.index.entry(album.id).or_insert(self.items.len());
        if new {
            self.items.push(Queued {
                id: album.id,
                artist: album.artist.clone(),
                title: album.title.clone(),
                year: album.year,
                disc: album.disc,
                lengths: BTreeMap::new(),
            });
        }
        for &track in &album.tracks {
            let seconds = album.track_lengths.get(&track).copied().unwrap_or(0);
            match self.items[slot].lengths.insert(track, seconds) {
                // A refreshed library can give a listed track another length.
                Some(old) => self.length = self.length - old + seconds,
                None => {
                    self.tracks += 1;
                    self.length += seconds;
                }
            }
        }
        (new, self.tracks - before)
    }
    fn remove(&mut self, id: u64) {
        if let Some(slot) = self.index.remove(&id) {
            let gone = self.items.remove(slot);
            self.tracks -= gone.lengths.len() as u64;
            self.length -= gone.lengths.values().sum::<u64>();
            for (i, item) in self.items.iter().enumerate().skip(slot) {
                self.index.insert(item.id, i);
            }
        }
    }
    fn clear(&mut self) {
        *self = Self::default();
    }
    fn groups(&self) -> Vec<Group> {
        self.items
            .iter()
            .map(|item| Group {
                id: item.id,
                tracks: item.lengths.keys().copied().collect(),
            })
            .collect()
    }
}

#[derive(Default)]
enum Confirm {
    #[default]
    None,
    Download,
    SignOut,
    Close,
}
#[derive(Clone, Copy, PartialEq)]
enum Tab {
    Library,
    Downloads,
}
struct Toast {
    text: String,
    bad: bool,
    until: Instant,
}
struct Artist {
    name: String,
    albums: u64,
    folded: String,
    // The letter the artist is filed under, as typed and with a leading "The" ignored.
    initials: [char; 2],
}
struct Desktop {
    model: Model,
    stop: CancellationToken,
    settings: Settings,
    view: View,
    pal: Palette,
    art: art::Art,
    metadata_revision: Option<u64>,
    artists: Vec<Artist>,
    artist_find: String,
    letter: Option<char>,
    active_artist: Option<String>,
    active_initial: Option<char>,
    genres: Vec<String>,
    formats: Vec<String>,
    tags: Vec<String>,
    playlists: Vec<String>,
    counts: (u64, u64),
    fields: SearchFields,
    genre_open: bool,
    results: Vec<Album>,
    result_tracks: u64,
    result_length: u64,
    searched: bool,
    failure: String,
    scroll_top: bool,
    checked: BTreeSet<u64>,
    last_click: Option<usize>,
    expanded: BTreeSet<u64>,
    queue: Queue,
    pending: Option<(CancellationToken, Receiver<Result<Vec<Album>>>)>,
    order: Option<Order>,
    descending: bool,
    verify: bool,
    adopt: bool,
    show_updates: bool,
    open_update_panel: bool,
    release_notes: (String, Vec<notes::Block>),
    confirm: Confirm,
    close_after_stop: bool,
    tab: Tab,
    toast: Option<Toast>,
    opened_login: String,
}
impl Desktop {
    fn new(model: Model, stop: CancellationToken, ctx: &egui::Context) -> Self {
        let settings = model.settings();
        theme::install_fonts(ctx);
        theme::apply(ctx, &settings);
        let view = model.view();
        Self {
            pal: Palette::current(ctx, &settings),
            art: art::Art::new(ctx),
            model,
            stop,
            settings,
            view,
            metadata_revision: None,
            artists: vec![],
            artist_find: String::new(),
            letter: None,
            active_artist: None,
            active_initial: None,
            genres: vec![],
            formats: vec![],
            tags: vec![],
            playlists: vec![],
            counts: (0, 0),
            fields: SearchFields::default(),
            genre_open: false,
            results: vec![],
            result_tracks: 0,
            result_length: 0,
            searched: false,
            failure: String::new(),
            scroll_top: false,
            checked: BTreeSet::new(),
            last_click: None,
            expanded: BTreeSet::new(),
            queue: Queue::default(),
            pending: None,
            order: None,
            descending: false,
            verify: false,
            adopt: false,
            show_updates: false,
            open_update_panel: false,
            release_notes: (String::new(), vec![]),
            confirm: Confirm::None,
            close_after_stop: false,
            tab: Tab::Library,
            toast: None,
            opened_login: String::new(),
        }
    }
    fn notice(&mut self, text: impl Into<String>, bad: bool) {
        self.toast = Some(Toast {
            text: text.into(),
            bad,
            until: Instant::now() + Duration::from_secs(if bad { 7 } else { 4 }),
        });
    }
    fn result(&mut self, result: Result<()>) {
        if let Err(e) = result {
            self.notice(e.to_string(), true);
        }
    }
    fn set_results(&mut self, rows: Vec<Album>) {
        self.result_tracks = rows.iter().map(|a| a.tracks.len() as u64).sum();
        self.result_length = rows.iter().map(|a| a.length).sum();
        self.results = rows;
        self.checked.clear();
        self.expanded.clear();
        self.last_click = None;
        self.scroll_top = true;
    }
    fn reset_library_view(&mut self) {
        self.cancel_search();
        self.set_results(vec![]);
        self.searched = false;
        self.failure.clear();
    }
    fn refresh_view(&mut self) {
        let view = self.model.view();
        if view.epoch != self.view.epoch {
            self.reset_library_view();
            self.artists.clear();
            self.fields = SearchFields::default();
            self.artist_find.clear();
            self.letter = None;
            self.active_artist = None;
            self.active_initial = None;
            self.queue.clear();
            self.tab = Tab::Library;
            self.toast = None;
        }
        let metadata_revision = view.library.as_ref().map(|_| view.revision);
        if metadata_revision != self.metadata_revision {
            self.metadata_revision = metadata_revision;
            self.reset_library_view();
            let meta = &view.metadata;
            self.artists = meta["artists"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|row| {
                    let name = row[0].as_str()?;
                    Some(Artist {
                        name: name.into(),
                        albums: row[1].as_u64()?,
                        folded: fold(name),
                        initials: [artist_initial(name, false), artist_initial(name, true)],
                    })
                })
                .collect();
            let strings = |name: &str| {
                meta[name]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(|v| v.as_str().map(str::to_owned))
                    .collect()
            };
            self.genres = strings("genres");
            self.formats = strings("formats");
            self.tags = strings("tags");
            self.playlists = strings("playlists");
            self.counts = (
                meta["counts"]["tracks"].as_u64().unwrap_or(0),
                meta["counts"]["albums"].as_u64().unwrap_or(0),
            );
        }
        // Open the provider page once for each sign-in this window started.
        if view.login["state"] == "waiting"
            && let Some(id) = view.login["request_id"].as_str()
            && id != self.opened_login
            && let Some(url) = view.login["url"].as_str()
        {
            self.opened_login = id.into();
            let opened = browser::open_url(url);
            self.result(opened);
        }
        self.view = view;
    }
    fn refresh_library(&mut self) {
        match self.model.load(true) {
            Ok(()) => self.notice("Fetching the library index again.", false),
            Err(e) => self.notice(e.to_string(), true),
        }
    }
    fn free_account(&self) -> bool {
        self.view.library.as_ref().is_some_and(|lib| !lib.premium)
    }
    fn cancel_search(&mut self) {
        if let Some((token, _)) = self.pending.take() {
            token.cancel();
        }
    }
    fn search(&mut self, ctx: &egui::Context, all: bool) {
        if !all && !self.fields.any() && self.active_initial.is_none() {
            self.notice("Pick an artist or fill in a field first.", false);
            return;
        }
        if self.view.library.is_none() {
            self.notice("The library is still loading.", false);
            return;
        }
        self.cancel_search();
        let filters = self.fields.filters(self.active_initial, all);
        let stop = CancellationToken::new();
        let (tx, rx) = mpsc::channel();
        let cancel = stop.clone();
        let model = self.model.clone();
        let ctx = ctx.clone();
        self.pending = Some((stop, rx));
        std::thread::spawn(move || {
            let _ = tx.send(model.search(&filters, &cancel));
            ctx.request_repaint();
        });
    }
    fn drain_search(&mut self) {
        if let Some((_, rx)) = &self.pending {
            match rx.try_recv() {
                Ok(value) => {
                    self.pending = None;
                    self.searched = true;
                    match value {
                        Ok(mut rows) => {
                            if let Some(column) = self.order {
                                order_albums(&mut rows, column, self.descending);
                            }
                            self.failure.clear();
                            self.set_results(rows);
                        }
                        Err(e) => {
                            self.failure = e.to_string();
                            self.set_results(vec![]);
                            self.notice(e.to_string(), true);
                        }
                    }
                }
                Err(mpsc::TryRecvError::Disconnected) => {
                    self.pending = None;
                    self.notice("Search ended unexpectedly.", true);
                }
                Err(mpsc::TryRecvError::Empty) => {}
            }
        }
    }
    fn sort_by(&mut self, column: Order) {
        self.descending = self.order == Some(column) && !self.descending;
        self.order = Some(column);
        order_albums(&mut self.results, column, self.descending);
        self.last_click = None;
        self.expanded.clear();
    }
    fn clear_filters(&mut self) {
        self.cancel_search();
        self.fields = SearchFields {
            skip_the: self.fields.skip_the,
            ..SearchFields::default()
        };
        self.active_artist = None;
        self.active_initial = None;
        self.set_results(vec![]);
        self.searched = false;
        self.failure.clear();
    }
    fn add_results(&mut self, all: bool) {
        let (mut albums, mut tracks) = (0, 0);
        for row in &self.results {
            if all || self.checked.contains(&row.id) {
                let (new, added) = self.queue.add(row);
                albums += u64::from(new);
                tracks += added;
            }
        }
        self.checked.clear();
        self.notice(
            if albums > 0 {
                format!(
                    "Added {} with {} tracks.",
                    plural(albums, "album"),
                    num(tracks)
                )
            } else if tracks > 0 {
                format!("Added {} more tracks.", num(tracks))
            } else {
                "Those albums are already in the list.".into()
            },
            false,
        );
    }
    fn begin_download(&mut self) {
        match self.model.start(
            self.queue.groups(),
            self.settings.clone(),
            self.verify,
            self.adopt,
        ) {
            Ok(_) => {
                self.queue.clear();
                self.adopt = false;
                self.tab = Tab::Downloads;
                self.refresh_view();
            }
            Err(e) => self.notice(e.to_string(), true),
        }
    }
    fn sign_out(&mut self, ctx: &egui::Context) {
        let model = self.model.clone();
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let _ = model.logout();
            ctx.request_repaint();
        });
    }
    fn confirm_close(&mut self, ctx: &egui::Context) {
        self.cancel_search();
        self.model.close();
        self.close_after_stop = true;
        if !self.model.busy() {
            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
        }
    }

    fn top(&mut self, ui: &mut egui::Ui, narrow: bool) {
        let p = self.pal;
        let update = self.model.updates.snapshot();
        let signed = self.view.signed_in;
        // The header drops its least important parts first as the window narrows.
        let width = ui.ctx().content_rect().width();
        ui.horizontal_centered(|ui| {
            ui.spacing_mut().item_spacing.x = 12.;
            ui.vertical(|ui| {
                ui.spacing_mut().item_spacing = vec2(0., 0.);
                ui.add_space(6.);
                ui.horizontal(|ui| {
                    if let Some(icon) = &self.art.icon {
                        ui.add(icon.image());
                        ui.add_space(6.);
                    }
                    ui.label(theme::title("ib", 19.).color(p.ink));
                    ui.label(theme::title("fetch", 19.).color(p.accent));
                });
                ui.label(theme::muted(&p, "by Cyberdelia", 10.));
            });
            if (!signed || width >= 1000.)
                && let Some(logo) = self.art.powered(p.dark).map(art::Picture::image)
            {
                let logo = ui
                    .add(logo.sense(Sense::click()))
                    .on_hover_cursor(CursorIcon::PointingHand)
                    .on_hover_text("Powered by iBroadcast");
                if logo.clicked() {
                    let opened = browser::open_url("https://www.ibroadcast.com/");
                    self.result(opened);
                }
            }
            let failed = !self.view.error.is_empty() && self.view.library.is_none();
            if signed && width >= 1180. {
                let info = if self.view.loading {
                    "Loading your library index".into()
                } else if failed {
                    "The library didn't load".into()
                } else if self.view.library.is_some() {
                    format!(
                        "{} tracks in {} albums",
                        num(self.counts.0),
                        num(self.counts.1)
                    )
                } else {
                    String::new()
                };
                ui.label(RichText::new(info).color(p.muted));
            }
            if signed
                && width >= 890.
                && !self.view.loading
                && theme::quiet(ui, &p, "Refresh library")
                    .on_hover_text("Download the library index again")
                    .clicked()
            {
                self.refresh_library();
            }
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                ui.spacing_mut().item_spacing.x = 8.;
                if signed && theme::quiet(ui, &p, "Sign out").clicked() {
                    if self.model.busy() {
                        self.notice("Stop the download before signing out.", true);
                    } else {
                        self.confirm = Confirm::SignOut;
                    }
                }
                if let Some(library) = self.view.library.as_ref().filter(|_| width >= 710.) {
                    badge(ui, &p, if library.premium { "Premium" } else { "Free" });
                    let name = if library.name.is_empty() {
                        "Signed in"
                    } else {
                        &library.name
                    };
                    if width >= 790. {
                        ui.label(RichText::new(name).color(p.muted));
                    }
                }
                let glyph = theme::APPEARANCES
                    .iter()
                    .find(|a| a.0 == self.settings.appearance)
                    .map_or("◐", |a| a.2);
                let label = if narrow { "▾" } else { "Theme ▾" };
                let button =
                    theme::quiet(ui, &p, &format!("{glyph} {label}")).on_hover_text("Theme");
                egui::Popup::from_toggle_button_response(&button)
                    .close_behavior(egui::PopupCloseBehavior::CloseOnClickOutside)
                    .frame(panel_frame(&p))
                    .width(240.)
                    .show(|ui| self.theme_menu(ui));

                let available = update["available"] == true;
                let button = if available {
                    let label = format!(
                        "{} · {}",
                        if narrow { "Update" } else { "Update available" },
                        update["latest"].as_str().unwrap_or_default()
                    );
                    ui.add(
                        egui::Button::new(RichText::new(label).color(p.accent))
                            .fill(p.accent_soft)
                            .stroke(Stroke::NONE),
                    )
                } else if narrow {
                    theme::quiet(ui, &p, VERSION).on_hover_text(format!("{APP_NAME} {VERSION}"))
                } else {
                    theme::quiet(ui, &p, &format!("{APP_NAME} {VERSION}"))
                };
                let command = if button.clicked() && available {
                    self.show_updates = true;
                    Some(egui::SetOpenCommand::Bool(false))
                } else if button.clicked() {
                    Some(egui::SetOpenCommand::Toggle)
                } else if std::mem::take(&mut self.open_update_panel) {
                    Some(egui::SetOpenCommand::Bool(true))
                } else {
                    None
                };
                egui::Popup::from_response(&button)
                    .open_memory(command)
                    .close_behavior(egui::PopupCloseBehavior::CloseOnClickOutside)
                    .frame(panel_frame(&p))
                    .width(310.)
                    .show(|ui| self.update_menu(ui, &update));

                if narrow && signed {
                    ui.selectable_value(
                        &mut self.tab,
                        Tab::Downloads,
                        format!("Downloads ({})", self.queue.items.len()),
                    );
                    ui.selectable_value(&mut self.tab, Tab::Library, "Library");
                }
            });
        });
    }
    fn theme_menu(&mut self, ui: &mut egui::Ui) {
        let p = self.pal;
        let old = (
            self.settings.appearance.clone(),
            self.settings.palette.clone(),
        );
        ui.spacing_mut().item_spacing.y = 6.;
        theme::caption(ui, &p, "Appearance");
        for (key, name, glyph) in theme::APPEARANCES {
            ui.radio_value(
                &mut self.settings.appearance,
                key.into(),
                format!("{glyph}  {name}"),
            );
        }
        ui.add_space(6.);
        theme::caption(ui, &p, "Colour");
        for (key, name) in theme::PALETTES {
            ui.radio_value(&mut self.settings.palette, key.into(), name);
        }
        if old
            != (
                self.settings.appearance.clone(),
                self.settings.palette.clone(),
            )
        {
            theme::apply(ui.ctx(), &self.settings);
            let (appearance, palette) = (
                self.settings.appearance.clone(),
                self.settings.palette.clone(),
            );
            if let Err(e) = self.model.save_settings(|s| {
                s.appearance = appearance;
                s.palette = palette;
            }) {
                self.notice(format!("Could not save the theme: {e}"), true);
            }
        }
    }
    fn update_menu(&mut self, ui: &mut egui::Ui, info: &Value) {
        let p = self.pal;
        let available = info["available"] == true;
        let latest = info["latest"].as_str().unwrap_or_default();
        ui.spacing_mut().item_spacing.y = 10.;
        ui.label(theme::semibold(format!("{APP_NAME} {VERSION}")).color(p.ink));
        theme::help_sized(
            ui,
            &p,
            "Independent app by Cyberdelia. Not made or endorsed by iBroadcast.",
            13.,
        );
        let status = if info["checking"] == true {
            "Checking for updates…".into()
        } else if info["error"] == true && available {
            format!("Last check failed. Last known update: {latest}.")
        } else if info["error"] == true {
            "Could not check for updates. Try again or view GitHub releases.".into()
        } else if available {
            format!("Version {latest} is available. You are running {VERSION}.")
        } else if !latest.is_empty() {
            format!("You are running {VERSION}. Latest stable release: {latest}.")
        } else if info["automatic"] == true {
            "Updates are checked automatically, at most once a day.".into()
        } else {
            "Automatic checks are off. You can still check manually.".into()
        };
        theme::help_sized(ui, &p, &status, 13.);
        let mut automatic = info["automatic"] == true;
        if ui
            .checkbox(
                &mut automatic,
                RichText::new("Automatically check for updates").color(p.muted),
            )
            .changed()
        {
            if let Err(e) = self.model.save_settings(|s| s.auto_updates = automatic) {
                self.notice(e.to_string(), true);
            }
            self.model.updates.check(false);
        }
        ui.horizontal_wrapped(|ui| {
            if ui
                .add_enabled(
                    info["checking"] != true,
                    egui::Button::new("Check for updates"),
                )
                .clicked()
            {
                self.model.updates.check(true);
            }
            let label = if available {
                format!("Download {latest}")
            } else {
                "Releases & downloads".into()
            };
            if theme::primary(ui, &p, true, &label).clicked() {
                let opened =
                    browser::open_url(info["url"].as_str().unwrap_or(ibfetch::update::RELEASES));
                self.result(opened);
            }
        });
        theme::help_sized(
            ui,
            &p,
            "No account or library data is sent. Open update details for the installation options.",
            13.,
        );
        if available && ui.button("View update details").clicked() {
            self.show_updates = true;
            ui.close();
        }
        ui.horizontal(|ui| {
            ui.hyperlink_to(RichText::new("Tests").size(13.), TESTS);
            ui.hyperlink_to(RichText::new("MIT license").size(13.), LICENSE);
        });
    }

    fn sign_in(&mut self, ui: &mut egui::Ui) {
        let p = self.pal;
        let state = self.view.login["state"]
            .as_str()
            .unwrap_or_default()
            .to_owned();
        let width = ui.available_width().min(460.);
        let top = ((ui.available_height() - 330.) / 2.).max(0.);
        ui.vertical_centered(|ui| {
            ui.add_space(top);
            ui.allocate_ui_with_layout(vec2(width, 0.), Layout::top_down(Align::Min), |ui| {
                ui.set_width(width);
                ui.spacing_mut().item_spacing.y = 12.;
                ui.label(theme::title("Sign in to iBroadcast", 28.).color(p.ink));
                if self.view.signing_out {
                    ui.horizontal(|ui| {
                        ui.spinner();
                        ui.label(RichText::new("Signing out…").color(p.muted));
                    });
                    return;
                }
                if state == "starting" || state == "waiting" {
                    let waiting = state == "waiting";
                    theme::help_sized(
                        ui,
                        &p,
                        if waiting {
                            "Approve the connection on the iBroadcast page in your browser. ibfetch continues automatically. Enter this code if requested:"
                        } else {
                            "Contacting iBroadcast…"
                        },
                        14.,
                    );
                    if waiting {
                        let code = self.view.login["code"].as_str().unwrap_or_default().to_owned();
                        egui::Frame::new()
                            .fill(p.window)
                            .corner_radius(10)
                            .inner_margin(Margin::symmetric(20, 18))
                            .show(ui, |ui| {
                                ui.set_width(ui.available_width());
                                ui.vertical_centered(|ui| {
                                    ui.add(
                                        egui::Label::new(
                                            RichText::new(&code)
                                                .font(egui::FontId::new(40., theme::rounded()))
                                                .variation("wght", 300.)
                                                .extra_letter_spacing(2.4)
                                                .color(p.window_ink),
                                        )
                                        .selectable(true),
                                    );
                                });
                            });
                        ui.horizontal_wrapped(|ui| {
                            if theme::primary(ui, &p, true, "Open iBroadcast in your browser ↗")
                                .clicked()
                                && let Some(url) = self.view.login["url"].as_str()
                            {
                                let opened = browser::open_url(url);
                                self.result(opened);
                            }
                            if ui.button("Copy sign-in link").clicked()
                                && let Some(url) = self.view.login["url"].as_str()
                            {
                                ui.ctx().copy_text(url.to_owned());
                                self.notice("Paste the link into your browser to sign in.", false);
                            }
                            if ui.button("Copy code").clicked() {
                                ui.ctx().copy_text(code.clone());
                                self.notice("Code copied.", false);
                            }
                            if theme::quiet(ui, &p, "Cancel").clicked() {
                                self.model.cancel_login(None);
                            }
                        });
                    } else {
                        ui.horizontal(|ui| {
                            ui.spinner();
                            if theme::quiet(ui, &p, "Cancel").clicked() {
                                self.model.cancel_login(None);
                            }
                        });
                    }
                } else {
                    theme::help_sized(
                        ui,
                        &p,
                        "Sign in on the official iBroadcast website in your browser. Approve the connection and ibfetch continues automatically. Your password stays on iBroadcast.",
                        14.,
                    );
                    if theme::primary(ui, &p, true, "Sign in with iBroadcast").clicked() {
                        let started = self.model.login().map(|_| ());
                        self.result(started);
                        self.refresh_view();
                    }
                    theme::help_sized(
                        ui,
                        &p,
                        "Using another account? Choose Switch User on iBroadcast's page if your browser remembers the previous one.",
                        13.,
                    );
                    if state == "error" {
                        let message = self.view.login["message"].as_str().unwrap_or_default();
                        ui.add(egui::Label::new(RichText::new(message).color(p.red)).wrap());
                    }
                    if !self.view.error.is_empty() {
                        ui.add(egui::Label::new(RichText::new(&self.view.error).color(p.red)).wrap());
                        if ui.button("Clear saved sign-in").clicked() {
                            self.sign_out(ui.ctx());
                        }
                    }
                }
            });
        });
    }

    fn sidebar(&mut self, ui: &mut egui::Ui) {
        let p = self.pal;
        ui.spacing_mut().item_spacing = Vec2::ZERO;
        let v = ui.visuals_mut();
        v.override_text_color = Some(p.shell_ink);
        v.extreme_bg_color = p.shell_field;
        v.text_edit_bg_color = Some(p.shell_field);
        v.weak_text_color = Some(p.shell_muted);
        v.selection.stroke = Stroke::new(1., p.shell_accent);
        for part in [
            &mut v.widgets.inactive,
            &mut v.widgets.hovered,
            &mut v.widgets.active,
        ] {
            part.bg_stroke = Stroke::new(1., p.shell_line);
        }
        egui::Frame::new()
            .inner_margin(Margin {
                left: 12,
                right: 12,
                top: 12,
                bottom: 8,
            })
            .show(ui, |ui| {
                ui.spacing_mut().item_spacing.y = 8.;
                ui.label(theme::title("Artists", 15.).color(p.shell_ink));
                ui.add(
                    egui::TextEdit::singleline(&mut self.artist_find)
                        .hint_text("Find an artist")
                        .desired_width(f32::INFINITY)
                        .margin(Margin::symmetric(9, 6)),
                );
            });
        let letters = egui::Frame::new()
            .inner_margin(Margin {
                left: 10,
                right: 10,
                top: 0,
                bottom: 8,
            })
            .show(ui, |ui| {
                ui.horizontal_wrapped(|ui| {
                    ui.spacing_mut().item_spacing = vec2(2., 2.);
                    let all = std::iter::once(None)
                        .chain(std::iter::once('#').chain('A'..='Z').map(Some));
                    for choice in all {
                        let label = choice.map_or("All".into(), |c| c.to_string());
                        if letter_button(ui, &p, &label, self.letter == choice) {
                            self.letter = choice;
                            self.artist_find.clear();
                        }
                    }
                });
            });
        let edge = letters.response.rect;
        ui.painter()
            .hline(edge.x_range(), edge.bottom(), Stroke::new(1., p.shell_line));
        ui.add_space(6.);

        let query = fold(&self.artist_find);
        let rows: Vec<&Artist> = self
            .artists
            .iter()
            .filter(|artist| {
                if query.is_empty() {
                    self.letter
                        .is_none_or(|c| c == artist.initials[usize::from(self.fields.skip_the)])
                } else {
                    artist.folded.contains(&query)
                }
            })
            .collect();
        if rows.is_empty() {
            if self.view.library.is_some() {
                egui::Frame::new()
                    .inner_margin(Margin::symmetric(14, 6))
                    .show(ui, |ui| {
                        ui.label(
                            RichText::new("No artist matches.")
                                .size(13.)
                                .color(p.shell_muted),
                        );
                    });
            }
            return;
        }
        // One letter also offers every artist filed under it at once.
        let every = self
            .letter
            .filter(|_| query.is_empty())
            .map(|c| (c, rows.iter().map(|artist| artist.albums).sum::<u64>()));
        let offset = usize::from(every.is_some());
        let mut choose = None;
        egui::ScrollArea::vertical()
            .id_salt("artists")
            .auto_shrink(false)
            .show_rows(ui, 28., rows.len() + offset, |ui, range| {
                for i in range {
                    if let Some((c, albums)) = every
                        && i == 0
                    {
                        let on = self.active_initial == Some(c);
                        if artist_row(ui, &p, &format!("Every artist on {c}"), albums, on, true) {
                            choose = Some((None, Some(c)));
                        }
                        continue;
                    }
                    let artist = rows[i - offset];
                    let on = self.active_artist.as_deref() == Some(artist.name.as_str());
                    if artist_row(ui, &p, &artist.name, artist.albums, on, false) {
                        choose = Some((Some(artist.name.clone()), None));
                    }
                }
            });
        if let Some((artist, initial)) = choose {
            self.fields.artist = artist.clone().unwrap_or_default();
            self.active_artist = artist;
            self.active_initial = initial;
            self.search(ui.ctx(), false);
        }
    }

    fn browse(&mut self, ui: &mut egui::Ui) {
        let p = self.pal;
        ui.spacing_mut().item_spacing = Vec2::ZERO;
        let margin = Margin {
            left: 16,
            right: 16,
            top: 12,
            bottom: 10,
        };
        band(ui, &p, p.bg, margin, |ui| self.filters(ui));
        band(ui, &p, p.panel, Margin::symmetric(16, 8), |ui| {
            self.result_bar(ui);
        });
        self.results(ui);
    }
    fn filters(&mut self, ui: &mut egui::Ui) {
        let p = self.pal;
        let columns = if ui.available_width() < 560. { 2 } else { 4 };
        let gap = 10.;
        let width = ((ui.available_width() - gap * (columns as f32 - 1.)) / columns as f32).floor();
        let mut search = false;
        for row in 0..8 / columns {
            ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = gap;
                for column in 0..columns {
                    ui.allocate_ui_with_layout(
                        vec2(width, 52.),
                        Layout::top_down(Align::Min),
                        |ui| {
                            ui.set_width(width);
                            ui.spacing_mut().item_spacing.y = 3.;
                            search |= self.field(ui, row * columns + column, width);
                        },
                    );
                }
            });
            ui.add_space(2.);
        }
        ui.add_space(2.);
        let (mut all, mut below) = (false, false);
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = 10.;
            search |= theme::primary(ui, &p, true, "Search").clicked();
            if theme::quiet(ui, &p, "Clear").clicked() {
                self.clear_filters();
            }
            all = theme::quiet(ui, &p, "Show all albums").clicked();
            ui.checkbox(
                &mut self.fields.skip_the,
                RichText::new("Ignore “The” at the start").color(p.muted),
            );
            if self.pending.is_some() {
                ui.spinner();
            }
            // The hint sits at the end of the row, or below it in a narrow pane.
            let fits = fit(ui, theme::muted(&p, HINT, 13.), f32::INFINITY).size().x;
            below = ui.available_width() < fits + 4.;
            if !below {
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    ui.label(theme::muted(&p, HINT, 13.));
                });
            }
        });
        if below {
            ui.with_layout(Layout::right_to_left(Align::Min), |ui| {
                ui.add(egui::Label::new(theme::muted(&p, HINT, 13.)).wrap());
            });
        }
        if search {
            self.active_artist = Some(self.fields.artist.clone());
        }
        if search || all {
            self.search(ui.ctx(), all);
        }
    }
    /// One labelled search field. Returns whether Enter asked for a search.
    fn field(&mut self, ui: &mut egui::Ui, index: usize, width: f32) -> bool {
        let p = self.pal;
        let initial = self.active_initial.map(|c| format!("Every artist on {c}"));
        let (label, hint) = [
            ("Album artist", initial.as_deref().unwrap_or("4hero or A*")),
            ("Album", "Title or Two*"),
            ("Track artist", "Also on compilations"),
            ("Year", "1998 or 1990-1999"),
            ("Genre", "Any genre"),
            ("Format", "Any format"),
            ("Tag", "Any tag"),
            ("Playlist", "Any playlist"),
        ][index];
        theme::caption(ui, &p, label);
        let (value, options) = match index {
            0 => (&mut self.fields.artist, None),
            1 => (&mut self.fields.album, None),
            2 => (&mut self.fields.track_artist, None),
            3 => (&mut self.fields.year, None),
            4 => (&mut self.fields.genre, None),
            5 => (&mut self.fields.format, Some(&self.formats)),
            6 => (&mut self.fields.tag, Some(&self.tags)),
            _ => (&mut self.fields.playlist, Some(&self.playlists)),
        };
        if let Some(options) = options {
            // Tags and playlists stay disabled while the library has none.
            ui.add_enabled_ui(index == 5 || !options.is_empty(), |ui| {
                egui::ComboBox::from_id_salt(("filter", index))
                    .icon(theme::chevron)
                    .width(width)
                    .selected_text(if value.is_empty() {
                        hint
                    } else {
                        value.as_str()
                    })
                    .truncate()
                    .show_ui(ui, |ui| {
                        ui.selectable_value(value, String::new(), hint);
                        for choice in options {
                            ui.selectable_value(value, choice.clone(), choice);
                        }
                    });
            });
            return false;
        }
        let response = ui.add(text_field(value, hint, width, &p));
        if index == 0 && response.changed() {
            self.active_initial = None;
        }
        if index == 4 {
            self.genre_suggestions(&response);
        }
        response.lost_focus() && ui.input(|input| input.key_pressed(egui::Key::Enter))
    }
    /// Known genres that contain the typed text, like the browser's suggestion list.
    fn genre_suggestions(&mut self, field: &egui::Response) {
        if field.gained_focus() || field.changed() {
            self.genre_open = true;
        }
        let query = fold(&self.fields.genre);
        if !self.genre_open || query.is_empty() {
            return;
        }
        let matches: Vec<&String> = self
            .genres
            .iter()
            .filter(|genre| {
                let folded = fold(genre);
                folded.contains(&query) && folded != query
            })
            .take(8)
            .collect();
        if matches.is_empty() {
            return;
        }
        let mut pick = None;
        egui::Popup::from_response(field)
            .open_bool(&mut self.genre_open)
            .close_behavior(egui::PopupCloseBehavior::CloseOnClickOutside)
            .layout(Layout::top_down_justified(Align::Min))
            .show(|ui| {
                ui.set_min_width(field.rect.width() - 18.);
                for genre in matches {
                    if ui
                        .add(egui::Button::new(genre.as_str()).frame_when_inactive(false))
                        .clicked()
                    {
                        pick = Some(genre.clone());
                    }
                }
            });
        if let Some(genre) = pick {
            self.fields.genre = genre;
            self.genre_open = false;
        }
    }
    fn result_bar(&mut self, ui: &mut egui::Ui) {
        let p = self.pal;
        let total = self.results.len() as u64;
        let selected = self.checked.len() as u64;
        let info = if self.pending.is_some() {
            "Searching".into()
        } else if total == 0 {
            if self.searched { "No albums" } else { "" }.into()
        } else if selected > 0 {
            format!("{} of {} albums selected", num(selected), num(total))
        } else {
            format!(
                "{}, {} tracks, {}",
                plural(total, "album"),
                num(self.result_tracks),
                span(self.result_length as f64)
            )
        };
        ui.horizontal(|ui| {
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                let label = if total > 0 {
                    format!("Add all {}", num(total))
                } else {
                    "Add all".into()
                };
                if ui
                    .add_enabled(total > 0, egui::Button::new(label))
                    .clicked()
                {
                    self.add_results(true);
                }
                let label = if selected > 0 {
                    format!("Add {} selected", num(selected))
                } else {
                    "Add selected".into()
                };
                if ui
                    .add_enabled(selected > 0, egui::Button::new(label))
                    .clicked()
                {
                    self.add_results(false);
                }
                ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                    ui.add(egui::Label::new(RichText::new(info).color(p.muted)).truncate());
                });
            });
        });
    }
    fn empty(&mut self, ui: &mut egui::Ui) {
        let p = self.pal;
        let failed = !self.view.error.is_empty() && self.view.library.is_none();
        let (title, body) = if self.view.loading {
            (
                "Loading your library index",
                "The first time takes a little while. After that ibfetch keeps a copy and only fetches it again when your library changes.".to_owned(),
            )
        } else if failed {
            ("The library didn't load", self.view.error.clone())
        } else if !self.failure.is_empty() {
            ("Nothing to show", self.failure.clone())
        } else if self.searched && self.pending.is_none() {
            (
                "Nothing to show",
                "No albums match. Check the spelling, or use * for any text, like *floyd*."
                    .to_owned(),
            )
        } else {
            (
                "Find the music you want",
                "Pick an artist on the left, or fill in a field above and press Search. Then add albums to the download list.".to_owned(),
            )
        };
        let width = (ui.available_width() - 48.).clamp(120., 520.);
        ui.vertical_centered(|ui| {
            ui.add_space(48.);
            ui.allocate_ui_with_layout(vec2(width, 0.), Layout::top_down(Align::Center), |ui| {
                ui.set_width(width);
                ui.spacing_mut().item_spacing.y = 6.;
                ui.label(theme::title(title, 18.).color(p.ink));
                ui.add(
                    egui::Label::new(RichText::new(body).color(p.muted))
                        .wrap()
                        .halign(Align::Center),
                );
                // The header's Refresh library button is hidden in a narrow window.
                if failed && !self.view.loading {
                    ui.add_space(6.);
                    if theme::primary(ui, &p, true, "Try again").clicked() {
                        self.refresh_library();
                    }
                }
            });
        });
    }
    fn results(&mut self, ui: &mut egui::Ui) {
        let p = self.pal;
        let Some(library) = self
            .view
            .library
            .clone()
            .filter(|_| !self.results.is_empty())
        else {
            self.empty(ui);
            return;
        };
        let (head, _) = ui.allocate_exact_size(vec2(ui.available_width(), 40.), Sense::hover());
        let columns = Columns::new(head.left(), head.width());
        let total = self.results.len();
        let mut all = total > 0 && self.checked.len() == total;
        let some = !self.checked.is_empty() && !all;
        let toggle = ui
            .put(
                columns.check(head),
                egui::Checkbox::without_text(&mut all).indeterminate(some),
            )
            .on_hover_text("Select all albums");
        if toggle.changed() {
            self.checked = if all {
                self.results.iter().map(|a| a.id).collect()
            } else {
                BTreeSet::new()
            };
        }
        for (column, rect) in Order::ALL.into_iter().zip(columns.cells(head)) {
            let Some(rect) = rect else { continue };
            let response = ui
                .interact(
                    rect,
                    ui.scope_id().with(("sort", column.label())),
                    Sense::click(),
                )
                .on_hover_cursor(CursorIcon::PointingHand)
                .on_hover_text(format!("Sort by {}", column.label().to_lowercase()));
            let arrow = match (self.order == Some(column), self.descending) {
                (true, false) => " ↑",
                (true, true) => " ↓",
                _ => "",
            };
            let mut label = RichText::new(format!("{}{arrow}", column.label())).size(12.);
            label = if response.hovered() {
                label.color(p.ink).underline()
            } else {
                label.color(p.muted)
            };
            paint(ui, rect, column.numeric(), fit(ui, label, rect.width()));
            response.widget_info(|| {
                egui::WidgetInfo::labeled(egui::accesskit::Role::Button, true, column.label())
            });
            if response.clicked() {
                self.sort_by(column);
            }
        }
        ui.painter()
            .hline(head.x_range(), head.bottom(), Stroke::new(1., p.line));

        let mut area = egui::ScrollArea::vertical()
            .id_salt("albums")
            .auto_shrink(false);
        if std::mem::take(&mut self.scroll_top) {
            area = area.vertical_scroll_offset(0.);
        }
        let shift = ui.input(|input| input.modifiers.shift);
        let (mut click, mut expand) = (None, None);
        area.show_viewport(ui, |ui, viewport| {
            let origin = ui.cursor().min;
            let width = ui.available_width();
            let columns = Columns::new(origin.x, width);
            let height = |album: &Album| {
                if self.expanded.contains(&album.id) {
                    ROW + 14. + TRACK * album.tracks.len() as f32
                } else {
                    ROW
                }
            };
            let total: f32 = self.results.iter().map(height).sum();
            ui.allocate_rect(
                Rect::from_min_size(origin, vec2(width, total)),
                Sense::hover(),
            );
            let mut y = 0.;
            for (i, album) in self.results.iter().enumerate() {
                let tall = height(album);
                if y > viewport.bottom() {
                    break;
                }
                if y + tall >= viewport.top() {
                    let row = Rect::from_min_size(pos2(origin.x, origin.y + y), vec2(width, ROW));
                    let state = RowState {
                        checked: self.checked.contains(&album.id),
                        queued: self.queue.contains(album.id),
                        open: self.expanded.contains(&album.id),
                    };
                    match album_row(ui, &p, &columns, row, album, state) {
                        Some(RowAction::Select(on)) => click = Some((i, on)),
                        Some(RowAction::Expand) => expand = Some(album.id),
                        None => {}
                    }
                    if state.open {
                        let below = Rect::from_min_max(
                            row.left_bottom(),
                            pos2(row.right(), row.top() + tall),
                        );
                        track_list(ui, &p, below, &library, album);
                    }
                }
                y += tall;
            }
        });
        if let Some((i, on)) = click {
            // Shift extends the choice from the previous row, as in the browser list.
            let (from, to) = match self.last_click.filter(|_| shift) {
                Some(last) => (last.min(i), last.max(i)),
                None => (i, i),
            };
            for album in &self.results[from..=to.min(total - 1)] {
                if on {
                    self.checked.insert(album.id);
                } else {
                    self.checked.remove(&album.id);
                }
            }
            self.last_click = Some(i);
        }
        if let Some(id) = expand
            && !self.expanded.remove(&id)
        {
            self.expanded.insert(id);
        }
    }

    fn deck(&mut self, ui: &mut egui::Ui) {
        let p = self.pal;
        egui::ScrollArea::vertical()
            .id_salt("deck")
            .auto_shrink(false)
            .show(ui, |ui| {
                egui::Frame::new()
                    .inner_margin(Margin {
                        left: 14,
                        right: 14,
                        top: 14,
                        bottom: 24,
                    })
                    .show(ui, |ui| {
                        ui.set_width(ui.available_width());
                        ui.spacing_mut().item_spacing = vec2(8., 6.);
                        self.queue_list(ui);
                        ui.add_space(8.);
                        self.download_settings(ui);
                        ui.add_space(8.);
                        let busy = self.model.busy();
                        let ready = self.view.library.is_some() && !busy && self.queue.tracks > 0;
                        let label = if busy {
                            "Downloading".into()
                        } else if self.queue.tracks > 0 {
                            format!("Download {}", plural(self.queue.tracks, "track"))
                        } else {
                            "Download".into()
                        };
                        let size = vec2(ui.available_width(), 42.);
                        let start = ui
                            .vertical_centered_justified(|ui| {
                                theme::primary_sized(ui, &p, ready, &label, 15., size)
                            })
                            .inner;
                        if start.clicked() {
                            if self.adopt || self.queue.tracks > 1000 {
                                self.confirm = Confirm::Download;
                            } else {
                                self.begin_download();
                            }
                        }
                        let can_retry = self.view.can_retry;
                        let action = self.view.transfer.as_ref().and_then(|transfer| {
                            ui.add_space(8.);
                            transfer::show(ui, &p, transfer, can_retry)
                        });
                        match action {
                            Some(transfer::Action::Stop) => self.model.cancel_download(),
                            Some(transfer::Action::OpenFolder) => {
                                let folder = self
                                    .view
                                    .transfer
                                    .as_ref()
                                    .and_then(|t| t["out"].as_str())
                                    .unwrap_or(&self.settings.out)
                                    .to_owned();
                                self.result(browser::open_folder(Path::new(&folder)));
                            }
                            Some(transfer::Action::Retry) => {
                                let started = self.model.retry().map(|_| ());
                                self.result(started);
                            }
                            None => {}
                        }
                    });
            });
    }
    fn queue_list(&mut self, ui: &mut egui::Ui) {
        let p = self.pal;
        let albums = self.queue.items.len();
        ui.horizontal(|ui| {
            ui.label(theme::title("Download list", 15.).color(p.ink));
            if albums > 0 {
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if theme::quiet(ui, &p, "Empty list").clicked() {
                        self.queue.clear();
                    }
                });
            }
        });
        let info = if self.queue.items.is_empty() {
            "Empty. Add albums from the results.".into()
        } else {
            format!(
                "{}, {} tracks, {}",
                plural(self.queue.items.len() as u64, "album"),
                num(self.queue.tracks),
                span(self.queue.length as f64)
            )
        };
        ui.label(theme::muted(&p, info, 13.));
        if self.queue.items.is_empty() {
            return;
        }
        let limit = (ui.ctx().content_rect().height() * 0.34).max(88.);
        let mut remove = None;
        egui::Frame::new()
            .fill(p.raised)
            .stroke(Stroke::new(1., p.line))
            .corner_radius(8)
            .show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.spacing_mut().item_spacing = Vec2::ZERO;
                egui::ScrollArea::vertical()
                    .id_salt("queue")
                    .max_height(limit)
                    .show_rows(ui, 44., self.queue.items.len(), |ui, range| {
                        for i in range {
                            let last = i + 1 == self.queue.items.len();
                            if queue_row(ui, &p, &self.queue.items[i], last) {
                                remove = Some(self.queue.items[i].id);
                            }
                        }
                    });
            });
        if let Some(id) = remove {
            self.queue.remove(id);
        }
    }
    fn download_settings(&mut self, ui: &mut egui::Ui) {
        let p = self.pal;
        let free = self.free_account();
        if free && self.settings.quality != "128" {
            self.settings.quality = "128".into();
        }
        theme::caption(ui, &p, "Save to");
        folder_field(ui, &p, &mut self.settings.out, "");
        ui.add_space(2.);
        let half = ((ui.available_width() - 10.) / 2.).floor();
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = 10.;
            ui.vertical(|ui| {
                ui.set_width(half);
                theme::caption(ui, &p, "Quality");
                egui::ComboBox::from_id_salt("quality")
                    .icon(theme::chevron)
                    .width(half)
                    .selected_text(quality_label(&self.settings.quality))
                    .show_ui(ui, |ui| {
                        for &quality in QUALITIES {
                            ui.add_enabled_ui(!free || quality == "128", |ui| {
                                ui.selectable_value(
                                    &mut self.settings.quality,
                                    quality.into(),
                                    quality_label(quality),
                                );
                            });
                        }
                    });
            });
            ui.vertical(|ui| {
                ui.set_width(half);
                theme::caption(ui, &p, "At the same time");
                egui::ComboBox::from_id_salt("jobs")
                    .icon(theme::chevron)
                    .width(half)
                    .selected_text(files_label(self.settings.jobs))
                    .show_ui(ui, |ui| {
                        for jobs in 1..=8 {
                            ui.selectable_value(&mut self.settings.jobs, jobs, files_label(jobs));
                        }
                    });
            });
        });
        // The download service only has original files; bitrates always stream.
        if self.settings.quality == "orig" {
            ui.add_space(2.);
            theme::caption(ui, &p, "Original files from");
            let label = |source: &str| {
                if source == "stream" {
                    "Streaming: exactly as uploaded"
                } else {
                    "Download service: faster, your edits"
                }
            };
            egui::ComboBox::from_id_salt("source")
                .icon(theme::chevron)
                .width(ui.available_width())
                .selected_text(label(&self.settings.source))
                .show_ui(ui, |ui| {
                    for source in ["download", "stream"] {
                        ui.selectable_value(
                            &mut self.settings.source,
                            source.into(),
                            label(source),
                        );
                    }
                });
            theme::help(
                ui,
                &p,
                "Updated tags require Download Updated Metadata to be enabled in iBroadcast. Existing files are preserved when you change source.",
            );
        }
        ui.add_space(2.);
        fold_out(ui, &p, "Folder and file names", false, |ui| {
            ui.add(
                egui::TextEdit::singleline(&mut self.settings.layout)
                    .desired_width(f32::INFINITY)
                    .margin(Margin::symmetric(9, 6)),
            );
            let fields: Vec<String> = FIELDS.iter().map(|field| format!("{{{field}}}")).collect();
            theme::help(
                ui,
                &p,
                &format!("Fields: {}. A slash makes a folder.", fields.join(" ")),
            );
        });
        let work = !self.settings.work.trim().is_empty();
        fold_out(ui, &p, "Work folder", work, |ui| {
            folder_field(ui, &p, &mut self.settings.work, "Off: save directly");
            theme::help(
                ui,
                &p,
                "Optional. Tracks download to this folder on your computer first, and each finished track then moves to Save to. Useful when Save to is a NAS.",
            );
        });
        if free {
            theme::help(
                ui,
                &p,
                "Free account: downloads are MP3 128 kbps. Original files need iBroadcast Premium.",
            );
        }
        fold_out(ui, &p, "Existing files", false, |ui| {
            ui.checkbox(
                &mut self.verify,
                RichText::new("Verify existing files with SHA-256").color(p.muted),
            );
            ui.checkbox(
                &mut self.adopt,
                RichText::new("Adopt existing files I trust").color(p.muted),
            );
            theme::help(
                ui,
                &p,
                "Normally, size and modification time are checked. Changed or unknown files are preserved and skipped. Adoption accepts matching-path files as local baselines; it does not verify their track or bitrate with iBroadcast.",
            );
        });
    }

    fn dialogs(&mut self, ctx: &egui::Context) {
        let p = self.pal;
        let screen = ctx.content_rect();
        let frame = egui::Frame::new()
            .fill(p.panel)
            .stroke(Stroke::new(1., p.line))
            .corner_radius(12)
            .inner_margin(18);
        if self.show_updates {
            let info = self.model.updates.snapshot();
            let source = info["notes"].as_str().unwrap_or_default();
            if self.release_notes.0 != source {
                self.release_notes = (source.into(), notes::parse(source));
            }
            let available = info["available"] == true;
            let latest = info["latest"].as_str().unwrap_or_default();
            let modal = egui::Modal::new(egui::Id::unique("ibfetch-updates"))
                .frame(frame)
                .show(ctx, |ui| {
                    ui.set_width((screen.width() - 68.).min(644.));
                    ui.spacing_mut().item_spacing.y = 8.;
                    let title = if available {
                        format!("{APP_NAME} {latest} is available")
                    } else {
                        format!("{APP_NAME} updates")
                    };
                    ui.label(theme::title(title, 20.).color(p.ink));
                    ui.label(format!("You are running {APP_NAME} {VERSION}."));
                    if let Some(name) = info["name"].as_str().filter(|name| !name.is_empty()) {
                        ui.label(name);
                    }
                    if !self.release_notes.1.is_empty() {
                        egui::Frame::new()
                            .stroke(Stroke::new(1., p.line))
                            .corner_radius(6)
                            .inner_margin(12)
                            .show(ui, |ui| {
                                ui.set_width(ui.available_width());
                                egui::ScrollArea::vertical()
                                    .id_salt("release-notes")
                                    .max_height(screen.height() * 0.35)
                                    .auto_shrink([false, true])
                                    .show(ui, |ui| notes::show(ui, &p, &self.release_notes.1));
                            });
                    }
                    let mut close = false;
                    ui.horizontal_wrapped(|ui| {
                        close = ui.button("Later").clicked();
                        if ui.button("Update settings").clicked() {
                            close = true;
                            self.open_update_panel = true;
                        }
                        if theme::primary(ui, &p, true, "Official release & downloads ↗").clicked()
                        {
                            let url = info["url"].as_str().unwrap_or(ibfetch::update::RELEASES);
                            let opened = browser::open_url(url);
                            self.result(opened);
                        }
                    });
                    close
                });
            if modal.inner || modal.should_close() {
                self.show_updates = false;
            }
        }
        if !matches!(self.confirm, Confirm::None) {
            let tracks = num(self.queue.tracks);
            let (title, text, no, yes) = match self.confirm {
                Confirm::Download => (
                    format!("Download {tracks} tracks?"),
                    if self.adopt {
                        "Trust existing files at these paths as local baselines? This does not verify their track or bitrate with iBroadcast. Their audio will be preserved."
                    } else {
                        ""
                    },
                    "Cancel",
                    "Download",
                ),
                Confirm::SignOut => (
                    "Sign out?".into(),
                    "ibfetch forgets your sign-in and its copy of the library index.",
                    "Cancel",
                    "Sign out",
                ),
                Confirm::Close => (
                    "Close ibfetch?".into(),
                    "A download or unsent download list is active. Closing stops downloads and discards the unsent list. Matching partial downloads can be resumed later.",
                    "Keep open",
                    "Close ibfetch",
                ),
                Confirm::None => unreachable!(),
            };
            let modal = egui::Modal::new(egui::Id::unique("ibfetch-confirm"))
                .frame(frame)
                .show(ctx, |ui| {
                    ui.set_width((screen.width() - 68.).min(420.));
                    ui.spacing_mut().item_spacing.y = 10.;
                    ui.label(theme::title(title, 18.).color(p.ink));
                    if !text.is_empty() {
                        ui.add(egui::Label::new(RichText::new(text).color(p.muted)).wrap());
                    }
                    ui.horizontal(|ui| {
                        let no = ui.button(no).clicked();
                        (no, theme::primary(ui, &p, true, yes).clicked())
                    })
                    .inner
                });
            let (no, yes) = modal.inner;
            if yes {
                match std::mem::take(&mut self.confirm) {
                    Confirm::Download => self.begin_download(),
                    Confirm::SignOut => self.sign_out(ctx),
                    Confirm::Close => self.confirm_close(ctx),
                    Confirm::None => {}
                }
            } else if no || modal.should_close() {
                self.confirm = Confirm::None;
            }
        }
    }
    fn show_toast(&mut self, ctx: &egui::Context) {
        let p = self.pal;
        let Some(toast) = &self.toast else { return };
        let left = toast.until.saturating_duration_since(Instant::now());
        if left.is_zero() {
            self.toast = None;
            return;
        }
        ctx.request_repaint_after(left);
        let (fill, ink) = if toast.bad {
            (p.red, Color32::WHITE)
        } else {
            (p.ink, p.bg)
        };
        egui::Area::new(egui::Id::unique("ibfetch-toast"))
            .order(egui::Order::Tooltip)
            .anchor(Align2::CENTER_BOTTOM, vec2(0., -18.))
            .interactable(false)
            .show(ctx, |ui| {
                egui::Frame::new()
                    .fill(fill)
                    .corner_radius(8)
                    .inner_margin(Margin::symmetric(14, 9))
                    .shadow(egui::Shadow {
                        offset: [0, 6],
                        blur: 24,
                        spread: 0,
                        color: Color32::from_black_alpha(64),
                    })
                    .show(ui, |ui| {
                        ui.set_max_width((ctx.content_rect().width() * 0.92).min(560.));
                        ui.add(
                            egui::Label::new(RichText::new(&toast.text).size(13.).color(ink))
                                .wrap(),
                        );
                    });
            });
    }
}
impl eframe::App for Desktop {
    fn logic(&mut self, ctx: &egui::Context, _: &mut eframe::Frame) {
        self.refresh_view();
        self.drain_search();
        if self.stop.is_cancelled() && !self.close_after_stop {
            self.confirm_close(ctx);
        }
        if self.close_after_stop && !self.model.busy() {
            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
        }
        if self.view.loading
            || self.view.signed_in
            || self.view.login.is_object()
            || self.view.signing_out
            || self.show_updates
            || self.close_after_stop
        {
            // The lamp and the waiting bars move while a download runs.
            ctx.request_repaint_after(Duration::from_millis(if self.model.busy() {
                50
            } else {
                250
            }));
        }
    }
    fn ui(&mut self, ui: &mut egui::Ui, _: &mut eframe::Frame) {
        let ctx = ui.ctx().clone();
        self.pal = Palette::current(&ctx, &self.settings);
        self.art.refresh(&ctx);
        if ctx.input(|i| i.viewport().close_requested()) && !self.close_after_stop {
            if self.model.busy() || !self.queue.items.is_empty() {
                ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
                self.confirm = Confirm::Close;
            } else {
                self.model.close();
            }
        }
        let p = self.pal;
        let narrow = ui.available_width() < 900.;
        egui::Panel::top("header")
            .exact_size(52.)
            .frame(
                egui::Frame::new()
                    .fill(p.panel)
                    .inner_margin(Margin::symmetric(16, 0)),
            )
            .show(ui, |ui| self.top(ui, narrow));
        let deck = egui::Frame::new().fill(p.panel);
        if !self.view.signed_in {
            egui::CentralPanel::default()
                .frame(egui::Frame::new().fill(p.bg).inner_margin(24))
                .show(ui, |ui| self.sign_in(ui));
        } else if narrow && self.tab == Tab::Downloads {
            egui::CentralPanel::default()
                .frame(deck)
                .show(ui, |ui| self.deck(ui));
        } else {
            if !narrow {
                egui::Panel::right("deck")
                    .default_size(350.)
                    .size_range(290. ..=460.)
                    .frame(deck)
                    .show(ui, |ui| self.deck(ui));
            }
            egui::Panel::left("crate")
                .default_size(if narrow { 190. } else { 250. })
                .size_range(160. ..=340.)
                .frame(egui::Frame::new().fill(p.shell))
                .show(ui, |ui| self.sidebar(ui));
            egui::CentralPanel::default()
                .frame(egui::Frame::new().fill(p.bg))
                .show(ui, |ui| self.browse(ui));
        }
        self.dialogs(&ctx);
        self.show_toast(&ctx);
    }
}

/// The floating panels under the header buttons.
fn panel_frame(p: &Palette) -> egui::Frame {
    egui::Frame::new()
        .fill(p.raised)
        .stroke(Stroke::new(1., p.line))
        .corner_radius(8)
        .inner_margin(14)
        .shadow(egui::Shadow {
            offset: [0, 12],
            blur: 32,
            spread: 0,
            color: Color32::from_black_alpha(48),
        })
}
/// A full-width band with its own background and a line below it.
fn band(
    ui: &mut egui::Ui,
    p: &Palette,
    fill: Color32,
    margin: Margin,
    add: impl FnOnce(&mut egui::Ui),
) {
    let band = egui::Frame::new()
        .fill(fill)
        .inner_margin(margin)
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            ui.spacing_mut().item_spacing = vec2(8., 6.);
            add(ui);
        });
    let rect = band.response.rect;
    ui.painter()
        .hline(rect.x_range(), rect.bottom(), Stroke::new(1., p.line));
}
/// The small pill behind the account name.
fn badge(ui: &mut egui::Ui, p: &Palette, text: &str) {
    let label = fit(ui, RichText::new(text).size(12.).color(p.ink), 120.);
    let (rect, _) = ui.allocate_exact_size(label.size() + vec2(14., 4.), Sense::hover());
    ui.painter().rect_filled(rect, 10., p.accent_soft);
    ui.painter()
        .galley(rect.center() - label.size() / 2., label, p.ink);
}
fn text_field<'a>(
    value: &'a mut String,
    hint: &str,
    width: f32,
    p: &Palette,
) -> egui::TextEdit<'a> {
    egui::TextEdit::singleline(value)
        .desired_width(width)
        .margin(Margin::symmetric(9, 6))
        .hint_text(RichText::new(hint).color(p.muted.gamma_multiply(0.8)))
}
fn folder_field(ui: &mut egui::Ui, p: &Palette, path: &mut String, hint: &str) {
    ui.horizontal(|ui| {
        ui.spacing_mut().item_spacing.x = 6.;
        let width = (ui.available_width() - 82.).max(80.);
        ui.add(text_field(path, hint, width, p));
        if ui.button("Choose").clicked()
            && let Some(chosen) = browser::choose_folder(Path::new(path))
        {
            *path = chosen.to_string_lossy().into();
        }
    });
}
fn fold_out(
    ui: &mut egui::Ui,
    p: &Palette,
    title: &str,
    open: bool,
    add: impl FnOnce(&mut egui::Ui),
) {
    egui::CollapsingHeader::new(theme::muted(p, title, 13.))
        .id_salt(title)
        .default_open(open)
        .show(ui, add);
}
fn quality_label(value: &str) -> String {
    if value == "orig" {
        "Original file".into()
    } else {
        format!("MP3 {value} kbps")
    }
}
fn files_label(jobs: usize) -> String {
    format!("{jobs} file{}", if jobs == 1 { "" } else { "s" })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn album(id: u64, artist: &str, title: &str, year: u64, tracks: &[u64]) -> Album {
        Album {
            id,
            artist: artist.into(),
            title: title.into(),
            year,
            disc: 0,
            tracks: tracks.to_vec(),
            formats: "FLAC".into(),
            length: tracks.len() as u64 * 100,
            track_lengths: tracks.iter().map(|&track| (track, 100)).collect(),
        }
    }

    #[test]
    fn queue_keeps_order_merges_tracks_and_tracks_totals() {
        let mut queue = Queue::default();
        assert_eq!(
            queue.add(&album(7, "B", "Second", 2001, &[1, 2])),
            (true, 2)
        );
        assert_eq!(queue.add(&album(3, "A", "First", 1999, &[9])), (true, 1));
        assert_eq!(
            queue.add(&album(7, "B", "Second", 2001, &[2, 4])),
            (false, 1)
        );
        assert_eq!((queue.tracks, queue.length), (4, 400));
        assert_eq!(queue.items.iter().map(|q| q.id).collect::<Vec<_>>(), [7, 3]);
        let groups = queue.groups();
        assert_eq!(groups[0].tracks, [1, 2, 4]);
        queue.remove(7);
        assert!(!queue.contains(7) && queue.contains(3));
        assert_eq!((queue.tracks, queue.length, queue.index[&3]), (1, 100, 0));
        queue.remove(7);
        queue.clear();
        assert!(queue.items.is_empty() && queue.tracks == 0);
    }

    #[test]
    fn queue_total_follows_a_track_length_that_changed_after_a_refresh() {
        let mut queue = Queue::default();
        let mut listed = album(7, "B", "Second", 2001, &[1, 2]);
        queue.add(&listed);
        listed.track_lengths.insert(1, 120);
        assert_eq!(queue.add(&listed), (false, 0));
        assert_eq!((queue.tracks, queue.length), (2, 220));
        queue.add(&album(3, "A", "First", 1999, &[9]));
        queue.remove(7);
        assert_eq!((queue.tracks, queue.length), (1, 100));
    }

    #[test]
    fn sorting_reverses_only_the_chosen_column() {
        let mut rows = vec![
            album(1, "b", "Vol. 10", 1990, &[1]),
            album(2, "A", "Vol. 2", 1990, &[1, 2]),
            album(3, "a", "Vol. 1", 2000, &[1]),
        ];
        order_albums(&mut rows, Order::Album, false);
        assert_eq!(rows.iter().map(|a| a.id).collect::<Vec<_>>(), [3, 2, 1]);
        order_albums(&mut rows, Order::Year, true);
        assert_eq!(rows.iter().map(|a| a.id).collect::<Vec<_>>(), [3, 2, 1]);
        order_albums(&mut rows, Order::Tracks, true);
        assert_eq!(rows.iter().map(|a| a.id).collect::<Vec<_>>(), [2, 3, 1]);
    }
}
