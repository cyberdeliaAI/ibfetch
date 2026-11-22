//! The colour roles of the bundled browser page, applied to egui.
use eframe::egui::{
    self, Color32, CornerRadius, FontData, FontDefinitions, FontFamily, FontId, Galley, Margin,
    Rect, Response, RichText, Stroke, TextStyle, Vec2, pos2, vec2,
};
use ibfetch::model::Settings;
use std::{path::PathBuf, sync::Arc};

/// One palette in one appearance. The names follow the CSS variables in `assets/browser.html`.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Palette {
    pub dark: bool,
    pub bg: Color32,
    pub panel: Color32,
    pub raised: Color32,
    pub ink: Color32,
    pub muted: Color32,
    pub line: Color32,
    pub accent: Color32,
    pub accent_fill: Color32,
    pub accent_soft: Color32,
    pub focus: Color32,
    pub shell: Color32,
    pub shell_ink: Color32,
    pub shell_muted: Color32,
    pub shell_active: Color32,
    pub shell_line: Color32,
    pub shell_accent: Color32,
    pub window: Color32,
    pub window_ink: Color32,
    pub window_dim: Color32,
    pub window_track: Color32,
    pub window_stats: Color32,
    pub primary_ink: Color32,
    pub red: Color32,
    /// Search field and hovered rows in the artist list.
    pub shell_field: Color32,
    pub shell_hover: Color32,
    /// Text on the selected letter.
    pub shell_on_accent: Color32,
    /// The counts in the transfer window.
    pub tally: Color32,
}

type Roles = [u32; 23];
// bg, panel, raised, ink, muted, line, accent, accent-fill, accent-soft, focus,
// shell, shell-ink, shell-muted, shell-active, shell-line, shell-accent,
// window, window-ink, window-dim, window-track, window-stats, primary-ink, red
const FOREST: [Roles; 2] = [
    [
        0xf7f8f4, 0xfbfcf9, 0xffffff, 0x203329, 0x68746c, 0xe0e5dd, 0x2d654d, 0x2d654d, 0xe5ecd9,
        0x759345, 0x1e3027, 0xdce7de, 0xb5c6b8, 0x344b3d, 0x3a4e40, 0xd2e59e, 0x1e3027, 0xd2e59e,
        0xb5c6b8, 0x344b3d, 0xdce7de, 0xffffff, 0xb9302a,
    ],
    [
        0x121915, 0x18211c, 0x1b2520, 0xd3ebde, 0xa7b3ab, 0x393b37, 0x91cbaf, 0x91cbaf, 0x2b2e26,
        0x8fae60, 0x1e3027, 0xdce7de, 0xb5c6b8, 0x344b3d, 0x3a4e40, 0xd2e59e, 0x1e3027, 0xd2e59e,
        0xb5c6b8, 0x344b3d, 0xdce7de, 0x1b2520, 0xe5574b,
    ],
];
const OCEAN: [Roles; 2] = [
    [
        0xf5f8fa, 0xf0f5f8, 0xffffff, 0x213746, 0x536b7a, 0xd7e3e9, 0x246276, 0x246276, 0xe5f0f4,
        0x559bac, 0x172d38, 0xe8f3f7, 0xb2c8d3, 0x294958, 0x435e6b, 0x9bdce7, 0x172d38, 0x9bdce7,
        0xb2c8d3, 0x294958, 0xe8f3f7, 0xffffff, 0xb9302a,
    ],
    [
        0x101f28, 0x162731, 0x1a2d38, 0xe0eef3, 0xafc4cf, 0x344956, 0x90d9e6, 0x286b7b, 0x233f4c,
        0x67b8cb, 0x172d38, 0xe8f3f7, 0xb2c8d3, 0x294958, 0x435e6b, 0x9bdce7, 0x172d38, 0x9bdce7,
        0xb2c8d3, 0x294958, 0xe8f3f7, 0xffffff, 0xe5574b,
    ],
];
const WARM: [Roles; 2] = [
    [
        0xfaf7f2, 0xf7f1e8, 0xfffefa, 0x3b2c23, 0x76614f, 0xe6dcd0, 0x805334, 0x805334, 0xf0e4d6,
        0xb18459, 0x33281f, 0xf7eee3, 0xd0bfae, 0x574233, 0x735b48, 0xefc99e, 0x33281f, 0xefc99e,
        0xd0bfae, 0x574233, 0xf7eee3, 0xffffff, 0xb9302a,
    ],
    [
        0x211a15, 0x2a211b, 0x30251d, 0xf2e8dc, 0xc7b6a5, 0x564336, 0xe6bf96, 0x865632, 0x483425,
        0xd2a16e, 0x33281f, 0xf7eee3, 0xd0bfae, 0x574233, 0x735b48, 0xefc99e, 0x33281f, 0xefc99e,
        0xd0bfae, 0x574233, 0xf7eee3, 0xffffff, 0xe5574b,
    ],
];
const IBROADCAST: [Roles; 2] = [
    [
        0xf4f7fb, 0xedf3fa, 0xffffff, 0x1c3048, 0x53677f, 0xd5dfeb, 0x1c559b, 0x1c559b, 0xe3edf9,
        0x518ec5, 0x162234, 0xecedf1, 0xb7bec9, 0x184e8e, 0x30445f, 0xa0dafa, 0x162234, 0xa0dafa,
        0xb7bec9, 0x184e8e, 0xecedf1, 0xffffff, 0xb9302a,
    ],
    [
        0x0d1828, 0x162234, 0x1e2d43, 0xeceef2, 0xb1bdcd, 0x2d405b, 0x9bd9f8, 0x1c559b, 0x213d60,
        0x5c99d6, 0x162234, 0xecedf1, 0xb7bec9, 0x184e8e, 0x30445f, 0xa0dafa, 0x162234, 0xa0dafa,
        0xb7bec9, 0x184e8e, 0xecedf1, 0xffffff, 0xe5574b,
    ],
];
// Amber keeps the original neutral grey surfaces and orange accents.
const AMBER: [Roles; 2] = [
    [
        0xe2e4df, 0xeff0ec, 0xfafaf8, 0x1c2024, 0x5c636a, 0xc6cac2, 0xb8730c, 0xe9a23a, 0xf3e1bd,
        0xb8730c, 0xeff0ec, 0x1c2024, 0x5c636a, 0xf3e1bd, 0xc6cac2, 0xe9a23a, 0x15181b, 0xf2a93b,
        0x7d6233, 0x2a2f34, 0xc9b48e, 0x241600, 0xb9302a,
    ],
    [
        0x16181a, 0x1f2225, 0x272b2f, 0xe5e6e2, 0x969ca2, 0x353a3f, 0xf0a63a, 0xf0a63a, 0x43361d,
        0xf0a63a, 0x1f2225, 0xe5e6e2, 0x969ca2, 0x43361d, 0x353a3f, 0xf0a63a, 0x0e1012, 0xf2a93b,
        0x7d6233, 0x23272b, 0xc9b48e, 0x241600, 0xe5574b,
    ],
];
pub const PALETTES: [(&str, &str); 5] = [
    ("forest", "Forest"),
    ("ocean", "Ocean"),
    ("warm", "Warm"),
    ("ibroadcast", "iBroadcast inspired"),
    ("amber", "Amber · Original"),
];
pub const APPEARANCES: [(&str, &str, &str); 3] = [
    ("auto", "Auto · follow system", "◐"),
    ("light", "Light", "☀"),
    ("dark", "Dark", "☾"),
];

const fn rgb(hex: u32) -> Color32 {
    Color32::from_rgb((hex >> 16) as u8, (hex >> 8) as u8, hex as u8)
}
/// `weight` of `b` over `a`, in gamma space like CSS `color-mix(in srgb, …)`.
pub fn mix(a: Color32, b: Color32, weight: f32) -> Color32 {
    let channel =
        |x: u8, y: u8| (f32::from(x) + (f32::from(y) - f32::from(x)) * weight).round() as u8;
    Color32::from_rgb(
        channel(a.r(), b.r()),
        channel(a.g(), b.g()),
        channel(a.b(), b.b()),
    )
}

impl Palette {
    pub fn new(name: &str, dark: bool) -> Self {
        let amber = name == "amber";
        let roles = match name {
            "ocean" => OCEAN,
            "warm" => WARM,
            "ibroadcast" => IBROADCAST,
            "amber" => AMBER,
            _ => FOREST,
        }[usize::from(dark)];
        let c = |i: usize| rgb(roles[i]);
        Self {
            dark,
            bg: c(0),
            panel: c(1),
            raised: c(2),
            ink: c(3),
            muted: c(4),
            line: c(5),
            accent: c(6),
            accent_fill: c(7),
            accent_soft: c(8),
            focus: c(9),
            shell: c(10),
            shell_ink: c(11),
            shell_muted: c(12),
            shell_active: c(13),
            shell_line: c(14),
            shell_accent: c(15),
            window: c(16),
            window_ink: c(17),
            window_dim: c(18),
            window_track: c(19),
            window_stats: c(20),
            primary_ink: c(21),
            red: c(22),
            shell_field: if amber { c(2) } else { c(13) },
            shell_hover: if amber { c(2) } else { c(13) },
            shell_on_accent: if amber { c(21) } else { c(10) },
            tally: if amber { rgb(0xd9c59f) } else { c(20) },
        }
    }
    /// The palette for the appearance egui is showing now.
    pub fn current(ctx: &egui::Context, settings: &Settings) -> Self {
        Self::new(&settings.palette, ctx.theme() == egui::Theme::Dark)
    }
    fn style(&self) -> egui::Style {
        let mut style = egui::Style {
            visuals: if self.dark {
                egui::Visuals::dark()
            } else {
                egui::Visuals::light()
            },
            ..Default::default()
        };
        let round = CornerRadius::same(6);
        let v = &mut style.visuals;
        v.panel_fill = self.bg;
        v.window_fill = self.raised;
        v.window_stroke = Stroke::new(1., self.line);
        v.window_corner_radius = CornerRadius::same(12);
        v.menu_corner_radius = CornerRadius::same(8);
        v.extreme_bg_color = self.raised;
        v.text_edit_bg_color = Some(self.raised);
        v.faint_bg_color = self.panel;
        v.code_bg_color = self.accent_soft;
        v.hyperlink_color = self.accent;
        v.warn_fg_color = self.red;
        v.error_fg_color = self.red;
        v.weak_text_color = Some(self.muted);
        v.selection.bg_fill = mix(self.raised, self.accent, 0.3);
        v.selection.stroke = Stroke::new(1., self.ink);
        v.window_shadow.color = Color32::from_black_alpha(48);
        v.popup_shadow.color = Color32::from_black_alpha(48);
        let w = &mut v.widgets;
        for part in [
            &mut w.noninteractive,
            &mut w.inactive,
            &mut w.hovered,
            &mut w.active,
            &mut w.open,
        ] {
            part.corner_radius = round;
            part.expansion = 0.;
            part.bg_fill = self.raised;
            part.weak_bg_fill = self.raised;
            part.bg_stroke = Stroke::new(1., self.line);
            part.fg_stroke = Stroke::new(1., self.ink);
        }
        w.noninteractive.bg_fill = self.panel;
        w.noninteractive.weak_bg_fill = self.panel;
        w.hovered.bg_stroke = Stroke::new(1., self.muted);
        w.open.bg_stroke = Stroke::new(1., self.muted);
        w.active.bg_fill = self.accent_soft;
        w.active.weak_bg_fill = self.accent_soft;
        w.active.bg_stroke = Stroke::new(1., self.accent);
        let s = &mut style.spacing;
        s.item_spacing = Vec2::new(8., 6.);
        s.button_padding = Vec2::new(12., 6.);
        s.interact_size = Vec2::new(40., 30.);
        s.window_margin = Margin::same(14);
        s.menu_margin = Margin::same(8);
        s.icon_width = 16.;
        s.icon_width_inner = 9.;
        s.combo_height = 320.;
        // Text is for reading here; only the sign-in code opts in to selection.
        style.interaction.selectable_labels = false;
        style.text_styles = [
            (TextStyle::Small, FontId::proportional(12.)),
            (TextStyle::Body, FontId::proportional(14.)),
            (TextStyle::Button, FontId::proportional(14.)),
            (TextStyle::Heading, FontId::new(19., rounded())),
            (TextStyle::Monospace, FontId::monospace(13.)),
        ]
        .into();
        style
    }
}

/// Follow the saved appearance and colour choice.
pub fn apply(ctx: &egui::Context, settings: &Settings) {
    ctx.set_theme(match settings.appearance.as_str() {
        "light" => egui::ThemePreference::Light,
        "dark" => egui::ThemePreference::Dark,
        _ => egui::ThemePreference::System,
    });
    for (theme, dark) in [(egui::Theme::Dark, true), (egui::Theme::Light, false)] {
        ctx.set_style_of(theme, Palette::new(&settings.palette, dark).style());
    }
}

const ROUNDED: &str = "rounded";
/// The rounded display face for the brand, headings and counters.
pub fn rounded() -> FontFamily {
    FontFamily::Name(ROUNDED.into())
}
fn system_fonts() -> (Vec<PathBuf>, Vec<PathBuf>) {
    if cfg!(target_os = "macos") {
        (
            vec!["/System/Library/Fonts/SFNS.ttf".into()],
            vec!["/System/Library/Fonts/SFNSRounded.ttf".into()],
        )
    } else if cfg!(windows) {
        let fonts =
            PathBuf::from(std::env::var_os("WINDIR").unwrap_or_else(|| "C:\\Windows".into()))
                .join("Fonts");
        (
            vec![fonts.join("SegUIVar.ttf"), fonts.join("segoeui.ttf")],
            vec![],
        )
    } else {
        (vec![], vec![])
    }
}
/// Prefer the platform's interface font, as the browser page does with `system-ui`.
/// The bundled egui fonts stay installed behind it.
pub fn install_fonts(ctx: &egui::Context) {
    let mut fonts = FontDefinitions::default();
    let read = |paths: Vec<PathBuf>| {
        paths.into_iter().find_map(|path| {
            let size = std::fs::metadata(&path).ok()?.len();
            (size <= 64 * 1024 * 1024).then(|| std::fs::read(&path).ok())?
        })
    };
    let (body, display) = system_fonts();
    if let Some(data) = read(body) {
        fonts
            .font_data
            .insert("system-ui".into(), Arc::new(FontData::from_owned(data)));
        fonts
            .families
            .entry(FontFamily::Proportional)
            .or_default()
            .insert(0, "system-ui".into());
    }
    let mut faces = fonts
        .families
        .get(&FontFamily::Proportional)
        .cloned()
        .unwrap_or_default();
    if let Some(data) = read(display) {
        fonts.font_data.insert(
            "system-rounded".into(),
            Arc::new(FontData::from_owned(data)),
        );
        faces.insert(0, "system-rounded".into());
    }
    fonts.families.insert(rounded(), faces);
    ctx.set_fonts(fonts);
}

/// Semibold where the interface font has a weight axis; the regular weight elsewhere.
pub fn semibold(text: impl Into<String>) -> RichText {
    RichText::new(text).variation("wght", 600.)
}
/// A section title in the rounded face.
pub fn title(text: impl Into<String>, size: f32) -> RichText {
    semibold(text).font(FontId::new(size, rounded()))
}
pub fn muted(p: &Palette, text: impl Into<String>, size: f32) -> RichText {
    RichText::new(text).size(size).color(p.muted)
}

/// Lay out one line, shortened with an ellipsis when it does not fit.
pub fn fit(ui: &egui::Ui, text: impl Into<egui::WidgetText>, width: f32) -> Arc<Galley> {
    text.into().into_galley(
        ui,
        Some(egui::TextWrapMode::Truncate),
        width.max(0.),
        TextStyle::Body,
    )
}
/// Draw a laid-out line at the left or right of `rect`, centred vertically.
pub fn paint(ui: &egui::Ui, rect: Rect, right: bool, galley: Arc<Galley>) {
    let x = if right {
        rect.right() - galley.size().x
    } else {
        rect.left()
    };
    let at = pos2(x, rect.center().y - galley.size().y / 2.);
    ui.painter().galley(at, galley, ui.visuals().text_color());
}
/// The thin chevron on dropdowns, in place of egui's filled triangle.
pub fn chevron(ui: &egui::Ui, rect: Rect, visuals: &egui::style::WidgetVisuals, _open: bool) {
    let centre = rect.center();
    let points = [vec2(-4., -2.), vec2(0., 2.), vec2(4., -2.)].map(|offset| centre + offset);
    ui.painter()
        .line(points.to_vec(), Stroke::new(1.5, visuals.fg_stroke.color));
}
/// The filled call-to-action button.
pub fn primary(ui: &mut egui::Ui, p: &Palette, enabled: bool, text: &str) -> Response {
    primary_sized(ui, p, enabled, text, 14., Vec2::ZERO)
}
pub fn primary_sized(
    ui: &mut egui::Ui,
    p: &Palette,
    enabled: bool,
    text: &str,
    size: f32,
    min: Vec2,
) -> Response {
    let button = if enabled {
        egui::Button::new(semibold(text).size(size).color(p.primary_ink))
            .fill(p.accent_fill)
            .stroke(Stroke::new(1., p.accent_fill))
    } else {
        egui::Button::new(RichText::new(text).size(size).color(p.muted))
            .fill(p.raised)
            .stroke(Stroke::new(1., p.line))
    };
    // Keep the muted look instead of egui's fade for a disabled primary action.
    let response = ui.add(button.min_size(min).sense(if enabled {
        egui::Sense::click()
    } else {
        egui::Sense::hover()
    }));
    if enabled {
        response.on_hover_cursor(egui::CursorIcon::PointingHand)
    } else {
        response
    }
}
/// A borderless button that shows its frame on hover.
pub fn quiet(ui: &mut egui::Ui, p: &Palette, text: &str) -> Response {
    ui.add(egui::Button::new(RichText::new(text).color(p.muted)).frame_when_inactive(false))
}
/// The small caption above a form field.
pub fn caption(ui: &mut egui::Ui, p: &Palette, text: &str) {
    ui.label(muted(p, text, 12.));
}
/// Explanatory text under a setting.
pub fn help(ui: &mut egui::Ui, p: &Palette, text: &str) {
    help_sized(ui, p, text, 12.);
}
pub fn help_sized(ui: &mut egui::Ui, p: &Palette, text: &str, size: f32) {
    ui.add(egui::Label::new(muted(p, text, size)).wrap());
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn palettes_follow_the_browser_page_roles() {
        let forest = Palette::new("forest", true);
        assert_eq!(forest.bg, rgb(0x121915));
        assert_eq!(forest.window_ink, rgb(0xd2e59e));
        assert_eq!(forest.tally, forest.window_stats);
        assert_eq!(forest.shell_on_accent, forest.shell);
        let amber = Palette::new("amber", false);
        assert_eq!(amber.accent_fill, rgb(0xe9a23a));
        assert_eq!(amber.shell_field, amber.raised);
        assert_eq!(amber.shell_on_accent, amber.primary_ink);
        assert_eq!(amber.tally, rgb(0xd9c59f));
        assert_eq!(
            Palette::new("unknown", false),
            Palette::new("forest", false)
        );
        for (name, _) in PALETTES {
            assert_ne!(Palette::new(name, true), Palette::new(name, false));
        }
        assert_eq!(mix(rgb(0x000000), rgb(0xffffff), 0.5), rgb(0x808080));
    }
}
